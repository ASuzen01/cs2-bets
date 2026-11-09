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


async def test_state_exposes_drunkard_inbox_and_bot_play(game_api, monkeypatch) -> None:
    client, database = game_api
    monkeypatch.setattr(
        drunkard,
        "simulate_game",
        lambda size: {
            "deck_size": size, "winner": "player", "rounds": 12, "wars": 1,
            "cards_player": size, "cards_opponent": 0,
            "last_player_card": "Т♠", "last_opponent_card": "К♠", "tie_reason": None,
        },
    )

    state = await (await client.get("/api/state")).json()
    assert state["drunkard"] == {"incoming": [], "outgoing": [], "last": None}
    response = await client.post(
        "/api/drunkard/play", json={"amount": 50, "deck_size": 24, "token": "api-play"}
    )
    payload = await response.json()

    assert response.status == 200 and payload["game"]["your_outcome"] == "win"
    assert payload["balance"] == 1050
    assert payload["drunkard"]["last"]["result"]["deck_size"] == 24
    assert await database.get_balance(101) == 1050


async def test_invite_accept_is_scoped_to_participant_and_idempotent(game_api, monkeypatch) -> None:
    client, database = game_api
    monkeypatch.setattr(
        drunkard,
        "simulate_game",
        lambda size: {
            "deck_size": size, "winner": "opponent", "rounds": 17, "wars": 2,
            "cards_player": 0, "cards_opponent": size,
            "last_player_card": "6♥", "last_opponent_card": "Т♣", "tie_reason": None,
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
    assert accepted.status == 200 and result["game"]["your_outcome"] == "win"
    assert result["balance"] == 1010
    assert await database.get_balance(101) == 990
    assert await database.get_balance(202) == 1010

    # Принятие/отклонение нельзя повторно применять к завершённой партии.
    repeated = await client.post("/api/drunkard/accept", json={"game_id": invite["game"]["id"]})
    assert repeated.status == 200
    assert await database.get_balance(101) == 990
    assert await database.get_balance(202) == 1010
