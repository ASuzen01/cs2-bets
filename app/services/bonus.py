"""Бонусы: колесо фортуны и пасхалка за настойчивость.

Секторов 24: 1 джекпот, 5 призов по 200, 12 по 50 и 6 пустых.
Порядок секторов на колесе каждый день перемешивается заново: набор призов
неизменен, но соседство каждый день другое. Шанс джекпота всё равно ровно 1 к 24.
"""

from __future__ import annotations

import random
import time
from typing import Final

WHEEL_JACKPOT: Final = 1000
WHEEL_VALUES: Final[tuple[int, ...]] = (
    (WHEEL_JACKPOT,)
    + (200,) * 5
    + (50,) * 12
    + (0,) * 6
)
"""Набор призов: 24 сектора, порядок задаётся раскладкой дня."""

TEASE_TAPS: Final = 10
TEASE_PRIZE: Final = 50
TEASE_TEXT: Final = "заебал, лови полтос на ход ноги"


def day_number(now: float | None = None) -> int:
    """Номер суток — из него получается раскладка дня."""
    return int((now if now is not None else time.time()) // 86400)


def daily_layout(day: int | None = None) -> tuple[int, ...]:
    """Раскладка секторов на сутки: у всех игроков одинаковая и не меняется до полуночи."""
    values = list(WHEEL_VALUES)
    random.Random(day if day is not None else day_number()).shuffle(values)
    return tuple(values)


def spin(layout: tuple[int, ...] | list[int], rng: random.Random | None = None) -> tuple[int, int]:
    """Крутит колесо: (номер сектора, приз). Сектора равновероятны."""
    index = (rng or random).randrange(len(layout))
    return index, int(layout[index])


def slots_payload(layout: tuple[int, ...] | list[int]) -> list[dict[str, int]]:
    """Сектора для фронта: значение и цветовая группа."""
    return [
        {
            "value": value,
            "tier": 2 if value >= WHEEL_JACKPOT else 1 if value >= 200 else 0 if value >= 50 else -1,
        }
        for value in layout
    ]
