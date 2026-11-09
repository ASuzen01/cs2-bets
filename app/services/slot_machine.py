"""Server-side 5×3, 243-ways slot machine with retrievable free spins."""

from __future__ import annotations

import itertools
import math
import secrets
from typing import Any

RTP = 0.955
ROWS = 3
REELS = 5
WAYS = 3**REELS
WEIGHT_SCALE = 10_000

# Order also controls the weighted random sampler.
SYMBOLS: tuple[dict[str, Any], ...] = (
    {"code": "cherry", "name": "Вишня", "icon": "🍒", "weight": 2900},
    {"code": "lemon", "name": "Лимон", "icon": "🍋", "weight": 2400},
    {"code": "bell", "name": "Колокол", "icon": "🔔", "weight": 1900},
    {"code": "bar", "name": "BAR", "icon": "BAR", "weight": 1300},
    {"code": "seven", "name": "Семёрка", "icon": "7️⃣", "weight": 900},
    {"code": "diamond", "name": "Бриллиант", "icon": "💎", "weight": 300},
    {"code": "scatter", "name": "Scatter", "icon": "✦", "weight": 300},
)
SCATTER_CODE = "scatter"
SCATTER_AWARDS = {3: 10, 4: 15, 5: 20}
CHEST_PICKS = {3: 1, 4: 2, 5: 3}
# 12 public prizes; shuffled server-side and saved before the player chooses.
CHEST_PRIZES: tuple[tuple[float, int], ...] = ((0.0, 6), (0.5, 3), (1.0, 2), (3.0, 1))
# Unscaled payout per one way / one coin wager, for exact 3, 4, or 5 matches.
BASE_PAYOUTS: dict[str, tuple[float, float, float]] = {
    "cherry": (0.35, 1.10, 4.50),
    "lemon": (0.45, 1.40, 6.00),
    "bell": (0.65, 2.20, 9.50),
    "bar": (1.20, 4.50, 22.00),
    "seven": (2.50, 10.00, 55.00),
    "diamond": (5.00, 30.00, 200.00),
}


def _scatter_award(count: int) -> int:
    if count < 3:
        return 0
    return SCATTER_AWARDS.get(count, 20)


def _chest_pick_count(count: int) -> int:
    if count < 3:
        return 0
    return CHEST_PICKS.get(count, 3)


def _expected_chest_return() -> float:
    scatter_probability = next(item["weight"] for item in SYMBOLS if item["code"] == SCATTER_CODE) / WEIGHT_SCALE
    average_prize = sum(multiplier * count for multiplier, count in CHEST_PRIZES) / sum(
        count for _, count in CHEST_PRIZES
    )
    return sum(
        math.comb(ROWS * REELS, count)
        * scatter_probability**count
        * (1 - scatter_probability) ** (ROWS * REELS - count)
        * _chest_pick_count(count)
        * average_prize
        for count in range(ROWS * REELS + 1)
    )


def _expected_free_spins() -> float:
    scatter_probability = next(item["weight"] for item in SYMBOLS if item["code"] == SCATTER_CODE) / WEIGHT_SCALE
    return sum(
        math.comb(ROWS * REELS, count)
        * scatter_probability**count
        * (1 - scatter_probability) ** (ROWS * REELS - count)
        * _scatter_award(count)
        for count in range(ROWS * REELS + 1)
    )


def _expected_return_per_way(paytable: dict[str, tuple[float, float, float]]) -> float:
    expected = 0.0
    for item in SYMBOLS:
        code = item["code"]
        if code == SCATTER_CODE:
            continue
        probability = item["weight"] / WEIGHT_SCALE
        pay3, pay4, pay5 = paytable[code]
        expected += (
            probability**3 * (1 - probability) * pay3
            + probability**4 * (1 - probability) * pay4
            + probability**5 * pay5
        )
    return expected


def _build_paytable() -> dict[str, tuple[float, float, float]]:
    """Normalize base-game paytable so base spins plus free spins target 95.5% RTP.

    Free spins use the triggering spin's total wager and do not retrigger. Chest
    rewards are also tied to that wager; their expected value is reserved before
    scaling the line paytable so the combined paid spins, free spins, and chest
    picks still target the configured RTP.
    """
    expected_bonus_spins = _expected_free_spins()
    expected_chest_return = _expected_chest_return()
    desired_paid_spin_return = (RTP - expected_chest_return) / (1 + expected_bonus_spins)
    raw_return = _expected_return_per_way(BASE_PAYOUTS)
    scale = desired_paid_spin_return / raw_return
    table: dict[str, tuple[float, float, float]] = {}
    # Rounded public multipliers remain near the target while matching display.
    for _ in range(10):
        table = {
            symbol: tuple(round(value * scale, 2) for value in values)
            for symbol, values in BASE_PAYOUTS.items()
        }
        actual_base_return = _expected_return_per_way(table)
        scale *= desired_paid_spin_return / actual_base_return
    return table


