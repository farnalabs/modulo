"""OAuth 2.0 client management endpoints (browser-authenticated).

POST /api/v1/mcp/oauth/clients         — Register a new OAuth client
GET  /api/v1/mcp/oauth/clients          — List OAuth clients
DELETE /api/v1/mcp/oauth/clients/{id}   — Delete an OAuth client

The protocol endpoints (POST /mcp/oauth/authorize, POST /mcp/oauth/token)
live in the MCP sub-app at ``mcp_server.py``.
"""

import logging

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from modulo.api.dependencies import get_db_session
from modulo.auth.dependencies import get_current_user
from modulo.auth.jwt import AuthenticatedPrincipal
from modulo.auth.oauth import (
    InvalidScopeError,
    create_oauth_client,
    delete_oauth_client,
    list_oauth_clients,
    normalize_scopes,
)
from modulo.db.rls import set_rls_org
from modulo.settings import Settings, get_settings

_log = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/mcp/oauth", tags=["mcp-oauth"])


class CreateOAuthClientRequest(BaseModel):
    name: str = Field(min_length=1, max_length=255)
    redirect_uris: list[str] = Field(min_length=1, description="Allowed redirect URIs")
    scopes: list[str] = Field(min_length=1, description="Allowed scopes")


class CreateOAuthClientResponse(BaseModel):
    id: str
    client_id: str
    client_secret: str
    name: str


class OAuthClientItem(BaseModel):
    id: str
    client_id: str
    name: str
    scopes: list[str]
    redirect_uris: list[str]
    created_at: str


class DeleteOAuthClientResponse(BaseModel):
    deleted: bool


@router.post("/clients", response_model=CreateOAuthClientResponse, status_code=status.HTTP_201_CREATED)
async def register_oauth_client(
    body: CreateOAuthClientRequest,
    session: AsyncSession = Depends(get_db_session),
    principal: AuthenticatedPrincipal = Depends(get_current_user),
    settings: Settings = Depends(get_settings),
) -> CreateOAuthClientResponse:
    if principal.org_role not in ("admin", "operator"):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Only admin or operator users can register OAuth clients",
        )

    if not settings.modulo_public_url or settings.modulo_public_url == "http://localhost:8000":
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="MODULO_PUBLIC_URL must be configured for OAuth flow",
        )

    try:
        normalize_scopes(" ".join(body.scopes))
    except InvalidScopeError as e:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(e),
        ) from e

    redirect_uris_str = " ".join(body.redirect_uris)
    scopes_str = " ".join(body.scopes)

    async with session.begin():
        await set_rls_org(session, principal.organisation_id)
        client, raw_secret = await create_oauth_client(
            session,
            org_id=principal.organisation_id,
            name=body.name,
            scopes=scopes_str,
            redirect_uris=redirect_uris_str,
            created_by=principal.account_id,
        )

    return CreateOAuthClientResponse(
        id=str(client.id),
        client_id=client.client_id,
        client_secret=raw_secret,
        name=client.name,
    )


@router.get("/clients", response_model=list[OAuthClientItem])
async def list_oauth_clients_endpoint(
    session: AsyncSession = Depends(get_db_session),
    principal: AuthenticatedPrincipal = Depends(get_current_user),
) -> list[OAuthClientItem]:
    async with session.begin():
        await set_rls_org(session, principal.organisation_id)
        clients = await list_oauth_clients(session, principal.organisation_id)
    return [OAuthClientItem(**c) for c in clients]


@router.delete("/clients/{client_id}", response_model=DeleteOAuthClientResponse)
async def remove_oauth_client(
    client_id: str,
    session: AsyncSession = Depends(get_db_session),
    principal: AuthenticatedPrincipal = Depends(get_current_user),
) -> DeleteOAuthClientResponse:
    if principal.org_role not in ("admin", "operator"):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Only admin or operator users can delete OAuth clients",
        )

    async with session.begin():
        await set_rls_org(session, principal.organisation_id)
        deleted = await delete_oauth_client(session, client_id=client_id, org_id=principal.organisation_id)

    if not deleted:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="OAuth client not found",
        )
    return DeleteOAuthClientResponse(deleted=True)
