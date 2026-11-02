"""Экономика бота: фиксированные кэфы, выплаты, возвраты, бонусы."""

from __future__ import annotations

import time

import pytest

from app.db import Database, InsufficientFunds, MatchAlreadySettled, MatchNotOpen


@pytest.fixture
async def db(tmp_path):
    database = Database(tmp_path / "test.db", start_balance=1000)
    await database.connect()
    for user_id in (1, 2, 3):
        await database.upsert_user(user_id, f"user{user_id}", f"User {user_id}")
    yield database
    await database.close()


async def _match(db: Database, *, start_in: int = 3600, odds_a: float = 1.65, odds_b: float = 2.25):
    row, created = await db.upsert_match(
        fonbet_id=123456,
        tournament="Counter-Strike. IEM Пекин. Групповой этап. Bo3",
        team_a="Aurora",
        team_b="G2 Esports",
        start_time=int(time.time()) + start_in,
        odds_a=odds_a,
        odds_b=odds_b,
    )
    assert created is True
    return row


async def test_start_balance(db):
    assert await db.get_balance(1) == 1000


async def test_fixed_odds_win(db):
    match = await _match(db)
    balance, odds = await db.place_bet(int(match["id"]), 1, -100, "a", 200)

    assert odds == 1.65
    assert balance == 800

    result = await db.settle_match(int(match["id"]), "a")
    assert result["paid"] == 330  # 200 × 1.65
    assert result["winners"] == 1
    assert result["team"] == "Aurora"
    assert await db.get_balance(1) == 1130  # 800 + 330


async def test_fixed_odds_loss(db):
    match = await _match(db)
    await db.place_bet(int(match["id"]), 1, -100, "a", 200)
    await db.settle_match(int(match["id"]), "b")

    assert await db.get_balance(1) == 800  # ставка сгорела
    bets = await db.match_bets(int(match["id"]))
    assert bets[0]["payout"] == 0


async def test_odds_are_locked_at_bet_time(db):
    match = await _match(db, odds_a=1.65)
    await db.place_bet(int(match["id"]), 1, -100, "a", 100)

    # линия сдвинулась
    await db.upsert_match(
        fonbet_id=123456,
        tournament="Counter-Strike. IEM Пекин. Групповой этап. Bo3",
        team_a="Aurora",
        team_b="G2 Esports",
        start_time=int(time.time()) + 3600,
        odds_a=1.40,
        odds_b=2.90,
    )

    await db.settle_match(int(match["id"]), "a")
    assert await db.get_balance(1) == 900 + 165  # по старому кэфу 1.65


async def test_cannot_bet_more_than_balance(db):
    match = await _match(db)
    with pytest.raises(InsufficientFunds):
        await db.place_bet(int(match["id"]), 1, -100, "a", 5000)
    assert await db.get_balance(1) == 1000


async def test_bets_blocked_after_start(db):
    match = await _match(db, start_in=-60)  # матч уже начался
    with pytest.raises(MatchNotOpen):
        await db.place_bet(int(match["id"]), 1, -100, "a", 100)


async def test_settle_twice_forbidden(db):
    match = await _match(db)
    await db.place_bet(int(match["id"]), 1, -100, "a", 100)
    await db.settle_match(int(match["id"]), "a")
    with pytest.raises(MatchAlreadySettled):
        await db.settle_match(int(match["id"]), "b")


async def test_void_returns_stakes(db):
    match = await _match(db)
    await db.place_bet(int(match["id"]), 1, -100, "a", 300)
    await db.place_bet(int(match["id"]), 2, -100, "b", 200)

    result = await db.cancel_match(int(match["id"]))
    assert result["refunded"] == 500
    assert await db.get_balance(1) == 1000
    assert await db.get_balance(2) == 1000


async def test_close_started_matches(db):
    await _match(db, start_in=-10)
    later = await db.upsert_match(
        fonbet_id=999, tournament="Counter-Strike. IEM. Bo3", team_a="A", team_b="B",
        start_time=int(time.time()) + 7200, odds_a=2.0, odds_b=1.8,
    )
    closed = await db.close_started_matches()
    assert len(closed) == 1
    assert len(await db.open_matches()) == 1
    assert later[0]["fonbet_id"] == 999


async def test_daily_bonus_cooldown(db):
    ok, _, balance = await db.daily_bonus(1, 250, 24 * 3600)
    assert ok is True and balance == 1250

    ok, wait, balance = await db.daily_bonus(1, 250, 24 * 3600)
    assert ok is False and wait > 0 and balance == 1250


async def test_upsert_match_updates_odds_only_for_open(db):
    match = await _match(db)
    row, created = await db.upsert_match(
        fonbet_id=123456, tournament="Counter-Strike. IEM Пекин. Групповой этап. Bo3",
        team_a="Aurora", team_b="G2 Esports", start_time=int(time.time()) + 3600,
        odds_a=1.5, odds_b=2.6,
    )
    assert created is False
    assert row["odds_a"] == 1.5

    await db.set_status(int(match["id"]), "live")
    row2, _ = await db.upsert_match(
        fonbet_id=123456, tournament="Counter-Strike. IEM Пекин. Групповой этап. Bo3",
        team_a="Aurora", team_b="G2 Esports", start_time=int(time.time()) + 3600,
        odds_a=1.2, odds_b=4.0,
    )
    assert row2["odds_a"] == 1.5  # у закрытого матча кэфы не трогаем


async def test_chat_autopost_toggle(db):
    await db.register_chat(-1001, "Друзья")
    assert [int(r["chat_id"]) for r in await db.list_autopost_chats()] == [-1001]

    await db.set_autopost(-1001, False)
    assert await db.list_autopost_chats() == []
