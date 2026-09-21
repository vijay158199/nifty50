import datetime as dt

import pandas as pd
import pytest


def _candle(ts: str, o, h, l, c, v=1000):
    """Upstox's raw row shape: [ts, open, high, low, close, volume, oi]."""
    return [ts, o, h, l, c, v, 0]


def test_candles_are_sorted_ascending_and_made_naive_ist():
    """Upstox returns newest-first with a +05:30 offset. Everything
    downstream assumes ascending naive-IST, so the client must not pass the
    provider's ordering through."""
    from app.data.upstox import candles_to_frame

    frame = candles_to_frame([
        _candle("2026-09-18T09:17:00+05:30", 3, 3, 3, 3),
        _candle("2026-09-18T09:16:00+05:30", 2, 2, 2, 2),
        _candle("2026-09-18T09:15:00+05:30", 1, 1, 1, 1),
    ])

    assert list(frame["Close"]) == [1.0, 2.0, 3.0]
    assert frame.index[0] == pd.Timestamp("2026-09-18 09:15:00")
    assert frame.index.tz is None
    assert frame.index.name == "ts"
    assert list(frame.columns) == ["Open", "High", "Low", "Close", "Volume"]


def test_duplicate_timestamps_keep_the_last_value():
    """Paged requests overlap at the boundaries; a duplicated bar must not
    become two rows or the resampler will double-count it."""
    from app.data.upstox import candles_to_frame

    frame = candles_to_frame([
        _candle("2026-09-18T09:15:00+05:30", 1, 1, 1, 1),
        _candle("2026-09-18T09:15:00+05:30", 9, 9, 9, 9),
    ])

    assert len(frame) == 1
    assert frame.iloc[0]["Close"] == 9.0


def test_empty_response_gives_an_empty_frame_of_the_right_shape():
    from app.data.upstox import candles_to_frame

    frame = candles_to_frame([])

    assert frame.empty
    assert list(frame.columns) == ["Open", "High", "Low", "Close", "Volume"]
    assert isinstance(frame.index, pd.DatetimeIndex)


def test_instrument_key_is_percent_encoded_in_the_url(monkeypatch):
    """`NSE_INDEX|Nifty 50` has a pipe and a space - unencoded it would not
    survive as a path segment."""
    from app.data import upstox

    seen = []
    monkeypatch.setattr(upstox, "_get", lambda url: seen.append(url) or [])

    upstox.fetch_intraday("NSE_INDEX|Nifty 50", 1)

    assert "NSE_INDEX%7CNifty%2050" in seen[0]
    assert "|" not in seen[0] and " " not in seen[0]


def test_long_ranges_are_paged_around_the_one_month_cap(monkeypatch):
    from app.data import upstox

    urls = []
    monkeypatch.setattr(upstox, "_get", lambda url: urls.append(url) or [])

    start, end = dt.date(2026, 1, 1), dt.date(2026, 3, 31)
    upstox.fetch_historical("NSE_INDEX|Nifty 50", 1, start, end)

    assert len(urls) == 3  # 90 days, 30 per request

    # What actually matters is that the windows tile the range exactly: no
    # gap (a silently missing day) and no overlap beyond the boundary.
    windows = []
    for url in urls:
        to_s, from_s = url.rsplit("/", 2)[-2:]
        windows.append((dt.date.fromisoformat(from_s), dt.date.fromisoformat(to_s)))

    assert windows[0][0] == start
    assert windows[-1][1] == end
    for (_, prev_end), (next_start, _) in zip(windows, windows[1:]):
        assert next_start == prev_end + dt.timedelta(days=1)
    for w_start, w_end in windows:
        assert (w_end - w_start).days < upstox.MAX_RANGE_DAYS


