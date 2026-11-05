"""Напоминание о старте матча, на который есть открытая ставка.

Игрок сделал ставку утром и закрыл приложение — матч начнётся без него. За 15
минут до старта бот пишет одно короткое сообщение: кто играет и что стоит.
Один матч — одно сообщение: метка живёт в events под именем `match_soon`.
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

log = logging.getLogger(__name__)

EVENT = "match_soon"
WINDOW_BEFORE = 20 * 60          # за сколько минут до старта писать
WINDOW_AFTER = 2 * 60            # и ещё чуть-чуть, если сервис проснулся позже


def _side_name(row: Any) -> str:
    """Человеческое имя исхода: команда или «ничья»."""
    side = str(row["side"])
    if side == "x":
        return "ничья"
    if side == "a":
        return str(row["team_a"])
    if side == "b":
        return str(row["team_b"])
    return "исход"


def text_for(kind: str, row: Any, minutes: int) -> str:
    match = f"{row['team_a']} — {row['team_b']}"
    stake = int(row["amount"])
    if kind == "express":
        return (
            f"⏳ Через {minutes} мин: <b>{match}</b>\n"
            f"Один из исходов твоего экспресса — {_side_name(row)}.\n"
            "Кэф зафиксирован, менять ничего не нужно."
        )
    return (
        f"⏳ Через {minutes} мин: <b>{match}</b>\n"
        f"Твоя ставка: {_side_name(row)} · {stake} 🪙.\n"
        "Кэф зафиксирован, менять ничего не нужно."
    )


async def _matches_soon(db: Database, now: int) -> list[Any]:
    """Матчи в окне старта, где у игроков есть открытые ставки."""
    return await db._fetchall(
        """
        SELECT DISTINCT m.id AS match_id FROM matches m
        WHERE m.status = 'open'
          AND m.start_time BETWEEN ? AND ?
          AND (
            EXISTS (SELECT 1 FROM bets b WHERE b.match_id = m.id AND b.cashed_out = 0)
            OR EXISTS (
                SELECT 1 FROM express_items i JOIN expresses e ON e.id = i.express_id
                WHERE i.match_id = m.id AND e.status = 'open'
            )
          )
        """,
        (now - WINDOW_AFTER, now + WINDOW_BEFORE),
    )


async def notify_once(bot: Bot, db: Database, now: int | None = None) -> int:
    """Одно прохождение: кому ещё не писали про этот матч — пишет. Возвращает число сообщений."""
    moment = int(now if now is not None else time.time())
    sent = 0
    for match in await _matches_soon(db, moment):
        match_id = int(match["match_id"])
        rows = await db._fetchall(
            """
            SELECT b.user_id AS user_id, b.side AS side, b.amount AS amount,
                   m.team_a AS team_a, m.team_b AS team_b, m.start_time AS start_time
            FROM bets b JOIN matches m ON m.id = b.match_id
            WHERE b.match_id = ? AND b.cashed_out = 0
            """,
            (match_id,),
        )
        if not rows:
            continue
        minutes = max(1, (int(rows[0]["start_time"]) - moment) // 60)
        for row in rows:
            user_id = int(row["user_id"])
            if await db.has_event(user_id, EVENT, str(match_id)):
                continue
            try:
                await bot.send_message(user_id, text_for("bet", row, minutes))
            except Exception as exc:
                log.warning("Не смог написать %s о старте матча %s: %s", user_id, match_id, exc)
                continue
            await db.log_event(user_id, EVENT, str(match_id))
            sent += 1
    return sent


class MatchNotifier:
    """Петля: раз в минуту проверяет, что стартует, и пишет тем, у кого ставка."""

    def __init__(self, bot: Bot, db: Database, interval_seconds: int = 60) -> None:
        self.bot = bot
        self.db = db
        self.interval = max(30, interval_seconds)
        self._task: asyncio.Task[None] | None = None
        self.sent = 0
        self.last_at = 0
        self.last_error: str | None = None

    @property
    def stats(self) -> dict[str, Any]:
        return {
            "enabled": settings.match_notify_enabled,
            "interval_seconds": self.interval,
            "sent": self.sent,
            "last_at": self.last_at or None,
            "last_error": self.last_error,
        }

    def start(self) -> None:
        if not settings.match_notify_enabled or self._task is not None:
            return
        self._task = asyncio.create_task(self._loop(), name="match-notify")

    async def stop(self) -> None:
        if self._task is None:
            return
        self._task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await self._task
        self._task = None

    async def _loop(self) -> None:
        await asyncio.sleep(30)
        while True:
            try:
                self.sent += await notify_once(self.bot, self.db)
                self.last_at = int(time.time())
                self.last_error = None
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self.last_error = str(exc)[:200]
                log.warning("Напоминание о старте матча не сработало: %s", exc)
            await asyncio.sleep(self.interval)


# один экземпляр на процесс, как и у напоминания о серии
NOTIFIER: MatchNotifier | None = None


def attach(bot: Bot, db: Database) -> MatchNotifier:
    global NOTIFIER
    if NOTIFIER is None:
        NOTIFIER = MatchNotifier(bot, db)
    return NOTIFIER


def stats() -> dict[str, Any] | None:
    return NOTIFIER.stats if NOTIFIER is not None else None


__all__ = [
    "EVENT",
    "NOTIFIER",
    "MatchNotifier",
    "attach",
    "notify_once",
    "stats",
    "text_for",
]
