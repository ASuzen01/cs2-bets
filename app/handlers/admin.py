"""Админские команды бота.

В боте больше нет кнопочного меню: игроки всё делают в мини-приложении.
У админа остаются только служебные команды и подтверждение исходов:

    /grant 123456789 500   — начислить монеты (можно /grant @ник 500)
    /players               — список игроков с id, чтобы знать кому начислять
    /maps                  — карты, которые не удалось рассчитать автоматически
    /cleanup [часов]       — отменить зависшие матчи и вернуть ставки
    /results               — матчи, которые ждут подтверждения исхода
    /stats                 — короткая сводка по базе

Кнопки «🏆 победитель» / «🚫 матч не состоялся» приходят вместе с уведомлением,
когда авто-расчёт не смог однозначно определить победителя.
"""

from __future__ import annotations

import contextlib
import logging
from typing import Any

from aiogram import Bot, F, Router
from aiogram.filters import BaseFilter, Command, CommandObject
from aiogram.types import CallbackQuery, Message

from app import keyboards, texts
from app.config import settings
from app.db import BettingError, Database
from app.services.notify import notify_bettors, notify_map_bettors

log = logging.getLogger(__name__)

router = Router(name="admin")


def callback_bot(message: Message) -> Bot:
    """Бот из апдейта — нужен для уведомлений игрокам."""
    assert message.bot is not None
    return message.bot


class IsAdmin(BaseFilter):
    async def __call__(self, event: Message | CallbackQuery) -> bool:
        user = event.from_user
        return bool(user and user.id in settings.admin_ids_set)


router.message.filter(IsAdmin())


@router.message(Command("grant"))
async def cmd_grant(message: Message, command: CommandObject, db: Database) -> None:
    """Начислить монеты: /grant 123456789 500, /grant @nick 500 или /grant nick 500."""
    tokens = (command.args or "").split()
    amount_token = tokens[-1] if tokens else ""
    if len(tokens) != 2 or not amount_token.lstrip("-").isdigit():
        await message.reply(
            "Формат: <code>/grant 123456789 500</code> — id игрока и сумма.\n"
            "Можно и по нику: <code>/grant @nick 500</code>.\n"
            "Список игроков с id: /players"
        )
        return

    ident, amount = tokens[0], int(amount_token)
    user = await db.find_user(ident)
    if user is None:
        await message.reply(
            f"Игрок <code>{ident}</code> не найден в базе.\n"
            "Пусть друг откроет приложение через бота — тогда он появится в списке /players. "
            "Свой id он может узнать командой /me."
        )
        return

    user_id = int(user["user_id"])
    balance = await db.add_coins(user_id, amount, "начисление админом")
    name = user["full_name"] or (f"@{user['username']}" if user["username"] else f"id{user_id}")
    await message.reply(
        f"Начислено {texts.money(amount)} игроку <b>{name}</b> (<code>{user_id}</code>).\n"
        f"Его баланс: <b>{texts.money(balance)}</b>"
    )


@router.message(Command("players"))
async def cmd_players(message: Message, db: Database) -> None:
    """Кому начислять: последние активные игроки с id."""
    rows = await db.recent_users(limit=25)
    if not rows:
        await message.answer("В базе пока нет игроков — пусть друзья откроют приложение.")
        return

    lines = ["<b>Игроки</b> (кого видел бот последним):", ""]
    for row in rows:
        name = row["full_name"] or (f"@{row['username']}" if row["username"] else "без имени")
        lines.append(
            f"<code>{int(row['user_id'])}</code> · {name} · {texts.money(int(row['balance']))}"
        )
    lines.append("")
    lines.append("Начислить: <code>/grant 123456789 500</code>")
    await message.answer("\n".join(lines))


@router.message(Command("results"))
async def cmd_results(message: Message, db: Database) -> None:
    rows = await db.matches_by_status(["awaiting"])
    if not rows:
        await message.answer("Все матчи рассчитаны автоматически — вручную подтверждать нечего ✅")
        return

    await message.answer(f"Ждут подтверждения исхода: <b>{len(rows)}</b>")
    for row in rows[:10]:
        await message.answer(
            f"#{row['id']} {texts.short_tournament(str(row['tournament']))}\n"
            f"{row['team_a']} 🆚 {row['team_b']}",
            reply_markup=keyboards.confirm_result_kb(row),
        )


