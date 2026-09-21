"""Candle fetching: pull OHLC from the configured provider with
retry/backoff, normalize to IST naive timestamps, and transparently merge
with the local cache.

Provider is `settings.data_provider`:

- **upstox** (default) - 1-minute candles from January 2022, so a 1m
  strategy can be backtested over years. See app/data/upstox.py.
- **yfinance** - kept selectable so runs cached under it stay reproducible.
  Its limits, which are why it is no longer the default: 1m only for the
  trailing ~30 days, 5m/30m for ~60, and no native 3m (derived by
  resampling 1m, so it inherits 1m's window).

Either way, once a day has been fetched and cached it remains available
locally even after the provider's own window moves past it.
"""
from __future__ import annotations

import datetime as dt
from dataclasses import dataclass

import pandas as pd
import yfinance as yf
from tenacity import retry, stop_after_attempt, wait_exponential, retry_if_exception_type

from app.config import settings
from app.data import cache
from app.data import upstox as upstox_mod
from app.data.calendar import IST, session_bounds
from app.data.resample import resample_ohlc
from app.models.db import log_event

_MAX_LOOKBACK_DAYS = {
    "1m": settings.yfinance_1m_lookback_days,
    "5m": settings.yfinance_5m_lookback_days,
}


class DataFetchError(RuntimeError):
    pass


def _normalize_index(df: pd.DataFrame) -> pd.DataFrame:
    """Convert yfinance's tz-aware index to naive IST wall-clock timestamps."""
    if df.empty:
        return df
    idx = df.index
    if idx.tz is None:
        idx = idx.tz_localize("UTC")
    idx = idx.tz_convert(IST).tz_localize(None)
    df = df.copy()
    df.index = idx
    df.index.name = "ts"
    # yfinance sometimes returns a MultiIndex column set for single tickers
    if isinstance(df.columns, pd.MultiIndex):
        df.columns = df.columns.get_level_values(0)
    return df[["Open", "High", "Low", "Close", "Volume"]]


@retry(
    stop=stop_after_attempt(settings.max_fetch_retries),
    wait=wait_exponential(multiplier=1, min=2, max=20),
    retry=retry_if_exception_type(Exception),
    reraise=True,
)
def _download(symbol: str, interval: str, start: dt.datetime, end: dt.datetime) -> pd.DataFrame:
    """Yahoo path. Upstox has its own retry/paging, so it does not come
    through here - see _download_upstox."""
    df = yf.download(
        symbol,
        interval=interval,
        start=start,
        end=end,
        progress=False,
        auto_adjust=False,
        prepost=False,
    )
    return _normalize_index(df)


def _download_upstox(symbol: str, interval: str, start: dt.datetime, end: dt.datetime) -> pd.DataFrame:
    instrument_key = settings.upstox_instrument_keys.get(symbol)
    if instrument_key is None:
        raise upstox_mod.UpstoxError(
            f"No Upstox instrument key mapped for {symbol!r}. Add one to "
            "settings.upstox_instrument_keys."
        )
    return upstox_mod.fetch_candles(
        instrument_key,
        INTERVAL_MINUTES.get(interval, 1),
        start,
        end,
        today=dt.datetime.now(IST).date(),
    )


def _using_upstox() -> bool:
    return settings.data_provider.lower() == "upstox"


def earliest_fetchable_date(interval: str, today: dt.date) -> dt.date:
    """The oldest date the configured provider can still serve for this
    interval. Upstox's 1m history is anchored to a fixed start date; Yahoo's
    is a rolling window measured back from today."""
    if _using_upstox():
        return upstox_mod.EARLIEST_1M_DATE
    return today - dt.timedelta(days=_MAX_LOOKBACK_DAYS.get(interval, 30) - 1)


def fetch_and_cache(symbol: str, interval: str, start: dt.datetime, end: dt.datetime) -> pd.DataFrame:
    """Fetch a range from the configured provider (clamped to what it can
    actually serve), persist to cache, and return the merged cached view for
    the full requested range. A failed fetch is logged, not raised - the
    cached view is still returned so a provider outage degrades rather than
    breaks the dashboard."""
    today = dt.datetime.now(IST).date()
    fetch_start = max(start.date(), earliest_fetchable_date(interval, today))
    fetch_end = end.date()

    if fetch_start <= fetch_end:
        provider = "upstox" if _using_upstox() else "yfinance"
        try:
            if _using_upstox():
                fresh = _download_upstox(
                    symbol,
                    interval,
                    dt.datetime.combine(fetch_start, dt.time.min),
                    dt.datetime.combine(fetch_end, dt.time.max),
                )
            else:
                fresh = _download(
                    symbol,
                    interval,
                    dt.datetime.combine(fetch_start, dt.time.min),
                    dt.datetime.combine(fetch_end + dt.timedelta(days=1), dt.time.min),
                )
            if not fresh.empty:
                cache.store_candles(symbol, interval, fresh)
        except Exception as exc:  # noqa: BLE001 - log and fall back to cache
            log_event(
                "ERROR",
                "data.fetcher",
                f"{provider} fetch failed for {symbol} {interval} "
                f"[{fetch_start}..{fetch_end}]: {exc}",
            )

    return cache.load_candles(symbol, interval, start, end)


