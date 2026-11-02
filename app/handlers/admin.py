"""Админские команды бота.

В боте больше нет кнопочного меню: игроки всё делают в мини-приложении.
У админа остаются только служебные команды и подтверждение исходов:

    /grant 123456789 500   — начислить монеты
    /results               — матчи, которые ждут подтверждения исхода
    /stats                 — короткая сводка по базе

Кнопки «🏆 победитель» / «🚫 матч не состоялся» приходят вместе с уведомлением,
когда авто-расчёт не смог однозначно определить победителя.
"""

from __future__ import annotations

import logging
from typing import Any

from aiogram import F, Router
from aiogram.filters import BaseFilter, Command, CommandObject
from aiogram.types import CallbackQuery, Message

from app import keyboards, texts
from app.config import settings
from app.db import BettingError, Database

log = logging.getLogger(__name__)

router = Router(name="admin")


class IsAdmin(BaseFilter):
    async def __call__(self, event: Message | CallbackQuery) -> bool:
        user = event.from_user
        return bool(user and user.id in settings.admin_ids_set)


router.message.filter(IsAdmin())


@router.message(Command("grant"))
async def cmd_grant(message: Message, command: CommandObject, db: Database) -> None:
    tokens = (command.args or "").split()
    if len(tokens) != 2 or not all(token.lstrip("-").isdigit() for token in tokens):
        await message.reply("Формат: <code>/grant 123456789 500</code>")
        return

    user_id, amount = int(tokens[0]), int(tokens[1])
    if await db.get_user(user_id) is None:
        await message.reply("Такого игрока ещё нет в базе — он должен сначала открыть приложение.")
        return

    balance = await db.add_coins(user_id, amount, "начисление админом")
    await message.reply(
        f"Начислено {texts.money(amount)} игроку <code>{user_id}</code>. "
        f"Его баланс: {texts.money(balance)}"
    )


@router.message(Command("results"))
async def cmd_results(message: Message, db: Database) -> None:
    rows = await db.matches_by_status(["awaiting"])
    if not rows:
        await message.answer("Все матчи рассчитаны автоматически — вручную подтверждать нечего ✅")
        return

    await message.answer(f"Ждут подтверждения исхода: <b>{len(rows)}</b>")
    for row in rows[:10]:
        await message.answer(
            f"#{row['id']} {texts.short_tournament(str(row['tournament']))}\n"
            f"{row['team_a']} 🆚 {row['team_b']}",
            reply_markup=keyboards.confirm_result_kb(row),
        )


@router.message(Command("stats"))
async def cmd_stats(message: Message, db: Database) -> None:
    stats = await db.stats()
    lines = [f"<b>{key}</b>: {value}" for key, value in stats.items()]
    await message.answer("\n".join(lines) or "Статистика пуста")


async def _render(callback: CallbackQuery, text: str, markup: Any = None) -> None:
    """В личке правим сообщение, в группе — отвечаем новым."""
    message = callback.message
    if message is None:
        return
    chat_type = getattr(getattr(message, "chat", None), "type", "")
    if chat_type in ("group", "supergroup"):
        await message.answer(text, reply_markup=markup)
        return
    try:
        await message.edit_text(text, reply_markup=markup)
    except Exception as exc:
        if "not modified" in str(exc).lower():
            return
        try:
            await message.answer(text, reply_markup=markup)
        except Exception:
            log.debug("Не смог отрисовать админский экран: %s", exc)


@router.callback_query(F.data.startswith("ase:"))
async def cb_admin_settle(callback: CallbackQuery, db: Database) -> None:
    if not await IsAdmin()(callback):
        await callback.answer("Подтверждать результат может только админ", show_alert=True)
        return

    try:
        _, match_id_raw, side = (callback.data or "").split(":", 2)
        match_id = int(match_id_raw)
    except (ValueError, TypeError):
        await callback.answer("Кнопка устарела", show_alert=True)
        return

    try:
        result = await db.settle_match(match_id, side, source="manual")
    except BettingError as exc:
        await callback.answer(str(exc), show_alert=True)
        return

    await callback.answer("Готово")
    await _render(callback, texts.settled_text(result, match_id))
    if callback.message is not None:
        for express in result.get("expresses") or []:
            await callback.message.answer(texts.express_result(express))


@router.callback_query(F.data.startswith("av:"))
async def cb_admin_void(callback: CallbackQuery, db: Database) -> None:
    if not await IsAdmin()(callback):
        await callback.answer("Отменять матч может только админ", show_alert=True)
        return

    try:
        match_id = int((callback.data or "av:0").split(":", 1)[1])
    except (ValueError, IndexError):
        await callback.answer("Кнопка устарела", show_alert=True)
        return

    try:
        result = await db.cancel_match(match_id)
    except BettingError as exc:
        await callback.answer(str(exc), show_alert=True)
        return

    await callback.answer("Матч отменён")
    await _render(
        callback,
        f"⚪️ Матч #{match_id} отменён. Возвращено {texts.money(result['refunded'])} "
        f"({result['bets']} ставок).",
    )
    if callback.message is not None:
        for express in result.get("expresses") or []:
            await callback.message.answer(texts.express_result(express))
