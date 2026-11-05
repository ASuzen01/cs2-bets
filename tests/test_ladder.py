"""Правила, выплаты и атомарные переходы игры «Лесенка»."""

from __future__ import annotations

import asyncio
import math

import pytest

from app.db import Database, InvalidBet
from app.services import ladder


def test_ladder_multipliers_match_target_rtp() -> None:
    for round_no in range(1, ladder.MAX_ROUNDS + 1):
        probability = ladder.reach_probability(round_no)
        assert probability == pytest.approx(1 / math.factorial(round_no + 1))
        assert ladder.multiplier(round_no) * probability == pytest.approx(ladder.RTP)

    assert [ladder.multiplier(i) for i in range(1, 9)] == pytest.approx(
        [1.91, 5.73, 22.92, 114.6, 687.6, 4813.2, 38505.6, 346550.4]
    )
    assert ladder.payout(50, 8) == 17_327_520


def test_ladder_stake_limits_and_window_bounds() -> None:
    assert ladder.validate_stake(10) == 10
    assert ladder.validate_stake(50) == 50
    for amount in (0, 9, 51):
        with pytest.raises(ValueError):
            ladder.validate_stake(amount)
    assert [ladder.window_count(i) for i in range(1, 9)] == list(range(2, 10))
    with pytest.raises(ValueError):
        ladder.window_count(9)
    with pytest.raises(ValueError):
        ladder.choose_window(1)


def test_choose_window_uses_secure_bounded_random(monkeypatch) -> None:
    seen = []

    def randbelow(bound: int) -> int:
        seen.append(bound)
        return bound - 1

    monkeypatch.setattr(ladder.secrets, "randbelow", randbelow)
    assert ladder.choose_window(9) == 8
    assert seen == [9]


@pytest.fixture
async def db(tmp_path):
    database = Database(tmp_path / "ladder.db", start_balance=1000)
    await database.connect()
    await database.upsert_user(7001, "stepper", "Step Player")
    yield database
    await database.close()


async def test_ladder_miss_ends_game_and_records_loss(db: Database) -> None:
    game = await db.start_ladder(7001, 10, prize_index=0)
    result = await db.pick_ladder(7001, game["id"], picked_index=1, payout=19)

    assert result["status"] == "lost"
    assert result["picked_index"] == 1
    assert result["revealed_index"] == 0
    assert result["payout"] == 0
    assert result["balance"] == 990
    saved = await db.ladder_state(7001)
    assert saved is not None
    assert {key: saved[key] for key in ("status", "picked_index", "revealed_index", "payout")} == {
        key: result[key] for key in ("status", "picked_index", "revealed_index", "payout")
    }
    history = await db.casino_history(7001)
    assert len(history) == 1
    assert history[0]["game"] == "ladder"
    assert history[0]["win"] == 0
    assert history[0]["amount"] == 10


async def test_ladder_cashout_and_repeat_are_safe(db: Database) -> None:
    game = await db.start_ladder(7001, 10, prize_index=1)
    win = await db.pick_ladder(7001, game["id"], picked_index=1, payout=ladder.payout(10, 1))
    assert win["status"] == "won"
    assert win["payout"] == 19
    assert win["balance"] == 990

    cashed = await db.cashout_ladder(7001, game["id"])
    assert cashed["status"] == "cashed"
    assert cashed["balance"] == 1009
    with pytest.raises(InvalidBet):
        await db.cashout_ladder(7001, game["id"])
    assert await db.get_balance(7001) == 1009
    history = await db.casino_history(7001)
    assert len(history) == 1 and history[0]["payout"] == 19


