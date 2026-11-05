"""Нога экспресса: один код исхода — один кэф.

Один и тот же код живёт в одиночной ставке и в экспрессе:

* `a` / `x` / `b` — исход матча; X в хоккее — ничья в основное время;
* `f:<рынок>:<параметр>` — рынок футбола (`f:total:over:2.5`, `f:btts:yes`…);
* `h:<рынок>:<параметр>` — рынок хоккея (`h:final:1`, `h:total:over:5.5`…).

Модуль отвечает на три вопроса, которые нужны и при ставке, и при расчёте:
какой кэф у этого исхода сейчас, как он называется для игрока и зашёл ли он
после матча.
"""

from __future__ import annotations

from typing import Any

from app.services import football, hockey


def is_market_side(side: str) -> bool:
    """`f:…` / `h:…` — это рынок, всё остальное — исход матча."""
    return str(side).startswith(("f:", "h:"))


def _market(row: Any, side: str) -> dict[str, Any] | None:
    """Элемент росписи, если код исхода ведёт на рынок."""
    parsed = football.parse_side(side)
    markets = football.markets_of(row) if parsed else None
    if parsed is None:
        parsed = hockey.parse_side(side)
        markets = hockey.markets_of(row) if parsed else None
    if parsed is None or not markets:
        return None
    key, param = parsed
    for market in markets:
        if market["key"] == key and market["param"] == param:
            return market
    return None


def odds(row: Any, side: str) -> float | None:
    """Текущий кэф исхода: колонки матча для 1X2, роспись — для рынков."""
    sport = str(row["sport"])
    if not is_market_side(side):
        if side == "x" and sport not in {"football", "hockey"}:
            return None                     # ничьей нет в CS2 и других матчах без X
        column = {"a": "odds_a", "x": "odds_x", "b": "odds_b"}.get(str(side))
        if column is None:
            return None
        value = row[column]
        return float(value) if value else None

    market = _market(row, side)
    if market is None or not market.get("odds"):
        return None
    return float(market["odds"])


def label(row: Any, side: str) -> str:
    """Как исход называется в истории и в списке ног экспресса."""
    team_a, team_b = str(row["team_a"]), str(row["team_b"])
    if not is_market_side(side):
        if side == "a":
            return team_a
        if side == "b":
            return team_b
        if side == "x":
            return "Ничья (осн. время)" if _sport(row) == "hockey" else "Ничья"
        return str(side)

    parsed_football = football.parse_side(side)
    if parsed_football is not None:
        return football.market_label(*parsed_football)

    parsed_hockey = hockey.parse_side(side)
    if parsed_hockey is not None:
        # в списке ног «П1 (с ОТ)» ничего не говорит: две ноги из разных матчей
        # выглядят одинаково, поэтому подписываем команду
        key, param = parsed_hockey
        if key == "final":
            return f"{team_a if param == '1' else team_b} (с ОТ)"
        if key == "reg":
            team = {"1": team_a, "2": team_b, "x": "Ничья"}.get(param)
            return f"{team} (осн. время)" if team else hockey.market_label(key, param)
        return hockey.market_label(*parsed_hockey)
    return str(side)


def outcome(row: Any, side: str, winner: str | None) -> str:
    """`won` / `lost` / `void` для ноги после расчёта матча.

    `winner` — исход матча из расчёта (`a`/`x`/`b`) или None, если матч отменён.
    Рынки считаем по счёту: футбол — по итоговому, хоккей — по основному времени
    или с учётом овертайма, смотря какой рынок.
    """
    if winner is None:
        return "void"

    parsed_football = football.parse_side(side)
    if parsed_football is not None:
        return _result(football.market_outcome(*parsed_football, _score_a(row), _score_b(row)))

    parsed_hockey = hockey.parse_side(side)
    if parsed_hockey is not None:
        return _result(
            hockey.market_outcome(
                *parsed_hockey,
                _int(row, "reg_a", _score_a(row)),
                _int(row, "reg_b", _score_b(row)),
                _score_a(row),
                _score_b(row),
            )
        )

    if _sport(row) == "hockey" and str(side) == "x":
        # В хоккее X означает ничью за 60 минут, хотя победитель матча — команда.
        reg_a = _int(row, "reg_a", _score_a(row))
        reg_b = _int(row, "reg_b", _score_b(row))
        return "won" if reg_a == reg_b else "lost"

    return "won" if str(side) == winner else "lost"


def _sport(row: Any) -> str:
    try:
        return str(row["sport"])
    except (KeyError, IndexError, TypeError):
        return ""


def _result(outcome: str) -> str:
    return {"win": "won", "lose": "lost"}.get(outcome, "void")


def _int(row: Any, column: str, fallback: int) -> int:
    try:
        value = row[column]
    except (KeyError, IndexError, TypeError):
        return fallback
    return int(value) if value is not None else fallback


def _score_a(row: Any) -> int:
    return _int(row, "score_a", 0)


def _score_b(row: Any) -> int:
    return _int(row, "score_b", 0)


__all__ = ["is_market_side", "label", "odds", "outcome"]
