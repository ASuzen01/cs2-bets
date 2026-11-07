"""Админские команды бота.

В боте больше нет кнопочного меню: игроки всё делают в мини-приложении.
У админа остаются только служебные команды и подтверждение исходов:

    /grant 123456789 500   — начислить монеты (можно /grant @ник 500)
    /freebet 123456789 500 — выдать ординарный фрибет заданного номинала
    /freebet_express 123456789 500 — выдать билет-фрибет на экспресс
    /version               — версия, коммит, uptime и backend
    /services              — фоновые задачи и состояние синхронизации
    /player 123456789      — профиль и последние операции игрока
    /player_stats 123456789 — оборот, выплаты, ROI и winrate игрока
    /notify 123456789 текст — отправить личное уведомление игроку
    /bettrace 42           — трассировка ставки, экспресса или казино
    /feed                  — статус спортивной линии
    /players               — список игроков с id, чтобы знать кому начислять
    /maps                  — карты, которые не удалось рассчитать автоматически
    /cleanup [часов]       — рассчитать зависшие матчи, остальные отменить с возвратом
    /recount ID 1|2        — пересчитать отменённый матч по реальному исходу
    /backup [status]       — снять копию базы и прислать файлом
    (файл .json.gz в личку) — восстановить базу из копии
    /results               — матчи, которые ждут подтверждения исхода
    /hockey ID 2:2 3:2     — рассчитать хоккей: счёт основного времени и итоговый
    /stats                 — короткая сводка по базе

Кнопки «🏆 победитель» / «🚫 матч не состоялся» приходят вместе с уведомлением,
когда авто-расчёт не смог однозначно определить победителя.
"""

from __future__ import annotations

import contextlib
import html
import logging
import os
import platform
import time
from io import BytesIO
from typing import Any

from aiogram import Bot, F, Router
from aiogram.exceptions import TelegramAPIError
from aiogram.filters import BaseFilter, Command, CommandObject
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import BufferedInputFile, CallbackQuery, InlineKeyboardMarkup, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder

from app import keyboards, texts
from app.config import settings
from app.db import BettingError, Database
from app.db.database import BACKUP_TABLES, BackupError
from app.services import matchnotify, reminders, winback
from app.services.backup import (
    BackupService,
    decode,
    make_dump,
    restore,
    send_dump,
    summary,
)
from app.services.maps import finish_maps_of_match
from app.services.notify import notify_bettors, notify_map_bettors
from app.services.recheck import recheck_recent, report_text
from app.services.sync import series_from_maps

log = logging.getLogger(__name__)
MODULE_STARTED_AT = int(time.time())

router = Router(name="admin")


class AdminPlayerFlow(StatesGroup):
    waiting_player = State()
    waiting_manual_id = State()
    waiting_amount = State()


def callback_bot(message: Message) -> Bot:
    """Бот из апдейта — нужен для уведомлений игрокам."""
    assert message.bot is not None
    return message.bot


def _player_picker_keyboard(rows: list[Any]) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    for row in rows:
        user_id = int(row["user_id"])
        name = str(row["full_name"] or (f"@{row['username']}" if row["username"] else "игрок"))
        label = f"{user_id} · {name}"
        builder.button(text=label[:64], callback_data=f"admin_flow:player:{user_id}")
    builder.button(text="Ввести ID вручную", callback_data="admin_flow:manual")
    builder.button(text="Отмена", callback_data="admin_flow:cancel")
    builder.adjust(1)
    return builder.as_markup()


def _cancel_keyboard() -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.button(text="Отмена", callback_data="admin_flow:cancel")
    return builder.as_markup()


async def _begin_player_flow(
    message: Message, state: FSMContext, db: Database, action: str
) -> None:
    await state.clear()
    await state.update_data(action=action)
    await state.set_state(AdminPlayerFlow.waiting_player)
    rows = await db.recent_users(limit=12)
    await message.answer(
        "Выберите игрока по ID или введите ID вручную:",
        reply_markup=_player_picker_keyboard(rows),
    )


async def _begin_amount_flow(
    message: Message, state: FSMContext, action: str, user: Any
) -> None:
    await state.clear()
    await state.update_data(action=action, target_user_id=int(user["user_id"]))
    await state.set_state(AdminPlayerFlow.waiting_amount)
    name = user["full_name"] or (f"@{user['username']}" if user["username"] else "игрок")
    await message.answer(
        f"Игрок: {html.escape(str(name))} (<code>{int(user['user_id'])}</code>). "
        "Введите сумму монет:",
        reply_markup=_cancel_keyboard(),
    )


class IsAdmin(BaseFilter):
    async def __call__(self, event: Message | CallbackQuery) -> bool:
        user = event.from_user
        return bool(user and user.id in settings.admin_ids_set)


router.message.filter(IsAdmin())


@router.message(Command("best"))
async def cmd_best(message: Message, command: CommandObject, db: Database) -> None:
    """Купон дня: лучший выигрыш за сутки — в канал (или ответом админу).

    Параметр — сколько часов назад смотреть: /best 48.
    """
    from app.services.coupon import item_of, render_coupon

    hours = 24
    args = (command.args or "").strip()
    if args.isdigit():
        hours = max(1, min(24 * 14, int(args)))

    best = await db.best_win_since(int(time.time()) - hours * 3600)
    if best is None:
        await message.answer(f"За последние {hours} ч выигрышей не было — постить нечего.")
        return

    item = await item_of(db, int(best["user_id"]), str(best["kind"]), int(best["id"]))
    if item is None:
        await message.answer("Ставка нашлась, но купон по ней собрать не удалось.")
        return

    png = render_coupon(item)
    caption = f"🔥 Купон дня: +{int(best['payout']) - int(item.get('stake') or 0)} чистыми"
    photo = BufferedInputFile(png, filename="best.png")
    target = settings.channel_id
    if target:
        try:
            sent = await callback_bot(message).send_photo(target, photo, caption=caption)
            await message.answer(f"Опубликовал в канал: message #{sent.message_id}")
            return
        except Exception as exc:
            await message.answer(f"В канал не ушло ({exc}) — вот купон, отправь руками.")
    await message.answer_photo(photo, caption=caption)


