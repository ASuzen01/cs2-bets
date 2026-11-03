"""Футбол: парсинг линии, модель рынков и расчёт ставок по счёту."""

from __future__ import annotations

import time

import pytest

from app.db import Database
from app.services.football import (
    FootballMatch,
    build_markets,
    implied_probabilities,
    is_knockout,
    market_label,
    market_outcome,
    markets_of,
    parse_football,
    parse_side,
    side_code,
    solve_lambdas,
)

# --------------------------------------------------------------- парсинг линии
PACKET = {
    "sports": [
        {"id": 1, "kind": "sport", "name": "Футбол"},
        {"id": 113724, "parentId": 1, "kind": "segment", "name": "Лига Чемпионов УЕФА. Общий этап"},
        {"id": 63099, "parentId": 1, "kind": "segment", "name": "Лига Чемпионов Азии. Групповой этап"},
        {"id": 11918, "parentId": 1, "kind": "segment", "name": "Англия. Премьер-Лига. Сезон 26/27"},
    ],
    "events": [
        {"id": 1, "level": 1, "sportId": 113724, "team1": "Лейпциг", "team2": "Манчестер Сити",
         "startTime": 1_800_000_000, "place": "line"},
        {"id": 2, "level": 1, "sportId": 113724, "team1": "Фейеноорд", "team2": "Интер",
         "startTime": 1_800_000_000, "place": "live"},
        {"id": 3, "level": 1, "sportId": 63099, "team1": "Аль-Хиляль", "team2": "Аль-Наср",
         "startTime": 1_800_000_000, "place": "line"},
        {"id": 4, "level": 1, "sportId": 11918, "team1": "Арсенал", "team2": "Челси",
         "startTime": 1_800_000_000, "place": "line"},
        {"id": 11, "level": 2, "parentId": 1, "sportId": 113724, "name": "угловые"},
    ],
    "customFactors": [
        {"e": 1, "factors": [{"f": 921, "v": 5.3}, {"f": 922, "v": 4.9}, {"f": 923, "v": 1.53}]},
        {"e": 2, "factors": [{"f": 921, "v": 28.0}, {"f": 922, "v": 10.5}, {"f": 923, "v": 1.06}]},
        {"e": 3, "factors": [{"f": 921, "v": 2.1}, {"f": 922, "v": 3.3}, {"f": 923, "v": 3.4}]},
        {"e": 4, "factors": [{"f": 921, "v": 2.0}, {"f": 922, "v": 3.4}, {"f": 923, "v": 3.6}]},
    ],
    "eventMiscs": [{"id": 2, "score1": 0, "score2": 2}],
    "liveEventInfos": [{"eventId": 2, "finished": False}],
}

INCLUDE = ["лига чемпионов"]
EXCLUDE = ["азии", "женщ"]


def test_parse_takes_only_league_and_skips_subevents() -> None:
    matches = parse_football(PACKET, INCLUDE, EXCLUDE)
    assert [m.team_a for m in matches] == ["Лейпциг", "Фейеноорд"]
    live = matches[1]
    assert live.is_live and live.score_a == 0 and live.score_b == 2


def test_knockout_detection() -> None:
    assert is_knockout("Лига Чемпионов УЕФА. 1/8 финала")
    assert not is_knockout("Лига Чемпионов УЕФА. Общий этап")


def test_qualify_only_for_single_match_ties() -> None:
    from app.services.football import has_qualify_market

    assert has_qualify_market("Лига Чемпионов УЕФА. Финал")
    assert has_qualify_market("Лига Чемпионов. 1/8 финала")      # одноматчевая пара
    assert not has_qualify_market("Лига Чемпионов. 1/8 финала. Первые матчи")
    assert not has_qualify_market("Лига Чемпионов. 1/4 финала. Ответные матчи")
    assert not has_qualify_market("Лига Чемпионов УЕФА. Общий этап")


# ------------------------------------------------------------------- модель
def test_model_matches_book_probabilities() -> None:
    p_home, p_draw, p_away = implied_probabilities(5.3, 4.9, 1.53)
    assert abs(p_home + p_draw + p_away - 1) < 1e-9

    lambda_home, lambda_away = solve_lambdas(p_home, p_draw, p_away)
    assert 0.2 < lambda_home < 3 and 0.2 < lambda_away < 3
    # фаворит должен иметь большую интенсивность голов
    assert lambda_away > lambda_home


