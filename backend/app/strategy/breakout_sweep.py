"""Detects the first candle of the session (settings.first_candle_minutes -
60m by default), then the first interaction (by settings.structure_interval
price action, not waiting on another first-candle-length bar to close) with
either of that candle's liquidity levels - its high or its low.

This does NOT decide a trade direction. It only flags WHICH level was
touched first; whether that resolves into a bullish or bearish trade is
determined afterward by market structure (BOS/CHOCH - see
structure.detect_bos_choch)."""
from __future__ import annotations

import datetime as dt

import pandas as pd

from app.strategy.types import LiquiditySide, TriggerEvent, TriggerType


def find_trigger(candles_30m: pd.DataFrame, candles_1m: pd.DataFrame, candle_minutes: int = 30) -> TriggerEvent | None:
    """`candles_30m` supplies the first candle's high/low (only its first
    row is used - a single completed candle is enough, no need to wait for
    a second 30-min candle to exist). `candles_1m` is scanned bar-by-bar,
    starting right after the first 30-min candle ENDS (its start + 30min,
    not its start), for the first bar whose range touches either level.
    Returns None if neither level has been touched yet in the available data."""
    if candles_30m.empty or candles_1m.empty:
        return None

    first = candles_30m.iloc[0]
    first_high, first_low = float(first["High"]), float(first["Low"])
    first_candle_end = candles_30m.index[0] + dt.timedelta(minutes=candle_minutes)

    onward = candles_1m[candles_1m.index >= first_candle_end]

    for ts, row in onward.iterrows():
        high, low, close = float(row["High"]), float(row["Low"]), float(row["Close"])
        touched_high = high >= first_high
        touched_low = low <= first_low

        if touched_high and touched_low:
            # a single bar spanning both levels (wide-range bar) - treat
            # whichever level the bar's open sits closer to as touched first
            open_px = float(row["Open"])
            touched_low = abs(open_px - first_low) <= abs(open_px - first_high)
            touched_high = not touched_low

        if touched_high:
            return TriggerEvent(
                liquidity_side=LiquiditySide.HIGH,
                trigger_type=TriggerType.BREAKOUT if close > first_high else TriggerType.SWEEP,
                trigger_time=ts,
                first_candle_high=first_high,
                first_candle_low=first_low,
                trigger_candle_close=close,
            )
        if touched_low:
            return TriggerEvent(
                liquidity_side=LiquiditySide.LOW,
                trigger_type=TriggerType.BREAKOUT if close < first_low else TriggerType.SWEEP,
                trigger_time=ts,
                first_candle_high=first_high,
                first_candle_low=first_low,
                trigger_candle_close=close,
            )

    return None
