"""Математика, атомарные переходы и HTTP-flow игры «Мины»."""

from __future__ import annotations

import asyncio
import math

import pytest

from app.db import Database, InsufficientFunds, InvalidBet
from app.services import mines


def test_mines_probability_multiplier_and_integer_payout() -> None:
    assert pytest.approx(0.955) == mines.RTP
    assert mines.survival_probability(3, 1) == pytest.approx(22 / 25)
    assert mines.survival_probability(3, 3) == pytest.approx(math.comb(22, 3) / math.comb(25, 3))
    for mine_count in (2, 3, 5, 10, 24):
        for safe_picks in range(1, mines.CELLS - mine_count + 1):
            assert mines.multiplier(mine_count, safe_picks) * mines.survival_probability(
                mine_count, safe_picks
            ) == pytest.approx(mines.RTP)
    assert mines.payout(100, 3, 1) == 108
    assert mines.payout(100, 3, 3) == 142
    assert mines.multiplier(24, 1) == pytest.approx(23.875)
    assert mines.payout(10, 24, 1) == 238


def test_mines_limits_and_layout_validation() -> None:
    assert mines.validate_stake(10) == 10
    for stake in (0, 9, -2):
        with pytest.raises(ValueError):
            mines.validate_stake(stake)
    assert [mines.validate_mine_count(value) for value in (2, 3, 24)] == [2, 3, 24]
    for count in (1, 25, -1):
        with pytest.raises(ValueError):
            mines.validate_mine_count(count)
    assert mines.validate_layout(3, [4, 2, 1]) == [1, 2, 4]
    for layout in ([0, 1], [0, 0, 1], [-1, 1, 2], [0, 1, 25]):
        with pytest.raises(ValueError):
            mines.validate_layout(3, list(layout))


def test_mines_layout_uses_unique_in_range_positions(monkeypatch) -> None:
    seen = []

    class FakeRandom:
        def sample(self, population, count):
            seen.append((list(population), count))
            return [24, 0, 12]

    monkeypatch.setattr(mines.secrets, "SystemRandom", FakeRandom)
    assert mines.place_mines(3) == [24, 0, 12]
    assert seen == [(list(range(25)), 3)]


@pytest.fixture
async def db(tmp_path):
    database = Database(tmp_path / "mines.db", start_balance=1000)
    await database.connect()
    await database.upsert_user(7801, "miner", "Mine Player")
    yield database
    await database.close()


async def test_mines_safe_pick_cashout_and_idempotent_repeats(db: Database) -> None:
    game = await db.start_mines(7801, 100, 3, [0, 1, 2], token="start-1")
    assert game["status"] == "active"
    assert game["balance"] == 900
    assert game["opened_cells"] == []
    assert "mine_positions" not in game

    opened = await db.pick_mines(7801, game["id"], 4, 0, token="pick-1")
    assert opened["status"] == "active"
    assert opened["opened_cells"] == [4]
    assert opened["safe_count"] == 1
    assert opened["potential_payout"] == 108
    assert "mine_positions" not in opened
    repeated_pick = await db.pick_mines(7801, game["id"], 4, 0, token="pick-1")
    assert repeated_pick["opened_cells"] == [4]

    cashed = await db.cashout_mines(7801, game["id"], 1, token="cash-1")
    assert cashed["status"] == "cashed"
    assert cashed["payout"] == 108
    assert cashed["balance"] == 1008
    assert cashed["mine_positions"] == [0, 1, 2]
    repeated_cashout = await db.cashout_mines(7801, game["id"], 1, token="cash-1")
    assert repeated_cashout["balance"] == 1008
    assert await db.get_balance(7801) == 1008

    history = await db.casino_history(7801)
    assert len(history) == 1
    assert history[0]["game"] == "mines"
    assert history[0]["amount"] == 100
    assert history[0]["payout"] == 108
    assert history[0]["outcome"] == "забрано · 1 безопасных"


async def test_mines_hit_reveals_layout_and_does_not_return_stake(db: Database) -> None:
    game = await db.start_mines(7801, 10, 3, [0, 1, 2])
    result = await db.pick_mines(7801, game["id"], 0, 0)

    assert result["status"] == "lost"
    assert result["payout"] == 0
    assert result["mine_positions"] == [0, 1, 2]
    assert result["opened_cells"] == [0]
    assert result["safe_count"] == 0
    assert result["balance"] == 990
    assert await db.get_balance(7801) == 990
    history = await db.casino_history(7801)
    assert len(history) == 1 and history[0]["win"] == 0


