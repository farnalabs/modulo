"""Admin evidence retention routes (FAR-961, chunk 9a §2.9).

GET  /api/v1/admin/evidence-retention — read the current policy.
PUT  /api/v1/admin/evidence-retention — update the policy.
POST /api/v1/admin/evidence-retention/purge — trigger a manual purge.

Auth: org admins operate within their own org; system admins may target any org.
Permission ``evidence_retention.manage`` (admin).
"""

from __future__ import annotations

import logging
import uuid
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi import status as http_status  # nosemgrep: loopvar-shadows-import
from pydantic import BaseModel, Field
from sqlalchemy.exc import IntegrityError, ProgrammingError, SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from modulo.api.constants import MSG_UNEXPECTED_ERROR
from modulo.api.dependencies import (
    get_db_session,
    require_system_or_org_admin,
)
from modulo.auth.jwt import TenantPrincipal
from modulo.core.evidence_retention import (
    DEFAULT_MAX_AGE_DAYS,
    EvidenceRetentionPolicy,
    PurgeResult,
    count_evidence_rows,
    load_policy,
    purge_evidence,
    save_policy,
)
from modulo.db.rls import set_rls_org

_log = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/admin/evidence-retention", tags=["admin-evidence-retention"])

_PERMISSION = "evidence_retention.manage"


# ── Request / response models ────────────────────────────────────────────


class EvidenceRetentionPolicyResponse(BaseModel):
    """Public admin response for the evidence retention policy."""

    max_age_days: int = DEFAULT_MAX_AGE_DAYS
    max_rows: int | None = None
    batch_size: int = 500
    lock_timeout_seconds: float = 30.0
    current_row_count: int = 0


class UpdateEvidenceRetentionPolicyRequest(BaseModel):
    """Request body for updating the evidence retention policy."""

    max_age_days: int = Field(default=DEFAULT_MAX_AGE_DAYS, ge=1, le=3650)
    max_rows: int | None = Field(default=None, ge=1)
    batch_size: int = Field(default=500, ge=1, le=10000)
    lock_timeout_seconds: float = Field(default=30.0, gt=0, le=300)


class PurgeResponse(BaseModel):
    """Response from a manual evidence purge."""

    rows_deleted: int
    batches: int
    max_age_days: int
    max_rows: int | None = None


# ── Helpers ──────────────────────────────────────────────────────────────


def _resolve_org_id(principal: TenantPrincipal, organisation_id: uuid.UUID | None) -> uuid.UUID | None:
    """Resolve the effective org scope for the request.

    A system admin may target any org via ``organisation_id`` (None = all orgs,
    cross-tenant). An org admin is always bound to their own organisation — a
    mismatched ``organisation_id`` is rejected rather than silently ignored.
    """
    if principal.is_system_admin:
        return organisation_id
    if organisation_id is not None and organisation_id != principal.organisation_id:
        raise HTTPException(
            status_code=http_status.HTTP_403_FORBIDDEN,
            detail="Org admins may only operate on their own organisation",
        )
    return principal.organisation_id


# ── Routes ───────────────────────────────────────────────────────────────


@router.get("")
async def get_evidence_retention(
    session: Annotated[AsyncSession, Depends(get_db_session)],
    organisation_id: Annotated[Any | None, Query()] = None,
    principal: TenantPrincipal = require_system_or_org_admin(_PERMISSION),
) -> EvidenceRetentionPolicyResponse:
    """Read the current evidence retention policy for the caller's org."""
    org_id = _resolve_org_id(principal, organisation_id)

    try:
        async with session.begin():
            await set_rls_org(session, org_id)
            policy = await load_policy(session, org_id)
            row_count = await count_evidence_rows(session, org_id)
    except ProgrammingError:
        _log.exception("evidence_retention.get.programming_error")
        raise HTTPException(
            status_code=http_status.HTTP_501_NOT_IMPLEMENTED,
            detail="Feature is not available. Run database migrations to enable it.",
        ) from None
    except SQLAlchemyError:
        _log.exception("evidence_retention.get.db_error")
        raise HTTPException(
            status_code=http_status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Database temporarily unavailable.",
        ) from None
    except HTTPException:
        raise
    except Exception:
        _log.exception("evidence_retention.get.unexpected_error")
        raise HTTPException(
            status_code=http_status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=MSG_UNEXPECTED_ERROR,
        ) from None

    return EvidenceRetentionPolicyResponse(
        max_age_days=policy.max_age_days,
        max_rows=policy.max_rows,
        batch_size=policy.batch_size,
        lock_timeout_seconds=policy.lock_timeout_seconds,
        current_row_count=row_count,
    )


