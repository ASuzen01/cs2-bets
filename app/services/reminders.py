"""Напоминание о серии дней: «серия сгорит — забери бонус».

Зачем: серия множит ежедневный бонус, и потерять её обидно именно из-за
забывчивости. Раз в сутки, вечером по Москве, бот пишет тем, у кого серия уже
есть, а бонус за сегодня не взят. Одно сообщение на игрока в сутки — не спам:
событие пишем в `events`, повторная отправка невозможна.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from typing import Any

from aiogram import Bot
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup, WebAppInfo

from app import keyboards
from app.config import settings
from app.db import Database
from app.db.database import bonus_amount

log = logging.getLogger(__name__)

EVENT = "bonus_reminder"
# окно отправки (UTC): 16–19 — это 19–22 по Москве и Минску
WINDOW_START_UTC = 16
WINDOW_HOURS = 3


def in_window(now: float | None = None) -> bool:
    hour = time.gmtime(now if now is not None else time.time()).tm_hour
    return WINDOW_START_UTC <= hour < WINDOW_START_UTC + WINDOW_HOURS


def text_for(streak: int, amount: int) -> str:
    return (
        f"🔥 Серия {streak} — бонус сегодня {amount} монет.\n"
        "Забери до полуночи, иначе серия начнётся заново."
    )


async def remind_once(
    bot: Bot, db: Database, *, now: float | None = None, force: bool = False
) -> int:
    """Рассылает напоминания. Возвращает число отправленных сообщений."""
    moment = now if now is not None else time.time()
    if not force and not in_window(moment):
        return 0

    day = int(moment) // 86400
    rows = await db.users_to_remind(day, EVENT)
    sent = 0
    for row in rows:
        user_id = int(row["user_id"])
        streak = int(row["streak_days"] or 0)
        amount = bonus_amount(settings.daily_bonus, streak + 1)
        markup = None
        url = keyboards.app_url()
        if url:
            markup = InlineKeyboardMarkup(
                inline_keyboard=[
                    [InlineKeyboardButton(text="Забрать бонус", web_app=WebAppInfo(url=url))]
                ]
            )
        try:
            await bot.send_message(user_id, text_for(streak, amount), reply_markup=markup)
        except Exception as exc:            # заблокировал бота — не беда
            log.debug("Напоминание %s не ушло: %s", user_id, exc)
            continue
        await db.log_event(user_id, EVENT, str(streak))
        sent += 1
    return sent


class BonusReminder:
    """Фоновая петля: проверяет раз в `interval` минут, шлёт не больше одного раза."""

    def __init__(
        self,
        bot: Bot,
        db: Database,
        *,
        interval_minutes: int = 30,
        first_delay: int = 120,
    ) -> None:
        self.bot = bot
        self.db = db
        self.interval = max(5, int(interval_minutes)) * 60
        self.first_delay = first_delay
        self._task: asyncio.Task[None] | None = None
        self.sent = 0
        self.last_at: int = 0
        self.last_error: str | None = None

    @property
    def stats(self) -> dict[str, Any]:
        return {
            "enabled": settings.reminders_enabled,
            "interval_minutes": self.interval // 60,
            "sent": self.sent,
            "last_at": self.last_at or None,
            "last_error": self.last_error,
        }

    def start(self) -> None:
        if not settings.reminders_enabled or self._task is not None:
            return
        self._task = asyncio.create_task(self._loop(), name="bonus-reminder")

    async def stop(self) -> None:
        if self._task is None:
            return
        self._task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await self._task
        self._task = None

    async def _loop(self) -> None:
        await asyncio.sleep(self.first_delay)
        while True:
            try:
                self.sent += await remind_once(self.bot, self.db)
                self.last_at = int(time.time())
                self.last_error = None
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self.last_error = str(exc)[:200]
                log.warning("Напоминание о серии не сработало: %s", exc)
            await asyncio.sleep(self.interval)


# один экземпляр на процесс: создаётся на старте, когда уже есть бот и база
REMINDER: BonusReminder | None = None


def attach(bot: Bot, db: Database) -> BonusReminder:
    """Петля напоминаний на этом процессе (повторный вызов — та же петля)."""
    global REMINDER
    if REMINDER is None:
        REMINDER = BonusReminder(
            bot, db, interval_minutes=settings.reminders_interval_minutes
        )
    return REMINDER


def stats() -> dict[str, Any] | None:
    return REMINDER.stats if REMINDER is not None else None


__all__ = [
    "EVENT",
    "REMINDER",
    "BonusReminder",
    "attach",
    "in_window",
    "remind_once",
    "stats",
    "text_for",
]
