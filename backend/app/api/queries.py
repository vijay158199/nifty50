"""Read-side query helpers for the dashboard routes - keeps SQL/ORM code out
of the route handlers and returns plain dicts that templates and the shared
`compute_stats` function can both consume."""
from __future__ import annotations

import datetime as dt
import os

from sqlalchemy import delete as sa_delete
from sqlalchemy import desc, select

from app.backtest.stats import BacktestStats, compute_stats
from app.data.calendar import now_ist
from app.models.db import get_session
from app.models.schema import BacktestRun, ErrorLog, LiveHeartbeat, Trade


def _row_to_dict(t: Trade) -> dict:
    return {c.name: getattr(t, c.name) for c in t.__table__.columns}


def get_live_trades(
    start: dt.date | None = None,
    end: dt.date | None = None,
    direction: str | None = None,
    entry_type: str | None = None,
    status: str | None = None,
    limit: int = 500,
) -> list[dict]:
    with get_session() as session:
        stmt = select(Trade).where(Trade.source == "live")
        if start:
            # datetime.min, not the bare date: a bare date binds as e.g.
            # "2026-08-04" which is lexicographically LESS than the stored
            # "2026-08-04 00:00:00.000000" (shorter string vs. its own
            # prefix), so a bare-date upper bound silently excludes every
            # row on that day - only >= happens to work by accident of
            # string comparison, which is what let this hide until now.
            stmt = stmt.where(Trade.trade_date >= dt.datetime.combine(start, dt.time.min))
        if end:
            stmt = stmt.where(Trade.trade_date <= dt.datetime.combine(end, dt.time.max))
        if direction:
            stmt = stmt.where(Trade.direction == direction)
        if entry_type:
            stmt = stmt.where(Trade.entry_type == entry_type)
        if status:
            stmt = stmt.where(Trade.status == status)
        # secondary sort on id: trade_date alone is a tie among every row for
        # the same day, and ties need a deterministic winner (the newest
        # row) rather than whatever order SQLite happens to return them in.
        stmt = stmt.order_by(desc(Trade.trade_date), desc(Trade.id)).limit(limit)
        rows = session.execute(stmt).scalars().all()
    return [_row_to_dict(t) for t in rows]


def get_today_trade() -> dict | None:
    today = now_ist().date()
    trades = get_live_trades(start=today, end=today, limit=1)
    return trades[0] if trades else None


_RESOLVED_STATUSES = {"TARGET_HIT", "STOP_HIT", "MANUAL_EXIT"}


def get_pipeline_stage(trade: dict | None) -> dict:
    """How far today's setup has actually progressed through the strategy's
    4 stages (Liquidity -> Structure -> Entry -> Outcome), plus the engine's
    own explanation for why it stopped where it did (TradeResult.notes,
    persisted as setup_notes) - drives the Overview page's pipeline view."""
    if trade is None:
        return {"reached": 0, "notes": None}
    reached = 0
    if trade.get("trigger_time"):
        reached = 1
    if trade.get("mss_choch_bos"):
        reached = 2
    if trade.get("entry_type"):
        reached = 3
    if trade.get("status") in _RESOLVED_STATUSES:
        reached = 4
    return {"reached": reached, "notes": trade.get("setup_notes")}


def get_live_stats() -> BacktestStats:
    return compute_stats(get_live_trades(limit=100_000))


def get_equity_curve(limit_days: int = 90) -> list[dict]:
    trades = get_live_trades(limit=100_000)
    resolved = sorted(
        (t for t in trades if t.get("status") in {"TARGET_HIT", "STOP_HIT", "MANUAL_EXIT"}),
        key=lambda t: t["trade_date"],
    )
    resolved = resolved[-limit_days:]
    curve = []
    equity = 0.0
    for t in resolved:
        equity += t["pnl_points"] or 0.0
        curve.append({"date": t["trade_date"].strftime("%Y-%m-%d") if hasattr(t["trade_date"], "strftime") else str(t["trade_date"]), "equity": round(equity, 1)})
    return curve


def get_heartbeat(day: dt.date | None = None) -> dict | None:
    day = day or now_ist().date()
    # Range, not exact equality: comparing a bare date against this
    # DateTime column via == was found to never match (confirmed on both
    # local SQLite and the deployed Turso DB) - this function silently
    # returned None for "today" every time before this fix.
    start = dt.datetime.combine(day, dt.time.min)
    end = dt.datetime.combine(day, dt.time.max)
    with get_session() as session:
        hb = session.execute(
            select(LiveHeartbeat)
            .where(LiveHeartbeat.trade_date >= start, LiveHeartbeat.trade_date <= end)
            .order_by(desc(LiveHeartbeat.id))
        ).scalars().first()
    if hb is None:
        return None
    return {"trade_date": hb.trade_date, "last_poll_at": hb.last_poll_at, "status": hb.status, "detail": hb.detail}


def get_recent_errors(limit: int = 100) -> list[dict]:
    with get_session() as session:
        rows = session.execute(select(ErrorLog).order_by(desc(ErrorLog.created_at)).limit(limit)).scalars().all()
    return [{"level": r.level, "source": r.source, "message": r.message, "created_at": r.created_at} for r in rows]


def get_backtest_runs(limit: int = 25) -> list[dict]:
    with get_session() as session:
        rows = session.execute(select(BacktestRun).order_by(desc(BacktestRun.created_at)).limit(limit)).scalars().all()
    return [
        {c.name: getattr(r, c.name) for c in r.__table__.columns}
        for r in rows
    ]


def get_backtest_run(run_id: int) -> dict | None:
    with get_session() as session:
        r = session.get(BacktestRun, run_id)
    if r is None:
        return None
    return {c.name: getattr(r, c.name) for c in r.__table__.columns}


def get_backtest_trades(run_id: int) -> list[dict]:
    with get_session() as session:
        rows = session.execute(
            select(Trade).where(Trade.backtest_run_id == run_id).order_by(Trade.trade_date)
        ).scalars().all()
    return [_row_to_dict(t) for t in rows]


def get_backtest_monthly(run_id: int) -> dict:
    trades = get_backtest_trades(run_id)
    return compute_stats(trades).monthly


def _remove_snapshot_file(snapshot_path: str | None) -> None:
    if not snapshot_path:
        return
    try:
        os.remove(snapshot_path)
    except OSError:
        pass  # already gone, or path was never valid - not worth failing the delete over


def delete_trade(trade_id: int) -> bool:
    """Deletes a single trade row (live or backtest) and its snapshot image
    if any. Returns False if the id didn't exist."""
    with get_session() as session:
        trade = session.get(Trade, trade_id)
        if trade is None:
            return False
        _remove_snapshot_file(trade.snapshot_path)
        session.delete(trade)
    return True


def delete_all_live_trades() -> int:
    """Clears the entire live Trade Log. Returns the number of rows removed."""
    with get_session() as session:
        rows = session.execute(select(Trade).where(Trade.source == "live")).scalars().all()
        count = len(rows)
        for t in rows:
            _remove_snapshot_file(t.snapshot_path)
        session.execute(sa_delete(Trade).where(Trade.source == "live"))
    return count


def delete_backtest_run(run_id: int) -> bool:
    """Deletes a backtest run and all trades/snapshots attached to it."""
    with get_session() as session:
        run = session.get(BacktestRun, run_id)
        if run is None:
            return False
        trades = session.execute(select(Trade).where(Trade.backtest_run_id == run_id)).scalars().all()
        for t in trades:
            _remove_snapshot_file(t.snapshot_path)
        session.execute(sa_delete(Trade).where(Trade.backtest_run_id == run_id))
        session.delete(run)
    return True
