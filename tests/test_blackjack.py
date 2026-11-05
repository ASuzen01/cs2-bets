"""Классические правила блэкджека, скрытая карта и атомарные расчёты."""

from __future__ import annotations

import asyncio

import pytest

from app.db import Database, InvalidBet
from app.services import blackjack


def cards_in_draw_order(*ranks: str) -> list[dict[str, str]]:
    """`new_game` достаёт последнюю карту shoe первой; тестовая shoe без дублей."""
    suits = ("♠", "♥", "♦", "♣")
    used: dict[str, set[str]] = {}
    draw_order = []
    for rank in ranks:
        used_suits = used.setdefault(rank, set())
        suit = next(suit for suit in suits if suit not in used_suits)
        used_suits.add(suit)
        draw_order.append({"rank": rank, "suit": suit})
    return list(reversed(draw_order))


def test_shoe_has_one_copy_of_every_physical_card() -> None:
    shoe = blackjack.make_shoe()
    faces = {(card["rank"], card["suit"]) for card in shoe}
    assert len(shoe) == 52
    assert len(faces) == 52


def test_draw_skips_duplicate_face_in_legacy_active_shoe() -> None:
    queen = {"rank": "Q", "suit": "♦"}
    other = {"rank": "K", "suit": "♣"}
    state = {
        "shoe": [other, queen.copy()],
        "dealer": [queen.copy()],
        "hands": [{"cards": [queen.copy()]}],
    }

    assert blackjack._draw(state) == other
    assert state["shoe"] == []


def test_hand_value_handles_aces_and_soft_totals() -> None:
    assert blackjack.hand_value([{"rank": "A", "suit": "♠"}, {"rank": "6", "suit": "♥"}]) == (17, True)
    assert blackjack.hand_value([
        {"rank": "A", "suit": "♠"}, {"rank": "A", "suit": "♥"},
        {"rank": "A", "suit": "♦"}, {"rank": "6", "suit": "♣"},
    ]) == (19, True)
    assert blackjack.hand_value([{"rank": "A", "suit": "♠"}, {"rank": "9", "suit": "♥"}, {"rank": "8", "suit": "♦"}]) == (18, False)


def test_natural_blackjack_pays_three_to_two(monkeypatch) -> None:
    monkeypatch.setattr(blackjack, "make_shoe", lambda: cards_in_draw_order("A", "9", "K", "7"))
    game = blackjack.new_game(10)
    assert game["status"] == "settled"
    assert game["payout"] == 25
    assert game["result"]["net"] == 15
    assert game["result"]["hands"][0]["label"] == "Блэкджек"


def test_dealer_stands_on_soft_seventeen(monkeypatch) -> None:
    monkeypatch.setattr(blackjack, "make_shoe", lambda: cards_in_draw_order("10", "6", "7", "A", "9"))
    game = blackjack.new_game(10)
    assert game["status"] == "playing"
    public = blackjack.public_state(1, game, 990)
    assert public["dealer"]["cards"][1] == {"hidden": True}

    blackjack.apply_action(game, "stand")

    assert game["status"] == "settled"
    assert blackjack.hand_value(game["dealer"])[0] == 17
    assert len(game["dealer"]) == 2
    assert game["payout"] == 10
    assert game["result"]["net"] == 0


def test_dealer_draws_below_seventeen_and_busts(monkeypatch) -> None:
    monkeypatch.setattr(blackjack, "make_shoe", lambda: cards_in_draw_order("10", "6", "8", "10", "10"))
    game = blackjack.new_game(10)
    blackjack.apply_action(game, "stand")
    assert game["status"] == "settled"
    assert blackjack.hand_value(game["dealer"])[0] == 26
    assert game["result"]["label"] == "Дилер перебрал"
    assert game["payout"] == 20


def test_insurance_is_hidden_until_player_decides(monkeypatch) -> None:
    monkeypatch.setattr(blackjack, "make_shoe", lambda: cards_in_draw_order("9", "A", "7", "K"))
    game = blackjack.new_game(10)
    assert game["status"] == "insurance"
    public = blackjack.public_state(1, game, 990)
    assert public["dealer"]["cards"][1] == {"hidden": True}
    assert public["insurance"]["can_take"] is True

    assert blackjack.apply_action(game, "insurance") == 5

    assert game["status"] == "settled"
    assert game["payout"] == 15
    assert game["result"]["net"] == 0
    assert game["result"]["insurance_payout"] == 15


def test_split_and_double_update_separate_hands(monkeypatch) -> None:
    monkeypatch.setattr(
        blackjack, "make_shoe",
        lambda: cards_in_draw_order("8", "6", "8", "10", "3", "10", "10", "10"),
    )
    game = blackjack.new_game(10)
    assert blackjack.apply_action(game, "split") == 10
    assert len(game["hands"]) == 2
    assert game["hands"][0]["cards"][1]["rank"] == "3"
    assert blackjack.apply_action(game, "double") == 10
    assert game["hands"][0]["bet"] == 20
    assert game["active_hand"] == 1
    assert blackjack.apply_action(game, "stand") == 0
    assert game["status"] == "settled"
    assert game["total_wagered"] == 30
    assert game["payout"] == 60