def test_markets_are_consistent_and_wide() -> None:
    match = FootballMatch(1, "Лига Чемпионов УЕФА", "Лейпциг", "Манчестер Сити", 0, 5.3, 4.9, 1.53)
    markets = build_markets(match)
    keys = {market["key"] for market in markets}
    assert {"result", "dc", "total", "btts", "handicap", "team_total", "exact"} <= keys
    assert "qualify" not in keys          # общий этап: прохода дальше нет

    by_param = {(m["key"], m["param"]): m["odds"] for m in markets}
    # исход — ровно кэфы книги
    assert by_param[("result", "1")] == 5.3
    assert by_param[("result", "x")] == 4.9
    assert by_param[("result", "2")] == 1.53

    # комплементарные рынки: сумма вероятностей чуть выше единицы (наша маржа)
    pairs = [
        (("total", "over:2.5"), ("total", "under:2.5")),
        (("btts", "yes"), ("btts", "no")),
        (("handicap", "1:1.5"), ("handicap", "2:-1.5")),
        (("dc", "1x"), ("result", "2")),
    ]
    for first, second in pairs:
        total = 1 / by_param[first] + 1 / by_param[second]
        assert 1.0 < total < 1.12, (first, second, total)

    # все кэфы в разумных пределах и не «1.00»
    assert all(1.01 <= m["odds"] <= 500 for m in markets)
    # точный счёт покрывает всё пространство счетов
    exact = [m for m in markets if m["key"] == "exact"]
    assert len(exact) == 26            # 0:0…4:4 + «другой»


def test_home_advantage_shows_in_totals() -> None:
    strong_home = FootballMatch(1, "ЛЧ", "A", "B", 0, 1.4, 4.6, 8.0)
    weak_home = FootballMatch(2, "ЛЧ", "A", "B", 0, 8.0, 4.6, 1.4)
    first = {(m["key"], m["param"]): m["odds"] for m in build_markets(strong_home)}
    second = {(m["key"], m["param"]): m["odds"] for m in build_markets(weak_home)}
    assert first[("team_total", "1:over:1.5")] < second[("team_total", "1:over:1.5")]
    assert first[("exact", "2:0")] < second[("exact", "2:0")]


def test_knockout_adds_qualify_market() -> None:
    match = FootballMatch(1, "Лига Чемпионов УЕФА. 1/8 финала", "A", "B", 0, 2.0, 3.4, 3.6)
    keys = {m["key"] for m in build_markets(match)}
    assert "qualify" in keys


# ----------------------------------------------------------- расчёт ставок
@pytest.mark.parametrize(
    ("key", "param", "score", "expected"),
    [
        ("result", "1", (2, 0), "win"),
        ("result", "x", (1, 1), "win"),
        ("result", "2", (0, 1), "win"),
        ("dc", "1x", (1, 1), "win"),
        ("dc", "12", (1, 1), "lose"),
        ("total", "over:2.5", (2, 1), "win"),
        ("total", "under:2.5", (2, 1), "lose"),
        ("total", "over:2.5", (1, 1), "lose"),
        ("btts", "yes", (1, 2), "win"),
        ("btts", "no", (0, 0), "win"),
        ("handicap", "1:1.5", (1, 0), "win"),
        ("handicap", "2:-1.5", (1, 0), "lose"),
        ("handicap", "1:-0.5", (0, 0), "lose"),
        ("team_total", "2:over:0.5", (0, 1), "win"),
        ("team_total", "1:under:0.5", (0, 2), "win"),
        ("exact", "2:1", (2, 1), "win"),
        ("exact", "2:1", (2, 2), "lose"),
        ("exact", "other", (5, 4), "win"),
        ("exact", "other", (3, 0), "lose"),
        ("qualify", "1", (2, 0), "win"),
        ("qualify", "1", (0, 0), "void"),   # ничья: кто прошёл — по счёту не видно
    ],
)
def test_market_outcome(key: str, param: str, score: tuple[int, int], expected: str) -> None:
    assert market_outcome(key, param, *score) == expected


def test_side_codes_roundtrip() -> None:
    for key, param in [("total", "over:2.5"), ("exact", "1:0"), ("handicap", "2:-1.5")]:
        code = side_code(key, param)
        assert code.startswith("f:")
        assert parse_side(code) == (key, param)
    assert parse_side("a") is None


def test_market_labels_are_human() -> None:
    assert market_label("total", "over:2.5") == "ТБ 2.5"
    assert market_label("total", "under:1.5") == "ТМ 1.5"
    assert market_label("exact", "1:0") == "1:0"
    assert market_label("btts", "yes") == "Обе забьют — да"
    assert market_label("handicap", "1:-1.5") == "Ф1 (-1.5)"