async def test_ladder_continue_hides_new_prize_until_pick(db: Database) -> None:
    game = await db.start_ladder(7001, 10, prize_index=0)
    win = await db.pick_ladder(7001, game["id"], picked_index=0, payout=19)
    assert win["status"] == "won"

    next_floor = await db.continue_ladder(7001, game["id"], prize_index=2)
    assert next_floor["round"] == 2
    assert next_floor["windows"] == 3
    assert next_floor["status"] == "choosing"
    assert next_floor["revealed_index"] is None

    win_again = await db.pick_ladder(7001, game["id"], picked_index=2, payout=57)
    assert win_again["status"] == "won"
    assert win_again["payout"] == 57
    cashed = await db.cashout_ladder(7001, game["id"])
    assert cashed["balance"] == 1047


async def test_ladder_full_run_credits_top_prize(db: Database) -> None:
    game = await db.start_ladder(7001, 50, prize_index=1)
    prize_index = 1
    for round_no in range(1, ladder.MAX_ROUNDS + 1):
        result = await db.pick_ladder(
            7001, game["id"], picked_index=prize_index, payout=ladder.payout(50, round_no)
        )
        assert result["status"] == ("complete" if round_no == 8 else "won")
        if round_no < ladder.MAX_ROUNDS:
            prize_index = round_no
            game = await db.continue_ladder(7001, game["id"], prize_index=prize_index)

    assert result["payout"] == 17_327_520
    assert result["balance"] == 1000 - 50 + 17_327_520
    assert await db.get_balance(7001) == result["balance"]
    history = await db.casino_history(7001)
    assert len(history) == 1
    assert history[0]["game"] == "ladder" and history[0]["win"] == 1


async def test_parallel_start_requests_deduct_only_once(db: Database) -> None:
    starts = await asyncio.gather(*(db.start_ladder(7001, 10, i % 2) for i in range(6)))
    assert len({item["id"] for item in starts}) == 1
    assert await db.get_balance(7001) == 990


@pytest.fixture
async def api_client(tmp_path, monkeypatch):
    from aiohttp.test_utils import TestClient, TestServer

    from app.config import settings
    from app.web.server import build_app

    monkeypatch.setattr(settings, "miniapp_dev_user", 7001)
    database = Database(tmp_path / "ladder-api.db", start_balance=1000)
    await database.connect()
    await database.upsert_user(7001, "stepper", "Step Player")
    client = TestClient(TestServer(build_app(database)))
    await client.start_server()
    yield client, database
    await client.close()
    await database.close()


async def test_ladder_api_full_floor_flow_and_hidden_prize(api_client, monkeypatch) -> None:
    from app.services import ladder as ladder_module

    client, database = api_client
    monkeypatch.setattr(ladder_module, "choose_window", lambda windows: 0)

    response = await client.post("/api/ladder/start", json={"amount": 10})
    assert response.status == 200
    started = await response.json()
    assert started["game"]["windows"] == 2
    assert "prize_index" not in started["game"]
    game_id = started["game"]["id"]

    picked = await client.post(
        "/api/ladder/pick", json={"game_id": game_id, "window_index": 0}
    )
    assert picked.status == 200
    assert (await picked.json())["game"]["status"] == "won"

    continued = await client.post("/api/ladder/continue", json={"game_id": game_id})
    continued_game = (await continued.json())["game"]
    assert continued.status == 200
    assert continued_game["round"] == 2
    assert continued_game["windows"] == 3
    assert continued_game["revealed_index"] is None

    picked_again = await client.post(
        "/api/ladder/pick", json={"game_id": game_id, "window_index": 0}
    )
    assert picked_again.status == 200
    assert (await picked_again.json())["game"]["payout"] == 57

    cashout = await client.post("/api/ladder/cashout", json={"game_id": game_id})
    cashed = await cashout.json()
    assert cashout.status == 200
    assert cashed["game"]["status"] == "cashed"
    assert cashed["balance"] == 1047
    assert await database.get_balance(7001) == 1047


async def test_ladder_api_rejects_stake_below_minimum_without_debit(api_client) -> None:
    client, database = api_client
    response = await client.post("/api/ladder/start", json={"amount": 9})
    assert response.status == 400
    assert "Минимальная ставка" in (await response.json())["error"]
    assert await database.get_balance(7001) == 1000
