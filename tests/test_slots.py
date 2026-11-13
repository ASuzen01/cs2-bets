"""5×3 slot math, durable free spins, and casino API flow."""

from __future__ import annotations

import sqlite3

import pytest

from app.db import Database, InsufficientFunds, InvalidBet
from app.services import slot_machine


def _fixed_result(
    *, payout: int, award: int = 0, free_spin: bool = False, chest_picks: int | None = None
) -> dict:
    scatter_count = {10: 3, 15: 4, 20: 5}.get(award, 0)
    if chest_picks is None:
        chest_picks = slot_machine._chest_pick_count(scatter_count) if not free_spin else 0
    return {
        "grid": [["cherry"] * 5 for _ in range(3)],
        "scatter_count": scatter_count,
        "awarded_free_spins": award if not free_spin else 0,
        "awarded_chest_picks": chest_picks if not free_spin else 0,
        "wins": [],
        "win_count": 0,
        "winning_cells": [],
        "payout": payout,
        "multiplier": payout / 100,
        "win": payout > 0,
        "free_spin": free_spin,
        "outcome": "test spin",
    }


def test_slot_configuration_and_expected_rtp() -> None:
    config = slot_machine.slot_config()
    assert (config["rows"], config["reels"], config["ways"]) == (3, 5, 243)
    assert config["rtp"] == 95.5
    assert config["scatter_awards"] == {"3": 10, "4": 15, "5": 20}
    assert config["chest_picks"] == {"3": 1, "4": 2, "5": 3}
    assert config["chest_rtp_points"] > 0
    assert next(symbol for symbol in config["symbols"] if symbol["code"] == "bar")["icon"] == "BAR"
    actual = (
        slot_machine._expected_return_per_way(slot_machine.PAYTABLE)
        * (1 + slot_machine._expected_free_spins())
        + slot_machine._expected_chest_return()
    )
    assert actual == pytest.approx(slot_machine.RTP, abs=0.0001)


def test_slot_spin_has_5x3_grid_and_no_free_spin_retrigger(monkeypatch: pytest.MonkeyPatch) -> None:
    # Three scatters in the first reel plus twelve cherries make the bonus deterministic.
    codes = iter(["scatter", "scatter", "scatter", *(["cherry"] * 12)])
    monkeypatch.setattr(slot_machine, "_draw_symbol", lambda: next(codes))
    paid = slot_machine.spin(243)
    assert len(paid["grid"]) == 3
    assert all(len(row) == 5 for row in paid["grid"])
    assert paid["scatter_count"] == 3
    assert paid["awarded_free_spins"] == 10
    assert paid["awarded_chest_picks"] == 1
    assert paid["payout"] >= 0
    assert paid["winning_cells"]

    codes = iter(["scatter"] * 5 + ["cherry"] * 10)
    monkeypatch.setattr(slot_machine, "_draw_symbol", lambda: next(codes))
    free = slot_machine.spin(243, free_spin=True)
    assert free["scatter_count"] == 5
    assert free["awarded_free_spins"] == 0
    assert free["awarded_chest_picks"] == 0


def test_chest_board_has_the_public_prize_composition() -> None:
    board = slot_machine.new_chest_board()
    assert len(board) == 12
    assert {multiplier: board.count(multiplier) for multiplier, _ in slot_machine.CHEST_PRIZES} == dict(
        slot_machine.CHEST_PRIZES
    )


