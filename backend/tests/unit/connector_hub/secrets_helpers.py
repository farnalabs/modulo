"""Shared Fernet-key credential helpers for connector-hub tests.

Every test file in this package used to declare its own ``_KEY`` /
``_encrypt`` / ``_encrypt_raw`` copies (four near-identical helper sets).
This module is the single home for them: one shared key per pytest session
and three encryption helpers matching the three credential shapes the hub
reads (JSON dict, legacy bare scalar, explicit-key variant).

The key is module-level and freshly generated per session — equivalent to
the former per-file keys, since each consumer encrypts and decrypts within
the same session.
"""

from __future__ import annotations

import json
from typing import Any

from cryptography.fernet import Fernet

SHARED_FERNET_KEY: str = Fernet.generate_key().decode()


def encrypt_credentials(payload: dict[str, Any]) -> bytes:
    """Encrypt a credentials JSON dict under the shared test key."""
    return Fernet(SHARED_FERNET_KEY.encode()).encrypt(json.dumps(payload).encode())


def encrypt_raw_str(payload: str) -> bytes:
    """Encrypt a raw (non-JSON) string — how legacy bare-token credentials rows look."""
    return Fernet(SHARED_FERNET_KEY.encode()).encrypt(payload.encode())


def encrypt_credentials_with_key(key: str, payload: dict[str, Any]) -> bytes:
    """Encrypt a credentials JSON dict under an explicitly supplied key."""
    return Fernet(key.encode()).encrypt(json.dumps(payload).encode())
