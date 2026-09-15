"""Symmetric encryption for broker credentials/tokens at rest - these grant
real trading-account access, so they're never stored as plaintext in the DB
(unlike, say, the login password, which is a local single-user gate rather
than a key to an external account)."""
from __future__ import annotations

from cryptography.fernet import Fernet

from app.config import get_broker_encryption_key


def _fernet() -> Fernet:
    return Fernet(get_broker_encryption_key())


def encrypt(plaintext: str) -> str:
    return _fernet().encrypt(plaintext.encode("utf-8")).decode("utf-8")


def decrypt(ciphertext: str) -> str:
    return _fernet().decrypt(ciphertext.encode("utf-8")).decode("utf-8")
