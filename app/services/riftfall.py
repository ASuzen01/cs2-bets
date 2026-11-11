"""Server-authoritative cascade slot with expanding Wilds and cumulative free-spin bonus."""

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
FREE_SPIN_AWARDS = {3: 8, 4: 12, 5: 16}
WILD_CODE = "wild"
SCATTER_CODE = "scatter"
MULTIPLIER_VALUES = {"multiplier_1": 1, "multiplier_2": 2, "multiplier_3": 3}
SYMBOLS: tuple[dict[str, Any], ...] = (
    {"code": "coin", "name": "Золотая монета", "icon": "◉"},
    {"code": "briefcase", "name": "Портфель", "icon": "▣"},
    {"code": "bull", "name": "Бык рынка", "icon": "↗"},
    {"code": "diamond", "name": "Бриллиант", "icon": "◆"},
    {"code": "crown", "name": "Корона", "icon": "♛"},
    {"code": WILD_CODE, "name": "Золотой Wild", "icon": "WILD"},
    {"code": SCATTER_CODE, "name": "Scatter", "icon": "✦"},
    {"code": "multiplier_1", "name": "Множитель +1", "icon": "+1×"},
    {"code": "multiplier_2", "name": "Множитель +2", "icon": "+2×"},
    {"code": "multiplier_3", "name": "Множитель +3", "icon": "+3×"},
)
# weights differ by mode: Scatter appears on paid spins; multiplier tiles only in free spins.
PAID_WEIGHTS = {"coin":1840,"briefcase":1840,"bull":1840,"diamond":1840,"crown":1840,"wild":400,"scatter":400}
FREE_WEIGHTS = {"coin":1888,"briefcase":1888,"bull":1888,"diamond":1888,"crown":1888,"wild":400,"multiplier_1":90,"multiplier_2":50,"multiplier_3":20}
FREE_REFILL_WEIGHTS = {"coin":1920,"briefcase":1920,"bull":1920,"diamond":1920,"crown":1920,"wild":400}
# Gross return per connected cluster before RTP calibration.
CLUSTER_BASE: dict[int, float] = {
    5: 0.20, 6: 0.28, 7: 0.38, 8: 0.52, 9: 0.70,
    10: 0.92, 11: 1.18, 12: 1.48, 13: 1.82, 14: 2.20,
    15: 2.62, 16: 3.08, 17: 3.58, 18: 4.12, 19: 4.70, 20: 5.35,
}
# Calibrated with a paid-spin + triggered free-spin simulator; all cluster awards scale linearly.
RTP_SCALE = 0.13274269


def _draw_symbol(mode: str = "paid", randbelow: Callable[[int], int] = secrets.randbelow) -> str:
    weights = FREE_WEIGHTS if mode == "free" else FREE_REFILL_WEIGHTS if mode == "free_refill" else PAID_WEIGHTS
    ticket = randbelow(10_000)
    threshold = 0
    for code, weight in weights.items():
        threshold += weight
        if ticket < threshold:
            return code
    raise AssertionError("Riftfall symbol weights must add to 10000")


def _draw(draw_symbol: Callable[..., str] | None, mode: str) -> str:
    if draw_symbol is None:
        return _draw_symbol(mode)
    try:
        return str(draw_symbol(mode))
    except TypeError:
        # Keep the original zero-argument deterministic test hook compatible.
        return str(draw_symbol())


def _validate_grid(grid: list[list[str]]) -> None:
    if len(grid) != ROWS or any(len(row) != REELS for row in grid):
        raise ValueError(f"Поле должно быть {ROWS}×{REELS}")
    valid = set(PAID_WEIGHTS) | set(MULTIPLIER_VALUES)
    if any(code not in valid for row in grid for code in row):
        raise ValueError("Неизвестный символ на поле")


