"""Хоккей (задача 51): линия КХЛ/НХЛ, два вида исхода и ручной расчёт КХЛ.

Ключевое отличие от футбола: у матча два счёта. Основное время (три периода)
даёт ничью и считается по `reg_a:reg_b`, а «победа в матче» — уже с овертаймом
и буллитами, поэтому считается по итоговому счёту.
"""

from __future__ import annotations

import time

import pytest

from app.db import Database, InvalidBet
from app.services import hockey
from app.services.hockey import (
    build_markets,
    market_label,
    market_outcome,
    parse_side,
    side_code,
)


@pytest.fixture
async def db(tmp_path):
    database = Database(tmp_path / "hockey.db", start_balance=1000)
    await database.connect()
    for user_id in (1, 2):
        await database.upsert_user(user_id, f"user{user_id}", f"User {user_id}")
    yield database
    await database.close()


def _line_packet() -> dict:
    """Минимальная линия Fonbet: КХЛ, НХЛ, женский ЧМ и отбор — всё в одном пакете."""
    factors = [
        {"f": 921, "v": 2.95},   # П1 основного времени
        {"f": 922, "v": 4.20},   # X основного времени
        {"f": 923, "v": 2.10},   # П2 основного времени
    ]
    events = [
        {"id": 901, "level": 1, "sportId": 13283, "team1": "Авангард", "team2": "ЦСКА",
         "startTime": 4102444800},
        {"id": 902, "level": 1, "sportId": 11781, "team1": "Монреаль Канадиенс",
         "team2": "Виннипег Джетс", "startTime": 4102448400},
        {"id": 903, "level": 1, "sportId": 45900, "team1": "Россия", "team2": "Китай",
         "startTime": 4102448400},
        {"id": 904, "level": 1, "sportId": 45800, "team1": "Канада", "team2": "США",
         "startTime": 4102448400},
    ]
    return {
        "sports": [
            {"id": 2, "kind": "sport", "name": "Хоккей"},
            {"id": 13283, "kind": "segment", "name": "Фонбет КХЛ. Регулярный сезон",
             "parentId": 2},
            {"id": 11781, "kind": "segment", "name": "НХЛ. Регулярный сезон", "parentId": 2},
            {"id": 45900, "kind": "segment", "name": "Чемпионат мира. Женщины", "parentId": 2},
            {"id": 45800, "kind": "segment",
             "name": "Чемпионат мира. Квалификация", "parentId": 2},
        ],
        "events": events,
        "customFactors": [{"e": event["id"], "factors": factors} for event in events],
        "eventMiscauses": [],
        "eventMiscs": [],
        "liveEventInfos": [],
    }


# ------------------------------------------------------------------ фильтр линии
def test_default_line_covers_khl_and_nhl_only() -> None:
    """КХЛ и НХЛ в линии; женский ЧМ и квалификация — нет."""
    from app.config import settings
    from app.services.hockey import parse_hockey

    matches = parse_hockey(
        _line_packet(), settings.hockey_include_list, settings.hockey_exclude_list
    )
    assert [m.team_a for m in matches] == ["Авангард", "Монреаль Канадиенс"]
    assert {m.tournament for m in matches} == {
        "Фонбет КХЛ. Регулярный сезон", "НХЛ. Регулярный сезон"
    }


def test_hockey_segments_require_hockey_sport() -> None:
    """Сегмент «Чемпионат мира» есть и в других видах спорта — берём только свои."""
    from app.config import settings
    from app.services.hockey import parse_hockey

    packet = _line_packet()
    packet["sports"].append(
        {"id": 45800, "kind": "segment", "name": "Чемпионат мира. Футбол", "parentId": 1}
    )
    matches = parse_hockey(
        packet, settings.hockey_include_list, settings.hockey_exclude_list
    )
    assert all(m.team_a not in ("Канада", "США") for m in matches)


