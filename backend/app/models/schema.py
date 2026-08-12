"""SQLAlchemy ORM models: trades, daily summaries, backtest runs, error log,
and a raw-candle cache table."""
from __future__ import annotations

import datetime as dt

from sqlalchemy import (
    Boolean,
    DateTime,
    Float,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


class Trade(Base):
    """One row per detected trade, shared shape for live and backtested trades."""

    __tablename__ = "trades"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    source: Mapped[str] = mapped_column(String(16))  # "live" | "backtest"
    backtest_run_id: Mapped[int | None] = mapped_column(Integer, nullable=True)

    trade_date: Mapped[dt.date] = mapped_column(DateTime, index=True)
    symbol: Mapped[str] = mapped_column(String(32))
    symbol_label: Mapped[str] = mapped_column(String(32))

    # Nullable: a liquidity interaction can fire (trigger_time set) without
    # ever resolving into a BOS/CHOCH, in which case direction is genuinely
    # unknown - not just "not yet decided" the way it always was pre-fix,
    # when direction was locked in from the trigger stage itself.
    direction: Mapped[str | None] = mapped_column(String(8), nullable=True)  # BUY | SELL

    trigger_time: Mapped[dt.datetime | None] = mapped_column(DateTime, nullable=True)
    trigger_type: Mapped[str | None] = mapped_column(String(16), nullable=True)  # BREAKOUT | SWEEP
    liquidity_side: Mapped[str | None] = mapped_column(String(8), nullable=True)  # HIGH | LOW - which side of the first candle was taken

    entry_time: Mapped[dt.datetime | None] = mapped_column(DateTime, nullable=True)
    entry_price: Mapped[float | None] = mapped_column(Float, nullable=True)
    entry_type: Mapped[str | None] = mapped_column(String(24), nullable=True)  # CISD/ORDER_BLOCK/...
    entry_reason: Mapped[str | None] = mapped_column(Text, nullable=True)

    stop_loss: Mapped[float | None] = mapped_column(Float, nullable=True)
    take_profit: Mapped[float | None] = mapped_column(Float, nullable=True)
    position_size_lots: Mapped[int | None] = mapped_column(Integer, nullable=True)

    exit_time: Mapped[dt.datetime | None] = mapped_column(DateTime, nullable=True)
    exit_price: Mapped[float | None] = mapped_column(Float, nullable=True)
    exit_reason: Mapped[str | None] = mapped_column(Text, nullable=True)

    pnl_points: Mapped[float | None] = mapped_column(Float, nullable=True)
    pnl_amount: Mapped[float | None] = mapped_column(Float, nullable=True)
    rr_achieved: Mapped[float | None] = mapped_column(Float, nullable=True)

    status: Mapped[str] = mapped_column(String(24), default="PENDING")
    # PENDING | AWAITING_ENTRY | OPEN | TARGET_HIT | STOP_HIT | MANUAL_EXIT | NO_SETUP | REDUCED_RESOLUTION

    reduced_resolution: Mapped[bool] = mapped_column(Boolean, default=False)
    snapshot_path: Mapped[str | None] = mapped_column(String(512), nullable=True)

    mss_choch_bos: Mapped[str | None] = mapped_column(String(16), nullable=True)
    # When BOS/CHOCH actually confirmed - distinct from trigger_time (the
    # earlier liquidity tap) - drives the Overview pipeline card's "N
    # candles since liquidity tap" and structure-confirmation-time display.
    structure_time: Mapped[dt.datetime | None] = mapped_column(DateTime, nullable=True)
    # Human-readable reason(s) the pipeline stopped where it did (e.g. "No
    # BOS/CHOCH resolved the liquidity interaction within the search
    # window.") - engine.run_day always computes this (TradeResult.notes)
    # but it was being silently dropped before reaching the DB/dashboard.
    setup_notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    smt_divergence: Mapped[bool] = mapped_column(Boolean, default=False)

    created_at: Mapped[dt.datetime] = mapped_column(DateTime, default=dt.datetime.utcnow)


class DailySummary(Base):
    __tablename__ = "daily_summaries"
    __table_args__ = (UniqueConstraint("trade_date", name="uq_daily_summary_date"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    trade_date: Mapped[dt.date] = mapped_column(DateTime, index=True)
    trades_count: Mapped[int] = mapped_column(Integer, default=0)
    wins: Mapped[int] = mapped_column(Integer, default=0)
    losses: Mapped[int] = mapped_column(Integer, default=0)
    net_pnl_points: Mapped[float] = mapped_column(Float, default=0.0)
    net_pnl_amount: Mapped[float] = mapped_column(Float, default=0.0)
    report_path: Mapped[str | None] = mapped_column(String(512), nullable=True)
    generated_at: Mapped[dt.datetime] = mapped_column(DateTime, default=dt.datetime.utcnow)


class BacktestRun(Base):
    __tablename__ = "backtest_runs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    start_date: Mapped[dt.date] = mapped_column(DateTime)
    end_date: Mapped[dt.date] = mapped_column(DateTime)
    structure_interval: Mapped[str] = mapped_column(String(4), default="5m")  # "1m" | "2m" | "3m" | "5m"
    status: Mapped[str] = mapped_column(String(16), default="RUNNING")  # RUNNING|DONE|FAILED
    total_trades: Mapped[int] = mapped_column(Integer, default=0)
    winning_trades: Mapped[int] = mapped_column(Integer, default=0)
    losing_trades: Mapped[int] = mapped_column(Integer, default=0)
    win_rate_pct: Mapped[float] = mapped_column(Float, default=0.0)
    loss_rate_pct: Mapped[float] = mapped_column(Float, default=0.0)
    total_profit_points: Mapped[float] = mapped_column(Float, default=0.0)
    total_loss_points: Mapped[float] = mapped_column(Float, default=0.0)
    net_profit_points: Mapped[float] = mapped_column(Float, default=0.0)
    net_profit_amount: Mapped[float] = mapped_column(Float, default=0.0)
    max_winning_streak: Mapped[int] = mapped_column(Integer, default=0)
    max_losing_streak: Mapped[int] = mapped_column(Integer, default=0)
    avg_profit_per_trade: Mapped[float] = mapped_column(Float, default=0.0)
    avg_loss_per_trade: Mapped[float] = mapped_column(Float, default=0.0)
    max_drawdown_points: Mapped[float] = mapped_column(Float, default=0.0)
    report_path: Mapped[str | None] = mapped_column(String(512), nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime, default=dt.datetime.utcnow)
    finished_at: Mapped[dt.datetime | None] = mapped_column(DateTime, nullable=True)


class ErrorLog(Base):
    __tablename__ = "error_log"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    level: Mapped[str] = mapped_column(String(16))  # INFO|WARNING|ERROR
    source: Mapped[str] = mapped_column(String(64))  # e.g. "live.monitor", "data.fetcher"
    message: Mapped[str] = mapped_column(Text)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime, default=dt.datetime.utcnow, index=True)


class CandleCache(Base):
    """Raw OHLC candles cached locally to avoid re-hitting Yahoo and to survive
    its rolling lookback windows for backtesting continuity."""

    __tablename__ = "candle_cache"
    __table_args__ = (
        UniqueConstraint("symbol", "interval", "ts", name="uq_candle_symbol_interval_ts"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    symbol: Mapped[str] = mapped_column(String(32), index=True)
    interval: Mapped[str] = mapped_column(String(8), index=True)  # "1m" | "5m" - the raw intervals actually fetched from Yahoo (3m/30m are derived in-memory from 1m, never cached separately)
    ts: Mapped[dt.datetime] = mapped_column(DateTime, index=True)
    open: Mapped[float] = mapped_column(Float)
    high: Mapped[float] = mapped_column(Float)
    low: Mapped[float] = mapped_column(Float)
    close: Mapped[float] = mapped_column(Float)
    volume: Mapped[float] = mapped_column(Float, default=0.0)
    fetched_at: Mapped[dt.datetime] = mapped_column(DateTime, default=dt.datetime.utcnow)


class LiveHeartbeat(Base):
    """Single-row-per-day heartbeat so the dashboard can show monitor health."""

    __tablename__ = "live_heartbeat"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    trade_date: Mapped[dt.date] = mapped_column(DateTime, index=True)
    last_poll_at: Mapped[dt.datetime] = mapped_column(DateTime, default=dt.datetime.utcnow)
    status: Mapped[str] = mapped_column(String(32), default="RUNNING")  # RUNNING|STOPPED|ERROR
    detail: Mapped[str | None] = mapped_column(Text, nullable=True)


class LiveControl(Base):
    """Single-row (id=1) on/off switch for the live monitor, toggled by the
    dashboard's Start/Stop button. The scheduler's poll job only does work
    when this is enabled AND the market is actually in session - clicking
    Start "arms" the monitor for today, it doesn't bypass market hours."""

    __tablename__ = "live_control"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, default=1)
    enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    enabled_for_date: Mapped[dt.date | None] = mapped_column(DateTime, nullable=True)
    structure_interval: Mapped[str] = mapped_column(String(4), default="5m")  # "1m" | "2m" | "3m" | "5m" - live monitor's active timeframe
    updated_at: Mapped[dt.datetime] = mapped_column(DateTime, default=dt.datetime.utcnow)
