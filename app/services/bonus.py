"""Бонусы: колесо фортуны и пасхалка за настойчивость.

Экономика колеса (пересчитана 5 ноября 2026)
-------------------------------------------
Секторов 24, «джекпот-шоу»: 5000×1, 500×2, 100×5, билет 100×2, 25×10, пусто×4.

* средний приз — (5000 + 1000 + 500 + 200 + 250) / 24 = 289.6 🪙;
* платная крутка стоит 300 🪙 → возврат 96.5 % (было 77.8 % при цене 150);
* бесплатная крутка раз в сутки — это подарок, она приносит в среднем 290 🪙;
* билет считается по номиналу: это ставка на 100 🪙 за счёт заведения.

Порядок секторов каждый день перемешивается заново, но так, чтобы рядом никогда
не стояли два одинаковых приза. Шанс джекпота — ровно 1 к 24.
"""

from __future__ import annotations

import random
import secrets
import time
from typing import Any, Final

WHEEL_JACKPOT: Final = 5000
WHEEL_TICKET: Final = -1                 # сектор «билет в экспресс» — не монеты
WHEEL_TICKET_VALUE: Final = 100          # номинал билета: ставка за счёт заведения
WHEEL_VALUES: Final[tuple[int, ...]] = (
    (WHEEL_JACKPOT,)
    + (500,) * 2
    + (100,) * 5
    + (WHEEL_TICKET,) * 2
    + (25,) * 10
    + (0,) * 4
)
"""Набор призов: 24 сектора, порядок задаётся раскладкой дня.

Билет — отдельный тип приза: он не начисляет монеты, а кладёт в профиль
бесплатную ставку на экспресс. Считаем по номиналу 100 🪙 при расчёте экономики.
"""

WHEEL_COUNTS: Final[dict[int, int]] = {value: WHEEL_VALUES.count(value) for value in set(WHEEL_VALUES)}


def prize_value(value: int) -> int:
    """Сколько монет приносит сектор: билет считаем по номиналу."""
    return WHEEL_TICKET_VALUE if value == WHEEL_TICKET else int(value)


def is_ticket(value: int) -> bool:
    return value == WHEEL_TICKET


def average_prize() -> float:
    """Средний приз крутки — из него считается возврат платной крутки."""
    return sum(prize_value(value) for value in WHEEL_VALUES) / len(WHEEL_VALUES)

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
    """Гарантированно валидная раскладка: частый приз через один с редкими."""
    ring: list[int] = []
    rest = [500, 500, 100, 100, WHEEL_JACKPOT, 100, 100, 100, WHEEL_TICKET, WHEEL_TICKET,
            0, 0, 0, 0]
    for index in range(10):
        ring.append(25)
        if index < len(rest):
            ring.append(rest[index])
    ring += rest[10:]
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
    index = rng.randrange(len(layout)) if rng is not None else secrets.randbelow(len(layout))
    return index, int(layout[index])


def slots_payload(layout: tuple[int, ...] | list[int]) -> list[dict[str, Any]]:
    """Сектора для фронта: значение, цветовая группа и тип приза."""
    return [
        {
            "value": prize_value(value),
            "tier": _tier(value),
            "ticket": is_ticket(value),
        }
        for value in layout
    ]


def _tier(value: int) -> str:
    """Цветовая группа сектора — по ней фронт выбирает палитру."""
    if value == WHEEL_JACKPOT:
        return "jack"
    if is_ticket(value):
        return "ticket"
    if value >= 500:
        return "big"
    if value >= 100:
        return "mid"
    if value > 0:
        return "small"
    return "empty"
