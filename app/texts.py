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

Всё внутри приложения, кнопки в чате больше не нужны:
🎮 матчи и кэфы из линии Fonbet (только тир-1)
🎟 экспрессы на 2–6 исходов
💰 баланс, бонус раз в сутки, история ставок и топ игроков

Открывай приложение — кнопка ниже 👇
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
        "бот пришлёт админу карточку с кнопками — исход подтверждают вручную.\n"
        "6. Матч отменён/не состоялся → админ отменяет его в боте, ставки возвращаются полностью.\n\n"
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


def awaiting_text(match: Mapping[str, Any], score: str | None = None) -> str:
    score_part = f"\nСчёт в линии: <b>{score}</b>" if score else ""
    return (
        f"⏳ Матч #{match['id']} закончился, но я не смог однозначно определить победителя."
        f"{score_part}\n"
        f"{escape(str(match['team_a']))} 🆚 {escape(str(match['team_b']))}\n"
        "Админ, подтверди исход кнопкой ниже 👇"
    )


# ------------------------------------------------------------------ экспрессы

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









    return "\n".join(lines)