async def _apply_coin_grant(message: Message, db: Database, user: Any, amount: int) -> None:
    user_id = int(user["user_id"])
    balance = await db.add_coins(user_id, amount, "начисление админом")
    name = user["full_name"] or (f"@{user['username']}" if user["username"] else f"id{user_id}")
    await message.reply(
        f"Начислено {texts.money(amount)} игроку <b>{html.escape(str(name))}</b> "
        f"(<code>{user_id}</code>).\nБаланс: <b>{texts.money(balance)}</b>"
    )


async def _apply_freebet(
    message: Message, db: Database, user: Any, amount: int, kind: str = "single"
) -> None:
    user_id = int(user["user_id"])
    if message.from_user is None:
        await message.reply("Не удалось определить администратора; билет не выдан.")
        return
    request_key = f"{message.from_user.id}:{message.chat.id}:{message.message_id}:{kind}:{amount}"
    ticket_id, status = await db.grant_admin_ticket(
        user_id,
        amount,
        kind,
        request_key,
        ttl_seconds=winback.TTL_HOURS * 3600,
    )
    if status != "granted" or ticket_id is None:
        messages = {
            "duplicate": "Эта команда уже обработана; повторный билет не выдаю.",
            "not_found": "Игрок не найден в базе.",
        }
        await message.reply(messages.get(status, "Не удалось выдать билет."))
        return

    kind_name = "экспресс-билет" if kind == "express" else "фрибет"
    use_hint = "на любой экспресс" if kind == "express" else "на любой ординар в линии"
    try:
        await callback_bot(message).send_message(
            user_id,
            f"Тебе выдан {kind_name} на {amount} монет! Он доступен в профиле и действует "
            f"{winback.TTL_HOURS} часа. Поставь его целиком {use_hint}.\n\n"
            "виртуальные монеты · без реальных ставок",
            reply_markup=keyboards.app_kb(),
        )
    except Exception as exc:
        log.warning("Фрибет #%s выдан игроку %s, но сообщение не отправлено: %s",
                    ticket_id, user_id, exc)
        await message.reply(
            f"{kind_name.capitalize()} {amount} выдан (билет #{ticket_id}), "
            "но уведомление отправить не удалось."
        )
        return

    name = user["full_name"] or (f"@{user['username']}" if user["username"] else "игрок")
    await message.reply(
        f"{kind_name.capitalize()} на {amount} монет на 24 часа выдан игроку "
        f"{html.escape(str(name))} (<code>{user_id}</code>), билет #{ticket_id}. "
        "Уведомление отправлено."
    )


async def _resolve_amount_target(
    message: Message, state: FSMContext, db: Database, action: str, ident: str
) -> None:
    user = await db.find_user(ident)
    if user is None:
        await message.reply(f"Игрок <code>{html.escape(ident)}</code> не найден. Выберите другого или начните команду заново.")
        return
    await _begin_amount_flow(message, state, action, user)


@router.message(Command("grant"))
async def cmd_grant(
    message: Message, command: CommandObject, db: Database, state: FSMContext
) -> None:
    """Начислить монеты: выбор игрока и суммы либо /grant ID|ник СУММА."""
    tokens = (command.args or "").split()
    if not tokens:
        await _begin_player_flow(message, state, db, "grant")
        return
    if len(tokens) not in (1, 2):
        await message.reply("Формат: /grant — выбрать игрока и сумму; или /grant ID СУММА.")
        return
    if len(tokens) == 1:
        await _resolve_amount_target(message, state, db, "grant", tokens[0])
        return

    if not tokens[1].lstrip("-").isdigit():
        await message.reply("Сумма должна быть целым числом. Формат: /grant ID СУММА.")
        return
    user = await db.find_user(tokens[0])
    if user is None:
        await message.reply(f"Игрок <code>{html.escape(tokens[0])}</code> не найден.")
        return
    await _apply_coin_grant(message, db, user, int(tokens[1]))


@router.message(Command("freebet"))
async def cmd_freebet(
    message: Message, command: CommandObject, db: Database, state: FSMContext
) -> None:
    """Админский фрибет: выбрать игрока и сумму либо /freebet ID СУММА."""
    args = (command.args or "").split()
    if not args:
        await _begin_player_flow(message, state, db, "freebet")
        return
    if len(args) not in (1, 2) or not args[0].isdigit():
        await message.reply("Формат: /freebet — выбрать игрока и сумму; или /freebet ID СУММА.")
        return

    user_id = int(args[0])
    if user_id <= 0 or user_id > 2**63 - 1:
        await message.reply("Укажи корректный положительный Telegram ID.")
        return
    user = await db.get_user(user_id)
    if user is None:
        await message.reply(f"Игрок <code>{user_id}</code> не найден.")
        return
    if len(args) == 1:
        await _begin_amount_flow(message, state, "freebet", user)
        return
    if not args[1].isdigit() or not 1 <= int(args[1]) <= 2**31 - 1:
        await message.reply("Сумма должна быть от 1 до 2 147 483 647 монет.")
        return
    await _apply_freebet(message, db, user, int(args[1]), kind="single")


