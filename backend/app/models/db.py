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
    _migrate_missing_columns()


# create_all() only creates missing TABLES, not missing COLUMNS on tables
# that already exist - each entry here is a column that was added to a
# model after its table was already live on some deployment (Render's Turso
# DB in particular), which otherwise fails every read of that table with
# "no such column". Hit twice already (trades' journal_* fields, then
# live_control's structure_interval/skip_no_fvg_structure) before this table
# replaced two near-identical one-off functions - add a row here instead of
# writing a third when the next column joins an existing table.
_COLUMN_MIGRATIONS: list[tuple[str, str, str]] = [
    ("trades", "journal_notes", "ALTER TABLE trades ADD COLUMN journal_notes TEXT"),
    ("trades", "journal_rating", "ALTER TABLE trades ADD COLUMN journal_rating INTEGER"),
    ("trades", "journal_tags", "ALTER TABLE trades ADD COLUMN journal_tags VARCHAR(255)"),
    ("live_control", "structure_interval", "ALTER TABLE live_control ADD COLUMN structure_interval VARCHAR(4) DEFAULT '1m'"),
    ("live_control", "skip_no_fvg_structure", "ALTER TABLE live_control ADD COLUMN skip_no_fvg_structure BOOLEAN DEFAULT 0"),
    ("backtest_runs", "structure_interval", "ALTER TABLE backtest_runs ADD COLUMN structure_interval VARCHAR(4) DEFAULT '1m'"),
    ("backtest_runs", "skip_no_fvg_structure", "ALTER TABLE backtest_runs ADD COLUMN skip_no_fvg_structure BOOLEAN DEFAULT 0"),
]


def _migrate_missing_columns() -> None:
    """Backfills each _COLUMN_MIGRATIONS entry onto tables that already
    existed before that column was added to the model. Idempotent - skips
    any column already present, so a from-scratch DB (where create_all()
    made the column from day one) is a no-op."""
    with engine.connect() as conn:
        existing_by_table: dict[str, set[str]] = {}
        for table, col, ddl in _COLUMN_MIGRATIONS:
            if table not in existing_by_table:
                existing_by_table[table] = {row[1] for row in conn.exec_driver_sql(f"PRAGMA table_info({table})")}
            if col not in existing_by_table[table]:
                conn.exec_driver_sql(ddl)
                existing_by_table[table].add(col)
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
