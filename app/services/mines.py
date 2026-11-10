"""Правила и математика игры «Мины» на поле 5×5.

Расклад генерируется сервером. Множитель каждого уровня равен целевому RTP,
делённому на вероятность открыть указанное число безопасных клеток.
"""

from __future__ import annotations

import math
import secrets

CELLS = 25
GRID_SIZE = 5
MIN_MINES = 2
MAX_MINES = 24
DEFAULT_MINES = 3
MIN_STAKE = 10
RTP = 0.955


def validate_stake(amount: int) -> int:
    stake = int(amount)
    if stake < MIN_STAKE:
        raise ValueError(f"Минимальная ставка в «Минах» — {MIN_STAKE} 🪙")
    return stake


def validate_mine_count(count: int) -> int:
    mines = int(count)
    if not MIN_MINES <= mines <= MAX_MINES:
        raise ValueError(f"Выбери от {MIN_MINES} до {MAX_MINES} мин")
    return mines


def survival_probability(mine_count: int, safe_picks: int) -> float:
    """Вероятность открыть `safe_picks` безопасных клеток подряд."""
    mines = validate_mine_count(mine_count)
    picks = int(safe_picks)
    if not 1 <= picks <= CELLS - mines:
        raise ValueError("Некорректное число безопасных клеток")
    return math.comb(CELLS - mines, picks) / math.comb(CELLS, picks)


def multiplier(mine_count: int, safe_picks: int) -> float:
    """Валовой множитель: включая исходную ставку, до округления выплаты."""
    return RTP / survival_probability(mine_count, safe_picks)


def payout(stake: int, mine_count: int, safe_picks: int) -> int:
    """Валовая выплата, точно вычисленная целыми числами и округлённая вниз."""
    wager = int(stake)
    if wager <= 0:
        raise ValueError("Ставка должна быть положительной")
    mines = validate_mine_count(mine_count)
    picks = int(safe_picks)
    if not 1 <= picks <= CELLS - mines:
        raise ValueError("Некорректное число безопасных клеток")
    # RTP = 191/200. Integer arithmetic avoids floating point drift.
    numerator = wager * 191 * math.comb(CELLS, picks)
    denominator = 200 * math.comb(CELLS - mines, picks)
    return numerator // denominator


def place_mines(mine_count: int) -> list[int]:
    """Криптографически выбирает уникальные индексы мин от 0 до 24."""
    mines = validate_mine_count(mine_count)
    return secrets.SystemRandom().sample(range(CELLS), mines)


def validate_layout(mine_count: int, positions: list[int]) -> list[int]:
    """Validate a server-generated layout before it is persisted."""
    mines = validate_mine_count(mine_count)
    layout = [int(position) for position in positions]
    if len(layout) != mines or len(set(layout)) != mines:
        raise ValueError("Некорректное расположение мин")
    if any(not 0 <= position < CELLS for position in layout):
        raise ValueError("Клетка вне игрового поля")
    return sorted(layout)


def config() -> dict[str, int | float]:
    return {
        "cells": CELLS,
        "grid_size": GRID_SIZE,
        "min_mines": MIN_MINES,
        "max_mines": MAX_MINES,
        "default_mines": DEFAULT_MINES,
        "min_stake": MIN_STAKE,
        "rtp": RTP * 100,
    }


__all__ = [
    "CELLS", "DEFAULT_MINES", "GRID_SIZE", "MAX_MINES", "MIN_MINES",
    "MIN_STAKE", "RTP", "config", "multiplier", "payout", "place_mines",
    "survival_probability", "validate_layout", "validate_mine_count", "validate_stake",
]
