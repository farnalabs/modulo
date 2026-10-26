"""Consent logic, prompt validation, and instance-switch resolution."""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from modulo.core.license import get_license
from modulo.core.product_analytics.constants import (
    DEFAULT_LEVEL,
    DEFAULT_LEVEL_CHANGED_AT,
    DEFAULT_PROMPTED,
    DEFAULT_PROMPTED_AT,
    DISMISS_COOLDOWN,
    ENV_INSTANCE_SWITCH,
    INSTANCE_SWITCH_KEY,
    LEVEL_ALL,
    LEVEL_OFF,
    PARTNER_LICENSE_CLAIM,
    PRODUCT_ANALYTICS_KEY,
    PROMPTED_DISMISSED,
    PROMPTED_NO,
    PROMPTED_YES,
    SWITCH_SOURCE_DEFAULT,
    SWITCH_SOURCE_ENV,
    SWITCH_SOURCE_LICENSE,
    SWITCH_SOURCE_OPERATOR,
    VALID_LEVELS,
)
from modulo.db.crud.system_config import get_config

_log = logging.getLogger(__name__)
_SWITCH_TRUTHY_VALUES = frozenset(("1", "true", "yes"))


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


def org_consent_level(settings_json: Any) -> str:
    """Return an organisation's product-analytics consent level (``all`` / ``off``).

    The single canonical reader for the stored consent level. Both consumers —
    the transparency endpoint (what the page reports) and the metrics dump (what
    actually egresses) — read it through here, so the reported posture cannot
    drift from the egress decision. A missing/blank level, a missing block, or a
    malformed (non-dict) ``settings_json`` degrades to ``off`` (fail-closed)
    rather than raising.
    """
    if not isinstance(settings_json, dict):
        return LEVEL_OFF
    raw_level = get_product_analytics_block(settings_json).get("level")
    return str(raw_level) if raw_level else LEVEL_OFF


def is_org_consenting(settings_json: Any) -> bool:
    """Return True when an organisation's settings enable the ``all`` level."""
    return org_consent_level(settings_json) == LEVEL_ALL


# ---------------------------------------------------------------------------
# Prompt eligibility
# ---------------------------------------------------------------------------


def _parse_prompted_at(prompted_at: Any) -> datetime | None:
    """Parse prompted_at value to an aware datetime, or return None on failure."""
    if prompted_at is None:
        return None
    if isinstance(prompted_at, str):
        try:
            parsed = datetime.fromisoformat(prompted_at)
        except (ValueError, TypeError):
            return None
    else:
        parsed = prompted_at
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed


def _is_cooldown_expired(prompted_at: datetime | None) -> bool:
    """Check if the dismiss cooldown has expired."""
    if prompted_at is None:
        return True
    return datetime.now(UTC) - prompted_at >= DISMISS_COOLDOWN


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
    prompted_at = _parse_prompted_at(consent.get("prompted_at"))
    return _is_cooldown_expired(prompted_at)


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


@dataclass(frozen=True)
class AnalyticsSwitchState:
    """The effective instance switch plus where its value came from.

    ``source`` is one of ``constants.SWITCH_SOURCE_*``:

    * ``operator``    - an operator explicitly set ``system_config[INSTANCE_SWITCH_KEY]``
    * ``environment`` - the operator set ``MODULO_PRODUCT_ANALYTICS_ENABLED``
    * ``licence``     - nothing explicit, but the installed licence requires analytics
    * ``default``     - nothing explicit and no licence requirement: OFF

    ``operator_value`` is the explicit value an operator set (stored or env), or
    ``None`` when the value is licence/default-derived.
    """

    enabled: bool
    source: str
    licence_requires_analytics: bool
    operator_value: bool | None = None


def coerce_switch_value(value: Any) -> bool:
    """Interpret a stored/env instance-switch value (fail-closed).

    Only an explicitly truthy value is ON: boolean ``True``, a non-zero number,
    or one of the strings ``"1"``/``"true"``/``"yes"`` (case-insensitive).
    Everything else - including the strings ``"false"``, ``"0"``, ``"no"`` and
    any non-numeric container - is OFF. This is deliberately NOT ``bool(value)``:
    a stored ``"false"`` (or an unexpected list/dict) must never run the dump.
    """
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value != 0
    if isinstance(value, str):
        return value.strip().lower() in _SWITCH_TRUTHY_VALUES
    return False


