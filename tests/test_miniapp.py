"""Мини-приложение: проверка initData и HTTP API."""

from __future__ import annotations

import time

import pytest
from aiohttp.test_utils import TestClient, TestServer

from app.config import settings
from app.db import Database
from app.services import games
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


async def test_lines_endpoint_returns_only_competition_data(client: TestClient, match) -> None:
    response = await client.get("/api/lines")
    payload = await response.json()

    assert response.status == 200
    assert payload["ok"] is True
    assert payload["matches"][0]["team_a"] == "Aurora"
    assert payload["live"] == []
    assert "user" not in payload
    assert "casino" not in payload


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
    assert "Добрая лудка" in html
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


def test_payload_survives_match_without_odds() -> None:
    """Fonbet снимает кэфы в лайве — карточка обязана отрисоваться, а не падать."""
    from app.web.payloads import match_payload

    row = {
        "id": 1, "tournament": "Counter-Strike. IEM. Bo3", "team_a": "A", "team_b": "B",
        "odds_a": None, "odds_b": None, "odds_x": None, "start_time": 1_800_000_000,
        "status": "live", "score_a": None, "score_b": None, "sport": "cs2",
    }
    payload = match_payload(row)
    assert payload["odds_a"] is None and payload["odds_b"] is None
    assert payload["status"] == "live"


# ------------------------------------------------- регрессии из дебага (задача 50)
async def test_garbage_amount_is_400_not_500(client: TestClient, match) -> None:
    """«abc» вместо суммы — понятная ошибка, а не 500 с трейсбеком."""
    response = await client.post(
        "/api/bet", json={"match_id": int(match["id"]), "side": "a", "amount": "abc"}
    )
    payload = await response.json()

    assert response.status == 400
    assert payload["ok"] is False
    assert "Сумма" in payload["error"]


async def test_garbage_express_amount_is_400(client: TestClient, match) -> None:
    response = await client.post(
        "/api/express",
        json={
            "amount": "abc",
            "legs": [
                {"match_id": int(match["id"]), "side": "a"},
                {"match_id": int(match["id"]) + 1, "side": "b"},
            ],
        },
    )
    assert response.status == 400


async def test_map_bet_from_foreign_match_rejected(
    client: TestClient, match, db: Database
) -> None:
    """Карту нельзя подшить к чужому матчу: иначе в истории ставка висит не там."""
    other, _ = await db.upsert_match(
        fonbet_id=556,
        tournament="Counter-Strike. IEM Пекин. Групповой этап. Bo3",
        team_a="Vitality",
        team_b="Spirit",
        start_time=int(time.time()) + 3600,
        odds_a=1.9,
        odds_b=1.9,
    )
    card, _ = await db.upsert_map(
        match_id=int(other["id"]), fonbet_id=90_556, map_number=1, odds_a=1.9, odds_b=1.9
    )

    response = await client.post(
        "/api/bet",
        json={
            "match_id": int(match["id"]),
            "market": "map",
            "map_id": int(card["id"]),
            "side": "a",
            "amount": 100,
        },
    )

    assert response.status == 400
    assert "другого матча" in (await response.json())["error"]


async def test_active_bets_include_football(client: TestClient, match, db: Database) -> None:
    """«Ставок в игре» на главном экране считает и футбольные ставки."""
    football, _ = await db.upsert_match(
        fonbet_id=557,
        tournament="Лига Чемпионов УЕФА. Общий этап",
        team_a="Лейпциг",
        team_b="Манчестер Сити",
        start_time=int(time.time()) + 3600,
        odds_a=5.3,
        odds_x=4.9,
        odds_b=1.53,
        sport="football",
    )
    await db.place_bet(int(match["id"]), USER["id"], None, "a", 100)
    await db.place_football_bet(int(football["id"]), USER["id"], None, "total", "over:2.5", 100)

    payload = await (await client.get("/api/state")).json()
    assert len(payload["bets"]) == 2
    # и исход CS2, и рынок футбола подписаны по-человечески
    assert {b["label"] for b in payload["bets"]} == {"Aurora", "ТБ 2.5"}


async def test_map_bet_retry_with_same_token_charges_once(
    client: TestClient, match, db: Database
) -> None:
    card, _ = await db.upsert_map(
        match_id=int(match["id"]), fonbet_id=55_555, map_number=1, odds_a=2.0, odds_b=2.0
    )
    body = {
        "match_id": int(match["id"]), "market": "map", "map_id": int(card["id"]),
        "side": "a", "amount": 100, "token": "map-api-once",
    }

    first = await client.post("/api/bet", json=body)
    repeat = await client.post("/api/bet", json=body)

    assert first.status == repeat.status == 200
    assert (await first.json())["balance"] == 900
    assert (await repeat.json())["balance"] == 900
    assert await db.get_balance(USER["id"]) == 900
    assert len(await db.user_bets_all(USER["id"])) == 1


