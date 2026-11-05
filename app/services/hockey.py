"""Хоккей: КХЛ, НХЛ и чемпионат мира из линии Fonbet.

Что предлагаем
--------------
Две ставки на исход и тоталы:

* **Основное время** — П1/X/П2 по трём периодам. Кэфы берём из линии как есть
  (факторы 921/922/923: у хоккея это исход основного времени), поэтому ничья
  здесь настоящая и считается по счёту после третьего периода.
* **Победа в матче** — П1/П2 без ничьей: кто выиграет с учётом овертайма и
  буллитов. Fonbet отдаёт такие кэфы не всегда и в неочевидных факторах, поэтому
  считаем сами той же моделью: ничья в основное время с равной вероятностью
  уходит в овертайм за любую из команд (овертайм — по сути лотерея 50/50,
  буллиты тоже).

Тоталы считаем по основному времени: это то, что обычно и понимают под «тоталом
матча» в хоккее, и то, что мы можем посчитать честно — из той же матрицы счёта.

Про счёт
--------
Для расчёта нужны два счёта: счёт основного времени (`reg_a`/`reg_b`) и итоговый
(`score_a`/`score_b`, с овертаймом). НХЛ ищется в фиде результатов livescore
(там есть `Tr1OR`/`Tr2OR` — счёт после трёх периодов), КХЛ в этом фиде нет, поэтому
такие матчи ждут ручного расчёта админом.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any

from app.services.football import (
    MARGIN,
    MAX_GOALS,
    _price,
    implied_probabilities,
    is_placeholder_team,
    score_matrix,
    segments_of,
    solve_lambdas,
)

# id факторов Fonbet для исхода основного времени — те же, что у футбола
FACTOR_HOME = 921
FACTOR_DRAW = 922
FACTOR_AWAY = 923

# тоталы: в хоккее линия гуляет вокруг 5 голов, а не 2.5, как в футболе
TOTAL_LINES = (3.5, 4.5, 5.5, 6.5)

# овертайм считаем честной монеткой: ничья в основное время делится пополам
OVERTIME_HOME_SHARE = 0.5


@dataclass(slots=True)
class HockeyMatch:
    event_id: int
    tournament: str
    team_a: str
    team_b: str
    start_time: int
    odds_1: float | None = None
    odds_x: float | None = None
    odds_2: float | None = None
    score_a: int | None = None
    score_b: int | None = None
    is_live: bool = False
    finished: bool = False

    @property
    def has_result_odds(self) -> bool:
        return bool(self.odds_1 and self.odds_x and self.odds_2)


def _int_or_none(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _is_hockey(tournament: str, include: Iterable[str], exclude: Iterable[str]) -> bool:
    name = tournament.lower()
    if not any(word.strip().lower() and word.strip().lower() in name for word in include):
        return False
    return not any(word.strip().lower() and word.strip().lower() in name for word in exclude)


# id хоккея в линии Fonbet (у сегментов-турниров parentId = 2)
SPORT_ID_HOCKEY = 2


def parse_hockey(
    packet: dict[str, Any],
    include: Iterable[str],
    exclude: Iterable[str],
    *,
    sport_id: int | None = SPORT_ID_HOCKEY,
) -> list[HockeyMatch]:
    """Матчи хоккея нужных турниров с исходом основного времени."""
    segments = segments_of(
        packet,
        lambda name: _is_hockey(name, include, exclude),
        sport_id=sport_id,
    )
    if not segments:
        return []

    factors = {b["e"]: b.get("factors", []) for b in packet.get("customFactors", []) if "e" in b}
    miscs = {m["id"]: m for m in packet.get("eventMiscs", []) if "id" in m}
    lives = {i["eventId"]: i for i in packet.get("liveEventInfos", []) if "eventId" in i}

    result: list[HockeyMatch] = []
    for event in packet.get("events", []):
        if event.get("level") != 1:
            continue
        tournament = segments.get(event.get("sportId"))
        if tournament is None:
            continue
        team_a, team_b = event.get("team1"), event.get("team2")
        if not team_a or not team_b:
            continue
        if is_placeholder_team(team_a) or is_placeholder_team(team_b):
            continue

        odds: dict[int, float] = {}
        for factor in factors.get(event["id"], []):
            value = factor.get("v")
            if value and factor.get("f") in (FACTOR_HOME, FACTOR_DRAW, FACTOR_AWAY):
                odds[int(factor["f"])] = float(value)
        if len(odds) != 3:
            continue      # без полного исхода основного времени матч не берём

        misc = miscs.get(event["id"], {})
        live = lives.get(event["id"], {})
        result.append(
            HockeyMatch(
                event_id=int(event["id"]),
                tournament=tournament,
                team_a=str(team_a),
                team_b=str(team_b),
                start_time=int(event.get("startTime") or 0),
                odds_1=odds[FACTOR_HOME],
                odds_x=odds[FACTOR_DRAW],
                odds_2=odds[FACTOR_AWAY],
                score_a=_int_or_none(misc.get("score1")),
                score_b=_int_or_none(misc.get("score2")),
                is_live=event.get("place") == "live" or bool(live),
                finished=bool(live.get("finished") or misc.get("finished")),
            )
        )
    return result


# ------------------------------------------------------------------ рынки
def build_markets(match: HockeyMatch) -> list[dict[str, Any]]:
    """Роспись хоккейного матча: основное время, победа в матче, тоталы."""
    if not match.has_result_odds:
        return []
    assert match.odds_1 and match.odds_x and match.odds_2
    p_home, p_draw, p_away = implied_probabilities(match.odds_1, match.odds_x, match.odds_2)
    lambda_home, lambda_away = solve_lambdas(p_home, p_draw, p_away)
    matrix = score_matrix(lambda_home, lambda_away)

    markets: list[dict[str, Any]] = []

    def add(key: str, param: str, label: str, group: str, probability: float,
            odds: float | None = None) -> None:
        if probability <= 0:
            return
        markets.append(
            {
                "key": key,
                "param": param,
                "label": label,
                "group": group,
                "odds": odds if odds is not None else _price(probability),
            }
        )

    # основное время — реальные кэфы книги
    add("reg", "1", "П1", "Основное время", p_home, round(match.odds_1, 2))
    add("reg", "x", "X", "Основное время", p_draw, round(match.odds_x, 2))
    add("reg", "2", "П2", "Основное время", p_away, round(match.odds_2, 2))

    # победа в матче: ничья в основное время уходит в овертайм/буллиты
    final_home = p_home + p_draw * OVERTIME_HOME_SHARE
    add("final", "1", f"Победа: {match.team_a}", "Победа в матче", final_home,
        _price(final_home, MARGIN))
    add("final", "2", f"Победа: {match.team_b}", "Победа в матче", 1 - final_home,
        _price(1 - final_home, MARGIN))

    for line in TOTAL_LINES:
        over = sum(
            matrix[i][j]
            for i in range(MAX_GOALS + 1)
            for j in range(MAX_GOALS + 1)
            if i + j > line
        )
        under = sum(
            matrix[i][j]
            for i in range(MAX_GOALS + 1)
            for j in range(MAX_GOALS + 1)
            if i + j < line
        )
        add("total", f"over:{line}", f"ТБ {line}", "Тотал (основное время)", over)
        add("total", f"under:{line}", f"ТМ {line}", "Тотал (основное время)", under)

    return markets


def market_outcome(
    key: str, param: str, reg_a: int, reg_b: int, final_a: int, final_b: int
) -> str:
    """'win' / 'lose' / 'void' для рынка хоккея.

    `reg_a`/`reg_b` — счёт основного времени, `final_a`/`final_b` — с учётом
    овертайма и буллитов.
    """
    if key == "reg":
        winner = "1" if reg_a > reg_b else ("2" if reg_b > reg_a else "x")
        return "win" if winner == param else "lose"
    if key == "final":
        if final_a == final_b:
            # счёт с ОТ не может быть равным: если данные такие — считаем возвратом
            return "void"
        winner = "1" if final_a > final_b else "2"
        return "win" if winner == param else "lose"
    if key == "total":
        kind, _, line = param.partition(":")
        goals = reg_a + reg_b
        limit = float(line)
        hit = goals > limit if kind == "over" else goals < limit
        return "win" if hit else "lose"
    return "void"


def market_label(key: str, param: str) -> str:
    """Подпись рынка для истории ставок и уведомлений."""
    if key == "reg":
        return {"1": "П1 (основное время)", "x": "X (основное время)", "2": "П2 (основное время)"}.get(
            param, "Исход (основное время)"
        )
    if key == "final":
        return "П1 (с ОТ)" if param == "1" else "П2 (с ОТ)"
    if key == "total":
        kind, _, line = param.partition(":")
        return f"ТБ {line} (основное время)" if kind == "over" else f"ТМ {line} (основное время)"
    return f"{key} {param}"


def side_code(key: str, param: str) -> str:
    """Сторона ставки в БД: `h:total:over:5.5`. Префикс `h:` отличает хоккей."""
    return f"h:{key}:{param}"


def parse_side(side: str) -> tuple[str, str] | None:
    """Обратная операция: `h:total:over:5.5` → ('total', 'over:5.5')."""
    if not side.startswith("h:"):
        return None
    parts = side.split(":", 2)
    if len(parts) != 3:
        return None
    return parts[1], parts[2]


_CACHE: dict[tuple[Any, ...], list[dict[str, Any]]] = {}


def markets_of(row: Any) -> list[dict[str, Any]]:
    """Роспись по строке матча из БД (только предматчевая, как у футбола)."""
    import time as _time

    if str(row["sport"]) != "hockey" or str(row["status"]) != "open":
        return []
    if int(row["start_time"]) <= int(_time.time()):
        return []
    key = (int(row["id"]), row["odds_a"], row["odds_x"], row["odds_b"])
    cached = _CACHE.get(key)
    if cached is not None:
        return cached

    match = HockeyMatch(
        event_id=int(row["fonbet_id"]) if "fonbet_id" in row.keys() else 0,  # noqa: SIM118
        tournament=str(row["tournament"]),
        team_a=str(row["team_a"]),
        team_b=str(row["team_b"]),
        start_time=int(row["start_time"]),
        odds_1=float(row["odds_a"]) if row["odds_a"] else None,
        odds_x=float(row["odds_x"]) if row["odds_x"] else None,
        odds_2=float(row["odds_b"]) if row["odds_b"] else None,
    )
    markets = build_markets(match)
    if len(_CACHE) > 2000:
        _CACHE.clear()
    _CACHE[key] = markets
    return markets
