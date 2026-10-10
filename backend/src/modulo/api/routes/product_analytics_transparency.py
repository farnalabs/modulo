"""Product analytics transparency endpoint."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Annotated, Any

from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from modulo.api.db_error_handling import handle_db_errors
from modulo.api.dependencies import get_db_session, require_system_permission
from modulo.auth.jwt import AuthenticatedPrincipal
from modulo.core.product_analytics.consent import (
    is_egress_allowed,
    is_instance_analytics_enabled,
    is_license_enforcement_enabled,
    is_org_consenting,
    org_consent_level,
)
from modulo.core.product_analytics.constants import (
    DUMP_COUNT_KEY,
    DUMP_WATERMARK_KEY,
    LEVEL_ALL,
    LEVEL_OFF,
    coerce_dump_count,
)
from modulo.db.crud.system_config import get_config
from modulo.db.models.organisation import Organisation
from modulo.db.soft_delete import include_soft_deleted

_CODE_PRODUCT_ANALYTICS_MANAGE = "system.config.manage"

_STALE_WARNING_DAYS = 3

router = APIRouter(
    prefix="/api/v1/product-analytics",
    tags=["product-analytics-transparency"],
)


class TransparencyResponse(BaseModel):
    last_successful_dump_at: str | None = None
    dump_count_total: int = 0
    consent_level: str = LEVEL_OFF
    instance_enabled: bool = False
    enforcement_enabled: bool = False
    egress_allowed: bool = False
    warning: str | None = None


def _coerce_last_dump(value: Any) -> str | None:
    """Normalise the stored watermark into the response timestamp field.

    A stored string passes through unchanged; any other non-None scalar is
    stringified (mirrors the historical endpoint coercion); a missing value
    yields ``None``.
    """
    if isinstance(value, str):
        return value
    if value is not None:
        return str(value)
    return None


async def _resolve_org(
    session: AsyncSession,
    organisation_id: uuid.UUID | None,
) -> Organisation | None:
    """Load the caller's organisation row, or ``None`` when it cannot be resolved.

    Mirrors ``api/routes/product_analytics.py``'s org resolution: a soft-deleted
    org is still operationally live, so the read opts out of the global
    soft-delete filter. The transparency endpoint is observability-only — an
    unresolvable org downgrades to the instance-level consent posture rather
    than 404ing.

    Returns the row (not just its settings) so the caller can distinguish
    "org resolved but has no settings" from "org unresolved" — the two take
    different consent sources.
    """
    if organisation_id is None:
        return None
    stmt = include_soft_deleted(select(Organisation).where(Organisation.id == organisation_id))
    result = await session.execute(stmt)
    return result.scalar_one_or_none()


async def _instance_consent_level(session: AsyncSession) -> str:
    """Return the INSTANCE-level consent posture for an unresolved caller org.

    Product decision (FAR-1635, Duncan): per-org consent is the preferred
    source — a system admin sees their own organisation's level. But when the
    caller's organisation cannot be resolved (no ``organisation_id``, or no
    matching row) the transparency surface must still report a truthful
    posture rather than a hardcoded ``off``; "per instance acceptable".

    The aggregate uses the SAME consent predicate as the daily dump
    (``metrics_dump._get_consenting_orgs``), both reading the level through
    ``consent.org_consent_level``: an ACTIVE organisation whose ``settings_json``
    enables the ``all`` level. At least one such org -> ``LEVEL_ALL``; otherwise
    ``LEVEL_OFF``. The ``organisations`` table is not RLS-scoped, so this
    cross-org read is safe on the app-role session.
    """
    stmt = select(Organisation).where(Organisation.status == "active")
    result = await session.execute(stmt)
    for org in result.scalars():
        if is_org_consenting(org.settings_json):
            return LEVEL_ALL
    return LEVEL_OFF


def _stale_dump_warning(last_dump_at: str | None, consent_level: str) -> str | None:
    """The staleness warning when dumps stopped reaching farnalabs.

    Fires only when the last successful dump is older than
    *_STALE_WARNING_DAYS* AND consent is ``all``. An unparseable timestamp
    never warns (best-effort, fail-silent).
    """
    if not last_dump_at:
        return None
    try:
        last_dt = datetime.fromisoformat(last_dump_at)
        now = datetime.now(UTC)
        if last_dt.tzinfo is None:
            last_dt = last_dt.replace(tzinfo=UTC)
        days_since = (now - last_dt).total_seconds() / 86400
        if days_since > _STALE_WARNING_DAYS and consent_level == LEVEL_ALL:
            return "not_reaching_farnalabs"
    except (ValueError, TypeError):
        pass
    return None


@router.get("/transparency")
@handle_db_errors("product_analytics.transparency")
async def get_transparency(
    session: Annotated[AsyncSession, Depends(get_db_session)],
    principal: AuthenticatedPrincipal = require_system_permission(_CODE_PRODUCT_ANALYTICS_MANAGE),  # type: ignore[assignment]
) -> TransparencyResponse:
    # Report the instance's REAL product-analytics posture. Every field is
    # sourced from the state a feature actually writes, so the
    # /admin/product-analytics page reflects reality rather than a set of
    # never-written config keys:
    #
    #   * instance_enabled - consent.is_instance_analytics_enabled (the same
    #     helper the dump gate uses): bool/string-aware (a stored "false" is OFF,
    #     not fail-open) with the MODULO_PRODUCT_ANALYTICS_ENABLED env fallback.
    #   * enforcement_enabled - consent.is_license_enforcement_enabled (the real
    #     license-enforcement kill switch; absent = enforced).
    #   * last_successful_dump_at / dump_count_total - the keys the metrics dump
    #     actually writes (constants.DUMP_WATERMARK_KEY / DUMP_COUNT_KEY, which
    #     live in the langgraph-free constants module so the API can import them
    #     statically without violating the import-linter contract).
    #   * consent_level - the caller's organisation real consent level
    #     (org.settings_json["product_analytics"]["level"], read through
    #     consent.org_consent_level) is the PREFERRED source. When the caller org
    #     cannot be resolved the endpoint reports an INSTANCE-level posture
    #     instead of a hardcoded "off" (see _instance_consent_level and
    #     FAR-1635).
    #
    # Deliberately a comment, not a docstring: FastAPI publishes a handler
    # docstring as the operation description in the OpenAPI schema, which would
    # make the committed frontend schema.ts stale.
    async with session.begin():
        instance_enabled = await is_instance_analytics_enabled(session)
        enforcement_enabled = await is_license_enforcement_enabled(session)
        last_dump_entry = await get_config(session, DUMP_WATERMARK_KEY)
        dump_count_entry = await get_config(session, DUMP_COUNT_KEY)
        org = await _resolve_org(session, principal.organisation_id)
        if org is None:
            # Per-org is preferred, but an unresolvable caller org falls back
            # to the instance-level posture ("per instance acceptable").
            consent_level = await _instance_consent_level(session)
        else:
            consent_level = org_consent_level(org.settings_json)

    last_dump_at = _coerce_last_dump(last_dump_entry.value if last_dump_entry else None)
    dump_count_total = coerce_dump_count(dump_count_entry.value if dump_count_entry else None)
    warning = _stale_dump_warning(last_dump_at, consent_level)

    return TransparencyResponse(
        last_successful_dump_at=last_dump_at,
        dump_count_total=dump_count_total,
        consent_level=consent_level,
        instance_enabled=instance_enabled,
        enforcement_enabled=enforcement_enabled,
        egress_allowed=is_egress_allowed(instance_enabled, consent_level),
        warning=warning,
    )
