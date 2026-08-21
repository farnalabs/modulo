"""HMAC verification for product analytics payloads.

Provides replay protection via a 5-minute timestamp window and monotonic
per-instance sequence numbers.
"""

from __future__ import annotations

import hashlib
import hmac
import time

_TIMESTAMP_WINDOW_SECONDS = 300  # 5 minutes


def compute_hmac(
    secret: str,
    payload_bytes: bytes,
    timestamp: float,
    sequence: int,
) -> str:
    """Compute HMAC-SHA256 over ``(payload || timestamp || sequence)``.

    Returns the hex digest string.
    """
    message = _build_message(payload_bytes, timestamp, sequence)
    return hmac.new(
        secret.encode("utf-8"),
        message,
        hashlib.sha256,
    ).hexdigest()


def verify_hmac(
    secret: str,
    payload_bytes: bytes,
    timestamp: float,
    sequence: int,
    expected_mac: str,
    *,
    now: float | None = None,
) -> bool:
    """Verify an HMAC and enforce the timestamp window.

    Parameters
    ----------
    secret:
        The shared secret.
    payload_bytes:
        The raw payload bytes.
    timestamp:
        Unix timestamp when the payload was signed.
    sequence:
        Monotonic per-instance sequence number.
    expected_mac:
        The hex digest to verify against.
    now:
        Override for ``time.time()`` (useful in tests).

    Returns
    -------
    bool
        ``True`` if the HMAC is valid and within the timestamp window.
    """
    current_time = now if now is not None else time.time()

    # Reject if timestamp is outside the 5-minute window.
    if abs(current_time - timestamp) > _TIMESTAMP_WINDOW_SECONDS:
        return False

    expected = compute_hmac(secret, payload_bytes, timestamp, sequence)
    return hmac.compare_digest(expected, expected_mac)


def _build_message(payload_bytes: bytes, timestamp: float, sequence: int) -> bytes:
    """Canonical byte string fed into the HMAC."""
    return payload_bytes + f"|{timestamp:.6f}".encode("ascii") + f"|{sequence}".encode("ascii")
