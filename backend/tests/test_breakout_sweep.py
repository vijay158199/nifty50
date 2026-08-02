import datetime as dt

from tests.conftest import make_candles


def _first_candle_30m(session_start):
    """A single completed first 30-min candle: range [100, 110]."""
    return make_candles([(102, 110, 100, 105)], session_start, 30)


def test_first_touch_is_high_breakout(session_start):
    from app.strategy.breakout_sweep import find_trigger
    from app.strategy.types import LiquiditySide, TriggerType

    candles_30m = _first_candle_30m(session_start)
    start_1m = session_start + dt.timedelta(minutes=30)  # 1m data starts once the first candle closes
    rows = [
        (105, 107, 104, 106),  # inside range
        (106, 108, 105, 107),  # inside range
        (107, 112, 106, 111),  # touches & closes above 110 -> BREAKOUT High
    ]
    candles_1m = make_candles(rows, start_1m, 1)

    trigger = find_trigger(candles_30m, candles_1m, candle_minutes=30)

    assert trigger is not None
    assert trigger.liquidity_side is LiquiditySide.HIGH
    assert trigger.trigger_type is TriggerType.BREAKOUT
    assert trigger.trigger_time == candles_1m.index[2]
    assert trigger.first_candle_high == 110
    assert trigger.first_candle_low == 100


def test_first_touch_is_low_sweep(session_start):
    from app.strategy.breakout_sweep import find_trigger
    from app.strategy.types import LiquiditySide, TriggerType

    candles_30m = _first_candle_30m(session_start)
    start_1m = session_start + dt.timedelta(minutes=30)
    rows = [
        (105, 106, 104, 105),
        (105, 106, 99, 100.5),  # wicks to 99 (< 100) but closes back at 100.5 -> SWEEP Low
    ]
    candles_1m = make_candles(rows, start_1m, 1)

    trigger = find_trigger(candles_30m, candles_1m, candle_minutes=30)

    assert trigger.liquidity_side is LiquiditySide.LOW
    assert trigger.trigger_type is TriggerType.SWEEP


def test_bars_still_forming_the_first_candle_are_ignored(session_start):
    """A 1m bar before the first 30m candle actually closes is part of that
    same candle, not a genuine touch after the level was established, and
    must never count as the interaction."""
    from app.strategy.breakout_sweep import find_trigger

    candles_30m = _first_candle_30m(session_start)
    candles_1m = make_candles([(102, 110, 100, 105)], session_start + dt.timedelta(minutes=5), 1)

    assert find_trigger(candles_30m, candles_1m, candle_minutes=30) is None


def test_returns_none_when_neither_level_touched(session_start):
    from app.strategy.breakout_sweep import find_trigger

    candles_30m = _first_candle_30m(session_start)
    start_1m = session_start + dt.timedelta(minutes=30)
    rows = [(105, 106, 104, 105), (105, 107, 104, 106)]
    candles_1m = make_candles(rows, start_1m, 1)

    assert find_trigger(candles_30m, candles_1m, candle_minutes=30) is None


def test_returns_none_with_empty_inputs(session_start):
    from app.strategy.breakout_sweep import find_trigger

    empty = make_candles([], session_start, 1)
    candles_30m = _first_candle_30m(session_start)
    assert find_trigger(candles_30m, empty) is None
    assert find_trigger(empty, candles_30m) is None
