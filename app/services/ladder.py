"""Математика игры «Лесенка»: 8 этажей, одна призовая дверь на каждом.

Сервер прячет приз за одной из n дверей. На этаже k число дверей k+1.
Целевой RTP 95.5 %, такой же как у костей; выплата считается валовой,
то есть включает исходную ставку.
"""

from __future__ import annotations

import math
import secrets

RTP = 0.955
MAX_ROUNDS = 8
MIN_STAKE = 10
MAX_STAKE = 50


def window_count(round_no: int) -> int:
    if not 1 <= int(round_no) <= MAX_ROUNDS:
        raise ValueError("Этаж должен быть от 1 до 8")
    return int(round_no) + 1


def reach_probability(round_no: int) -> float:
    """Шанс дойти с первого этажа до этого включительно."""
    window_count(round_no)  # validates
    return 1 / math.factorial(int(round_no) + 1)


def multiplier(round_no: int) -> float:
    """Общий множитель выплаты: RTP / шанс дойти до указанного этажа."""
    return RTP / reach_probability(round_no)


def payout(stake: int, round_no: int) -> int:
    if stake <= 0:
        raise ValueError("Ставка должна быть положительной")
    return round(int(stake) * multiplier(round_no))


def choose_window(windows: int) -> int:
    """Серверный случайный индекс призовой двери."""
    if windows < 2:
        raise ValueError("Нужно минимум две двери")
    return secrets.randbelow(windows)


def validate_stake(stake: int, max_stake: int = MAX_STAKE) -> int:
    amount = int(stake)
    if amount < MIN_STAKE:
        raise ValueError(f"Минимальная ставка — {MIN_STAKE} 🪙")
    if amount > int(max_stake):
        raise ValueError(f"Максимальная ставка в «Лесенке» — {int(max_stake)} 🪙")
    return amount


__all__ = [
    "MAX_ROUNDS",
    "MAX_STAKE",
    "MIN_STAKE",
    "RTP",
    "choose_window",
    "multiplier",
    "payout",
    "reach_probability",
    "validate_stake",
    "window_count",
]
