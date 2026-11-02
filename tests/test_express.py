"""Экспрессы: перемножение кэфов, проигрыш ноги, возвраты, лимиты."""

from __future__ import annotations

import time

import pytest

from app.config import settings
from app.db import Database, InsufficientFunds, InvalidBet, MatchNotOpen


@pytest.fixture
async def db(tmp_path):
    database = Database(tmp_path / "test.db", start_balance=1000)
    await database.connect()
    for user_id in (1, 2):
        await database.upsert_user(user_id, f"user{user_id}", f"User {user_id}")
    yield database
    await database.close()


async def make_match(db: Database, fonbet_id: int, *, odds_a=1.65, odds_b=2.25, start_in: int = 3600):
    row, _ = await db.upsert_match(
        fonbet_id=fonbet_id,
        tournament="Counter-Strike. IEM Пекин. Групповой этап. Bo3",
        team_a=f"TeamA{fonbet_id}",
        team_b=f"TeamB{fonbet_id}",
        start_time=int(time.time()) + start_in,
        odds_a=odds_a,
        odds_b=odds_b,
    )
    return row


async def test_express_wins_when_all_legs_win(db):
    m1 = await make_match(db, 1, odds_a=1.65)
    m2 = await make_match(db, 2, odds_a=2.0)

    express_id, odds, balance = await db.place_express(
        1, -100, 100, [{"match_id": int(m1["id"]), "side": "a"}, {"match_id": int(m2["id"]), "side": "a"}]
    )
    assert odds == pytest.approx(3.3)
    assert balance == 900

    r1 = await db.settle_match(int(m1["id"]), "a")
    assert r1["expresses"] == []  # вторая нога ещё не рассчитана
    express = await db.get_express(express_id)
    assert express["status"] == "open"
    assert await db.get_balance(1) == 900

    r2 = await db.settle_match(int(m2["id"]), "a")
    assert len(r2["expresses"]) == 1
    assert r2["expresses"][0]["status"] == "won"
    assert r2["expresses"][0]["payout"] == 330  # 100 × 1.65 × 2.0
    assert await db.get_balance(1) == 900 + 330

    express = await db.get_express(express_id)
    assert express["status"] == "won" and express["payout"] == 330


async def test_express_loses_on_first_failed_leg(db):
    m1 = await make_match(db, 1)
    m2 = await make_match(db, 2)
    express_id, _, _ = await db.place_express(
        1, -100, 200,
        [{"match_id": int(m1["id"]), "side": "a"}, {"match_id": int(m2["id"]), "side": "a"}],
    )

    result = await db.settle_match(int(m1["id"]), "b")  # первая нога не зашла
    assert len(result["expresses"]) == 1
    assert result["expresses"][0]["status"] == "lost"
    assert result["expresses"][0]["payout"] == 0
    assert "TeamA1" in result["expresses"][0]["lost_leg"]
    assert await db.get_balance(1) == 800

    # вторая нога уже ни на что не влияет
    result2 = await db.settle_match(int(m2["id"]), "a")
    assert result2["expresses"] == []
    assert (await db.get_express(express_id))["payout"] == 0


async def test_void_leg_is_counted_as_odds_one(db):
    m1 = await make_match(db, 1, odds_a=2.0)
    m2 = await make_match(db, 2, odds_a=1.5)
    express_id, _, _ = await db.place_express(
        1, -100, 100,
        [{"match_id": int(m1["id"]), "side": "a"}, {"match_id": int(m2["id"]), "side": "a"}],
    )

    # первый матч отменили — его кэф становится 1.0
    await db.cancel_match(int(m1["id"]))
    assert (await db.get_express(express_id))["status"] == "open"

    result = await db.settle_match(int(m2["id"]), "a")
    express = result["expresses"][0]
    assert express["status"] == "won"
    assert express["payout"] == 150  # 100 × 1.0 × 1.5
    assert await db.get_balance(1) == 900 + 150