# ------------------------------------------------------------------ кэфы и рынки
def test_regulation_odds_come_from_bookmaker() -> None:
    """Кэфы 1X2 основного времени не пересчитываем — берём как у книги."""
    from app.config import settings
    from app.services.hockey import parse_hockey

    match = parse_hockey(
        _line_packet(), settings.hockey_include_list, settings.hockey_exclude_list
    )[0]
    assert (match.odds_1, match.odds_x, match.odds_2) == (2.95, 4.2, 2.1)


def test_markets_have_regulation_final_and_totals() -> None:
    from app.config import settings
    from app.services.hockey import parse_hockey

    match = parse_hockey(
        _line_packet(), settings.hockey_include_list, settings.hockey_exclude_list
    )[0]
    keys = [(m["key"], m["param"]) for m in build_markets(match)]
    assert ("reg", "1") in keys and ("reg", "x") in keys and ("reg", "2") in keys
    assert ("final", "1") in keys and ("final", "2") in keys
    assert ("total", "over:5.5") in keys and ("total", "under:5.5") in keys
    # «обе забьют», фор и точного счёта в хоккее нет — по договорённости роспись базовая
    assert {key for key, _ in keys} <= {"reg", "final", "total"}


def test_final_odd_is_above_regulation_even_for_favourite() -> None:
    """Победа в матче вероятнее, чем победа в основное время: кэф ниже."""
    from app.config import settings
    from app.services.hockey import parse_hockey

    match = parse_hockey(
        _line_packet(), settings.hockey_include_list, settings.hockey_exclude_list
    )[0]
    odds = {(m["key"], m["param"]): m["odds"] for m in build_markets(match)}
    assert odds[("final", "1")] < odds[("reg", "1")]
    assert odds[("final", "2")] < odds[("reg", "2")]


# ------------------------------------------------------------------ расчёт исхода
@pytest.mark.parametrize(
    ("key", "param", "reg", "final", "expected"),
    [
        # матч основного времени: 2:2 и 3:2 в овертайме
        ("reg", "1", (2, 2), (3, 2), "lose"),
        ("reg", "x", (2, 2), (3, 2), "win"),
        ("reg", "2", (2, 2), (3, 2), "lose"),
        ("final", "1", (2, 2), (3, 2), "win"),
        ("final", "2", (2, 2), (3, 2), "lose"),
        # победа в основное время
        ("reg", "2", (1, 3), (1, 3), "win"),
        ("final", "2", (1, 3), (1, 3), "win"),
        # буллиты: 1:1 в основное, 2:1 по буллитам
        ("reg", "x", (1, 1), (2, 1), "win"),
        ("final", "1", (1, 1), (2, 1), "win"),
    ],
)
def test_market_outcome(key, param, reg, final, expected) -> None:
    assert market_outcome(key, param, reg[0], reg[1], final[0], final[1]) == expected


def test_final_market_is_void_without_overtime_score() -> None:
    """Итоговый счёт совпал с равным счётом основного времени — считать нечем."""
    assert market_outcome("final", "1", 2, 2, 2, 2) == "void"


def test_totals_count_regulation_goals_only() -> None:
    """Тотал в хоккее — по трём периодам: гол в овертайме его не двигает."""
    # 4:2 в основное (6 голов) и 5:2 с овертаймом
    assert market_outcome("total", "over:5.5", 4, 2, 5, 2) == "win"
    assert market_outcome("total", "under:5.5", 4, 2, 5, 2) == "lose"
    assert market_outcome("total", "under:5.5", 2, 2, 3, 2) == "win"


def test_side_codes_round_trip() -> None:
    assert side_code("reg", "1") == "h:reg:1"
    assert side_code("final", "2") == "h:final:2"
    assert side_code("total", "over:5.5") == "h:total:over:5.5"
    assert parse_side("h:total:over:5.5") == ("total", "over:5.5")
    assert parse_side("f:total:over:5.5") is None
    assert parse_side("a") is None
    assert market_label("final", "1") == "П1 (с ОТ)"


