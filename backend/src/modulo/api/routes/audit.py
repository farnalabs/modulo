"""Audit chain verification, browsing, and export API."""

from __future__ import annotations

import logging
from uuid import UUID as _UUID

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel
from sqlalchemy.exc import ProgrammingError, SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

_log = logging.getLogger(__name__)

from modulo.api.dependencies import get_db_session, require_feature
from modulo.auth.dependencies import get_current_user
from modulo.auth.jwt import AuthenticatedPrincipal
from modulo.core.audit_logger import (
    export_chain,
    get_audit_events_batch,
    list_audit_events,
    verify_chain,
)
from modulo.db.rls import set_rls_org

router = APIRouter(prefix="/api/v1/admin/audit", tags=["audit"])


def _require_admin(principal: AuthenticatedPrincipal) -> None:
    if principal.org_role != "admin":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Admin role required",
        )


class BatchDetailRequest(BaseModel):
    event_ids: list[str]


@router.get("", response_model=dict[str, object], dependencies=[require_feature("audit_viewer")])
async def list_audit_events_endpoint(
    cursor: str | None = Query(None, max_length=256, description="Cursor: JSON {c:created_at, i:id}"),
    limit: int = Query(50, ge=1, le=200, description="Number of events per page"),
    event_type: str | None = Query(None, max_length=64, description="Filter by event type (action_type)"),
    actor_user_id: str | None = Query(None, max_length=64, alias="user_id", description="Filter by actor user ID"),
    resource_type: str | None = Query(
        None,
        max_length=64,
        alias="entity_type",
        description="Filter by resource type (entity_type)",
    ),
    from_date: str | None = Query(
        None, max_length=32, alias="from_date", description="Filter by start date (ISO 8601)"
    ),
    to_date: str | None = Query(None, max_length=32, alias="to_date", description="Filter by end date (ISO 8601)"),
    session: AsyncSession = Depends(get_db_session),
    principal: AuthenticatedPrincipal = Depends(get_current_user),
) -> dict[str, object]:
    """List audit events with cursor pagination and filters.

    Supports filtering by event_type (action_type), user_id (actor_user_id),
    entity_type (resource_type), from_date, to_date.
    """
    actor_uid = None
    if actor_user_id:
        try:
            actor_uid = _UUID(actor_user_id)
        except ValueError:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail=f"Invalid user_id format: {actor_user_id!r}. Must be a valid UUID.",
            )
    _require_admin(principal)
    try:
        async with session.begin():
            await set_rls_org(session, principal.organisation_id)
            result = await list_audit_events(
                session,
                principal.organisation_id,
                cursor=cursor,
                limit=limit,
                event_type=event_type,
                actor_user_id=actor_uid,
                resource_type=resource_type,
                from_date=from_date,
                to_date=to_date,
            )
    except ProgrammingError:
        raise HTTPException(
            status_code=status.HTTP_501_NOT_IMPLEMENTED,
            detail="Feature is not available. Run database migrations to enable it.",
        )
    except SQLAlchemyError:
        _log.exception("list_audit_events: database error")
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Database connection failed. Please try again.",
        )
    return result


@router.post("/batch-detail", response_model=list[dict[str, object]], dependencies=[require_feature("audit_viewer")])
async def batch_detail_endpoint(
    body: BatchDetailRequest,
    session: AsyncSession = Depends(get_db_session),
    principal: AuthenticatedPrincipal = Depends(get_current_user),
) -> list[dict[str, object]]:
    """Return full details for a batch of audit event IDs."""
    try:
        async with session.begin():
            _require_admin(principal)
            await set_rls_org(session, principal.organisation_id)
            result = await get_audit_events_batch(
                session,
                principal.organisation_id,
                body.event_ids,
            )
    except ProgrammingError:
        raise HTTPException(
            status_code=status.HTTP_501_NOT_IMPLEMENTED,
            detail="Feature is not available. Run database migrations to enable it.",
        )
    except SQLAlchemyError:
        _log.exception("batch_detail: database error")
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Database connection failed. Please try again.",
        )
    return result


@router.get("/verify", response_model=dict[str, object])
async def verify_chain_endpoint(
    session: AsyncSession = Depends(get_db_session),
    principal: AuthenticatedPrincipal = Depends(get_current_user),
) -> dict[str, object]:
    """Verify the cryptographic integrity of the org's audit chain."""
    try:
        async with session.begin():
            _require_admin(principal)
            await set_rls_org(session, principal.organisation_id)
            result = await verify_chain(session, principal.organisation_id)
    except ProgrammingError:
        raise HTTPException(
            status_code=status.HTTP_501_NOT_IMPLEMENTED,
            detail="Feature is not available. Run database migrations to enable it.",
        )
    except SQLAlchemyError:
        _log.exception("verify_chain: database error")
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Database connection failed. Please try again.",
        )
    return result


@router.get("/export", response_model=dict[str, object], dependencies=[require_feature("audit_viewer")])
async def export_chain_endpoint(
    page: int = Query(1, ge=1),
    page_size: int = Query(100, ge=1, le=1000),
    event_type: str | None = Query(None, max_length=64, description="Filter by event type"),
    actor_user_id: str | None = Query(None, max_length=64, alias="user_id", description="Filter by actor user ID"),
    resource_type: str | None = Query(
        None, max_length=64, alias="entity_type", description="Filter by resource type"
    ),
    from_date: str | None = Query(None, max_length=32, description="Filter by start date (ISO 8601)"),
    to_date: str | None = Query(None, max_length=32, description="Filter by end date (ISO 8601)"),
    session: AsyncSession = Depends(get_db_session),
    principal: AuthenticatedPrincipal = Depends(get_current_user),
) -> dict[str, object]:
    """Export audit events as paginated JSON with optional filters."""
    actor_uid = None
    if actor_user_id:
        try:
            actor_uid = _UUID(actor_user_id)
        except ValueError:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail=f"Invalid user_id format: {actor_user_id!r}. Must be a valid UUID.",
            )
    try:
        async with session.begin():
            _require_admin(principal)
            await set_rls_org(session, principal.organisation_id)
            result = await export_chain(
                session,
                principal.organisation_id,
                page=page,
                page_size=page_size,
                event_type=event_type,
                actor_user_id=actor_uid,
                resource_type=resource_type,
                from_date=from_date,
                to_date=to_date,
            )
    except ProgrammingError:
        raise HTTPException(
            status_code=status.HTTP_501_NOT_IMPLEMENTED,
            detail="Feature is not available. Run database migrations to enable it.",
        )
    except SQLAlchemyError:
        _log.exception("export_chain: database error")
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Database connection failed. Please try again.",
        )
    return result
