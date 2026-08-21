"""HMAC verification for product analytics payloads.

Provides replay protection via a 5-minute timestamp window and monotonic
per-instance sequence numbers.

Canonical message format (cross-SDK contract)
---------------------------------------------
The byte string fed into HMAC-SHA256 is built by ``_build_message`` as::

    payload_bytes + b"|" + f"{timestamp:.6f}".encode("ascii") + b"|" + f"{sequence}".encode("ascii")

i.e. ``<payload>|<timestamp with exactly 6 decimal places>|<sequence>``.

This format is an implicit contract with the client SDK (which lives outside
this repo). The timestamp MUST be rendered as a fixed-point float with exactly
six decimal places (``"%.6f"``) — any other precision (``int(ts)``, ``"%.9f"``,
scientific notation, etc.) silently breaks verification because the signed byte
string no longer matches. Keep every signer and verifier in lockstep with this
format.

Distinct protocol — do NOT conflate with the vendor batch signer
----------------------------------------------------------------
This module is the signer/verifier for the inbound *rotation-request* protocol
(see :mod:`modulo.api.routes.product_analytics_identity`).  It is **NOT**
interchangeable with ``sign_outbound_batch`` in
:mod:`modulo.core.product_analytics.vendor_client`, which signs outbound metrics
batches with the ``<payload><timestamp>:<sequence>`` format.  The two protocols
intentionally use different wire formats and must never be cross-used.
"""

from __future__ import annotations

import hashlib
import hmac
import time

_TIMESTAMP_WINDOW_SECONDS = 300  # 5 minutes


def sign_rotation_request(
    secret: str,
    payload_bytes: bytes,
    timestamp: float,
    sequence: int,
) -> str:
    """Compute HMAC-SHA256 for the inbound rotation-request protocol.

    Returns the hex digest string.

    This is the canonical signer for rotation requests verified by
    :func:`verify_hmac`.  It is NOT interchangeable with
    ``sign_outbound_batch`` in
    :mod:`modulo.core.product_analytics.vendor_client` — see the module
    docstring for the distinct wire formats of the two protocols.
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

    expected = sign_rotation_request(secret, payload_bytes, timestamp, sequence)
    return hmac.compare_digest(expected, expected_mac)


def _build_message(payload_bytes: bytes, timestamp: float, sequence: int) -> bytes:
    """Canonical byte string fed into the HMAC.

    The timestamp is rendered as a fixed-point float with exactly six decimal
    places (``"%.6f"``) — this precision is part of the cross-SDK wire contract
    and MUST NOT change without updating every signer.
    """
    return payload_bytes + f"|{timestamp:.6f}".encode("ascii") + f"|{sequence}".encode("ascii")
