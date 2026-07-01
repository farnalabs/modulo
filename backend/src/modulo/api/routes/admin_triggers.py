"""Admin-only trigger event log endpoints."""

import uuid
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from modulo.api.dependencies import get_db_session
from modulo.auth.dependencies import get_current_user
from modulo.auth.jwt import AuthenticatedPrincipal
from modulo.db.models.trigger_event import TriggerEvent
from modulo.db.rls import set_rls_org

router = APIRouter(prefix="/api/v1/admin/trigger-events", tags=["admin-trigger-events"])


def _require_admin(principal: AuthenticatedPrincipal) -> None:
    if principal.org_role != "admin":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Admin role required",
        )


class TriggerEventItem(BaseModel):
    id: str
    trigger_id: str
    trigger_type: str
    validation_result: str
    received_at: str | None = None
    created_at: str | None = None
    run_id: str | None = None
    error_detail: str | None = None


class TriggerEventListResponse(BaseModel):
    items: list[TriggerEventItem]
    next_cursor: str | None = None
    prev_cursor: str | None = None
    total: int


@router.get("", response_model=TriggerEventListResponse)
async def list_trigger_events(
    trigger_type: str | None = Query(None),
    validation_result: str | None = Query(None),
    cursor: str | None = Query(None, description="Cursor: createdAt_id"),
    limit: int = Query(25, ge=1, le=100),
    session: AsyncSession = Depends(get_db_session),
    principal: AuthenticatedPrincipal = Depends(get_current_user),
) -> TriggerEventListResponse:
    _require_admin(principal)
    async with session.begin():
        await set_rls_org(session, principal.organisation_id)

        q = select(TriggerEvent).where(
            TriggerEvent.organisation_id == principal.organisation_id,
        )
        if trigger_type:
            q = q.where(TriggerEvent.trigger_type == trigger_type)
        if validation_result:
            q = q.where(TriggerEvent.validation_result == validation_result)

        if cursor:
            try:
                cursor_ts_str, cursor_id = cursor.split("_", 1)
                cursor_dt = datetime.fromisoformat(cursor_ts_str)
                cursor_uuid = uuid.UUID(cursor_id)
                q = q.where(
                    (TriggerEvent.created_at < cursor_dt)
                    | ((TriggerEvent.created_at == cursor_dt) & (TriggerEvent.id < cursor_uuid))
                )
            except (ValueError, AttributeError):
                pass

        q = q.order_by(TriggerEvent.created_at.desc(), TriggerEvent.id.desc()).limit(limit + 1)
        rows = (await session.execute(q)).scalars().all()

    has_more = len(rows) > limit
    if has_more:
        rows = rows[:limit]

    items = [
        TriggerEventItem(
            id=str(e.id),
            trigger_id=str(e.trigger_id),
            trigger_type=e.trigger_type,
            validation_result=e.validation_result,
            received_at=e.received_at.isoformat() if e.received_at else None,
            created_at=e.created_at.isoformat() if e.created_at else None,
            run_id=str(e.run_id) if e.run_id else None,
            error_detail=e.error_detail,
        )
        for e in rows
    ]

    next_cursor: str | None = None
    prev_cursor: str | None = None
    if has_more and rows:
        last = rows[-1]
        next_cursor = f"{last.created_at.isoformat()}_{last.id}"
    if rows:
        first = rows[0]
        prev_cursor = f"{first.created_at.isoformat()}_{first.id}"

    count_result = await session.execute(
        select(func.count(TriggerEvent.id)).where(
            TriggerEvent.organisation_id == principal.organisation_id,
        )
    )
    total = count_result.scalar() or 0

    return TriggerEventListResponse(
        items=items,
        next_cursor=next_cursor,
        prev_cursor=prev_cursor,
        total=total,
    )
