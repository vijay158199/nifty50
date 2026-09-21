"""Orchestrates the full pipeline for a single trading day:

  directional bias (settings.bias_source) - either an RSI line break after
  settings.rsi_scan_start (the "RSI" default, see rsi.py) or the original
  first-candle liquidity sweep ("FIRST_CANDLE", see breakout_sweep.py),
  detected on `settings.structure_interval` candles (1m by default, also
  selectable per live-session/backtest-run as 2m/3m/5m)
    -> structure on the same interval (BOS = continuation, CHOCH = reversal),
      requiring the confirming candle itself to show strong displacement (a
      "long body") by default (see structure.detect_bos_choch) - derives
      direction; a break with no displacement is simply not considered
      -> SMT divergence check (supportive by default, mandatory if configured)
        -> entry timing (settings.entry_priority - Fair Value Gap by
          default, entered at its 50% level or a deeper fill; Order Block /
          Breaker Block / Golden Ratio / CISD zones are also built but
          unused unless added back to the priority tuple - first zone
          touched wins)
          -> risk management + position sizing: SL/TP from the
            displacement leg's own high/low by default (settings.
            dynamic_risk_from_displacement), fixed 15/30 points as a
            fallback - see risk.build_risk_plan
            -> exit simulation (SL/TP walk-forward on the same candles)

A trade is only ever taken on a structure event. Under
settings.require_choch_only (the default) that means a CHOCH - this
codebase's market structure shift - and a BOS along the way is stepped over
rather than ending the day, matching "dont take trade wait mss".

In FIRST_CANDLE mode the direction is not known until the structure stage
resolves; the trigger only flags which liquidity level was touched first. In
RSI mode the bias direction is known up front but is still expressed as the
liquidity side whose CHOCH resolves that way, so the structure stage stays
the single place a direction is confirmed. See rsi.find_rsi_trigger,
breakout_sweep.find_trigger and structure.detect_bos_choch.

This module is shared verbatim by the live monitor and the backtester - the
only difference is where the candle DataFrames come from (a live poll vs. a
historical fetch), which keeps live and backtest behaviour guaranteed
consistent.
"""
from __future__ import annotations

import datetime as dt

import pandas as pd

from app.config import settings
from app.strategy import entries as entries_mod
from app.strategy import rsi as rsi_mod
from app.strategy import structure as structure_mod
from app.strategy.breakout_sweep import find_trigger
from app.strategy.risk import build_risk_plan
from app.strategy.smt import check_smt_divergence
from app.strategy.types import Direction, StructureType, TradeResult, TradeStatus


def _parse_hhmm(value: str) -> dt.time:
    """"09:30" -> time(9, 30). Kept here rather than in config so the setting
    stays a plain string that env vars can override without a custom parser."""
    hh, mm = value.split(":")
    return dt.time(int(hh), int(mm))