@pytest.fixture
async def db(tmp_path):
    database = Database(tmp_path / "blackjack.db", start_balance=1000)
    await database.connect()
    await database.upsert_user(7001, "cards", "Card Player")
    yield database
    await database.close()


async def test_database_debits_actions_once_and_records_net_result(db: Database, monkeypatch) -> None:
    monkeypatch.setattr(
        blackjack, "make_shoe",
        lambda: cards_in_draw_order("8", "6", "8", "10", "3", "10", "10", "10"),
    )
    state = blackjack.new_game(10)
    game = await db.start_blackjack(7001, 10, state, token="start-one")
    assert game["status"] == "playing"
    assert await db.get_balance(7001) == 990

    split = await db.blackjack_action(7001, game["id"], "split", token="split-one")
    assert split["balance"] == 980
    replay = await db.blackjack_action(7001, game["id"], "split", token="split-one")
    assert replay == split
    assert await db.get_balance(7001) == 980

    doubled = await db.blackjack_action(7001, game["id"], "double", token="double-one")
    assert doubled["balance"] == 970
    settled = await db.blackjack_action(7001, game["id"], "stand", token="stand-one")
    assert settled["game"]["status"] == "settled"
    assert settled["balance"] == 1030
    assert await db.get_balance(7001) == 1030
    history = await db.casino_history(7001)
    assert len(history) == 1
    assert history[0]["game"] == "blackjack"
    assert history[0]["amount"] == 30
    assert history[0]["payout"] == 60
    assert history[0]["payout"] - history[0]["amount"] == 30
    assert (await db.blackjack_state(7001))["status"] == "settled"


async def test_database_prevents_parallel_active_hands_and_invalid_actions(db: Database, monkeypatch) -> None:
    monkeypatch.setattr(blackjack, "make_shoe", lambda: cards_in_draw_order("10", "6", "7", "10", "10"))
    state = blackjack.new_game(10)
    first, second = await asyncio.gather(
        db.start_blackjack(7001, 10, state, token="first"),
        db.start_blackjack(7001, 10, blackjack.new_game(10), token="second"),
    )
    assert first["id"] == second["id"]
    assert await db.get_balance(7001) == 990
    with pytest.raises(InvalidBet):
        await db.blackjack_action(7001, first["id"], "insurance", token="bad-action")
    assert await db.get_balance(7001) == 990


async def test_database_rejects_action_from_stale_tab(db: Database, monkeypatch) -> None:
    monkeypatch.setattr(blackjack, "make_shoe", lambda: cards_in_draw_order("10", "6", "7", "10", "2"))
    game = await db.start_blackjack(7001, 10, blackjack.new_game(10), token="start")
    first = await db.blackjack_action(
        7001, game["id"], "hit", token="hit-v0", expected_revision=0
    )
    assert first["game"]["revision"] == 1
    with pytest.raises(InvalidBet, match="обновилась"):
        await db.blackjack_action(
            7001, game["id"], "stand", token="stale-v0", expected_revision=0
        )
    assert await db.get_balance(7001) == 990


@pytest.fixture
async def api_client(tmp_path, monkeypatch):
    from aiohttp.test_utils import TestClient, TestServer

    from app.config import settings
    from app.web.server import build_app

    monkeypatch.setattr(settings, "miniapp_dev_user", 7001)
    database = Database(tmp_path / "blackjack-api.db", start_balance=1000)
    await database.connect()
    await database.upsert_user(7001, "cards", "Card Player")
    client = TestClient(TestServer(build_app(database)))
    await client.start_server()
    yield client, database
    await client.close()
    await database.close()


async def test_api_start_hit_stand_and_resume(api_client, monkeypatch) -> None:
    client, database = api_client
    monkeypatch.setattr(
        blackjack, "make_shoe", lambda: cards_in_draw_order("10", "6", "7", "10", "10")
    )
    response = await client.post("/api/blackjack/start", json={"amount": 10, "token": "start"})
    assert response.status == 200
    started = await response.json()
    game_id = started["game"]["id"]
    assert started["game"]["dealer"]["cards"][1] == {"hidden": True}
    assert started["balance"] == 990

    response = await client.post(
        "/api/blackjack/action", json={"game_id": game_id, "action": "stand", "token": "stand"}
    )
    assert response.status == 200
    settled = await response.json()
    assert settled["game"]["status"] == "settled"
    assert settled["game"]["dealer"]["cards"][1]["rank"] == "10"
    assert settled["game"]["result"]["label"] == "Дилер перебрал"
    assert settled["balance"] == 1010

    current = await client.get("/api/state")
    assert current.status == 200
    assert (await current.json())["blackjack"]["id"] == game_id
    assert await database.get_balance(7001) == 1010


async def test_api_rejects_invalid_stake_without_debit(api_client):
    client, database = api_client
    response = await client.post("/api/blackjack/start", json={"amount": 9})
    assert response.status == 400
    assert await database.get_balance(7001) == 1000
