"""In-app notification CRUD routes.

Endpoints for the in-app notification system — dashboard panel, full notification
page, dismiss flow, and user preferences.
"""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from modulo.api.dependencies import get_db_session
from modulo.auth.dependencies import get_current_user
from modulo.auth.jwt import AuthenticatedPrincipal
from modulo.db.crud.notifications import (
    count_notifications_for_user,
    create_notification,
    dismiss_notification,
    get_dashboard_notifications,
    get_notification,
    get_notifications_for_user,
    get_unread_count,
    review_later,
)
from modulo.db.rls import set_rls_org, set_rls_user_context
from modulo.core.events.event_bus import get_event_bus

router = APIRouter(prefix="/api/v1/notifications/in-app", tags=["notifications"])


class NotificationResponse(BaseModel):
    id: uuid.UUID
    scope: str
    level: str
    category: str
    title: str
    body: str
    action_url: str | None = None
    dismiss_strategy: str = "user_only"
    dismissible_at_scope: bool = False
    created_at: str
    scope_label: str = ""


class DashboardNotificationResponse(BaseModel):
    notifications: list[NotificationResponse]
    total_unread: int


class NotificationPreferencesResponse(BaseModel):
    dashboard_level: str = "warning"
    notification_opt_outs: dict[str, bool] = Field(default_factory=dict)


class NotificationPreferencesUpdate(BaseModel):
    dashboard_level: str | None = None
    notification_opt_outs: dict[str, bool] | None = None


class DismissRequest(BaseModel):
    dismiss_scope: str = Field(default="self", pattern=r"^(self|scope)$")


class PaginatedNotificationsResponse(BaseModel):
    items: list[NotificationResponse]
    total: int
    page: int
    page_size: int


def _notification_to_response(n: object) -> NotificationResponse:
    scope_labels = {"user": "Personal", "org": "Org-wide", "admin": "Admin"}
    return NotificationResponse(
        id=n.id,
        scope=n.scope,
        level=n.level,
        category=n.category,
        title=n.title,
        body=n.body,
        action_url=n.action_url,
        dismiss_strategy=n.dismiss_strategy,
        dismissible_at_scope=n.dismissible_at_scope,
        created_at=n.created_at.isoformat() if n.created_at else "",
        scope_label=scope_labels.get(n.scope, n.scope),
    )


@router.get("/dashboard", response_model=DashboardNotificationResponse)
async def get_dashboard(
    session: AsyncSession = Depends(get_db_session),
    principal: AuthenticatedPrincipal = Depends(get_current_user),
) -> DashboardNotificationResponse:
    async with session.begin():
        await set_rls_org(session, principal.organisation_id)
        await set_rls_user_context(session, principal.account_id, principal.org_role)
        notifications = await get_dashboard_notifications(
            session=session,
            org_id=principal.organisation_id,
            user_id=principal.account_id,
            limit=5,
        )
        unread = await get_unread_count(
            session=session,
            org_id=principal.organisation_id,
            user_id=principal.account_id,
        )
    return DashboardNotificationResponse(
        notifications=[_notification_to_response(n) for n in notifications],
        total_unread=unread,
    )


@router.get("/unread-count", response_model=dict)
async def get_unread(
    session: AsyncSession = Depends(get_db_session),
    principal: AuthenticatedPrincipal = Depends(get_current_user),
) -> dict:
    async with session.begin():
        await set_rls_org(session, principal.organisation_id)
        await set_rls_user_context(session, principal.account_id, principal.org_role)
        count = await get_unread_count(
            session=session,
            org_id=principal.organisation_id,
            user_id=principal.account_id,
        )
    return {"count": count}


