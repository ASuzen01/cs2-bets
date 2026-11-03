"""Резервные копии базы: снимок всех таблиц в один gzip-файл.

Формат намеренно простой и переносимый: JSON внутри gzip. Такой дамп читается
и на SQLite, и на Postgres, не требует pg_dump и живёт вне облака — копию можно
отправить в Telegram, положить на VPS или куда угодно ещё.
"""

from __future__ import annotations

import asyncio
import contextlib
import gzip
import json
import logging
import time
from html import escape
from typing import Any

from aiogram import Bot
from aiogram.exceptions import TelegramAPIError
from aiogram.types import BufferedInputFile

from app.config import settings
from app.db import Database
from app.db.database import BackupError

log = logging.getLogger(__name__)

BACKUP_VERSION = 1


def _checksum(tables: dict[str, list[dict[str, Any]]]) -> dict[str, int]:
    return {name: len(rows) for name, rows in tables.items()}


def encode(tables: dict[str, list[dict[str, Any]]], *, kind: str = "manual") -> bytes:
    """Собирает дамп: шапка с контрольными числами + все строки."""
    payload = {
        "app": "cs2-bets",
        "version": BACKUP_VERSION,
        "created_at": int(time.time()),
        "kind": kind,
        "counts": _checksum(tables),
        "tables": tables,
    }
    raw = json.dumps(payload, ensure_ascii=False, separators=(",", ":"), default=str)
    return gzip.compress(raw.encode("utf-8"), compresslevel=9)


def decode(blob: bytes) -> dict[str, Any]:
    """Разбирает и проверяет дамп до того, как им что-то затирать."""
    try:
        raw = gzip.decompress(blob).decode("utf-8")
    except (OSError, EOFError, UnicodeDecodeError) as exc:
        raise BackupError("Файл не похож на наш дамп (не gzip)") from exc
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise BackupError("Внутри файла битый JSON") from exc

    if payload.get("app") != "cs2-bets":
        raise BackupError("Это дамп другого приложения")
    if int(payload.get("version", 0)) > BACKUP_VERSION:
        raise BackupError("Дамп сделан более новой версией приложения")
    tables = payload.get("tables")
    if not isinstance(tables, dict):
        raise BackupError("В дампе нет таблиц")

    expected = payload.get("counts") or {}
    actual = _checksum(tables)
    for name, count in expected.items():
        if actual.get(name) != count:
            raise BackupError(f"Таблица {name} повреждена: {actual.get(name)} строк вместо {count}")
    return payload


async def make_dump(db: Database, *, kind: str = "manual") -> tuple[bytes, dict[str, Any]]:
    """Снимает дамп и пишет отметку в журнал копий."""
    tables = await db.dump_rows()
    blob = encode(tables, kind=kind)
    payload = decode(blob)
    users = len(tables.get("users", []))
    bets = len(tables.get("bets", []))
    await db.log_backup(len(blob), users, bets, kind)
    log.info("Бэкап готов: %.1f КБ, пользователей %s, ставок %s", len(blob) / 1024, users, bets)
    return blob, payload


async def restore(db: Database, blob: bytes, *, wipe: bool = True) -> dict[str, int]:
    """Возвращает базу из дампа. wipe=False — только досыпать недостающие строки."""
    payload = decode(blob)
    if not wipe:
        raise BackupError("Дозапись пока не поддержана — восстановление всегда полное")
    counts = await db.restore_rows(payload["tables"])
    log.warning("База восстановлена из дампа: %s", counts)
    return counts


def filename(created_at: int, *, kind: str = "backup") -> str:
    stamp = time.strftime("%Y-%m-%d_%H-%M", time.gmtime(created_at))
    return f"dobraya-ludka_{kind}_{stamp}.json.gz"


def summary(payload: dict[str, Any]) -> str:
    counts = payload.get("counts") or {}
    created = time.strftime("%d.%m.%Y %H:%M UTC", time.gmtime(int(payload.get("created_at", 0))))
    return (
        f"<b>Бэкап от {created}</b>\n"
        f"Игроков: {counts.get('users', 0)} · ставок: {counts.get('bets', 0)} · "
        f"матчей: {counts.get('matches', 0)} · операций: {counts.get('ledger', 0)}\n"
        f"Таблиц в дампе: {len(counts)}"
    )


def target_chat_id() -> int:
    """Куда шлём копии: отдельный чат/канал, если задан, иначе личка главного админа."""
    if settings.backup_chat_id:
        return int(settings.backup_chat_id)
    admins = settings.admin_ids_list
    return admins[0] if admins else 0    # первый в ADMIN_IDS — главный админ


async def send_dump(
    bot: Bot,
    chat_id: int,
    blob: bytes,
    payload: dict[str, Any],
    *,
    silent: bool = False,
) -> bool:
    """Отправляет файл копии в чат (обычно — в личку админу)."""
    document = BufferedInputFile(
        blob, filename=filename(int(payload.get("created_at", time.time())))
    )
    try:
        await bot.send_document(
            chat_id=chat_id,
            document=document,
            disable_notification=silent,   # в час по копии — без звука на телефоне
            caption=summary(payload) + "\n\nВосстановление: <code>/restore</code> в боте или "
            "<code>python scripts/restore_db.py файл</code>",
        )
        return True
    except TelegramAPIError as exc:
        log.error("Не удалось отправить бэкап в чат %s: %s", chat_id, escape(str(exc)))
        return False


class BackupService:
    """Раз в сутки снимает копию базы и присылает её админу в личку."""

    CHECK_EVERY = 15 * 60      # как часто проверяем, не пора ли

    def __init__(self, bot: Bot, db: Database) -> None:
        self.bot = bot
        self.db = db
        self._task: asyncio.Task[None] | None = None
        self.last_run_at: int = 0

    def start(self) -> None:
        if not settings.backup_enabled:
            log.info("Ежедневные бэкапы выключены (BACKUP_ENABLED=0)")
            return
        if not settings.admin_ids_set:
            log.warning("Некуда отправлять бэкапы: не задан ADMIN_IDS")
            return
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._run(), name="backup")

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
            self._task = None

    async def _run(self) -> None:
        while True:
            try:
                await self.run_if_due()
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("Не смог снять бэкап")
            await asyncio.sleep(self.CHECK_EVERY)

    async def run_if_due(self) -> bool:
        last = await self.db.last_backup()
        now = int(time.time())
        interval = max(1, settings.backup_interval_hours) * 3600
        if last is not None and now - int(last["created_at"]) < interval:
            return False
        return await self.send_now(kind="auto")

    async def send_now(self, *, kind: str = "manual") -> bool:
        blob, payload = await make_dump(self.db, kind=kind)
        chat_id = target_chat_id()
        if not chat_id:
            log.warning("Нет получателя для бэкапа: задай BACKUP_CHAT_ID или ADMIN_IDS")
            return False
        sent = await send_dump(self.bot, chat_id, blob, payload, silent=kind == "auto")
        if sent:
            self.last_run_at = int(payload["created_at"])
        return sent
