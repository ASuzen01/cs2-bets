"""Punto Banco rules, standard payouts and casino integration."""

from __future__ import annotations

import pytest

from app.db import Database
from app.services import baccarat


def card(rank: str, suit: str = "♠") -> dict:
    return {"rank": rank, "suit": suit, "points": baccarat.CARD_POINTS[rank]}


def shoe_in_deal_order(*cards: dict) -> list[dict]:
    # Baccarat pops from the end of the shuffled shoe.
    return list(reversed(cards))


def test_baccarat_shoe_has_eight_standard_decks() -> None:
    shoe = baccarat.build_shoe()
    assert len(shoe) == 416
    assert len({(item["rank"], item["suit"]) for item in shoe}) == 52
    assert baccarat._points([card("K"), card("9"), card("8")]) == 7


def test_banker_third_card_tableau() -> None:
    assert baccarat.banker_draws(0, None)
    assert baccarat.banker_draws(5, None)
    assert not baccarat.banker_draws(6, None)
    assert baccarat.banker_draws(2, 8)
    assert not baccarat.banker_draws(3, 8)
    assert baccarat.banker_draws(3, 7)
    assert baccarat.banker_draws(4, 2)
    assert not baccarat.banker_draws(4, 8)
    assert baccarat.banker_draws(5, 4)
    assert not baccarat.banker_draws(5, 3)
    assert baccarat.banker_draws(6, 6)
    assert not baccarat.banker_draws(6, 5)
    assert not baccarat.banker_draws(7, 7)


def test_baccarat_standard_payouts_and_tie_push() -> None:
    player_win_shoe = shoe_in_deal_order(card("9"), card("2"), card("K"), card("3"))
    player_win = baccarat.play("player", 100, deck=player_win_shoe)
    assert player_win["result"] == "player" and player_win["natural"]
    assert player_win["payout"] == 200 and player_win["win"] is True

    banker_win_shoe = shoe_in_deal_order(card("7"), card("9"), card("K"), card("K"))
    banker_win = baccarat.play("banker", 100, deck=banker_win_shoe)
    assert banker_win["result"] == "banker"
    assert banker_win["payout"] == 195 and banker_win["win"] is True

    tie_shoe = shoe_in_deal_order(card("5"), card("4"), card("3"), card("4"))
    player_push = baccarat.play("player", 100, deck=tie_shoe.copy())
    assert player_push["result"] == "tie"
    assert player_push["payout"] == 100 and player_push["win"] is False
    tie_bet = baccarat.play("tie", 100, deck=tie_shoe.copy())
    assert tie_bet["payout"] == 900 and tie_bet["win"] is True


def test_baccarat_player_and_banker_draw_by_tableau() -> None:
    # Player 5 draws a 4; Banker 4 then draws on the Player's 4.
    shoe = shoe_in_deal_order(card("2"), card("A"), card("3"), card("3"), card("4"), card("5"))
    result = baccarat.play("banker", 25, deck=shoe)
    assert len(result["player_cards"]) == 3
    assert len(result["banker_cards"]) == 3
    assert result["player_points"] == result["banker_points"] == 9


def test_invalid_baccarat_bet_is_rejected() -> None:
    with pytest.raises(ValueError):
        baccarat.play("draw", 100)
    with pytest.raises(ValueError):
        baccarat.play("tie", 0)


@pytest.fixture
async def baccarat_api_client(tmp_path, monkeypatch):
    from aiohttp.test_utils import TestClient, TestServer

    from app.config import settings
    from app.web.server import build_app

    monkeypatch.setattr(settings, "miniapp_dev_user", 9901)
    database = Database(tmp_path / "baccarat-api.db", start_balance=1000)
    await database.connect()
    await database.upsert_user(9901, "player", "Player")
    client = TestClient(TestServer(build_app(database)))
    await client.start_server()
    yield client, database
    await client.close()
    await database.close()


async def test_baccarat_api_state_and_round_record(baccarat_api_client) -> None:
    client, database = baccarat_api_client
    state_response = await client.get("/api/casino")
    assert state_response.status == 200
    state = await state_response.json()
    assert {item["code"] for item in state["options"]["baccarat"]} == {"player", "banker", "tie"}

    response = await client.post(
        "/api/casino/play",
        json={"game": "baccarat", "selection": "banker", "amount": 50, "token": "baccarat-test-1"},
    )
    assert response.status == 200
    result = await response.json()
    assert result["game"] == "baccarat"
    assert 0 <= result["player_points"] <= 9 and 0 <= result["banker_points"] <= 9
    assert len(result["player_cards"]) in (2, 3)
    assert len(result["banker_cards"]) in (2, 3)
    assert result["balance"] == 950 + result["payout"]
    assert result["payout"] in (0, 50, 98)
    history = await database.casino_history(9901)
    assert len(history) == 1 and history[0]["game"] == "baccarat"
