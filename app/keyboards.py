"""Клавиатуры бота.

В боте осталась ровно одна кнопка — «Открыть приложение»: все ставки, экспрессы,
история и профиль живут в мини-аппе. Плюс админская клавиатура подтверждения
исхода, когда авто-расчёт не смог определить победителя.

Важно про группы: Telegram показывает кнопку `web_app` только в личке с ботом,
в групповом чате она не отрисовывается. Поэтому для групп используется обычная
URL-кнопка на диплинк `https://t.me/<бот>?start=app` — человек попадает в личку
с ботом и оттуда открывает приложение.
"""

from __future__ import annotations

from typing import Any, Mapping

from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton, WebAppInfo
from aiogram.utils.keyboard import InlineKeyboardBuilder

from app.config import settings


def app_url() -> str:
    """Адрес мини-приложения в виде https://… или пустая строка."""
    return settings._normalize_url(settings.miniapp_url)


def deep_link() -> str:
    """Ссылка-вход для групп: откроет личку с ботом, где уже есть кнопка приложения."""
    if not settings.bot_username:
        return ""
    # startapp открывает приложение сразу, но требует «Main Mini App» в BotFather;
    # start=app работает всегда — открывает личку с ботом, где уже есть кнопка.
    param = "startapp" if settings.miniapp_startapp else "start"
    return f"https://t.me/{settings.bot_username}?{param}=app"


def app_kb(*, group: bool = False) -> InlineKeyboardMarkup | None:
    """Кнопка приложения. В личке — WebApp, в группе — ссылка на бота."""
    url = app_url()
    if not url:
        return None

    if group:
        link = deep_link()
        if not link:
            return None
        return InlineKeyboardMarkup(
            inline_keyboard=[[InlineKeyboardButton(text="📱 Открыть приложение", url=link)]]
        )

    builder = InlineKeyboardBuilder()
    builder.button(text="📱 Открыть приложение", web_app=WebAppInfo(url=url))
    return builder.as_markup()


def _clip(text: str, limit: int = 18) -> str:
    text = str(text)
    return text if len(text) <= limit else text[: limit - 1] + "…"


def confirm_result_kb(match: Mapping[str, Any]) -> InlineKeyboardMarkup:
    """Подтверждение исхода админом (когда авто-расчёт не смог)."""
    builder = InlineKeyboardBuilder()
    builder.button(text=f"🏆 {_clip(match['team_a'])}", callback_data=f"ase:{match['id']}:a")
    builder.button(text=f"🏆 {_clip(match['team_b'])}", callback_data=f"ase:{match['id']}:b")
    builder.button(text="🚫 Матч не состоялся", callback_data=f"av:{match['id']}")
    builder.adjust(2, 1)
    return builder.as_markup()


__all__ = ["app_kb", "app_url", "deep_link", "confirm_result_kb"]