def run_day(
    trade_date: dt.date,
    primary_30m: pd.DataFrame,
    primary_1m: pd.DataFrame,
    confirm_1m: pd.DataFrame,
    symbol: str = settings.primary_symbol,
    symbol_label: str = settings.primary_label,
    reduced_resolution: bool = False,
    candle_interval_minutes: int = 1,
    skip_no_fvg_structure: bool | None = None,
) -> TradeResult:
    """Runs the full pipeline for one session and returns a single
    TradeResult (possibly with status NO_SETUP if nothing qualified).

    `primary_1m`/`confirm_1m` are named for their historical default but
    may be any interval - `candle_interval_minutes` MUST match whatever
    interval they're actually sampled at (1 for "1m", 5 for "5m"), since
    it's used to convert `entry_search_minutes` into a bar count. Getting
    this wrong doesn't crash anything, but silently searches for way more
    or less real time than configured.

    `skip_no_fvg_structure` (None = use settings.skip_no_fvg_structure):
    when True, a BOS/CHOCH that confirms but never gets an FVG entry
    touched is skipped in favor of the NEXT BOS/CHOCH later in the same
    session, instead of ending the day right there - kept OFF by default,
    see settings.skip_no_fvg_structure for why."""
    result = TradeResult(trade_date=trade_date, symbol=symbol, symbol_label=symbol_label,
                          reduced_resolution=reduced_resolution)
    search_bar_limit = max(1, settings.entry_search_minutes // candle_interval_minutes)
    skip_no_fvg = settings.skip_no_fvg_structure if skip_no_fvg_structure is None else skip_no_fvg_structure

    # --- Stage 1: establish the day's directional bias ----------------------
    # Either an RSI line break (settings.bias_source="RSI", the default) or
    # the original first-candle liquidity sweep. Both produce a TriggerEvent
    # whose liquidity_side drives the structure stage identically, so nothing
    # downstream of here needs to know which model ran.
    if settings.bias_source.upper() == "RSI":
        trigger = rsi_mod.find_rsi_trigger(
            primary_1m,
            period=settings.rsi_period,
            overbought=settings.rsi_overbought,
            oversold=settings.rsi_oversold,
            mode=settings.rsi_bias_mode,
            scan_start=_parse_hhmm(settings.rsi_scan_start),
        )
        no_trigger_note = (
            f"RSI({settings.rsi_period}) never crossed {settings.rsi_oversold:.0f}/"
            f"{settings.rsi_overbought:.0f} in '{settings.rsi_bias_mode}' mode after "
            f"{settings.rsi_scan_start}."
        )
        # An RSI bias is a short-lived read on the tape, so the structure
        # shift that confirms it has its own (usually tighter) deadline -
        # settings.rsi_bias_expiry_minutes, not entry_search_minutes.
        structure_bar_limit = max(1, settings.rsi_bias_expiry_minutes // candle_interval_minutes)
    else:
        trigger = find_trigger(primary_30m, primary_1m, candle_minutes=settings.first_candle_minutes)
        no_trigger_note = (
            f"Neither the first {settings.first_candle_minutes}m candle's high nor low "
            "was touched during the session."
        )
        structure_bar_limit = search_bar_limit

    if trigger is None:
        result.status = TradeStatus.NO_SETUP
        result.notes.append(no_trigger_note)
        return result
    result.trigger = trigger

    if primary_1m.empty:
        result.status = TradeStatus.NO_SETUP
        result.notes.append("No 1m data available from the trigger time onward.")
        return result

    # 1m candles from the moment the trigger candle actually closed onward
    # (trigger.trigger_time is that close, not the candle's open - see
    # breakout_sweep.find_trigger) - structure analysis starts here.
    onward_1m = primary_1m[primary_1m.index >= trigger.trigger_time]
    if onward_1m.empty:
        result.status = TradeStatus.NO_SETUP
        result.notes.append("Trigger fired but no 1m candles followed it (end of data).")
        return result

    # --- Stages 2-3: BOS/CHOCH structure, then entry timing -------------------
    # A single pass unless skip_no_fvg is on, in which case a structure event
    # that confirms but never gets an entry touched doesn't end the day - the
    # search window advances past it and looks for the NEXT BOS/CHOCH instead.
    search_window = onward_1m
    skipped_no_fvg = 0
    skipped_bos = 0
    structure_event = zones = entry = None

    while True:
        structure_event = structure_mod.detect_bos_choch(
            search_window,
            trigger.liquidity_side,
            window=settings.swing_fractal_window,
            search_bar_limit=structure_bar_limit,
        )
        if structure_event is None:
            reasons = []
            if skipped_bos:
                reasons.append(f"{skipped_bos} BOS skipped while waiting for an MSS")
            if skipped_no_fvg:
                reasons.append(f"{skipped_no_fvg} structure event(s) skipped for having no FVG entry")
            result.status = TradeStatus.NO_SETUP
            result.notes.append(
                "No market structure shift confirmed the bias within the search window."
                + (f" ({'; '.join(reasons)}.)" if reasons else "")
            )
            return result

        if settings.require_choch_only and structure_event.structure_type is not structure_mod.StructureType.CHOCH:
            # Explicit user spec: a BOS in the bias direction is not a trade,
            # but it is not the end of the day either - "dont take trade wait
            # mss". Step over it and keep scanning the same session for a
            # genuine structure shift. The window strictly shrinks each pass,
            # so this always terminates.
            skipped_bos += 1
            search_window = search_window[search_window.index > structure_event.ts]
            if search_window.empty:
                result.status = TradeStatus.NO_SETUP
                result.notes.append(
                    f"Session ended waiting for an MSS; {skipped_bos} BOS seen and skipped."
                )
                return result
            continue

        # --- SMT divergence check (supportive by default, mandatory if configured) ---
        confirm_onward = confirm_1m[confirm_1m.index <= structure_event.ts] if not confirm_1m.empty else confirm_1m
        primary_onward_truncated = onward_1m[onward_1m.index <= structure_event.ts]
        smt_found, smt_detail = check_smt_divergence(
            primary_onward_truncated, confirm_onward, structure_event.direction, settings.swing_fractal_window
        )
        structure_event.smt_divergence = smt_found
        structure_event.smt_detail = smt_detail
        if settings.require_smt_alignment and not smt_found:
            result.status = TradeStatus.NO_SETUP
            result.notes.append("SMT divergence required by config but not found; setup rejected.")
            return result

        # --- Entry timing -------------------------------------------------
        zones = entries_mod.build_entry_zones(primary_1m, structure_event, settings.swing_fractal_window)
        entry = None
        if zones is not None:
            entry = entries_mod.scan_for_entry(
                primary_1m,
                structure_event,
                zones,
                priority=settings.entry_priority,
                search_bar_limit=search_bar_limit,
            )

        if entry is not None:
            break

        if not skip_no_fvg:
            result.status = TradeStatus.NO_SETUP
            result.notes.append(
                "Could not build entry zones (no origin swing found for the displacement leg)." if zones is None
                else "Structure confirmed but no entry zone (per settings.entry_priority) was touched in time."
            )
            return result

        # skip_no_fvg is on: ignore this structure event and keep looking
        # forward for the next one within the same session.
        skipped_no_fvg += 1
        search_window = search_window[search_window.index > structure_event.ts]
        if search_window.empty:
            result.status = TradeStatus.NO_SETUP
            result.notes.append(f"No further structure after skipping {skipped_no_fvg} BOS/CHOCH event(s) with no FVG entry.")
            return result

    result.structure = structure_event
    result.direction = structure_event.direction
    result.leg_candle_count = zones.leg_candle_count
    result.entry = entry
    if skipped_bos:
        result.notes.append(f"Waited through {skipped_bos} BOS before this MSS confirmed.")
    if skipped_no_fvg:
        result.notes.append(f"Took this BOS/CHOCH after skipping {skipped_no_fvg} earlier one(s) with no FVG entry.")

    # --- Stage 4: risk management ---------------------------------------------
    risk_plan = build_risk_plan(entry.entry_price, structure_event.direction, zones.leg_high, zones.leg_low)
    result.risk = risk_plan
    result.status = TradeStatus.OPEN

    # --- Stage 5: exit simulation (walk forward on 1m candles from entry) -----
    _simulate_exit(result, primary_1m)
    return result


def _simulate_exit(result: TradeResult, primary_1m: pd.DataFrame) -> None:
    """Walk 1m candles forward from the entry bar, exiting on whichever of
    SL/TP is touched first; if the session ends with the trade still open,
    mark it MANUAL_EXIT at the last available close (paper-trading
    convention: flatten at session end)."""
    entry = result.entry
    risk = result.risk
    direction = result.direction
    assert entry is not None and risk is not None and direction is not None

    # Include the entry bar itself: the zone touch that triggered entry may
    # only account for part of that candle's range, and the remainder of the
    # same bar can still reach SL/TP before the next bar even opens.
    after_entry = primary_1m[primary_1m.index >= entry.entry_time]
    for ts, row in after_entry.iterrows():
        low, high = float(row["Low"]), float(row["High"])
        if direction is Direction.BUY:
            hit_sl = low <= risk.stop_loss
            hit_tp = high >= risk.take_profit
        else:
            hit_sl = high >= risk.stop_loss
            hit_tp = low <= risk.take_profit

        # Conservative convention when both could occur in the same bar:
        # assume the stop is hit first (protects against overstating results).
        if hit_sl and hit_tp:
            result.exit_time = ts
            result.exit_price = risk.stop_loss
            result.exit_reason = "Stop-loss and target both in range on the same candle; stop assumed hit first."
            result.status = TradeStatus.STOP_HIT
            return
        if hit_sl:
            result.exit_time = ts
            result.exit_price = risk.stop_loss
            result.exit_reason = f"Stop-loss touched at {risk.stop_loss:.1f}."
            result.status = TradeStatus.STOP_HIT
            return
        if hit_tp:
            result.exit_time = ts
            result.exit_price = risk.take_profit
            result.exit_reason = f"Take-profit touched at {risk.take_profit:.1f}."
            result.status = TradeStatus.TARGET_HIT
            return

    if not after_entry.empty:
        last_ts = after_entry.index[-1]
        last_close = float(after_entry.iloc[-1]["Close"])
        result.exit_time = last_ts
        result.exit_price = last_close
        result.exit_reason = "Session ended before SL/TP was hit; flattened at last available close."
        result.status = TradeStatus.MANUAL_EXIT
    else:
        result.status = TradeStatus.AWAITING_ENTRY
        result.notes.append("Entry filled but no further 1m candles available yet to simulate an exit.")
