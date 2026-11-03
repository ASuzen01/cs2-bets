"""Личные уведомления игрокам: зашла ставка или нет.

Отдельный модуль, чтобы одинаково отправлять и из фонового синка (авто-расчёт),
и из админских действий (ручное подтверждение, отмена матча).
"""

from __future__ import annotations

import logging
from typing import Any

from aiogram import Bot
from aiogram.exceptions import TelegramAPIError
from aiogram.types import FSInputFile

from app import keyboards, texts
from app.assets import MASCOT_PATH
from app.db import Database

log = logging.getLogger(__name__)


async def send_with_mascot(bot: Bot, chat_id: int, caption: str, markup: Any = None) -> None:
    """Сообщение с маскотом-картинкой; если фото не ушло — отправляем текстом."""
    if MASCOT_PATH.is_file():
        try:
            await bot.send_photo(
                chat_id=chat_id,
                photo=FSInputFile(str(MASCOT_PATH)),
                caption=caption,
                reply_markup=markup,
            )
            return
        except TelegramAPIError as exc:
            log.debug("Фото-уведомление не ушло (%s), отправляю текстом", exc)
    await safe_send(bot, chat_id, caption, reply_markup=markup)


async def safe_send(bot: Bot, chat_id: int, text: str, **kwargs: Any) -> None:
    try:
        await bot.send_message(chat_id=chat_id, text=text, **kwargs)
    except TelegramAPIError as exc:
        log.warning("Не удалось отправить сообщение в чат %s: %s", chat_id, exc)


async def notify_bettors(
    bot: Bot,
    db: Database,
    result: dict[str, Any],
    *,
    score: str | None = None,
    void: bool = False,
) -> None:
    """Персональное сообщение по каждой одиночной ставке рассчитанного матча."""
    settlements = result.get("settlements") or []
    if not settlements:
        return

    team_a = str(result.get("team_a") or "")
    team_b = str(result.get("team_b") or "")
    markup = keyboards.app_kb()

    for item in settlements:
        user_id = int(item["user_id"])
        picked = team_a if item["side"] == "a" else team_b
        balance = await db.get_balance(user_id)
        text = texts.bet_result(
            won=bool(item["won"]) and not void,
            void=void,
            team_a=team_a,
            team_b=team_b,
            picked=picked,
            amount=int(item["amount"]),
            odds_value=float(item["odds"]),
            payout=int(item["payout"]),
            balance=balance,
            score=score,
        )
        if item["won"] and not void:
            await send_with_mascot(bot, user_id, text, markup)
        else:
            await safe_send(bot, user_id, text, reply_markup=markup)


async def notify_map_bettors(
    bot: Bot,
    db: Database,
    result: dict[str, Any],
    *,
    score: str | None = None,
) -> None:
    """Персональное сообщение по ставкам на конкретную карту."""
    settlements = result.get("settlements") or []
    if not settlements:
        return

    team_a = str(result.get("team_a") or "")
    team_b = str(result.get("team_b") or "")
    label = f"Карта {int(result.get('map_number') or 0)}"
    markup = keyboards.app_kb()

    for item in settlements:
        user_id = int(item["user_id"])
        picked = team_a if item["side"] == "a" else team_b
        balance = await db.get_balance(user_id)
        text = texts.bet_result(
            won=bool(item["won"]),
            void=False,
            team_a=team_a,
            team_b=team_b,
            picked=picked,
            amount=int(item["amount"]),
            odds_value=float(item["odds"]),
            payout=int(item["payout"]),
            balance=balance,
            score=score,
            market_label=label,
        )
        if item["won"]:
            await send_with_mascot(bot, user_id, text, markup)
        else:
            await safe_send(bot, user_id, text, reply_markup=markup)


__all__ = ["notify_bettors", "notify_map_bettors", "safe_send", "send_with_mascot"]