@router.get("", response_model=PaginatedNotificationsResponse)
async def list_notifications(
    session: AsyncSession = Depends(get_db_session),
    principal: AuthenticatedPrincipal = Depends(get_current_user),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    level: str | None = Query(None),
    scope: str | None = Query(None),
    category: str | None = Query(None),
    status: str | None = Query(None),
) -> PaginatedNotificationsResponse:
    async with session.begin():
        await set_rls_org(session, principal.organisation_id)
        await set_rls_user_context(session, principal.account_id, principal.org_role)
        offset = (page - 1) * page_size
        notifications = await get_notifications_for_user(
            session=session,
            org_id=principal.organisation_id,
            user_id=principal.account_id,
            level=level,
            scope=scope,
            category=category,
            status_filter=status,
            limit=page_size,
            offset=offset,
        )
        total = await count_notifications_for_user(
            session=session,
            org_id=principal.organisation_id,
            user_id=principal.account_id,
            level=level,
            scope=scope,
            category=category,
            status_filter=status,
        )
    return PaginatedNotificationsResponse(
        items=[_notification_to_response(n) for n in notifications],
        total=total,
        page=page,
        page_size=page_size,
    )


@router.get("/{notification_id}", response_model=NotificationResponse)
async def get_notification_detail(
    notification_id: uuid.UUID,
    session: AsyncSession = Depends(get_db_session),
    principal: AuthenticatedPrincipal = Depends(get_current_user),
) -> NotificationResponse:
    async with session.begin():
        await set_rls_org(session, principal.organisation_id)
        await set_rls_user_context(session, principal.account_id, principal.org_role)
        n = await get_notification(
            session=session,
            org_id=principal.organisation_id,
            notification_id=notification_id,
        )
        if n is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Notification not found")
    return _notification_to_response(n)


@router.post("/{notification_id}/review-later", status_code=status.HTTP_200_OK)
async def review_later_endpoint(
    notification_id: uuid.UUID,
    session: AsyncSession = Depends(get_db_session),
    principal: AuthenticatedPrincipal = Depends(get_current_user),
) -> dict:
    async with session.begin():
        await set_rls_org(session, principal.organisation_id)
        await set_rls_user_context(session, principal.account_id, principal.org_role)
        try:
            await review_later(
                session=session,
                notification_id=notification_id,
                user_id=principal.account_id,
            )
        except ValueError as exc:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
    return {"status": "dismissed_for_self"}


@router.post("/{notification_id}/dismiss", status_code=status.HTTP_200_OK)
async def dismiss_endpoint(
    notification_id: uuid.UUID,
    body: DismissRequest,
    session: AsyncSession = Depends(get_db_session),
    principal: AuthenticatedPrincipal = Depends(get_current_user),
) -> dict:
    async with session.begin():
        await set_rls_org(session, principal.organisation_id)
        await set_rls_user_context(session, principal.account_id, principal.org_role)
        try:
            await dismiss_notification(
                session=session,
                notification_id=notification_id,
                user_id=principal.account_id,
                dismiss_scope=body.dismiss_scope,
                is_admin=principal.org_role == "admin",
            )
        except ValueError as exc:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc

    event_bus = get_event_bus()
    await event_bus.publish(
        org_id=str(principal.organisation_id),
        resource_type="notification",
        resource_id=str(notification_id),
        action="dismissed",
        version=1,
    )

    scope_label = "for_everyone" if body.dismiss_scope == "scope" else "for_self"
    return {"status": f"dismissed_{scope_label}"}


@router.get("/preferences", response_model=NotificationPreferencesResponse)
async def get_preferences(
    session: AsyncSession = Depends(get_db_session),
    principal: AuthenticatedPrincipal = Depends(get_current_user),
) -> NotificationPreferencesResponse:
    async with session.begin():
        await set_rls_org(session, principal.organisation_id)
        await set_rls_user_context(session, principal.account_id, principal.org_role)
    return NotificationPreferencesResponse()


@router.put("/preferences", response_model=NotificationPreferencesResponse)
async def update_preferences(
    body: NotificationPreferencesUpdate,
    session: AsyncSession = Depends(get_db_session),
    principal: AuthenticatedPrincipal = Depends(get_current_user),
) -> NotificationPreferencesResponse:
    async with session.begin():
        await set_rls_org(session, principal.organisation_id)
        await set_rls_user_context(session, principal.account_id, principal.org_role)
    return NotificationPreferencesResponse()