@router.message(Command("freebet_express"))
async def cmd_freebet_express(
    message: Message, command: CommandObject, db: Database, state: FSMContext
) -> None:
    """Выдать фрибет на экспресс: выбрать игрока/сумму либо передать аргументы."""
    args = (command.args or "").split()
    if not args:
        await _begin_player_flow(message, state, db, "freebet_express")
        return
    if len(args) not in (1, 2) or not args[0].isdigit():
        await message.reply(
            "Формат: /freebet_express — выбрать игрока и сумму; "
            "или /freebet_express ID СУММА."
        )
        return

    user_id = int(args[0])
    if user_id <= 0 or user_id > 2**63 - 1:
        await message.reply("Укажи корректный положительный Telegram ID.")
        return
    user = await db.get_user(user_id)
    if user is None:
        await message.reply(f"Игрок <code>{user_id}</code> не найден.")
        return
    if len(args) == 1:
        await _begin_amount_flow(message, state, "freebet_express", user)
        return
    if not args[1].isdigit() or not 1 <= int(args[1]) <= 2**31 - 1:
        await message.reply("Сумма должна быть от 1 до 2 147 483 647 монет.")
        return
    await _apply_freebet(message, db, user, int(args[1]), kind="express")


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


@router.message(Command("notify", "notify_player"))
async def cmd_notify_player(
    message: Message, command: CommandObject, db: Database
) -> None:
    """Send a plain-text personal notification: /notify ID|@username text."""
    if not _private_admin_report_chat(message):
        await message.reply("Отправлять личные уведомления можно только в личном чате с ботом.")
        return

    args = (command.args or "").strip()
    ident, separator, text = args.partition(" ")
    text = text.strip()
    if not separator or not ident or not text:
        await message.reply(
            "Формат: <code>/notify ID|@username текст уведомления</code>\n"
            "Пример: <code>/notify 752192302 Матч уже начался!</code>"
        )
        return

    safe_text = html.escape(text)
    if len(safe_text) > 3900:
        await message.reply("Текст слишком длинный: максимум 3900 символов после обработки.")
        return

    user = await db.find_user(ident)
    if user is None:
        await message.reply(f"Игрок <code>{html.escape(ident)}</code> не найден.")
        return

    user_id = int(user["user_id"])
    name = user["full_name"] or (f"@{user['username']}" if user["username"] else "игрок")
    try:
        await callback_bot(message).send_message(chat_id=user_id, text=safe_text)
    except TelegramAPIError as exc:
        log.warning("Admin notification to player %s failed: %s", user_id, exc)
        await message.answer(
            f"Telegram не доставил уведомление игроку {html.escape(str(name))} "
            f"(<code>{user_id}</code>). Проверьте, не заблокировал ли он бота."
        )
        return

    admin_id = int(message.from_user.id) if message.from_user is not None else 0
    await db.log_event(user_id, "admin_notification", f"admin={admin_id}")
    await message.answer(
        f"Уведомление игроку {html.escape(str(name))} (<code>{user_id}</code>) отправлено."
    )


@router.message(Command("results"))
async def cmd_results(message: Message, db: Database) -> None:
    rows = await db.matches_by_status(["awaiting"])
    if not rows:
        await message.answer("Все матчи рассчитаны автоматически — вручную подтверждать нечего ✅")
        return

    await message.answer(f"Ждут подтверждения исхода: <b>{len(rows)}</b>")
    for row in rows[:10]:
        title = (
            f"#{row['id']} {texts.short_tournament(str(row['tournament']))}\n"
            f"{row['team_a']} 🆚 {row['team_b']}"
        )
        if str(row["sport"]) == "hockey":
            # у хоккея два счёта, кнопками «победила первая/вторая» не обойтись
            await message.answer(
                f"{title}\n🏒 Хоккей. Расчёт: "
                f"<code>/hockey {row['id']} 2:2 3:2</code>\n"
                "(основное время, затем итог с овертаймом и буллитами)"
            )
            continue
        await message.answer(title, reply_markup=keyboards.confirm_result_kb(row))


def _parse_score(token: str) -> tuple[int, int] | None:
    """«3:2» или «3-2» → (3, 2)."""
    for sep in (":", "-", "х", "x"):
        if sep in token:
            left, _, right = token.partition(sep)
            if left.isdigit() and right.isdigit():
                return int(left), int(right)
            return None
    return None


@router.message(Command("hockey"))
async def cmd_hockey(message: Message, command: CommandObject, db: Database) -> None:
    """Ручной расчёт хоккея: /hockey ID 2:2 3:2 (основное время и итог).

    КХЛ автоматически не считается — фид результатов её не отдаёт. Если матч
    закончился в основное время, хватит одного счёта: /hockey ID 4:1.
    """
    tokens = (command.args or "").split()
    if len(tokens) < 2:
        await message.reply(
            "Формат: <code>/hockey ID 2:2 3:2</code> — id матча, счёт основного времени "
            "и итоговый счёт (с овертаймом и буллитами).\n"
            "Матч закончился в основное время — хватит одного счёта: "
            "<code>/hockey ID 4:1</code>.\nСписок ждущих: /results"
        )
        return

    match_id_token, *score_tokens = tokens
    if not match_id_token.isdigit() or len(score_tokens) > 2:
        await message.reply("Не понял формат. Пример: <code>/hockey 12 2:2 3:2</code>")
        return

    reg = _parse_score(score_tokens[0])
    if reg is None:
        await message.reply("Счёт основного времени не разобрал. Пример: <code>2:2</code>")
        return
    final = _parse_score(score_tokens[1]) if len(score_tokens) > 1 else reg
    if final is None:
        await message.reply("Итоговый счёт не разобрал. Пример: <code>3:2</code>")
        return

    match_id = int(match_id_token)
    try:
        match = await db.get_match(match_id)
        if match is None:
            raise ValueError("матч не найден")
        if final == reg and reg[0] == reg[1]:
            await message.reply(
                "Хоккей не заканчивается вничью: если в основное время ничья, "
                "нужен итоговый счёт (овертайм или буллиты)."
            )
            return
        result = await db.settle_hockey(match_id, reg[0], reg[1], final[0], final[1], source="admin")
    except (BettingError, ValueError) as exc:
        await message.reply(f"Не вышло: {exc}")
        return

    bot = callback_bot(message)
    await notify_bettors(
        bot, db, result,
        score=f"{reg[0]}:{reg[1]} (осн.) · {final[0]}:{final[1]} (итог)",
    )
    await message.answer(texts.hockey_settled_text(
        result, match_id, f"{reg[0]}:{reg[1]}", f"{final[0]}:{final[1]}"
    ))
    for express in result.get("expresses") or []:
        chat_id = express.get("chat_id")
        if chat_id:
            with contextlib.suppress(Exception):
                await bot.send_message(int(chat_id), texts.express_result(express))


