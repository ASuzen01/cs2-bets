"""Reproduce the seeded Monte Carlo RTP check for the Golden Rift slot.

Run: python scripts/check_riftfall_rtp.py --spins 250000
"""

from __future__ import annotations

import argparse
import random
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.services import riftfall


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--spins", type=int, default=250_000)
    parser.add_argument("--seed", type=int, default=20_261_102)
    args = parser.parse_args()
    if args.spins <= 0:
        parser.error("--spins must be positive")

    rng = random.Random(args.seed)
    codes = [str(item["code"]) for item in riftfall.SYMBOLS]
    weights = [int(item["weight"]) for item in riftfall.SYMBOLS]

    def draw_symbol() -> str:
        return rng.choices(codes, weights=weights, k=1)[0]

    wager = 100
    paid = wins = cascades = 0
    started = time.perf_counter()
    for _ in range(args.spins):
        result = riftfall.spin(wager, draw_symbol=draw_symbol)
        paid += int(result["payout"])
        wins += int(bool(result["win"]))
        cascades += int(result["cascade_count"])

    rtp = paid / (args.spins * wager) * 100
    hit_rate = wins / args.spins * 100
    average_cascades = cascades / args.spins
    print(
        f"seed={args.seed} spins={args.spins} wager={wager} "
        f"simulated_rtp={rtp:.4f}% hit_rate={hit_rate:.4f}% "
        f"average_cascades={average_cascades:.4f} "
        f"elapsed={time.perf_counter() - started:.1f}s"
    )


if __name__ == "__main__":
    main()
