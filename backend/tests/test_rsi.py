import datetime as dt

import numpy as np
import pandas as pd
import pytest

from tests.conftest import make_candles


# Wilder's own worked example series, the one every charting package gets
# checked against. The expected RSI below is derived by hand in the test
# rather than copied from a published table - secondary sources disagree in
# the last decimal depending on how they round the running averages.
_WILDER_CLOSES = [
    44.34, 44.09, 44.15, 43.61, 44.33, 44.83, 45.10, 45.42, 45.84, 46.08,
    45.89, 46.03, 45.61, 46.28, 46.28, 46.00, 46.03, 46.41, 46.22, 45.64,
    46.21, 46.25, 45.71, 46.45, 45.78, 45.35, 44.03, 44.18, 44.22, 44.57,
    43.42, 42.66, 43.13,
]


def _naive_wilder_rsi(closes: list[float], period: int) -> list[float]:
    """Deliberately dumb, straight-from-the-definition reference: seed with
    the simple mean of the first `period` deltas, then smooth one bar at a
    time. Exists to catch vectorisation mistakes in the real implementation."""
    deltas = [closes[i] - closes[i - 1] for i in range(1, len(closes))]
    gains = [max(d, 0.0) for d in deltas]
    losses = [max(-d, 0.0) for d in deltas]

    out = [float("nan")] * len(closes)
    avg_g = sum(gains[:period]) / period
    avg_l = sum(losses[:period]) / period

    def to_rsi(g, l):
        if g == 0 and l == 0:
            return 50.0
        if l == 0:
            return 100.0
        return 100.0 - 100.0 / (1.0 + g / l)

    out[period] = to_rsi(avg_g, avg_l)
    for i in range(period + 1, len(closes)):
        avg_g = (avg_g * (period - 1) + gains[i - 1]) / period
        avg_l = (avg_l * (period - 1) + losses[i - 1]) / period
        out[i] = to_rsi(avg_g, avg_l)
    return out


def test_rsi_matches_wilders_worked_example():
    """The seed bar is fully hand-checkable. Over the first 14 deltas of
    Wilder's series the gains sum to 3.34 and the losses to 1.40, so
    RS = (3.34/14) / (1.40/14) = 2.3857 and RSI = 100 - 100/(1+RS) = 70.4641.
    Deriving it here instead of trusting a remembered figure is the whole
    point: this is the number the entire strategy's trigger depends on."""
    from app.strategy.rsi import wilder_rsi

    deltas = [_WILDER_CLOSES[i] - _WILDER_CLOSES[i - 1] for i in range(1, 15)]
    gains = sum(d for d in deltas if d > 0)
    losses = sum(-d for d in deltas if d < 0)
    assert gains == pytest.approx(3.34, abs=1e-9)
    assert losses == pytest.approx(1.40, abs=1e-9)
    expected = 100.0 - 100.0 / (1.0 + (gains / 14) / (losses / 14))
    assert expected == pytest.approx(70.4641, abs=1e-4)

    rsi = wilder_rsi(pd.Series(_WILDER_CLOSES), period=14)

    # Nothing before the seed bar - RSI is undefined, not zero.
    assert rsi.iloc[:14].isna().all()
    assert rsi.iloc[14] == pytest.approx(expected, abs=1e-6)
    # Wilder smoothing from there on.
    assert rsi.iloc[15] == pytest.approx(66.25, abs=0.01)
    assert rsi.iloc[16] == pytest.approx(66.48, abs=0.01)


def test_rsi_matches_a_naive_reference_on_a_random_walk():
    from app.strategy.rsi import wilder_rsi

    rng = np.random.default_rng(20260921)
    closes = list(100 + np.cumsum(rng.normal(0, 1.0, 300)))

    fast = wilder_rsi(pd.Series(closes), period=14).to_numpy()
    slow = np.array(_naive_wilder_rsi(closes, 14))

    np.testing.assert_allclose(fast[14:], slow[14:], rtol=1e-9, atol=1e-9)


def test_rsi_is_100_when_price_only_rises_and_0_when_it_only_falls():
    from app.strategy.rsi import wilder_rsi

    up = wilder_rsi(pd.Series([100 + i for i in range(40)]), period=14)
    down = wilder_rsi(pd.Series([100 - i for i in range(40)]), period=14)

    assert up.iloc[-1] == pytest.approx(100.0)
    assert down.iloc[-1] == pytest.approx(0.0)


def test_rsi_is_neutral_50_on_a_dead_flat_series():
    """A flat series has no gains and no losses - 0/0. It must come out as
    the neutral midpoint, not NaN or inf, or the trigger scan blows up."""
    from app.strategy.rsi import wilder_rsi

    flat = wilder_rsi(pd.Series([100.0] * 40), period=14)

    assert flat.iloc[-1] == pytest.approx(50.0)
    assert np.isfinite(flat.iloc[14:]).all()