PAYTABLE = _build_paytable()


def new_chest_board() -> list[float]:
    """Create a private, fixed board so retries and reloads cannot reroll it."""
    board = [multiplier for multiplier, count in CHEST_PRIZES for _ in range(count)]
    secrets.SystemRandom().shuffle(board)
    return board


def _draw_symbol() -> str:
    ticket = secrets.randbelow(WEIGHT_SCALE)
    threshold = 0
    for item in SYMBOLS:
        threshold += int(item["weight"])
        if ticket < threshold:
            return str(item["code"])
    raise AssertionError("slot symbol weights must add to 10000")


def _round_coins(value: float) -> int:
    return math.floor(value + 0.5)


def slot_config() -> dict[str, Any]:
    """Public slot rules and paytable for the Mini App; never chooses outcomes."""
    return {
        "rows": ROWS,
        "reels": REELS,
        "ways": WAYS,
        "rtp": RTP * 100,
        "symbols": [
            {key: item[key] for key in ("code", "name", "icon")}
            for item in SYMBOLS
        ],
        "paytable": {
            symbol: {"3": values[0], "4": values[1], "5": values[2]}
            for symbol, values in PAYTABLE.items()
        },
        "scatter_awards": {str(count): award for count, award in SCATTER_AWARDS.items()},
        "chest_picks": {str(count): picks for count, picks in CHEST_PICKS.items()},
        "chest_prizes": [
            {"multiplier": multiplier, "count": count}
            for multiplier, count in CHEST_PRIZES
        ],
        "chest_rtp_points": round(_expected_chest_return() * 100, 4),
    }


def spin(amount: int, *, free_spin: bool = False) -> dict[str, Any]:
    """Generate a server-authoritative spin and gross payout for a total spin bet."""
    wager = int(amount)
    if wager <= 0:
        raise ValueError("Ставка должна быть положительной")

    grid = [[_draw_symbol() for _ in range(REELS)] for _ in range(ROWS)]
    scatter_count = sum(cell == SCATTER_CODE for row in grid for cell in row)
    awarded = 0 if free_spin else _scatter_award(scatter_count)
    chest_picks = 0 if free_spin else _chest_pick_count(scatter_count)

    # A way selects one of three row positions from each reel, left to right.
    # The wager is the total spin budget, shared uniformly by the 243 ways.
    multiplier_sum = 0.0
    groups: dict[tuple[str, int], int] = {}
    winning_cells: set[tuple[int, int]] = set()
    for row_choices in itertools.product(range(ROWS), repeat=REELS):
        first = grid[row_choices[0]][0]
        if first == SCATTER_CODE:
            continue
        length = 1
        while length < REELS and grid[row_choices[length]][length] == first:
            length += 1
        if length < 3:
            continue
        multiplier = PAYTABLE[first][length - 3]
        multiplier_sum += multiplier
        groups[(first, length)] = groups.get((first, length), 0) + 1
        for reel in range(length):
            winning_cells.add((row_choices[reel], reel))

    payout = _round_coins(wager * multiplier_sum / WAYS)
    wins = [
        {
            "symbol": symbol,
            "length": length,
            "ways": count,
            "multiplier": PAYTABLE[symbol][length - 3],
        }
        for (symbol, length), count in sorted(groups.items(), key=lambda item: (item[0][1], item[0][0]))
    ]
    if awarded:
        outcome = f"Scatter ×{scatter_count} · {awarded} фри-спинов · сундуки ×{chest_picks}"
    elif wins:
        outcome = f"{sum(item['ways'] for item in wins)} выигрышных ways"
    else:
        outcome = "Без выигрыша"

    return {
        "grid": grid,
        "scatter_count": scatter_count,
        "awarded_free_spins": awarded,
        "awarded_chest_picks": chest_picks,
        "wins": wins,
        "win_count": sum(item["ways"] for item in wins),
        "winning_cells": [list(cell) for cell in sorted(winning_cells)],
        "payout": payout,
        "multiplier": round(payout / wager, 4),
        "win": payout > 0,
        "free_spin": bool(free_spin),
        "outcome": outcome,
    }


__all__ = [
    "PAYTABLE",
    "REELS",
    "ROWS",
    "RTP",
    "SCATTER_AWARDS",
    "SYMBOLS",
    "WAYS",
    "_expected_chest_return",
    "_expected_free_spins",
    "_expected_return_per_way",
    "new_chest_board",
    "slot_config",
    "spin",
]