def test_unknown_market_is_not_offered() -> None:
    match = FootballMatch(1, "ЛЧ", "A", "B", 0, None, None, None)
    assert build_markets(match) == []


# ------------------------------------------------------- ставки в базе
@pytest.fixture
async def db(tmp_path):
    database = Database(tmp_path / "football.db", start_balance=1000)
    await database.connect()
    await database.upsert_user(1, "sap", "Антон")
    yield database
    await database.close()


async def _add_match(db: Database, *, start_in: int = 3600) -> int:
    import time

    row, _ = await db.upsert_match(
        fonbet_id=777001,
        tournament="Лига Чемпионов УЕФА. Общий этап",
        team_a="Лейпциг",
        team_b="Манчестер Сити",
        start_time=int(time.time()) + start_in,
        odds_a=5.3,
        odds_x=4.9,
        odds_b=1.53,
        sport="football",
    )
    return int(row["id"])


async def test_football_markets_visible_and_bettable(db) -> None:
    match_id = _add_match_dupe = await _add_match(db)
    row = await db.get_match(match_id)
    markets = markets_of(row)
    assert any(m["key"] == "total" for m in markets)

    balance, odds = await db.place_football_bet(match_id, 1, None, "total", "over:2.5", 100)
    assert balance == 900
    total = next(m for m in markets if m["key"] == "total" and m["param"] == "over:2.5")
    assert odds == total["odds"]

    bets = await db.match_bets(match_id)
    assert len(bets) == 1 and bets[0]["side"] == "f:total:over:2.5"
    assert _add_match_dupe == match_id


async def test_football_settlement_pays_winners_and_refunds_void(db) -> None:
    match_id = await _add_match(db)

    await db.place_football_bet(match_id, 1, None, "total", "over:2.5", 100)   # 300 * odds
    await db.place_football_bet(match_id, 1, None, "btts", "no", 100)          # не зашла
    await db.place_bet(match_id, 1, None, "a", 100)                            # П1: хозяева
    await db.place_bet(match_id, 1, None, "x", 100)                            # ничья

    result = await db.settle_football(match_id, 3, 1)
    assert result["winner"] == "a"
    bet_rows = {int(b["id"]): b for b in await db.match_bets(match_id)}

    paid = 0
    for bet in bet_rows.values():
        side = str(bet["side"])
        if side == "f:total:over:2.5":
            assert int(bet["payout"]) == round(100 * float(bet["odds"]))
            paid += int(bet["payout"])
        elif side == "f:btts:no":
            assert int(bet["payout"]) == 0
        elif side == "a":
            assert int(bet["payout"]) == round(100 * float(bet["odds"]))
            paid += int(bet["payout"])
        elif side == "x":
            assert int(bet["payout"]) == 0

    assert await db.get_balance(1) == 1000 - 400 + paid
    assert result["paid"] == paid


async def test_void_market_returns_stake(db) -> None:
    import time

    row, _ = await db.upsert_match(
        fonbet_id=777002,
        tournament="Лига Чемпионов УЕФА. 1/8 финала",
        team_a="A",
        team_b="B",
        start_time=int(time.time()) + 3600,
        odds_a=2.0,
        odds_x=3.4,
        odds_b=3.6,
        sport="football",
    )
    match_id = int(row["id"])
    await db.place_football_bet(match_id, 1, None, "qualify", "1", 200)
    assert await db.get_balance(1) == 800

    result = await db.settle_football(match_id, 1, 1)   # ничья: проход не определён
    assert result["refunded"] == 200
    assert await db.get_balance(1) == 1000


async def test_bets_close_when_match_starts(db) -> None:
    from app.db import MatchNotOpen

    match_id = await _add_match(db, start_in=-10)
    with pytest.raises(MatchNotOpen):
        await db.place_football_bet(match_id, 1, None, "total", "over:2.5", 100)


async def test_unknown_market_rejected(db) -> None:
    from app.db import InvalidBet

    match_id = await _add_match(db)
    with pytest.raises(InvalidBet):
        await db.place_football_bet(match_id, 1, None, "total", "over:9.5", 100)
    with pytest.raises(InvalidBet):
        await db.place_football_bet(match_id, 1, None, "qualify", "1", 100)   # не плей-офф


