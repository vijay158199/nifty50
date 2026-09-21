"""RSI bias detection - stage 1 of the pipeline when
``settings.bias_source == "RSI"``.

This replaces the first-candle liquidity sweep (see breakout_sweep.py) as
the way the day's directional bias is established. The rest of the pipeline
is unchanged: whatever bias this produces still has to be confirmed by a
market structure shift before any trade is taken.

Two readings of "RSI breaking the top/bottom line" are implemented, because
they are genuinely different strategies and the spec is ambiguous between
them. Pick with ``settings.rsi_bias_mode``:

- ``"reversal"`` (default) - an exhaustion read. RSI dips to/below the
  oversold line and then crosses back UP through it: sellers are spent,
  bias turns bullish. Mirrored for a drop back down through overbought.
- ``"momentum"`` - a strength read. RSI pushes UP through the overbought
  line: buyers are in control, bias turns bullish. Mirrored below oversold.

These produce opposite trades on the same chart, so they are deliberately
not blended. Run a backtest of each over the same window to settle which
one the strategy actually means.

Mapping onto the existing structure stage
-----------------------------------------
``structure.detect_bos_choch`` is written in terms of a LiquiditySide, and
derives BOS (continuation) vs CHOCH (reversal) relative to it. A bullish
bias maps to ``LiquiditySide.LOW``, which makes a confirmed CHOCH come out
as a BUY - i.e. the bullish market structure shift we're waiting for - while
plain continuation downward comes out as a BOS SELL and is skipped under
``settings.require_choch_only``. Mirrored for a bearish bias. So the mapping
is bias direction -> the side whose *reversal* is the trade we want, not an
assertion that any liquidity was actually taken there.
"""
from __future__ import annotations

import datetime as dt

import numpy as np
import pandas as pd

from app.strategy.types import Direction, LiquiditySide, TriggerEvent, TriggerType


def _wilder_average(values: pd.Series, period: int) -> pd.Series:
    """Wilder's smoothing: seed with the simple mean of the first `period`
    observations, then ``avg = (prev * (period - 1) + x) / period``.

    Deliberately not a plain ``ewm(alpha=1/period)``: without the SMA seed
    the first several dozen values differ from what TradingView, and every
    other charting package, will show - which matters a lot when the whole
    signal is "did RSI cross 30".
    """
    arr = values.to_numpy(dtype=float)
    out = np.full(len(arr), np.nan)
    if len(arr) <= period:
        return pd.Series(out, index=values.index)

    # arr[0] is NaN (it comes from .diff()), so the first `period` real
    # observations are arr[1..period] inclusive.
    seed = np.nanmean(arr[1 : period + 1])
    out[period] = seed
    for i in range(period + 1, len(arr)):
        out[i] = (out[i - 1] * (period - 1) + arr[i]) / period
    return pd.Series(out, index=values.index)


def wilder_rsi(closes: pd.Series, period: int = 14) -> pd.Series:
    """Classic Wilder RSI. Returns a series aligned to `closes`, NaN until
    enough bars exist to seed the averages."""
    delta = closes.astype(float).diff()
    gain = delta.clip(lower=0.0)
    loss = (-delta).clip(lower=0.0)

    avg_gain = _wilder_average(gain, period)
    avg_loss = _wilder_average(loss, period)

    rsi = pd.Series(np.nan, index=closes.index, dtype=float)
    valid = avg_gain.notna() & avg_loss.notna()

    # Both flat (a dead-straight series) has no meaningful RS - call it 50,
    # the neutral midpoint, rather than propagating 0/0.
    flat = valid & (avg_gain == 0) & (avg_loss == 0)
    # All gain and no loss is RSI 100 by definition; guard it explicitly so
    # the division never produces an inf.
    no_loss = valid & (avg_loss == 0) & (avg_gain > 0)
    normal = valid & (avg_loss > 0)

    rs = avg_gain[normal] / avg_loss[normal]
    rsi[normal] = 100.0 - (100.0 / (1.0 + rs))
    rsi[no_loss] = 100.0
    rsi[flat] = 50.0
    return rsi


def _bias_to_liquidity_side(direction: Direction) -> LiquiditySide:
    """See the module docstring: a bullish bias is expressed as the LOW side,
    because that is the side whose CHOCH resolves to a BUY."""
    return LiquiditySide.LOW if direction is Direction.BUY else LiquiditySide.HIGH


def find_rsi_trigger(
    candles: pd.DataFrame,
    *,
    period: int = 14,
    overbought: float = 70.0,
    oversold: float = 30.0,
    mode: str = "reversal",
    scan_start: dt.time | None = None,
) -> TriggerEvent | None:
    """Scan `candles` for the first RSI line break at or after `scan_start`,
    and return it as a TriggerEvent carrying the resulting bias.

    RSI is computed over the WHOLE frame so it is properly warmed up by the
    time the scan window opens - only the *scan* is restricted to bars at or
    after `scan_start`, not the calculation. A cross is detected between
    consecutive bars, so the first scanned bar can only fire if the bar
    immediately before it is also present in the frame.

    Returns None when no qualifying cross happens.
    """
    if candles.empty or "Close" not in candles:
        return None
    if mode not in ("reversal", "momentum"):
        raise ValueError(f"rsi_bias_mode must be 'reversal' or 'momentum', got {mode!r}")
    if not 0 < oversold < overbought < 100:
        raise ValueError(f"Need 0 < oversold < overbought < 100, got {oversold} / {overbought}")

    rsi = wilder_rsi(candles["Close"], period)

    for i in range(1, len(candles)):
        ts = candles.index[i]
        if scan_start is not None and ts.time() < scan_start:
            continue

        prev, curr = rsi.iloc[i - 1], rsi.iloc[i]
        if pd.isna(prev) or pd.isna(curr):
            continue

        direction = trigger_type = None
        if mode == "reversal":
            # Exhaustion: back up through the oversold line = buying pressure.
            if prev <= oversold < curr:
                direction, trigger_type = Direction.BUY, TriggerType.RSI_OS_EXIT
            elif prev >= overbought > curr:
                direction, trigger_type = Direction.SELL, TriggerType.RSI_OB_EXIT
        else:
            # Momentum: pushing up through the overbought line = buying pressure.
            if prev <= overbought < curr:
                direction, trigger_type = Direction.BUY, TriggerType.RSI_OB_PUSH
            elif prev >= oversold > curr:
                direction, trigger_type = Direction.SELL, TriggerType.RSI_OS_PUSH

        if direction is None:
            continue

        return TriggerEvent(
            liquidity_side=_bias_to_liquidity_side(direction),
            trigger_type=trigger_type,
            trigger_time=ts,
            trigger_candle_close=float(candles.iloc[i]["Close"]),
            rsi_value=float(curr),
            rsi_previous=float(prev),
            bias_direction=direction,
        )

    return None
