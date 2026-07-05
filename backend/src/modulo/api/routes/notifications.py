"""Notification endpoint CRUD routes.

Endpoints are org-scoped and managed via standard REST operations.
Secrets are Fernet-encrypted at rest and never exposed in responses.
"""

from __future__ import annotations

import contextlib
import json
import logging
import uuid

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import select
from sqlalchemy.exc import ProgrammingError
from sqlalchemy.ext.asyncio import AsyncSession

from modulo.api.dependencies import get_db_session
from modulo.auth.dependencies import get_current_user
from modulo.auth.jwt import AuthenticatedPrincipal
from modulo.db.models.notification_endpoint import NotificationEndpoint
from modulo.db.rls import set_rls_org, set_rls_user_context
from modulo.settings import Settings, get_settings

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/notifications", tags=["notifications"])


class NotificationEndpointCreate(BaseModel):
    url: str = Field(..., max_length=2048)
    secret: str | None = Field(None)
    events: list[str] = Field(default_factory=list)
    description: str | None = Field(None, max_length=500)
    team_id: str | None = None

    @field_validator("url")
    @classmethod
    def _url_must_be_http(cls, v: str) -> str:
        if not v.startswith(("http://", "https://")):
            raise ValueError("url must start with http:// or https://")
        return v

    @field_validator("team_id")
    @classmethod
    def _team_id_must_be_uuid(cls, v: str | None) -> str | None:
        if v is not None:
            try:
                uuid.UUID(v)
            except ValueError as exc:
                raise ValueError("team_id must be a valid UUID") from exc
        return v


class NotificationEndpointUpdate(BaseModel):
    url: str | None = Field(None, max_length=2048)
    secret: str | None = None
    events: list[str] | None = None
    description: str | None = Field(None, max_length=500)
    team_id: str | None = None


class NotificationEndpointResponse(BaseModel):
    id: uuid.UUID
    url: str
    events: list[str]
    description: str | None
    auto_disabled: bool
    consecutive_dead_letter_count: int
    team_id: str | None = None


@router.get("", response_model=list[NotificationEndpointResponse])
async def list_endpoints(
    session: AsyncSession = Depends(get_db_session),
    principal: AuthenticatedPrincipal = Depends(get_current_user),
) -> list[NotificationEndpointResponse]:
    try:
        async with session.begin():
            await set_rls_org(session, principal.organisation_id)
            await set_rls_user_context(session, principal.account_id, principal.org_role)
            result = await session.execute(
                select(NotificationEndpoint).where(NotificationEndpoint.organisation_id == principal.organisation_id)
            )
            endpoints = list(result.scalars())
    except ProgrammingError:
        logger.exception("notifications.endpoint_table_missing")
        raise HTTPException(
            status_code=status.HTTP_501_NOT_IMPLEMENTED,
            detail="Notifications are not available. Run database migrations to enable this feature.",
        ) from None
    return [_ep_to_response(ep) for ep in endpoints]


@router.post("", response_model=NotificationEndpointResponse, status_code=status.HTTP_201_CREATED)
async def create_endpoint(
    req: NotificationEndpointCreate,
    session: AsyncSession = Depends(get_db_session),
    principal: AuthenticatedPrincipal = Depends(get_current_user),
    settings: Settings = Depends(get_settings),
) -> NotificationEndpointResponse:
    from cryptography.fernet import Fernet

    if req.team_id is not None and principal.org_role != "admin":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Only admins can create team-scoped notification endpoints",
        )

    fernet = Fernet(settings.fernet_key.encode())
    secret_ciphertext: bytes | None = None
    if req.secret:
        secret_ciphertext = fernet.encrypt(req.secret.encode())

    team_id: uuid.UUID | None = None
    if req.team_id is not None:
        team_id = uuid.UUID(req.team_id)

    try:
        async with session.begin():
            await set_rls_org(session, principal.organisation_id)
            await set_rls_user_context(session, principal.account_id, principal.org_role)
            ep = NotificationEndpoint(
                id=uuid.uuid4(),
                organisation_id=principal.organisation_id,
                url=req.url,
                secret_ciphertext=secret_ciphertext,
                events=json.dumps(req.events),
                description=req.description,
                account_id=principal.account_id,
                team_id=team_id,
            )
            session.add(ep)
            await session.flush()
    except ProgrammingError:
        logger.exception("notifications.endpoint_table_missing")
        raise HTTPException(
            status_code=status.HTTP_501_NOT_IMPLEMENTED,
            detail="Notifications are not available. Run database migrations to enable this feature.",
        ) from None

    return _ep_to_response(ep)