# ------------------------------------------------------------------ ставки и деньги
async def _hockey_match(db: Database) -> dict:
    row, _ = await db.upsert_match(
        fonbet_id=901,
        tournament="Фонбет КХЛ. Регулярный сезон",
        team_a="Авангард",
        team_b="ЦСКА",
        start_time=int(time.time()) + 3600,
        odds_a=2.95,
        odds_x=4.2,
        odds_b=2.1,
        sport="hockey",
    )
    return row


async def test_bet_on_regulation_and_money_flow(db: Database) -> None:
    match = await _hockey_match(db)
    balance, odds = await db.place_hockey_bet(int(match["id"]), 1, None, "reg", "x", 100)
    assert odds == 4.2
    assert balance == 900

    result = await db.settle_hockey(int(match["id"]), 2, 2, 3, 2, source="admin")
    assert result["paid"] == 420
    assert result["reg_score"] == "2:2" and result["score"] == "3:2"
    assert await db.get_balance(1) == 1320


async def test_final_bet_wins_when_team_wins_in_overtime(db: Database) -> None:
    """Ставка «победа в матче» на команду, которая выиграла только в овертайме."""
    match = await _hockey_match(db)
    await db.place_hockey_bet(int(match["id"]), 1, None, "final", "1", 100)
    await db.place_hockey_bet(int(match["id"]), 2, None, "reg", "x", 100)

    result = await db.settle_hockey(int(match["id"]), 2, 2, 3, 2, source="admin")
    labels = {item["side"]: item for item in result["settlements"]}
    assert labels["h:final:1"]["won"] is True
    assert labels["h:reg:x"]["won"] is True
    assert result["paid"] == round(100 * labels["h:final:1"]["odds"]) + round(
        100 * labels["h:reg:x"]["odds"]
    )


async def test_unknown_market_is_rejected(db: Database) -> None:
    match = await _hockey_match(db)
    with pytest.raises(InvalidBet):
        await db.place_hockey_bet(int(match["id"]), 1, None, "handicap", "1", 100)


async def test_hockey_bet_is_not_placed_on_cs2_match(db: Database) -> None:
    row, _ = await db.upsert_match(
        fonbet_id=1,
        tournament="Counter-Strike. IEM. Групповой этап. Bo3",
        team_a="Aurora",
        team_b="G2 Esports",
        start_time=int(time.time()) + 3600,
        odds_a=1.7,
        odds_b=2.2,
    )
    with pytest.raises(InvalidBet):
        await db.place_hockey_bet(int(row["id"]), 1, None, "reg", "1", 100)


async def test_draw_in_final_score_is_rejected(db: Database) -> None:
    """Хоккей не заканчивается вничью: без счёта с овертаймом считать нельзя."""
    match = await _hockey_match(db)
    with pytest.raises(InvalidBet):
        await db.settle_hockey(int(match["id"]), 2, 2, 2, 2, source="admin")


async def test_active_bets_include_hockey(db: Database) -> None:
    match = await _hockey_match(db)
    await db.place_hockey_bet(int(match["id"]), 1, None, "reg", "1", 100)
    assert any(int(row["match_id"]) == int(match["id"]) for row in await db.user_active_bets(1))


async def test_cancel_returns_hockey_stakes(db: Database) -> None:
    match = await _hockey_match(db)
    await db.place_hockey_bet(int(match["id"]), 1, None, "reg", "1", 100)
    await db.cancel_football_match(int(match["id"]))
    assert await db.get_balance(1) == 1000


# ------------------------------------------------------------------ фид результатов
def test_hockey_results_feed_parses_regulation_and_final() -> None:
    """Фид отдаёт Tr1OR/Tr2OR (основное время) и Tr1/Tr2 (итог)."""
    from app.services.results import ResultsClient, parse_results

    packet = {
        "Stages": [
            {
                "Events": [
                    {
                        "T1": [{"Nm": "Montreal Canadiens"}],
                        "T2": [{"Nm": "Winnipeg Jets"}],
                        "Tr1": 5, "Tr2": 4, "Tr1OR": 4, "Tr2OR": 4,
                        "Eps": "OT", "Esd": 20261104,
                    }
                ]
            }
        ]
    }
    assert ResultsClient(sport="hockey")._sport == "hockey"
    results = parse_results(packet)
    assert len(results) == 1
    result = results[0]
    assert result.status == "OT" and result.finished is True
    assert (result.score_home, result.score_away) == (4, 4)
    assert (result.final_home, result.final_away) == (5, 4)


