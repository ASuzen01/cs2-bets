"""Футбол: матчи Лиги Чемпионов из линии Fonbet и широкая роспись рынков.

Почему рынки считаем сами
-------------------------
Fonbet отдаёт по футбольному матчу 400+ коэффициентов, но без словаря: приходят
только числовые id факторов и параметры вида `pt="+2.5"`. Угадывать по id, что
`f=2327` — это «точный счёт 2:3», а не «тотал 4-й карты», нельзя: ошибка в один
номер превращается в неправильную подпись и неправильный расчёт ставки.

Поэтому берём из линии то, что читается однозначно — исход 1X2 (факторы
921/922/923, они же используются для CS2) — и разворачиваем из него всю роспись
моделью Пуассона. Плюсы:

* рынки не противоречат друг другу (тотал 2.5 и точный счёт считаются из одного
  распределения, арбитража «между своими же рынками» не возникает);
* расчёт однозначен: у нас есть тип рынка и параметр, а не чужой числовой id;
* линия работает, даже если Fonbet перетасует свою нумерацию факторов.

Модель калибруется по трём вероятностям (П1/X/П2) — подбираем интенсивности
голов хозяев и гостей так, чтобы их вероятности совпали с книгой, дальше считаем
всё остальное по матрице счетов.
"""

from __future__ import annotations

import math
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any

# id факторов Fonbet: 921 — победа первой команды, 922 — ничья, 923 — второй
FACTOR_HOME = 921
FACTOR_DRAW = 922
FACTOR_AWAY = 923

MAX_GOALS = 9                 # 0..9 голов на команду — дальше вероятность ничтожна
MARGIN = 1.05                 # наша маржа на производных рынках (реальный 1X2 — как у книги)
MAX_ODDS = 500.0
MIN_ODDS = 1.05

# турнирные маркеры плей-офф: там есть смысл в рынке «проход дальше»
KNOCKOUT_MARKERS = ("1/8", "1/4", "1/2", "1/16", "стыков", "плей-офф", "финал", "квал")
# два матча в паре: один матч не решает, кто прошёл — рынок не предлагаем
LEG_MARKERS = ("первые матчи", "вторые матчи", "ответные матчи", "1-е матчи", "2-е матчи")

# Fonbet иногда отдаёт событие без названий команд — «Хозяева — Гости». Такое
# событие нельзя ни показать, ни рассчитать: в фиде результатов его не найти.
PLACEHOLDER_TEAMS = {
    "хозяева",
    "гости",
    "хозяева поля",
    "гости поля",
    "команда 1",
    "команда 2",
    "team 1",
    "team 2",
    "home",
    "away",
}

_TOTAL_LINES = (0.5, 1.5, 2.5, 3.5, 4.5)
_HANDICAPS = (1.5, 0.5, -0.5, -1.5, -2.5)
_TEAM_TOTALS = (0.5, 1.5, 2.5)
_EXACT_SCORES = tuple((a, b) for a in range(5) for b in range(5))


@dataclass(slots=True)
class FootballMatch:
    """Матч из линии: только то, что читается из пакета однозначно."""

    event_id: int
    tournament: str
    team_a: str
    team_b: str
    start_time: int
    odds_1: float | None
    odds_x: float | None
    odds_2: float | None
    score_a: int | None = None
    score_b: int | None = None
    is_live: bool = False
    finished: bool = False

    @property
    def has_result_odds(self) -> bool:
        return bool(self.odds_1 and self.odds_x and self.odds_2)


def is_placeholder_team(name: str) -> bool:
    """«Хозяева»/«Гости» вместо названий — событие ещё не распознано."""
    return " ".join(str(name or "").lower().split()) in PLACEHOLDER_TEAMS


# Турниры сборных: в приложении они лежат в подменю «Сборные», всё остальное — «Клубы».
# Признак ищем в названии турнира: клубные турниры сборных в названии не упоминают,
# а турниры сборных всегда называют либо сам турнир, либо «сборные».
NATIONAL_TOKENS: tuple[str, ...] = (
    "лига наций", "чемпионат мира", "чемпионат европы", "кубок америки", "копа америка",
    "кубок конфедераций", "кубок наций", "олимпийск", "кубок мира",
    "сборная", "сборных", "еврохоккейтур", "чм ", "чм.", "че ", "че.",
)


