"""Admin housekeeping routes — list and delete cleanup candidates."""

import logging
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError, ProgrammingError, SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from modulo.api.constants import MSG_INTERNAL_SERVER_ERROR
from modulo.api.dependencies import get_db_session, require_permission
from modulo.auth.jwt import TenantPrincipal
from modulo.core.housekeeping import ENTITY_MODEL_MAP, NON_DELETABLE_ENTITY_TYPES, scan_all
from modulo.db.crud.run_retention import CHECKPOINT_RETENTION_DAYS, purge_terminal_checkpoints
from modulo.db.rls import set_rls_execution_context, set_rls_org

_log = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/admin/housekeeping", tags=["admin-housekeeping"])


class CandidateItem(BaseModel):
    id: str
    name: str
    detail: str
    created_at: str | None = None
    entity_type: str = ""


class HousekeepingCategory(BaseModel):
    category: str
    label: str
    description: str
    candidates: list[CandidateItem]
    count: int


class HousekeepingScanResponse(BaseModel):
    categories: list[HousekeepingCategory]
    total_count: int


class CleanupItem(BaseModel):
    id: str
    entity_type: str


class CleanupRequest(BaseModel):
    items: list[CleanupItem]


class CleanupResponse(BaseModel):
    deleted_count: int
    errors: list[dict[str, str]]


@router.get("")
async def list_housekeeping(
    session: Annotated[AsyncSession, Depends(get_db_session)],
    principal: TenantPrincipal = require_permission("housekeeping.manage"),
) -> HousekeepingScanResponse:
    try:
        async with session.begin():
            await set_rls_org(session, principal.organisation_id)
            await set_rls_execution_context(session)
            results = await scan_all(session, principal.organisation_id)
    except ProgrammingError:
        _log.exception("admin_housekeeping.list_housekeeping")
        raise HTTPException(
            status_code=status.HTTP_501_NOT_IMPLEMENTED,
            detail="Feature is not available. Run database migrations to enable it.",
        ) from None
    except SQLAlchemyError:
        _log.exception("admin_housekeeping.list")
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Database temporarily unavailable.",
        ) from None
    except HTTPException:
        raise
    except Exception:
        _log.exception("admin_housekeeping.list")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=MSG_INTERNAL_SERVER_ERROR,
        ) from None

    categories_list = [
        HousekeepingCategory(
            category=r.category,
            label=r.label,
            description=r.description,
            candidates=[
                CandidateItem(
                    id=c.id,
                    name=c.name,
                    detail=c.detail,
                    created_at=c.created_at,
                    entity_type=c.entity_type,
                )
                for c in r.candidates
            ],
            count=len(r.candidates),
        )
        for r in results
    ]
    total = sum(len(r.candidates) for r in results)
    return HousekeepingScanResponse(categories=categories_list, total_count=total)


@router.post("/cleanup")
async def perform_cleanup(
    req: CleanupRequest,
    session: Annotated[AsyncSession, Depends(get_db_session)],
    principal: TenantPrincipal = require_permission("housekeeping.manage"),
) -> CleanupResponse:
    deleted_count = 0
    errors: list[dict[str, str]] = []

    grouped: dict[str, list[str]] = {}
    for item in req.items:
        grouped.setdefault(item.entity_type, []).append(item.id)

    try:
        async with session.begin():
            await set_rls_org(session, principal.organisation_id)
            await set_rls_execution_context(session)

            for entity_type, ids in grouped.items():
                if entity_type in NON_DELETABLE_ENTITY_TYPES:
                    errors.append(
                        {
                            "entity_type": entity_type,
                            "error": "Surfaced for triage only — not auto-deleted.",
                        }
                    )
                    continue
                model_cls = ENTITY_MODEL_MAP.get(entity_type)
                if model_cls is None:
                    errors.append({"entity_type": entity_type, "error": f"Unknown entity type: {entity_type}"})
                    continue

                for eid in ids:
                    try:
                        async with session.begin_nested():
                            stmt = select(model_cls).where(  # type: ignore[var-annotated]
                                model_cls.id == eid,  # type: ignore[attr-defined]
                                model_cls.organisation_id == principal.organisation_id,  # type: ignore[attr-defined]
                            )
                            obj = (await session.execute(stmt)).scalar_one_or_none()
                            if obj is not None:
                                await session.delete(obj)
                                deleted_count += 1
                    except IntegrityError:
                        _log.exception("admin_housekeeping.perform_cleanup")
                        _log.warning("IntegrityError cleaning up %s %s", entity_type, eid)
                        errors.append(
                            {"id": eid, "entity_type": entity_type, "error": "Foreign key constraint violation"}
                        )
    except ProgrammingError:
        _log.exception("admin_housekeeping.perform_cleanup")
        raise HTTPException(
            status_code=status.HTTP_501_NOT_IMPLEMENTED,
            detail="Feature is not available. Run database migrations to enable it.",
        ) from None
    except SQLAlchemyError:
        _log.exception("admin_housekeeping.cleanup")
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Database temporarily unavailable.",
        ) from None
    except HTTPException:
        raise
    except Exception:
        _log.exception("admin_housekeeping.cleanup")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=MSG_INTERNAL_SERVER_ERROR,
        ) from None

    return CleanupResponse(deleted_count=deleted_count, errors=errors)


