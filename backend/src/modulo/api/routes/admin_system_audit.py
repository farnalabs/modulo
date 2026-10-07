"""System-admin read surface for the durable ``system_audit_events`` ledger (FAR-1538).

``system_audit_events`` (FAR-1517) is the org-independent, append-only record of
organisation-lifecycle events that survives a hard organisation delete. Until
now the only way to read it was SQL. This module exposes a read-only,
system-admin-gated, paginated list of those records.

Deliberate differences from ``routes/audit.py`` (the org-scoped trail):

* **No RLS org context.** The table has no ``organisation_id`` column by design
  - scoping the query to the caller's org would defeat the point of a ledger
  that outlives its org, and there is no RLS policy on the table to honour.
* **System-admin gate, not an org-role gate.** These records span every
  organisation, so ``require_system_permission`` (pure ``is_system_admin``) is
  the only gate that makes sense; an org admin must not read another org's
  deletion history.
* **No ``audited()`` dependency.** Reads are not audited (FAR-1472 covers
  mutating routes), so the coverage baseline is untouched.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime
from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel
from sqlalchemy.exc import ProgrammingError, SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from modulo.api.constants import MSG_INTERNAL_SERVER_ERROR
from modulo.api.db_error_handling import handle_db_errors, raise_session_contract_error
from modulo.api.dependencies import get_db_session, require_system_permission
from modulo.auth.jwt import AuthenticatedPrincipal
from modulo.db.crud.system_audit_event import (
    LIST_DEFAULT_PAGE_SIZE,
    LIST_MAX_PAGE_SIZE,
    list_system_audit_events,
)

#: The audit-log permission key resolves at import time (fail-fast on a typo);
#: the gate itself is purely the principal's ``is_system_admin`` flag.
_CODE_AUDIT_MANAGE = "audit.manage"
_CODE_ROUTES_ADMIN_SYSTEM_AUDIT = "routes.admin_system_audit"
_MSG_DATABASE_CONNECTION_FAILED_PLEASE = "Database connection failed. Please try again."
_MSG_DATABASE_NOT_AVAILABLE_RUN = "Database not available. Run migrations."

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/admin/system-audit", tags=["admin-system-audit"])


class SystemAuditEventItem(BaseModel):
    """One durable org-lifecycle record as it appears on the wire."""

    id: str
    event_type: str
    #: The organisation the event is ABOUT - a plain value, never an FK.
    org_id: str | None = None
    actor_user_id: str | None = None
    resource_type: str | None = None
    resource_id: str | None = None
    payload_json: dict[str, Any]
    request_id: str | None = None
    created_at: str | None = None


class SystemAuditEventPage(BaseModel):
    """Repo-standard offset page envelope (items / total / page / page_size)."""

    items: list[SystemAuditEventItem]
    total: int
    page: int
    page_size: int


def _to_item(event: Any) -> SystemAuditEventItem:
    return SystemAuditEventItem(
        id=str(event.id),
        event_type=event.event_type,
        org_id=str(event.org_id) if event.org_id else None,
        actor_user_id=str(event.actor_user_id) if event.actor_user_id else None,
        resource_type=event.resource_type,
        resource_id=str(event.resource_id) if event.resource_id else None,
        payload_json=event.payload_json or {},
        request_id=event.request_id,
        created_at=event.created_at.isoformat() if event.created_at else None,
    )


@router.get(
    "",
    responses={
        500: {"description": "Internal Server Error"},
        501: {"description": "Not Implemented"},
        503: {"description": "Service Unavailable"},
    },
)
@handle_db_errors("admin.system_audit.admin_list_system_audit_events")
async def admin_list_system_audit_events(
    _current_user: Annotated[AuthenticatedPrincipal, require_system_permission(_CODE_AUDIT_MANAGE)],
    session: AsyncSession = Depends(get_db_session),
    page: int = Query(1, ge=1, description="1-based page number"),
    page_size: int = Query(
        LIST_DEFAULT_PAGE_SIZE,
        ge=1,
        le=LIST_MAX_PAGE_SIZE,
        description="Number of records per page",
    ),
    event_type: str | None = Query(None, max_length=100, description="Filter by exact event type"),
    org_id: UUID | None = Query(None, description="Filter by the organisation the event is about"),
    from_date: datetime | None = Query(None, description="Inclusive lower bound on created_at (ISO 8601)"),
    to_date: datetime | None = Query(None, description="Inclusive upper bound on created_at (ISO 8601)"),
) -> SystemAuditEventPage:
    """List durable, org-independent org-lifecycle audit records.

    System-admin only: these records span every organisation, including ones
    that no longer exist, so an org-role gate would leak cross-org history.
    """
    try:
        async with session.begin():
            events, total = await list_system_audit_events(
                session,
                page=page,
                page_size=page_size,
                event_type=event_type,
                org_id=org_id,
                from_date=from_date,
                to_date=to_date,
            )
        return SystemAuditEventPage(
            items=[_to_item(event) for event in events],
            total=total,
            page=max(1, page),
            page_size=page_size,
        )
    except HTTPException:
        raise
    except asyncio.CancelledError:
        raise
    except ProgrammingError:
        logger.exception("admin_system_audit.admin_list_system_audit_events")
        raise HTTPException(
            status_code=status.HTTP_501_NOT_IMPLEMENTED,
            detail=_MSG_DATABASE_NOT_AVAILABLE_RUN,
        ) from None
    except SQLAlchemyError as exc:
        raise_session_contract_error(exc, "admin_system_audit.admin_list_system_audit_events")
        logger.exception(_CODE_ROUTES_ADMIN_SYSTEM_AUDIT)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=_MSG_DATABASE_CONNECTION_FAILED_PLEASE,
        ) from None
    except Exception:
        logger.exception("Unexpected error in admin_list_system_audit_events")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=MSG_INTERNAL_SERVER_ERROR,
        ) from None
