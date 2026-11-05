"""Админские команды бота.

В боте больше нет кнопочного меню: игроки всё делают в мини-приложении.
У админа остаются только служебные команды и подтверждение исходов:

    /grant 123456789 500   — начислить монеты (можно /grant @ник 500)
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
import logging
import time
from io import BytesIO
from typing import Any

from aiogram import Bot, F, Router
from aiogram.filters import BaseFilter, Command, CommandObject
from aiogram.types import BufferedInputFile, CallbackQuery, Message

from app import keyboards, texts
from app.config import settings
from app.db import BettingError, Database
from app.db.database import BackupError
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
