"""API key management — create, list, revoke. Returns MCP config snippet."""

import uuid
from datetime import datetime
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from modulo.api.dependencies import get_db_session
from modulo.auth.api_key import create_api_key, list_api_keys, revoke_api_key, update_api_key
from modulo.auth.dependencies import get_current_user
from modulo.auth.jwt import AuthenticatedPrincipal
from modulo.auth.team_rbac import ORG_ROLE_HIERARCHY
from modulo.core.feature_flags import resolve_plan_context
from modulo.db.rls import set_rls_org, set_rls_user_context
from modulo.settings import Settings, get_settings

router = APIRouter(prefix="/api/v1/api-keys", tags=["api-keys"])


class ApiKeyCreate(BaseModel):
    name: str = Field(min_length=1)
    role: str = "operator"
    expires_at: str | None = None
    team_id: str | None = None


class ApiKeyUpdate(BaseModel):
    name: str | None = Field(None, min_length=1)
    role: str | None = Field(None, min_length=1)
    team_id: str | None = None


class ApiKeyCreatedResponse(BaseModel):
    id: uuid.UUID
    name: str
    role: str
    full_key: str
    lookup_prefix: str
    created_at: datetime
    team_id: str | None = None

    model_config = {"from_attributes": False}


class ApiKeyRevokeResponse(BaseModel):
    id: uuid.UUID
    revoked: bool


class McpConfigResponse(BaseModel):
    mcp_url: str
    config_snippet: dict[str, Any]


def _require_team_rbac(settings: Settings) -> None:
    ctx = resolve_plan_context(settings)
    if not ctx.feature_enabled("team_rbac"):
        raise HTTPException(
            status_code=status.HTTP_402_PAYMENT_REQUIRED,
            detail="Team-scoped API keys require an upgraded plan",
        )


def _require_admin(principal: AuthenticatedPrincipal) -> None:
    if ORG_ROLE_HIERARCHY.get(principal.org_role, -1) < ORG_ROLE_HIERARCHY["admin"]:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Only admin users can perform this action",
        )


@router.post("", response_model=ApiKeyCreatedResponse, status_code=status.HTTP_201_CREATED)
async def create_api_key_endpoint(
    body: ApiKeyCreate,
    session: AsyncSession = Depends(get_db_session),
    principal: AuthenticatedPrincipal = Depends(get_current_user),
    settings: Settings = Depends(get_settings),
) -> ApiKeyCreatedResponse:
    if body.role not in ("operator", "runner"):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="role must be 'operator' or 'runner'. admin keys are prohibited.",
        )
    team_id: uuid.UUID | None = None
    if body.team_id is not None:
        _require_team_rbac(settings)
        _require_admin(principal)
        team_id = uuid.UUID(body.team_id)
    expires_at: datetime | None = None
    if body.expires_at:
        expires_at = datetime.fromisoformat(body.expires_at)
    async with session.begin():
        await set_rls_org(session, principal.organisation_id)
        await set_rls_user_context(session, principal.account_id, principal.org_role)
        key, full_key = await create_api_key(
            session,
            org_id=principal.organisation_id,
            name=body.name,
            role=body.role,
            created_by=principal.account_id,
            team_id=team_id,
            expires_at=expires_at,
        )
    return ApiKeyCreatedResponse(
        id=key.id,
        name=key.name,
        role=key.role,
        full_key=full_key,
        lookup_prefix=f"mk_{key.lookup_prefix}****",
        created_at=key.created_at,
        team_id=str(key.team_id) if key.team_id else None,
    )


@router.get("", response_model=list[dict[str, Any]])
async def list_api_keys_endpoint(
    session: AsyncSession = Depends(get_db_session),
    principal: AuthenticatedPrincipal = Depends(get_current_user),
) -> list[dict[str, Any]]:
    async with session.begin():
        await set_rls_org(session, principal.organisation_id)
        await set_rls_user_context(session, principal.account_id, principal.org_role)
        return await list_api_keys(session, principal.organisation_id)


@router.put("/{key_id}")
async def update_api_key_endpoint(
    key_id: uuid.UUID,
    body: ApiKeyUpdate,
    session: AsyncSession = Depends(get_db_session),
    principal: AuthenticatedPrincipal = Depends(get_current_user),
    settings: Settings = Depends(get_settings),
) -> dict[str, Any]:
    if body.role is not None and body.role not in ("operator", "runner"):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="role must be 'operator' or 'runner'.",
        )
    team_id: uuid.UUID | None = None
    if body.team_id is not None:
        _require_team_rbac(settings)
        _require_admin(principal)
        team_id = uuid.UUID(body.team_id)
    async with session.begin():
        await set_rls_org(session, principal.organisation_id)
        await set_rls_user_context(session, principal.account_id, principal.org_role)
        key = await update_api_key(
            session,
            key_id,
            principal.organisation_id,
            name=body.name,
            role=body.role,
            team_id=team_id,
        )
    if key is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="API key not found")
    return {
        "id": str(key.id),
        "name": key.name,
        "role": key.role,
        "team_id": str(key.team_id) if key.team_id else None,
        "expires_at": key.expires_at.isoformat() if key.expires_at else None,
    }


@router.delete("/{key_id}", response_model=ApiKeyRevokeResponse)
async def revoke_api_key_endpoint(
    key_id: uuid.UUID,
    session: AsyncSession = Depends(get_db_session),
    principal: AuthenticatedPrincipal = Depends(get_current_user),
) -> ApiKeyRevokeResponse:
    async with session.begin():
        await set_rls_org(session, principal.organisation_id)
        await set_rls_user_context(session, principal.account_id, principal.org_role)
        revoked = await revoke_api_key(session, key_id, principal.organisation_id)
    if not revoked:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="API key not found")
    return ApiKeyRevokeResponse(id=key_id, revoked=True)


@router.get("/mcp-config", response_model=McpConfigResponse)
async def mcp_config_endpoint(
    settings: Settings = Depends(get_settings),
    _: str = Depends(get_current_user),
) -> McpConfigResponse:
    """Return the MCP server URL and config snippet for Claude Desktop / Cursor."""
    mcp_url = f"{settings.modulo_public_url}/mcp"
    snippet = {
        "mcpServers": {
            "modulo": {
                "url": mcp_url,
                "apiKey": "mk_<your-api-key>",
                "description": "Governed orchestration for your agentic SDLC",
            }
        }
    }
    return McpConfigResponse(mcp_url=mcp_url, config_snippet=snippet)