def get_candles(symbol: str, interval: str, start: dt.datetime, end: dt.datetime) -> pd.DataFrame:
    """Primary entry point used by the strategy engine/backtester/live monitor."""
    return fetch_and_cache(symbol, interval, start, end)


INTERVAL_MINUTES = {"1m": 1, "2m": 2, "3m": 3, "5m": 5}


@dataclass
class SessionData:
    fine: pd.DataFrame          # candles at the requested structure_interval (or a fallback) for the session
    resolution: str             # "1m" | "2m" | "3m" | "5m" - the resolution actually returned
    candles_30m: pd.DataFrame   # our own session-aligned 30m candles, derived from `fine`
    reduced_resolution: bool    # True when the requested structure_interval wasn't available and we fell back coarser


def get_session_data(symbol: str, date: dt.date, structure_interval: str | None = None) -> SessionData:
    """Fetch one trading day's candles at `structure_interval` (falls back
    to `settings.structure_interval` if not given - e.g. live polling uses
    the configured default, while each backtest run can request its own),
    then derive session-aligned 30m candles ourselves (see resample.py for
    why we don't use Yahoo's native 30m bars).

    Yahoo only has native "1m" and "5m" intervals. Anything else ("2m",
    "3m", ...) is derived by resampling 1-minute data ourselves (same
    technique as the 30m candles) - meaning those interim intervals inherit
    1m's ~30-day lookback window, not 5m's ~60-day one. Any non-"5m" request
    falls back to 5m for days outside that ~30-day window (5m's own window
    is ~60 days, matching settings.backtest_lookback_days); this fallback
    never applies to "5m" itself, since there's nothing coarser to drop to."""
    requested = structure_interval or settings.structure_interval
    start, end = session_bounds(date)
    today = dt.datetime.now(IST).date()

    if requested == "5m":
        reduced = False
        native_interval = "5m"
    else:
        # every other interval needs native 1m data as its base. Upstox
        # serves 1m all the way back to 2022, so there is nothing to reduce
        # to unless the date predates that entirely.
        if _using_upstox():
            reduced = date < upstox_mod.EARLIEST_1M_DATE
        else:
            reduced = (today - date).days >= settings.yfinance_1m_lookback_days
        native_interval = "5m" if reduced else "1m"

    base = get_candles(symbol, native_interval, start, end)
    if base.empty and native_interval == "1m":
        # 1m window edge case (e.g. just rolled past 30 days) - fall back to 5m
        reduced = True
        native_interval = "5m"
        base = get_candles(symbol, native_interval, start, end)

    if requested not in ("1m", "5m") and native_interval == "1m":
        minutes = INTERVAL_MINUTES[requested]
        fine = resample_ohlc(base, minutes, start)
        fine = _drop_unclosed_candle(fine, minutes)
        resolution = requested
    else:
        # either got exactly what was requested (1m or 5m), or wanted a
        # derived interval but 1m wasn't available to derive it from -
        # already flagged reduced
        fine = base
        resolution = native_interval
        if requested not in ("1m", "5m") and native_interval == "5m":
            reduced = True

    candles_30m = resample_ohlc(base, settings.first_candle_minutes, start)
    candles_30m = _drop_unclosed_candle(candles_30m, settings.first_candle_minutes)
    return SessionData(fine=fine, resolution=resolution, candles_30m=candles_30m, reduced_resolution=reduced)


def _drop_unclosed_candle(candles_30m: pd.DataFrame, candle_minutes: int) -> pd.DataFrame:
    """During a live poll, the most recent 30m bin may still be forming (its
    "Close" is really just the latest price so far, not a real close). Left
    in, that lets find_trigger() mistake an in-progress candle for a
    confirmed breakout - trim it off until its window has actually elapsed.
    A no-op for backtests, since every bin in a fully-historical day has
    already closed by the time "now" is evaluated."""
    if candles_30m.empty:
        return candles_30m
    now_naive = dt.datetime.now(IST).replace(tzinfo=None)
    last_start = candles_30m.index[-1]
    candle_close = last_start + dt.timedelta(minutes=candle_minutes)
    if candle_close > now_naive:
        return candles_30m.iloc[:-1]
    return candles_30m
