"""Возврат игроков: три дня тишины → сообщение и фрибет 300 🪙 на 24 часа.

Проверяем и сервис (кому пишем, как часто, что выдаём), и ставку по фрибету.
"""

from __future__ import annotations

import time

import pytest
from aiohttp.test_utils import TestClient, TestServer

from app.config import settings
from app.db import Database
from app.db.database import InvalidBet
from app.services import winback
from app.web.server import build_app

TOKEN = "123456:WINBACK_TOKEN"
USER = {"id": 5150, "first_name": "Пропавший", "username": "gone"}


@pytest.fixture
async def db(tmp_path):
    database = Database(tmp_path / "winback.db", start_balance=1000)
    await database.connect()
    await database.upsert_user(USER["id"], USER["username"], "Пропавший")
    yield database
    await database.close()


@pytest.fixture
async def client(db: Database, monkeypatch):
    monkeypatch.setattr(settings, "bot_token", TOKEN)
    monkeypatch.setattr(settings, "miniapp_dev_user", USER["id"])
    monkeypatch.setattr(settings, "winback_freebet", 300)
    async with TestClient(TestServer(build_app(db))) as test_client:
        yield test_client


class FakeBot:
    def __init__(self) -> None:
        self.sent: list[tuple[int, str]] = []

    async def send_message(self, chat_id, text, **kwargs):
        self.sent.append((chat_id, text))


async def silence(db: Database, days: int) -> None:
    """Сдвигаем «последний визит» в прошлое."""
    await db._write(
        "UPDATE users SET last_seen_at = ? WHERE user_id = ?",
        (int(time.time()) - days * 86400, USER["id"]),
    )


async def test_text_matches_request() -> None:
    assert "Давно тебя не было в «Доброй лудке»!" in winback.TEXT
    assert "фрибет 300 монет" in winback.TEXT
    assert "виртуальные монеты" in winback.TEXT


async def test_absent_three_days_gets_message_and_freebet(db: Database) -> None:
    await silence(db, 3)
    bot = FakeBot()

    assert await winback.notify_once(bot, db) == 1

    assert bot.sent[0][0] == USER["id"]
    assert "фрибет 300" in bot.sent[0][1]

    tickets = await db.tickets(USER["id"], kind="single")
    assert len(tickets) == 1
    assert int(tickets[0]["amount"]) == 300
    assert str(tickets[0]["source"]) == "возврат"
    # срок жизни — сутки
    assert int(tickets[0]["expires_at"]) - int(tickets[0]["created_at"]) == 24 * 3600


async def test_recent_player_is_left_alone(db: Database) -> None:
    await silence(db, 2)                      # был на днях — не дёргаем
    bot = FakeBot()

    assert await winback.notify_once(bot, db) == 0
    assert bot.sent == []
    assert await db.tickets(USER["id"]) == []


async def test_one_freebet_per_campaign(db: Database) -> None:
    await silence(db, 4)
    bot = FakeBot()
    assert await winback.notify_once(bot, db) == 1
    assert await winback.notify_once(bot, db) == 0      # второй раз молчим
    assert len(await db.tickets(USER["id"])) == 1


async def test_one_freebet_per_absence_even_after_a_month(db: Database) -> None:
    now = int(time.time())
    await silence(db, 5)
    bot = FakeBot()
    assert await winback.notify_once(bot, db, now=now) == 1

    # игрок не возвращался; сколько бы ни прошло, второй билет не начисляем
    assert await winback.notify_once(bot, db, now=now + 45 * 86400) == 0
    assert len(await db.tickets(USER["id"])) == 1


async def test_new_absence_after_real_return_can_get_new_freebet(db: Database) -> None:
    now = int(time.time())
    await silence(db, 5)
    bot = FakeBot()
    assert await winback.notify_once(bot, db, now=now) == 1

    # вернулся в приложение на следующий день, затем снова отсутствовал трое суток
    returned_at = now + 86400
    await db._write("UPDATE users SET last_seen_at = ? WHERE user_id = ?",
                    (returned_at, USER["id"]))
    next_absence_check = returned_at + 3 * 86400 + 1
    await db._write("UPDATE users SET last_seen_at = ? WHERE user_id = ?",
                    (next_absence_check - 3 * 86400, USER["id"]))
    assert await winback.notify_once(bot, db, now=next_absence_check) == 1
    assert len(bot.sent) == 2