@router.message(Command("cleanup"))
async def cmd_cleanup(message: Message, command: CommandObject, db: Database) -> None:
    """Убрать зависшие матчи: /cleanup или /cleanup 3 (часов)."""
    arg = (command.args or "").strip()
    hours = int(arg) if arg.isdigit() else settings.stale_match_hours
    rows = await db.stale_matches(hours)
    if not rows:
        await message.answer(f"Зависших матчей старше {hours} ч нет ✅")
        return

    bot = callback_bot(message)
    settled = 0
    cancelled = 0
    waiting = 0
    refunded = 0
    for row in rows:
        # сначала пытаемся посчитать по факту, и только потом решаем, что делать дальше
        winner, score = await series_from_maps(db, row)
        if winner is not None:
            with contextlib.suppress(BettingError):
                result = await db.settle_match(int(row["id"]), winner, source="maps")
                await notify_bettors(bot, db, result, score=score)
                settled += 1
                continue

        # матч со ставками не отменяем: он игрался, значит исход будет — ждём его
        if await db.match_bet_count(int(row["id"])):
            with contextlib.suppress(BettingError):
                await db.set_status(int(row["id"]), "awaiting")
                waiting += 1
            continue

        try:
            result = await db.cancel_match(int(row["id"]))
        except BettingError:
            continue
        cancelled += 1
        refunded += int(result["refunded"])
        await notify_bettors(bot, db, result, void=True)
        for card in await db.match_maps(int(row["id"])):
            if str(card["status"]) in ("open", "live", "awaiting"):
                with contextlib.suppress(BettingError):
                    await db.cancel_map(card["id"])

    await message.answer(
        f"Рассчитал по картам: <b>{settled}</b>\n"
        f"Ждут фактический исход (ставки в игре): <b>{waiting}</b>\n"
        f"Снял без ставок: <b>{cancelled}</b>, вернул {texts.money(refunded)}.\n"
        "Матчи из второй строки считаются по победителю командой "
        "<code>/recount ID 1</code> (1 — команда A, 2 — команда B)."
    )


@router.message(Command("recount"))
async def cmd_recount(message: Message, command: CommandObject, db: Database) -> None:
    """Пересчитать отменённый матч по факту: /recount 42 1."""
    parts = (command.args or "").split()
    if len(parts) < 2 or not parts[0].isdigit() or parts[1] not in ("1", "2"):
        rows = await db.recent_cancelled(8)
        lines = [
            f"#{row['id']} {row['team_a']} — {row['team_b']}" for row in rows
        ] or ["Пока ничего не отменяли."]
        await message.answer(
            "<b>Пересчёт отменённого матча</b>\n"
            + "\n".join(lines)
            + "\n\nФормат: <code>/recount ID 1</code> — победила первая команда, "
            "<code>/recount ID 2</code> — вторая."
        )
        return

    match_id = int(parts[0])
    winner = "a" if parts[1] == "1" else "b"

    try:
        result = await db.recount_match(match_id, winner)
    except BettingError as exc:
        await message.answer(f"Не вышло: {exc}")
        return

    bot = callback_bot(message)
    await notify_bettors(bot, db, result)
    maps = await finish_maps_of_match(db, bot, match_id, source="recount")
    await message.answer(
        f"Пересчитал матч #{match_id}: победила <b>{result['team']}</b>.\n"
        f"Выплачено {texts.money(result['paid'])}, откатил возвратов "
        f"{texts.money(result.get('reversed', 0))}.\n"
        f"Карты: рассчитано {maps['settled']}, возвращено {maps['refunded']}, "
        f"ждут результата {maps['waiting']}."
    )


@router.message(Command("recheck"))
async def cmd_recheck(message: Message, command: CommandObject, db: Database) -> None:
    """Перепроверить расчёты: /recheck или /recheck 72 (часов)."""
    arg = (command.args or "").strip()
    hours = int(arg) if arg.isdigit() else 48
    bot = callback_bot(message)

    progress = await message.answer(f"Перепроверяю расчёты за {hours} ч…")
    report = await recheck_recent(db, bot, hours=hours)
    text = report_text(report)
    with contextlib.suppress(Exception):
        await progress.edit_text(text)
        return
    await message.answer(text)


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


def _admin_time(value: Any) -> str:
    if not value:
        return "—"
    try:
        return time.strftime("%Y-%m-%d %H:%M:%S UTC", time.gmtime(int(value)))
    except (TypeError, ValueError, OverflowError):
        return "неизвестно"


