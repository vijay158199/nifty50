"""Builds session-aligned 30-minute candles from finer-resolution data.

Yahoo's native interval="30m" bars are NOT aligned to NSE's 09:15 session
open (they bin from 09:30), which would silently drop the real first 15
minutes of the first 30m candle. Instead we always fetch 1m (preferred) or
5m (fallback, for days outside the 1m lookback window) candles and resample
them ourselves with an explicit origin at the session start, so the first
bin is always exactly 09:15-09:45.
"""
from __future__ import annotations

import datetime as dt

import pandas as pd


def resample_ohlc(candles: pd.DataFrame, minutes: int, session_start: dt.datetime) -> pd.DataFrame:
    if candles.empty:
        return candles
    origin = session_start.replace(tzinfo=None) if session_start.tzinfo else session_start
    resampled = candles.resample(
        f"{minutes}min", origin=origin, closed="left", label="left"
    ).agg(
        {"Open": "first", "High": "max", "Low": "min", "Close": "last", "Volume": "sum"}
    )
    return resampled.dropna(subset=["Open", "High", "Low", "Close"])
