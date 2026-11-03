"""Бонусы: колесо фортуны (бесплатная и платные крутки) и пасхалка за десять нажатий."""

from __future__ import annotations

import random
from collections import Counter

import pytest

from app.db import Database
from app.services.bonus import (
    TEASE_PRIZE,
    WHEEL_JACKPOT,
    WHEEL_VALUES,
    daily_layout,
    slots_payload,
    spin,
)


@pytest.fixture
async def db(tmp_path):
    database = Database(tmp_path / "bonus.db", start_balance=1000)
    await database.connect()
    await database.upsert_user(1, "lucky", "Счастливчик")
    yield database
    await database.close()


# --------------------------------------------------------------- раскладка
def test_prize_set() -> None:
    assert len(WHEEL_VALUES) == 24
    assert Counter(WHEEL_VALUES) == {0: 8, 50: 8, 200: 7, WHEEL_JACKPOT: 1}


def test_layout_keeps_same_prizes_but_mixes_order() -> None:
    today = daily_layout(day=20_260_103)
    tomorrow = daily_layout(day=20_260_104)

    assert Counter(today) == Counter(WHEEL_VALUES)     # набор тот же
    assert today != tomorrow                            # порядок другой
    assert list(today) != sorted(today, key=WHEEL_VALUES.index)  # и не исходный


def test_no_two_equal_sectors_side_by_side() -> None:
    """Соседние сектора всегда разные — в том числе последний с первым."""
    for day in range(1, 401):
        layout = daily_layout(day)
        dupes = [i for i in range(24) if layout[i] == layout[(i + 1) % 24]]
        assert not dupes, f"день {day}: одинаковые сектора рядом на позициях {dupes}"


def test_layout_shuffles_between_days() -> None:
    layouts = {daily_layout(day) for day in range(1, 60)}
    assert len(layouts) > 30      # раскладки реально разные, а не одна на все дни


def test_layout_is_stable_within_the_day() -> None:
    assert daily_layout(day=42) == daily_layout(day=42)


def test_jackpot_chance_is_one_in_24() -> None:
    layout = daily_layout(day=7)
    rng = random.Random(2026)
    winners = Counter(spin(layout, rng)[1] for _ in range(24_000))
    assert winners[WHEEL_JACKPOT] / 24_000 == pytest.approx(1 / 24, abs=0.004)
    assert winners[200] / 24_000 == pytest.approx(7 / 24, abs=0.008)
    assert winners[50] / 24_000 == pytest.approx(8 / 24, abs=0.008)
    assert winners[0] / 24_000 == pytest.approx(8 / 24, abs=0.008)


def test_slot_index_matches_payload() -> None:
    layout = daily_layout(day=3)
    slot, prize = spin(layout, random.Random(1))
    assert slots_payload(layout)[slot]["value"] == prize


def test_slots_payload_matches_layout() -> None:
    layout = daily_layout(day=5)
    payload = slots_payload(layout)
    assert len(payload) == 24
    assert [slot["value"] for slot in payload] == list(layout)


# ------------------------------------------------------------------ выдачи
async def test_free_wheel_once_per_cooldown(db: Database) -> None:
    ok, _, balance = await db.claim_wheel(1, 200, 24 * 3600)
    assert ok is True and balance == 1200

    again, wait, balance = await db.claim_wheel(1, WHEEL_JACKPOT, 24 * 3600)
    assert again is False
    assert 0 < wait <= 24 * 3600
    assert balance == 1200                       # второй приз не начислился

    ok, _, balance = await db.claim_wheel(1, WHEEL_JACKPOT, 0)   # окно истекло
    assert ok is True and balance == 2200


async def test_paid_wheel_charges_and_pays(db: Database) -> None:
    ok, _, balance = await db.claim_paid_wheel(1, 100, 200)
    assert ok is True
    assert balance == 1100                       # 1000 − 100 + 200

    ok, _, balance = await db.claim_paid_wheel(1, 100, 0)
    assert ok is True and balance == 1000        # пустой сектор: минус сотка


async def test_paid_wheel_requires_money(db: Database) -> None:
    ok, _, balance = await db.claim_paid_wheel(1, 5000, WHEEL_JACKPOT)
    assert ok is False
    assert balance == 1000                       # баланс не тронули


async def test_paid_wheel_does_not_use_free_spin(db: Database) -> None:
    """Платная крутка не расходует бесплатную."""
    await db.claim_paid_wheel(1, 100, 50)
    state = await db.bonus_state(1, 24 * 3600, 24 * 3600, 24 * 3600)
    assert state["wheel"]["wait_seconds"] == 0


async def test_tease_once_per_day(db: Database) -> None:
    ok, _, balance = await db.claim_tease(1, TEASE_PRIZE, 24 * 3600)
    assert ok is True and balance == 1050
    again, wait, _ = await db.claim_tease(1, TEASE_PRIZE, 24 * 3600)
    assert again is False and wait > 0


async def test_bonus_state_reports_windows(db: Database) -> None:
    state = await db.bonus_state(1, 24 * 3600, 24 * 3600, 24 * 3600)
    assert state["daily"]["wait_seconds"] == 0
    assert state["wheel"]["wait_seconds"] == 0
    assert state["tease"]["wait_seconds"] == 0

    await db.claim_wheel(1, 50, 24 * 3600)
    state = await db.bonus_state(1, 24 * 3600, 24 * 3600, 24 * 3600)
    assert state["wheel"]["wait_seconds"] > 0
    assert state["tease"]["wait_seconds"] == 0           # пасхалка независима от колеса
