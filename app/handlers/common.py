"""Вход в бота: /start и /help. Оба показывают меню с кнопками — дальше только нажатия."""

from __future__ import annotations

from aiogram import Router
from aiogram.filters import Command, CommandStart
from aiogram.types import Message

from app import keyboards, texts
from app.config import settings
from app.db import Database

router = Router(name="common")


def _is_admin(user_id: int | None) -> bool:
    return bool(user_id) and user_id in settings.admin_ids_set


@router.message(CommandStart())
async def cmd_start(message: Message, db: Database) -> None:
    balance = await db.get_balance(message.from_user.id)
    user = message.from_user
    name = (user.first_name if user else None) or "друг"
    await message.answer(texts.START.format(name=name, balance=texts.money(balance)))
    await message.answer(
        texts.menu_text(name, balance),
        reply_markup=keyboards.main_menu_kb(_is_admin(user.id if user else None)),
    )


@router.message(Command("help", "menu"))
async def cmd_help(message: Message, db: Database) -> None:
    balance = await db.get_balance(message.from_user.id)
    user = message.from_user
    name = (user.first_name if user else None) or "друг"
    await message.answer(
        texts.menu_text(name, balance),
        reply_markup=keyboards.main_menu_kb(_is_admin(user.id if user else None)),
    )
