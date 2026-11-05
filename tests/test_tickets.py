"""Билет в экспресс: приз колеса, которым оплачивают ставку.

Проверяем весь путь игрока: выиграл билет на колесе → увидел его в профиле →
оплатил экспресс → билет исчез, монеты со счёта не ушли → экспресс зашёл, выплата
пришла целиком на баланс.
"""

from __future__ import annotations

import time

import pytest
from aiohttp.test_utils import TestClient, TestServer

from app.config import settings
from app.db import Database
from app.db.database import InvalidBet
from app.web.server import build_app

TOKEN = "123456:TICKET_TOKEN"
USER = {"id": 4321, "first_name": "Игрок", "username": "player"}


@pytest.fixture
async def db(tmp_path):
    database = Database(tmp_path / "tickets.db", start_balance=1000)
    await database.connect()
    await database.upsert_user(USER["id"], USER["username"], "Игрок")
    yield database
    await database.close()


@pytest.fixture
async def client(db: Database, monkeypatch):
    monkeypatch.setattr(settings, "bot_token", TOKEN)
    monkeypatch.setattr(settings, "miniapp_dev_user", USER["id"])
    monkeypatch.setattr(settings, "wheel_ticket_value", 100)
    async with TestClient(TestServer(build_app(db))) as test_client:
        yield test_client


async def two_matches(db: Database, *, fonbet_id: int = 9100) -> list[dict]:
    day = int(time.time()) + 3600
    first, _ = await db.upsert_match(
        fonbet_id=fonbet_id, tournament="IEM", team_a="Aurora", team_b="G2 Esports",
        start_time=day, odds_a=2.0, odds_b=2.0,
    )
    second, _ = await db.upsert_match(
        fonbet_id=fonbet_id + 1, tournament="IEM", team_a="Vitality", team_b="Spirit",
        start_time=day, odds_a=2.0, odds_b=2.0,
    )
    return [first, second]


async def test_ticket_appears_in_state(client: TestClient, db: Database) -> None:
    await db.add_ticket(USER["id"], 100, "колесо")

    body = await (await client.get("/api/state")).json()

    assert body["tickets"] == [
        {"id": 1, "amount": 100, "kind": "express", "expires_at": None}
    ]
    assert body["bonus"]["wheel"]["ticket"] == 100


async def test_express_paid_by_ticket_keeps_balance(client: TestClient, db: Database) -> None:
    matches = await two_matches(db)
    ticket_id = await db.add_ticket(USER["id"], 100, "колесо")

    response = await client.post(
        "/api/express",
        json={
            "amount": 100,
            "ticket_id": ticket_id,
            "legs": [
                {"match_id": int(matches[0]["id"]), "side": "a"},
                {"match_id": int(matches[1]["id"]), "side": "a"},
            ],
        },
    )
    body = await response.json()

    assert response.status == 200
    assert body["balance"] == 1000                     # монеты не тронуты
    assert await db.tickets(USER["id"]) == []           # билет израсходован

    opened = await db.user_open_expresses(USER["id"])
    assert int(opened[0]["stake"]) == 100


async def test_ticket_is_not_reusable(client: TestClient, db: Database) -> None:
    matches = await two_matches(db, fonbet_id=9200)
    ticket_id = await db.add_ticket(USER["id"], 100, "колесо")
    legs = [
        {"match_id": int(matches[0]["id"]), "side": "a"},
        {"match_id": int(matches[1]["id"]), "side": "b"},
    ]
    assert (await client.post(
        "/api/express", json={"amount": 100, "ticket_id": ticket_id, "legs": legs},
    )).status == 200

    again = await client.post(
        "/api/express", json={"amount": 100, "ticket_id": ticket_id, "legs": legs},
    )
    assert again.status == 400
    assert "использован" in (await again.json())["error"]


async def test_ticket_amount_must_match_stake(db: Database) -> None:
    matches = await two_matches(db, fonbet_id=9300)
    ticket_id = await db.add_ticket(USER["id"], 100, "колесо")

    with pytest.raises(InvalidBet):
        await db.place_express(
            USER["id"], None, 50,
            [
                {"match_id": int(matches[0]["id"]), "side": "a"},
                {"match_id": int(matches[1]["id"]), "side": "a"},
            ],
            ticket_id=ticket_id,
        )


async def test_foreign_ticket_is_rejected(db: Database) -> None:
    matches = await two_matches(db, fonbet_id=9400)
    await db.upsert_user(999, "other", "Другой")
    ticket_id = await db.add_ticket(999, 100, "колесо")

    with pytest.raises(InvalidBet):
        await db.place_express(
            USER["id"], None, 100,
            [
                {"match_id": int(matches[0]["id"]), "side": "a"},
                {"match_id": int(matches[1]["id"]), "side": "a"},
            ],
            ticket_id=ticket_id,
        )

    # чужой билет не сгорел
    assert len(await db.tickets(999)) == 1


async def test_winning_ticket_express_pays_full_payout(db: Database) -> None:
    matches = await two_matches(db, fonbet_id=9500)
    ticket_id = await db.add_ticket(USER["id"], 100, "колесо")
    express_id, odds, balance = await db.place_express(
        USER["id"], None, 100,
        [
            {"match_id": int(matches[0]["id"]), "side": "a"},
            {"match_id": int(matches[1]["id"]), "side": "a"},
        ],
        ticket_id=ticket_id,
    )
    assert balance == 1000

    for match in matches:
        await db.settle_match(int(match["id"]), "a")

    assert await db.get_balance(USER["id"]) == 1000 + round(100 * odds)
    settled = await db.get_express(express_id)
    assert settled["status"] == "won"


async def test_wheel_ticket_prize_creates_ticket(db: Database) -> None:
    """Сектор-билет не начисляет монеты, но кладёт билет в профиль."""
    from app.services.bonus import WHEEL_TICKET

    ticket_id = await db.add_ticket(USER["id"], settings.wheel_ticket_value, "колесо")
    assert ticket_id > 0
    assert WHEEL_TICKET < 0                       # монет за него не платят
    rows = await db.tickets(USER["id"])
    assert [int(row["amount"]) for row in rows] == [settings.wheel_ticket_value]
    assert str(rows[0]["source"]) == "колесо"


async def test_wheel_ticket_prize_wires_to_profile(client: TestClient, db: Database, monkeypatch) -> None:
    """Выпал сектор-билет → монет не начислили, зато в профиле появился билет."""
    monkeypatch.setattr("app.web.server.spin", lambda layout: (7, -1))

    response = await client.post("/api/wheel", json={"paid": False})
    body = await response.json()

    assert response.status == 200
    assert body["spun"] is True
    assert body["prize"] == 0                       # монет за билет не дают
    assert body["ticket"] == {"id": 1, "amount": 100}
    assert body["balance"] == 1000                  # баланс не изменился

    state = await (await client.get("/api/state")).json()
    assert [item["amount"] for item in state["tickets"]] == [100]
    assert state["tickets"][0]["kind"] == "express"