def test_hockey_and_soccer_use_different_feeds() -> None:
    from app.services.results import HOCKEY_URL, SPORT_URLS

    assert "/hockey/" in HOCKEY_URL and SPORT_URLS["hockey"] == HOCKEY_URL
    assert "/soccer/" in SPORT_URLS["soccer"]


# ------------------------------------------------------------------ мини-апп
def test_payload_markets_are_picked_by_sport() -> None:
    from app.web.payloads import markets_for, side_label

    row = {
        "id": 5,
        "sport": "hockey",
        "team_a": "Авангард",
        "team_b": "ЦСКА",
        "tournament": "Фонбет КХЛ. Регулярный сезон",
        "start_time": 4102444800,
        "odds_a": 2.95,
        "odds_x": 4.2,
        "odds_b": 2.1,
        "status": "open",
    }
    markets = markets_for(row)
    assert {market["key"] for market in markets} == {"reg", "final", "total"}
    assert side_label("h:final:1", "Авангард", "ЦСКА") == "П1 (с ОТ)"
    assert side_label("h:total:over:5.5", "Авангард", "ЦСКА") == "ТБ 5.5 (основное время)"


def test_hockey_match_payload_has_no_draw_for_final() -> None:
    from app.web.payloads import match_payload

    payload = match_payload(
        {
            "id": 5,
            "sport": "hockey",
            "team_a": "Авангард",
            "team_b": "ЦСКА",
            "tournament": "Фонбет КХЛ. Регулярный сезон",
            "start_time": 4102444800,
            "odds_a": 2.95,
            "odds_x": 4.2,
            "odds_b": 2.1,
            "status": "open",
        }
    )
    assert payload["sport"] == "hockey"
    assert payload["odds_x"] == 4.2
    final = [m for m in payload["markets"] if m["key"] == "final"]
    assert {m["label"] for m in final} == {"Победа: Авангард", "Победа: ЦСКА"}


def test_hockey_tournament_label_keeps_league() -> None:
    """У хоккея лига стоит в начале названия — её нельзя терять, как «Counter-Strike»."""
    from app.web.payloads import short_tournament

    assert short_tournament("Фонбет КХЛ. Регулярный сезон") == "КХЛ · Регулярный сезон"
    assert short_tournament("НХЛ. Регулярный сезон") == "НХЛ · Регулярный сезон"
    assert short_tournament("Чемпионат мира. Групповой этап") == "ЧМ · Групповой этап"


def test_hockey_settled_text_shows_both_scores() -> None:
    from app import texts

    result = {
        "team_a": "Авангард", "team_b": "ЦСКА", "team": "Авангард",
        "paid": 300, "bets": 2, "winners": 1, "refunded": 0,
    }
    text = texts.hockey_settled_text(result, 7, "2:2", "3:2")
    assert "основное время" in text and "2:2" in text and "3:2" in text
    assert "🏒" in text


def test_hockey_market_labels_are_russian() -> None:
    assert hockey.market_label("reg", "x") == "X (основное время)"
    assert hockey.market_label("total", "under:4.5") == "ТМ 4.5 (основное время)"


# ------------------------------------------------------- авто/ручной расчёт синка
class _FakeHockeyFeed:
    """Заглушка хоккейного фида: НХЛ отдаёт счёт, КХЛ — нет."""

    def __init__(self, result=None) -> None:
        self.result = result
        self.calls = 0

    async def find(self, *args, **kwargs):
        self.calls += 1
        return self.result

    async def day(self, *args, **kwargs):
        return []

    async def close(self) -> None:
        return None


