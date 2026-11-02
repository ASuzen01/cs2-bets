from __future__ import annotations

from aiogram import F, Router
from aiogram.enums import ChatType
from aiogram.types import Message

router = Router(name="misc")


@router.message(F.text, ~F.text.startswith("/"), F.chat.type == ChatType.PRIVATE)
async def private_fallback(message: Message) -> None:
    """В личке отвечаем на непонятный текст, в группе — молчим, чтобы не спамить."""
    await message.answer(
        "Я не читаю текст — всё делается кнопками 🙂\n"
        "Жми /start, и дальше только нажатия.\n"
        "Текст нужен лишь для своей суммы (кнопка «✏️ Своя сумма»)."
    )
