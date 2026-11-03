"""Бонусы: колесо фортуны и пасхалка за десять нажатий."""

from __future__ import annotations

import random
from collections import Counter

import pytest

from app.db import Database
from app.services.bonus import TEASE_PRIZE, WHEEL_JACKPOT, WHEEL_SLOTS, slots_payload, spin


@pytest.fixture
async def db(tmp_path):
    database = Database(tmp_path / "bonus.db", start_balance=1000)
    await database.connect()
    await database.upsert_user(1, "lucky", "Счастливчик")
    yield database
    await database.close()


def test_wheel_layout() -> None:
    assert len(WHEEL_SLOTS) == 24
    assert Counter(WHEEL_SLOTS) == {0: 6, 50: 12, 200: 5, WHEEL_JACKPOT: 1}


def test_jackpot_chance_is_one_in_24() -> None:
    rng = random.Random(2026)
    winners = Counter(spin(rng)[1] for _ in range(24_000))
    assert winners[WHEEL_JACKPOT] / 24_000 == pytest.approx(1 / 24, abs=0.004)
    assert winners[200] / 24_000 == pytest.approx(5 / 24, abs=0.008)
    assert winners[50] / 24_000 == pytest.approx(12 / 24, abs=0.008)


def test_slots_payload_matches_wheel() -> None:
    payload = slots_payload()
    assert len(payload) == 24
    assert [slot["value"] for slot in payload] == list(WHEEL_SLOTS)


async def test_wheel_once_per_cooldown(db: Database) -> None:
    ok, _, balance = await db.claim_wheel(1, 200, 24 * 3600)
    assert ok is True and balance == 1200

    again, wait, balance = await db.claim_wheel(1, WHEEL_JACKPOT, 24 * 3600)
    assert again is False
    assert 0 < wait <= 24 * 3600
    assert balance == 1200                       # второй приз не начислился

    ok, _, balance = await db.claim_wheel(1, WHEEL_JACKPOT, 0)   # окно истекло
    assert ok is True and balance == 2200


async def test_wheel_zero_prize_still_uses_the_day(db: Database) -> None:
    ok, _, balance = await db.claim_wheel(1, 0, 24 * 3600)
    assert ok is True and balance == 1000
    again, _, _ = await db.claim_wheel(1, WHEEL_JACKPOT, 24 * 3600)
    assert again is False


async def test_tease_once_per_day(db: Database) -> None:
    ok, _, balance = await db.claim_tease(1, TEASE_PRIZE, 24 * 3600)
    assert ok is True and balance == 1050
    again, wait, _ = await db.claim_tease(1, TEASE_PRIZE, 24 * 3600)
    assert again is False and wait > 0


async def test_bonus_state_reports_windows(db: Database) -> None:
    state = await db.bonus_state(1, 24 * 3600, 24 * 3600, 24 * 3600)
    assert state["daily"]["wait_seconds"] == 0          # бонус ещё не брали
    assert state["wheel"]["wait_seconds"] == 0
    assert state["tease"]["wait_seconds"] == 0

    await db.claim_wheel(1, 50, 24 * 3600)
    state = await db.bonus_state(1, 24 * 3600, 24 * 3600, 24 * 3600)
    assert state["wheel"]["wait_seconds"] > 0
    assert state["tease"]["wait_seconds"] == 0           # пасхалка независима от колеса
