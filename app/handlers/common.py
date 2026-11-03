"""Вход в бота: /start и /help.

Единственное, что делает бот, — даёт кнопку «Открыть приложение».
Все ставки, экспрессы, история и профиль живут в мини-приложении.

В личке кнопка открывает мини-апп напрямую (WebApp). В группе Telegram такие
кнопки не показывает, поэтому там отдаём ссылку на бота — она приводит человека
в личку, где кнопка приложения уже есть.
"""

from __future__ import annotations

from html import escape

from aiogram import Router
from aiogram.enums import ChatType
from aiogram.filters import Command, CommandStart
from aiogram.types import Message

from app import keyboards, texts
from app.assets import MASCOT_PATH
from app.config import settings
from app.db import Database
from app.services.notify import send_with_mascot

router = Router(name="common")

NO_APP_URL = (
    "Приложение пока не подключено: в переменных окружения пустой <code>MINIAPP_URL</code>.\n"
    "Задай адрес вида <code>https://имя-сервиса.onrender.com</code> и перезапусти бота."
)


def _is_group(message: Message) -> bool:
    return message.chat.type in (ChatType.GROUP, ChatType.SUPERGROUP)


def _name(message: Message) -> str:
    user = message.from_user
    return escape((user.first_name if user else None) or "друг")


@router.message(CommandStart())
async def cmd_start(message: Message, db: Database) -> None:
    group = _is_group(message)

    if group:
        # в группе баланс не показываем: он у каждого свой, а сообщение видят все
        if message.from_user is not None:
            await db.upsert_user(
                message.from_user.id, message.from_user.username, message.from_user.full_name
            )
            # понадобится, если личка закрыта: результат ставки отправим сюда
            await db.remember_chat(message.from_user.id, message.chat.id)
        await message.answer(
            texts.GROUP_START.format(name=_name(message)),
            reply_markup=keyboards.app_kb(group=True),
        )
        return

    balance = await db.get_balance(message.from_user.id)  # заодно создаёт игрока
    text = texts.START.format(name=_name(message), balance=texts.money(balance))
    if MASCOT_PATH.is_file() and message.bot is not None:
        # приветствие приходит картинкой с маскотом; если фото не ушло — текстом
        await send_with_mascot(message.bot, message.chat.id, text, keyboards.app_kb())
        return
    await message.answer(text, reply_markup=keyboards.app_kb())


@router.message(Command("me", "whoami", "id"))
async def cmd_me(message: Message) -> None:
    """Свой id и статус админа — по нему видно, доходят ли права."""
    user = message.from_user
    if user is None:
        return
    is_admin = user.id in settings.admin_ids_set
    lines = [f"🆔 Твой id: <code>{user.id}</code>"]
    if is_admin:
        lines.append("Права админа: <b>есть</b> ✅")
        lines.append("")
        lines.append("Начислить монеты: <code>/grant " + str(user.id) + " 500</code>")
        lines.append("Кому начислять: /players")
    else:
        lines.append("Права админа: <b>нет</b>")
        lines.append("")
        lines.append("Монеты начисляет админ — пришли ему свой id.")
    await message.answer("\n".join(lines))


@router.message(Command("help", "menu", "rules"))
async def cmd_help(message: Message) -> None:
    markup = keyboards.app_kb(group=_is_group(message))
    await message.answer(texts.rules_text(), reply_markup=markup)
    if markup is None:
        await message.answer(NO_APP_URL)


__all__ = ["NO_APP_URL", "router"]