async def test_freebet_expires_in_24_hours(db: Database) -> None:
    ticket_id = await db.add_ticket(
        USER["id"], 300, "возврат", kind="single", ttl_seconds=24 * 3600
    )
    # «переводим часы»: билет просрочен
    await db._write("UPDATE tickets SET expires_at = ? WHERE id = ?",
                    (int(time.time()) - 60, ticket_id))

    assert await db.tickets(USER["id"]) == []

    match, _ = await db.upsert_match(
        fonbet_id=7100, tournament="IEM", team_a="Aurora", team_b="G2 Esports",
        start_time=int(time.time()) + 3600, odds_a=2.0, odds_b=2.0,
    )
    with pytest.raises(InvalidBet):
        await db.place_bet(
            int(match["id"]), USER["id"], None, "a", 300, ticket_id=ticket_id
        )


async def test_freebet_covers_single_bet(client: TestClient, db: Database) -> None:
    match, _ = await db.upsert_match(
        fonbet_id=7101, tournament="IEM", team_a="Aurora", team_b="G2 Esports",
        start_time=int(time.time()) + 3600, odds_a=2.0, odds_b=2.0,
    )
    ticket_id = await db.add_ticket(
        USER["id"], 300, "возврат", kind="single", ttl_seconds=24 * 3600
    )

    state = await (await client.get("/api/state")).json()
    assert state["tickets"][0]["kind"] == "single"
    assert state["tickets"][0]["expires_at"] > int(time.time())

    wager = {
        "match_id": int(match["id"]),
        "side": "a",
        "amount": 300,
        "ticket_id": ticket_id,
        "token": "one-freebet-tap",
    }
    response = await client.post("/api/bet", json=wager)
    body = await response.json()
    retry = await client.post("/api/bet", json=wager)
    retry_body = await retry.json()

    assert response.status == retry.status == 200
    assert body["balance"] == retry_body["balance"] == 1000  # монеты не списаны
    assert await db.tickets(USER["id"]) == []                  # фрибет израсходован
    count = await db._fetchone("SELECT COUNT(*) AS n FROM bets WHERE user_id = ?", (USER["id"],))
    assert int(count["n"]) == 1


async def test_freebet_pays_out_like_a_normal_bet(db: Database) -> None:
    match, _ = await db.upsert_match(
        fonbet_id=7102, tournament="IEM", team_a="Aurora", team_b="G2 Esports",
        start_time=int(time.time()) + 3600, odds_a=2.0, odds_b=2.0,
    )
    ticket_id = await db.add_ticket(
        USER["id"], 300, "возврат", kind="single", ttl_seconds=24 * 3600
    )
    await db.place_bet(int(match["id"]), USER["id"], None, "a", 300, ticket_id=ticket_id)
    await db.settle_match(int(match["id"]), "a")

    assert await db.get_balance(USER["id"]) == 1000 + 600      # 300 × 2.00


async def test_freebet_amount_must_match(db: Database) -> None:
    match, _ = await db.upsert_match(
        fonbet_id=7103, tournament="IEM", team_a="Aurora", team_b="G2 Esports",
        start_time=int(time.time()) + 3600, odds_a=2.0, odds_b=2.0,
    )
    ticket_id = await db.add_ticket(
        USER["id"], 300, "возврат", kind="single", ttl_seconds=24 * 3600
    )

    with pytest.raises(InvalidBet):
        await db.place_bet(int(match["id"]), USER["id"], None, "a", 100, ticket_id=ticket_id)


async def test_express_ticket_is_not_a_freebet(db: Database) -> None:
    """Билет с колеса нельзя поставить на ординар."""
    match, _ = await db.upsert_match(
        fonbet_id=7104, tournament="IEM", team_a="Aurora", team_b="G2 Esports",
        start_time=int(time.time()) + 3600, odds_a=2.0, odds_b=2.0,
    )
    expr_ticket = await db.add_ticket(USER["id"], 300, "колесо", kind="express")

    with pytest.raises(InvalidBet):
        await db.place_bet(int(match["id"]), USER["id"], None, "a", 300, ticket_id=expr_ticket)


async def test_winback_marks_event_once(db: Database) -> None:
    await silence(db, 3)
    bot = FakeBot()
    assert await winback.notify_once(bot, db) == 1
    user = await db.get_user(USER["id"])
    assert await db.has_event(USER["id"], winback.EVENT, str(user["last_seen_at"]))


async def test_concurrent_winback_ticks_grant_only_once(db: Database) -> None:
    await silence(db, 4)
    bot = FakeBot()

    results = await __import__("asyncio").gather(
        winback.notify_once(bot, db),
        winback.notify_once(bot, db),
    )

    assert sum(results) == 1
    assert len(await db.tickets(USER["id"])) == 1


async def test_telegram_failure_does_not_issue_another_ticket(db: Database) -> None:
    await silence(db, 4)

    class BrokenBot(FakeBot):
        async def send_message(self, chat_id, text, **kwargs):
            raise RuntimeError("blocked")

    assert await winback.notify_once(BrokenBot(), db) == 0
    assert len(await db.tickets(USER["id"])) == 1
    assert await winback.notify_once(FakeBot(), db) == 0
    assert len(await db.tickets(USER["id"])) == 1


