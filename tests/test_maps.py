"""Ставки на победителя конкретной карты: парсинг линии, приём и расчёт."""

from __future__ import annotations

import time

import pytest

from app.db import Database, MatchNotOpen
from app.services.cs2_rules import map_winner, required_maps, series_winner
from app.services.fonbet import FonbetClient, parse_maps


@pytest.fixture
async def db(tmp_path):
    database = Database(tmp_path / "maps.db", start_balance=1000)
    await database.connect()
    for user_id in (1, 2):
        await database.upsert_user(user_id, f"user{user_id}", f"User {user_id}")
    yield database
    await database.close()


def _packet() -> dict:
    """Кусок линии Fonbet: матч level=1 и две карты level=2."""
    return {
        "sports": [{"id": 1, "name": "Counter-Strike. IEM Пекин. Групповой этап. Bo3"}],
        "events": [
            {
                "id": 100,
                "sportId": 1,
                "level": 1,
                "team1": "Aurora",
                "team2": "G2 Esports",
                "startTime": int(time.time()) + 3600,
            },
            {"id": 101, "sportId": 1, "level": 2, "parentId": 100, "name": "1-я карта"},
            {"id": 102, "sportId": 1, "level": 2, "parentId": 100, "name": "2-я карта"},
            {"id": 103, "sportId": 1, "level": 2, "parentId": 100, "name": "Показатели игроков. 1-я карта"},
        ],
        "customFactors": [
            {"e": 100, "factors": [{"f": 921, "v": 1.65}, {"f": 923, "v": 2.25}]},
            {"e": 101, "factors": [{"f": 921, "v": 1.70}, {"f": 923, "v": 2.10}]},
            {"e": 102, "factors": [{"f": 921, "v": 1.90}, {"f": 923, "v": 1.90}]},
            {"e": 103, "factors": []},
        ],
    }


def _client() -> FonbetClient:
    return FonbetClient(
        "http://example.invalid",
        include=["IEM"],
        exclude=["Qualifier"],
    )


async def _match(db: Database) -> dict:
    row, _ = await db.upsert_match(
        fonbet_id=100,
        tournament="Counter-Strike. IEM Пекин. Групповой этап. Bo3",
        team_a="Aurora",
        team_b="G2 Esports",
        start_time=int(time.time()) + 3600,
        odds_a=1.65,
        odds_b=2.25,
    )
    return row


async def _map(db: Database, match_id: int, *, number: int = 1, odds_a=1.70, odds_b=2.10) -> dict:
    row, _ = await db.upsert_map(
        match_id=match_id,
        fonbet_id=100000 + number,
        map_number=number,
        odds_a=odds_a,
        odds_b=odds_b,
    )
    return row


# ------------------------------------------------------------------ парсинг
def test_parse_maps_takes_only_real_maps() -> None:
    packet = _packet()
    maps = parse_maps(packet, _client())

    assert list(maps) == [100]
    assert [card.map_number for card in maps[100]] == [1, 2]
    assert maps[100][0].odds_a == 1.70
    assert maps[100][1].odds_b == 1.90


def test_parse_maps_ignores_unknown_parent() -> None:
    packet = _packet()
    packet["events"] = [event for event in packet["events"] if event["id"] != 100]
    assert parse_maps(packet, _client()) == {}


# ------------------------------------------------------------- расчёт карты
def test_map_winner_by_rounds() -> None:
    assert map_winner(13, 9) == "a"       # карта до 13 раундов
    assert map_winner(7, 13) == "b"
    assert map_winner(12, 11) is None     # карта ещё идёт
    assert map_winner(13, 12) is None     # разница 1 — так не бывает, ждём
    assert map_winner(None, 13) is None


def test_map_winner_with_overtime() -> None:
    """12:12 — овертайм: шесть раундов, для победы нужно взять четыре."""
    assert map_winner(12, 12) is None     # начался овертайм
    assert map_winner(15, 12) is None     # три раунда блока — ещё играют
    assert map_winner(14, 12) is None     # такого финала не бывает
    assert map_winner(15, 14) is None     # разница 1 — не победа
    assert map_winner(15, 15) is None     # ровно — следующий блок из шести
    assert map_winner(16, 12) == "a"      # 4:0 в овертайме
    assert map_winner(16, 14) == "a"      # 4:2 в овертайме
    assert map_winner(14, 16) == "b"
    assert map_winner(19, 17) == "a"      # второй блок овертайма
    assert map_winner(19, 14) is None     # отрыв 5 — такого счёта не бывает


def test_series_needs_two_maps_in_bo3() -> None:
    """Bo1 — одна карта, Bo3 — две, Bo5 — три."""
    assert required_maps("Counter-Strike. IEM. Bo1") == 1
    assert required_maps("Counter-Strike. IEM. Bo3") == 2
    assert required_maps("Counter-Strike. IEM. Bo5") == 3
    assert series_winner(2, 0, "Bo3") == "a"
    assert series_winner(2, 1, "Bo3") == "a"
    assert series_winner(1, 1, "Bo3") is None
    assert series_winner(1, 0, "Bo1") == "a"
    assert series_winner(2, 1, "Bo5") is None


# --------------------------------------------------------------- приём ставок
async def test_map_bet_and_settlement(db: Database) -> None:
    match = await _match(db)
    card = await _map(db, int(match["id"]))
    away = await _map(db, int(match["id"]), number=2, odds_a=1.90, odds_b=1.90)

    balance, odds = await db.place_map_bet(int(card["id"]), 1, None, "a", 100)
    assert balance == 900 and odds == 1.70
    balance, _ = await db.place_map_bet(int(away["id"]), 2, None, "a", 100)
    assert balance == 900

    result = await db.settle_map(int(card["id"]), "a", source="fonbet")
    assert result["map_number"] == 1
    assert result["paid"] == 170
    assert result["winners"] == 1

    # ставка на вторую карту не тронута
    rows = await db.match_bets(int(match["id"]))
    unplayed = next(row for row in rows if row["map_id"] == int(away["id"]))
    assert unplayed["payout"] is None

    assert await db.get_balance(1) == 1070
    assert await db.get_balance(2) == 900

    card_row = await db.get_map(int(card["id"]))
    assert card_row["status"] == "finished" and card_row["winner"] == "a"


async def test_match_settlement_does_not_touch_map_bets(db: Database) -> None:
    match = await _match(db)
    await db.place_bet(int(match["id"]), 1, None, "b", 100)             # ставка на матч
    card = await _map(db, int(match["id"]))
    await db.place_map_bet(int(card["id"]), 2, None, "a", 200)          # ставка на карту

    result = await db.settle_match(int(match["id"]), "a")
    assert result["bets"] == 1                                          # только матчевые
    assert len(result["settlements"]) == 1

    rows = await db.match_bets(int(match["id"]))
    map_bet = next(row for row in rows if row["market"] == "map")
    assert map_bet["payout"] is None                                    # карта ещё не сыграна


async def test_map_bets_closed_after_map_finished(db: Database) -> None:
    match = await _match(db)
    card = await _map(db, int(match["id"]))
    await db.settle_map(int(card["id"]), "a")

    with pytest.raises(MatchNotOpen):
        await db.place_map_bet(int(card["id"]), 1, None, "a", 100)


async def test_maps_for_matches_groups_by_match(db: Database) -> None:
    match = await _match(db)
    await _map(db, int(match["id"]), number=1)
    await _map(db, int(match["id"]), number=2)

    grouped = await db.maps_for_matches([int(match["id"])])
    assert [row["map_number"] for row in grouped[int(match["id"])]] == [1, 2]
    assert await db.maps_for_matches([]) == {}
