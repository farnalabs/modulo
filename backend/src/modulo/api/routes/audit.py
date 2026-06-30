"""Audit chain verification, browsing, and export API."""

from __future__ import annotations

from uuid import UUID as _UUID

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

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


@router.get("", response_model=dict[str, object])
async def list_audit_events_endpoint(
    cursor: str | None = Query(None, max_length=64, description="Cursor for pagination (event ID)"),
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
    actor_uid = _UUID(actor_user_id) if actor_user_id else None
    _require_admin(principal)
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
    return result


@router.post("/batch-detail", response_model=list[dict[str, object]], dependencies=[require_feature("audit_viewer")])
async def batch_detail_endpoint(
    body: BatchDetailRequest,
    session: AsyncSession = Depends(get_db_session),
    principal: AuthenticatedPrincipal = Depends(get_current_user),
) -> list[dict[str, object]]:
    """Return full details for a batch of audit event IDs."""
    async with session.begin():
        _require_admin(principal)
        await set_rls_org(session, principal.organisation_id)
        result = await get_audit_events_batch(
            session,
            principal.organisation_id,
            body.event_ids,
        )
    return result


@router.get("/verify", response_model=dict[str, object])
async def verify_chain_endpoint(
    session: AsyncSession = Depends(get_db_session),
    principal: AuthenticatedPrincipal = Depends(get_current_user),
) -> dict[str, object]:
    """Verify the cryptographic integrity of the org's audit chain."""
    async with session.begin():
        _require_admin(principal)
        await set_rls_org(session, principal.organisation_id)
        result = await verify_chain(session, principal.organisation_id)
    return result


@router.get("/export", response_model=dict[str, object], dependencies=[require_feature("audit_viewer")])
async def export_chain_endpoint(
    page: int = Query(1, ge=1),
    page_size: int = Query(100, ge=1, le=1000),
    session: AsyncSession = Depends(get_db_session),
    principal: AuthenticatedPrincipal = Depends(get_current_user),
) -> dict[str, object]:
    """Export audit events as paginated JSON."""
    async with session.begin():
        _require_admin(principal)
        await set_rls_org(session, principal.organisation_id)
        result = await export_chain(session, principal.organisation_id, page=page, page_size=page_size)
    return result
