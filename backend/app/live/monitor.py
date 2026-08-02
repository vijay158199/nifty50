"""Live signal monitor: polls Yahoo during market hours, runs the same
`engine.run_day` pipeline used by the backtester on the day's candles so far,
and persists/updates the day's Trade row as the setup progresses. This is a
paper/signal system only - it never places real broker orders.
"""
from __future__ import annotations

import datetime as dt

from sqlalchemy import select

from app.config import settings
from app.data import calendar
from app.data.fetcher import INTERVAL_MINUTES, get_session_data
from app.live import control
from app.models.db import get_session, log_event
from app.models.schema import LiveHeartbeat, Trade
from app.reports import charts, excel as excel_reports
from app.strategy.engine import run_day
from app.strategy.mapping import trade_result_to_row
from app.strategy.types import TradeStatus

_TERMINAL_STATUSES = {
    TradeStatus.TARGET_HIT.value,
    TradeStatus.STOP_HIT.value,
    TradeStatus.MANUAL_EXIT.value,
    TradeStatus.NO_SETUP.value,
}


def _upsert_today_trade(row: dict, trade_date: dt.date) -> None:
    """There is at most one Trade row per (source="live", trade_date) - it
    gets replaced as the day's setup evolves (NO_SETUP -> AWAITING_ENTRY ->
    OPEN -> TARGET_HIT/STOP_HIT/MANUAL_EXIT)."""
    with get_session() as session:
        existing = session.execute(
            select(Trade).where(Trade.source == "live", Trade.trade_date == trade_date)
        ).scalar_one_or_none()
        if existing is None:
            session.add(Trade(**row))
        else:
            for key, value in row.items():
                setattr(existing, key, value)


def _record_heartbeat(trade_date: dt.date, status: str, detail: str | None = None) -> None:
    with get_session() as session:
        existing = session.execute(
            select(LiveHeartbeat).where(LiveHeartbeat.trade_date == trade_date)
        ).scalar_one_or_none()
        now = dt.datetime.utcnow()
        if existing is None:
            session.add(LiveHeartbeat(trade_date=trade_date, last_poll_at=now, status=status, detail=detail))
        else:
            existing.last_poll_at = now
            existing.status = status
            existing.detail = detail


def poll_once(trade_date: dt.date | None = None) -> dict:
    """Fetches the latest candles for the session so far and re-runs the
    engine. Safe to call repeatedly (idempotent upsert) - this is what both
    the scheduler's periodic job and a manual "refresh now" dashboard action
    call."""
    trade_date = trade_date or calendar.now_ist().date()

    try:
        live_interval = control.get_structure_interval()
        sd_primary = get_session_data(settings.primary_symbol, trade_date, structure_interval=live_interval)
        sd_confirm = get_session_data(settings.confirm_symbol, trade_date, structure_interval=live_interval)

        if sd_primary.candles_30m.empty:
            _record_heartbeat(trade_date, "RUNNING", "Waiting for first candle data...")
            return {"status": "waiting_for_data"}

        result = run_day(
            trade_date,
            sd_primary.candles_30m,
            sd_primary.fine,
            sd_confirm.fine,
            reduced_resolution=sd_primary.reduced_resolution,
            candle_interval_minutes=INTERVAL_MINUTES.get(sd_primary.resolution, 1),
        )

        row = trade_result_to_row(result, source="live")

        if result.entry is not None and result.status.value in _TERMINAL_STATUSES:
            try:
                row["snapshot_path"] = charts.render_trade_snapshot(result, sd_primary.fine)
            except Exception as exc:  # noqa: BLE001
                log_event("WARNING", "live.monitor", f"Snapshot render failed: {exc}")

        _upsert_today_trade(row, trade_date)
        _record_heartbeat(trade_date, "RUNNING", f"status={result.status.value}")

        return {"status": result.status.value, "direction": row.get("direction"), "entry_type": row.get("entry_type")}

    except Exception as exc:  # noqa: BLE001 - never let a poll failure kill the scheduler loop
        log_event("ERROR", "live.monitor", f"poll_once failed for {trade_date}: {exc}")
        _record_heartbeat(trade_date, "ERROR", str(exc))
        return {"status": "error", "detail": str(exc)}


def finalize_daily_report(trade_date: dt.date | None = None) -> str | None:
    """Runs a final poll, then writes the day's Excel monitoring sheet.
    Called by the 17:00 IST scheduler job (also safe to call manually)."""
    trade_date = trade_date or calendar.now_ist().date()

    if not calendar.is_trading_day(trade_date):
        log_event("INFO", "live.monitor", f"{trade_date} is not a trading day; skipping daily report.")
        return None

    poll_once(trade_date)

    with get_session() as session:
        trades = session.execute(select(Trade).where(Trade.source == "live", Trade.trade_date == trade_date)).scalars().all()
        rows = [
            {c.name: getattr(t, c.name) for c in t.__table__.columns}
            for t in trades
        ]

    report_path = excel_reports.write_daily_sheet(rows, trade_date)
    _record_heartbeat(trade_date, "RUNNING", f"Daily report finalized: {report_path.name}")
    log_event("INFO", "live.monitor", f"Daily report written to {report_path}")
    return str(report_path)


def stop_session(trade_date: dt.date | None = None) -> None:
    trade_date = trade_date or calendar.now_ist().date()
    _record_heartbeat(trade_date, "STOPPED", "Session ended (market close).")