@router.get("/{endpoint_id}", response_model=NotificationEndpointResponse)
async def get_endpoint(
    endpoint_id: uuid.UUID,
    session: AsyncSession = Depends(get_db_session),
    principal: AuthenticatedPrincipal = Depends(get_current_user),
) -> NotificationEndpointResponse:
    try:
        async with session.begin():
            await set_rls_org(session, principal.organisation_id)
            await set_rls_user_context(session, principal.account_id, principal.org_role)
            ep = await session.get(NotificationEndpoint, endpoint_id)
            if ep is None or ep.organisation_id != principal.organisation_id:
                raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Endpoint not found")
    except ProgrammingError:
        logger.exception("notifications.endpoint_table_missing")
        raise HTTPException(
            status_code=status.HTTP_501_NOT_IMPLEMENTED,
            detail="Notifications are not available. Run database migrations to enable this feature.",
        ) from None
    return _ep_to_response(ep)


@router.put("/{endpoint_id}", response_model=NotificationEndpointResponse)
async def update_endpoint(
    endpoint_id: uuid.UUID,
    req: NotificationEndpointUpdate,
    session: AsyncSession = Depends(get_db_session),
    principal: AuthenticatedPrincipal = Depends(get_current_user),
    settings: Settings = Depends(get_settings),
) -> NotificationEndpointResponse:
    from cryptography.fernet import Fernet

    if req.team_id is not None and principal.org_role != "admin":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Only admins can assign team-scoped notification endpoints",
        )

    try:
        async with session.begin():
            await set_rls_org(session, principal.organisation_id)
            await set_rls_user_context(session, principal.account_id, principal.org_role)
            ep = await session.get(NotificationEndpoint, endpoint_id)
            if ep is None or ep.organisation_id != principal.organisation_id:
                raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Endpoint not found")

            if req.url is not None:
                ep.url = req.url
            if req.secret is not None:
                fernet = Fernet(settings.fernet_key.encode())
                ep.secret_ciphertext = fernet.encrypt(req.secret.encode())
            if req.events is not None:
                ep.events = json.dumps(req.events)
            if req.description is not None:
                ep.description = req.description
            if req.team_id is not None:
                ep.team_id = uuid.UUID(req.team_id)

            await session.flush()
    except ProgrammingError:
        logger.exception("notifications.endpoint_table_missing")
        raise HTTPException(
            status_code=status.HTTP_501_NOT_IMPLEMENTED,
            detail="Notifications are not available. Run database migrations to enable this feature.",
        ) from None

    return _ep_to_response(ep)


@router.delete("/{endpoint_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_endpoint(
    endpoint_id: uuid.UUID,
    session: AsyncSession = Depends(get_db_session),
    principal: AuthenticatedPrincipal = Depends(get_current_user),
) -> None:
    try:
        async with session.begin():
            await set_rls_org(session, principal.organisation_id)
            await set_rls_user_context(session, principal.account_id, principal.org_role)
            ep = await session.get(NotificationEndpoint, endpoint_id)
            if ep is None or ep.organisation_id != principal.organisation_id:
                raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Endpoint not found")
            await session.delete(ep)
    except ProgrammingError:
        logger.exception("notifications.endpoint_table_missing")
        raise HTTPException(
            status_code=status.HTTP_501_NOT_IMPLEMENTED,
            detail="Notifications are not available. Run database migrations to enable this feature.",
        ) from None


def _ep_to_response(ep: NotificationEndpoint) -> NotificationEndpointResponse:
    events: list[str] = []
    with contextlib.suppress(json.JSONDecodeError, TypeError):
        events = json.loads(ep.events) if ep.events else []
    return NotificationEndpointResponse(
        id=ep.id,
        url=ep.url,
        events=events,
        description=ep.description,
        auto_disabled=bool(ep.auto_disabled) if ep.auto_disabled is not None else False,
        consecutive_dead_letter_count=ep.consecutive_dead_letter_count or 0,
        team_id=str(ep.team_id) if ep.team_id else None,
    )