async def resolve_instance_analytics_switch(session: AsyncSession) -> AnalyticsSwitchState:
    """Resolve the effective instance-level analytics switch and its provenance.

    PRECEDENCE - an operator always wins; a licence only supplies a DEFAULT:

    1. ``system_config["product_analytics_enabled"]`` present -> the operator's
       explicit value (ON *or* OFF). An explicit OFF vetoes even a licence that
       requires analytics.
    2. ``MODULO_PRODUCT_ANALYTICS_ENABLED`` set -> the deployment operator's
       value (same veto).
    3. Otherwise the installed licence decides: ``product_analytics_required``
       present -> ON (source ``licence``); absent -> OFF (source ``default``).

    A licence can therefore only turn the DEFAULT on - it can never override an
    operator. Nothing here degrades an organisation's plan or tier for an
    analytics reason: a partner that disables the dump is breaching a contract,
    which is a conversation, not a software control.
    """
    licence_requires = partner_license_requires_analytics(get_license())

    config = await get_config(session, INSTANCE_SWITCH_KEY)
    if config is not None:
        operator_value = coerce_switch_value(config.value)
        return AnalyticsSwitchState(
            enabled=operator_value,
            source=SWITCH_SOURCE_OPERATOR,
            licence_requires_analytics=licence_requires,
            operator_value=operator_value,
        )

    env_raw = os.environ.get(ENV_INSTANCE_SWITCH)
    if env_raw is not None:
        # A SET-but-blank variable is an explicit operator value, not "unset":
        # an exported-but-empty variable must read OFF (fail-closed) rather than
        # silently falling through to a licence that would turn analytics ON.
        env_value = coerce_switch_value(env_raw)
        return AnalyticsSwitchState(
            enabled=env_value,
            source=SWITCH_SOURCE_ENV,
            licence_requires_analytics=licence_requires,
            operator_value=env_value,
        )

    if licence_requires:
        return AnalyticsSwitchState(
            enabled=True,
            source=SWITCH_SOURCE_LICENSE,
            licence_requires_analytics=True,
        )

    return AnalyticsSwitchState(
        enabled=False,
        source=SWITCH_SOURCE_DEFAULT,
        licence_requires_analytics=False,
    )


async def is_instance_analytics_enabled(session: AsyncSession) -> bool:
    """Return True when the effective instance-level analytics switch is ON.

    Delegates to :func:`resolve_instance_analytics_switch` so every reader (the
    dump gate, the consent routes and the transparency endpoint) applies the
    same precedence and cannot disagree about whether egress is enabled.
    """
    return (await resolve_instance_analytics_switch(session)).enabled


def is_egress_allowed(instance_enabled: bool, org_level: str) -> bool:
    """Return True if analytics egress is allowed (instance switch AND org level both on)."""
    return instance_enabled and org_level == LEVEL_ALL


# ---------------------------------------------------------------------------
# Licence-required analytics (drives the DEFAULT, never enforcement)
# ---------------------------------------------------------------------------


def partner_license_requires_analytics(license_data: Any) -> bool:
    """Return True when the licence carries ``product_analytics_required: true``.

    Accepts a :class:`modulo.core.license.LicenseData` (reads its signed
    ``raw_payload``), an object exposing a ``claims`` mapping, or a mapping
    itself. The claim must be exactly boolean ``True`` or the string
    ``"true"``; everything else (including ``"false"``, a missing claim, or
    missing/malformed input) is False - fail-safe, because this can only ever
    turn the default ON.
    """
    try:
        payload: Any
        if isinstance(license_data, dict):
            payload = license_data
        else:
            payload = getattr(license_data, "claims", None)
            if not isinstance(payload, dict):
                payload = getattr(license_data, "raw_payload", None)
        if not isinstance(payload, dict):
            return False
        claim = payload.get(PARTNER_LICENSE_CLAIM, False)
        if claim is True:
            return True
        return isinstance(claim, str) and claim.strip().lower() == "true"
    except Exception:
        _log.warning("partner_license_requires_analytics: failed to read license claims", exc_info=True)
        return False
