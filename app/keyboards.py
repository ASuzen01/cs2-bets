"""Клавиатуры бота.

В боте осталась ровно одна кнопка — «Открыть приложение»: все ставки, экспрессы,
история и профиль живут в мини-аппе. Плюс админская клавиатура подтверждения
исхода, когда авто-расчёт не смог определить победителя.
"""

from __future__ import annotations

from typing import Any, Mapping

from aiogram.types import InlineKeyboardMarkup, WebAppInfo
from aiogram.utils.keyboard import InlineKeyboardBuilder

from app.config import settings


def app_url() -> str:
    """Адрес мини-приложения в виде https://… или пустая строка."""
    return settings._normalize_url(settings.miniapp_url)


def app_kb() -> InlineKeyboardMarkup | None:
    """Единственная кнопка бота. None, если адрес приложения не задан."""
    url = app_url()
    if not url:
        return None
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


__all__ = ["app_kb", "app_url", "confirm_result_kb"]
