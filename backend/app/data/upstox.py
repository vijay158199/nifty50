"""Upstox API v3 market-data client.

Replaces Yahoo/yfinance as the candle source (explicit user instruction,
2026-09-21). The reason it matters beyond preference: Yahoo only serves
1-minute candles for the trailing ~30 days, which silently degraded any
backtest older than that to 5-minute resolution. Upstox serves 1-minute
candles from January 2022, so a 1m strategy can finally be backtested over
years rather than weeks.

Two endpoints are used:

  historical  /v3/historical-candle/{key}/minutes/{n}/{to}/{from}
  intraday    /v3/historical-candle/intraday/{key}/minutes/{n}

The historical endpoint covers completed days and is capped at one month per
request, so longer ranges are paged a month at a time. It does not include
the current session - today's candles come from the intraday endpoint, which
is why `fetch_candles` may combine the two.

Authentication
--------------
Both endpoints are called with a Bearer token from
``settings.upstox_access_token``. Use an **Analytics Token**, not a normal
access token: a normal one expires at 3:30 AM IST every day with no refresh
mechanism, which would mean a manual browser login before every session. The
Analytics Token is valid for a year, is generated without an OAuth redirect,
and is read-only - which is all this system needs, since it never places an
order through Upstox.

NOTE: the request/response handling here is written against Upstox's
published API documentation. It has not been exercised against the live API
from the development sandbox, whose network policy blocks api.upstox.com, so
treat the first real run as the actual verification.
"""
from __future__ import annotations

import datetime as dt
from urllib.parse import quote

import pandas as pd
import requests
from tenacity import retry, retry_if_exception_type, stop_after_attempt, wait_exponential

from app.config import settings

BASE_URL = "https://api.upstox.com/v3"

# Upstox serves 1-minute candles no further back than this.
EARLIEST_1M_DATE = dt.date(2022, 1, 1)
# The historical endpoint's documented per-request ceiling.
MAX_RANGE_DAYS = 30

_COLUMNS = ["Open", "High", "Low", "Close", "Volume"]


class UpstoxError(RuntimeError):
    pass


class UpstoxAuthError(UpstoxError):
    """Raised for 401/403 - almost always an expired or missing token, which
    is worth distinguishing because the fix is 'generate a new Analytics
    Token', not 'retry later'."""


def _headers() -> dict[str, str]:
    token = settings.upstox_access_token
    if not token:
        raise UpstoxAuthError(
            "No Upstox token configured. Generate an Analytics Token from the Upstox "
            "developer console and set NIFTY_UPSTOX_ACCESS_TOKEN."
        )
    return {"Accept": "application/json", "Authorization": f"Bearer {token}"}


def candles_to_frame(candles: list[list]) -> pd.DataFrame:
    """Turn Upstox's raw candle array into the OHLCV frame the rest of the
    app expects: naive IST index named "ts", ascending, no duplicates.

    Each row is [timestamp, open, high, low, close, volume, open_interest].
    Upstox returns them newest-first; everything downstream assumes
    chronological order, so they are always re-sorted rather than trusted.
    """
    if not candles:
        return pd.DataFrame(columns=_COLUMNS, index=pd.DatetimeIndex([], name="ts"))

    frame = pd.DataFrame(
        [row[:6] for row in candles],
        columns=["ts", "Open", "High", "Low", "Close", "Volume"],
    )
    # The timestamps carry a +05:30 offset; convert to naive IST wall-clock
    # so they line up with the rest of the system (and with the cache).
    ts = pd.to_datetime(frame["ts"], format="ISO8601", utc=True)
    frame.index = ts.dt.tz_convert("Asia/Kolkata").dt.tz_localize(None)
    frame.index.name = "ts"
    frame = frame.drop(columns=["ts"]).astype(float)
    frame = frame[~frame.index.duplicated(keep="last")]
    return frame.sort_index()[_COLUMNS]


@retry(
    stop=stop_after_attempt(settings.max_fetch_retries),
    wait=wait_exponential(multiplier=1, min=2, max=20),
    retry=retry_if_exception_type(requests.RequestException),
    reraise=True,
)
def _get(url: str) -> list[list]:
    response = requests.get(url, headers=_headers(), timeout=30)
    if response.status_code in (401, 403):
        raise UpstoxAuthError(
            f"Upstox rejected the token ({response.status_code}). An Analytics Token lasts a "
            "year; a standard access token expires at 3:30 AM IST daily. Body: "
            f"{response.text[:200]}"
        )
    response.raise_for_status()
    payload = response.json()
    if payload.get("status") != "success":
        raise UpstoxError(f"Upstox returned status={payload.get('status')!r}: {str(payload)[:300]}")
    return payload.get("data", {}).get("candles", []) or []


def _encode(instrument_key: str) -> str:
    """`NSE_INDEX|Nifty 50` has both a pipe and a space, so it has to be
    percent-encoded to survive being a path segment."""
    return quote(instrument_key, safe="")


def fetch_historical(
    instrument_key: str, interval_minutes: int, start: dt.date, end: dt.date
) -> pd.DataFrame:
    """Completed sessions only, paged around the one-month request cap."""
    if start > end:
        return candles_to_frame([])
    if interval_minutes == 1 and start < EARLIEST_1M_DATE:
        start = EARLIEST_1M_DATE
        if start > end:
            return candles_to_frame([])

    key = _encode(instrument_key)
    chunks: list[pd.DataFrame] = []
    window_start = start
    while window_start <= end:
        window_end = min(window_start + dt.timedelta(days=MAX_RANGE_DAYS - 1), end)
        url = (
            f"{BASE_URL}/historical-candle/{key}/minutes/{interval_minutes}"
            f"/{window_end:%Y-%m-%d}/{window_start:%Y-%m-%d}"
        )
        chunks.append(candles_to_frame(_get(url)))
        window_start = window_end + dt.timedelta(days=1)

    if not chunks:
        return candles_to_frame([])
    merged = pd.concat(chunks)
    merged = merged[~merged.index.duplicated(keep="last")]
    return merged.sort_index()


def fetch_intraday(instrument_key: str, interval_minutes: int) -> pd.DataFrame:
    """The current session only - the historical endpoint excludes today."""
    url = f"{BASE_URL}/historical-candle/intraday/{_encode(instrument_key)}/minutes/{interval_minutes}"
    return candles_to_frame(_get(url))


def fetch_candles(
    instrument_key: str, interval_minutes: int, start: dt.datetime, end: dt.datetime, today: dt.date
) -> pd.DataFrame:
    """Whole range, stitching the historical and intraday endpoints as
    needed, then trimmed to the exact [start, end] window asked for."""
    start_date, end_date = start.date(), end.date()
    frames = []

    historical_end = min(end_date, today - dt.timedelta(days=1))
    if start_date <= historical_end:
        frames.append(fetch_historical(instrument_key, interval_minutes, start_date, historical_end))
    if start_date <= today <= end_date:
        frames.append(fetch_intraday(instrument_key, interval_minutes))

    frames = [f for f in frames if not f.empty]
    if not frames:
        return candles_to_frame([])

    merged = pd.concat(frames)
    merged = merged[~merged.index.duplicated(keep="last")].sort_index()
    return merged.loc[(merged.index >= start) & (merged.index <= end)]
