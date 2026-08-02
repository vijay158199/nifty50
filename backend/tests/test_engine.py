import datetime as dt

import pandas as pd
import pytest


def _flat_1m(start: dt.datetime, n: int, price: float) -> pd.DataFrame:
    idx = pd.date_range(start=start, periods=n, freq="1min")
    df = pd.DataFrame(
        {"Open": price, "High": price + 0.5, "Low": price - 0.5, "Close": price, "Volume": 0.0}, index=idx
    )
    df.index.name = "ts"
    return df


def test_run_day_returns_no_setup_when_first_30m_candle_never_breaks(monkeypatch):
    from app.strategy.engine import run_day
    from app.strategy.types import TradeStatus

    day = dt.date(2026, 1, 5)
    start = dt.datetime(2026, 1, 5, 9, 15)

    # 30m candles that never break the first candle's [99, 101] range
    idx30 = pd.date_range(start=start, periods=4, freq="30min")
    primary_30m = pd.DataFrame(
        {"Open": 100, "High": 101, "Low": 99, "Close": 100, "Volume": 0.0}, index=idx30
    )
    primary_30m.index.name = "ts"

    primary_1m = _flat_1m(start, 120, 100.0)
    confirm_1m = _flat_1m(start, 120, 100.0)

    result = run_day(day, primary_30m, primary_1m, confirm_1m)

    assert result.status is TradeStatus.NO_SETUP
    assert result.trigger is None


def test_run_day_produces_no_setup_when_no_1m_data_at_all(monkeypatch):
    """Liquidity interaction detection now runs on 1m data (not 30m candle
    closes) - with none available at all, there's nothing to detect a
    touch against, so the engine must bail out cleanly rather than crash."""
    from app.strategy.engine import run_day
    from app.strategy.types import TradeStatus

    day = dt.date(2026, 1, 5)
    start = dt.datetime(2026, 1, 5, 9, 15)

    idx30 = pd.date_range(start=start, periods=3, freq="30min")
    primary_30m = pd.DataFrame(
        {
            "Open": [100, 100, 103],
            "High": [101, 101, 110],
            "Low": [99, 99, 102],
            "Close": [100, 100, 108],
            "Volume": 0.0,
        },
        index=idx30,
    )
    primary_30m.index.name = "ts"

    empty_1m = pd.DataFrame(columns=["Open", "High", "Low", "Close", "Volume"])

    result = run_day(day, primary_30m, empty_1m, empty_1m)

    assert result.status is TradeStatus.NO_SETUP
    assert result.trigger is None
