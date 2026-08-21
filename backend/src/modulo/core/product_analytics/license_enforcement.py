"""Product analytics license enforcement for Partner free Team licenses.

Implements the enforcement logic from the product analytics design doc §6:
Partner licenses carry a ``product_analytics_required`` claim.  When present
and the org's analytics level is not ``"all"``, the effective plan degrades
to Community until analytics is enabled.

Kill switch: ``system_config.product_analytics_license_enforcement_kill_switch``
— absent = enforced (fail-safe, matching ``authz_enforce`` pattern).
"""

from __future__ import annotations

import logging
from typing import Any, Literal

from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from modulo.core.license import LicenseData, parse_and_verify

_log = logging.getLogger(__name__)

PRODUCT_ANALYTICS_REQUIRED_KEY = "product_analytics_required"
ENFORCEMENT_KILL_SWITCH_KEY = "product_analytics_license_enforcement_kill_switch"
_PRODUCT_ANALYTICS_SETTINGS_KEY = "product_analytics"

RequirementResult = Literal["not_required", "satisfied", "pending", "degraded"]


def _extract_org_license(org: Any) -> LicenseData | None:
    """Parse and verify the org-level license key. Returns LicenseData or None."""
    org_settings = getattr(org, "settings_json", None)
    if not isinstance(org_settings, dict):
        return None
    license_key = org_settings.get("license_key")
    if not license_key or not isinstance(license_key, str):
        return None
    try:
        validation = parse_and_verify(license_key)
        if validation.valid and validation.license_data is not None:
            return validation.license_data
    except Exception:
        _log.warning("license_enforcement.parse_org_license_failed", exc_info=True)
    return None


def _license_requires_analytics(license_data: LicenseData | None) -> bool:
    """Check whether the license payload carries the product_analytics_required claim."""
    if license_data is None:
        return False
    raw = license_data.raw_payload
    if not isinstance(raw, dict):
        return False
    claim = raw.get(PRODUCT_ANALYTICS_REQUIRED_KEY)
    return claim is True or claim == "true"


def _org_analytics_level(org: Any) -> str | None:
    """Return the org's product analytics level (``"all"``, ``"off"``, or None if unset)."""
    org_settings = getattr(org, "settings_json", None)
    if not isinstance(org_settings, dict):
        return None
    pa_block = org_settings.get(_PRODUCT_ANALYTICS_SETTINGS_KEY)
    if not isinstance(pa_block, dict):
        return None
    level = pa_block.get("level")
    if level in ("all", "off"):
        return level
    return None


def check_product_analytics_requirement(org: Any) -> RequirementResult:
    """Determine the product analytics requirement status for an org.

    Pure function — no DB reads.  Callers pass the org object (with
    ``settings_json`` already loaded).

    Returns:
        ``"not_required"`` — license doesn't carry the claim (paid or community).
        ``"satisfied"`` — license requires it AND level == ``"all"``.
        ``"pending"`` — license requires it AND level is unset (not yet prompted).
        ``"degraded"`` — license requires it AND level != ``"all"`` (declined / dismissed).
    """
    license_data = _extract_org_license(org)
    if not _license_requires_analytics(license_data):
        return "not_required"

    level = _org_analytics_level(org)
    if level == "all":
        return "satisfied"
    if level is None:
        return "pending"
    return "degraded"


async def is_enforcement_active(session: AsyncSession) -> bool:
    """Check whether the license enforcement kill switch is ON.

    Reads ``system_config.product_analytics_license_enforcement_kill_switch``.
    Absent = enforced (fail-safe, matching the ``authz_enforce`` pattern).
    A truthy value means enforcement is DISABLED (kill switch ON).
    """
    from modulo.db.crud.system_config import get_config

    try:
        config = await get_config(session, ENFORCEMENT_KILL_SWITCH_KEY)
    except SQLAlchemyError:
        _log.warning("license_enforcement.kill_switch_read_failed", exc_info=True)
        return True
    if config is None:
        return True
    return not bool(config.value)


def should_degrade_to_community(org: Any, enforcement_active: bool) -> bool:
    """Combine requirement check + enforcement active to decide degradation.

    Pure function — no DB reads.  Returns True when the org should be
    degraded to Community tier due to missing product analytics consent.
    """
    if not enforcement_active:
        return False
    requirement = check_product_analytics_requirement(org)
    return requirement == "degraded"
