"""Cluster cascades, full-reel Wild expansion, and the Riftfall casino API."""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from aiohttp.test_utils import TestClient, TestServer

from app.db import Database
from app.services import riftfall
from app.web.server import build_app


def test_riftfall_config_discloses_rules_and_payouts() -> None:
    config = riftfall.slot_config()
    assert (config["rows"], config["reels"]) == (4, 5)
    assert config["rtp"] == 95.5
    assert config["max_cascades"] == 4
    assert config["chain_multipliers"] == [1.0, 1.5, 2.25, 3.5]
    assert sum(symbol["weight"] for symbol in riftfall.SYMBOLS) == 10_000
    assert set(config["cluster_payouts"]) == {str(size) for size in range(5, 21)}
    assert config["wild_code"] == "wild"


def test_landed_wild_expands_to_the_entire_reel() -> None:
    grid = [["coin"] * riftfall.REELS for _ in range(riftfall.ROWS)]
    grid[0][2] = riftfall.WILD_CODE
    clusters, cleared, shown = riftfall._winning_clusters(grid)
    assert [shown[row][2] for row in range(riftfall.ROWS)] == [riftfall.WILD_CODE] * riftfall.ROWS
    assert len(clusters) == 1
    assert clusters[0]["symbol"] == "coin"
    assert clusters[0]["size"] == riftfall.ROWS * riftfall.REELS
    assert len(cleared) == riftfall.ROWS * riftfall.REELS


def test_cluster_detection_uses_orthogonal_adjacency() -> None:
    grid = [
        ["coin", "coin", "coin", "bull", "bull"],
        ["coin", "coin", "bull", "diamond", "briefcase"],
        ["coin", "bull", "diamond", "briefcase", "crown"],
        ["bull", "diamond", "briefcase", "crown", "coin"],
    ]
    clusters, cleared, _ = riftfall._winning_clusters(grid)
    coin_clusters = [cluster for cluster in clusters if cluster["symbol"] == "coin"]
    assert [cluster["size"] for cluster in coin_clusters] == [6]
    assert set(map(tuple, coin_clusters[0]["cells"])) <= cleared


def test_spin_records_all_cascades_and_cumulative_payouts() -> None:
    result = riftfall.spin(100, draw_symbol=lambda: "coin")
    assert len(result["initial_grid"]) == riftfall.ROWS
    assert all(len(row) == riftfall.REELS for row in result["initial_grid"])
    assert result["cascade_count"] == riftfall.MAX_CASCADES
    assert [step["chain_multiplier"] for step in result["steps"]] == list(riftfall.CHAIN_MULTIPLIERS)
    assert all(len(step["winning_cells"]) == riftfall.ROWS * riftfall.REELS for step in result["steps"])
    assert sum(step["cascade_payout"] for step in result["steps"]) == result["payout"]
    assert result["win"] is True
    assert len(result["grid"]) == riftfall.ROWS
    assert all(len(row) == riftfall.REELS for row in result["grid"])


def test_spin_rejects_invalid_stakes_and_drawn_symbols() -> None:
    with pytest.raises(ValueError, match="положительной"):
        riftfall.spin(0)
    with pytest.raises(ValueError, match="Неизвестный символ"):
        riftfall.spin(10, draw_symbol=lambda: "bogus")


@pytest.fixture
async def riftfall_api(tmp_path, monkeypatch) -> Iterator[tuple[TestClient, Database]]:
    from app.config import settings

    monkeypatch.setattr(settings, "miniapp_dev_user", 7201)
    database = Database(tmp_path / "riftfall.db", start_balance=1000)
    await database.connect()
    await database.upsert_user(7201, "highroller", "High Roller")
    client = TestClient(TestServer(build_app(database)))
    await client.start_server()
    try:
        yield client, database
    finally:
        await client.close()
        await database.close()


async def test_riftfall_api_debits_once_and_returns_server_result(riftfall_api, monkeypatch) -> None:
    client, database = riftfall_api
    def fixed_spin(amount: int) -> dict:
        return {
            "game": "riftfall",
            "selection": "spin",
            "label": "Золотой разлом",
            "amount": amount,
            "spin_bet": amount,
            "odds": 1.5,
            "payout": 150,
            "win": True,
            "outcome": "2 каскада · 3 кластера · ×1.50",
            "grid": [["coin"] * riftfall.REELS for _ in range(riftfall.ROWS)],
            "initial_grid": [["coin"] * riftfall.REELS for _ in range(riftfall.ROWS)],
            "steps": [],
            "cascade_count": 2,
            "cluster_count": 3,
            "total_multiplier": 1.5,
            "max_cascades": 4,
            "rtp": 95.5,
        }

    monkeypatch.setattr(riftfall, "spin", fixed_spin)
    state_response = await client.get("/api/casino")
    state = await state_response.json()
    assert state["riftfall"]["max_cascades"] == 4

    body = {"game": "riftfall", "selection": "spin", "amount": 100, "token": "riftfall-round-once"}
    first_response = await client.post("/api/casino/play", json=body)
    duplicate_response = await client.post("/api/casino/play", json=body)
    assert first_response.status == duplicate_response.status == 200, await first_response.text()
    first = await first_response.json()
    duplicate = await duplicate_response.json()
    assert first["round"] == duplicate["round"]
    assert first["balance"] == duplicate["balance"] == 1050
    assert first["payout"] == duplicate["payout"] == 150
    assert len(await database.casino_history(7201)) == 1