async def test_mines_last_safe_cell_auto_settles(db: Database) -> None:
    game = await db.start_mines(7801, 10, 24, list(range(24)))
    result = await db.pick_mines(7801, game["id"], 24, 0)

    assert result["status"] == "cleared"
    assert result["safe_count"] == 1
    assert result["payout"] == 238
    assert result["balance"] == 1228
    assert len(result["mine_positions"]) == 24
    assert len(await db.casino_history(7801)) == 1


async def test_mines_stale_requests_and_invalid_actions_are_rejected(db: Database) -> None:
    game = await db.start_mines(7801, 10, 2, [0, 1])
    with pytest.raises(InvalidBet):
        await db.pick_mines(7801, game["id"], 25, 0)
    with pytest.raises(InvalidBet):
        await db.cashout_mines(7801, game["id"], 0)

    await db.pick_mines(7801, game["id"], 2, 0)
    with pytest.raises(InvalidBet):
        await db.pick_mines(7801, game["id"], 3, 0)
    with pytest.raises(InvalidBet):
        await db.cashout_mines(7801, game["id"], 0)
    assert (await db.mines_state(7801))["opened_cells"] == [2]


async def test_mines_start_is_atomic_and_only_one_active_round(db: Database) -> None:
    starts = await asyncio.gather(*(
        db.start_mines(7801, 10, 3, [index, index + 1, index + 2]) for index in range(5)
    ))
    assert len({game["id"] for game in starts}) == 1
    assert await db.get_balance(7801) == 990
    await db.upsert_user(7802, "lowbalance", "Low Balance")
    with pytest.raises(InsufficientFunds):
        await db.start_mines(7802, 2000, 3, [0, 1, 2])


@pytest.fixture
async def api_client(tmp_path, monkeypatch):
    from aiohttp.test_utils import TestClient, TestServer

    from app.config import settings
    from app.web.server import build_app

    monkeypatch.setattr(settings, "miniapp_dev_user", 7801)
    database = Database(tmp_path / "mines-api.db", start_balance=1000)
    await database.connect()
    await database.upsert_user(7801, "miner", "Mine Player")
    client = TestClient(TestServer(build_app(database)))
    await client.start_server()
    yield client, database
    await client.close()
    await database.close()


async def test_mines_api_hides_layout_and_cashout_flow(api_client, monkeypatch) -> None:
    from app.services import mines as mines_service

    client, db = api_client
    monkeypatch.setattr(mines_service, "place_mines", lambda _count: [0, 1, 2])
    response = await client.post("/api/mines/start", json={
        "amount": 100, "mine_count": 3, "token": "api-start",
    })
    assert response.status == 200
    started = await response.json()
    assert started["game"]["status"] == "active"
    assert "mine_positions" not in started["game"]
    game_id = started["game"]["id"]

    state_response = await client.get("/api/mines/state")
    state = await state_response.json()
    assert "mine_positions" not in state["game"]

    picked_response = await client.post("/api/mines/pick", json={
        "game_id": game_id, "cell": 4, "expected_open_count": 0, "token": "api-pick",
    })
    picked = await picked_response.json()
    assert picked["game"]["safe_count"] == 1
    assert "mine_positions" not in picked["game"]

    cash_response = await client.post("/api/mines/cashout", json={
        "game_id": game_id, "expected_open_count": 1, "token": "api-cash",
    })
    cashed = await cash_response.json()
    assert cashed["game"]["status"] == "cashed"
    assert cashed["game"]["payout"] == 108
    assert cashed["balance"] == 1008
    assert await db.get_balance(7801) == 1008


async def test_mines_api_validates_stake_and_mine_count(api_client) -> None:
    client, _db = api_client
    for body in (
        {"amount": 9, "mine_count": 3},
        {"amount": 10, "mine_count": 1},
        {"amount": 10, "mine_count": 25},
    ):
        response = await client.post("/api/mines/start", json=body)
        assert response.status == 400
        assert (await response.json())["ok"] is False
