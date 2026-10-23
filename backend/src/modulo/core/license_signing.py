"""Team license signing — generate Ed25519-signed license keys.

Keys produced by :func:`encode_license_key` / :func:`generate_team_license`
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

from modulo.core.license import DEFAULT_RENEWAL_MODE, RENEWAL_MODES
from modulo.core.registry.crypto import sign_primitive
from modulo.settings import get_settings

_log = logging.getLogger(__name__)

# The license payload tier that activates Team feature gates. See
# ``modulo.core.feature_flags.TIER_RANK`` — "team" is the only paid tier.
LICENSE_TIER = "team"

# Feature flag names granted to team licences. These are the real flag
# names registered in ``feature_flags._KNOWN_FLAGS`` under the Team tier.
TEAM_FEATURES: list[str] = ["team_rbac", "sso", "audit_viewer"]


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


def build_team_payload(
    org_name: str,
    term_months: int = 12,
    *,
    org_id: str | None = None,
    features: list[str] | None = None,
    renewal_mode: str = DEFAULT_RENEWAL_MODE,
    licence_id: str | None = None,
    issued_at: datetime | None = None,
) -> dict[str, Any]:
    """Build the signed license payload for a team customer.

    ``org_id`` defaults to a stable UUID v5 derived from ``org_name`` so the
    same customer always maps to the same org identifier.

    FAR-2075 renewal claims, emitted on every newly-minted key:

    - ``renewal_mode`` — ``"auto"`` or ``"manual"`` (default ``"manual"``).
    - ``term`` — the term in MONTHS, the same unit ``expires_at`` is derived
      from, so a renewal can compute the next expiry.
    - ``licence_id`` — opaque stable identifier for THIS licence; defaults to
      a fresh UUID v4 so a renewal can be tied to the licence it replaces.
    - ``issued_at`` — ISO 8601 mint timestamp.

    ``expires_at`` is derived from ``issued_at`` (default: now), so minting a
    renewal with a later issue date shifts its expiry accordingly.

    An explicit ``renewal_mode`` outside the vocabulary raises
    :class:`LicenseSigningError` rather than silently minting the default: a
    typo must not quietly downgrade an auto-renewal licence to manual.
    """
    if not isinstance(renewal_mode, str) or renewal_mode not in RENEWAL_MODES:
        raise LicenseSigningError(f"renewal_mode must be one of {sorted(RENEWAL_MODES)}, got {renewal_mode!r}")
    resolved_org_id = org_id or str(uuid.uuid5(uuid.NAMESPACE_DNS, f"modulo:{org_name}"))
    issued = issued_at or datetime.now(UTC)
    expires_at = _add_months(issued, term_months).isoformat()
    return {
        "tier": LICENSE_TIER,
        "features": list(features if features is not None else TEAM_FEATURES),
        "expires_at": expires_at,
        "org_id": resolved_org_id,
        "renewal_mode": renewal_mode,
        "term": term_months,
        "licence_id": licence_id or str(uuid.uuid4()),
        "issued_at": issued.isoformat(),
    }


def generate_team_license(
    org_name: str,
    term_months: int = 12,
    *,
    private_key_hex: str | None = None,
    org_id: str | None = None,
    features: list[str] | None = None,
    renewal_mode: str = DEFAULT_RENEWAL_MODE,
    licence_id: str | None = None,
    issued_at: datetime | None = None,
) -> str:
    """Generate an Ed25519-signed team license key for *org_name*.

    The signing key is ``private_key_hex`` when provided, otherwise resolved
    from ``MODULO_LICENSE_PRIVATE_KEY``. Raises :class:`LicenseSigningError`
    when no key is available or the key is malformed.

    The FAR-2075 renewal claims are emitted on every key; existing call sites
    keep working because every new parameter has a safe default (see
    :func:`build_team_payload`).
    """
    key = _resolve_private_key(private_key_hex)
    payload = build_team_payload(
        org_name,
        term_months,
        org_id=org_id,
        features=features,
        renewal_mode=renewal_mode,
        licence_id=licence_id,
        issued_at=issued_at,
    )
    return encode_license_key(payload, key)
