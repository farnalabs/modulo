"""Notification endpoint CRUD routes.

Endpoints are org-scoped and managed via standard REST operations.
Secrets are Fernet-encrypted at rest and never exposed in responses.
"""

from __future__ import annotations

import json
import uuid

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from modulo.api.dependencies import get_db_session
from modulo.auth.dependencies import get_current_user
from modulo.auth.jwt import AuthenticatedPrincipal
from modulo.db.models.notification_endpoint import NotificationEndpoint
from modulo.db.rls import set_rls_org
from modulo.settings import Settings, get_settings

router = APIRouter(prefix="/api/v1/notifications", tags=["notifications"])


class NotificationEndpointCreate(BaseModel):
    url: str = Field(..., max_length=2048)
    secret: str | None = Field(None)
    events: list[str] = Field(default_factory=list)
    description: str | None = Field(None, max_length=500)

    @field_validator("url")
    @classmethod
    def _url_must_be_http(cls, v: str) -> str:
        if not v.startswith(("http://", "https://")):
            raise ValueError("url must start with http:// or https://")
        return v


class NotificationEndpointUpdate(BaseModel):
    url: str | None = Field(None, max_length=2048)
    secret: str | None = None
    events: list[str] | None = None
    description: str | None = Field(None, max_length=500)


class NotificationEndpointResponse(BaseModel):
    id: uuid.UUID
    url: str
    events: list[str]
    description: str | None
    auto_disabled: bool
    consecutive_dead_letter_count: int


@router.get("", response_model=list[NotificationEndpointResponse])
async def list_endpoints(
    session: AsyncSession = Depends(get_db_session),
    principal: AuthenticatedPrincipal = Depends(get_current_user),
) -> list[NotificationEndpointResponse]:
    async with session.begin():
        await set_rls_org(session, principal.organisation_id)
        result = await session.execute(
            select(NotificationEndpoint).where(
                NotificationEndpoint.organisation_id == principal.organisation_id
            )
        )
        endpoints = list(result.scalars())
    return [_ep_to_response(ep) for ep in endpoints]


@router.post("", response_model=NotificationEndpointResponse, status_code=status.HTTP_201_CREATED)
async def create_endpoint(
    body: NotificationEndpointCreate,
    session: AsyncSession = Depends(get_db_session),
    principal: AuthenticatedPrincipal = Depends(get_current_user),
    settings: Settings = Depends(get_settings),
) -> NotificationEndpointResponse:
    from cryptography.fernet import Fernet

    fernet = Fernet(settings.fernet_key.encode())
    secret_ciphertext: bytes | None = None
    if body.secret:
        secret_ciphertext = fernet.encrypt(body.secret.encode())

    async with session.begin():
        await set_rls_org(session, principal.organisation_id)
        ep = NotificationEndpoint(
            id=uuid.uuid4(),
            organisation_id=principal.organisation_id,
            url=body.url,
            secret_ciphertext=secret_ciphertext,
            events=json.dumps(body.events),
            description=body.description,
            created_by=principal.user_id,
        )
        session.add(ep)
        await session.flush()

    return _ep_to_response(ep)


@router.get("/{endpoint_id}", response_model=NotificationEndpointResponse)
async def get_endpoint(
    endpoint_id: uuid.UUID,
    session: AsyncSession = Depends(get_db_session),
    principal: AuthenticatedPrincipal = Depends(get_current_user),
) -> NotificationEndpointResponse:
    async with session.begin():
        await set_rls_org(session, principal.organisation_id)
        ep = await session.get(NotificationEndpoint, endpoint_id)
        if ep is None or ep.organisation_id != principal.organisation_id:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Endpoint not found")
    return _ep_to_response(ep)


@router.put("/{endpoint_id}", response_model=NotificationEndpointResponse)
async def update_endpoint(
    endpoint_id: uuid.UUID,
    body: NotificationEndpointUpdate,
    session: AsyncSession = Depends(get_db_session),
    principal: AuthenticatedPrincipal = Depends(get_current_user),
    settings: Settings = Depends(get_settings),
) -> NotificationEndpointResponse:
    from cryptography.fernet import Fernet

    async with session.begin():
        await set_rls_org(session, principal.organisation_id)
        ep = await session.get(NotificationEndpoint, endpoint_id)
        if ep is None or ep.organisation_id != principal.organisation_id:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Endpoint not found")

        if body.url is not None:
            ep.url = body.url
        if body.secret is not None:
            fernet = Fernet(settings.fernet_key.encode())
            ep.secret_ciphertext = fernet.encrypt(body.secret.encode())
        if body.events is not None:
            ep.events = json.dumps(body.events)
        if body.description is not None:
            ep.description = body.description

        await session.flush()

    return _ep_to_response(ep)


@router.delete("/{endpoint_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_endpoint(
    endpoint_id: uuid.UUID,
    session: AsyncSession = Depends(get_db_session),
    principal: AuthenticatedPrincipal = Depends(get_current_user),
) -> None:
    async with session.begin():
        await set_rls_org(session, principal.organisation_id)
        ep = await session.get(NotificationEndpoint, endpoint_id)
        if ep is None or ep.organisation_id != principal.organisation_id:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Endpoint not found")
        await session.delete(ep)


def _ep_to_response(ep: NotificationEndpoint) -> NotificationEndpointResponse:
    events: list[str] = []
    try:
        events = json.loads(ep.events) if ep.events else []
    except (json.JSONDecodeError, TypeError):
        pass
    return NotificationEndpointResponse(
        id=ep.id,
        url=ep.url,
        events=events,
        description=ep.description,
        auto_disabled=bool(ep.auto_disabled) if ep.auto_disabled is not None else False,
        consecutive_dead_letter_count=ep.consecutive_dead_letter_count or 0,
    )
