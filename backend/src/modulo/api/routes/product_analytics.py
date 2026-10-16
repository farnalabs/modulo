"""Product analytics consent & settings routes (FAR-354).

Provides:
- POST /api/v1/org/product-analytics/consent  — accept/decline/dismiss
- GET  /api/v1/org/product-analytics          — current consent state + instance switch
- PUT  /api/v1/org/product-analytics          — admin level toggle
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from datetime import UTC, datetime
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel
from sqlalchemy import delete, select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from modulo.api.constants import MSG_INTERNAL_SERVER_ERROR
from modulo.api.db_error_handling import handle_db_errors, raise_lock_timeout_conflict_if
from modulo.api.dependencies import get_db_session, require_permission
from modulo.api.routes._org_settings import apply_org_settings_patch_or_404
from modulo.auth.dependencies import get_current_tenant_user
from modulo.auth.jwt import TenantPrincipal
from modulo.core.audit_coverage import audited
from modulo.core.audit_logger import append_audit_event
from modulo.core.product_analytics.consent import (
    apply_consent_action,
    get_product_analytics_block,
    is_egress_allowed,
    is_instance_analytics_enabled,
    is_prompt_eligible,
    set_level,
)
from modulo.core.product_analytics.constants import DEFAULT_LEVEL, LEVEL_OFF, PRODUCT_ANALYTICS_KEY
from modulo.db.models.metrics_staging import MetricsStaging
from modulo.db.models.organisation import Organisation
from modulo.db.rls import set_rls_org
from modulo.db.soft_delete import include_soft_deleted

_log = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/org/product-analytics", tags=["org", "product-analytics"])


# ---------------------------------------------------------------------------
# Request / response models
# ---------------------------------------------------------------------------


class ConsentRequest(BaseModel):
    action: Literal["accept", "decline", "dismiss"]


class ConsentResponse(BaseModel):
    level: str = DEFAULT_LEVEL
    prompted: str | None = None
    prompted_at: str | None = None
    level_changed_at: str | None = None
    instance_enabled: bool = False
    egress_allowed: bool = False
    prompt_eligible: bool = False


class LevelUpdateRequest(BaseModel):
    level: Literal["off", "all"]


class LevelUpdateResponse(BaseModel):
    level: str
    level_changed_at: str | None = None


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def _get_org_or_404(
    session: AsyncSession,
    org_id: uuid.UUID,
) -> Organisation:
    """Fetch an organisation by ID or raise 404.

    FAR-1025: opts out of the global soft-delete filter -- a pending-deletion
    org is still operationally live; the consent check must still work during
    the confirmation window.

    FAR-1832: this is a READ-ONLY existence check. The writers no longer take
    a ``FOR UPDATE`` row lock here — they write through the single-statement
    atomic settings patch, whose UPDATE takes the row lock only for its own
    statement (and refuses a soft-deleted org with ``LookupError``, which the
    handlers map to the same 404).
    """
    stmt = include_soft_deleted(select(Organisation).where(Organisation.id == org_id))
    result = await session.execute(stmt)
    org = result.scalar_one_or_none()
    if org is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Organisation not found.",
        )
    return org


async def _purge_staged_events(session: AsyncSession, org_id: uuid.UUID) -> None:
    """Delete the org's staged product-analytics events on consent withdrawal.

    Called whenever the level lands on ``off`` (admin toggle or prompt decline):
    events staged while the org was consenting are dropped immediately rather
    than held until the daily dump/retention purge (FAR-1717). The session
    already carries ``set_rls_org``, and the ``rls_org_isolation`` policy scopes
    the DELETE to this org. Idempotent — re-running on an already-off level is
    a no-op delete.
    """
    await session.execute(delete(MetricsStaging).where(MetricsStaging.organisation_id == org_id))


def _build_consent_response(
    consent: dict[str, object],
    instance_enabled: bool,
) -> ConsentResponse:
    raw_level = consent.get("level", DEFAULT_LEVEL)
    level = str(raw_level) if raw_level is not None else DEFAULT_LEVEL
    egress = is_egress_allowed(instance_enabled, level)
    prompted = consent.get("prompted")
    prompted_at = consent.get("prompted_at")
    level_changed_at = consent.get("level_changed_at")
    return ConsentResponse(
        level=level,
        prompted=prompted if isinstance(prompted, str) else None,
        prompted_at=prompted_at if isinstance(prompted_at, str) else None,
        level_changed_at=level_changed_at if isinstance(level_changed_at, str) else None,
        instance_enabled=instance_enabled,
        egress_allowed=egress,
        prompt_eligible=is_prompt_eligible(consent),
    )


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------


@router.post(
    "/consent",
    dependencies=[
        Depends(
            audited(
                "product_analytics_consent_posted", "product_analytics_consent", principal_dep=get_current_tenant_user
            )
        )
    ],
)
@handle_db_errors("product_analytics.consent")
async def post_consent(
    req: ConsentRequest,
    current_user: TenantPrincipal = Depends(get_current_tenant_user),
    session: AsyncSession = Depends(get_db_session),
) -> ConsentResponse:
    """Accept, decline, or dismiss the product analytics consent prompt."""
    try:
        async with session.begin():
            await set_rls_org(session, current_user.organisation_id)
            org = await _get_org_or_404(session, current_user.organisation_id)

            consent = get_product_analytics_block(org.settings_json)

            # Prompt eligibility gate: only eligible orgs can consent
            if not is_prompt_eligible(consent):
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT,
                    detail="Consent prompt is not currently eligible. Dismissed prompts re-appear after 7 days.",
                )

            # FAR-1832 QA F1/F8: NESTED sub-field patch, composed ONLY from the
            # request — ``apply_consent_action({}, action, now)`` derives the
            # exact sub-fields this action changes WITHOUT consulting the
            # stored block, and the helper's ``jsonb_set(… ||
            # :nested_patch)`` composes them into the STORED product_analytics
            # value at the sub-field level. Option (b) over a FOR UPDATE
            # pre-lock because the write does NOT depend on the previous stored
            # value: each action emits only its own sub-fields — ``accept`` and
            # ``decline`` write ``level``, ``level_changed_at`` AND the
            # ``prompted*`` pair, while ``dismiss`` writes only ``prompted*`` —
            # so two concurrent writers of DIFFERENT sub-fields both survive
            # (the old whole-block patch replaced the entire product_analytics
            # value and lost one of them). A soft-deleted org
            # (pending-deletion window) is refused with ``LookupError`` → the
            # same 404 — the FAR-1830 retention-writer precedent; reads keep
            # their FAR-1025 opt-out above.
            now = datetime.now(UTC)
            subpatch = apply_consent_action({}, req.action, now)
            # QA F8: drive the response payload AND the LEVEL_OFF purge decision
            # from the block the patch RETURNED (the committed post-write state),
            # never a pre-write read. Under a concurrent writer that lands a
            # different level between our read and our write, a stale pre-read
            # would report — and purge on — a level the org is no longer on. The
            # ``LookupError`` → 404 translation is the shared route helper
            # (QA F7), byte-identical to the hand-copied block it replaces.
            returned = await apply_org_settings_patch_or_404(
                session,
                current_user.organisation_id,
                nested_patch={PRODUCT_ANALYTICS_KEY: subpatch},
            )
            updated_consent = get_product_analytics_block(returned)

            # Consent withdrawn -> drop staged events immediately (FAR-1717);
            # they can never be dumped for a non-consenting org anyway.
            if updated_consent.get("level") == LEVEL_OFF:
                await _purge_staged_events(session, current_user.organisation_id)

            await append_audit_event(
                session,
                org_id=current_user.organisation_id,
                event_type="product_analytics_consent",
                actor_user_id=current_user.user_id,
                payload_json={"action": req.action, "level": updated_consent.get("level")},
            )

            instance_enabled = await is_instance_analytics_enabled(session)

            return _build_consent_response(updated_consent, instance_enabled)
    except HTTPException:
        raise
    except asyncio.CancelledError:
        raise
    except SQLAlchemyError as exc:
        # FAR-1832 QA F2: the settings patch bounds its row-lock wait, so a
        # 55P03 lock timeout is reachable here. Classify it (409 conflict)
        # via the shared classifier, then re-raise every other DB error so
        # ``handle_db_errors`` maps it — genuine connection/unavailability
        # errors stay 503, session-contract violations stay 500 — instead of
        # the old blanket 500 below.
        raise_lock_timeout_conflict_if(exc, "product_analytics.consent")
        raise
    except Exception:
        _log.exception("product_analytics.consent unexpected error")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=MSG_INTERNAL_SERVER_ERROR,
        ) from None


@router.get("")
@handle_db_errors("product_analytics.get")
async def get_product_analytics(
    current_user: TenantPrincipal = Depends(get_current_tenant_user),
    session: AsyncSession = Depends(get_db_session),
) -> ConsentResponse:
    """Read the current product analytics consent state and instance switch."""
    try:
        async with session.begin():
            await set_rls_org(session, current_user.organisation_id)
            org = await _get_org_or_404(session, current_user.organisation_id)

            consent = get_product_analytics_block(org.settings_json)
            instance_enabled = await is_instance_analytics_enabled(session)

            return _build_consent_response(consent, instance_enabled)
    except HTTPException:
        raise
    except asyncio.CancelledError:
        raise
    except Exception:
        _log.exception("product_analytics.get unexpected error")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=MSG_INTERNAL_SERVER_ERROR,
        ) from None


@router.put(
    "",
    dependencies=[
        Depends(
            audited(
                "product_analytics_level_updated", "product_analytics_settings", principal_dep=get_current_tenant_user
            )
        )
    ],
)
@handle_db_errors("product_analytics.update_level")
async def update_product_analytics_level(
    req: LevelUpdateRequest,
    current_user: TenantPrincipal = require_permission("org.settings.update"),
    session: AsyncSession = Depends(get_db_session),
) -> LevelUpdateResponse:
    """Update the analytics level (admin toggle).

    Turning off sets level=off and purges the org's ``metrics_staging`` rows in
    the same transaction (FAR-1717): staged events are dropped immediately on
    withdrawal instead of waiting for the daily dump's consumed/expired purge.
    """
    try:
        async with session.begin():
            await set_rls_org(session, current_user.organisation_id)
            await _get_org_or_404(session, current_user.organisation_id)

            now = datetime.now(UTC)
            # FAR-1832 QA F1/F8: same contract as the consent POST — the subpatch
            # is composed ONLY from the request (``set_level({}, level, now)``
            # emits exactly this toggle's sub-fields) and is written through
            # the helper's nested sub-field patch, so a concurrent consent
            # writer's prompted* sub-fields survive (no ``FOR UPDATE``
            # pre-lock, no whole-block replace, soft-deleted org → 404). The
            # response and the LEVEL_OFF purge decision both read the block the
            # patch RETURNED, never a stale pre-write read (QA F8).
            subpatch = set_level({}, req.level, now)
            returned = await apply_org_settings_patch_or_404(
                session,
                current_user.organisation_id,
                nested_patch={PRODUCT_ANALYTICS_KEY: subpatch},
            )
            updated_consent = get_product_analytics_block(returned)

            # Turning analytics OFF withdraws consent: drop staged events now
            # (FAR-1717), in the same transaction as the level write.
            if updated_consent["level"] == LEVEL_OFF:
                await _purge_staged_events(session, current_user.organisation_id)

            await append_audit_event(
                session,
                org_id=current_user.organisation_id,
                event_type="product_analytics_level_update",
                actor_user_id=current_user.user_id,
                payload_json={"level": req.level},
            )

            return LevelUpdateResponse(
                level=updated_consent["level"],
                level_changed_at=updated_consent.get("level_changed_at"),
            )
    except HTTPException:
        raise
    except asyncio.CancelledError:
        raise
    except SQLAlchemyError as exc:
        # FAR-1832 QA F2: same contract as the consent POST — 55P03 is a 409
        # conflict via the shared classifier; other DB errors re-raise to
        # ``handle_db_errors`` (503 for outages) instead of a blanket 500.
        raise_lock_timeout_conflict_if(exc, "product_analytics.update_level")
        raise
    except Exception:
        _log.exception("product_analytics.update_level unexpected error")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=MSG_INTERNAL_SERVER_ERROR,
        ) from None