def test_slot_five_of_a_kind_is_one_way_payout(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(slot_machine, "_draw_symbol", lambda: "cherry")
    result = slot_machine.spin(243)
    cherry_ways = [win for win in result["wins"] if win["symbol"] == "cherry"]
    assert cherry_ways == [{
        "symbol": "cherry",
        "length": 5,
        "ways": 3**5,
        "multiplier": slot_machine.PAYTABLE["cherry"][2],
    }]
    assert result["payout"] == round(243 * slot_machine.PAYTABLE["cherry"][2])


@pytest.fixture
async def slot_db(tmp_path):
    database = Database(tmp_path / "slots.db", start_balance=1000)
    await database.connect()
    await database.upsert_user(9001, "spinner", "Spinner")
    yield database
    await database.close()


async def test_adding_chest_state_migration_preserves_existing_free_spins(tmp_path) -> None:
    path = tmp_path / "legacy-slots.db"
    raw = sqlite3.connect(path)
    raw.execute(
        "CREATE TABLE slot_states (user_id INTEGER PRIMARY KEY, free_spins INTEGER NOT NULL, "
        "spin_bet INTEGER NOT NULL, updated_at INTEGER NOT NULL)"
    )
    raw.execute("INSERT INTO slot_states VALUES (9001, 7, 75, 123)")
    raw.commit()
    raw.close()

    database = Database(path)
    await database.connect()
    try:
        assert await database.slot_state(9001) == {"free_spins": 7, "spin_bet": 75, "bonus": None}
    finally:
        await database.close()


async def test_scatter_chest_pick_persists_and_free_spins_keep_locked_bet(slot_db: Database, monkeypatch) -> None:
    monkeypatch.setattr(slot_machine, "new_chest_board", lambda: [0, 0.5, 1, 3] + [0] * 8)
    paid = await slot_db.play_slot(
        9001,
        100,
        spin=lambda wager, free: _fixed_result(payout=25, award=10, free_spin=free),
        token="slot-paid-1",
    )
    assert paid["balance"] == 925
    assert paid["free_spins_remaining"] == 10
    assert paid["free_spin_bet"] == 100
    assert paid["slot_state"]["bonus"]["status"] == "pending"
    assert "board" not in paid["slot_state"]["bonus"]
    assert (await slot_db.slot_state(9001))["bonus"]["picks_remaining"] == 1

    with pytest.raises(InvalidBet, match="Сначала открой все сундуки"):
        await slot_db.play_slot(
            9001, 500, spin=lambda wager, free: _fixed_result(payout=0, free_spin=free)
        )
    pick = await slot_db.pick_slot_chest(9001, 2, "chest-pick-1")
    repeated = await slot_db.pick_slot_chest(9001, 2, "chest-pick-1")
    assert pick["payout"] == 100 and repeated["payout"] == 100
    assert await slot_db.get_balance(9001) == 1025
    assert (await slot_db.slot_state(9001))["bonus"]["status"] == "complete"
    assert len(await slot_db.casino_history(9001)) == 2

    free = await slot_db.play_slot(
        9001,
        500,
        spin=lambda wager, is_free: _fixed_result(payout=10, free_spin=is_free),
        token="slot-free-1",
    )
    assert free["free_spin"] is True
    assert free["spin_bet"] == 100
    assert free["amount"] == 0
    assert free["free_spins_remaining"] == 9
    assert free["balance"] == 1035
    history = await slot_db.casino_history(9001)
    assert len(history) == 3
    assert history[0]["game"] == "slots" and history[0]["amount"] == 0


async def test_slot_insufficient_funds_does_not_consume_or_log(slot_db: Database) -> None:
    with pytest.raises(InsufficientFunds):
        await slot_db.play_slot(
            9001,
            1001,
            spin=lambda wager, free: _fixed_result(payout=0, free_spin=free),
        )
    assert await slot_db.get_balance(9001) == 1000
    assert await slot_db.casino_history(9001) == []
    assert await slot_db.slot_state(9001) == {"free_spins": 0, "spin_bet": 0, "bonus": None}


async def test_slot_idempotency_does_not_redraw_or_debit_twice(slot_db: Database) -> None:
    calls = []

    def draw(wager: int, free: bool) -> dict:
        calls.append((wager, free))
        return _fixed_result(payout=20, award=0, free_spin=free)

    first = await slot_db.play_slot(9001, 100, spin=draw, token="same-slot-tap")
    repeated = await slot_db.play_slot(9001, 100, spin=draw, token="same-slot-tap")
    assert first["round"] == repeated["round"]
    assert first["balance"] == repeated["balance"] == 920
    assert calls == [(100, False)]
    assert len(await slot_db.casino_history(9001)) == 1


@pytest.fixture
async def slot_api_client(tmp_path, monkeypatch):
    from aiohttp.test_utils import TestClient, TestServer

    from app.config import settings
    from app.web.server import build_app

    monkeypatch.setattr(settings, "miniapp_dev_user", 9001)
    database = Database(tmp_path / "slots-api.db", start_balance=1000)
    await database.connect()
    await database.upsert_user(9001, "spinner", "Spinner")
    client = TestClient(TestServer(build_app(database)))
    await client.start_server()
    yield client, database
    await client.close()
    await database.close()


async def test_slot_api_config_paid_and_free_spin_flow(slot_api_client, monkeypatch: pytest.MonkeyPatch) -> None:
    client, database = slot_api_client
    # слот-машина архивирована: конфигурация из /api/casino не публикуется,
    # но серверный маршрут /api/casino/play продолжает работать
    state_response = await client.get("/api/casino")
    assert state_response.status == 200
    state = await state_response.json()
    assert "slots" not in state and "slot_state" not in state

    monkeypatch.setattr(slot_machine, "new_chest_board", lambda: [1.0] * 12)
    results = iter([
        _fixed_result(payout=25, award=10),
        _fixed_result(payout=10, free_spin=True),
    ])
    monkeypatch.setattr(slot_machine, "spin", lambda wager, *, free_spin=False: next(results))
    paid_response = await client.post(
        "/api/casino/play",
        json={"game": "slots", "selection": "spin", "amount": 100, "token": "api-slot-paid"},
    )
    assert paid_response.status == 200
    paid = await paid_response.json()
    assert paid["balance"] == 925
    assert paid["free_spins_remaining"] == 10
    assert paid["slot_state"]["bonus"]["picks_remaining"] == 1

    chest_response = await client.post(
        "/api/casino/slots/chest",
        json={"chest": 0, "token": "api-chest-pick"},
    )
    assert chest_response.status == 200
    chest = await chest_response.json()
    assert chest["payout"] == 100 and chest["balance"] == 1025
    assert chest["slot_state"]["bonus"]["status"] == "complete"

    free_response = await client.post(
        "/api/casino/play",
        json={"game": "slots", "selection": "spin", "amount": 500, "token": "api-slot-free"},
    )
    assert free_response.status == 200
    free = await free_response.json()
    assert free["free_spin"] is True
    assert free["spin_bet"] == 100
    assert free["balance"] == 1035
    saved = await database.slot_state(9001)
    assert saved["free_spins"] == 9 and saved["spin_bet"] == 100
    assert saved["bonus"]["status"] == "complete"
