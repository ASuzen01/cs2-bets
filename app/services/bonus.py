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
    """Раскладка секторов на сутки: одна и та же для всех, до полуночи не меняется.

    Сектора раскладываем так, чтобы рядом не оказалось двух одинаковых призов.
    Призов у нас 12 полтин на 24 сектора — значит они должны стоять через один,
    а в промежутки надо чередовать пусто и 200/1000. Внутри этого правила
    порядок перемешивается: группы из четырёх секторов собираются в случайном
    порядке, призы и пустышки ходят по своим местам, плюс случайный поворот.
    """
    rng = random.Random(day if day is not None else day_number())
    prizes = [200] * 5 + [WHEEL_JACKPOT]
    rng.shuffle(prizes)

    ring: list[int] = []
    for index in range(6):
        # каждая четвёрка — [50, пусто, 50, приз] или [50, приз, 50, пусто]
        middle = [0, prizes[index]] if rng.random() < 0.5 else [prizes[index], 0]
        ring.extend([50, middle[0], 50, middle[1]])

    shift = rng.randrange(len(ring))
    ring = ring[shift:] + ring[:shift]
    if rng.random() < 0.5:          # отражение — ещё один вариант раскладки
        ring = [ring[0], *reversed(ring[1:])]
    return tuple(ring)


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
