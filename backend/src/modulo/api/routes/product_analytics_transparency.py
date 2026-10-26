"""Product analytics transparency endpoint and instance-switch writer."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Annotated, Any

from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from modulo.api.db_error_handling import handle_db_errors
from modulo.api.dependencies import get_db_session, require_system_permission, resolve_audit_principal
from modulo.auth.jwt import AuthenticatedPrincipal
from modulo.core.audit_coverage import audited
from modulo.core.product_analytics.consent import (
    is_egress_allowed,
    is_org_consenting,
    org_consent_level,
    resolve_instance_analytics_switch,
)
from modulo.core.product_analytics.constants import (
    DUMP_COUNT_KEY,
    DUMP_WATERMARK_KEY,
    INSTANCE_SWITCH_KEY,
    LEVEL_ALL,
    LEVEL_OFF,
    SWITCH_SOURCE_DEFAULT,
    coerce_dump_count,
)
from modulo.core.system_audit_logger import append_system_audit_event
from modulo.db.crud.system_config import delete_config, get_config, update_config
from modulo.db.models.organisation import Organisation
from modulo.db.soft_delete import include_soft_deleted

_CODE_PRODUCT_ANALYTICS_MANAGE = "system.config.manage"
_AUDIT_EVENT_INSTANCE_SWITCH_UPDATED = "product_analytics_instance_switch_updated"

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
    #: Where ``instance_enabled`` came from: ``operator`` (stored setting),
    #: ``environment`` (MODULO_PRODUCT_ANALYTICS_ENABLED), ``licence`` (the
    #: installed licence requires analytics) or ``default`` (off).
    instance_switch_source: str = SWITCH_SOURCE_DEFAULT
    #: True when the installed licence carries ``product_analytics_required``.
    #: This only supplies the DEFAULT - an operator can always turn it off.
    licence_requires_analytics: bool = False
    egress_allowed: bool = False
    warning: str | None = None


class InstanceSwitchUpdateRequest(BaseModel):
    #: ``true`` / ``false`` writes an explicit operator value (which always wins,
    #: including OFF over a licence that requires analytics). ``null`` clears the
    #: stored value so the licence-derived default applies again.
    enabled: bool | None = None


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


async def _build_transparency(
    session: AsyncSession,
    principal: AuthenticatedPrincipal,
) -> TransparencyResponse:
    """Read the instance's real product-analytics posture.

    Every field is sourced from the state a feature actually writes, so the
    /admin/product-analytics page reflects reality rather than a set of
    never-written config keys:

      * instance_enabled / instance_switch_source / licence_requires_analytics -
        consent.resolve_instance_analytics_switch, the SAME helper the dump gate
        uses. Precedence: explicit operator value (stored system_config, then
        MODULO_PRODUCT_ANALYTICS_ENABLED) -> licence-required default -> OFF.
      * last_successful_dump_at / dump_count_total - the keys the metrics dump
        actually writes (constants.DUMP_WATERMARK_KEY / DUMP_COUNT_KEY, which
        live in the langgraph-free constants module so the API can import them
        statically without violating the import-linter contract).
      * consent_level - the caller's organisation real consent level
        (org.settings_json["product_analytics"]["level"], read through
        consent.org_consent_level) is the PREFERRED source. When the caller org
        cannot be resolved the endpoint reports an INSTANCE-level posture
        instead of a hardcoded "off" (see _instance_consent_level and
        FAR-1635).
    """
    switch = await resolve_instance_analytics_switch(session)
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
        instance_enabled=switch.enabled,
        instance_switch_source=switch.source,
        licence_requires_analytics=switch.licence_requires_analytics,
        egress_allowed=is_egress_allowed(switch.enabled, consent_level),
        warning=warning,
    )


@router.get("/transparency")
@handle_db_errors("product_analytics.transparency")
async def get_transparency(
    session: Annotated[AsyncSession, Depends(get_db_session)],
    principal: AuthenticatedPrincipal = require_system_permission(_CODE_PRODUCT_ANALYTICS_MANAGE),  # type: ignore[assignment]
) -> TransparencyResponse:
    async with session.begin():
        return await _build_transparency(session, principal)


@router.put(
    "/instance-switch",
    dependencies=[
        Depends(
            audited(
                "api_access_put",
                "product_analytics",
                principal_dep=resolve_audit_principal,
            )
        )
    ],
)
@handle_db_errors("product_analytics.update_instance_switch")
async def update_instance_switch(
    req: InstanceSwitchUpdateRequest,
    session: Annotated[AsyncSession, Depends(get_db_session)],
    principal: AuthenticatedPrincipal = require_system_permission(_CODE_PRODUCT_ANALYTICS_MANAGE),  # type: ignore[assignment]
) -> TransparencyResponse:
    # Write or clear the instance-level analytics switch (system admin only).
    # An explicit value always wins - including OFF over a licence that requires
    # analytics. Clearing the stored value restores the licence-derived default.
    # The change is audited in the org-independent system ledger with the action,
    # the operator value and the resulting effective state/source.
    #
    # Deliberately a comment, not a docstring: FastAPI publishes a handler
    # docstring as the operation description in the OpenAPI schema, which would
    # make the committed frontend schema.ts stale.
    async with session.begin():
        if req.enabled is None:
            cleared = await delete_config(session, INSTANCE_SWITCH_KEY)
            action = "cleared" if cleared else "already_default"
        else:
            await update_config(
                session,
                INSTANCE_SWITCH_KEY,
                req.enabled,
                updated_by=principal.account_id,
            )
            action = "set"

        # Resolve inside the same transaction so the response (and the audit
        # payload) reflect the committed-through-this-session state.
        state = await resolve_instance_analytics_switch(session)

        await append_system_audit_event(
            session,
            event_type=_AUDIT_EVENT_INSTANCE_SWITCH_UPDATED,
            org_id=principal.organisation_id,
            actor_user_id=principal.account_id,
            resource_type="product_analytics",
            payload_json={
                "action": action,
                "operator_value": req.enabled,
                "effective_enabled": state.enabled,
                "source": state.source,
            },
        )

        return await _build_transparency(session, principal)
