"""Мини-приложение: проверка initData и HTTP API."""

from __future__ import annotations

import time

import pytest
from aiohttp.test_utils import TestClient, TestServer

from app.config import settings
from app.db import Database
from app.services.miniapp import AuthError, sign_init_data, validate_init_data
from app.web.server import build_app

TOKEN = "123456:MINIAPP_TEST_TOKEN"
USER = {"id": 777, "first_name": "Антон", "username": "sap"}


@pytest.fixture
async def db(tmp_path):
    database = Database(tmp_path / "mini.db", start_balance=1000)
    await database.connect()
    await database.upsert_user(USER["id"], USER["username"], "Антон")
    yield database
    await database.close()


@pytest.fixture
async def match(db: Database):
    row, _ = await db.upsert_match(
        fonbet_id=555,
        tournament="Counter-Strike. IEM Пекин. Групповой этап. Bo3",
        team_a="Aurora",
        team_b="G2 Esports",
        start_time=int(time.time()) + 3600,
        odds_a=1.65,
        odds_b=2.25,
    )
    return row


@pytest.fixture
async def client(db: Database, monkeypatch):
    monkeypatch.setattr(settings, "bot_token", TOKEN)
    monkeypatch.setattr(settings, "miniapp_dev_user", USER["id"])
    app = build_app(db)
    async with TestClient(TestServer(app)) as test_client:
        yield test_client


# --------------------------------------------------------------------- initData
def test_init_data_valid() -> None:
    parsed = validate_init_data(sign_init_data(TOKEN, USER), TOKEN)
    assert parsed["user_id"] == USER["id"]
    assert parsed["full_name"] == "Антон"


def test_init_data_foreign_token() -> None:
    with pytest.raises(AuthError):
        validate_init_data(sign_init_data(TOKEN, USER), "999:OTHER")


def test_init_data_expired() -> None:
    stale = sign_init_data(TOKEN, USER, auth_date=int(time.time()) - 3 * 86400)
    with pytest.raises(AuthError, match="устарел"):
        validate_init_data(stale, TOKEN)


def test_init_data_tampered() -> None:
    data = sign_init_data(TOKEN, USER)
    with pytest.raises(AuthError):
        validate_init_data(data.replace("777", "778"), TOKEN)


# -------------------------------------------------------------------------- API
async def test_health(client: TestClient) -> None:
    response = await client.get("/health")
    assert response.status == 200
    assert (await response.json())["ok"] is True


async def test_state_returns_matches_and_balance(client: TestClient, match) -> None:
    response = await client.get("/api/state")
    payload = await response.json()

    assert payload["ok"] is True
    assert payload["user"]["balance"] == 1000
    assert payload["matches"][0]["team_a"] == "Aurora"
    assert payload["limits"]["min_bet"] == settings.min_bet


async def test_place_bet_over_http(client: TestClient, match, db: Database) -> None:
    response = await client.post(
        "/api/bet", json={"match_id": int(match["id"]), "side": "a", "amount": 200}
    )
    payload = await response.json()

    assert payload["ok"] is True
    assert payload["balance"] == 800
    assert payload["odds"] == pytest.approx(1.65)
    assert payload["potential"] == 330
    assert await db.get_balance(USER["id"]) == 800


async def test_place_express_over_http(client: TestClient, match, db: Database) -> None:
    second, _ = await db.upsert_match(
        fonbet_id=556,
        tournament="Counter-Strike. BLAST Rivals. Групповой этап. Bo3",
        team_a="MOUZ",
        team_b="Team Vitality",
        start_time=int(time.time()) + 7200,
        odds_a=2.00,
        odds_b=1.80,
    )
    response = await client.post(
        "/api/express",
        json={
            "amount": 250,
            "legs": [
                {"match_id": int(match["id"]), "side": "a"},
                {"match_id": int(second["id"]), "side": "a"},
            ],
        },
    )
    payload = await response.json()

    assert payload["ok"] is True
    assert payload["odds"] == pytest.approx(1.65 * 2.00)
    assert payload["balance"] == 750
    assert len(payload["legs"]) == 2