def _expanded_grid(grid: list[list[str]]) -> tuple[list[list[str]], set[int]]:
    wild_reels = {
        reel for reel in range(REELS)
        if any(grid[row][reel] == WILD_CODE for row in range(ROWS))
    }
    return (
        [[WILD_CODE if reel in wild_reels else grid[row][reel] for reel in range(REELS)] for row in range(ROWS)],
        wild_reels,
    )


def _winning_clusters(grid: list[list[str]]) -> tuple[list[dict[str, Any]], set[tuple[int, int]], list[list[str]]]:
    """Find orthogonal symbol clusters; a landed Wild expands to its full reel."""
    shown, _ = _expanded_grid(grid)
    clusters: list[dict[str, Any]] = []
    cleared: set[tuple[int, int]] = set()
    base_symbols = ("coin", "briefcase", "bull", "diamond", "crown")
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
                    for nr, nc in ((current_row-1,current_reel),(current_row+1,current_reel),(current_row,current_reel-1),(current_row,current_reel+1)):
                        neighbour=(nr,nc)
                        if 0 <= nr < ROWS and 0 <= nc < REELS and neighbour not in seen and shown[nr][nc] in (symbol,WILD_CODE):
                            seen.add(neighbour); stack.append(neighbour)
                if len(cells) >= 5 and has_symbol:
                    cells.sort()
                    clusters.append({"symbol":symbol,"size":len(cells),"cells":[list(p) for p in cells]})
                    cleared.update(cells)
    clusters.sort(key=lambda item:(item["symbol"],item["cells"][0]))
    return clusters, cleared, shown


def _drop_and_refill(grid: list[list[str]], cleared: set[tuple[int, int]], draw_symbol: Callable[..., str] | None, mode: str) -> list[list[str]]:
    result = [["" for _ in range(REELS)] for _ in range(ROWS)]
    for reel in range(REELS):
        survivors = [grid[row][reel] for row in range(ROWS-1,-1,-1) if (row,reel) not in cleared]
        column = [_draw(draw_symbol, mode) for _ in range(ROWS-len(survivors))] + list(reversed(survivors))
        for row,symbol in enumerate(column): result[row][reel]=symbol
    return result


def free_spins_for(scatter_count: int) -> int:
    if scatter_count >= 5: return 16
    if scatter_count == 4: return 12
    if scatter_count == 3: return 8
    return 0


def slot_config() -> dict[str, Any]:
    return {
        "rows":ROWS,"reels":REELS,"rtp":RTP*100,"max_cascades":MAX_CASCADES,
        "chain_multipliers":list(CHAIN_MULTIPLIERS),
        "symbols":[{k:v for k,v in item.items() if k in ("code","name","icon")} for item in SYMBOLS],
        "cluster_payouts":{str(size):round(value*RTP_SCALE,4) for size,value in CLUSTER_BASE.items()},
        "wild_code":WILD_CODE,"scatter_code":SCATTER_CODE,
        "free_spin_awards":{str(k):v for k,v in FREE_SPIN_AWARDS.items()},
        "free_spin_multiplier_values":MULTIPLIER_VALUES.copy(),
        "free_spin_multiplier_rule":"К накопителю ×1 прибавляется значение каждого жетона; в конце сумма выигрышей фри-спинов умножается на накопитель.",
    }


