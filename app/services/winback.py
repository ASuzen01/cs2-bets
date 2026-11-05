"""Возврат игроков: кто не заходил три дня — получает фрибет 300 🪙 и сообщение.

Правила
-------
* отсутствие — от 3 дней (`ABSENT_DAYS`); долгий перерыв тоже считается;
* за одну паузу выдаётся ровно один фрибет; новая выдача возможна только после
  того, как игрок вернулся в приложение, а затем снова не заходил 3 дня;
* фрибет живёт 24 часа (`TTL_HOURS`), тратится только на ординар и только
  целиком: сумма ставки должна совпасть с номиналом;
* сообщение уходит раз в сутки на игрока — проверяем по таблице событий.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from typing import Any

from aiogram import Bot

from app.config import settings
from app.db import Database
from app.keyboards import app_kb

log = logging.getLogger(__name__)

EVENT = "winback"
ABSENT_DAYS = 3
TTL_HOURS = 24

TEXT = (
    "Давно тебя не было в «Доброй лудке»! Тебе зачислен фрибет 300 монет!\n\n"
    "Он ждёт в профиле: поставь его на любой ординар в линии. "
    f"Фрибет действует {TTL_HOURS} часа — потом сгорит.\n\n"
    "виртуальные монеты · без реальных ставок"
)


def freebet_amount() -> int:
    return int(settings.winback_freebet)


def expiry_from(now: int | None = None) -> int:
    return int(now if now is not None else time.time()) + TTL_HOURS * 3600


async def candidates(db: Database, now: int | None = None) -> list[Any]:
    """Кому пора написать: три дня тишины и не получал фрибет последний месяц."""
    moment = int(now if now is not None else time.time())
    return await db.users_to_winback(moment, ABSENT_DAYS)


async def notify_once(bot: Bot, db: Database, now: int | None = None) -> int:
    """Одно прохождение: выдаёт фрибеты и пишет игрокам. Возвращает число сообщений.

    Повтор защищён выборкой: `users_to_winback` не отдаёт тех, кому уже выдавали
    фрибет за последние 30 дней, — отдельная проверка по событиям не нужна.
    """
    moment = int(now if now is not None else time.time())
    sent = 0
    for row in await candidates(db, moment):
        user_id = int(row["user_id"])
        try:
            ticket_id = await db.grant_winback_ticket(
                user_id, freebet_amount(), moment, TTL_HOURS * 3600
            )
            if ticket_id is None:
                continue  # другой тик уже выдал награду или игрок снова активен
            # выдача уже зафиксирована атомарно; Telegram-ошибка не приведёт к дублю.
            await bot.send_message(user_id, TEXT, reply_markup=app_kb())
            sent += 1
        except Exception as exc:
            log.warning("Не смог вернуть игрока %s: %s", user_id, exc)
            continue
    return sent


class WinbackService:
    """Петля: раз в час смотрит, кто пропал, и выдаёт фрибет."""

    def __init__(self, bot: Bot, db: Database, interval_minutes: int = 60) -> None:
        self.bot = bot
        self.db = db
        self.interval = max(15, interval_minutes) * 60
        self._task: asyncio.Task[None] | None = None
        self.sent = 0
        self.last_at = 0
        self.last_error: str | None = None

    @property
    def stats(self) -> dict[str, Any]:
        return {
            "enabled": settings.winback_enabled,
            "interval_minutes": self.interval // 60,
            "absent_days": ABSENT_DAYS,
            "freebet": freebet_amount(),
            "sent": self.sent,
            "last_at": self.last_at or None,
            "last_error": self.last_error,
        }

    def start(self) -> None:
        if not settings.winback_enabled or self._task is not None:
            return
        self._task = asyncio.create_task(self._loop(), name="winback")

    async def stop(self) -> None:
        if self._task is None:
            return
        self._task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await self._task
        self._task = None

    async def _loop(self) -> None:
        await asyncio.sleep(45)
        while True:
            try:
                self.sent += await notify_once(self.bot, self.db)
                self.last_at = int(time.time())
                self.last_error = None
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self.last_error = str(exc)[:200]
                log.warning("Возврат игроков не сработал: %s", exc)
            await asyncio.sleep(self.interval)


WINBACK: WinbackService | None = None


def attach(bot: Bot, db: Database) -> WinbackService:
    global WINBACK
    if WINBACK is None:
        WINBACK = WinbackService(bot, db, settings.winback_interval_minutes)
    return WINBACK


def stats() -> dict[str, Any] | None:
    return WINBACK.stats if WINBACK is not None else None


__all__ = [
    "ABSENT_DAYS",
    "EVENT",
    "TEXT",
    "TTL_HOURS",
    "WINBACK",
    "WinbackService",
    "attach",
    "candidates",
    "expiry_from",
    "freebet_amount",
    "notify_once",
    "stats",
]