async def test_football_first_bet_pays_referral_and_retry_is_idempotent(
    client: TestClient, db: Database
) -> None:
    inviter_id = 778
    await db.upsert_user(inviter_id, "inviter", "Пригласивший")
    attached, error = await db.attach_referral(USER["id"], inviter_id)
    assert attached and not error
    football, _ = await db.upsert_match(
        fonbet_id=558,
        tournament="Лига Чемпионов УЕФА. Общий этап",
        team_a="Лейпциг",
        team_b="Манчестер Сити",
        start_time=int(time.time()) + 3600,
        odds_a=5.3,
        odds_x=4.9,
        odds_b=1.53,
        sport="football",
    )
    body = {
        "match_id": int(football["id"]), "side": "f:total:over:2.5",
        "amount": 100, "token": "football-api-once",
    }

    first = await client.post("/api/bet", json=body)
    repeat = await client.post("/api/bet", json=body)

    assert first.status == repeat.status == 200
    assert isinstance((await first.json())["new_achievements"], list)
    assert len(await db.user_bets_all(USER["id"])) == 1
    assert await db.get_balance(USER["id"]) == 1000 - 100 + settings.referral_welcome
    assert await db.get_balance(inviter_id) == 1000 + settings.referral_bonus
    referral = await db._fetchone("SELECT paid_at FROM referrals WHERE invited_id = ?", (USER["id"],))
    assert referral["paid_at"] is not None


async def test_casino_retry_returns_original_random_result(
    client: TestClient, db: Database, monkeypatch
) -> None:
    outcomes = iter((0, 1))
    monkeypatch.setattr(games, "spin_roulette", lambda: next(outcomes))
    body = {"game": "roulette", "selection": "red", "amount": 100, "token": "roulette-once"}

    first = await client.post("/api/casino/play", json=body)
    repeat = await client.post("/api/casino/play", json=body)
    first_payload, repeat_payload = await first.json(), await repeat.json()

    assert first_payload["number"] == repeat_payload["number"] == 0
    assert first_payload["win"] == repeat_payload["win"] is False
    assert first_payload["payout"] == repeat_payload["payout"] == 0
    assert await db.get_balance(USER["id"]) == 900
    assert len(await db.casino_history(USER["id"])) == 1


async def test_oversized_integer_is_rejected_as_client_error(
    client: TestClient, match
) -> None:
    response = await client.post(
        "/api/bet",
        json={"match_id": int(match["id"]), "side": "a", "amount": 10**100},
    )
    assert response.status == 400
    assert "SQLite" not in (await response.text())


async def test_unexpected_errors_do_not_leak_exception_details(
    client: TestClient, match, db: Database, monkeypatch
) -> None:
    async def fail(*args, **kwargs):
        raise RuntimeError("secret backend detail")

    monkeypatch.setattr(db, "place_bet", fail)
    response = await client.post(
        "/api/bet", json={"match_id": int(match["id"]), "side": "a", "amount": 100}
    )
    body = await response.text()

    assert response.status == 500
    assert "secret backend detail" not in body
    assert "Внутренняя ошибка" in (await response.json())["error"]


async def test_ticket_funded_bet_cannot_be_cashed_out(
    client: TestClient, match, db: Database
) -> None:
    ticket_id = await db.add_ticket(
        USER["id"], 300, "возврат", kind="single", ttl_seconds=86400
    )
    before = await db.get_balance(USER["id"])
    placed = await client.post(
        "/api/bet",
        json={
            "match_id": int(match["id"]), "side": "a", "amount": 300,
            "ticket_id": ticket_id, "token": "ticket-cashout-check",
        },
    )
    assert placed.status == 200
    assert (await placed.json())["balance"] == before

    bets = await db.user_bets_all(USER["id"])
    cashout = await client.post("/api/cashout", json={"bet_id": int(bets[0]["id"])})
    payload = await cashout.json()

    assert cashout.status == 400
    assert "фрибетом" in payload["error"].lower()
    assert await db.get_balance(USER["id"]) == before
    refreshed = await db.user_bets_all(USER["id"])
    assert refreshed[0]["cashed_out"] == 0


async def test_referral_cannot_be_attached_after_active_ladder_wager(
    client: TestClient, db: Database
) -> None:
    inviter_id = 779
    await db.upsert_user(inviter_id, "inviter", "Пригласивший")
    start = await client.post("/api/ladder/start", json={"amount": 10})
    assert start.status == 200

    attached = await client.post("/api/referral", json={"code": f"ref_{inviter_id}"})
    payload = await attached.json()

    assert attached.status == 400
    assert "первой ставки" in payload["error"].lower()
    assert await db._fetchone(
        "SELECT 1 FROM referrals WHERE invited_id = ?", (USER["id"],)
    ) is None


async def test_tease_requires_daily_bonus_and_ten_server_taps(
    client: TestClient, db: Database
) -> None:
    bypass = await client.post("/api/tease")
    assert bypass.status == 400
    assert await db.get_balance(USER["id"]) == 1000

    daily = await client.post("/api/bonus")
    assert (await daily.json())["received"] is True
    first = await client.post("/api/tease")
    first_payload = await first.json()
    assert first_payload["claimed"] is False
    assert first_payload["progress"] == 1

    for _ in range(8):
        response = await client.post("/api/tease")
        assert (await response.json())["claimed"] is False
    final = await client.post("/api/tease")
    final_payload = await final.json()

    assert final_payload["claimed"] is True
    assert final_payload["amount"] == 50
    assert await db.get_balance(USER["id"]) == 1000 + settings.daily_bonus + 50