@router.message(Command("version"))
async def cmd_version(message: Message, db: Database) -> None:
    """Короткая версия сборки и backend базы, без секретной конфигурации."""
    commit = (os.getenv("RENDER_GIT_COMMIT") or "local")[:12]
    backend = html.escape(str(getattr(db.conn, "name", "unknown")))
    uptime = max(0, int(time.time()) - MODULE_STARTED_AT)
    hours, remainder = divmod(uptime, 3600)
    minutes = remainder // 60
    await message.answer(
        f"<b>Версия приложения</b>\n"
        f"Коммит: <code>{html.escape(commit)}</code>\n"
        f"Режим: <code>{html.escape(settings.run_mode)}</code> · "
        f"Python: <code>{platform.python_version()}</code>\n"
        f"База: <code>{backend}</code> · uptime: {hours} ч {minutes} мин"
    )


@router.message(Command("services"))
async def cmd_services(message: Message, syncer: Any, recheck: Any = None) -> None:
    """Состояние синхронизации и фоновых циклов (read-only)."""
    now = int(time.time())
    sync_at = int(getattr(syncer, "last_sync_at", 0) or 0)
    sync_error = getattr(syncer, "last_error", None)
    sync_line = (
        f"• линия: последняя синхронизация {_admin_time(sync_at)}"
        + (f", {max(0, now - sync_at)} сек назад" if sync_at else "")
        + (f"; ошибка: {html.escape(str(sync_error)[:180])}" if sync_error else "")
    )
    lines = ["<b>Фоновые сервисы</b>", sync_line]

    services = [
        ("перепроверка", recheck.stats if recheck is not None else None),
        ("winback", winback.stats()),
        ("напоминания", reminders.stats()),
        ("уведомления о старте", matchnotify.stats()),
    ]
    for name, stats in services:
        if stats is None:
            lines.append(f"• {name}: не запущен")
            continue
        state = "включён" if stats.get("enabled", True) else "выключен"
        last = _admin_time(stats.get("last_at"))
        detail = f"; последний запуск {last}"
        if stats.get("last_error"):
            detail += f"; ошибка: {html.escape(str(stats['last_error'])[:180])}"
        lines.append(f"• {name}: {state}{detail}")

    await message.answer("\n".join(lines))


@router.message(Command("feed"))
async def cmd_feed(message: Message, db: Database, syncer: Any) -> None:
    """Состояние последней загрузки линии и её счётчики."""
    last_at = int(getattr(syncer, "last_sync_at", 0) or 0)
    if not last_at:
        raw = await db.get_meta("last_sync_at")
        last_at = int(raw or 0)
    age = max(0, int(time.time()) - last_at) if last_at else None
    stats = getattr(syncer, "last_stats", None) or {}
    error = getattr(syncer, "last_error", None)
    stale = age is None or age > max(900, int(settings.sync_interval) * 20)
    status = "ошибка" if error else "устаревшие данные" if stale else "OK"
    lines = [
        "<b>Спортивная линия</b>",
        f"Статус: {status}",
        f"Последний успех: {_admin_time(last_at)}",
        f"Возраст данных: {f'{age} сек' if age is not None else 'неизвестен'}",
        f"Интервал: {int(settings.sync_interval)} сек",
    ]
    if stats:
        keys = ("fetched", "created", "updated", "football", "hockey", "closed", "settled", "awaiting")
        lines.append("Счётчики: " + " · ".join(
            f"{key}={int(stats[key])}" for key in keys if key in stats
        ))
    if error:
        lines.append(f"Ошибка: {html.escape(str(error)[:240])}")
    await message.answer("\n".join(lines))


def _private_admin_report_chat(message: Message) -> bool:
    """Reports contain player-only details; never expose them in group chats."""
    chat = getattr(message, "chat", None)
    return getattr(chat, "type", "private") == "private"


async def _send_player_diagnostics(message: Message, db: Database, user_id: int) -> None:
    if not _private_admin_report_chat(message):
        await message.answer("Отчёты по игрокам доступны только в личном чате с ботом.")
        return
    user = await db.get_user(user_id)
    if user is None:
        await message.answer(f"Игрок <code>{user_id}</code> не найден.")
        return

    name = user["full_name"] or (f"@{user['username']}" if user["username"] else "без имени")
    tickets = await db.tickets(user_id, limit=20)
    active_bets = await db.user_active_bets(user_id)
    active_expresses = await db.user_open_expresses(user_id)
    history = await db.history(user_id, limit=5)
    events = await db.user_recent_events(user_id, limit=5)
    recent_bets = await db.user_bets_all(user_id, limit=3)
    lines = [
        f"<b>Игрок {html.escape(str(name))}</b> · <code>{user_id}</code>",
        f"Баланс: {int(user['balance'])} · создан: {_admin_time(user['created_at'])}",
        f"Последний вход: {_admin_time(user['last_seen_at'])}",
        f"Активные ставки: {len(active_bets)} · экспрессы: {len(active_expresses)}",
        "Билеты: " + ("; ".join(
            f"#{int(t['id'])} {int(t['amount'])} ({html.escape(str(t['kind']))}, "
            f"{html.escape(str(t['source']))}, до {_admin_time(t['expires_at'])})"
            for t in tickets
        ) if tickets else "нет"),
    ]
    if recent_bets:
        lines.append("Последние ставки:")
        lines.extend(
            f"• #{int(row['id'])} {html.escape(str(row['team_a']))} — "
            f"{html.escape(str(row['team_b']))}; {int(row['amount'])}, "
            f"{html.escape(str(row['market']))}, статус "
            f"{html.escape(str(row['match_status']))}, выплата "
            f"{int(row['payout']) if row['payout'] is not None else '—'}"
            for row in recent_bets
        )
    if history:
        lines.append("Последние движения баланса:")
        lines.extend(
            f"• {int(row['delta']):+} · {html.escape(str(row['reason'])[:90])} · "
            f"{_admin_time(row['created_at'])}"
            for row in history
        )
    if events:
        lines.append("Последние события:")
        lines.extend(
            f"• {html.escape(str(row['name']))}: {html.escape(str(row['payload'] or '')[:90])} · "
            f"{_admin_time(row['created_at'])}"
            for row in events
        )
    await message.answer("\n".join(lines))