@router.message(Command("cleanup"))
async def cmd_cleanup(message: Message, command: CommandObject, db: Database) -> None:
    """Убрать зависшие матчи руками: /cleanup или /cleanup 2 (часов)."""
    arg = (command.args or "").strip()
    hours = int(arg) if arg.isdigit() else 3
    rows = await db.stale_matches(hours)
    if not rows:
        await message.answer(f"Зависших матчей старше {hours} ч нет ✅")
        return

    cancelled = 0
    refunded = 0
    for row in rows:
        try:
            result = await db.cancel_match(int(row["id"]))
        except BettingError:
            continue
        cancelled += 1
        refunded += int(result["refunded"])
        await notify_bettors(callback_bot(message), db, result, void=True)
        for card in await db.match_maps(int(row["id"])):
            if str(card["status"]) in ("open", "live", "awaiting"):
                with contextlib.suppress(BettingError):
                    await db.cancel_map(int(card["id"]))

    await message.answer(
        f"Отменил матчей: <b>{cancelled}</b>, вернул игрокам {texts.money(refunded)}.\n"
        "Ставки больше не висят в приложении."
    )


@router.message(Command("maps"))
async def cmd_maps(message: Message, db: Database) -> None:
    """Карты, по которым не удалось определить победителя автоматически."""
    rows = await db.maps_by_status(["awaiting"])
    if not rows:
        await message.answer("Все карты рассчитаны автоматически — подтверждать нечего ✅")
        return

    await message.answer(f"Карты без авторасчёта: <b>{len(rows)}</b>")
    for row in rows[:10]:
        match = await db.get_match(int(row["match_id"]))
        title = f"{match['team_a']} 🆚 {match['team_b']}" if match is not None else "матч удалён"
        score = f" · счёт в линии {row['score_a']}:{row['score_b']}" if row["score_a"] is not None and row["score_b"] is not None else ""
        await message.answer(
            f"#{row['id']} карта {row['map_number']}\n{title}{score}",
            reply_markup=keyboards.map_confirm_kb(row),
        )


@router.callback_query(F.data.startswith("ams:"))
async def on_map_confirm(callback: CallbackQuery, db: Database) -> None:
    """Админ нажал «кто взял карту»."""
    try:
        _, map_id, winner = str(callback.data).split(":")
        result = await db.settle_map(int(map_id), winner, source="admin")
    except BettingError as exc:
        await callback.answer(str(exc), show_alert=True)
        return

    await notify_map_bettors(callback.bot, db, result)
    await callback.message.edit_text(
        f"Карта {result['map_number']} рассчитана: победила <b>{result['team']}</b>\n"
        f"Выплачено: {texts.money(int(result['paid']))} · ставок: {result['bets']}"
    )
    await callback.answer("Готово")


@router.message(Command("stats"))
async def cmd_stats(message: Message, db: Database) -> None:
    stats = await db.stats()
    lines = [f"<b>{key}</b>: {value}" for key, value in stats.items()]
    await message.answer("\n".join(lines) or "Статистика пуста")


async def _render(callback: CallbackQuery, text: str, markup: Any = None) -> None:
    """В личке правим сообщение, в группе — отвечаем новым."""
    message = callback.message
    if message is None:
        return
    chat_type = getattr(getattr(message, "chat", None), "type", "")
    if chat_type in ("group", "supergroup"):
        await message.answer(text, reply_markup=markup)
        return
    try:
        await message.edit_text(text, reply_markup=markup)
    except Exception as exc:
        if "not modified" in str(exc).lower():
            return
        try:
            await message.answer(text, reply_markup=markup)
        except Exception:
            log.debug("Не смог отрисовать админский экран: %s", exc)


@router.callback_query(F.data.startswith("ase:"))
async def cb_admin_settle(callback: CallbackQuery, db: Database) -> None:
    if not await IsAdmin()(callback):
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
    await _render(callback, texts.settled_text(result, match_id))
    if callback.message is not None:
        for express in result.get("expresses") or []:
            await callback.message.answer(texts.express_result(express))


@router.callback_query(F.data.startswith("av:"))
async def cb_admin_void(callback: CallbackQuery, db: Database) -> None:
    if not await IsAdmin()(callback):
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
    if callback.bot is not None:
        await notify_bettors(callback.bot, db, result, void=True)
        # ставки на карты этого матча тоже возвращаем
        for card in await db.match_maps(match_id):
            if str(card["status"]) in ("open", "live", "awaiting"):
                try:
                    map_result = await db.cancel_map(int(card["id"]))
                except BettingError:
                    continue
                await notify_map_bettors(callback.bot, db, map_result, void=True)
    await _render(
        callback,
        f"⚪️ Матч #{match_id} отменён. Возвращено {texts.money(result['refunded'])} "
        f"({result['bets']} ставок).",
    )
    if callback.message is not None:
        for express in result.get("expresses") or []:
            await callback.message.answer(texts.express_result(express))
