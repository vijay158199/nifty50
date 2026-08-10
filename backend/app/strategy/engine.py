"""Orchestrates the full pipeline for a single trading day:

  settings.first_candle_minutes first-candle (60m by default - see that
  setting's comment for why) -> first liquidity interaction (high or low
  touched), detected on `settings.structure_interval` candles (5m by
  default - found to produce cleaner signals than 1m, with less noise)
    -> structure on the same interval (BOS = continuation, CHOCH = reversal),
      requiring a long-body confirmation candle by default (see
      structure.detect_bos_choch) - derives direction
      -> SMT divergence check (supportive by default, mandatory if configured)
        -> entry timing (settings.entry_priority - Fair Value Gap, entered
          at its 50% level or a deeper fill, plus Order Block / Breaker
          Block / CISD as fallbacks by default; Golden Ratio is also built
          but unused unless added to the priority tuple - first zone
          touched, across all bars scanned, wins)
          -> fixed 15/30 point risk management + position sizing
            -> exit simulation (SL/TP walk-forward on the same candles)

Trade direction is NOT known until the structure stage resolves - the
trigger only flags which liquidity level was touched first, not which way
to trade it. See breakout_sweep.find_trigger and structure.detect_bos_choch.

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
from app.strategy import structure as structure_mod
from app.strategy.breakout_sweep import find_trigger
from app.strategy.risk import build_risk_plan
from app.strategy.smt import check_smt_divergence
from app.strategy.types import Direction, StructureType, TradeResult, TradeStatus


def run_day(
    trade_date: dt.date,
    primary_30m: pd.DataFrame,
    primary_1m: pd.DataFrame,
    confirm_1m: pd.DataFrame,
    symbol: str = settings.primary_symbol,
    symbol_label: str = settings.primary_label,
    reduced_resolution: bool = False,
    candle_interval_minutes: int = 1,
) -> TradeResult:
    """Runs the full pipeline for one session and returns a single
    TradeResult (possibly with status NO_SETUP if nothing qualified).

    `primary_1m`/`confirm_1m` are named for their historical default but
    may be any interval - `candle_interval_minutes` MUST match whatever
    interval they're actually sampled at (1 for "1m", 5 for "5m"), since
    it's used to convert `entry_search_minutes` into a bar count. Getting
    this wrong doesn't crash anything, but silently searches for way more
    or less real time than configured."""
    result = TradeResult(trade_date=trade_date, symbol=symbol, symbol_label=symbol_label,
                          reduced_resolution=reduced_resolution)
    search_bar_limit = max(1, settings.entry_search_minutes // candle_interval_minutes)

    # --- Stage 1: first structure-interval interaction with the first candle's liquidity ---
    trigger = find_trigger(primary_30m, primary_1m, candle_minutes=settings.first_candle_minutes)
    if trigger is None:
        result.status = TradeStatus.NO_SETUP
        result.notes.append(
            f"Neither the first {settings.first_candle_minutes}m candle's high nor low was touched during the session."
        )
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

    # --- Stage 2: BOS (continuation) or CHOCH (reversal) determines direction ---
    structure_event = structure_mod.detect_bos_choch(
        onward_1m,
        trigger.liquidity_side,
        window=settings.swing_fractal_window,
        search_bar_limit=search_bar_limit,
    )
    if structure_event is None:
        result.status = TradeStatus.NO_SETUP
        result.notes.append("No BOS/CHOCH resolved the liquidity interaction within the search window.")
        return result
    if settings.require_choch_only and structure_event.structure_type is not structure_mod.StructureType.CHOCH:
        result.status = TradeStatus.NO_SETUP
        result.notes.append(
            f"{structure_event.signal_label} resolved the liquidity interaction, but only CHOCH "
            "setups are traded per config; setup rejected."
        )
        return result
    result.structure = structure_event
    result.direction = structure_event.direction

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

    # --- Stage 3: entry timing ------------------------------------------------
    zones = entries_mod.build_entry_zones(primary_1m, structure_event, settings.swing_fractal_window)
    if zones is None:
        result.status = TradeStatus.NO_SETUP
        result.notes.append("Could not build entry zones (no origin swing found for the displacement leg).")
        return result

    entry = entries_mod.scan_for_entry(
        primary_1m,
        structure_event,
        zones,
        priority=settings.entry_priority,
        search_bar_limit=search_bar_limit,
    )
    if entry is None:
        result.status = TradeStatus.NO_SETUP
        result.notes.append("Structure confirmed but no entry zone (per settings.entry_priority) was touched in time.")
        return result
    result.entry = entry

    # --- Stage 4: risk management ---------------------------------------------
    risk_plan = build_risk_plan(entry.entry_price, structure_event.direction)
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
