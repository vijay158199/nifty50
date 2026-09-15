"""SQLAlchemy engine/session setup and a tiny error-log helper used across
the app (data fetcher, live monitor, scheduler, backtester).

Uses a remote Turso (libSQL) database when TURSO_DATABASE_URL/TURSO_AUTH_TOKEN
are set in the environment - this is what makes trade history durable on
hosts with ephemeral local disks (e.g. Render's free tier), since every
read/write goes straight to Turso rather than a local file that could reset
on restart. Falls back to a local SQLite file (unchanged local-dev
experience) when those aren't set.
"""
from __future__ import annotations

import datetime as dt
import logging
import os
from contextlib import contextmanager
from typing import Iterator

from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from app.config import settings
from app.models.schema import Base, ErrorLog

_TURSO_URL = os.environ.get("TURSO_DATABASE_URL")
_TURSO_TOKEN = os.environ.get("TURSO_AUTH_TOKEN")

if _TURSO_URL and _TURSO_TOKEN:
    engine = create_engine(
        f"sqlite+{_TURSO_URL}?secure=true",
        connect_args={"auth_token": _TURSO_TOKEN, "check_same_thread": False},
    )
else:
    engine = create_engine(f"sqlite:///{settings.db_path}", connect_args={"check_same_thread": False})

SessionLocal = sessionmaker(bind=engine, expire_on_commit=False)

logger = logging.getLogger("nifty_strategy")


def init_db() -> None:
    Base.metadata.create_all(engine)
    _migrate_trade_journal_columns()
    _migrate_live_control_columns()


def _migrate_trade_journal_columns() -> None:
    """create_all() only creates missing TABLES, not missing columns on
    tables that already exist - the DB already had `trades` before the
    Journal page's self-graded fields were added (2026-09-14), so a fresh
    SQLite/libSQL-compatible ALTER TABLE is needed to backfill them on
    existing deployments. Idempotent: skips columns that are already there
    (a from-scratch DB gets them from create_all instead and this is a
    no-op)."""
    with engine.connect() as conn:
        existing = {row[1] for row in conn.exec_driver_sql("PRAGMA table_info(trades)")}
        for col, ddl in (
            ("journal_notes", "ALTER TABLE trades ADD COLUMN journal_notes TEXT"),
            ("journal_rating", "ALTER TABLE trades ADD COLUMN journal_rating INTEGER"),
            ("journal_tags", "ALTER TABLE trades ADD COLUMN journal_tags VARCHAR(255)"),
        ):
            if col not in existing:
                conn.exec_driver_sql(ddl)
        conn.commit()


def _migrate_live_control_columns() -> None:
    """Same gap as _migrate_trade_journal_columns() above, but for
    `live_control`: `structure_interval` and `skip_no_fvg_structure` were
    added to the model after this table already existed on deployments from
    before those features - without this, any read of the single live_control
    row (e.g. every dashboard-home load, via live.control.get_status())
    fails with "no such column" on those older, un-migrated databases."""
    with engine.connect() as conn:
        existing = {row[1] for row in conn.exec_driver_sql("PRAGMA table_info(live_control)")}
        for col, ddl in (
            ("structure_interval", "ALTER TABLE live_control ADD COLUMN structure_interval VARCHAR(4) DEFAULT '1m'"),
            ("skip_no_fvg_structure", "ALTER TABLE live_control ADD COLUMN skip_no_fvg_structure BOOLEAN DEFAULT 0"),
        ):
            if col not in existing:
                conn.exec_driver_sql(ddl)
        conn.commit()


@contextmanager
def get_session() -> Iterator[Session]:
    session = SessionLocal()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def log_event(level: str, source: str, message: str) -> None:
    """Write to the standard logger AND mirror into the ErrorLog table so it
    surfaces on the dashboard's Logs/Health page."""
    getattr(logger, level.lower(), logger.info)(f"[{source}] {message}")
    try:
        with get_session() as session:
            session.add(
                ErrorLog(level=level.upper(), source=source, message=message, created_at=dt.datetime.utcnow())
            )
    except Exception:
        logger.exception("Failed to write ErrorLog row (DB unavailable?)")
