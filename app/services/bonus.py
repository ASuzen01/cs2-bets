"""Бонусы: колесо фортуны и пасхалка за настойчивость.

Секторов 24: 1 джекпот, 7 призов по 200, 8 по 50 и 8 пустых.
Порядок секторов каждый день перемешивается заново, но так, чтобы рядом никогда
не стояли два одинаковых приза. Шанс джекпота — ровно 1 к 24.
"""

from __future__ import annotations

import random
import time
from typing import Final

WHEEL_JACKPOT: Final = 1000
WHEEL_VALUES: Final[tuple[int, ...]] = (
    (WHEEL_JACKPOT,)
    + (200,) * 7
    + (50,) * 8
    + (0,) * 8
)
"""Набор призов: 24 сектора, порядок задаётся раскладкой дня."""

WHEEL_COUNTS: Final[dict[int, int]] = {value: WHEEL_VALUES.count(value) for value in set(WHEEL_VALUES)}

TEASE_TAPS: Final = 10
TEASE_PRIZE: Final = 50
TEASE_TEXT: Final = "заебал, лови полтос на ход ноги"


def day_number(now: float | None = None) -> int:
    """Номер суток — из него получается раскладка дня."""
    return int((now if now is not None else time.time()) // 86400)


def _greedy_ring(rng: random.Random) -> list[int] | None:
    """Собирает колесо с конца: берём самый частый приз, который не равен предыдущему."""
    left = dict(WHEEL_COUNTS)
    ring: list[int] = []
    while left:
        options = [value for value in left if not ring or value != ring[-1]]
        if not options:
            return None
        most = max(left[value] for value in options)
        value = rng.choice([value for value in options if left[value] == most])
        ring.append(value)
        left[value] -= 1
        if not left[value]:
            del left[value]
    if len(ring) > 1 and ring[0] == ring[-1]:   # стык последнего сектора с первым
        return None
    return ring


def _fallback_ring() -> list[int]:
    """Гарантированно валидная раскладка: 50 · 200 · 0 по кругу, один 200 — джекпот."""
    ring = [50, 200, 0] * 8
    ring[1] = WHEEL_JACKPOT
    return ring


def daily_layout(day: int | None = None) -> tuple[int, ...]:
    """Раскладка секторов на сутки: одна для всех, до полуночи не меняется.

    Правило одно: два одинаковых приза не стоят рядом (включая стык круга).
    Порядок подбирается случайно, но с проверкой — если стык не сошёлся,
    перемешиваем заново; есть и заранее проверенный запасной вариант.
    """
    rng = random.Random(day if day is not None else day_number())
    for _ in range(200):
        ring = _greedy_ring(rng)
        if ring is not None:
            return tuple(ring)
    return tuple(_fallback_ring())


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
