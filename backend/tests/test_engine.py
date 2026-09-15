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


def _triggering_30m_and_1m(start: dt.datetime):
    """A first 30m candle whose high (101) is touched by the very first 1m
    bar (settings.first_candle_minutes=60 later, matching find_trigger's
    real logic) - trigger.trigger_time lands exactly at primary_1m.index[0]
    so `onward_1m` is the whole series, keeping index-based structure-event
    timestamps used by tests predictable. Isolates the skip_no_fvg loop
    itself rather than needing a fully realistic BOS/CHOCH+FVG fixture."""
    idx30 = pd.date_range(start=start, periods=1, freq="30min")
    primary_30m = pd.DataFrame({"Open": 100, "High": 101, "Low": 99, "Close": 100, "Volume": 0.0}, index=idx30)
    primary_30m.index.name = "ts"

    trigger_start = start + dt.timedelta(minutes=60)  # matches settings.first_candle_minutes default
    idx1 = pd.date_range(start=trigger_start, periods=60, freq="1min")
    primary_1m = pd.DataFrame(
        {"Open": 102, "High": 102.5, "Low": 101.5, "Close": 102, "Volume": 0.0}, index=idx1
    )
    primary_1m.index.name = "ts"
    return primary_30m, primary_1m


def test_run_day_stops_at_first_no_entry_structure_by_default(monkeypatch):
    """skip_no_fvg_structure=False (the default): a BOS/CHOCH that confirms
    but has no FVG entry ends the day right there - detect_bos_choch is
    never even asked for a second event."""
    from app.strategy import engine
    from app.strategy.types import Direction, LiquiditySide, StructureEvent, StructureType, SwingPoint, TradeStatus

    day = dt.date(2026, 1, 5)
    start = dt.datetime(2026, 1, 5, 9, 15)
    primary_30m, primary_1m = _triggering_30m_and_1m(start)

    calls = {"n": 0}

    def fake_detect(*args, **kwargs):
        calls["n"] += 1
        return StructureEvent(
            StructureType.BOS, Direction.BUY, LiquiditySide.HIGH, primary_1m.index[5],
            SwingPoint(primary_1m.index[0], 101.0, "high", 0),
        )

    monkeypatch.setattr(engine.structure_mod, "detect_bos_choch", fake_detect)
    monkeypatch.setattr(engine.entries_mod, "build_entry_zones", lambda *a, **k: None)

    result = engine.run_day(day, primary_30m, primary_1m, primary_1m, skip_no_fvg_structure=False)

    assert result.status is TradeStatus.NO_SETUP
    assert calls["n"] == 1  # never looked for a second structure event


def test_run_day_skips_no_entry_structure_and_takes_the_next_one(monkeypatch):
    """skip_no_fvg_structure=True: the first BOS/CHOCH has no FVG (zones is
    None), so the loop advances past it and takes the SECOND one, which
    does have a valid entry."""
    from app.strategy import engine
    from app.strategy.types import (
        Direction, EntrySignal, EntryType, LiquiditySide, RiskPlan, StructureEvent, StructureType, SwingPoint, TradeStatus,
    )

    day = dt.date(2026, 1, 5)
    start = dt.datetime(2026, 1, 5, 9, 15)
    primary_30m, primary_1m = _triggering_30m_and_1m(start)

    first_event = StructureEvent(
        StructureType.BOS, Direction.BUY, LiquiditySide.HIGH, primary_1m.index[5],
        SwingPoint(primary_1m.index[0], 101.0, "high", 0),
    )
    second_event = StructureEvent(
        StructureType.BOS, Direction.BUY, LiquiditySide.HIGH, primary_1m.index[10],
        SwingPoint(primary_1m.index[6], 101.5, "high", 6),
    )
    detect_calls = {"n": 0}

    def fake_detect(candles, *args, **kwargs):
        detect_calls["n"] += 1
        # first call sees the full window (starts before first_event.ts);
        # after the skip, the loop passes a window starting AFTER it.
        if candles.index[0] <= first_event.ts:
            return first_event
        return second_event

    class _FakeZones:
        leg_high = 103.0
        leg_low = 101.0
        leg_candle_count = 4

    def fake_build_zones(candles, structure_event, window):
        return None if structure_event is first_event else _FakeZones()

    def fake_scan(candles, structure_event, zones, priority, search_bar_limit):
        if structure_event is second_event:
            return EntrySignal(EntryType.FAIR_VALUE_GAP, Direction.BUY, primary_1m.index[12], 102.0, "fvg")
        return None

    monkeypatch.setattr(engine.structure_mod, "detect_bos_choch", fake_detect)
    monkeypatch.setattr(engine.entries_mod, "build_entry_zones", fake_build_zones)
    monkeypatch.setattr(engine.entries_mod, "scan_for_entry", fake_scan)

    result = engine.run_day(day, primary_30m, primary_1m, primary_1m, skip_no_fvg_structure=True)

    assert detect_calls["n"] == 2
    assert result.structure is second_event
    assert result.entry is not None and result.entry.entry_price == 102.0
    assert any("skipping 1" in n or "skipped 1" in n for n in result.notes)
