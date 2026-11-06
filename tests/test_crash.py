"""Rules, money safety and HTTP flow for «Самолётик» (crash)."""

from __future__ import annotations

import math

import pytest

from app.db import Database, InsufficientFunds, InvalidBet
from app.services import crash


def test_crash_distribution_endpoints_and_multiplier() -> None:
    assert pytest.approx(0.955) == crash.RTP
    assert crash.crash_point(0) == 1.0
    assert crash.crash_point(500_000) == 1.91
    assert crash.crash_point(999_999) == 955_000.0
    assert crash.multiplier_at(100, 100) == 1.0
    assert crash.multiplier_at(100, 100 + math.log(2) / crash.GROWTH_PER_SECOND) == pytest.approx(1.99)
    assert crash.payout(50, 2.39) == 119


def test_crash_probability_buckets_match_the_distribution() -> None:
    # RNG uses every integer in [0, 1_000_000) with equal probability.
    # Monotone boundary checks pin the exact counts in the x1.00 and below-x2 buckets.
    assert crash.crash_point(54_455) == 1.00
    assert crash.crash_point(54_456) == 1.01
    assert crash.crash_point(522_499) == 1.99
    assert crash.crash_point(522_500) == 2.00
    assert pytest.approx(0.054456) == 54_456 / 1_000_000  # exactly x1.00
    assert pytest.approx(0.5225) == 522_500 / 1_000_000  # x1.00 through x1.99
    assert pytest.approx(crash.RTP) == 477_500 / 1_000_000 * 2  # reaches x2.00


def test_crash_minimum_stake_without_maximum() -> None:
    assert crash.validate_stake(10) == 10
    assert crash.validate_stake(5_000) == 5_000
    assert crash.validate_stake(100_000) == 100_000
    for amount in (0, 9):
        with pytest.raises(ValueError):
            crash.validate_stake(amount)


@pytest.fixture
async def db(tmp_path):
    database = Database(tmp_path / "crash.db", start_balance=1000)
    await database.connect()
    await database.upsert_user(7001, "pilot", "Pilot")
    yield database
    await database.close()


async def test_crash_cashout_credits_gross_payout_once(db: Database) -> None:
    game = await db.start_crash(7001, 10, 5.0, now=100.0)
    assert game["status"] == "flying"
    assert "crash_multiplier" not in game
    assert game["balance"] == 990

    result = await db.cashout_crash(7001, game["id"], now=105.0)
    assert result["status"] == "cashed"
    assert result["cashout_multiplier"] == pytest.approx(2.22)
    assert result["payout"] == 22
    assert result["crash_multiplier"] == 5.0
    assert result["balance"] == 1012
    repeated = await db.cashout_crash(7001, game["id"], now=105.0)
    assert repeated["balance"] == 1012
    history = await db.casino_history(7001)
    assert len(history) == 1
    assert history[0]["game"] == "crash"
    assert history[0]["win"] == 1
    assert history[0]["payout"] == 22


async def test_crash_state_settles_loss_and_never_exposes_early(db: Database) -> None:
    game = await db.start_crash(7001, 20, 2.5, now=100.0)
    active = await db.crash_state(7001, now=100.1)
    assert active is not None and active["status"] == "flying"
    assert "crash_multiplier" not in active

    lost = await db.crash_state(7001, now=120.0)
    assert lost is not None and lost["status"] == "crashed"
    assert lost["crash_multiplier"] == 2.5
    assert lost["payout"] == 0
    assert lost["balance"] == 980
    history = await db.casino_history(7001)
    assert len(history) == 1 and history[0]["win"] == 0

    repeated = await db.crash_state(7001, now=121.0)
    assert repeated is not None and repeated["status"] == "crashed"
    assert len(await db.casino_history(7001)) == 1
    with pytest.raises(InvalidBet):
        await db.cashout_crash(7001, game["id"] + 1, now=121)


async def test_crash_repeated_start_does_not_debit_twice(db: Database) -> None:
    first = await db.start_crash(7001, 50, 5.0, now=100.0)
    second = await db.start_crash(7001, 100, 8.0, now=101.0)
    assert first["id"] == second["id"]
    assert second["stake"] == 50
    assert second["balance"] == 950
    await db.cashout_crash(7001, first["id"], now=120.0)  # flight expires without a cash-out
    with pytest.raises(InsufficientFunds):
        await db.start_crash(7001, 951, 2.0, now=121.0)
    assert await db.get_balance(7001) == 950


@pytest.fixture
async def crash_api_client(tmp_path, monkeypatch):
    from aiohttp.test_utils import TestClient, TestServer

    from app.config import settings
    from app.web.server import build_app

    monkeypatch.setattr(settings, "miniapp_dev_user", 7001)
    database = Database(tmp_path / "crash-api.db", start_balance=1000)
    await database.connect()
    await database.upsert_user(7001, "pilot", "Pilot")
    client = TestClient(TestServer(build_app(database)))
    await client.start_server()
    yield client, database
    await client.close()
    await database.close()


async def test_crash_api_round_flow(crash_api_client, monkeypatch) -> None:
    from app.db import database as database_module
    from app.services import crash as crash_module

    client, _database = crash_api_client
    clock = [100.0]
    monkeypatch.setattr(database_module.time, "time", lambda: clock[0])
    monkeypatch.setattr(crash_module, "crash_point", lambda: 5.0)

    started_response = await client.post("/api/crash/start", json={"amount": 10})
    assert started_response.status == 200
    started = await started_response.json()
    assert started["game"]["status"] == "flying"
    assert "crash_multiplier" not in started["game"]

    clock[0] = 105.0
    cashout_response = await client.post(
        "/api/crash/cashout", json={"game_id": started["game"]["id"]}
    )
    assert cashout_response.status == 200
    cashed = await cashout_response.json()
    assert cashed["game"]["status"] == "cashed"
    assert cashed["balance"] == 1012

    state_response = await client.get("/api/state")
    assert state_response.status == 200
    state = await state_response.json()
    assert state["crash"]["status"] == "cashed"
    assert any(item["game"] == "crash" for item in state["casino"]["history"])


async def test_crash_api_accepts_stake_above_former_ceiling(crash_api_client) -> None:
    client, database = crash_api_client
    await database.add_coins(7001, 6_000, "тест большого баланса")
    response = await client.post("/api/crash/start", json={"amount": 6_000})
    assert response.status == 200
    result = await response.json()
    assert result["ok"] is True
    assert result["game"]["stake"] == 6_000
    assert result["balance"] == 1_000
