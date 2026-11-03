"""Личные уведомления игрокам: зашла ставка или нет.

Отдельный модуль, чтобы одинаково отправлять и из фонового синка (авто-расчёт),
и из админских действий (ручное подтверждение, отмена матча).
"""

from __future__ import annotations

import logging
from html import escape
from typing import Any

from aiogram import Bot
from aiogram.exceptions import TelegramAPIError, TelegramForbiddenError
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


async def send_user(
    bot: Bot,
    db: Database,
    user_id: int,
    text: str,
    markup: Any = None,
    *,
    with_mascot: bool = False,
) -> bool:
    """Сообщение игроку в личку. Если личка закрыта — пишем в чат, откуда он заходил."""
    if with_mascot and MASCOT_PATH.is_file():
        try:
            await bot.send_photo(
                chat_id=user_id,
                photo=FSInputFile(str(MASCOT_PATH)),
                caption=text,
                reply_markup=markup,
            )
            return True
        except TelegramForbiddenError:
            pass
        except TelegramAPIError as exc:
            log.debug("Фото в личку не ушло (%s), пробую текстом", exc)

    try:
        await bot.send_message(chat_id=user_id, text=text, reply_markup=markup)
        return True
    except TelegramForbiddenError:
        log.info("Игрок %s не открывал личку — пишу в общий чат", user_id)
    except TelegramAPIError as exc:
        log.warning("Не смог написать игроку %s: %s", user_id, exc)
        return False

    row = await db.get_user(user_id)
    chat_id = dict(row).get("last_chat_id") if row is not None else None
    if not chat_id:
        log.warning("Игроку %s не доставить уведомление: личка закрыта, чата нет", user_id)
        return False

    name = (dict(row).get("full_name") or "игрок") if row is not None else "игрок"
    mention = f'<a href="tg://user?id={user_id}">{escape(str(name))}</a>, '
    await safe_send(bot, int(chat_id), mention + text, reply_markup=markup)
    return True


async def safe_send(bot: Bot, chat_id: int, text: str, **kwargs: Any) -> None:
    try:
        await bot.send_message(chat_id=chat_id, text=text, **kwargs)
    except TelegramAPIError as exc:
        log.warning("Не удалось отправить сообщение в чат %s: %s", chat_id, exc)


def _side_label(side: str, team_a: str, team_b: str) -> str:
    """Что именно поставил игрок: команда, ничья или конкретный рынок."""
    if side == "a":
        return team_a
    if side == "b":
        return team_b
    if side == "x":
        return "Ничья"
    from app.services.football import market_label, parse_side

    parsed = parse_side(side)
    return market_label(parsed[0], parsed[1]) if parsed else side


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
        picked = item.get("label") or _side_label(str(item["side"]), team_a, team_b)
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
        await send_user(bot, db, user_id, text, markup, with_mascot=bool(item["won"]) and not void)


async def notify_map_bettors(
    bot: Bot,
    db: Database,
    result: dict[str, Any],
    *,
    score: str | None = None,
    void: bool = False,
) -> None:
    """Персональное сообщение по ставкам на конкретную карту (или возврат по ней)."""
    settlements = result.get("settlements") or []
    if not settlements:
        return

    team_a = str(result.get("team_a") or "")
    team_b = str(result.get("team_b") or "")
    label = f"Карта {int(result.get('map_number') or 0)}"
    markup = keyboards.app_kb()

    for item in settlements:
        user_id = int(item["user_id"])
        picked = item.get("label") or _side_label(str(item["side"]), team_a, team_b)
        balance = await db.get_balance(user_id)
        text = texts.bet_result(
            won=bool(item["won"]),
            void=void,
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
        await send_user(bot, db, user_id, text, markup, with_mascot=bool(item["won"]) and not void)


__all__ = [
    "notify_bettors",
    "notify_map_bettors",
    "safe_send",
    "send_user",
    "send_with_mascot",
]
