"""Server-authoritative cascade slot with expanding reel Wilds."""

from __future__ import annotations

import math
import secrets
from collections.abc import Callable
from typing import Any

RTP = 0.955
ROWS = 4
REELS = 5
MAX_CASCADES = 4
CHAIN_MULTIPLIERS: tuple[float, ...] = (1.0, 1.5, 2.25, 3.5)
WILD_CODE = "wild"
SYMBOLS: tuple[dict[str, Any], ...] = (
    {"code": "coin", "name": "Золотая монета", "icon": "◉", "weight": 1920},
    {"code": "briefcase", "name": "Портфель", "icon": "▣", "weight": 1920},
    {"code": "bull", "name": "Бык рынка", "icon": "↗", "weight": 1920},
    {"code": "diamond", "name": "Бриллиант", "icon": "◆", "weight": 1920},
    {"code": "crown", "name": "Корона", "icon": "♛", "weight": 1920},
    {"code": WILD_CODE, "name": "Золотой Wild", "icon": "WILD", "weight": 400},
)
# Unscaled gross return for one connected cluster of the given size.
CLUSTER_BASE: dict[int, float] = {
    5: 0.20, 6: 0.28, 7: 0.38, 8: 0.52, 9: 0.70,
    10: 0.92, 11: 1.18, 12: 1.48, 13: 1.82, 14: 2.20,
    15: 2.62, 16: 3.08, 17: 3.58, 18: 4.12, 19: 4.70, 20: 5.35,
}
# Calibrated against 250,000 deterministic simulation spins using this same
# evaluator and weighted symbol draw; gross return converges to the 95.5% target.
# Kept separate from the public paytable so the business model is easy to audit.
RTP_SCALE = 0.3609


def _draw_symbol() -> str:
    ticket = secrets.randbelow(10_000)
    threshold = 0
    for item in SYMBOLS:
        threshold += int(item["weight"])
        if ticket < threshold:
            return str(item["code"])
    raise AssertionError("Riftfall symbol weights must add to 10000")


def _validate_grid(grid: list[list[str]]) -> None:
    if len(grid) != ROWS or any(len(row) != REELS for row in grid):
        raise ValueError(f"Поле должно быть {ROWS}×{REELS}")
    valid = {item["code"] for item in SYMBOLS}
    if any(code not in valid for row in grid for code in row):
        raise ValueError("Неизвестный символ на поле")


def _expanded_grid(grid: list[list[str]]) -> tuple[list[list[str]], set[int]]:
    wild_reels = {
        reel for reel in range(REELS)
        if any(grid[row][reel] == WILD_CODE for row in range(ROWS))
    }
    return (
        [
            [WILD_CODE if reel in wild_reels else grid[row][reel] for reel in range(REELS)]
            for row in range(ROWS)
        ],
        wild_reels,
    )


def _winning_clusters(grid: list[list[str]]) -> tuple[list[dict[str, Any]], set[tuple[int, int]], list[list[str]]]:
    """Find 4-neighbour symbol clusters; a landed Wild expands to its full reel."""
    shown, _wild_reels = _expanded_grid(grid)
    clusters: list[dict[str, Any]] = []
    cleared: set[tuple[int, int]] = set()
    base_symbols = [str(item["code"]) for item in SYMBOLS if item["code"] != WILD_CODE]

    for symbol in base_symbols:
        seen: set[tuple[int, int]] = set()
        for row in range(ROWS):
            for reel in range(REELS):
                start = (row, reel)
                if start in seen or shown[row][reel] not in (symbol, WILD_CODE):
                    continue
                stack = [start]
                seen.add(start)
                cells: list[tuple[int, int]] = []
                has_symbol = False
                while stack:
                    current_row, current_reel = stack.pop()
                    cells.append((current_row, current_reel))
                    has_symbol |= shown[current_row][current_reel] == symbol
                    for neighbour in (
                        (current_row - 1, current_reel), (current_row + 1, current_reel),
                        (current_row, current_reel - 1), (current_row, current_reel + 1),
                    ):
                        nr, nc = neighbour
                        if (
                            0 <= nr < ROWS and 0 <= nc < REELS and neighbour not in seen
                            and shown[nr][nc] in (symbol, WILD_CODE)
                        ):
                            seen.add(neighbour)
                            stack.append(neighbour)
                if len(cells) >= 5 and has_symbol:
                    cells.sort()
                    clusters.append({"symbol": symbol, "size": len(cells), "cells": [list(p) for p in cells]})
                    cleared.update(cells)

    clusters.sort(key=lambda item: (item["symbol"], item["cells"][0]))
    return clusters, cleared, shown


