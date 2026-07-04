"""License key parsing, verification, and storage.

License keys are base64-encoded, Ed25519-signed JSON payloads in the
format::

    <base64(payload)>.<base64(signature)>

The payload is a JSON object with keys:

    tier        — "community" | "team" | "v1" | "v2"
    features    — list of feature flag names
    expires_at  — ISO 8601 expiration timestamp
    org_id      — organisation identifier
"""

from __future__ import annotations

import base64
import json
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from modulo.core.registry.crypto import verify_signature

# Ed25519 public key (hex-encoded, 64 hex chars).
# In production this would be set via environment or mounted secret.
# This is the dev/test key — replace for production deployments.
_LICENSE_PUBLIC_KEY_HEX: str = "a101dd3f34a806455d25ca247fb84a0a61decff4cf1a467b9456175634b0fc51"

# In-memory store for the current validated license.
_current_license: LicenseData | None = None


def _validate_public_key_hex(hex_key: str) -> None:
    if len(hex_key) != 64:
        msg = f"License public key must be 64 hex chars, got {len(hex_key)}"
        raise ValueError(msg)
    try:
        bytes.fromhex(hex_key)
    except ValueError as exc:
        raise ValueError(f"License public key is not valid hex: {exc}") from exc


# Validate key format at module load time.
_validate_public_key_hex(_LICENSE_PUBLIC_KEY_HEX)


def set_public_key(hex_key: str) -> None:
    global _LICENSE_PUBLIC_KEY_HEX
    _validate_public_key_hex(hex_key)
    _LICENSE_PUBLIC_KEY_HEX = hex_key


@dataclass
class LicenseData:
    tier: str
    features: list[str]
    expires_at: str
    org_id: str
    raw_payload: dict[str, Any] = field(repr=False)
    raw_key: str = field(repr=False)


@dataclass
class LicenseValidation:
    valid: bool
    license_data: LicenseData | None = None
    error: str | None = None


class LicenseError(ValueError):
    ...


def _decode_license_key(key: str) -> tuple[bytes, bytes]:
    try:
        payload_b64, sig_b64 = key.split(".", 1)
    except ValueError as exc:
        raise LicenseError("Invalid license key format: expected <payload>.<signature>") from exc

    def _pad(b64: str) -> str:
        return b64 + "=" * (-len(b64) % 4)

    try:
        payload_bytes = base64.urlsafe_b64decode(_pad(payload_b64))
        sig_bytes = base64.urlsafe_b64decode(_pad(sig_b64))
    except Exception as exc:
        raise LicenseError(f"Invalid base64 encoding: {exc}") from exc

    return payload_bytes, sig_bytes


def _check_expired(expires_at: str) -> str | None:
    try:
        exp = datetime.fromisoformat(expires_at)
        if exp < datetime.now(exp.tzinfo):
            return "License has expired"
    except ValueError:
        return f"Invalid expires_at format: {expires_at}"
    return None


def parse_and_verify(key: str) -> LicenseValidation:
    try:
        payload_bytes, sig_bytes = _decode_license_key(key)
    except LicenseError as exc:
        return LicenseValidation(valid=False, error=str(exc))

    try:
        payload = json.loads(payload_bytes)
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        return LicenseValidation(valid=False, error=f"Invalid JSON payload: {exc}")

    if not isinstance(payload, dict):
        return LicenseValidation(valid=False, error="Payload must be a JSON object")

    sig_hex = sig_bytes.hex()

    if not verify_signature(payload, sig_hex, _LICENSE_PUBLIC_KEY_HEX):
        return LicenseValidation(valid=False, error="Signature verification failed")

    tier = payload.get("tier", "community")
    features = payload.get("features", [])
    expires_at = payload.get("expires_at", "")
    org_id = payload.get("org_id", "")

    if expires_at:
        error = _check_expired(expires_at)
        if error:
            return LicenseValidation(valid=False, error=error)

    data = LicenseData(
        tier=tier,
        features=features,
        expires_at=expires_at,
        org_id=org_id,
        raw_payload=payload,
        raw_key=key,
    )

    return LicenseValidation(valid=True, license_data=data)


def store_license(_key: str, data: LicenseData) -> None:
    global _current_license
    _current_license = data


def get_license() -> LicenseData | None:
    """Return the in-memory license if present and not expired."""
    if _current_license is None:
        return None
    if _current_license.expires_at:
        error = _check_expired(_current_license.expires_at)
        if error:
            clear_license()
            return None
    return _current_license


def clear_license() -> None:
    global _current_license
    _current_license = None
