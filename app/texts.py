"""Все тексты бота. Правьте смело — логика от них не зависит."""

from __future__ import annotations

from datetime import datetime
from html import escape
from typing import Any, Mapping

from app.config import settings


def money(amount: int) -> str:
    return f"{amount:,}".replace(",", "\u202f") + f" {settings.currency}"


def odds(value: float | None) -> str:
    return f"{value:.2f}" if value else "—"


def fmt_time(ts: int) -> str:
    """02.11 21:00 — по времени сервера."""
    return datetime.fromtimestamp(ts).strftime("%d.%m %H:%M")


def fmt_dt(ts: int) -> str:
    return datetime.fromtimestamp(ts).strftime("%d.%m %H:%M")


def short_tournament(name: str) -> str:
    """'Counter-Strike. IEM Пекин. Групповой этап. Bo3' -> 'IEM Пекин · Bo3'."""
    parts = [part.strip() for part in name.split(".") if part.strip()]
    parts = [part for part in parts if part.lower() not in ("counter-strike", "cs2")]
    return " · ".join(parts[:2]) if parts else name


START = """🎯 <b>CS2 BETS</b> — ставки виртуальными монетами на реальные матчи CS2.

Привет, {name}! Баланс: <b>{balance}</b>

<b>Как играть</b>
1️⃣ Жми <b>🎮 Матчи и кэфы</b> — живые матчи тир-1 с кэфами Fonbet
2️⃣ Выбери матч → исход → сумму. Всё кнопками
3️⃣ Кэф фиксируется в момент ставки
4️⃣ После матча бот сам считает результат и платит: <code>ставка × кэф</code>

Никаких реальных денег: монеты начисляет бот, вывести их нельзя.
Команды не нужны — меню всегда под кнопкой ниже.
"""



ERROR_TEXT = "😔 Что-то сломалось на моей стороне. Уже смотрю логи, попробуй ещё раз."




def rules_text() -> str:
    return (
        "<b>Правила</b>\n\n"
        "<b>Экспресс</b> — несколько исходов в одной ставке, кэфы перемножаются. "
        "Все исходы должны зайти; отменённый матч считается с кэфом 1.0. "
        f"Минимум 2 исхода, максимум {settings.max_express_legs}, "
        f"потолок итогового кэфа — {settings.max_express_odds:.0f}.\n\n"
        "1. Матчи и кэфы берутся из реальной линии <b>Fonbet</b> — только CS2 тир-1 "
        "(IEM, ESL Pro League, BLAST, PGL, мейджоры, Esports World Cup). "
        "Квалифайеры, академии, 2x2 и прочий мусор отфильтрованы.\n"
        "2. Кэф фиксируется в момент ставки. Если линия сдвинулась — ваша ставка уже не меняется.\n"
        "3. Ставки принимаются до времени начала матча. После старта приём закрывается автоматически.\n"
        f"4. Выплата: <code>ставка × кэф</code> при угаданном исходе, 0 при ошибке. "
        f"Минимальная ставка — {money(settings.min_bet)}.\n"
        "5. Результат бот берёт из счёта линии. Если счёт не удалось получить однозначно, "
        "он пришлёт карточку с кнопками — админ подтверждает исход вручную.\n"
        "6. Матч отменён/не состоялся → админ жмёт ⚪️ Отменить, ставки возвращаются полностью.\n\n"
        "<b>Пример</b>\n"
        "Кэф на Aurora — 1.65. Ставите 200 🪙. Aurora выигрывает → получаете 200 × 1.65 = 330 🪙 "
        "(чистыми +130). Проигрывает → −200 🪙.\n\n"
        "Кэфы букмекера включают маржу, поэтому в среднем игра идёт с небольшим минусом — "
        "как в реальном беттинге, только на виртуальные монеты."
    )


def match_card(match: Mapping[str, Any], *, with_bets: bool = True) -> str:
    team_a = escape(str(match["team_a"]))
    team_b = escape(str(match["team_b"]))
    status = str(match["status"])

    head = {
        "open": "🟢 приём ставок",
        "live": "🔴 идёт матч, ставки закрыты",
        "awaiting": "⏳ ждём результат",
        "finished": "🏁 рассчитан",
        "cancelled": "⚪️ отменён",
    }.get(status, status)

    lines = [
        f"🎮 <b>{escape(short_tournament(str(match['tournament'])))}</b> · матч #{match['id']} · {head}",
        "",
        f"🔵 <b>{team_a}</b> — кэф <b>{odds(match['odds_a'])}</b>",
        f"🟠 <b>{team_b}</b> — кэф <b>{odds(match['odds_b'])}</b>",
        f"🕐 Начало: {fmt_time(int(match['start_time']))}",
    ]

    if status == "finished" and match["winner"]:
        winner = team_a if match["winner"] == "a" else team_b
        lines.append(f"Победитель: <b>{winner}</b>")

    if with_bets:
        lines.append("")
        if status == "open":
            lines.append("Ставка: выбери исход кнопкой ниже 👇")
        else:
            lines.append(f"Приём ставок закрыт · следи за результатом: <code>/match {match['id']}</code>")
    return "\n".join(lines)