def _drop_and_refill(grid: list[list[str]], cleared: set[tuple[int, int]], draw: Callable[[], str]) -> list[list[str]]:
    result = [["" for _ in range(REELS)] for _ in range(ROWS)]
    for reel in range(REELS):
        survivors = [grid[row][reel] for row in range(ROWS - 1, -1, -1) if (row, reel) not in cleared]
        column = [draw() for _ in range(ROWS - len(survivors))] + list(reversed(survivors))
        for row, symbol in enumerate(column):
            result[row][reel] = symbol
    return result


def slot_config() -> dict[str, Any]:
    """Public rules and payout table; the server alone draws and settles results."""
    return {
        "rows": ROWS,
        "reels": REELS,
        "rtp": RTP * 100,
        "max_cascades": MAX_CASCADES,
        "chain_multipliers": list(CHAIN_MULTIPLIERS),
        "symbols": [{key: item[key] for key in ("code", "name", "icon")} for item in SYMBOLS],
        "cluster_payouts": {
            str(size): round(value * RTP_SCALE, 4) for size, value in CLUSTER_BASE.items()
        },
        "wild_code": WILD_CODE,
    }


def spin(amount: int, *, draw_symbol: Callable[[], str] | None = None) -> dict[str, Any]:
    """Draw and settle a complete cascade spin for a total wager."""
    wager = int(amount)
    if wager <= 0:
        raise ValueError("Ставка должна быть положительной")
    draw = draw_symbol or _draw_symbol
    raw_grid = [[draw() for _ in range(REELS)] for _ in range(ROWS)]
    _validate_grid(raw_grid)
    initial_grid, initial_wild_reels = _expanded_grid(raw_grid)
    current = raw_grid
    steps: list[dict[str, Any]] = []
    total_multiplier = 0.0
    unrounded_cascade_payouts: list[float] = []

    for cascade_index in range(MAX_CASCADES):
        clusters, cleared, shown = _winning_clusters(current)
        wild_reels = {
            reel for reel in range(REELS)
            if any(shown[row][reel] == WILD_CODE for row in range(ROWS))
        }
        if not clusters:
            break
        chain_multiplier = CHAIN_MULTIPLIERS[cascade_index]
        cluster_wins = []
        cascade_multiplier = 0.0
        for cluster in clusters:
            base = CLUSTER_BASE.get(min(20, int(cluster["size"])), 0.0)
            prize_multiplier = base * RTP_SCALE * chain_multiplier
            cascade_multiplier += prize_multiplier
            cluster_wins.append({
                **cluster,
                "base_multiplier": round(base * RTP_SCALE, 4),
                "chain_multiplier": chain_multiplier,
                "multiplier": round(prize_multiplier, 4),
            })
        total_multiplier += cascade_multiplier
        unrounded_cascade_payouts.append(wager * cascade_multiplier)
        next_raw = _drop_and_refill(current, cleared, draw)
        next_grid, next_wild_reels = _expanded_grid(next_raw)
        steps.append({
            "index": cascade_index + 1,
            "grid": shown,
            "clusters": cluster_wins,
            "winning_cells": [list(cell) for cell in sorted(cleared)],
            "chain_multiplier": chain_multiplier,
            "cascade_multiplier": round(cascade_multiplier, 4),
            "cascade_payout": 0,
            "next_grid": next_grid,
            "wild_reels": sorted(wild_reels),
            "next_wild_reels": sorted(next_wild_reels),
        })
        current = next_raw

    final_grid, final_wild_reels = _expanded_grid(current)
    payout = math.floor(wager * total_multiplier + 0.5)
    cumulative_amount = 0.0
    distributed = 0
    for index, step in enumerate(steps):
        cumulative_amount += unrounded_cascade_payouts[index]
        cumulative_paid = payout if index == len(steps) - 1 else math.floor(cumulative_amount + 0.5)
        step["cascade_payout"] = cumulative_paid - distributed
        distributed = cumulative_paid
    win_count = sum(len(step["clusters"]) for step in steps)
    outcome = (
        f"{len(steps)} каскад(а) · {win_count} кластер(а) · ×{total_multiplier:.2f}"
        if steps else "Выигрышных кластеров нет"
    )
    return {
        "game": "riftfall",
        "selection": "spin",
        "label": "Золотой разлом",
        "amount": wager,
        "spin_bet": wager,
        "odds": round(total_multiplier, 4),
        "payout": payout,
        "win": payout > 0,
        "outcome": outcome,
        "grid": final_grid,
        "initial_grid": initial_grid,
        "initial_wild_reels": sorted(initial_wild_reels),
        "wild_reels": sorted(final_wild_reels),
        "steps": steps,
        "cascade_count": len(steps),
        "cluster_count": win_count,
        "total_multiplier": round(total_multiplier, 4),
        "max_cascades": MAX_CASCADES,
        "rtp": RTP * 100,
    }