def is_national_tournament(name: str) -> bool:
    """Турнир сборных? «Лига наций УЕФА» — да, «Лига Чемпионов УЕФА» — нет."""
    text = f"{name.strip().lower()} "
    return any(token in text for token in NATIONAL_TOKENS)


def tournament_category(name: str) -> str:
    """Подменю матча: 'national' (сборные) или 'clubs' (клубы)."""
    return "national" if is_national_tournament(name) else "clubs"


def _is_football(tournament: str, include: Iterable[str], exclude: Iterable[str]) -> bool:
    name = tournament.lower()
    if not any(word.strip().lower() and word.strip().lower() in name for word in include):
        return False
    return not any(word.strip().lower() and word.strip().lower() in name for word in exclude)


def is_knockout(tournament: str) -> bool:
    name = tournament.lower()
    return any(marker in name for marker in KNOCKOUT_MARKERS)


def has_qualify_market(tournament: str) -> bool:
    """«Проход дальше» — только там, где пара решается одним матчем.

    В двухматчевых парах (1/8, 1/4, полуфиналы) победитель первого матча ещё
    ничего не значит, поэтому рынок не предлагаем — считать его по счёту одной
    игры было бы просто неправдой.
    """
    name = tournament.lower()
    if any(marker in name for marker in LEG_MARKERS):
        return False
    return is_knockout(tournament)


# id футбола в линии Fonbet: сегменты (турниры) ссылаются на него через parentId
SPORT_ID_FOOTBALL = 1


def segments_of(
    packet: dict[str, Any],
    match: Any,
    *,
    sport_id: int | None,
) -> dict[int, str]:
    """Сегменты линии, которые относятся к нужному спорту и проходят фильтр.

    Проверять только название турнира нельзя: «Лига Чемпионов» есть и в футболе,
    и в хоккее, «Бундеслига» — и в футболе, и в баскетболе. Поэтому сегмент берём
    ещё и по родительскому виду спорта (`parentId`).
    """
    sports = {s.get("id"): s for s in packet.get("sports", [])}
    has_parent = any(
        s.get("parentId") is not None for s in sports.values() if s.get("kind") == "segment"
    )
    segments: dict[int, str] = {}
    for seg_id, sport in sports.items():
        if sport.get("kind") != "segment":
            continue
        name = str(sport.get("name") or "")
        if not match(name):
            continue
        if has_parent and sport_id is not None and sport.get("parentId") != sport_id:
            continue
        segments[seg_id] = name
    return segments