def spin(amount: int, *, free_spin: bool = False, draw_symbol: Callable[..., str] | None = None) -> dict[str, Any]:
    """Draw one base/free spin. DB session accounting pays bonus only at its end."""
    wager=int(amount)
    if wager<=0: raise ValueError("Ставка должна быть положительной")
    mode="free" if free_spin else "paid"
    raw_grid=[[_draw(draw_symbol,mode) for _ in range(REELS)] for _ in range(ROWS)]
    _validate_grid(raw_grid)
    scatter_count=sum(cell==SCATTER_CODE for row in raw_grid for cell in row) if not free_spin else 0
    initial_grid,initial_wild_reels=_expanded_grid(raw_grid)
    multiplier_awards=[MULTIPLIER_VALUES[cell] for row in initial_grid for cell in row if cell in MULTIPLIER_VALUES] if free_spin else []
    current=raw_grid
    steps:list[dict[str,Any]]=[]
    total_multiplier=0.0
    unrounded_cascade_payouts:list[float]=[]
    for cascade_index in range(MAX_CASCADES):
        clusters,cleared,shown=_winning_clusters(current)
        wild_reels={reel for reel in range(REELS) if any(shown[row][reel]==WILD_CODE for row in range(ROWS))}
        if not clusters: break
        chain_multiplier=CHAIN_MULTIPLIERS[cascade_index]
        cluster_wins=[]; cascade_multiplier=0.0
        for cluster in clusters:
            base=CLUSTER_BASE.get(min(20,int(cluster["size"])),0.0)
            prize_multiplier=base*RTP_SCALE*chain_multiplier
            cascade_multiplier+=prize_multiplier
            cluster_wins.append({**cluster,"base_multiplier":round(base*RTP_SCALE,4),"chain_multiplier":chain_multiplier,"multiplier":round(prize_multiplier,4)})
        total_multiplier+=cascade_multiplier
        unrounded_cascade_payouts.append(wager*cascade_multiplier)
        next_raw=_drop_and_refill(current,cleared,draw_symbol,"free_refill" if free_spin else mode)
        next_grid,next_wild_reels=_expanded_grid(next_raw)
        steps.append({"index":cascade_index+1,"grid":shown,"clusters":cluster_wins,"winning_cells":[list(cell) for cell in sorted(cleared)],"chain_multiplier":chain_multiplier,"cascade_multiplier":round(cascade_multiplier,4),"cascade_payout":0,"next_grid":next_grid,"wild_reels":sorted(wild_reels),"next_wild_reels":sorted(next_wild_reels)})
        current=next_raw
    final_grid,final_wild_reels=_expanded_grid(current)
    payout=math.floor(wager*total_multiplier+0.5)
    cumulative_amount=0.0;distributed=0
    for index,step in enumerate(steps):
        cumulative_amount+=unrounded_cascade_payouts[index]
        cumulative_paid=payout if index==len(steps)-1 else math.floor(cumulative_amount+0.5)
        step["cascade_payout"]=cumulative_paid-distributed; distributed=cumulative_paid
    win_count=sum(len(step["clusters"]) for step in steps)
    outcome=(f"{len(steps)} каскада · {win_count} кластеров · ×{total_multiplier:.2f}" if steps else "Выигрышных кластеров нет")
    awarded=free_spins_for(scatter_count)
    return {
        "game":"riftfall","selection":"free_spin" if free_spin else "spin","label":"Золотой разлом",
        "amount":wager,"spin_bet":wager,"odds":round(total_multiplier,4),"payout":payout,"base_payout":payout,
        "win":payout>0,"outcome":outcome,"grid":final_grid,"initial_grid":initial_grid,
        "initial_wild_reels":sorted(initial_wild_reels),"wild_reels":sorted(final_wild_reels),
        "steps":steps,"cascade_count":len(steps),"cluster_count":win_count,"total_multiplier":round(total_multiplier,4),
        "max_cascades":MAX_CASCADES,"rtp":RTP*100,"free_spin":bool(free_spin),
        "scatter_count":scatter_count,"awarded_free_spins":awarded,"multiplier_awards":multiplier_awards,
    }


__all__=["CLUSTER_BASE","CHAIN_MULTIPLIERS","FREE_SPIN_AWARDS","FREE_WEIGHTS","MAX_CASCADES","MULTIPLIER_VALUES","PAID_WEIGHTS","RTP","RTP_SCALE","ROWS","REELS","SCATTER_CODE","SYMBOLS","WILD_CODE","_winning_clusters","free_spins_for","slot_config","spin"]
