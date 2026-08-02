from tests.conftest import make_candles


def _primary_lower_low_rows():
    # swing lows at idx1 (98) then idx5 (93) -> a genuine new (lower) low
    return [
        (100, 101, 99, 100),
        (100, 101, 98, 99),
        (99, 100, 99, 100),
        (100, 102, 100, 101),
        (101, 102, 100, 101),
        (100, 100, 93, 94),
        (94, 96, 94, 95),
    ]


def _confirm_higher_low_rows():
    # swing lows at idx1 (98) then idx5 (99) -> fails to make a new low
    return [
        (100, 101, 99, 100),
        (100, 101, 98, 99),
        (99, 100, 99, 100),
        (100, 102, 100, 101),
        (101, 102, 100, 101),
        (100, 100, 99, 99.5),
        (99.5, 101, 100, 100.5),
    ]


def _confirm_also_lower_low_rows():
    # same shape as primary - also makes a new (lower) low -> no divergence
    return _primary_lower_low_rows()


def test_detects_bullish_smt_divergence(session_start):
    from app.strategy.smt import check_smt_divergence
    from app.strategy.types import Direction

    primary = make_candles(_primary_lower_low_rows(), session_start, 1)
    confirm = make_candles(_confirm_higher_low_rows(), session_start, 1)

    found, detail = check_smt_divergence(primary, confirm, Direction.BUY, window=1)
    assert found is True
    assert detail is not None and "SMT divergence" in detail


def test_no_divergence_when_both_make_new_lows(session_start):
    from app.strategy.smt import check_smt_divergence
    from app.strategy.types import Direction

    primary = make_candles(_primary_lower_low_rows(), session_start, 1)
    confirm = make_candles(_confirm_also_lower_low_rows(), session_start, 1)

    found, detail = check_smt_divergence(primary, confirm, Direction.BUY, window=1)
    assert found is False
    assert detail is None


def test_no_divergence_with_insufficient_swing_history(session_start):
    from app.strategy.smt import check_smt_divergence
    from app.strategy.types import Direction

    short_rows = _primary_lower_low_rows()[:3]
    primary = make_candles(short_rows, session_start, 1)
    confirm = make_candles(short_rows, session_start, 1)

    found, detail = check_smt_divergence(primary, confirm, Direction.BUY, window=1)
    assert found is False


def test_empty_frames_return_no_divergence(session_start):
    import pandas as pd

    from app.strategy.smt import check_smt_divergence
    from app.strategy.types import Direction

    empty = pd.DataFrame(columns=["Open", "High", "Low", "Close", "Volume"])
    found, detail = check_smt_divergence(empty, empty, Direction.BUY, window=1)
    assert found is False
    assert detail is None
