"""Защита от «ставки на уже идущий матч» и от двойного списания по двойному тапу."""

from __future__ import annotations

import time

import pytest

from app.db import Database, MatchNotOpen


@pytest.fixture
async def db(tmp_path):
    database = Database(tmp_path / "guards.db", start_balance=1000)
    await database.connect()
    await database.upsert_user(1, "user1", "User 1")
    yield database
    await database.close()


async def _match(db: Database, *, start_in: int) -> dict:
    row, _ = await db.upsert_match(
        fonbet_id=4242,
        tournament="Counter-Strike. IEM. Групповой этап. Bo3",
        team_a="Aurora",
        team_b="G2 Esports",
        start_time=int(time.time()) + start_in,
        odds_a=2.0,
        odds_b=2.0,
    )
    return row


async def test_bet_on_started_match_is_refused(db: Database) -> None:
    """Фид не успел перевести матч в live: предматчевый кэф на идущую игру не принимаем."""
    match = await _match(db, start_in=-120)

    with pytest.raises(MatchNotOpen):
        await db.place_bet(int(match["id"]), 1, None, "a", 100)

    assert await db.get_balance(1) == 1000


async def test_bet_on_live_match_is_accepted(db: Database) -> None:
    """live_betting включён: матч в эфире — ставить можно."""
    match = await _match(db, start_in=-120)
    await db.set_status(int(match["id"]), "live")

    balance, odds = await db.place_bet(int(match["id"]), 1, None, "a", 100)

    assert balance == 900
    assert odds == 2.0


async def test_double_tap_charges_once(db: Database) -> None:
    """Два одинаковых тапа с одним токеном — одна ставка и одно списание."""
    match = await _match(db, start_in=3600)
    match_id = int(match["id"])

    first = await db.place_bet(match_id, 1, None, "a", 100, token="tap-1")
    second = await db.place_bet(match_id, 1, None, "a", 100, token="tap-1")

    assert first == second == (900, 2.0)
    assert await db.get_balance(1) == 900
    assert len(await db.user_bets_all(1)) == 1

    # другой тап — уже новая ставка
    third = await db.place_bet(match_id, 1, None, "a", 100, token="tap-2")
    assert third == (800, 2.0)


async def test_double_tap_in_casino_charges_once(db: Database) -> None:
    """То же для рулетки: повтор с тем же токеном не запускает второй раунд."""
    kwargs = {
        "game": "roulette",
        "selection": "red",
        "label": "Красное",
        "amount": 100,
        "odds": 2.0,
        "outcome": "17 — чёрное",
        "won": False,
        "payout": 0,
    }

    first = await db.play_casino(1, token="spin-1", **kwargs)
    second = await db.play_casino(1, token="spin-1", **kwargs)

    assert first == second
    assert await db.get_balance(1) == 900
    assert len(await db.casino_history(1, limit=10)) == 1


async def test_express_double_tap_charges_once(db: Database) -> None:
    """Экспресс: повторный тап отдаёт тот же экспресс, а не второй."""
    first_match = await _match(db, start_in=3600)
    row, _ = await db.upsert_match(
        fonbet_id=4243,
        tournament="Counter-Strike. IEM. Групповой этап. Bo3",
        team_a="Team Liquid",
        team_b="TYLOO",
        start_time=int(time.time()) + 3600,
        odds_a=2.0,
        odds_b=2.0,
    )
    legs = [
        {"match_id": int(first_match["id"]), "side": "a"},
        {"match_id": int(row["id"]), "side": "a"},
    ]

    one = await db.place_express(1, None, 100, legs, token="cupon-1")
    two = await db.place_express(1, None, 100, legs, token="cupon-1")

    assert one[0] == two[0]            # тот же id экспресса
    assert await db.get_balance(1) == 900


async def test_empty_token_means_no_idempotency(db: Database) -> None:
    """Без токена (старый клиент) ставки работают как раньше — каждая новая."""
    match = await _match(db, start_in=3600)
    match_id = int(match["id"])

    await db.place_bet(match_id, 1, None, "a", 100)
    await db.place_bet(match_id, 1, None, "a", 100)

    assert await db.get_balance(1) == 800


async def test_map_bet_is_idempotent(db: Database) -> None:
    match = await _match(db, start_in=3600)
    card, _ = await db.upsert_map(
        match_id=int(match["id"]), fonbet_id=424_240, map_number=1, odds_a=2.0, odds_b=2.0
    )

    first = await db.place_map_bet(int(card["id"]), 1, None, "a", 100, token="map-once")
    repeat = await db.place_map_bet(int(card["id"]), 1, None, "a", 100, token="map-once")

    assert first == repeat == (900, 2.0)
    assert await db.get_balance(1) == 900
    assert len(await db.user_bets_all(1)) == 1
