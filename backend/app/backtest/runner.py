"""Backtest orchestration: replays each trading day in a date range through
the same `engine.run_day` used live, persists every day's outcome (including
no-setup days, for transparency) as Trade rows, computes summary statistics,
and writes the backtest Excel workbook."""
from __future__ import annotations

import datetime as dt
from typing import Callable

from app.backtest.stats import compute_stats
from app.config import settings
from app.data import calendar
from app.data.fetcher import INTERVAL_MINUTES, get_session_data
from app.models.db import get_session, log_event
from app.models.schema import BacktestRun, Trade
from app.reports import charts, excel as excel_reports
from app.strategy.engine import run_day
from app.strategy.mapping import trade_result_to_row

ProgressCallback = Callable[[int, int, dt.date], None]


def run_backtest(
    start_date: dt.date,
    end_date: dt.date,
    symbol: str = settings.primary_symbol,
    symbol_label: str = settings.primary_label,
    confirm_symbol: str = settings.confirm_symbol,
    structure_interval: str | None = None,
    generate_snapshots: bool = True,
    progress_cb: ProgressCallback | None = None,
    existing_run_id: int | None = None,
) -> int:
    """`existing_run_id`: pass this when the caller (e.g. the dashboard route)
    already created the BacktestRun row itself - e.g. to have an id to show
    a "running" status page against immediately - so this function reuses it
    instead of creating a duplicate.

    `structure_interval` ("1m"/"3m"/"5m") is explicit per-run rather than
    read from global settings, so two backtests with different timeframes
    can safely run concurrently (each in its own background thread) without
    one clobbering the other's config."""
    structure_interval = structure_interval or settings.structure_interval

    if existing_run_id is not None:
        run_id = existing_run_id
    else:
        with get_session() as session:
            run = BacktestRun(start_date=start_date, end_date=end_date, status="RUNNING", structure_interval=structure_interval)
            session.add(run)
            session.flush()
            run_id = run.id

    try:
        days = calendar.trading_days(start_date, end_date)
        rows: list[dict] = []

        for i, day in enumerate(days, start=1):
            try:
                sd_primary = get_session_data(symbol, day, structure_interval=structure_interval)
                sd_confirm = get_session_data(confirm_symbol, day, structure_interval=structure_interval)

                if sd_primary.candles_30m.empty:
                    log_event("WARNING", "backtest.runner", f"No data available for {symbol} on {day}; skipping.")
                    continue

                result = run_day(
                    day,
                    sd_primary.candles_30m,
                    sd_primary.fine,
                    sd_confirm.fine,
                    symbol=symbol,
                    symbol_label=symbol_label,
                    reduced_resolution=sd_primary.reduced_resolution,
                    candle_interval_minutes=INTERVAL_MINUTES.get(sd_primary.resolution, 1),
                )

                row = trade_result_to_row(result, source="backtest", backtest_run_id=run_id)

                if generate_snapshots and result.entry is not None:
                    try:
                        row["snapshot_path"] = charts.render_trade_snapshot(result, sd_primary.fine)
                    except Exception as exc:  # noqa: BLE001 - a failed chart shouldn't fail the backtest
                        log_event("WARNING", "backtest.runner", f"Snapshot render failed for {day}: {exc}")

                rows.append(row)
            except Exception as exc:  # noqa: BLE001 - one bad day shouldn't kill the whole run
                log_event("ERROR", "backtest.runner", f"Failed to process {day}: {exc}")
            finally:
                if progress_cb:
                    progress_cb(i, len(days), day)

        with get_session() as session:
            for row in rows:
                session.add(Trade(**row))

        stats = compute_stats(rows)
        report_path = excel_reports.write_backtest_workbook(rows, stats, start_date, end_date, run_id)

        with get_session() as session:
            run = session.get(BacktestRun, run_id)
            run.status = "DONE"
            run.total_trades = stats.total_trades
            run.winning_trades = stats.winning_trades
            run.losing_trades = stats.losing_trades
            run.win_rate_pct = stats.win_rate_pct
            run.loss_rate_pct = stats.loss_rate_pct
            run.total_profit_points = stats.total_profit_points
            run.total_loss_points = stats.total_loss_points
            run.net_profit_points = stats.net_profit_points
            run.net_profit_amount = stats.net_profit_amount
            run.max_winning_streak = stats.max_winning_streak
            run.max_losing_streak = stats.max_losing_streak
            run.avg_profit_per_trade = stats.avg_profit_per_trade
            run.avg_loss_per_trade = stats.avg_loss_per_trade
            run.max_drawdown_points = stats.max_drawdown_points
            run.report_path = str(report_path)
            run.finished_at = dt.datetime.utcnow()

        return run_id

    except Exception as exc:  # noqa: BLE001
        log_event("ERROR", "backtest.runner", f"Backtest run {run_id} failed: {exc}")
        with get_session() as session:
            run = session.get(BacktestRun, run_id)
            run.status = "FAILED"
            run.error_message = str(exc)
            run.finished_at = dt.datetime.utcnow()
        raise
