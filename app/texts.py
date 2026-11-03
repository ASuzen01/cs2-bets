"""Все тексты бота. Правьте смело — логика от них не зависит."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime
from html import escape
from typing import Any

from app.config import settings


def money(amount: int) -> str:
    return f"{amount:,}".replace(",", "\u202f") + f" {settings.currency}"

def odds(value: float | None) -> str:
    return f"{value:.2f}" if value else "—"

def score_text(match: Mapping[str, Any]) -> str | None:
    """Счёт по картам, если он уже есть в базе."""
    keys = match.keys() if hasattr(match, "keys") else ()
    if "score_a" not in keys or "score_b" not in keys:
        return None
    score_a, score_b = match["score_a"], match["score_b"]
    if score_a is None or score_b is None:
        return None
    return f"{int(score_a)}:{int(score_b)}"

def fmt_time(ts: int) -> str:
    """02.11 21:00 — по времени сервера."""
    return datetime.fromtimestamp(ts).strftime("%d.%m %H:%M")

def short_tournament(name: str) -> str:
    """'Counter-Strike. IEM Пекин. Групповой этап. Bo3' -> 'IEM Пекин · Bo3'."""
    parts = [part.strip() for part in name.split(".") if part.strip()]
    parts = [part for part in parts if part.lower() not in ("counter-strike", "cs2")]
    return " · ".join(parts[:2]) if parts else name

START = """🍀 <b>Добрая лудка</b>

Привет, {name}! На счету: <b>{balance}</b>

CS2, виртуальные монеты, живые кэфы. Заходи и пробуй 🎲

Кнопка ниже 👇
"""

GROUP_START = """🍀 <b>Добрая лудка</b>

{name}, привет! Ставки на CS2 виртуальными монетами, у каждого свой баланс.

Жми кнопку ниже — откроется бот, а там уже приложение 👇
Уведомления о ставках приходят в личку с ботом.
"""

GROUP_HINT = "Ставки и баланс — в приложении: жми кнопку ниже, откроется бот с кнопкой приложения."

ERROR_TEXT = "😔 Что-то сломалось на моей стороне. Уже смотрю логи, попробуй ещё раз."

def rules_text() -> str:
    """Правила — только про экспрессы, остальное игроку знать не обязательно."""
    return (
        "<b>Экспресс</b>\n\n"
        "Экспресс — это несколько исходов в одной ставке, кэфы перемножаются.\n"
        "Зашли все исходы — выплата по итоговому кэфу. Не зашёл хотя бы один — сгорает вся ставка.\n"
        "Отменённый матч считается с кэфом 1.0, то есть просто выпадает из экспресса.\n\n"
        "Минимум исходов: 2, максимума нет. Потолка по кэфу нет.\n"
        f"Минимальная ставка: {money(settings.min_bet)}."
    )

def match_card(match: Mapping[str, Any], *, with_bets: bool = True) -> str:
    team_a = escape(str(match["team_a"]))
    team_b = escape(str(match["team_b"]))
    status = str(match["status"])

    live_open = settings.live_betting
    head = {
        "open": "🟢 приём ставок",
        "live": "🔴 live · приём ставок открыт" if live_open else "🔴 идёт матч, ставки закрыты",
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

    score = score_text(match)
    if score and status in ("live", "awaiting"):
        lines.append(f"📊 Счёт по картам: <b>{score}</b>")

    if status == "finished" and match["winner"]:
        winner = team_a if match["winner"] == "a" else team_b
        lines.append(f"Победитель: <b>{winner}</b>")

    if with_bets:
        lines.append("")
        if status == "open" or (status == "live" and live_open):
            lines.append("Ставки — в приложении, кнопка ниже 👇")
        else:
            lines.append("Приём ставок закрыт · результат подтянется в приложение автоматически")
    return "\n".join(lines)

def settled_text(result: Mapping[str, Any], match_id: int, score: str | None = None) -> str:
    score_part = f" ({score})" if score else ""
    return (
        f"🏁 Матч #{match_id} рассчитан: победила <b>{escape(str(result['team']))}</b>{score_part}\n"
        f"Выплачено: {money(int(result['paid']))} · ставок: {result['bets']} · "
        f"угадали: {result['winners']}"
    )

def football_settled_text(result: Mapping[str, Any], match_id: int, score: str | None = None) -> str:
    """Итог футбольного матча: счёт, сколько выплачено, сколько ставок угадало."""
    score_part = f" · счёт <b>{score}</b>" if score else ""
    refunded = int(result.get("refunded") or 0)
    refund_part = f" · возвращено: {money(refunded)}" if refunded else ""
    return (
        f"⚽️ Матч #{match_id}: {escape(str(result['team_a']))} — "
        f"{escape(str(result['team_b']))}{score_part}\n"
        f"Выплачено: {money(int(result['paid']))} · ставок: {result['bets']} · "
        f"угадали: {result['winners']}{refund_part}"
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

def bet_result(
    *,
    won: bool,
    void: bool,
    team_a: str,
    team_b: str,
    picked: str,
    amount: int,
    odds_value: float,
    payout: int,
    balance: int,
    score: str | None = None,
    market_label: str | None = None,
) -> str:
    """Персональное сообщение игроку по его одиночной ставке.

    market_label — «Карта 1», если ставка была на конкретную карту, а не на матч.
    """
    score_part = f" · {score}" if score else ""
    market_part = f" · <b>{escape(str(market_label))}</b>" if market_label else ""
    head = f"{escape(str(team_a))} — {escape(str(team_b))}{market_part}{score_part}"
    stake_line = f"Ставка {money(amount)} на <b>{escape(str(picked))}</b> · кэф {odds_value:.2f}"

    if void:
        return (
            "⚪️ <b>Ставка возвращена.</b>\n"
            f"{head}\n"
            "Матч отменён, ставка не считается.\n"
            f"Возврат <b>{money(payout)}</b> · баланс <b>{money(balance)}</b>"
        )

    if won:
        return (
            "✅ <b>Ставка сыграла!</b>\n"
            f"{head}\n"
            f"{stake_line}\n"
            f"Выплата <b>{money(payout)}</b> · баланс <b>{money(balance)}</b>"
        )

    return (
        "❌ <b>Ставка не зашла.</b>\n"
        f"{head}\n"
        f"{stake_line}\n"
        f"Ставка сгорела · баланс <b>{money(balance)}</b>"
    )


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