def bet_accepted(
    match: Mapping[str, Any], side: str, amount: int, bet_odds: float, balance: int
) -> str:
    team = escape(str(match["team_a"] if side == "a" else match["team_b"]))
    return (
        f"✅ Ставка принята: <b>{money(amount)}</b> на <b>{team}</b> по кэфу <b>{bet_odds:.2f}</b>\n"
        f"Потенциальная выплата: <b>{money(round(amount * bet_odds))}</b>\n"
        f"Баланс: {money(balance)}"
    )


def settled_text(result: Mapping[str, Any], match_id: int, score: str | None = None) -> str:
    score_part = f" ({score})" if score else ""
    return (
        f"🏁 Матч #{match_id} рассчитан: победила <b>{escape(str(result['team']))}</b>{score_part}\n"
        f"Выплачено: {money(int(result['paid']))} · ставок: {result['bets']} · "
        f"угадали: {result['winners']}"
    )


def awaiting_text(match: Mapping[str, Any], score: str | None = None) -> str:
    score_part = f"\nСчёт в линии: <b>{score}</b>" if score else ""
    return (
        f"⏳ Матч #{match['id']} закончился, но я не смог однозначно определить победителя."
        f"{score_part}\n"
        f"{escape(str(match['team_a']))} 🆚 {escape(str(match['team_b']))}\n"
        "Админ, подтверди исход кнопкой ниже 👇"
    )


# ------------------------------------------------------------------ экспрессы
def express_accepted(
    express_id: int, legs: list[dict[str, Any]], stake: int, odds: float, balance: int
) -> str:
    lines = [
        f"✅ Экспресс #{express_id} принят",
        "",
    ]
    for index, leg in enumerate(legs, start=1):
        lines.append(
            f"{index}. {escape(str(leg['team']))} · кэф {float(leg['odds']):.2f} "
            f"({escape(str(leg['match']))})"
        )
    lines += [
        "",
        f"Ставка: <b>{money(stake)}</b> · кэф: <b>{odds:.2f}</b>",
        f"Потенциальная выплата: <b>{money(round(stake * odds))}</b>",
        f"Баланс: {money(balance)}",
    ]
    return "\n".join(lines)


def express_result(express: Mapping[str, Any]) -> str:
    express_id = express["express_id"]
    stake = money(int(express["stake"]))
    odds_value = float(express["odds"])

    if express["status"] == "won":
        return (
            f"🎟 <b>Экспресс #{express_id} сыграл!</b>\n"
            f"Кэф {odds_value:.2f} · ставка {stake} → выплата "
            f"<b>{money(int(express['payout']))}</b>"
        )
    if express["status"] == "lost":
        lost = express.get("lost_leg") or "один из исходов"
        return (
            f"🎟 Экспресс #{express_id} проиграл: не зашёл <b>{escape(str(lost))}</b>.\n"
            f"Ставка {stake} сгорела."
        )
    return (
        f"🎟 Экспресс #{express_id} возвращён (все матчи отменены).\n"
        f"Возврат: <b>{money(int(express['payout']))}</b>"
    )


# ------------------------------------------------------- экраны (кнопочный UI)
def menu_text(name: str, balance: int) -> str:
    return (
        "🎯 <b>CS2 BETS</b>\n"
        f"{escape(name)}, баланс: <b>{money(balance)}</b>\n\n"
        "Реальные матчи и кэфы Fonbet, только турниры тир-1.\n"
        "Ставки — виртуальными монетами.\n\n"
        "Выбирай действие кнопкой 👇"
    )


def matches_page_text(rows: list[Mapping[str, Any]], page: int, pages: int) -> str:
    lines = [f"🎮 <b>Матчи тир-1</b> · стр. {page + 1}/{pages}", ""]
    for match in rows:
        lines.append(
            f"🕐 {fmt_time(int(match['start_time']))} · "
            f"{escape(short_tournament(str(match['tournament'])))}\n"
            f"   <b>{escape(str(match['team_a']))}</b> {odds(match['odds_a'])} — "
            f"{odds(match['odds_b'])} <b>{escape(str(match['team_b']))}</b>"
        )
    lines += ["", "Жми на матч — покажу карточку с кэфами."]
    return "\n".join(lines)


def no_matches_text() -> str:
    return (
        "🟢 Сейчас нет матчей с открытыми ставками.\n\n"
        "Линия тир-1 обновляется автоматически — загляни позже."
    )


def amount_prompt_text(match: Mapping[str, Any], side: str, balance: int) -> str:
    team = escape(str(match["team_a"] if side == "a" else match["team_b"]))
    bet_odds = match["odds_a"] if side == "a" else match["odds_b"]
    return (
        f"💸 Ставка на <b>{team}</b> · кэф <b>{odds(bet_odds)}</b>\n"
        f"{escape(short_tournament(str(match['tournament'])))} · "
        f"матч #{match['id']} · начало {fmt_time(int(match['start_time']))}\n\n"
        f"Баланс: {money(balance)}\n"
        f"Минимум: {money(settings.min_bet)}\n\n"
        "Выбери сумму 👇"
    )