@router.put("", status_code=http_status.HTTP_200_OK)
async def update_evidence_retention(
    req: UpdateEvidenceRetentionPolicyRequest,
    session: Annotated[AsyncSession, Depends(get_db_session)],
    organisation_id: Annotated[Any | None, Query()] = None,
    principal: TenantPrincipal = require_system_or_org_admin(_PERMISSION),
) -> EvidenceRetentionPolicyResponse:
    """Update the evidence retention policy for the caller's org."""
    org_id = _resolve_org_id(principal, organisation_id)

    new_policy = EvidenceRetentionPolicy(
        max_age_days=req.max_age_days,
        max_rows=req.max_rows,
        batch_size=req.batch_size,
        lock_timeout_seconds=req.lock_timeout_seconds,
    )

    try:
        async with session.begin():
            await set_rls_org(session, org_id)
            await save_policy(session, org_id, new_policy)
            row_count = await count_evidence_rows(session, org_id)
    except ValueError as exc:
        _log.warning("evidence_retention.update.org_not_found", extra={"org_id": str(org_id)})
        raise HTTPException(
            status_code=http_status.HTTP_404_NOT_FOUND,
            detail=str(exc),
        ) from exc
    except IntegrityError:
        _log.exception("evidence_retention.update.integrity_error")
        raise HTTPException(
            status_code=http_status.HTTP_409_CONFLICT,
            detail="Resource conflict. The policy could not be updated.",
        ) from None
    except ProgrammingError:
        _log.exception("evidence_retention.update.programming_error")
        raise HTTPException(
            status_code=http_status.HTTP_501_NOT_IMPLEMENTED,
            detail="Feature is not available. Run database migrations to enable it.",
        ) from None
    except SQLAlchemyError:
        _log.exception("evidence_retention.update.db_error")
        raise HTTPException(
            status_code=http_status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Database temporarily unavailable.",
        ) from None
    except HTTPException:
        raise
    except Exception:
        _log.exception("evidence_retention.update.unexpected_error")
        raise HTTPException(
            status_code=http_status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=MSG_UNEXPECTED_ERROR,
        ) from None

    _log.info(
        "evidence.retention.policy_updated",
        extra={
            "org_id": str(org_id),
            "max_age_days": req.max_age_days,
            "max_rows": req.max_rows,
        },
    )
    return EvidenceRetentionPolicyResponse(
        max_age_days=new_policy.max_age_days,
        max_rows=new_policy.max_rows,
        batch_size=new_policy.batch_size,
        lock_timeout_seconds=new_policy.lock_timeout_seconds,
        current_row_count=row_count,
    )


@router.post("/purge", status_code=http_status.HTTP_200_OK)
async def purge_evidence_retention(
    session: Annotated[AsyncSession, Depends(get_db_session)],
    organisation_id: Annotated[Any | None, Query()] = None,
    principal: TenantPrincipal = require_system_or_org_admin(_PERMISSION),
) -> PurgeResponse:
    """Trigger a manual evidence purge for the caller's org (or a target org for system admins).

    Returns the count of deleted rows.
    """
    org_id = _resolve_org_id(principal, organisation_id)

    try:
        async with session.begin():
            await set_rls_org(session, org_id)
            result: PurgeResult = await purge_evidence(session, org_id)
    except ProgrammingError:
        _log.exception("evidence_retention.purge.programming_error")
        raise HTTPException(
            status_code=http_status.HTTP_501_NOT_IMPLEMENTED,
            detail="Feature is not available. Run database migrations to enable it.",
        ) from None
    except SQLAlchemyError:
        _log.exception("evidence_retention.purge.db_error")
        raise HTTPException(
            status_code=http_status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Database temporarily unavailable.",
        ) from None
    except HTTPException:
        raise
    except Exception:
        _log.exception("evidence_retention.purge.unexpected_error")
        raise HTTPException(
            status_code=http_status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=MSG_UNEXPECTED_ERROR,
        ) from None

    _log.info(
        "evidence.retention.purged",
        extra={
            "org_id": str(org_id),
            "rows_deleted": result.rows_deleted,
            "batches": result.batches,
        },
    )
    return PurgeResponse(
        rows_deleted=result.rows_deleted,
        batches=result.batches,
        max_age_days=result.max_age_days,
        max_rows=result.max_rows,
    )