class CheckpointRetentionPurgeRequest(BaseModel):
    max_age_days: int | None = None
    confirm: bool = False


class CheckpointRetentionPurgeResponse(BaseModel):
    checkpoints_purged: int
    threads_purged: int
    bytes_freed: int


@router.post("/checkpoints/purge", response_model=CheckpointRetentionPurgeResponse)
async def purge_checkpoints(
    req: CheckpointRetentionPurgeRequest,
    session: Annotated[AsyncSession, Depends(get_db_session)],
    principal: TenantPrincipal = require_permission("housekeeping.manage"),
) -> CheckpointRetentionPurgeResponse:
    """Purge LangGraph checkpoint rows for old terminal runs (keep the ``runs``).

    FAR-432: a terminal run's checkpoint rows are unread after the run finishes
    and dominate DB volume. This releases them for TERMINAL runs older than
    ``max_age_days`` (default ``CHECKPOINT_RETENTION_DAYS`` = 3) while leaving
    the ``runs`` rows intact (outputs, telemetry, classification stay for audit
    + analytics, ADR 020). Never purges a non-terminal / HITL-paused run — an
    ``awaiting_human`` run keeps its interrupt checkpoint so ``resume_run``
    continues the graph instead of re-running side-effectful nodes.

    Requires ``confirm: true``. Scoped to the caller's organisation (RLS).
    """

    if not req.confirm:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Checkpoint purge requires an explicit `confirm: true` body value.",
        )

    max_age_days = req.max_age_days or CHECKPOINT_RETENTION_DAYS
    result: CheckpointRetentionPurgeResponse
    try:
        async with session.begin():
            await set_rls_org(session, principal.organisation_id)
            await set_rls_execution_context(session)
            purge_result = await purge_terminal_checkpoints(
                session,
                org_id=principal.organisation_id,
                max_age_days=max_age_days,
            )
            result = CheckpointRetentionPurgeResponse(**purge_result)
    except ProgrammingError:
        _log.exception("admin_housekeeping.purge_checkpoints.programming_error")
        raise HTTPException(
            status_code=status.HTTP_501_NOT_IMPLEMENTED,
            detail="Feature is not available. Run database migrations to enable it.",
        ) from None
    except SQLAlchemyError:
        _log.exception("admin_housekeeping.purge_checkpoints.db_error")
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Database temporarily unavailable.",
        ) from None
    except HTTPException:
        raise
    except Exception:
        _log.exception("admin_housekeeping.purge_checkpoints.unexpected_error")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=MSG_INTERNAL_SERVER_ERROR,
        ) from None

    _log.info(
        "admin_housekeeping.checkpoints_purged",
        extra={
            "org_id": str(principal.organisation_id),
            "max_age_days": max_age_days,
            "checkpoints_purged": result.checkpoints_purged,
            "threads_purged": result.threads_purged,
            "bytes_freed": result.bytes_freed,
        },
    )
    return result
