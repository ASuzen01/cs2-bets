"""Plinko paytable, server-side route and casino API integration."""

from __future__ import annotations

import math

import pytest

from app.db import Database
from app.services import games


@pytest.mark.parametrize("rows", games.PLINKO_ROWS)
@pytest.mark.parametrize("risk", tuple(games.PLINKO_RISKS))
def test_plinko_paytable_is_symmetric_and_targets_rtp(rows: int, risk: str) -> None:
    multipliers = games.plinko_multipliers(rows, risk)
    assert len(multipliers) == rows + 1
    assert multipliers == multipliers[::-1]
    expected_return = sum(
        math.comb(rows, slot) * multipliers[slot]
        for slot in range(rows + 1)
    ) / (2**rows)
    assert expected_return == pytest.approx(games.PLINKO_RTP, abs=0.006)
    assert multipliers[0] == max(multipliers)


def test_plinko_config_exposes_only_valid_choices() -> None:
    config = games.plinko_config()
    assert config["rows"] == [8, 12, 16]
    assert config["rtp"] == 95.5
    assert set(config["risks"]) == {"low", "medium", "high"}
    assert set(config["multipliers"]) == {
        f"{rows}:{risk}" for rows in games.PLINKO_ROWS for risk in games.PLINKO_RISKS
    }


def test_plinko_path_and_selection_validation() -> None:
    for rows in games.PLINKO_ROWS:
        route = games.plinko_path(rows)
        assert len(route) == rows
        assert set(route) <= {0, 1}
    for selection in ("7:low", "8:extreme", "12", "banana:high"):
        with pytest.raises(ValueError):
            games.play("plinko", selection, 50)


def test_plinko_round_is_settled_from_server_path(monkeypatch: pytest.MonkeyPatch) -> None:
    route = (1,) * 8
    monkeypatch.setattr(games, "plinko_path", lambda rows: route)
    result = games.play("plinko", "8:high", 100)
    assert result["game"] == "plinko"
    assert result["path"] == list(route)
    assert result["slot"] == 8
    assert result["outcome"] == "9"
    assert result["multiplier"] == result["odds"] == result["multipliers"][8]
    assert result["payout"] == round(100 * result["multiplier"])
    assert result["win"] is (result["payout"] > 100)


@pytest.fixture
async def plinko_api_client(tmp_path, monkeypatch):
    from aiohttp.test_utils import TestClient, TestServer

    from app.config import settings
    from app.web.server import build_app

    monkeypatch.setattr(settings, "miniapp_dev_user", 7001)
    database = Database(tmp_path / "plinko-api.db", start_balance=1000)
    await database.connect()
    await database.upsert_user(7001, "lucky", "Lucky")
    client = TestClient(TestServer(build_app(database)))
    await client.start_server()
    yield client, database
    await client.close()
    await database.close()


async def test_plinko_api_publishes_config_and_plays_server_route(
    plinko_api_client, monkeypatch: pytest.MonkeyPatch
) -> None:
    client, database = plinko_api_client
    route = (1,) * 8
    monkeypatch.setattr(games, "plinko_path", lambda rows: route)

    state_response = await client.get("/api/casino")
    assert state_response.status == 200
    state = await state_response.json()
    assert state["plinko"]["rtp"] == 95.5
    assert "8:high" in state["plinko"]["multipliers"]

    response = await client.post(
        "/api/casino/play",
        json={"game": "plinko", "selection": "8:high", "amount": 100, "token": "plinko-test-1"},
    )
    assert response.status == 200
    result = await response.json()
    assert result["path"] == list(route)
    assert result["slot"] == 8
    assert result["payout"] == round(100 * result["multiplier"])
    assert result["balance"] == 900 + result["payout"]
    history = await database.casino_history(7001)
    assert len(history) == 1
    assert history[0]["game"] == "plinko"
    assert history[0]["payout"] == result["payout"]
