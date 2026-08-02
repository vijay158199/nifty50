"""Single on/off switch for the live monitor, backing the dashboard's
Start/Stop button. Kept separate from monitor.py/scheduler.py so both can
import it without a circular dependency.

Clicking Start does not bypass market hours - it "arms" today's session so
the scheduler's interval job is allowed to actually poll once 09:15 IST
arrives (or immediately, if it's already within the session). It always
resets at market close (and self-heals if a stale flag from a prior day
survives an unclean shutdown) so the next trading day requires a fresh
Start click, matching "whenever I click Start, begin monitoring today."
"""
from __future__ import annotations

import datetime as dt

from app.config import settings
from app.data import calendar
from app.models.db import get_session
from app.models.schema import LiveControl

_ROW_ID = 1
VALID_INTERVALS = ("1m", "3m", "5m")


def _as_date(value: dt.date | dt.datetime | None) -> dt.date | None:
    """The `enabled_for_date` column is DateTime-typed (matching the rest of
    this codebase's date columns), so SQLite/SQLAlchemy hands it back as a
    `datetime` even though a `date` was stored - compare on `.date()` or a
    bare date will never equal it, even for the same day."""
    if value is None:
        return None
    return value.date() if isinstance(value, dt.datetime) else value


def _get_or_create(session) -> LiveControl:
    row = session.get(LiveControl, _ROW_ID)
    if row is None:
        row = LiveControl(id=_ROW_ID, enabled=False, structure_interval=settings.structure_interval)
        session.add(row)
        session.flush()
    return row


def get_status() -> dict:
    with get_session() as session:
        row = _get_or_create(session)
        today = calendar.now_ist().date()
        return {
            "enabled": bool(row.enabled),
            "enabled_for_date": _as_date(row.enabled_for_date),
            "updated_at": row.updated_at,
            "is_active_today": bool(row.enabled and _as_date(row.enabled_for_date) == today),
            "structure_interval": row.structure_interval,
        }


def get_structure_interval() -> str:
    with get_session() as session:
        return _get_or_create(session).structure_interval


def set_structure_interval(interval: str) -> None:
    if interval not in VALID_INTERVALS:
        raise ValueError(f"structure_interval must be one of {VALID_INTERVALS}, got {interval!r}")
    with get_session() as session:
        row = _get_or_create(session)
        row.structure_interval = interval
        row.updated_at = dt.datetime.utcnow()


def start() -> None:
    today = calendar.now_ist().date()
    with get_session() as session:
        row = _get_or_create(session)
        row.enabled = True
        row.enabled_for_date = today
        row.updated_at = dt.datetime.utcnow()


def stop() -> None:
    with get_session() as session:
        row = _get_or_create(session)
        row.enabled = False
        row.updated_at = dt.datetime.utcnow()


def is_enabled_today() -> bool:
    today = calendar.now_ist().date()
    with get_session() as session:
        row = _get_or_create(session)
        return bool(row.enabled and _as_date(row.enabled_for_date) == today)
