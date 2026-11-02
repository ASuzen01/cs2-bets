"""Все клавиатуры бота. Интерфейс полностью кнопочный."""

from __future__ import annotations

from typing import Any, Mapping, Sequence

from aiogram.types import InlineKeyboardMarkup, WebAppInfo
from aiogram.utils.keyboard import InlineKeyboardBuilder

from app.config import settings
from app.texts import money, odds

AMOUNT_PRESETS: tuple[int, ...] = (50, 100, 250, 500, 1000)


def _clip(text: str, limit: int = 24) -> str:
    text = str(text).strip()
    return text if len(text) <= limit else text[: limit - 1] + "…"


# ------------------------------------------------------------------- меню
def main_menu_kb(is_admin: bool = False) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    if settings.miniapp_enabled:
        builder.button(text="📱 Открыть приложение", web_app=WebAppInfo(url=settings._normalize_url(settings.miniapp_url)))
    builder.button(text="🎮 Матчи и кэфы", callback_data="ml:0")
    builder.button(text="💰 Баланс", callback_data="b")
    builder.button(text="🎁 Бонус", callback_data="bn")
    builder.button(text="🎟 Мой экспресс", callback_data="xc")
    builder.button(text="🏆 Топ", callback_data="tp")
    builder.button(text="📜 История", callback_data="hs")
    builder.button(text="📖 Правила", callback_data="rl")
    if is_admin:
        builder.button(text="🛠 Результаты", callback_data="res")
        builder.button(text="🔄 Обновить линию", callback_data="as")
        builder.button(text="📊 Статистика", callback_data="ast")
        builder.button(text="⚙️ Автопост", callback_data="ap")
        builder.adjust(1, 1, 2, 2, 2, 2, 1, 1)
    elif settings.miniapp_enabled:
        builder.adjust(1, 1, 2, 2, 2)
    else:
        builder.adjust(1, 2, 2, 2)
    return builder.as_markup()


def back_to_menu_kb(is_admin: bool = False) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.button(text="🎮 Матчи", callback_data="ml:0")
    builder.button(text="⬅️ В меню", callback_data="menu")
    builder.adjust(2)
    return builder.as_markup()


# --------------------------------------------------------------- матчи
def matches_list_kb(rows: Sequence[Mapping[str, Any]], page: int, pages: int) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    for match in rows:
        builder.button(
            text=f"{_clip(match['team_a'], 14)} {odds(match['odds_a'])} — "
            f"{odds(match['odds_b'])} {_clip(match['team_b'], 14)}",
            callback_data=f"mc:{match['id']}",
        )
    builder.adjust(1)

    nav = InlineKeyboardBuilder()
    if page > 0:
        nav.button(text="⬅️ Назад", callback_data=f"ml:{page - 1}")
    nav.button(text=f"{page + 1}/{pages}", callback_data="noop")
    if page + 1 < pages:
        nav.button(text="Вперёд ➡️", callback_data=f"ml:{page + 1}")
    nav.button(text="🎟 Мой экспресс", callback_data="xc")
    nav.button(text="⬅️ В меню", callback_data="menu")
    nav.adjust(3, 1, 1)

    builder.attach(nav)
    return builder.as_markup()


def match_kb(match: Mapping[str, Any]) -> InlineKeyboardMarkup | None:
    """Карточка матча: исходы, добавление в экспресс, навигация."""
    builder = InlineKeyboardBuilder()

    if match["status"] == "open":
        builder.button(
            text=f"1️⃣ {_clip(match['team_a'], 18)} · {odds(match['odds_a'])}",
            callback_data=f"ms:{match['id']}:a",
        )
        builder.button(
            text=f"2️⃣ {_clip(match['team_b'], 18)} · {odds(match['odds_b'])}",
            callback_data=f"ms:{match['id']}:b",
        )
        builder.button(
            text=f"🎟 1 в экспресс · {odds(match['odds_a'])}",
            callback_data=f"xa:{match['id']}:a",
        )
        builder.button(
            text=f"🎟 2 в экспресс · {odds(match['odds_b'])}",
            callback_data=f"xa:{match['id']}:b",
        )
        builder.adjust(1, 1, 2)

    nav = InlineKeyboardBuilder()
    nav.button(text="⬅️ К матчам", callback_data="ml:0")
    nav.button(text="🎟 Экспресс", callback_data="xc")
    nav.adjust(2)
    builder.attach(nav)
    return builder.as_markup()