def ask_custom_amount_text(kind: str) -> str:
    what = "ставку" if kind == "single" else "экспресс"
    return (
        f"✏️ Напиши сумму {what} числом (мин. {money(settings.min_bet)}).\n"
        "Отмена — кнопка ниже или /cancel."
    )


def balance_text(
    balance: int, singles: list[Mapping[str, Any]], expresses: list[Mapping[str, Any]]
) -> str:
    lines = [f"💰 Баланс: <b>{money(balance)}</b>"]

    if singles:
        total = sum(int(bet["amount"]) for bet in singles)
        lines.append(f"\nСтавки в игре ({len(singles)}) на {money(total)}:")
        for bet in singles[:8]:
            team = bet["team_a"] if bet["side"] == "a" else bet["team_b"]
            potential = round(int(bet["amount"]) * float(bet["odds"]))
            lines.append(
                f"• #{bet['match_id']} {escape(str(team))} · {money(int(bet['amount']))} "
                f"× {float(bet['odds']):.2f} → {money(potential)}"
            )
    else:
        lines.append("\nОдиночных ставок нет.")

    if expresses:
        lines.append(f"\n🎟 Экспрессы ({len(expresses)}):")
        for express in expresses[:5]:
            potential = round(int(express["stake"]) * float(express["odds"]))
            lines.append(
                f"• #{express['id']} · кэф {float(express['odds']):.2f} · "
                f"{money(int(express['stake']))} → {money(potential)}"
            )
    return "\n".join(lines)


def top_text(rows: list[Mapping[str, Any]]) -> str:
    medals = ["🥇", "🥈", "🥉"] + ["▫️"] * 7
    lines = ["🏆 <b>Топ по балансу</b>", ""]
    for index, row in enumerate(rows):
        if row["full_name"]:
            name = str(row["full_name"])
        elif row["username"]:
            name = f"@{row['username']}"
        else:
            name = f"id{row['user_id']}"
        lines.append(f"{medals[index]} {escape(name)} — {money(int(row['balance']))}")
    return "\n".join(lines)


def history_text(rows: list[Mapping[str, Any]]) -> str:
    lines = ["📜 <b>Последние операции</b>", ""]
    for row in rows:
        delta = int(row["delta"])
        sign = "+" if delta >= 0 else "−"
        lines.append(
            f"{sign}{money(abs(delta))} · {escape(str(row['reason']))} "
            f"· {fmt_dt(int(row['created_at']))}"
        )
    return "\n".join(lines)


def cart_text(legs: list[dict[str, Any]], balance: int) -> str:
    if not legs:
        return (
            "🎟 <b>Экспресс пуст</b>\n\n"
            "Открой матч и жми «🎟 … в экспресс», чтобы добавить исход.\n"
            f"Минимум 2 исхода. Баланс: {money(balance)}"
        )

    total = 1.0
    lines = ["🎟 <b>Экспресс</b>", ""]
    for index, leg in enumerate(legs, start=1):
        total *= float(leg["odds"])
        lines.append(
            f"{index}. {escape(str(leg['team']))} · кэф <b>{float(leg['odds']):.2f}</b>\n"
            f"   {escape(str(leg['match']))} · матч #{leg['match_id']}"
        )

    lines += ["", f"Исходов: {len(legs)} · итоговый кэф: <b>{total:.2f}</b>"]
    if len(legs) < 2:
        lines.append("Добавь ещё минимум один исход 👇")
    else:
        lines += [
            f"Баланс: {money(balance)} · потолок кэфа: {settings.max_express_odds:.0f}",
            "",
            "Выбери сумму ставки 👇",
        ]
    return "\n".join(lines)


def stats_text(stats: Mapping[str, int], mode: str, platform_python: str, aiogram_version: str) -> str:
    return (
        "<b>Статистика</b>\n"
        f"Игроков: {stats['users']}\n"
        f"Монет на руках: {money(stats['balance_sum'])}\n"
        f"Матчей в работе: {stats['active_matches']}\n"
        f"Ставок сделано: {stats['bets']} на {money(stats['bets_sum'])}\n\n"
        f"Python {platform_python} · aiogram {aiogram_version} · режим {mode}"
    )


def admin_results_text(rows: list[Mapping[str, Any]]) -> str:
    if not rows:
        return (
            "🛠 <b>Очередь результатов пуста</b>\n\n"
            "Матчи с неясным исходом появятся здесь автоматически."
        )
    lines = ["🛠 <b>Ждут подтверждения</b>", ""]
    for match in rows:
        keys = match.keys() if hasattr(match, "keys") else match
        score = f" · счёт {match['score_text']}" if "score_text" in keys and match["score_text"] else ""
        lines.append(
            f"#{match['id']} {fmt_time(int(match['start_time']))} · "
            f"{escape(short_tournament(str(match['tournament'])))}{score}\n"
            f"   {escape(str(match['team_a']))} {odds(match['odds_a'])} — "
            f"{odds(match['odds_b'])} {escape(str(match['team_b']))}"
        )
    lines += ["", "Кто выиграл? Жми кнопку под матчем 👇"]
    return "\n".join(lines)
