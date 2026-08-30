"""Admin-only trigger event log endpoints."""

import logging

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel
from sqlalchemy import func, select
from sqlalchemy.exc import ProgrammingError, SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from modulo.api.constants import MSG_INTERNAL_SERVER_ERROR
from modulo.api.db_error_handling import handle_db_errors
from modulo.api.dependencies import get_db_session, require_permission
from modulo.auth.jwt import TenantPrincipal
from modulo.db.crud.trigger import apply_trigger_event_cursor
from modulo.db.models.trigger_event import TriggerEvent
from modulo.db.rls import set_rls_org

_CODE_ADMIN_TRIGGERS_LIST_TRIGGER = "admin_triggers.list_trigger_events"

_log = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/admin/trigger-events", tags=["admin-trigger-events"])


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


@router.get("")
@handle_db_errors("admin.triggers.list_trigger_events")
async def list_trigger_events(
    trigger_type: str | None = Query(None),
    validation_result: str | None = Query(None),
    cursor: str | None = Query(None, description="Cursor: createdAt_id"),
    limit: int = Query(25, ge=1, le=100),
    session: AsyncSession = Depends(get_db_session),
    principal: TenantPrincipal = require_permission("admin.trigger_events"),
) -> TriggerEventListResponse:
    try:
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
                q = apply_trigger_event_cursor(q, cursor)

            q = q.order_by(TriggerEvent.created_at.desc(), TriggerEvent.id.desc()).limit(limit + 1)
            rows = (await session.execute(q)).scalars().all()
    except ProgrammingError:
        _log.exception(_CODE_ADMIN_TRIGGERS_LIST_TRIGGER)
        raise HTTPException(
            status_code=status.HTTP_501_NOT_IMPLEMENTED,
            detail="Feature is not available. Run database migrations to enable it.",
        ) from None
    except SQLAlchemyError:
        _log.exception(_CODE_ADMIN_TRIGGERS_LIST_TRIGGER)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Database operation failed. Please try again later.",
        ) from None
    except HTTPException:
        raise
    except Exception:
        _log.exception("admin list_trigger_events failed")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=MSG_INTERNAL_SERVER_ERROR,
        ) from None

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

    try:
        async with session.begin():
            await set_rls_org(session, principal.organisation_id)
            count_result = await session.execute(
                select(func.count(TriggerEvent.id)).where(
                    TriggerEvent.organisation_id == principal.organisation_id,
                )
            )
            total = count_result.scalar() or 0
    except ProgrammingError:
        _log.exception(_CODE_ADMIN_TRIGGERS_LIST_TRIGGER)
        raise HTTPException(
            status_code=status.HTTP_501_NOT_IMPLEMENTED,
            detail="Feature is not available. Run database migrations to enable it.",
        ) from None
    except SQLAlchemyError:
        _log.exception(_CODE_ADMIN_TRIGGERS_LIST_TRIGGER)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Database operation failed. Please try again later.",
        ) from None
    except HTTPException:
        raise
    except Exception:
        _log.exception("admin list_trigger_events count query failed")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=MSG_INTERNAL_SERVER_ERROR,
        ) from None

    return TriggerEventListResponse(
        items=items,
        next_cursor=next_cursor,
        prev_cursor=prev_cursor,
        total=total,
    )
