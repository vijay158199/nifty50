"""Persistence for connected broker accounts - encrypts credentials/tokens
via app.broker.crypto before they ever touch the DB, decrypts only when a
route needs to actually call the broker's API."""
from __future__ import annotations

import datetime as dt
import json

from sqlalchemy import select

from app.broker import crypto
from app.models.db import get_session
from app.models.schema import BrokerAccount


def _row_to_dict(row: BrokerAccount) -> dict:
    return {
        "id": row.id,
        "broker": row.broker,
        "label": row.label,
        "auth_method": row.auth_method,
        "status": row.status,
        "last_checked_at": row.last_checked_at,
        "last_error": row.last_error,
        "token_generated_at": row.token_generated_at,
        "created_at": row.created_at,
    }


def get_all_accounts() -> dict[str, dict]:
    with get_session() as session:
        rows = session.execute(select(BrokerAccount)).scalars().all()
    return {r.broker: _row_to_dict(r) for r in rows}


def get_access_token(broker: str) -> str | None:
    with get_session() as session:
        row = session.execute(select(BrokerAccount).where(BrokerAccount.broker == broker)).scalars().first()
        if row is None or not row.encrypted_access_token:
            return None
        return crypto.decrypt(row.encrypted_access_token)


def save_connection(broker: str, label: str | None, auth_method: str, credentials: dict, access_token: str) -> None:
    enc_creds = crypto.encrypt(json.dumps(credentials))
    enc_token = crypto.encrypt(access_token)
    now = dt.datetime.utcnow()
    with get_session() as session:
        existing = session.execute(select(BrokerAccount).where(BrokerAccount.broker == broker)).scalars().first()
        if existing:
            existing.label = label
            existing.auth_method = auth_method
            existing.encrypted_credentials = enc_creds
            existing.encrypted_access_token = enc_token
            existing.token_generated_at = now
            existing.status = "CONNECTED"
            existing.last_checked_at = now
            existing.last_error = None
        else:
            session.add(BrokerAccount(
                broker=broker, label=label, auth_method=auth_method,
                encrypted_credentials=enc_creds, encrypted_access_token=enc_token,
                token_generated_at=now, status="CONNECTED", last_checked_at=now,
            ))


def mark_error(broker: str, auth_method: str, credentials: dict, message: str) -> None:
    """Upserts (not just updates) so a FIRST connect attempt that fails
    still leaves a visible row - otherwise a first-time typo would fail
    silently with nothing on the Broker page explaining why."""
    enc_creds = crypto.encrypt(json.dumps(credentials))
    now = dt.datetime.utcnow()
    with get_session() as session:
        existing = session.execute(select(BrokerAccount).where(BrokerAccount.broker == broker)).scalars().first()
        if existing:
            existing.auth_method = auth_method
            existing.encrypted_credentials = enc_creds
            existing.status = "ERROR"
            existing.last_error = message
            existing.last_checked_at = now
        else:
            session.add(BrokerAccount(
                broker=broker, label=None, auth_method=auth_method,
                encrypted_credentials=enc_creds, encrypted_access_token=None,
                status="ERROR", last_error=message, last_checked_at=now,
            ))


def disconnect(broker: str) -> bool:
    with get_session() as session:
        row = session.execute(select(BrokerAccount).where(BrokerAccount.broker == broker)).scalars().first()
        if row is None:
            return False
        session.delete(row)
    return True
