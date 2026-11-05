"""Купон в чат: /coupon 12 — бот присылает картинку ставки.

Зачем отдельная команда: купон живёт в истории приложения, а переслать его
другу удобнее из чата. Своя ставка — свой купон: чужой id не отдаём.
"""

from __future__ import annotations

import logging

from aiogram import Router
from aiogram.filters import Command, CommandObject
from aiogram.types import BufferedInputFile, Message

from app.db import Database
from app.services.coupon import item_of, render_coupon

log = logging.getLogger(__name__)
router = Router(name="coupon")

HINT = (
    "Картинка купона: <code>/coupon 12</code> — ставка, <code>/coupon express 5</code> — экспресс.\n"
    "Номер ставки виден в приложении в истории."
)


@router.message(Command("coupon"))
async def cmd_coupon(message: Message, command: CommandObject, db: Database) -> None:
    user = message.from_user
    if user is None:
        return
    args = (command.args or "").split()
    kind = "bet"
    raw_id = ""
    for token in args:
        if token in ("bet", "express"):
            kind = token
        elif token.isdigit():
            raw_id = token
    if not raw_id:
        await message.answer(HINT)
        return

    item = await item_of(db, user.id, kind, int(raw_id))
    if item is None and kind == "bet":
        # не угадали со ставкой — попробуем тот же номер среди экспрессов
        item = await item_of(db, user.id, "express", int(raw_id))
    if item is None:
        await message.answer("Такой ставки не нашёл — проверь номер в истории приложения.")
        return

    png = render_coupon(item)
    await message.answer_photo(
        BufferedInputFile(png, filename="coupon.png"),
        caption="Купон · Добрая лудка",
    )
