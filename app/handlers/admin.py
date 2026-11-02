"""Единственная админская команда, у которой нет кнопки: /grant.

Обновление линии, статистика, автопост и подтверждение результатов — кнопками в меню.
"""

from __future__ import annotations

from aiogram import Router
from aiogram.filters import BaseFilter, Command, CommandObject
from aiogram.types import CallbackQuery, Message

from app import texts
from app.config import settings
from app.db import Database

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
        await message.reply("Такого игрока ещё нет в базе — он должен сначала написать боту.")
        return

    balance = await db.add_coins(user_id, amount, "начисление админом")
    await message.reply(
        f"Начислено {texts.money(amount)} игроку <code>{user_id}</code>. "
        f"Его баланс: {texts.money(balance)}"
    )