async def test_freebet_can_cover_map_ordinal(db: Database) -> None:
    match, _ = await db.upsert_match(
        fonbet_id=7105, tournament="IEM", team_a="Aurora", team_b="G2 Esports",
        start_time=int(time.time()) + 3600, odds_a=2.0, odds_b=2.0,
    )
    card, _ = await db.upsert_map(
        match_id=int(match["id"]), fonbet_id=8105, map_number=1, odds_a=2.0, odds_b=2.0,
    )
    ticket_id = await db.add_ticket(
        USER["id"], 300, "возврат", kind="single", ttl_seconds=24 * 3600
    )

    balance, odds = await db.place_map_bet(
        int(card["id"]), USER["id"], None, "a", 300, ticket_id=ticket_id
    )
    await db.settle_map(int(card["id"]), "a")

    assert odds == 2.0
    assert balance == 1000
    assert await db.get_balance(USER["id"]) == 1600


async def test_spent_ticket_cannot_be_used_again(db: Database) -> None:
    match, _ = await db.upsert_match(
        fonbet_id=7106, tournament="IEM", team_a="Aurora", team_b="G2 Esports",
        start_time=int(time.time()) + 3600, odds_a=2.0, odds_b=2.0,
    )
    ticket_id = await db.add_ticket(
        USER["id"], 300, "возврат", kind="single", ttl_seconds=24 * 3600
    )
    await db.place_bet(int(match["id"]), USER["id"], None, "a", 300, ticket_id=ticket_id)

    with pytest.raises(InvalidBet, match="уже использован"):
        await db.place_bet(int(match["id"]), USER["id"], None, "b", 300, ticket_id=ticket_id)


async def test_another_players_freebet_cannot_be_stolen(db: Database) -> None:
    match, _ = await db.upsert_match(
        fonbet_id=7107, tournament="IEM", team_a="Aurora", team_b="G2 Esports",
        start_time=int(time.time()) + 3600, odds_a=2.0, odds_b=2.0,
    )
    ticket_id = await db.add_ticket(99999, 300, "возврат", kind="single", ttl_seconds=24 * 3600)

    with pytest.raises(InvalidBet):
        await db.place_bet(int(match["id"]), USER["id"], None, "a", 300, ticket_id=ticket_id)


async def test_existing_live_single_ticket_blocks_another_grant(db: Database) -> None:
    await silence(db, 5)
    await db.add_ticket(USER["id"], 300, "ручной бонус", kind="single", ttl_seconds=24 * 3600)
    bot = FakeBot()

    assert await winback.notify_once(bot, db) == 0
    assert bot.sent == []
    assert len(await db.tickets(USER["id"])) == 1


async def test_admin_winback_grants_to_recent_user_and_suppresses_auto_grant(db: Database) -> None:
    now = int(time.time())

    ticket_id, status = await db.grant_manual_winback_ticket(
        USER["id"], 300, now=now, ttl_seconds=24 * 3600
    )

    assert status == "granted"
    assert ticket_id is not None
    tickets = await db.tickets(USER["id"], kind="single")
    assert len(tickets) == 1
    assert int(tickets[0]["amount"]) == 300
    assert str(tickets[0]["source"]) == "возврат (админ)"
    assert int(tickets[0]["expires_at"]) - int(tickets[0]["created_at"]) == 24 * 3600

    # Админская выдача отмечена как winback за этот период: автоцикл не продублирует её.
    assert await db.users_to_winback(now + 4 * 86400) == []


async def test_admin_winback_is_idempotent_for_same_activity_period(db: Database) -> None:
    now = int(time.time())
    ticket_id, status = await db.grant_manual_winback_ticket(USER["id"], 300, now=now)
    assert status == "granted"
    assert ticket_id is not None

    await db._write("UPDATE tickets SET used_at = ? WHERE id = ?", (now + 1, ticket_id))
    second_id, second_status = await db.grant_manual_winback_ticket(
        USER["id"], 300, now=now + 2
    )

    assert second_id is None
    assert second_status == "already_granted"
    assert len(await db.tickets(USER["id"], kind="single")) == 0


async def test_admin_winback_does_not_stack_an_active_single_ticket(db: Database) -> None:
    await db.add_ticket(USER["id"], 300, "предыдущий бонус", kind="single", ttl_seconds=24 * 3600)

    ticket_id, status = await db.grant_manual_winback_ticket(USER["id"], 300)

    assert ticket_id is None
    assert status == "active_ticket"
    assert len(await db.tickets(USER["id"], kind="single")) == 1
