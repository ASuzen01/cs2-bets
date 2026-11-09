"""HTTP API for «Пьяница»: bot game and private player challenges."""

from __future__ import annotations

from pathlib import Path

import pytest
from aiohttp.test_utils import TestClient, TestServer

from app.config import settings
from app.db import Database
from app.services import drunkard
from app.web.server import build_app


def test_game_catalog_and_controls_expose_requested_options() -> None:
    html = Path("app/web/index.html").read_text(encoding="utf-8")
    js = Path("app/web/static/app.js").read_text(encoding="utf-8")
    assert 'data-game="drunkard"' in html and "Пьяница" in html
    assert "[24, 36, 52]" in js
    assert "[10, 50, 100, 250, 500]" in js
    assert "Игрок по приглашению" in js
    assert 'drunkardRequest("invite"' in js and 'drunkardRequest("accept"' in js
    assert 'drunkardRequest("step"' in js and "Открыть следующие карты" in js
    assert "ПО ОДНОЙ КАРТЕ РУБАШКОЙ ВВЕРХ" in js
    assert "Туз старше всех карт" in js


@pytest.fixture
async def game_api(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "miniapp_dev_user", 101)
    database = Database(tmp_path / "drunkard-api.db", start_balance=1000)
    await database.connect()
    await database.upsert_user(101, "alice", "Алиса")
    await database.upsert_user(202, "boris", "Борис")
    client = TestClient(TestServer(build_app(database)))
    await client.start_server()
    yield client, database
    await client.close()
    await database.close()


async def test_bot_play_starts_live_game_and_step_finishes_it(game_api, monkeypatch) -> None:
    client, database = game_api
    monkeypatch.setattr(
        drunkard,
        "new_game_state",
        lambda size: {
            "version": 1, "deck_size": size,
            "hands": {
                "player": [{"value": 14, "rank": "Т", "suit": "♠", "text": "Т♠"}],
                "opponent": [{"value": 13, "rank": "К", "suit": "♥", "text": "К♥"}],
            },
            "rounds": 0, "wars": 0, "last_battle": None, "winner": None, "tie_reason": None,
        },
    )

    state = await (await client.get("/api/state")).json()
    assert state["drunkard"] == {"incoming": [], "outgoing": [], "active": None, "last": None}
    response = await client.post(
        "/api/drunkard/play", json={"amount": 50, "deck_size": 24, "token": "api-play"}
    )
    payload = await response.json()

    assert response.status == 200 and payload["game"]["status"] == "active"
    assert payload["game"]["result"] is None and payload["game"]["your_outcome"] is None
    assert "hands" not in payload["game"]["game_state"]
    assert payload["balance"] == 950
    assert payload["drunkard"]["active"]["id"] == payload["game"]["id"]
    assert await database.casino_history(101, 10) == []

    step = await client.post(
        "/api/drunkard/step", json={"game_id": payload["game"]["id"], "token": "turn-1"}
    )
    finished = await step.json()
    assert step.status == 200 and finished["game"]["status"] == "finished"
    assert finished["game"]["your_outcome"] == "win"
    assert finished["balance"] == 1050
    assert finished["drunkard"]["active"] is None
    assert finished["drunkard"]["last"]["result"]["deck_size"] == 24
    assert await database.get_balance(101) == 1050


async def test_invite_accept_is_scoped_to_participant_and_idempotent(game_api, monkeypatch) -> None:
    client, database = game_api
    monkeypatch.setattr(
        drunkard,
        "new_game_state",
        lambda size: {
            "version": 1, "deck_size": size,
            "hands": {
                "player": [{"value": 14, "rank": "Т", "suit": "♣", "text": "Т♣"}],
                "opponent": [{"value": 6, "rank": "6", "suit": "♥", "text": "6♥"}],
            },
            "rounds": 0, "wars": 0, "last_battle": None, "winner": None, "tie_reason": None,
        },
    )
    challenge = await client.post(
        "/api/drunkard/invite",
        json={"opponent": "@boris", "amount": 10, "deck_size": 36, "token": "api-invite"},
    )
    invite = await challenge.json()
    assert challenge.status == 200 and invite["game"]["status"] == "invited"
    assert await database.get_balance(101) == 1000
    monkeypatch.setattr(settings, "miniapp_dev_user", 202)

    accepted = await client.post("/api/drunkard/accept", json={"game_id": invite["game"]["id"]})
    result = await accepted.json()
    assert accepted.status == 200 and result["game"]["status"] == "active"
    assert result["game"]["result"] is None
    assert result["balance"] == 990
    assert await database.get_balance(101) == 990
    assert await database.get_balance(202) == 990

    repeated = await client.post("/api/drunkard/accept", json={"game_id": invite["game"]["id"]})
    assert repeated.status == 200
    assert await database.get_balance(101) == 990
    assert await database.get_balance(202) == 990

    monkeypatch.setattr(settings, "miniapp_dev_user", 303)
    forbidden = await client.post(
        "/api/drunkard/step", json={"game_id": invite["game"]["id"], "token": "forbidden"}
    )
    assert forbidden.status == 400
    monkeypatch.setattr(settings, "miniapp_dev_user", 202)
    step = await client.post(
        "/api/drunkard/step", json={"game_id": invite["game"]["id"], "token": "api-turn"}
    )
    settled = await step.json()
    assert step.status == 200 and settled["game"]["your_outcome"] == "loss"
    assert await database.get_balance(101) == 1010
    assert await database.get_balance(202) == 990