def parse_football(
    packet: dict[str, Any],
    include: Iterable[str],
    exclude: Iterable[str],
    *,
    sport_id: int | None = SPORT_ID_FOOTBALL,
) -> list[FootballMatch]:
    """Матчи футбола нужных турниров с исходом 1X2."""
    segments = segments_of(
        packet,
        lambda name: _is_football(name, include, exclude),
        sport_id=sport_id,
    )
    if not segments:
        return []

    factors = {b["e"]: b.get("factors", []) for b in packet.get("customFactors", []) if "e" in b}
    miscs = {m["id"]: m for m in packet.get("eventMiscs", []) if "id" in m}
    lives = {i["eventId"]: i for i in packet.get("liveEventInfos", []) if "eventId" in i}

    result: list[FootballMatch] = []
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
            continue      # «Хозяева — Гости»: команды не распознаны

        odds: dict[int, float] = {}
        for factor in factors.get(event["id"], []):
            value = factor.get("v")
            if value and factor.get("f") in (FACTOR_HOME, FACTOR_DRAW, FACTOR_AWAY):
                odds[int(factor["f"])] = float(value)
        if len(odds) != 3:
            continue      # нет полного исхода — такой матч не берём

        misc = miscs.get(event["id"], {})
        live = lives.get(event["id"], {})
        result.append(
            FootballMatch(
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


# ------------------------------------------------------------------ модель
def implied_probabilities(odds_1: float, odds_x: float, odds_2: float) -> tuple[float, float, float]:
    """Вероятности из кэфов книги, нормированные к единице (маржа книги убрана)."""
    raw = [1 / odds_1, 1 / odds_x, 1 / odds_2]
    total = sum(raw)
    return raw[0] / total, raw[1] / total, raw[2] / total


def _poisson(lam: float, k: int) -> float:
    return math.exp(-lam) * lam**k / math.factorial(k)


def score_matrix(lambda_home: float, lambda_away: float) -> list[list[float]]:
    """Матрица вероятностей счёта [голы хозяев][голы гостей] до MAX_GOALS."""
    home = [_poisson(lambda_home, i) for i in range(MAX_GOALS + 1)]
    away = [_poisson(lambda_away, i) for i in range(MAX_GOALS + 1)]
    return [[home[i] * away[j] for j in range(MAX_GOALS + 1)] for i in range(MAX_GOALS + 1)]


def _home_draw_away(matrix: list[list[float]]) -> tuple[float, float, float]:
    home = sum(matrix[i][j] for i in range(len(matrix)) for j in range(len(matrix)) if i > j)
    draw = sum(matrix[i][i] for i in range(len(matrix)))
    away = sum(matrix[i][j] for i in range(len(matrix)) for j in range(len(matrix)) if i < j)
    return home, draw, away


def solve_lambdas(
    p_home: float, p_draw: float, p_away: float
) -> tuple[float, float]:
    """Подбирает интенсивности голов хозяев и гостей под вероятности книги.

    Сначала грубая сетка, потом уточнение вокруг лучшей точки: так предсказуемо
    по времени и не требует scipy/numpy на бесплатном инстансе.
    """
    def error(lh: float, la: float) -> float:
        home, draw, away = _home_draw_away(score_matrix(lh, la))
        return (home - p_home) ** 2 + (draw - p_draw) ** 2 + (away - p_away) ** 2

    best = (0.1, 0.1, 1.0)
    lam = 0.15
    while lam <= 4.05:
        for lh in (lam * 0.5, lam, lam * 1.5):
            for la in (lam * 0.5, lam, lam * 1.5):
                if lh <= 0 or la <= 0:
                    continue
                value = error(lh, la)
                if value < best[2]:
                    best = (lh, la, value)
        lam += 0.3

    lh, la, _ = best
    step = 0.15
    for _ in range(6):
        for candidate_home in (lh - step, lh, lh + step):
            for candidate_away in (la - step, la, la + step):
                if candidate_home <= 0.02 or candidate_away <= 0.02:
                    continue
                value = error(candidate_home, candidate_away)
                if value < best[2]:
                    best = (candidate_home, candidate_away, value)
        lh, la, _ = best
        step /= 2
    return lh, la


def _price(probability: float, margin: float = MARGIN) -> float:
    if probability <= 0.0005:
        return MAX_ODDS
    odds = 1 / (probability * margin)
    return round(min(MAX_ODDS, max(MIN_ODDS, odds)), 2)


# ------------------------------------------------------------------ рынки
def build_markets(match: FootballMatch) -> list[dict[str, Any]]:
    """Вся роспись матча: группы, подписи, кэфы.

    `param` — то, что нужно для расчёта: он же попадает в ставку, поэтому
    расчёт никогда не зависит от того, что мы показали на экране.
    """
    if not match.has_result_odds:
        return []
    assert match.odds_1 and match.odds_x and match.odds_2
    p_home, p_draw, p_away = implied_probabilities(match.odds_1, match.odds_x, match.odds_2)
    lambda_home, lambda_away = solve_lambdas(p_home, p_draw, p_away)
    matrix = score_matrix(lambda_home, lambda_away)

    def prob(predicate) -> float:
        return sum(
            matrix[i][j]
            for i in range(MAX_GOALS + 1)
            for j in range(MAX_GOALS + 1)
            if predicate(i, j)
        )

    markets: list[dict[str, Any]] = []

    def add(key: str, param: str, label: str, group: str, probability: float, odds: float | None = None) -> None:
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

    # исход — реальные кэфы книги, остальное считаем сами
    add("result", "1", "П1", "Исход", p_home, round(match.odds_1, 2))
    add("result", "x", "X", "Исход", p_draw, round(match.odds_x, 2))
    add("result", "2", "П2", "Исход", p_away, round(match.odds_2, 2))

    add("dc", "1x", "1X", "Двойной шанс", p_home + p_draw)
    add("dc", "12", "12", "Двойной шанс", p_home + p_away)
    add("dc", "x2", "X2", "Двойной шанс", p_draw + p_away)

    for line in _TOTAL_LINES:
        add("total", f"over:{line}", f"ТБ {line}", "Тотал голов", prob(lambda i, j, line_=line: i + j > line_))
        add("total", f"under:{line}", f"ТМ {line}", "Тотал голов", prob(lambda i, j, line_=line: i + j < line_))

    add("btts", "yes", "Да", "Обе забьют", prob(lambda i, j: i > 0 and j > 0))
    add("btts", "no", "Нет", "Обе забьют", prob(lambda i, j: i == 0 or j == 0))

    for handicap in _HANDICAPS:
        add(
            "handicap",
            f"1:{handicap}",
            f"Ф1 ({handicap:+g})",
            "Фора",
            prob(lambda i, j, h=handicap: i + h > j),
        )
        add(
            "handicap",
            f"2:{-handicap}",
            f"Ф2 ({-handicap:+g})",
            "Фора",
            prob(lambda i, j, h=-handicap: j + h > i),
        )

    for line in _TEAM_TOTALS:
        add(
            "team_total", f"1:over:{line}", f"ИТ1Б {line}", "Тотал команды",
            prob(lambda i, j, line_=line: i > line_),
        )
        add(
            "team_total", f"1:under:{line}", f"ИТ1М {line}", "Тотал команды",
            prob(lambda i, j, line_=line: i < line_),
        )
        add(
            "team_total", f"2:over:{line}", f"ИТ2Б {line}", "Тотал команды",
            prob(lambda i, j, line_=line: j > line_),
        )
        add(
            "team_total", f"2:under:{line}", f"ИТ2М {line}", "Тотал команды",
            prob(lambda i, j, line_=line: j < line_),
        )

    covered = 0.0
    for goals_a, goals_b in _EXACT_SCORES:
        chance = matrix[goals_a][goals_b]
        covered += chance
        add("exact", f"{goals_a}:{goals_b}", f"{goals_a}:{goals_b}", "Точный счёт", chance)
    add("exact", "other", "Другой", "Точный счёт", max(0.0, 1 - covered))

    if has_qualify_market(match.tournament):
        # ничья в основное время не решает, кто прошёл: такие ставки возвращаем
        advance = p_home + p_draw * 0.5
        add("qualify", "1", f"Пройти: {match.team_a}", "Проход дальше", advance)
        add("qualify", "2", f"Пройти: {match.team_b}", "Проход дальше", 1 - advance)

    return markets


def market_outcome(key: str, param: str, score_a: int, score_b: int) -> str:
    """'win' / 'lose' / 'void' для конкретного рынка и финального счёта."""
    if key == "result":
        winner = "1" if score_a > score_b else ("2" if score_b > score_a else "x")
        return "win" if winner == param else "lose"
    if key == "dc":
        winner = "1" if score_a > score_b else ("2" if score_b > score_a else "x")
        options = {"1x": "1x", "x2": "x2", "12": "12"}[param]
        hit = (
            (options == "1x" and winner in ("1", "x"))
            or (options == "x2" and winner in ("x", "2"))
            or (options == "12" and winner in ("1", "2"))
        )
        return "win" if hit else "lose"
    if key == "total":
        kind, _, line = param.partition(":")
        goals = score_a + score_b
        limit = float(line)
        return "win" if (goals > limit if kind == "over" else goals < limit) else "lose"
    if key == "btts":
        hit = score_a > 0 and score_b > 0
        return "win" if (hit if param == "yes" else not hit) else "lose"
    if key == "handicap":
        side, _, value = param.partition(":")
        handicap = float(value)
        own, other = (score_a, score_b) if side == "1" else (score_b, score_a)
        return "win" if own + handicap > other else "lose"
    if key == "team_total":
        side, kind, line = param.split(":")
        goals = score_a if side == "1" else score_b
        limit = float(line)
        return "win" if (goals > limit if kind == "over" else goals < limit) else "lose"
    if key == "exact":
        actual = f"{score_a}:{score_b}"
        if param == "other":
            return "win" if actual not in {f"{a}:{b}" for a, b in _EXACT_SCORES} else "lose"
        return "win" if actual == param else "lose"
    if key == "qualify":
        if score_a == score_b:
            return "void"      # кто прошёл — по этому счёту не определить
        winner = "1" if score_a > score_b else "2"
        return "win" if winner == param else "lose"
    return "void"


def market_label(key: str, param: str) -> str:
    """Подпись рынка по его ключу — для истории ставок и уведомлений."""
    names = {
        "result": {"1": "П1", "x": "X", "2": "П2"},
        "dc": {"1x": "1X", "12": "12", "x2": "X2"},
        "btts": {"yes": "Обе забьют — да", "no": "Обе забьют — нет"},
        "exact": {"other": "Другой счёт"},
        "qualify": {"1": "Проход дальше: хозяева", "2": "Проход дальше: гости"},
    }
    if key in names and param in names[key]:
        return names[key][param]
    if key == "total":
        kind, _, line = param.partition(":")
        return f"ТБ {line}" if kind == "over" else f"ТМ {line}"
    if key == "handicap":
        side, _, value = param.partition(":")
        return f"Ф{side} ({float(value):+g})"
    if key == "team_total":
        side, kind, line = param.split(":")
        return f"ИТ{side}{'Б' if kind == 'over' else 'М'} {line}"
    if key == "exact":
        return param
    return f"{key} {param}"


def side_code(key: str, param: str) -> str:
    """Сторона ставки в БД: `f:total:over:2.5`. Префикс `f:` отличает футбол."""
    return f"f:{key}:{param}"


def parse_side(side: str) -> tuple[str, str] | None:
    """Обратная операция: `f:total:over:2.5` → ('total', 'over:2.5')."""
    if not side.startswith("f:"):
        return None
    parts = side.split(":", 2)
    if len(parts) != 3:
        return None
    return parts[1], parts[2]


def _int_or_none(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


_CACHE: dict[tuple[Any, ...], list[dict[str, Any]]] = {}


def markets_of(row: Any) -> list[dict[str, Any]]:
    """Роспись по строке матча из БД.

    Кэш по (id, кэфы): во время синка кэфы меняются, тогда модель пересчитывается,
    а между запросами одного и того же экрана — нет.
    """
    import time as _time

    if str(row["sport"]) != "football" or str(row["status"]) != "open":
        return []
    if int(row["start_time"]) <= int(_time.time()):
        return []          # матч начался: роспись считается от предматчевых кэфов
    key = (int(row["id"]), row["odds_a"], row["odds_x"], row["odds_b"])
    cached = _CACHE.get(key)
    if cached is not None:
        return cached

    match = FootballMatch(
        event_id=int(row["fonbet_id"]),
        tournament=str(row["tournament"]),
        team_a=str(row["team_a"]),
        team_b=str(row["team_b"]),
        start_time=int(row["start_time"]),
        odds_1=float(row["odds_a"]) if row["odds_a"] else None,
        odds_x=float(row["odds_x"]) if row["odds_x"] else None,
        odds_2=float(row["odds_b"]) if row["odds_b"] else None,
    )
    markets = build_markets(match)
    if len(_CACHE) > 2000:      # простая защита от роста памяти
        _CACHE.clear()
    _CACHE[key] = markets
    return markets
