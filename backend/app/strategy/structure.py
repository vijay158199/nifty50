"""Market structure analysis on the 1-minute chart after a liquidity
interaction with the first 30-min candle's high or low: determines whether
the move that took that liquidity is a genuine continuation (BOS) or a
reversal (CHOCH) - see breakout_sweep.find_trigger for what precedes this.

Definitions used here (matching the BOS/CHOCH-at-liquidity spec):
- BOS High: after price takes the HIGH, it closes beyond a swing HIGH first
  (before any break the other way) -> bullish continuation, no reversal risk
  to wait out. Direction = BUY. Mirrored for BOS Low (SELL).
- CHOCH High: after price takes the HIGH, it instead closes beyond a swing
  LOW first -> a candidate bearish reversal. This is only emitted once
  CONFIRMED by a second break, of a swing formed after the first one, in
  that same reversed direction (a fresh Lower Low) - a single break against
  the liquidity side isn't enough on its own, matching the spec's "Lower
  Low or bearish MSS confirms the reversal." Direction = SELL. Mirrored for
  CHOCH Low (BUY, confirmed by a fresh Higher High).

Deliberately conservative: an unconfirmed reversal candidate is never
downgraded back to a fresh BOS search if price reverts again - it either
confirms within the search window or the day produces no signal.
"""
from __future__ import annotations

import pandas as pd

from app.strategy.swings import confirmed_swings_as_of, last_swing
from app.strategy.types import Direction, LiquiditySide, StructureEvent, StructureType, SwingPoint


def detect_bos_choch(
    candles_1m: pd.DataFrame,
    liquidity_side: LiquiditySide,
    window: int,
    search_bar_limit: int,
) -> StructureEvent | None:
    """`candles_1m` must start at/after the liquidity-interaction bar. Scans
    forward bar by bar, without look-ahead, for the first swing break in
    either direction. Returns None if nothing qualifies within
    `search_bar_limit` bars."""
    n = min(len(candles_1m), search_bar_limit)
    if n < window * 2 + 1:
        return None

    closes = candles_1m["Close"].to_numpy()
    pending: tuple[Direction, SwingPoint] | None = None  # unconfirmed CHOCH candidate

    for i in range(window, n):
        swings_so_far = confirmed_swings_as_of(candles_1m, window, i - 1)
        close = float(closes[i])
        ts = candles_1m.index[i]

        swing_high = last_swing(swings_so_far, "high")
        swing_low = last_swing(swings_so_far, "low")
        broke_high = swing_high is not None and close > swing_high.price
        broke_low = swing_low is not None and close < swing_low.price

        if pending is None:
            # BOS: the first break continues the direction that took liquidity
            if liquidity_side is LiquiditySide.HIGH and broke_high:
                return StructureEvent(StructureType.BOS, Direction.BUY, liquidity_side, ts, swing_high)
            if liquidity_side is LiquiditySide.LOW and broke_low:
                return StructureEvent(StructureType.BOS, Direction.SELL, liquidity_side, ts, swing_low)
            # otherwise, a break the OTHER way opens a CHOCH candidate, not yet a signal
            if liquidity_side is LiquiditySide.HIGH and broke_low:
                pending = (Direction.SELL, swing_low)
            elif liquidity_side is LiquiditySide.LOW and broke_high:
                pending = (Direction.BUY, swing_high)
        else:
            reversal_direction, choch_swing = pending
            if reversal_direction is Direction.SELL and broke_low and swing_low.index > choch_swing.index:
                return StructureEvent(StructureType.CHOCH, Direction.SELL, liquidity_side, ts, swing_low)
            if reversal_direction is Direction.BUY and broke_high and swing_high.index > choch_swing.index:
                return StructureEvent(StructureType.CHOCH, Direction.BUY, liquidity_side, ts, swing_high)

    return None