@router.message(Command("player"))
async def cmd_player(
    message: Message, command: CommandObject, db: Database, state: FSMContext
) -> None:
    """Read-only сводка по игроку; без ID предлагает выбрать игрока."""
    if not _private_admin_report_chat(message):
        await message.reply("Отчёты по игрокам доступны только в личном чате с ботом.")
        return
    args = (command.args or "").split()
    if not args:
        await _begin_player_flow(message, state, db, "player")
        return
    if len(args) != 1 or not args[0].isdigit() or int(args[0]) <= 0:
        await message.reply("Формат: <code>/player ID</code> или выберите игрока из списка.")
        return
    user_id = int(args[0])
    if user_id > 2**63 - 1:
        await message.reply("Telegram ID слишком большой.")
        return
    await _send_player_diagnostics(message, db, user_id)


async def _send_player_stats(message: Message, db: Database, user_id: int) -> None:
    if not _private_admin_report_chat(message):
        await message.answer("Отчёты по игрокам доступны только в личном чате с ботом.")
        return
    user = await db.get_user(user_id)
    if user is None:
        await message.answer(f"Игрок <code>{user_id}</code> не найден.")
        return

    stats = await db.player_stats(user_id)
    active_bets = await db.user_active_bets(user_id)
    active_expresses = await db.user_open_expresses(user_id)
    name = user["full_name"] or (f"@{user['username']}" if user["username"] else "без имени")
    staked = int(stats["staked"])
    returned = int(stats["returned"])
    cash_staked = int(stats["cash_staked"])
    cash_returned = int(stats["cash_returned"])
    settled_cash_staked = int(stats["settled_cash_staked"])
    promo_staked = int(stats["promo_staked"])
    promo_returned = int(stats["promo_returned"])
    net = cash_returned - settled_cash_staked
    net_text = ("+" if net >= 0 else "−") + texts.money(abs(net))
    tournament = str(stats["favourite_tournament"] or "нет данных")

    lines = [
        f"<b>Статистика игрока {html.escape(str(name))}</b> · <code>{user_id}</code>",
        f"Баланс: {texts.money(int(user['balance']))}",
        f"Спорт: {int(stats['bets'])} ставок/экспрессов · рассчитано {int(stats['settled'])} · "
        f"выиграно {int(stats['won'])} · winrate {int(stats['winrate'])}%",
        f"Открыто: {len(active_bets)} ставок · {len(active_expresses)} экспрессов",
        f"Свои монеты (спорт + казино): поставлено {texts.money(cash_staked)} "
        f"(включая открытые), возвращено по рассчитанным {texts.money(cash_returned)}",
        f"Бонусные ставки: номинал {texts.money(promo_staked)} "
        f"(включая открытые), выплаты по рассчитанным {texts.money(promo_returned)}",
        f"Общий оборот с бонусами: поставлено {texts.money(staked)}, "
        f"возвращено по рассчитанным {texts.money(returned)}",
        f"Итог по рассчитанным ставкам (свои монеты, спорт + казино): "
        f"<b>{net_text}</b> · ROI {int(stats['roi'])}%",
        f"Казино: {int(stats['casino_rounds'])} раундов · результат "
        f"{('+' if int(stats['casino_net']) >= 0 else '−')}{texts.money(abs(int(stats['casino_net'])))}",
        f"Крупнейшая спортивная выплата: {texts.money(int(stats['biggest_win']))}",
        f"Частый турнир: {html.escape(tournament)}",
    ]
    await message.answer("\n".join(lines))


@router.message(Command("player_stats"))
async def cmd_player_stats(
    message: Message, command: CommandObject, db: Database, state: FSMContext
) -> None:
    """Сводная статистика игрока: /player_stats ID или выбор из списка."""
    if not _private_admin_report_chat(message):
        await message.reply("Отчёты по игрокам доступны только в личном чате с ботом.")
        return
    args = (command.args or "").split()
    if not args:
        await _begin_player_flow(message, state, db, "player_stats")
        return
    if len(args) != 1 or not args[0].isdigit() or int(args[0]) <= 0:
        await message.reply("Формат: <code>/player_stats ID</code> или выберите игрока из списка.")
        return
    user_id = int(args[0])
    if user_id > 2**63 - 1:
        await message.reply("Telegram ID слишком большой.")
        return
    await _send_player_stats(message, db, user_id)


async def _continue_admin_player_flow(
    message: Message, state: FSMContext, db: Database, user: Any
) -> None:
    data = await state.get_data()
    action = data.get("action")
    if action == "player":
        await state.clear()
        await _send_player_diagnostics(message, db, int(user["user_id"]))
    elif action == "player_stats":
        await state.clear()
        await _send_player_stats(message, db, int(user["user_id"]))
    elif action in ("grant", "freebet", "freebet_express"):
        await _begin_amount_flow(message, state, str(action), user)
    else:
        await state.clear()
        await message.answer("Сценарий устарел. Запустите команду ещё раз.")


