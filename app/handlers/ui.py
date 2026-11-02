"""Кнопочный интерфейс: меню, матчи, ставки, экспресс, админ-экраны.

Ни одна операция не требует ввода команды: всё доступно инлайн-кнопками.
Текстовый ввод остаётся только для произвольной суммы (кнопка «Своя сумма»).
"""

from __future__ import annotations

import logging
import platform
import re
from typing import Any, TYPE_CHECKING

import aiogram
from aiogram import F, Router
from aiogram.enums import ChatType
from aiogram.filters import Command
from aiogram.exceptions import TelegramBadRequest
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, Message

from app import keyboards, texts
from app.config import settings
from app.db import (
    BettingError,
    Database,
    InsufficientFunds,
    InvalidBet,
    MatchNotFound,
    MatchNotOpen,
)
from app.services.cart import clear_draft, get_draft, leg_view, save_draft

if TYPE_CHECKING:
    from app.services.sync import MatchSyncer

log = logging.getLogger(__name__)
router = Router(name="ui")

PAGE_SIZE = 6
DIGITS_ONLY = re.compile(r"^\s*\d{1,7}\s*$")
SIDE_TITLES = {"a": "первую", "b": "вторую"}


class CustomFlow(StatesGroup):
    waiting_amount = State()


# ------------------------------------------------------------------ helpers
def _is_admin(user_id: int | None) -> bool:
    return bool(user_id) and user_id in settings.admin_ids_set


def _amounts(balance: int) -> list[int]:
    """Пресеты сумм: только те, что по карману (плюс всегда минимальная)."""
    presets = [amount for amount in keyboards.AMOUNT_PRESETS if amount <= balance]
    if not presets:
        presets = [settings.min_bet]
    return presets


async def _render(
    callback: CallbackQuery, text: str, markup: Any = None, *, disable_preview: bool = True
) -> None:
    """В личке редактируем сообщение, в группе — отвечаем новым (карточку не ломаем)."""
    message = callback.message
    if message is None:
        return

    chat_type = getattr(getattr(message, "chat", None), "type", ChatType.PRIVATE)
    if chat_type in (ChatType.GROUP, ChatType.SUPERGROUP):
        try:
            await message.answer(text, reply_markup=markup, disable_web_page_preview=disable_preview)
            return
        except TelegramBadRequest as exc:
            log.debug("Не смог ответить в группе: %s", exc)
            return

    try:
        await message.edit_text(text, reply_markup=markup, disable_web_page_preview=disable_preview)
    except TelegramBadRequest as exc:
        if "not modified" in str(exc).lower():
            return
        try:
            await message.answer(text, reply_markup=markup, disable_web_page_preview=disable_preview)
        except TelegramBadRequest:
            log.debug("Экран не отрисован: %s", exc)


async def _menu_screen(callback: CallbackQuery, state: FSMContext, db: Database) -> None:
    await state.clear()
    user = callback.from_user
    balance = await db.get_balance(user.id)
    name = user.first_name or "друг"
    await _render(
        callback,
        texts.menu_text(name, balance),
        keyboards.main_menu_kb(_is_admin(user.id)),
    )


async def _show_match(callback: CallbackQuery, state: FSMContext, db: Database, match_id: int) -> None:
    match = await db.get_match(match_id)
    if match is None:
        await callback.answer("Матч не найден", show_alert=True)
        return
    await _render(callback, texts.match_card(match), keyboards.match_kb(match))


# --------------------------------------------------------------------- меню
@router.callback_query(F.data == "noop")
async def cb_noop(callback: CallbackQuery) -> None:
    await callback.answer()


@router.callback_query(F.data == "menu")
async def cb_menu(callback: CallbackQuery, state: FSMContext, db: Database) -> None:
    await callback.answer()
    await _menu_screen(callback, state, db)


