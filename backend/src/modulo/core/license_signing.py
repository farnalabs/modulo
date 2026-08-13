"""Enterprise license signing — generate Ed25519-signed license keys.

Keys produced by :func:`encode_license_key` / :func:`generate_enterprise_license`
round-trip through ``modulo.core.license.parse_and_verify``: the payload is
canonical JSON, signed with the configured Ed25519 private key, and encoded as
``<base64(payload)>.<base64(signature)>`` — exactly the format
``parse_and_verify`` decodes.

The signing private key is resolved from ``MODULO_LICENSE_PRIVATE_KEY`` (via
``Settings.modulo_license_private_key``) or an explicit ``private_key_hex``
parameter. It is never hardcoded; signing fails closed when no key is
configured.
"""

from __future__ import annotations

import base64
import calendar
import json
import logging
import uuid
from datetime import UTC, datetime
from typing import Any

from modulo.core.registry.crypto import sign_primitive
from modulo.settings import get_settings

_log = logging.getLogger(__name__)

# The license payload tier that activates Team/enterprise feature gates. See
# ``modulo.core.feature_flags.TIER_RANK`` — "team" is the only paid tier.
LICENSE_TIER = "team"

# Feature flag names granted to enterprise licences. These are the real flag
# names registered in ``feature_flags._KNOWN_FLAGS`` under the Team tier.
ENTERPRISE_FEATURES: list[str] = ["team_rbac", "sso", "audit_viewer"]


class LicenseSigningError(ValueError):
    """Raised when a license cannot be signed (e.g. no private key configured)."""


def _add_months(dt: datetime, months: int) -> datetime:
    """Return ``dt`` plus ``months`` months, clamping the day-of-month."""
    month_index = dt.month - 1 + months
    year = dt.year + month_index // 12
    month = month_index % 12 + 1
    day = min(dt.day, calendar.monthrange(year, month)[1])
    return dt.replace(year=year, month=month, day=day)


def _canonical_json(obj: dict[str, Any]) -> bytes:
    """Deterministic JSON serialisation matching ``registry.crypto._canonical_json``.

    ``verify_signature`` (used by ``parse_and_verify``) re-canonicalises the
    parsed payload with ``sort_keys=True``, so this identical serialisation
    guarantees a produced key verifies against a freshly-parsed payload.
    """
    return json.dumps(obj, separators=(",", ":"), sort_keys=True).encode()


def encode_license_key(payload: dict[str, Any], private_key_hex: str) -> str:
    """Sign *payload* with an Ed25519 private key and encode to a license key.

    Produces ``<base64(payload)>.<base64(signature)>`` using URL-safe base64
    without padding, which ``modulo.core.license._decode_license_key`` accepts
    (it re-pads before decoding).
    """
    payload_bytes = _canonical_json(payload)
    sig_bytes = bytes.fromhex(sign_primitive(payload, private_key_hex))
    payload_b64 = base64.urlsafe_b64encode(payload_bytes).decode().rstrip("=")
    sig_b64 = base64.urlsafe_b64encode(sig_bytes).decode().rstrip("=")
    return f"{payload_b64}.{sig_b64}"


def _resolve_private_key(private_key_hex: str | None) -> str:
    if private_key_hex:
        return private_key_hex
    configured = get_settings().modulo_license_private_key
    if configured:
        return configured
    raise LicenseSigningError(
        "No license signing private key configured — set MODULO_LICENSE_PRIVATE_KEY or pass private_key_hex."
    )


def build_enterprise_payload(
    org_name: str,
    term_months: int = 12,
    *,
    org_id: str | None = None,
    features: list[str] | None = None,
) -> dict[str, Any]:
    """Build the signed license payload for an enterprise customer.

    ``org_id`` defaults to a stable UUID v5 derived from ``org_name`` so the
    same customer always maps to the same org identifier.
    """
    resolved_org_id = org_id or str(uuid.uuid5(uuid.NAMESPACE_DNS, f"modulo:{org_name}"))
    expires_at = _add_months(datetime.now(UTC), term_months).isoformat()
    return {
        "tier": LICENSE_TIER,
        "features": list(features if features is not None else ENTERPRISE_FEATURES),
        "expires_at": expires_at,
        "org_id": resolved_org_id,
    }


def generate_enterprise_license(
    org_name: str,
    term_months: int = 12,
    *,
    private_key_hex: str | None = None,
    org_id: str | None = None,
    features: list[str] | None = None,
) -> str:
    """Generate an Ed25519-signed enterprise license key for *org_name*.

    The signing key is ``private_key_hex`` when provided, otherwise resolved
    from ``MODULO_LICENSE_PRIVATE_KEY``. Raises :class:`LicenseSigningError`
    when no key is available or the key is malformed.
    """
    key = _resolve_private_key(private_key_hex)
    payload = build_enterprise_payload(org_name, term_months, org_id=org_id, features=features)
    return encode_license_key(payload, key)
