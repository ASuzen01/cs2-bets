"""Economy and timing for the one-player «Самолётик» crash game.

A stake is debited before takeoff. The server commits a private crash point at
round creation. The displayed multiplier grows exponentially; cash-out before
that point pays stake × multiplier (gross), otherwise the stake is lost.
The crash distribution has a 95.5% theoretical RTP, matching Dice/Ladder.
"""

from __future__ import annotations

import math
import secrets

RTP = 0.955
MIN_STAKE = 10
MAX_STAKE = 5_000
MAX_MULTIPLIER = 1_000_000.0
GROWTH_PER_SECOND = 0.16
_SCALE = 1_000_000


def validate_stake(amount: int, *, maximum: int = MAX_STAKE) -> int:
    value = int(amount)
    if not MIN_STAKE <= value <= int(maximum):
        raise ValueError(f"Ставка в «Самолётике» — от {MIN_STAKE} до {int(maximum)} 🪙")
    return value


def crash_point(random_value: int | None = None) -> float:
    """Return a server-only crash point with P(crash >= x) ≈ RTP / x.

    There is a 4.5% instant-crash atom at ×1.00. The distribution is rounded
down to hundredths and capped only at an astronomically rare tail.
    """
    value = secrets.randbelow(_SCALE) if random_value is None else int(random_value)
    if not 0 <= value < _SCALE:
        raise ValueError("Random value outside range")
    raw = RTP * _SCALE / (_SCALE - value)
    return min(MAX_MULTIPLIER, max(1.0, math.floor(raw * 100) / 100))


def multiplier_at(started_at: float, now: float) -> float:
    """Multiplier displayed at time `now`, rounded to cents and starting at ×1."""
    elapsed = max(0.0, float(now) - float(started_at))
    if elapsed >= math.log(MAX_MULTIPLIER) / GROWTH_PER_SECOND:
        return MAX_MULTIPLIER
    return math.floor(math.exp(GROWTH_PER_SECOND * elapsed) * 100) / 100


def payout(stake: int, multiplier: float) -> int:
    """Gross payout includes returned stake; integer coin economy rounds down."""
    if int(stake) <= 0 or float(multiplier) < 1.0:
        raise ValueError("Invalid stake or multiplier")
    return math.floor(int(stake) * float(multiplier))


__all__ = [
    "GROWTH_PER_SECOND",
    "MAX_MULTIPLIER",
    "MAX_STAKE",
    "MIN_STAKE",
    "RTP",
    "crash_point",
    "multiplier_at",
    "payout",
    "validate_stake",
]
