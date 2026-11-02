"""Вход в бота: /start и /help.

Единственное, что делает бот, — даёт кнопку «Открыть приложение».
Все ставки, экспрессы, история и профиль живут в мини-приложении.
"""

from __future__ import annotations

from html import escape

from aiogram import Router
from aiogram.filters import Command, CommandStart
from aiogram.types import Message

from app import keyboards, texts
from app.config import settings
from app.db import Database

router = Router(name="common")

NO_APP_URL = (
    "Приложение пока не подключено: в переменных окружения пустой <code>MINIAPP_URL</code>.\n"
    "Задай адрес вида <code>https://имя-сервиса.onrender.com</code> и перезапусти бота."
)


def _name(message: Message) -> str:
    user = message.from_user
    return escape((user.first_name if user else None) or "друг")


@router.message(CommandStart())
async def cmd_start(message: Message, db: Database) -> None:
    balance = await db.get_balance(message.from_user.id)  # заодно создаёт игрока
    await message.answer(
        texts.START.format(name=_name(message), balance=texts.money(balance)),
        reply_markup=keyboards.app_kb(),
    )


@router.message(Command("help", "menu", "rules"))
async def cmd_help(message: Message) -> None:
    markup = keyboards.app_kb()
    await message.answer(texts.rules_text(), reply_markup=markup)
    if markup is None:
        await message.answer(NO_APP_URL)


__all__ = ["router", "NO_APP_URL", "settings"]