def _decline_then_rally(start: dt.datetime) -> pd.DataFrame:
    """25 bars down hard (drives RSI below 30), then 15 bars up hard (drags
    it back through the line) - the shape a 'reversal' bias is looking for."""
    rows = []
    price = 200.0
    for _ in range(25):
        nxt = price - 2.0
        rows.append((price, price + 0.2, nxt - 0.2, nxt))
        price = nxt
    for _ in range(15):
        nxt = price + 3.0
        rows.append((price, nxt + 0.2, price - 0.2, nxt))
        price = nxt
    return make_candles(rows, start, 1)


def test_reversal_mode_fires_bullish_when_rsi_climbs_back_through_oversold():
    from app.strategy.rsi import find_rsi_trigger
    from app.strategy.types import Direction, LiquiditySide, TriggerType

    candles = _decline_then_rally(dt.datetime(2026, 1, 5, 9, 30))

    trigger = find_rsi_trigger(candles, period=14, overbought=70, oversold=30, mode="reversal")

    assert trigger is not None
    assert trigger.bias_direction is Direction.BUY
    assert trigger.trigger_type is TriggerType.RSI_OS_EXIT
    # A bullish bias is carried as the LOW side, because that is the side
    # whose CHOCH resolves to a BUY - see rsi.py's module docstring.
    assert trigger.liquidity_side is LiquiditySide.LOW
    assert trigger.rsi_previous <= 30 < trigger.rsi_value


def _neutral_then_decline(start: dt.datetime) -> pd.DataFrame:
    """20 bars of small alternating moves so RSI seeds around neutral, then a
    hard decline that genuinely crosses DOWN through the oversold line.

    The warm-up matters: without it the very first computable RSI value is
    already below 30, so there is no crossing to detect and momentum mode
    correctly finds nothing on the way down.
    """
    rows = []
    price = 200.0
    for i in range(20):
        nxt = price + (0.5 if i % 2 == 0 else -0.5)
        rows.append((price, max(price, nxt) + 0.1, min(price, nxt) - 0.1, nxt))
        price = nxt
    for _ in range(20):
        nxt = price - 2.0
        rows.append((price, price + 0.2, nxt - 0.2, nxt))
        price = nxt
    return make_candles(rows, start, 1)


def test_momentum_mode_reads_a_breakdown_as_bearish():
    """The mirror of the reversal test: price driving DOWN through the
    oversold line is selling *strength*, so momentum mode goes short where
    reversal mode would be waiting to buy exhaustion."""
    from app.strategy.rsi import find_rsi_trigger
    from app.strategy.types import Direction, LiquiditySide, TriggerType

    candles = _neutral_then_decline(dt.datetime(2026, 1, 5, 9, 30))

    trigger = find_rsi_trigger(candles, period=14, overbought=70, oversold=30, mode="momentum")

    assert trigger is not None
    assert trigger.bias_direction is Direction.SELL
    assert trigger.trigger_type is TriggerType.RSI_OS_PUSH
    assert trigger.liquidity_side is LiquiditySide.HIGH
    assert trigger.rsi_previous >= 30 > trigger.rsi_value


def test_the_two_modes_disagree_on_the_same_candles():
    """The reason both are implemented: on one chart they produce opposite
    trades. Nothing should ever quietly blend them."""
    from app.strategy.rsi import find_rsi_trigger

    candles = _decline_then_rally(dt.datetime(2026, 1, 5, 9, 30))

    reversal = find_rsi_trigger(candles, mode="reversal")
    momentum = find_rsi_trigger(candles, mode="momentum")

    assert reversal is not None and momentum is not None
    assert reversal.trigger_type is not momentum.trigger_type


def test_scan_start_ignores_a_cross_that_happens_too_early():
    """The cross is at ~09:40 here. Scanning from 09:30 sees it; scanning
    from 10:00 must not, even though RSI is computed over the same bars."""
    from app.strategy.rsi import find_rsi_trigger

    candles = _decline_then_rally(dt.datetime(2026, 1, 5, 9, 15))

    early = find_rsi_trigger(candles, mode="reversal", scan_start=dt.time(9, 30))
    late = find_rsi_trigger(candles, mode="reversal", scan_start=dt.time(10, 0))

    assert early is not None
    assert early.trigger_time.time() >= dt.time(9, 30)
    assert late is None


def test_no_trigger_on_a_quiet_session():
    from app.strategy.rsi import find_rsi_trigger

    rows = [(100, 100.4, 99.6, 100)] * 60
    candles = make_candles(rows, dt.datetime(2026, 1, 5, 9, 30), 1)

    assert find_rsi_trigger(candles, mode="reversal") is None


def test_empty_frame_returns_none_rather_than_raising():
    from app.strategy.rsi import find_rsi_trigger

    assert find_rsi_trigger(pd.DataFrame()) is None


@pytest.mark.parametrize(
    "kwargs",
    [
        {"mode": "sideways"},
        {"oversold": 80, "overbought": 70},
        {"oversold": 0},
    ],
)
def test_nonsense_configuration_is_rejected_loudly(kwargs):
    from app.strategy.rsi import find_rsi_trigger

    candles = _decline_then_rally(dt.datetime(2026, 1, 5, 9, 30))

    with pytest.raises(ValueError):
        find_rsi_trigger(candles, **kwargs)
