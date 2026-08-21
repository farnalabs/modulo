"""Consent logic, prompt validation, and partner carve-out check."""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from modulo.core.product_analytics.constants import (
    DEFAULT_LEVEL,
    DEFAULT_LEVEL_CHANGED_AT,
    DEFAULT_PROMPTED,
    DEFAULT_PROMPTED_AT,
    DISMISS_COOLDOWN,
    INSTANCE_SWITCH_KEY,
    LEVEL_ALL,
    LEVEL_OFF,
    LICENSE_ENFORCEMENT_KILL_SWITCH_KEY,
    PARTNER_LICENSE_CLAIM,
    PRODUCT_ANALYTICS_KEY,
    PROMPTED_DISMISSED,
    PROMPTED_NO,
    PROMPTED_YES,
    VALID_LEVELS,
)
from modulo.db.crud.system_config import get_config

_log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Org settings helpers
# ---------------------------------------------------------------------------


def default_consent_state() -> dict[str, Any]:
    """Return the default consent state dict for a fresh org."""
    return {
        "level": DEFAULT_LEVEL,
        "prompted": DEFAULT_PROMPTED,
        "prompted_at": DEFAULT_PROMPTED_AT,
        "level_changed_at": DEFAULT_LEVEL_CHANGED_AT,
    }


def get_product_analytics_block(org_settings_json: dict[str, Any] | None) -> dict[str, Any]:
    """Extract the product_analytics block from org settings, returning defaults for missing fields."""
    raw = (org_settings_json or {}).get(PRODUCT_ANALYTICS_KEY)
    if not isinstance(raw, dict):
        return default_consent_state()
    result = default_consent_state()
    result.update(raw)
    return result


def merge_product_analytics_block(
    org_settings_json: dict[str, Any] | None,
    updates: dict[str, Any],
) -> dict[str, Any]:
    """Return a new settings_json dict with the product_analytics block merged."""
    settings = dict(org_settings_json or {})
    existing = dict(settings.get(PRODUCT_ANALYTICS_KEY) or {})
    existing.update(updates)
    settings[PRODUCT_ANALYTICS_KEY] = existing
    return settings


# ---------------------------------------------------------------------------
# Prompt eligibility
# ---------------------------------------------------------------------------


def is_prompt_eligible(consent: dict[str, Any]) -> bool:
    """Return True if the org is eligible for a consent prompt.

    Eligible when:
    - prompted is None (never prompted), OR
    - prompted is 'dismissed' AND the cooldown has expired
    """
    prompted = consent.get("prompted")
    if prompted is None:
        return True
    if prompted != PROMPTED_DISMISSED:
        return False
    # Check cooldown
    prompted_at = consent.get("prompted_at")
    if prompted_at is None:
        return True
    if isinstance(prompted_at, str):
        try:
            prompted_at = datetime.fromisoformat(prompted_at)
        except (ValueError, TypeError):
            return True
    if prompted_at.tzinfo is None:
        prompted_at = prompted_at.replace(tzinfo=UTC)
    return datetime.now(UTC) - prompted_at >= DISMISS_COOLDOWN


# ---------------------------------------------------------------------------
# Consent transitions
# ---------------------------------------------------------------------------


def apply_consent_action(
    consent: dict[str, Any],
    action: str,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Apply a consent action and return the updated consent dict.

    Actions:
    - accept: level=all, prompted=yes, prompted_at=now, level_changed_at=now
    - decline: level=off, prompted=no, prompted_at=now, level_changed_at=now
    - dismiss: prompted=dismissed, prompted_at=now (level unchanged)
    """
    if now is None:
        now = datetime.now(UTC)
    result = dict(consent)
    if action == "accept":
        result["level"] = LEVEL_ALL
        result["prompted"] = PROMPTED_YES
        result["prompted_at"] = now.isoformat()
        result["level_changed_at"] = now.isoformat()
    elif action == "decline":
        result["level"] = LEVEL_OFF
        result["prompted"] = PROMPTED_NO
        result["prompted_at"] = now.isoformat()
        result["level_changed_at"] = now.isoformat()
    elif action == "dismiss":
        result["prompted"] = PROMPTED_DISMISSED
        result["prompted_at"] = now.isoformat()
    else:
        raise ValueError(f"Invalid consent action: {action!r}")
    return result


def set_level(
    consent: dict[str, Any],
    level: str,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Set the analytics level directly (admin toggle).

    Only updates level and level_changed_at; prompted state is unchanged.
    """
    if level not in VALID_LEVELS:
        raise ValueError(f"Invalid level: {level!r}. Must be one of {VALID_LEVELS}")
    if now is None:
        now = datetime.now(UTC)
    result = dict(consent)
    result["level"] = level
    result["level_changed_at"] = now.isoformat()
    return result


# ---------------------------------------------------------------------------
# Instance-level master switch
# ---------------------------------------------------------------------------


async def is_instance_analytics_enabled(session: AsyncSession) -> bool:
    """Return True if the instance-level master switch is ON.

    Reads SystemConfig first; falls back to env var.
    """
    import os

    config = await get_config(session, INSTANCE_SWITCH_KEY)
    if config is not None:
        if isinstance(config.value, bool):
            return config.value
        if isinstance(config.value, str):
            return config.value.lower() in ("1", "true", "yes")
        return bool(config.value)
    # Env var fallback
    env_val = os.environ.get("MODULO_PRODUCT_ANALYTICS_ENABLED", "")
    return env_val.lower() in ("1", "true", "yes")


def is_egress_allowed(instance_enabled: bool, org_level: str) -> bool:
    """Return True if analytics egress is allowed (instance switch AND org level both on)."""
    return instance_enabled and org_level == LEVEL_ALL


# ---------------------------------------------------------------------------
# Partner carve-out
# ---------------------------------------------------------------------------


def partner_license_requires_analytics(license_data: Any) -> bool:
    """Return True if the license carries product_analytics_required: true.

    Accepts a LicenseData object or any object with a claims dict.
    Returns False for missing/malformed claims (fail-safe).
    """
    try:
        claims = getattr(license_data, "claims", None)
        if claims is None and isinstance(license_data, dict):
            claims = license_data
        if not isinstance(claims, dict):
            return False
        return bool(claims.get(PARTNER_LICENSE_CLAIM, False))
    except Exception:
        _log.warning("partner_license_requires_analytics: failed to read license claims", exc_info=True)
        return False


def is_partner_carve_out_active(license_data: Any, org_level: str) -> bool:
    """Return True if the partner carve-out is active.

    Active when: license has product_analytics_required: true AND org level != 'all'.
    """
    return partner_license_requires_analytics(license_data) and org_level != LEVEL_ALL


# ---------------------------------------------------------------------------
# License enforcement kill switch
# ---------------------------------------------------------------------------


async def is_license_enforcement_enabled(session: AsyncSession) -> bool:
    """Return True if license enforcement is active (kill switch is OFF by default).

    The kill switch is absent → enforced (matching authz_enforce convention).
    """
    config = await get_config(session, LICENSE_ENFORCEMENT_KILL_SWITCH_KEY)
    if config is None:
        return True  # absent = enforced
    return not bool(config.value)