@router.callback_query(F.data.startswith("admin_flow:"))
async def on_admin_player_flow(
    callback: CallbackQuery, state: FSMContext, db: Database
) -> None:
    if callback.from_user is None or callback.from_user.id not in settings.admin_ids_set:
        await callback.answer("Только для администратора", show_alert=True)
        return
    if not isinstance(callback.message, Message):
        await callback.answer("Сообщение со списком недоступно", show_alert=True)
        return

    action = str(callback.data or "").split(":", 2)
    if len(action) < 2:
        await callback.answer("Кнопка устарела", show_alert=True)
        return
    choice = action[1]
    current = await state.get_state()
    if choice == "cancel":
        await state.clear()
        with contextlib.suppress(Exception):
            await callback.message.edit_text("Действие отменено.", reply_markup=None)
        await callback.answer("Отменено")
        return
    if current != AdminPlayerFlow.waiting_player.state:
        await callback.answer("Список устарел — запустите команду заново.", show_alert=True)
        return
    if choice == "manual":
        await state.set_state(AdminPlayerFlow.waiting_manual_id)
        await callback.message.answer(
            "Введите Telegram ID игрока:", reply_markup=_cancel_keyboard()
        )
        await callback.answer()
        return
    if choice != "player" or len(action) != 3 or not action[2].isdigit():
        await callback.answer("Некорректный выбор", show_alert=True)
        return

    user_id = int(action[2])
    user = await db.get_user(user_id)
    if user is None:
        await callback.answer("Игрок больше не найден", show_alert=True)
        return
    await callback.answer()
    await _continue_admin_player_flow(callback.message, state, db, user)


@router.message(AdminPlayerFlow.waiting_manual_id, F.text & ~F.text.startswith("/"))
async def on_admin_manual_player_id(
    message: Message, state: FSMContext, db: Database
) -> None:
    token = (message.text or "").strip()
    if not token.isdigit() or not 0 < int(token) <= 2**63 - 1:
        await message.reply("ID должен быть положительным числом.", reply_markup=_cancel_keyboard())
        return
    user = await db.get_user(int(token))
    if user is None:
        await message.reply("Игрок не найден. Введите другой ID или отмените действие.",
                            reply_markup=_cancel_keyboard())
        return
    await _continue_admin_player_flow(message, state, db, user)


@router.message(AdminPlayerFlow.waiting_amount, F.text & ~F.text.startswith("/"))
async def on_admin_amount(
    message: Message, state: FSMContext, db: Database
) -> None:
    token = (message.text or "").strip().replace(" ", "")
    if not token.isdigit() or not 1 <= int(token) <= 2**31 - 1:
        await message.reply("Введите целую сумму от 1 до 2 147 483 647.",
                            reply_markup=_cancel_keyboard())
        return
    data = await state.get_data()
    action = data.get("action")
    user_id = int(data.get("target_user_id") or 0)
    user = await db.get_user(user_id)
    if user is None:
        await state.clear()
        await message.reply("Игрок больше не найден. Запустите команду заново.")
        return
    await state.clear()
    if action == "grant":
        await _apply_coin_grant(message, db, user, int(token))
    elif action == "freebet":
        await _apply_freebet(message, db, user, int(token), kind="single")
    elif action == "freebet_express":
        await _apply_freebet(message, db, user, int(token), kind="express")
    else:
        await message.reply("Сценарий устарел. Запустите команду заново.")


@router.message(Command("bettrace"))
async def cmd_bettrace(message: Message, command: CommandObject, db: Database) -> None:
    """Read-only trace ставки/экспресса/раунда казино по числовому ID."""
    args = (command.args or "").split()
    if len(args) != 1 or not args[0].isdigit() or int(args[0]) <= 0:
        await message.reply("Формат: <code>/bettrace ID</code>")
        return
    bet_id = int(args[0])
    if bet_id > 2**63 - 1:
        await message.reply("ID слишком большой.")
        return
    traces = await db.trace_bet(bet_id)
    if not traces:
        await message.reply("Запись с таким ID не найдена.")
        return

    lines = [f"<b>Трассировка ID {int(args[0])}</b>"]
    for trace in traces:
        row = trace["row"]
        if trace["kind"] == "single":
            market = html.escape(str(row["market"]))
            teams = html.escape(f"{row['team_a']} — {row['team_b']}")
            state = "продана" if row["cashed_out"] else (
                f"выплата {int(row['payout'])}" if row["payout"] is not None else "ожидает расчёта"
            )
            lines.extend([
                f"<b>Ординар #{int(row['id'])}</b> · игрок <code>{int(row['user_id'])}</code>",
                f"{teams} · рынок {market} · выбор {html.escape(str(row['side']))}",
                f"Ставка {int(row['amount'])} · кэф {float(row['odds']):g} · статус "
                f"{html.escape(str(row['match_status']))}/{state}",
                f"Создана: {_admin_time(row['created_at'])}",
            ])
            if row["ticket_id"] is not None:
                lines.append(
                    f"Фрибет #{int(row['ticket_id'])}: {int(row['ticket_amount'])} "
                    f"({html.escape(str(row['ticket_source']))}), использован "
                    f"{_admin_time(row['ticket_used_at'])}"
                )
        elif trace["kind"] == "express":
            lines.extend([
                f"<b>Экспресс #{int(row['id'])}</b> · игрок <code>{int(row['user_id'])}</code>",
                f"Ставка {int(row['stake'])} · кэф {float(row['odds']):g} · "
                f"статус {html.escape(str(row['status']))} · выплата "
                f"{int(row['payout']) if row['payout'] is not None else '—'}",
            ])
            for leg in trace["legs"][:8]:
                lines.append(
                    f"• {html.escape(str(leg['team_a']))} — {html.escape(str(leg['team_b']))}: "
                    f"{html.escape(str(leg['side']))}, исход {html.escape(str(leg['result']))}"
                )
            if len(trace["legs"]) > 8:
                lines.append(f"…ещё {len(trace['legs']) - 8} исходов")
        else:
            lines.extend([
                f"<b>Казино #{int(row['id'])}</b> · игрок <code>{int(row['user_id'])}</code>",
                f"Игра {html.escape(str(row['game']))} · {html.escape(str(row['label']))}",
                f"Ставка {int(row['amount'])} · исход {html.escape(str(row['outcome']))} · "
                f"выплата {int(row['payout'])}",
                f"Создана: {_admin_time(row['created_at'])}",
            ])
    await message.answer("\n".join(lines))


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