def test_requests_before_2022_are_clamped_not_sent(monkeypatch):
    """Upstox has no 1m data before January 2022. Asking anyway wastes a
    call and returns nothing useful."""
    from app.data import upstox

    urls = []
    monkeypatch.setattr(upstox, "_get", lambda url: urls.append(url) or [])

    upstox.fetch_historical("NSE_INDEX|Nifty 50", 1, dt.date(2019, 5, 1), dt.date(2022, 1, 15))

    assert len(urls) == 1
    assert "/2022-01-01" in urls[0]


def test_a_wholly_pre_2022_range_makes_no_call_at_all(monkeypatch):
    from app.data import upstox

    urls = []
    monkeypatch.setattr(upstox, "_get", lambda url: urls.append(url) or [])

    frame = upstox.fetch_historical("NSE_INDEX|Nifty 50", 1, dt.date(2019, 1, 1), dt.date(2019, 6, 1))

    assert urls == []
    assert frame.empty


def test_fetch_candles_stitches_history_and_today(monkeypatch):
    """The historical endpoint excludes the current session, so a range
    ending today needs both endpoints - and must not ask history for today."""
    from app.data import upstox

    today = dt.date(2026, 9, 21)
    calls = {"historical": [], "intraday": 0}

    def fake_historical(key, mins, start, end):
        calls["historical"].append((start, end))
        return upstox.candles_to_frame([_candle("2026-09-18T09:15:00+05:30", 1, 1, 1, 1)])

    def fake_intraday(key, mins):
        calls["intraday"] += 1
        return upstox.candles_to_frame([_candle("2026-09-21T09:15:00+05:30", 2, 2, 2, 2)])

    monkeypatch.setattr(upstox, "fetch_historical", fake_historical)
    monkeypatch.setattr(upstox, "fetch_intraday", fake_intraday)

    frame = upstox.fetch_candles(
        "NSE_INDEX|Nifty 50", 1,
        dt.datetime(2026, 9, 18, 9, 15), dt.datetime(2026, 9, 21, 15, 30),
        today=today,
    )

    assert calls["intraday"] == 1
    # History was asked for up to yesterday only.
    assert calls["historical"][0][1] == dt.date(2026, 9, 20)
    assert len(frame) == 2


def test_fetch_candles_skips_the_intraday_call_for_a_purely_historical_range(monkeypatch):
    from app.data import upstox

    calls = {"intraday": 0}
    monkeypatch.setattr(upstox, "fetch_historical", lambda *a: upstox.candles_to_frame([]))
    monkeypatch.setattr(
        upstox, "fetch_intraday",
        lambda *a: calls.__setitem__("intraday", calls["intraday"] + 1) or upstox.candles_to_frame([]),
    )

    upstox.fetch_candles(
        "NSE_INDEX|Nifty 50", 1,
        dt.datetime(2026, 5, 1, 9, 15), dt.datetime(2026, 5, 10, 15, 30),
        today=dt.date(2026, 9, 21),
    )

    assert calls["intraday"] == 0


def test_a_missing_token_fails_with_an_actionable_message(monkeypatch):
    """The most likely first-run failure. The error has to say what to do,
    because 'unauthorized' alone sends you to the wrong fix."""
    from app.data import upstox

    monkeypatch.setattr(upstox.settings, "upstox_access_token", "")

    with pytest.raises(upstox.UpstoxAuthError) as excinfo:
        upstox._headers()

    assert "Analytics Token" in str(excinfo.value)


def test_a_non_success_payload_is_raised_not_silently_empty(monkeypatch):
    """A failed fetch that returns [] would be cached as 'no candles' and
    look like a quiet market instead of an outage."""
    from app.data import upstox

    class _Resp:
        status_code = 200
        text = ""

        def raise_for_status(self):
            pass

        def json(self):
            return {"status": "error", "errors": [{"message": "bad instrument key"}]}

    monkeypatch.setattr(upstox.settings, "upstox_access_token", "tok")
    monkeypatch.setattr(upstox.requests, "get", lambda *a, **k: _Resp())

    with pytest.raises(upstox.UpstoxError):
        upstox._get("https://api.upstox.com/v3/whatever")