async def test_bad_bet_returns_json_error(client: TestClient, match) -> None:
    response = await client.post(
        "/api/bet", json={"match_id": int(match["id"]), "side": "a", "amount": 10_000}
    )
    payload = await response.json()

    assert response.status == 400
    assert payload["ok"] is False
    assert payload["error"]


async def test_bonus_endpoint(client: TestClient) -> None:
    response = await client.post("/api/bonus")
    payload = await response.json()
    assert payload["received"] is True
    assert payload["balance"] == 1000 + settings.daily_bonus


async def test_unauthorized_without_init_data(db: Database, monkeypatch) -> None:
    monkeypatch.setattr(settings, "bot_token", TOKEN)
    monkeypatch.setattr(settings, "miniapp_dev_user", 0)

    app = build_app(db)
    async with TestClient(TestServer(app)) as anonymous:
        response = await anonymous.get("/api/state")
        assert response.status == 401
        assert (await response.json())["ok"] is False


async def test_authorized_with_signed_init_data(db: Database, monkeypatch) -> None:
    monkeypatch.setattr(settings, "bot_token", TOKEN)
    monkeypatch.setattr(settings, "miniapp_dev_user", 0)
    init_data = sign_init_data(TOKEN, USER)

    app = build_app(db)
    async with TestClient(TestServer(app)) as signed:
        response = await signed.get("/api/state", headers={"X-Telegram-Init-Data": init_data})
        payload = await response.json()
        assert response.status == 200
        assert payload["user"]["id"] == USER["id"]


async def test_index_page_renders(client: TestClient) -> None:
    response = await client.get("/")
    html = await response.text()
    assert response.status == 200
    assert "CS2 BETS" in html
    assert "window.__BOOT__" in html


# --------------------------------------------------------------- история ставок
async def test_bets_endpoint_empty(client: TestClient) -> None:
    response = await client.get("/api/bets")
    assert response.status == 200
    body = await response.json()
    assert body["open"] == [] and body["settled"] == []
    assert body["counts"] == {"open": 0, "settled": 0, "won": 0, "lost": 0}


async def test_bets_endpoint_marks_open_bet(client: TestClient, match) -> None:
    placed = await client.post(
        "/api/bet", json={"match_id": int(match["id"]), "side": "a", "amount": 100}
    )
    assert placed.status == 200

    body = await (await client.get("/api/bets")).json()
    assert len(body["open"]) == 1 and body["settled"] == []
    item = body["open"][0]
    assert item["kind"] == "single"
    assert item["status"] == "open"
    assert item["team"] == "Aurora"
    assert item["stake"] == 100
    assert item["potential"] == 165
    assert "Aurora" in item["title"]


async def test_bets_endpoint_moves_settled_bet(client: TestClient, db: Database, match) -> None:
    await client.post("/api/bet", json={"match_id": int(match["id"]), "side": "a", "amount": 100})
    await db.settle_match(int(match["id"]), "a", source="manual")

    body = await (await client.get("/api/bets")).json()
    assert body["open"] == []
    assert len(body["settled"]) == 1
    item = body["settled"][0]
    assert item["result"] == "won"
    assert item["payout"] == 165
    assert body["counts"]["won"] == 1


async def test_bets_endpoint_express(client: TestClient, db: Database, match) -> None:
    other, _ = await db.upsert_match(
        fonbet_id=556,
        tournament="Counter-Strike. BLAST Rivals. Групповой этап. Bo3",
        team_a="MOUZ",
        team_b="Team Vitality",
        start_time=int(time.time()) + 7200,
        odds_a=2.0,
        odds_b=1.8,
    )
    response = await client.post(
        "/api/express",
        json={
            "amount": 50,
            "legs": [
                {"match_id": int(match["id"]), "side": "a"},
                {"match_id": int(other["id"]), "side": "a"},
            ],
        },
    )
    assert response.status == 200

    body = await (await client.get("/api/bets")).json()
    assert len(body["open"]) == 1
    item = body["open"][0]
    assert item["kind"] == "express"
    assert len(item["legs"]) == 2
    assert {leg["team"] for leg in item["legs"]} == {"Aurora", "MOUZ"}
    assert item["stake"] == 50
    assert item["potential"] == round(50 * 1.65 * 2.0)
