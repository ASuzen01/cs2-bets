"""Бонусы: колесо фортуны и пасхалка за настойчивость.

Колесо на 24 сектора: 1 джекпот, 5 призов по 200, 12 по 50 и 6 пустых.
Шанс джекпота ровно 1 к 24 — сектора выпадают равновероятно.
"""

from __future__ import annotations

import random
from typing import Final

WHEEL_JACKPOT: Final = 1000
WHEEL_SLOTS: Final[tuple[int, ...]] = (
    (WHEEL_JACKPOT,)
    + (200,) * 5
    + (50,) * 12
    + (0,) * 6
)
"""24 сектора по порядку: значения совпадают с секторами на клиенте."""

TEASE_TAPS: Final = 10
TEASE_PRIZE: Final = 50
TEASE_TEXT: Final = "заебал, лови полтос на ход ноги"


def spin(rng: random.Random | None = None) -> tuple[int, int]:
    """Крутит колесо: (номер сектора, приз). Сектора равновероятны."""
    index = (rng or random).randrange(len(WHEEL_SLOTS))
    return index, WHEEL_SLOTS[index]


def slots_payload() -> list[dict[str, int]]:
    """Сектора для фронта: значение и цветовая группа."""
    return [
        {
            "value": value,
            "tier": 2 if value >= WHEEL_JACKPOT else 1 if value >= 200 else 0 if value >= 50 else -1,
        }
        for value in WHEEL_SLOTS
    ]