def amounts_kb(
    amounts: Sequence[int],
    make_callback,
    *,
    manual_callback: str,
    back_callback: str,
    all_callback: str | None = None,
) -> InlineKeyboardMarkup:
    """Выбор суммы кнопками. make_callback(amount) -> callback_data."""
    builder = InlineKeyboardBuilder()
    for amount in amounts:
        builder.button(text=money(amount), callback_data=make_callback(amount))
    builder.adjust(3)

    extra = InlineKeyboardBuilder()
    if all_callback:
        extra.button(text="💥 Всё", callback_data=all_callback)
    extra.button(text="✏️ Своя сумма", callback_data=manual_callback)
    extra.button(text="⬅️ Назад", callback_data=back_callback)
    extra.adjust(2 if all_callback else 1, 1)
    builder.attach(extra)
    return builder.as_markup()


# ------------------------------------------------------------- экспресс
def cart_kb(legs: Sequence[Mapping[str, Any]], amounts: Sequence[int]) -> InlineKeyboardMarkup:
    """Корзина: суммы ставки (если исходов ≥ 2) + управление черновиком."""
    builder = InlineKeyboardBuilder()
    ready = len(legs) >= 2
    if ready:
        for amount in amounts:
            builder.button(text=f"✅ Поставить {money(amount)}", callback_data=f"xm:{amount}")
        builder.adjust(2)

    nav = InlineKeyboardBuilder()
    nav.button(text="➕ Добавить исход", callback_data="ml:0")
    if legs:
        nav.button(text="🗑 Очистить", callback_data="xclear")
    nav.adjust(2)

    extra = InlineKeyboardBuilder()
    if ready:
        extra.button(text="✏️ Своя сумма", callback_data="xm_manual")
        extra.button(text="💥 Всё", callback_data="xm:all")
        extra.button(text="⬅️ В меню", callback_data="menu")
        extra.adjust(2, 1)
    else:
        extra.button(text="⬅️ В меню", callback_data="menu")
        extra.adjust(1)

    builder.attach(nav)
    builder.attach(extra)
    return builder.as_markup()


def admin_results_kb(rows: Sequence[Mapping[str, Any]]) -> InlineKeyboardMarkup:
    """Очередь подтверждения: по строке кнопок на каждый матч."""
    builder = InlineKeyboardBuilder()
    for match in rows:
        builder.button(text=f"✅ {_clip(match['team_a'], 12)}", callback_data=f"ase:{match['id']}:a")
        builder.button(text=f"✅ {_clip(match['team_b'], 12)}", callback_data=f"ase:{match['id']}:b")
        builder.button(text=f"⚪️ #{match['id']}", callback_data=f"av:{match['id']}")
        builder.adjust(2, 1)
    nav = InlineKeyboardBuilder()
    nav.button(text="🔄 Обновить", callback_data="res")
    nav.button(text="⬅️ В меню", callback_data="menu")
    nav.adjust(2)
    builder.attach(nav)
    return builder.as_markup()


# ------------------------------------------------------------- результат
def after_bet_kb() -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.button(text="🎮 Ещё ставка", callback_data="ml:0")
    builder.button(text="🎟 Экспресс", callback_data="xc")
    builder.button(text="💰 Баланс", callback_data="b")
    builder.adjust(3)
    return builder.as_markup()


def confirm_result_kb(match: Mapping[str, Any]) -> InlineKeyboardMarkup:
    """Подтверждение исхода админом (когда авто-расчёт не смог)."""
    builder = InlineKeyboardBuilder()
    builder.button(text=f"🏆 {_clip(match['team_a'], 18)}", callback_data=f"ase:{match['id']}:a")
    builder.button(text=f"🏆 {_clip(match['team_b'], 18)}", callback_data=f"ase:{match['id']}:b")
    builder.button(text="🚫 Матч не состоялся", callback_data=f"av:{match['id']}")
    builder.adjust(2, 1)
    return builder.as_markup()
