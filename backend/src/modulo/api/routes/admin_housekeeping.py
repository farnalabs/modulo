"""Admin housekeeping routes — list and delete cleanup candidates."""

import logging
import uuid
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel
from sqlalchemy.exc import IntegrityError, ProgrammingError, SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from modulo.api.constants import (
    MSG_DATABASE_TEMPORARILY_UNAVAILABLE,
    MSG_FEATURE_NOT_AVAILABLE,
    MSG_INTERNAL_SERVER_ERROR,
)
from modulo.api.db_error_handling import raise_session_contract_error
from modulo.api.dependencies import get_db_session, require_permission, require_system_permission
from modulo.auth.dependencies import get_current_tenant_user
from modulo.auth.jwt import TenantPrincipal
from modulo.core.audit_coverage import audited
from modulo.core.housekeeping import (
    ENTITY_MODEL_MAP,
    NON_DELETABLE_ENTITY_TYPES,
    delete_one_in_savepoint,
    scan_all,
)
from modulo.db.crud.policy_gate_decision import is_policy_gate_decision_fk_error
from modulo.db.crud.run_retention import CHECKPOINT_RETENTION_DAYS, purge_terminal_checkpoints
from modulo.db.rls import set_rls_execution_context, set_rls_org

_CODE_ADMIN_HOUSEKEEPING_PERFORM_CLEANUP = "admin_housekeeping.perform_cleanup"

_log = logging.getLogger(__name__)

# Permission guarding every housekeeping route (same check, three routes).
_PERM_HOUSEKEEPING_MANAGE = "housekeeping.manage"

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
    # FAR-1977: ``status`` distinguishes a scanner that RAN (``"ok"`` — an empty
    # candidate list is a clean scan) from one that RAISED (``"failed"``). A
    # failed category carries a structured ``error`` naming the exception CLASS
    # only — never its message or a traceback. Mirrors the MCP ``list_housekeeping``
    # shape so the two surfaces cannot drift.
    status: str
    error: dict[str, str] | None = None


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
    principal: TenantPrincipal = require_permission(_PERM_HOUSEKEEPING_MANAGE),
    _sys: TenantPrincipal = require_system_permission(_PERM_HOUSEKEEPING_MANAGE),  # type: ignore[assignment]
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
            detail=MSG_FEATURE_NOT_AVAILABLE,
        ) from None
    except SQLAlchemyError as exc:
        raise_session_contract_error(exc, "admin_housekeeping.list_housekeeping")
        _log.exception("admin_housekeeping.list")
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=MSG_DATABASE_TEMPORARILY_UNAVAILABLE,
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
            status=r.status,
            error=r.error,
        )
        for r in results
    ]
    total = sum(len(r.candidates) for r in results)
    return HousekeepingScanResponse(categories=categories_list, total_count=total)


def _group_cleanup_items(items: list[CleanupItem]) -> dict[str, list[str]]:
    """Group requested items by entity type, preserving request order."""
    grouped: dict[str, list[str]] = {}
    for item in items:
        grouped.setdefault(item.entity_type, []).append(item.id)
    return grouped


def _integrity_error_entry(exc: IntegrityError, entity_type: str, eid: str) -> dict[str, str]:
    """Translate a per-item ``IntegrityError`` into its structured error entry."""
    _log.exception(_CODE_ADMIN_HOUSEKEEPING_PERFORM_CLEANUP)
    _log.warning("IntegrityError cleaning up %s %s", entity_type, eid)
    if is_policy_gate_decision_fk_error(exc):
        return {
            "id": eid,
            "entity_type": entity_type,
            "error": (
                f"Cannot delete {entity_type} {eid}: governance decision records present"
                " — archive or purge policy_gate_decisions first"
            ),
            "blocked_by": "policy_gate_decisions",
        }
    return {"id": eid, "entity_type": entity_type, "error": "Foreign key constraint violation"}