# ------------------------------------------------------------------- матчи
@router.callback_query(F.data.startswith("ml:"))
async def cb_matches(callback: CallbackQuery, state: FSMContext, db: Database) -> None:
    await callback.answer()
    await state.clear()

    try:
        page = max(0, int((callback.data or "ml:0").split(":")[1]))
    except (IndexError, ValueError):
        page = 0

    all_matches = await db.open_matches(limit=200)
    if not all_matches:
        await _render(callback, texts.no_matches_text(), keyboards.back_to_menu_kb())
        return

    pages = max(1, (len(all_matches) + PAGE_SIZE - 1) // PAGE_SIZE)
    page = min(page, pages - 1)
    chunk = all_matches[page * PAGE_SIZE : (page + 1) * PAGE_SIZE]

    await _render(
        callback,
        texts.matches_page_text(chunk, page, pages),
        keyboards.matches_list_kb(chunk, page, pages),
    )


@router.callback_query(F.data.startswith("mc:"))
async def cb_match_card(callback: CallbackQuery, state: FSMContext, db: Database) -> None:
    await callback.answer()
    try:
        match_id = int((callback.data or "mc:0").split(":")[1])
    except (IndexError, ValueError):
        return
    await _show_match(callback, state, db, match_id)


@router.callback_query(F.data.startswith("ms:"))
async def cb_choose_amount(callback: CallbackQuery, state: FSMContext, db: Database) -> None:
    await callback.answer()
    try:
        _, match_id_raw, side = (callback.data or "").split(":", 2)
        match_id = int(match_id_raw)
    except (ValueError, TypeError):
        return

    match = await db.get_match(match_id)
    if match is None:
        await callback.answer("Матч не найден", show_alert=True)
        return
    if match["status"] != "open":
        await callback.answer("Ставки по этому матчу закрыты", show_alert=True)
        return

    balance = await db.get_balance(callback.from_user.id)
    await state.update_data(kind="single", match_id=match_id, side=side)

    markup = keyboards.amounts_kb(
        _amounts(balance),
        lambda amount: f"ma:{match_id}:{side}:{amount}",
        manual_callback="ma_manual",
        back_callback=f"mc:{match_id}",
        all_callback=f"ma:{match_id}:{side}:all" if balance >= settings.min_bet else None,
    )
    await _render(callback, texts.amount_prompt_text(match, side, balance), markup)


# ------------------------------------------------------------ одиночная ставка
@router.callback_query(F.data.startswith("ma:"))
async def cb_place_bet(callback: CallbackQuery, state: FSMContext, db: Database) -> None:
    try:
        _, match_id_raw, side, amount_raw = (callback.data or "").split(":", 3)
        match_id = int(match_id_raw)
    except (ValueError, TypeError):
        await callback.answer("Кнопка устарела", show_alert=True)
        return

    match = await db.get_match(match_id)
    if match is None or match["status"] != "open":
        await callback.answer("Ставки по этому матчу закрыты", show_alert=True)
        return

    balance = await db.get_balance(callback.from_user.id)
    amount = balance if amount_raw == "all" else int(amount_raw)

    if amount < settings.min_bet:
        await callback.answer(f"Минимальная ставка — {texts.money(settings.min_bet)}", show_alert=True)
        return
    if amount > balance:
        await callback.answer(f"Не хватает монет. Баланс: {texts.money(balance)}", show_alert=True)
        return

    try:
        new_balance, bet_odds = await db.place_bet(
            match_id, callback.from_user.id, callback.message.chat.id if callback.message else None,
            side, amount,
        )
    except InsufficientFunds:
        await callback.answer("Не хватает монет", show_alert=True)
        return
    except (MatchNotOpen, MatchNotFound, InvalidBet) as exc:
        await callback.answer(str(exc), show_alert=True)
        return

    await callback.answer("Ставка принята ✅")
    await state.clear()
    await _render(
        callback,
        texts.bet_accepted(match, side, amount, bet_odds, new_balance),
        keyboards.after_bet_kb(),
    )


@router.callback_query(F.data == "ma_manual")
async def cb_manual_bet(callback: CallbackQuery, state: FSMContext) -> None:
    data = await state.get_data()
    if not data.get("match_id"):
        await callback.answer("Сначала выбери матч", show_alert=True)
        return
    await state.set_state(CustomFlow.waiting_amount)
    await callback.answer()
    await _render(
        callback,
        texts.ask_custom_amount_text("single"),
        keyboards.back_to_menu_kb(_is_admin(callback.from_user.id)),
    )


# ------------------------------------------------------------------ экспресс
@router.callback_query(F.data.startswith("xa:"))
async def cb_add_leg(callback: CallbackQuery, state: FSMContext, db: Database) -> None:
    try:
        _, match_id_raw, side = (callback.data or "").split(":", 2)
        match_id = int(match_id_raw)
    except (ValueError, TypeError):
        await callback.answer("Кнопка устарела", show_alert=True)
        return

    match = await db.get_match(match_id)
    if match is None or match["status"] != "open":
        await callback.answer("Ставки по этому матчу закрыты", show_alert=True)
        return

    legs = await get_draft(state)
    if any(leg["match_id"] == match_id for leg in legs):
        await callback.answer("Этот матч уже в экспрессе", show_alert=True)
        return
    if len(legs) >= settings.max_express_legs:
        await callback.answer(
            f"Максимум {settings.max_express_legs} исходов в экспрессе", show_alert=True
        )
        return

    legs.append(leg_view(match, side, texts.short_tournament))
    await save_draft(state, legs)
    await callback.answer("Добавлено в экспресс 🎟")

    balance = await db.get_balance(callback.from_user.id)
    await _render(
        callback,
        texts.cart_text(legs, balance),
        keyboards.cart_kb(legs, _amounts(balance)),
    )


@router.callback_query(F.data == "xc")
async def cb_cart(callback: CallbackQuery, state: FSMContext, db: Database) -> None:
    await callback.answer()
    legs = await get_draft(state)
    balance = await db.get_balance(callback.from_user.id)
    await _render(callback, texts.cart_text(legs, balance), keyboards.cart_kb(legs, _amounts(balance)))


@router.callback_query(F.data == "xclear")
async def cb_clear_cart(callback: CallbackQuery, state: FSMContext, db: Database) -> None:
    await clear_draft(state)
    await callback.answer("Экспресс очищен")
    balance = await db.get_balance(callback.from_user.id)
    await _render(callback, texts.cart_text([], balance), keyboards.cart_kb([], _amounts(balance)))


@router.callback_query(F.data.startswith("xm:"))
async def cb_place_express(callback: CallbackQuery, state: FSMContext, db: Database) -> None:
    legs = await get_draft(state)
    if len(legs) < 2:
        await callback.answer("Нужно минимум 2 исхода", show_alert=True)
        return

    balance = await db.get_balance(callback.from_user.id)
    amount_raw = (callback.data or "xm:0").split(":", 1)[1]
    amount = balance if amount_raw == "all" else int(amount_raw)

    if amount < settings.min_bet:
        await callback.answer(f"Минимальная ставка — {texts.money(settings.min_bet)}", show_alert=True)
        return
    if amount > balance:
        await callback.answer(f"Не хватает монет. Баланс: {texts.money(balance)}", show_alert=True)
        return

    try:
        express_id, total_odds, new_balance = await db.place_express(
            callback.from_user.id,
            callback.message.chat.id if callback.message else None,
            amount,
            [{"match_id": leg["match_id"], "side": leg["side"]} for leg in legs],
            max_odds=settings.max_express_odds,
        )
    except InsufficientFunds:
        await callback.answer("Не хватает монет", show_alert=True)
        return
    except (InvalidBet, MatchNotOpen, MatchNotFound) as exc:
        await callback.answer(f"{exc}", show_alert=True)
        return

    await state.clear()
    await callback.answer("Экспресс принят ✅")
    await _render(
        callback,
        texts.express_accepted(express_id, legs, amount, total_odds, new_balance),
        keyboards.after_bet_kb(),
    )


@router.callback_query(F.data == "xm_manual")
async def cb_manual_express(callback: CallbackQuery, state: FSMContext) -> None:
    legs = await get_draft(state)
    if len(legs) < 2:
        await callback.answer("Нужно минимум 2 исхода", show_alert=True)
        return
    await state.update_data(kind="express")
    await state.set_state(CustomFlow.waiting_amount)
    await callback.answer()
    await _render(
        callback,
        texts.ask_custom_amount_text("express"),
        keyboards.back_to_menu_kb(_is_admin(callback.from_user.id)),
    )


# ----------------------------------------------- ввод произвольной суммы
@router.message(CustomFlow.waiting_amount, Command("cancel", "отмена"))
async def custom_amount_cancel(message: Message, state: FSMContext, db: Database) -> None:
    await state.clear()
    balance = await db.get_balance(message.from_user.id)
    await message.answer(
        texts.menu_text(message.from_user.first_name or "друг", balance),
        reply_markup=keyboards.main_menu_kb(_is_admin(message.from_user.id)),
    )


@router.message(CustomFlow.waiting_amount, F.text.regexp(DIGITS_ONLY.pattern))
async def custom_amount_received(
    message: Message, state: FSMContext, db: Database
) -> None:
    data = await state.get_data()
    amount = int((message.text or "").strip())
    kind = data.get("kind", "single")

    if kind == "express":
        legs = await get_draft(state)
        if len(legs) < 2:
            await message.reply("Нужно минимум 2 исхода. Открой «🎟 Мой экспресс».")
            await state.clear()
            return
        try:
            express_id, total_odds, balance = await db.place_express(
                message.from_user.id,
                message.chat.id,
                amount,
                [{"match_id": leg["match_id"], "side": leg["side"]} for leg in legs],
                max_odds=settings.max_express_odds,
            )
        except BettingError as exc:
            await message.reply(f"Не получилось: {exc}")
            return
        await state.clear()
        await message.answer(
            texts.express_accepted(express_id, legs, amount, total_odds, balance),
            reply_markup=keyboards.after_bet_kb(),
        )
        return

    match = await db.get_match(int(data.get("match_id") or 0))
    side = str(data.get("side") or "a")
    if match is None or match["status"] != "open":
        await state.clear()
        await message.reply("Ставки по этому матчу закрыты.")
        return

    try:
        balance, bet_odds = await db.place_bet(
            int(match["id"]), message.from_user.id, message.chat.id, side, amount
        )
    except BettingError as exc:
        await message.reply(f"Не получилось: {exc}")
        return

    await state.clear()
    await message.answer(
        texts.bet_accepted(match, side, amount, bet_odds, balance),
        reply_markup=keyboards.after_bet_kb(),
    )


@router.message(CustomFlow.waiting_amount)
async def custom_amount_invalid(message: Message) -> None:
    await message.reply(
        f"Нужна сумма числом (мин. {texts.money(settings.min_bet)}). Отмена — /cancel"
    )


# --------------------------------------------------------- баланс и прочее
@router.callback_query(F.data == "b")
async def cb_balance(callback: CallbackQuery, state: FSMContext, db: Database) -> None:
    await callback.answer()
    await state.clear()
    user_id = callback.from_user.id
    balance = await db.get_balance(user_id)
    singles = await db.user_active_bets(user_id)
    expresses = await db.user_open_expresses(user_id)
    await _render(
        callback, texts.balance_text(balance, singles, expresses), keyboards.back_to_menu_kb()
    )


@router.callback_query(F.data == "bn")
async def cb_bonus(callback: CallbackQuery, state: FSMContext, db: Database) -> None:
    ok, wait_seconds, balance = await db.daily_bonus(
        callback.from_user.id, settings.daily_bonus, settings.bonus_cooldown_hours * 3600
    )
    if ok:
        await callback.answer(f"Бонус {texts.money(settings.daily_bonus)} зачислен 🎁")
        text = (
            f"🎁 Бонус получен: <b>{texts.money(settings.daily_bonus)}</b>\n"
            f"Баланс: {texts.money(balance)}"
        )
    else:
        hours, remainder = divmod(wait_seconds, 3600)
        await callback.answer("Бонус уже получен", show_alert=False)
        text = (
            f"⏳ Бонус уже получен. Следующий через {hours} ч {remainder // 60} мин.\n"
            f"Баланс: {texts.money(balance)}"
        )
    await _render(callback, text, keyboards.back_to_menu_kb(_is_admin(callback.from_user.id)))


@router.callback_query(F.data == "tp")
async def cb_top(callback: CallbackQuery, db: Database) -> None:
    await callback.answer()
    rows = await db.top_users(10)
    text = texts.top_text(rows) if rows else "Пока никто не играл. Открой «🎮 Матчи»."
    await _render(callback, text, keyboards.back_to_menu_kb())


@router.callback_query(F.data == "hs")
async def cb_history(callback: CallbackQuery, db: Database) -> None:
    await callback.answer()
    rows = await db.history(callback.from_user.id, 10)
    text = texts.history_text(rows) if rows else "История пуста — сделай первую ставку 🎮"
    await _render(callback, text, keyboards.back_to_menu_kb())


@router.callback_query(F.data == "rl")
async def cb_rules(callback: CallbackQuery) -> None:
    await callback.answer()
    await _render(callback, texts.rules_text(), keyboards.back_to_menu_kb())


# ------------------------------------------------------------- админ-кнопки
@router.callback_query(F.data == "as")
async def cb_sync(callback: CallbackQuery, syncer: "MatchSyncer" | None = None) -> None:
    if not _is_admin(callback.from_user.id):
        await callback.answer("Только для админа", show_alert=True)
        return
    if syncer is None:
        await callback.answer("Синхронизация недоступна", show_alert=True)
        return

    await callback.answer("Обновляю линию…")
    try:
        stats = await syncer.tick()
    except Exception as exc:  # noqa: BLE001
        await _render(callback, f"❌ Не смог обновить линию: <code>{exc}</code>", keyboards.back_to_menu_kb(True))
        return

    await _render(
        callback,
        "✅ <b>Линия обновлена</b>\n"
        f"Матчей тир-1: {stats['fetched']}\n"
        f"Новых: {stats['created']} · обновлено кэфов: {stats['updated']}\n"
        f"Закрыто ставок: {stats['closed']} · рассчитано: {stats['settled']}",
        keyboards.back_to_menu_kb(True),
    )


@router.callback_query(F.data == "res")
async def cb_results(callback: CallbackQuery, db: Database) -> None:
    if not _is_admin(callback.from_user.id):
        await callback.answer("Только для админа", show_alert=True)
        return
    await callback.answer()
    rows = await db.matches_by_status(["awaiting"])
    await _render(
        callback,
        texts.admin_results_text(rows),
        keyboards.admin_results_kb(rows),
    )


@router.callback_query(F.data == "ast")
async def cb_stats(callback: CallbackQuery, db: Database) -> None:
    if not _is_admin(callback.from_user.id):
        await callback.answer("Только для админа", show_alert=True)
        return
    await callback.answer()
    stats = await db.stats()
    await _render(
        callback,
        texts.stats_text(stats, settings.run_mode, platform.python_version(), aiogram.__version__),
        keyboards.back_to_menu_kb(True),
    )


@router.callback_query(F.data == "ap")
async def cb_autopost(callback: CallbackQuery, db: Database) -> None:
    if not _is_admin(callback.from_user.id):
        await callback.answer("Только для админа", show_alert=True)
        return

    chat = callback.message.chat if callback.message else None
    if chat is None or chat.type == ChatType.PRIVATE:
        await callback.answer("Кнопка работает в групповом чате", show_alert=True)
        return

    row = await db.get_chat(chat.id)
    current = bool(row["autopost"]) if row else True
    await db.set_autopost(chat.id, not current)
    await callback.answer("Автопост выключен" if current else "Автопост включён")
    await _render(
        callback,
        f"Автопост новых матчей в этом чате: <b>{'выключен' if current else 'включён'}</b>",
        keyboards.back_to_menu_kb(True),
    )


@router.callback_query(F.data.startswith("ase:"))
async def cb_admin_settle(callback: CallbackQuery, db: Database) -> None:
    if not _is_admin(callback.from_user.id):
        await callback.answer("Подтверждать результат может только админ", show_alert=True)
        return

    try:
        _, match_id_raw, side = (callback.data or "").split(":", 2)
        match_id = int(match_id_raw)
    except (ValueError, TypeError):
        await callback.answer("Кнопка устарела", show_alert=True)
        return

    try:
        result = await db.settle_match(match_id, side, source="manual")
    except BettingError as exc:
        await callback.answer(str(exc), show_alert=True)
        return

    await callback.answer("Готово")
    await _render(callback, texts.settled_text(result, match_id), keyboards.back_to_menu_kb(True))
    for express in result.get("expresses") or []:
        if callback.message is not None:
            await callback.message.answer(texts.express_result(express))


@router.callback_query(F.data.startswith("av:"))
async def cb_admin_void(callback: CallbackQuery, db: Database) -> None:
    if not _is_admin(callback.from_user.id):
        await callback.answer("Отменять матч может только админ", show_alert=True)
        return

    try:
        match_id = int((callback.data or "av:0").split(":", 1)[1])
    except (ValueError, IndexError):
        await callback.answer("Кнопка устарела", show_alert=True)
        return

    try:
        result = await db.cancel_match(match_id)
    except BettingError as exc:
        await callback.answer(str(exc), show_alert=True)
        return

    await callback.answer("Матч отменён")
    await _render(
        callback,
        f"⚪️ Матч #{match_id} отменён. Возвращено {texts.money(result['refunded'])} "
        f"({result['bets']} ставок).",
        keyboards.back_to_menu_kb(True),
    )
    for express in result.get("expresses") or []:
        if callback.message is not None:
            await callback.message.answer(texts.express_result(express))
