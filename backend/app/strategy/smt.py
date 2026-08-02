"""SMT (Smart Money Technique) divergence between the primary index (NIFTY)
and the confirmation index (BANKNIFTY): one index prints a new swing extreme
while the other fails to, signalling the move may not be genuine broad-market
strength/weakness."""
from __future__ import annotations

import pandas as pd

from app.strategy.swings import find_swings
from app.strategy.types import Direction


def check_smt_divergence(
    primary_1m: pd.DataFrame,
    confirm_1m: pd.DataFrame,
    direction: Direction,
    window: int,
) -> tuple[bool, str | None]:
    """`primary_1m`/`confirm_1m` should both be truncated to "as of now"
    (no look-ahead) - i.e. only candles up to the bar being evaluated.
    Returns (divergence_found, human-readable detail)."""
    if primary_1m.empty or confirm_1m.empty:
        return False, None

    primary_swings = find_swings(primary_1m, window)
    confirm_swings = find_swings(confirm_1m, window)

    kind = "low" if direction is Direction.BUY else "high"
    p_points = [s for s in primary_swings if s.kind == kind]
    c_points = [s for s in confirm_swings if s.kind == kind]

    if len(p_points) < 2 or len(c_points) < 2:
        return False, None

    p_prev, p_last = p_points[-2], p_points[-1]
    c_prev, c_last = c_points[-2], c_points[-1]

    if direction is Direction.BUY:
        # bullish divergence: primary sweeps to a LOWER low, confirm makes a HIGHER low
        primary_new_extreme = p_last.price < p_prev.price
        confirm_failed = c_last.price > c_prev.price
    else:
        # bearish divergence: primary sweeps to a HIGHER high, confirm makes a LOWER high
        primary_new_extreme = p_last.price > p_prev.price
        confirm_failed = c_last.price < c_prev.price

    if primary_new_extreme and confirm_failed:
        detail = (
            f"SMT divergence: primary {kind} {p_prev.price:.1f}->{p_last.price:.1f}, "
            f"confirm {kind} {c_prev.price:.1f}->{c_last.price:.1f} (failed to confirm)"
        )
        return True, detail

    return False, None