@pytest.fixture(autouse=True)
def _quiet_notifications(monkeypatch):
    async def _noop(*args, **kwargs):
        return None

    monkeypatch.setattr("app.services.sync.notify_bettors", _noop)


def _syncer(db: Database, feed: _FakeHockeyFeed) -> object:
    from app.services.sync import MatchSyncer

    syncer = MatchSyncer(bot=None, db=db, results=_FakeHockeyFeed())
    syncer.hockey_results = feed
    return syncer


async def _started_hockey(db: Database, *, minutes_ago: int = 150) -> dict:
    """Матч, который уже прошёл: ставим до старта, потом время уходит вперёд."""
    fields = {
        "fonbet_id": 4242,
        "tournament": "НХЛ. Регулярный сезон",
        "team_a": "Монреаль Канадиенс",
        "team_b": "Виннипег Джетс",
        "odds_a": 2.5,
        "odds_x": 4.3,
        "odds_b": 2.3,
        "sport": "hockey",
    }
    row, _ = await db.upsert_match(start_time=int(time.time()) + 3600, **fields)
    await db.place_hockey_bet(int(row["id"]), 1, None, "reg", "1", 100)
    await db.place_hockey_bet(int(row["id"]), 1, None, "final", "1", 100)
    # время ушло вперёд: матч уже давно закончился
    row, _ = await db.upsert_match(
        start_time=int(time.time()) - minutes_ago * 60, **fields
    )
    await db.set_status(int(row["id"]), "live")
    return row


async def test_nhl_is_settled_automatically_with_ot_score(db: Database) -> None:
    """НХЛ: 2:2 в основное и 2:3 в овертайме — «основное» проиграло, «матч» тоже."""
    from app.services.results import FootballResult

    row = await _started_hockey(db)
    feed = _FakeHockeyFeed(
        FootballResult(
            home="Montreal Canadiens", away="Winnipeg Jets",
            score_home=2, score_away=2, final_home=2, final_away=3,
            status="OT", start_ts=int(time.time()) - 150 * 60,
        )
    )
    stats = {"settled": 0, "awaiting": 0}
    await _syncer(db, feed)._settle_hockey([], stats)

    assert stats["settled"] == 1
    match = await db.get_match(int(row["id"]))
    assert str(match["status"]) == "finished"
    assert (int(match["reg_a"]), int(match["reg_b"])) == (2, 2)
    assert (int(match["score_a"]), int(match["score_b"])) == (2, 3)
    assert await db.get_balance(1) == 800        # обе ставки проиграли — верно


async def test_nhl_settles_winning_final_bet(db: Database) -> None:
    """Ставка «победа в матче» заходит, даже если основное время — ничья."""
    from app.services.results import FootballResult

    row = await _started_hockey(db)
    feed = _FakeHockeyFeed(
        FootballResult(
            home="Montreal Canadiens", away="Winnipeg Jets",
            score_home=1, score_away=1, final_home=2, final_away=1,
            status="OT", start_ts=int(time.time()) - 150 * 60,
        )
    )
    stats = {"settled": 0, "awaiting": 0}
    await _syncer(db, feed)._settle_hockey([], stats)

    bets = await db.match_bets(int(row["id"]))
    final_bet = next(b for b in bets if str(b["side"]) == "h:final:1")
    expected = 800 + int(100 * float(final_bet["odds"]))   # 800 — после двух ставок
    assert await db.get_balance(1) == expected


async def test_khl_without_feed_waits_for_admin(db: Database) -> None:
    """КХЛ в фиде нет: матч ждёт админа, деньги не двигаем."""
    fields = {
        "fonbet_id": 777,
        "tournament": "Фонбет КХЛ. Регулярный сезон",
        "team_a": "Авангард",
        "team_b": "ЦСКА",
        "odds_a": 2.95,
        "odds_x": 4.2,
        "odds_b": 2.1,
        "sport": "hockey",
    }
    row, _ = await db.upsert_match(start_time=int(time.time()) + 3600, **fields)
    await db.place_hockey_bet(int(row["id"]), 1, None, "reg", "1", 100)
    row, _ = await db.upsert_match(start_time=int(time.time()) - 180 * 60, **fields)
    await db.set_status(int(row["id"]), "live")

    stats = {"settled": 0, "awaiting": 0}
    await _syncer(db, _FakeHockeyFeed(None))._settle_hockey([], stats)

    assert stats["settled"] == 0 and stats["awaiting"] == 1
    match = await db.get_match(int(row["id"]))
    assert str(match["status"]) == "awaiting"
    assert await db.get_balance(1) == 900          # ставка в игре, деньги не двигаем


