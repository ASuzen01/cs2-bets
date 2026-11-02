from __future__ import annotations

from aiogram import F, Router
from aiogram.enums import ChatType
from aiogram.types import Message

from app import keyboards

router = Router(name="misc")


@router.message(F.text, ~F.text.startswith("/"), F.chat.type == ChatType.PRIVATE)
async def private_fallback(message: Message) -> None:
    """Текст больше ни для чего не нужен: всё делается в приложении."""
    await message.answer(
        "Ставки, экспрессы, баланс и история — в приложении 🙂\n"
        "Кнопка ниже или «Ставки» слева внизу в строке ввода.",
        reply_markup=keyboards.app_kb(),
    )