async def _delete_cleanup_item(
    session: AsyncSession,
    model_cls: type[Any],
    eid: str,
    entity_type: str,
    organisation_id: uuid.UUID,
) -> tuple[int, dict[str, str] | None]:
    """Delete one item in its own savepoint; return ``(deleted, error_entry)``.

    FAR-1857: count only AFTER the savepoint released. ``delete_one_in_savepoint``
    returns True only once the DELETE (flushed on savepoint exit) has committed,
    so an FK RESTRICT refusal cannot be counted as a deletion. Shared with the
    MCP path so the two cannot drift.
    """
    try:
        if await delete_one_in_savepoint(session, model_cls, eid, organisation_id):
            return 1, None
        return 0, None
    except IntegrityError as exc:
        return 0, _integrity_error_entry(exc, entity_type, eid)


async def _cleanup_entity_type(
    session: AsyncSession,
    entity_type: str,
    ids: list[str],
    organisation_id: uuid.UUID,
) -> tuple[int, list[dict[str, str]]]:
    """Delete every id in one entity-type group, collecting structured errors."""
    if entity_type in NON_DELETABLE_ENTITY_TYPES:
        return 0, [{"entity_type": entity_type, "error": "Surfaced for triage only — not auto-deleted."}]
    model_cls = ENTITY_MODEL_MAP.get(entity_type)
    if model_cls is None:
        return 0, [{"entity_type": entity_type, "error": f"Unknown entity type: {entity_type}"}]
    deleted_count = 0
    errors: list[dict[str, str]] = []
    for eid in ids:
        deleted, error = await _delete_cleanup_item(session, model_cls, eid, entity_type, organisation_id)
        deleted_count += deleted
        if error is not None:
            errors.append(error)
    return deleted_count, errors


@router.post(
    "/cleanup",
    dependencies=[
        Depends(
            audited(
                "housekeeping_cleanup_performed",
                "housekeeping",
                principal_dep=get_current_tenant_user,
                fail_closed=True,
            )
        )
    ],
)
async def perform_cleanup(
    req: CleanupRequest,
    session: Annotated[AsyncSession, Depends(get_db_session)],
    principal: TenantPrincipal = require_permission(_PERM_HOUSEKEEPING_MANAGE),
    _sys: TenantPrincipal = require_system_permission(_PERM_HOUSEKEEPING_MANAGE),  # type: ignore[assignment]
) -> CleanupResponse:
    deleted_count = 0
    errors: list[dict[str, str]] = []
    grouped = _group_cleanup_items(req.items)

    try:
        async with session.begin():
            await set_rls_org(session, principal.organisation_id)
            await set_rls_execution_context(session)

            for entity_type, ids in grouped.items():
                deleted, group_errors = await _cleanup_entity_type(session, entity_type, ids, principal.organisation_id)
                deleted_count += deleted
                errors.extend(group_errors)
    except ProgrammingError:
        _log.exception(_CODE_ADMIN_HOUSEKEEPING_PERFORM_CLEANUP)
        raise HTTPException(
            status_code=status.HTTP_501_NOT_IMPLEMENTED,
            detail=MSG_FEATURE_NOT_AVAILABLE,
        ) from None
    except SQLAlchemyError as exc:
        raise_session_contract_error(exc, _CODE_ADMIN_HOUSEKEEPING_PERFORM_CLEANUP)
        _log.exception("admin_housekeeping.cleanup")
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=MSG_DATABASE_TEMPORARILY_UNAVAILABLE,
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


@router.post(
    "/checkpoints/purge",
    dependencies=[
        Depends(
            audited(
                "checkpoint_purged",
                "checkpoint",
                principal_dep=get_current_tenant_user,
                fail_closed=True,
            )
        )
    ],
)
async def purge_checkpoints(
    req: CheckpointRetentionPurgeRequest,
    session: Annotated[AsyncSession, Depends(get_db_session)],
    principal: TenantPrincipal = require_permission(_PERM_HOUSEKEEPING_MANAGE),
    _sys: TenantPrincipal = require_system_permission(_PERM_HOUSEKEEPING_MANAGE),  # type: ignore[assignment]
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
            detail=MSG_FEATURE_NOT_AVAILABLE,
        ) from None
    except SQLAlchemyError as exc:
        raise_session_contract_error(exc, "admin_housekeeping.purge_checkpoints")
        _log.exception("admin_housekeeping.purge_checkpoints.db_error")
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=MSG_DATABASE_TEMPORARILY_UNAVAILABLE,
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