async def test_finished_hockey_match_is_not_settled_twice(db: Database) -> None:
    from app.services.results import FootballResult

    row = await _started_hockey(db)
    feed = _FakeHockeyFeed(
        FootballResult(
            home="Montreal Canadiens", away="Winnipeg Jets",
            score_home=3, score_away=1, final_home=3, final_away=1,
            status="FT", start_ts=int(time.time()) - 150 * 60,
        )
    )
    stats = {"settled": 0, "awaiting": 0}
    await _syncer(db, feed)._settle_hockey([], stats)
    balance = await db.get_balance(1)

    stats2 = {"settled": 0, "awaiting": 0}
    await _syncer(db, feed)._settle_hockey([], stats2)
    assert stats2["settled"] == 0
    assert await db.get_balance(1) == balance
    del row


# ------------------------------------------------------------------ админская команда
@pytest.mark.parametrize(
    ("token", "expected"),
    [("3:2", (3, 2)), ("3-2", (3, 2)), ("0:0", (0, 0)), ("3х2", (3, 2)), ("32", None),
     ("a:b", None)],
)
def test_admin_score_parsing(token: str, expected) -> None:
    from app.handlers.admin import _parse_score

    assert _parse_score(token) == expected


def test_find_matches_hockey_by_day_when_feed_has_no_time() -> None:
    """Хоккейный фид отдаёт счёт с датой вместо времени — матч всё равно находится."""
    import asyncio
    from datetime import datetime

    from app.services.results import FootballResult, ResultsClient

    evening = int(datetime(2026, 11, 4, 19, 30).timestamp())
    midnight = int(datetime(2026, 11, 4).timestamp())
    result = FootballResult(
        home="Montreal Canadiens", away="Winnipeg Jets",
        score_home=4, score_away=4, final_home=4, final_away=5,
        status="OT", start_ts=midnight,
    )

    class Feed(ResultsClient):
        async def day(self, day, *, ttl: int = 600):
            return [result]

    client = Feed(sport="hockey")
    found = asyncio.run(client.find("Монреаль", "Виннипег", evening,
                                    english_a="Montreal Canadiens",
                                    english_b="Winnipeg Jets"))
    assert found is result


async def test_express_leg_wins_when_team_wins_in_overtime(db: Database) -> None:
    """Нога экспресса — это «победа команды»: овертайм её закрывает, а не отменяет."""
    match = await _hockey_match(db)
    other, _ = await db.upsert_match(
        fonbet_id=902,
        tournament="НХЛ. Регулярный сезон",
        team_a="Монреаль",
        team_b="Бостон",
        start_time=int(time.time()) + 3600,
        odds_a=1.8,
        odds_b=2.0,
        sport="hockey",
    )
    await db.place_express(
        1, None, 100,
        [{"match_id": int(match["id"]), "side": "a"},
         {"match_id": int(other["id"]), "side": "a"}],
    )
    # хозяева выиграли только в овертайме: 2:2 в основное, 3:2 по итогу
    await db.settle_hockey(int(match["id"]), 2, 2, 3, 2, source="admin")

    express = (await db.user_open_expresses(1))[0]
    legs = await db.express_items(int(express["id"]))
    first = next(leg for leg in legs if int(leg["match_id"]) == int(match["id"]))
    assert str(first["result"]) == "won"        # овертайм не превращает ногу в проигрыш
    assert str(express["status"]) == "open"     # вторая нога ещё не сыграна