async def test_all_legs_void_refunds_stake(db):
    m1 = await make_match(db, 1)
    m2 = await make_match(db, 2)
    express_id, _, _ = await db.place_express(
        1, -100, 300,
        [{"match_id": int(m1["id"]), "side": "a"}, {"match_id": int(m2["id"]), "side": "b"}],
    )

    await db.cancel_match(int(m1["id"]))
    result = await db.cancel_match(int(m2["id"]))
    assert result["expresses"][0]["status"] == "void"
    assert result["expresses"][0]["payout"] == 300
    assert await db.get_balance(1) == 1000
    assert (await db.get_express(express_id))["status"] == "void"


async def test_express_requires_two_legs(db):
    m1 = await make_match(db, 1)
    with pytest.raises(InvalidBet):
        await db.place_express(1, -100, 100, [{"match_id": int(m1["id"]), "side": "a"}])


async def test_duplicate_match_in_express_rejected(db):
    m1 = await make_match(db, 1)
    with pytest.raises(InvalidBet):
        await db.place_express(
            1, -100, 100,
            [{"match_id": int(m1["id"]), "side": "a"}, {"match_id": int(m1["id"]), "side": "b"}],
        )


async def test_express_odds_cap(db):
    m1 = await make_match(db, 1, odds_a=3.0)
    m2 = await make_match(db, 2, odds_a=3.0)
    m3 = await make_match(db, 3, odds_a=3.0)
    with pytest.raises(InvalidBet):
        await db.place_express(
            1, -100, 100,
            [
                {"match_id": int(m1["id"]), "side": "a"},
                {"match_id": int(m2["id"]), "side": "a"},
                {"match_id": int(m3["id"]), "side": "a"},
            ],
            max_odds=20.0,
        )


async def test_express_insufficient_funds(db):
    m1 = await make_match(db, 1)
    m2 = await make_match(db, 2)
    with pytest.raises(InsufficientFunds):
        await db.place_express(
            1, -100, 5000,
            [{"match_id": int(m1["id"]), "side": "a"}, {"match_id": int(m2["id"]), "side": "a"}],
        )
    assert await db.get_balance(1) == 1000


async def test_express_legs_must_be_open(db, monkeypatch):
    """С выключенными live-ставками нога экспресса не может быть в идущем матче."""
    monkeypatch.setattr(settings, "live_betting", False)
    m1 = await make_match(db, 1, start_in=-60)
    m2 = await make_match(db, 2)
    with pytest.raises(MatchNotOpen):
        await db.place_express(
            1, -100, 100,
            [{"match_id": int(m1["id"]), "side": "a"}, {"match_id": int(m2["id"]), "side": "a"}],
        )


async def test_express_can_take_live_leg(db):
    """А с включёнными — можно собрать экспресс из матча в эфире."""
    m1 = await make_match(db, 1, start_in=-600)
    await db.set_status(int(m1["id"]), "live")
    m2 = await make_match(db, 2)
    express_id, odds, balance = await db.place_express(
        1, -100, 100,
        [{"match_id": int(m1["id"]), "side": "a"}, {"match_id": int(m2["id"]), "side": "a"}],
    )
    assert express_id > 0 and odds > 1 and balance == 900


async def test_single_and_express_on_same_match(db):
    m1 = await make_match(db, 1, odds_a=2.0)
    m2 = await make_match(db, 2, odds_a=2.0)

    await db.place_bet(int(m1["id"]), 1, -100, "a", 100)
    await db.place_express(
        1, -100, 100,
        [{"match_id": int(m1["id"]), "side": "a"}, {"match_id": int(m2["id"]), "side": "a"}],
    )
    assert await db.get_balance(1) == 800

    await db.settle_match(int(m1["id"]), "a")
    assert await db.get_balance(1) == 800 + 200  # одиночная ставка 100 × 2.0

    await db.settle_match(int(m2["id"]), "a")
    assert await db.get_balance(1) == 1000 + 400  # + экспресс 100 × 2.0 × 2.0