# --------------------------------------------------------------- бэкапы
_pending_restore: dict[int, tuple[bytes, dict[str, Any], float]] = {}
RESTORE_TTL = 10 * 60       # подтвердить восстановление нужно за 10 минут


@router.message(Command("backup"))
async def cmd_backup(message: Message, command: CommandObject, db: Database) -> None:
    """Снять копию базы прямо сейчас: /backup или /backup status."""
    arg = (command.args or "").strip().lower()
    if arg in ("status", "info"):
        last = await db.last_backup()
        if last is None:
            await message.answer("Копий ещё не было. /backup — сделаю сейчас.")
            return
        created = time.strftime("%d.%m.%Y %H:%M UTC", time.gmtime(int(last["created_at"])))
        age = (int(time.time()) - int(last["created_at"])) // 3600
        await message.answer(
            f"<b>Последняя копия</b>: {created} ({age} ч назад)\n"
            f"Размер: {int(last['size_bytes']) / 1024:.1f} КБ · игроков {last['users']} · "
            f"ставок {last['bets']}\n"
            f"Тип: {last['kind']}\n\n"
            "Копии приходят сюда, в личку, и хранятся в истории Telegram — это вне Render и Neon."
        )
        return

    bot = callback_bot(message)
    await message.answer("Собираю дамп…")
    blob, payload = await make_dump(db, kind="manual")
    assert message.from_user is not None
    sent = await send_dump(bot, message.from_user.id, blob, payload)
    if sent:
        await message.answer(summary(payload) + f"\nРазмер: {len(blob) / 1024:.1f} КБ")
    else:
        await message.answer("Дамп собрал, но отправить файл не смог — смотри лог.")


@router.message(F.document)
async def on_backup_file(message: Message, db: Database, backup: BackupService) -> None:
    """Админ прислал файл копии — показываем, что внутри, и ждём подтверждения."""
    document = message.document
    assert document is not None
    if (message.from_user is None or not document.file_name
            or not document.file_name.endswith(".json.gz")):
        return

    limit = settings.backup_max_mb * 1024 * 1024
    if int(document.file_size or 0) > limit:
        await message.answer(f"Файл больше {settings.backup_max_mb} МБ — такой не приму.")
        return

    bot = callback_bot(message)
    file = await bot.get_file(document.file_id)
    buffer = BytesIO()
    if file.file_path:
        await bot.download_file(file.file_path, buffer)
    try:
        payload = decode(buffer.getvalue())
    except BackupError as exc:
        await message.answer(f"Это не наш дамп: {exc}")
        return

    missing_tables = set(BACKUP_TABLES) - set(payload.get("tables") or {})
    if missing_tables:
        await message.answer(
            "Дамп старого или неполного формата. Не хватает таблиц: "
            + ", ".join(sorted(missing_tables))
            + ". Восстановление не запущено, чтобы не стереть несохранённые данные."
        )
        return

    current = await db.dump_rows()
    old_users = len(current.get("users", []))
    new_users = payload["counts"].get("users", 0)
    _pending_restore[int(message.from_user.id)] = (buffer.getvalue(), payload, time.time())

    preview = summary(payload)
    await message.answer(
        f"{preview}\n\n"
        f"Сейчас в базе игроков: <b>{old_users}</b>, в дампе — <b>{new_users}</b>.\n"
        "Восстановление <b>затрёт</b> текущие данные полностью. Подтверди, если уверен "
        "(10 минут на раздумья).",
        reply_markup=keyboards.restore_confirm_kb(),
    )


@router.callback_query(F.data.startswith("rst:"))
async def on_restore_confirm(callback: CallbackQuery, db: Database, backup: BackupService) -> None:
    if callback.from_user is None or callback.from_user.id not in settings.admin_ids_set:
        await callback.answer("Только для админа", show_alert=True)
        return
    user_id = int(callback.from_user.id)

    if callback.data == "rst:no":
        _pending_restore.pop(user_id, None)
        await callback.answer("Отменил")
        if callback.message is not None:
            await callback.message.answer("Восстановление отменено, база не тронута.")
        return

    pending = _pending_restore.pop(user_id, None)
    if pending is None:
        await callback.answer("Файл уже неактуален", show_alert=True)
        return
    blob, _, taken_at = pending
    if time.time() - taken_at > RESTORE_TTL:
        await callback.answer("Прошло больше 10 минут — пришли файл заново", show_alert=True)
        return

    # перед затиранием сохраняем текущее состояние: откат всегда возможен
    safety, safety_payload = await make_dump(db, kind="before-restore")
    if callback.bot is not None:
        await send_dump(callback.bot, int(settings.admin_ids_list[0]), safety, safety_payload)

    try:
        counts = await restore(db, blob)
    except BackupError as exc:
        await callback.answer("Не вышло", show_alert=True)
        if callback.message is not None:
            await callback.message.answer(f"Восстановление не прошло: {exc}")
        return

    await callback.answer("Готово")
    if callback.message is not None:
        await callback.message.answer(
            "База восстановлена.\n"
            + "\n".join(f"{table}: {count}" for table, count in counts.items() if count)
            + "\n\nПредыдущее состояние прислал отдельным файлом — если что, вернём его."
        )