# ----------------------------------------- чужие виды спорта и минута матча
HOCKEY_PACKET = {
    "sports": [
        {"id": 1, "kind": "sport", "name": "Футбол"},
        {"id": 2, "kind": "sport", "name": "Хоккей"},
        {"id": 113724, "parentId": 1, "kind": "segment", "name": "Лига Чемпионов УЕФА. Общий этап"},
        # хоккейная Лига Чемпионов: по названию похожа, по родителю — нет
        {"id": 29572, "parentId": 2, "kind": "segment",
         "name": "Лига Чемпионов. 1/8 финала. Первые матчи"},
    ],
    "events": [
        {"id": 10, "level": 1, "sportId": 113724, "team1": "Лейпциг", "team2": "Сити",
         "startTime": 1_800_000_000, "place": "line"},
        {"id": 11, "level": 1, "sportId": 29572, "team1": "КооКоо", "team2": "Айсберен Берлин",
         "startTime": 1_800_000_000, "place": "line"},
    ],
    "customFactors": [
        {"e": 10, "factors": [{"f": 921, "v": 5.3}, {"f": 922, "v": 4.9}, {"f": 923, "v": 1.53}]},
        {"e": 11, "factors": [{"f": 921, "v": 2.2}, {"f": 922, "v": 4.1}, {"f": 923, "v": 2.9}]},
    ],
    "eventMiscs": [{"id": 11, "score1": 1, "score2": 1}],
    "liveEventInfos": [],
}


def test_hockey_champions_league_is_not_football() -> None:
    matches = parse_football(HOCKEY_PACKET, ["лига чемпионов"], [])
    assert [m.team_a for m in matches] == ["Лейпциг"]

    from app.services.football import non_football_event_ids

    assert non_football_event_ids(HOCKEY_PACKET) == {11}


def test_live_minute_from_timer() -> None:
    packet = {
        "sports": [
            {"id": 1, "kind": "sport", "name": "Футбол"},
            {"id": 113724, "parentId": 1, "kind": "segment", "name": "Лига Чемпионов УЕФА. Общий этап"},
        ],
        "events": [{"id": 20, "level": 1, "sportId": 113724, "team1": "A", "team2": "B",
                    "startTime": 1_800_000_000, "place": "live"}],
        "customFactors": [{"e": 20, "factors": [{"f": 921, "v": 2.1}, {"f": 922, "v": 3.4},
                                                {"f": 923, "v": 3.3}]}],
        "eventMiscs": [{"id": 20, "score1": 1, "score2": 0, "timerSeconds": 4020,
                        "timerDirection": 1,
                        "timerUpdateTimestampMsec": int(time.time() * 1000)}],
        "liveEventInfos": [{"eventId": 20, "finished": False}],
    }
    match = parse_football(packet, ["лига чемпионов"], [])[0]
    assert match.minute == 67          # 4020 с = 67 минут, снимок только что


def test_weird_timer_is_ignored() -> None:
    def match_with(misc):
        packet = {
            "sports": [
                {"id": 1, "kind": "sport", "name": "Футбол"},
                {"id": 113724, "parentId": 1, "kind": "segment", "name": "Лига Чемпионов УЕФА. Общий этап"},
            ],
            "events": [{"id": 21, "level": 1, "sportId": 113724, "team1": "A", "team2": "B",
                        "startTime": 1_800_000_000, "place": "live"}],
            "customFactors": [{"e": 21, "factors": [{"f": 921, "v": 2.1}, {"f": 922, "v": 3.4},
                                                    {"f": 923, "v": 3.3}]}],
            "eventMiscs": [dict({"id": 21}, **misc)],
            "liveEventInfos": [],
        }
        return parse_football(packet, ["лига чемпионов"], [])[0]

    assert match_with({"timerSeconds": 600, "timerDirection": 0}).minute is None   # таймер замер
    assert match_with({"timerSeconds": 999999, "timerDirection": 1}).minute is None  # мусор
    assert match_with({}).minute is None                                           # нет таймера


async def test_foreign_match_removed_from_football_line(db) -> None:
    """Хоккейный матч, попавший в футбольную линию, снимается и возвращает ставки."""
    import time

    row, _ = await db.upsert_match(
        fonbet_id=11,
        tournament="Лига Чемпионов. 1/8 финала. Первые матчи",
        team_a="КооКоо",
        team_b="Айсберен Берлин",
        start_time=int(time.time()) + 7200,
        odds_a=2.2,
        odds_x=4.1,
        odds_b=2.9,
        sport="football",
    )
    match_id = int(row["id"])
    await db.place_football_bet(match_id, 1, None, "total", "over:2.5", 100)
    assert await db.get_balance(1) == 900

    result = await db.cancel_match(match_id)
    assert result["refunded"] == 100
    assert await db.get_balance(1) == 1000
    assert str((await db.get_match(match_id))["status"]) == "cancelled"
