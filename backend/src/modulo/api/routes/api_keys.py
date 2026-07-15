"""API key management — create, list, revoke. Returns MCP config snippet."""

import logging
import uuid
from datetime import datetime
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy.exc import IntegrityError, ProgrammingError, SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from modulo.api.db_error_handling import handle_db_errors
from modulo.api.dependencies import get_db_session
from modulo.auth.api_key import create_api_key, list_api_keys, revoke_api_key, update_api_key
from modulo.auth.dependencies import get_current_tenant_user
from modulo.auth.jwt import TenantPrincipal
from modulo.auth.team_rbac import ORG_ROLE_HIERARCHY
from modulo.core.feature_flags import resolve_plan_context
from modulo.db.rls import set_rls_org, set_rls_user_context
from modulo.settings import Settings, get_settings

logger = logging.getLogger(__name__)

_log = logging.getLogger(__name__)

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
    expires_at: str | None = None


class ApiKeyCreatedResponse(BaseModel):
    id: uuid.UUID
    name: str
    role: str
    key_value: str
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


async def _require_team_rbac(settings: Settings, session: AsyncSession) -> None:
    ctx = await resolve_plan_context(settings, session)
    if not ctx.feature_enabled("team_rbac"):
        raise HTTPException(
            status_code=status.HTTP_402_PAYMENT_REQUIRED,
            detail="Team-scoped API keys require an upgraded plan",
        )


def _require_admin(principal: TenantPrincipal) -> None:
    if ORG_ROLE_HIERARCHY.get(principal.org_role, -1) < ORG_ROLE_HIERARCHY["admin"]:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Only admin users can perform this action",
        )


@handle_db_errors("api_keys.create_api_key_endpoint")
@router.post("", response_model=ApiKeyCreatedResponse, status_code=status.HTTP_201_CREATED)
async def create_api_key_endpoint(
    req: ApiKeyCreate,
    session: AsyncSession = Depends(get_db_session),
    principal: TenantPrincipal = Depends(get_current_tenant_user),
    settings: Settings = Depends(get_settings),
) -> ApiKeyCreatedResponse:
    if req.role not in ("operator", "runner"):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="role must be 'operator' or 'runner'. admin keys are prohibited.",
        )
    team_id: uuid.UUID | None = None
    if req.team_id is not None:
        await _require_team_rbac(settings, session)
        _require_admin(principal)
        team_id = uuid.UUID(req.team_id)
    expires_at: datetime | None = None
    if req.expires_at:
        expires_at = datetime.fromisoformat(req.expires_at)
    try:
        async with session.begin():
            await set_rls_org(session, principal.organisation_id)
            await set_rls_user_context(session, principal.account_id, principal.org_role)
            key, full_key = await create_api_key(
                session,
                org_id=principal.organisation_id,
                name=req.name,
                role=req.role,
                account_id=principal.account_id,
                team_id=team_id,
                expires_at=expires_at,
            )
    except IntegrityError:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="A resource with this value already exists",
        ) from None
    except ProgrammingError:
        raise HTTPException(
            status_code=status.HTTP_501_NOT_IMPLEMENTED,
            detail="API keys are not available. Run database migrations to enable this feature.",
        ) from None
    except SQLAlchemyError:
        logger.warning("create_api_key SQLAlchemyError", extra={"org_id": str(principal.organisation_id)})
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Database temporarily unavailable. Please try again.",
        ) from None
    except HTTPException:
        raise
    except Exception:
        logger.exception("Unexpected error in create_api_key_endpoint")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Internal server error",
        ) from None
    return ApiKeyCreatedResponse(
        id=key.id,
        name=key.name,
        role=key.role,
        key_value=full_key,
        lookup_prefix=f"mk_{key.lookup_prefix}****",
        created_at=key.created_at,
        team_id=str(key.team_id) if key.team_id else None,
    )


@handle_db_errors("api_keys.list_api_keys_endpoint")
@router.get("", response_model=list[dict[str, Any]])
async def list_api_keys_endpoint(
    session: AsyncSession = Depends(get_db_session),
    principal: TenantPrincipal = Depends(get_current_tenant_user),
) -> list[dict[str, Any]]:
    try:
        async with session.begin():
            await set_rls_org(session, principal.organisation_id)
            await set_rls_user_context(session, principal.account_id, principal.org_role)
            return await list_api_keys(session, principal.organisation_id)
    except ProgrammingError:
        raise HTTPException(
            status_code=status.HTTP_501_NOT_IMPLEMENTED,
            detail="API keys are not available. Run database migrations to enable this feature.",
        ) from None
    except SQLAlchemyError:
        logger.warning("list_api_keys SQLAlchemyError", extra={"org_id": str(principal.organisation_id)})
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Database temporarily unavailable. Please try again.",
        ) from None
    except HTTPException:
        raise
    except Exception:
        logger.exception("Unexpected error in list_api_keys_endpoint")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Internal server error",
        ) from None


@handle_db_errors("api_keys.update_api_key_endpoint")
@router.put("/{key_id}")
async def update_api_key_endpoint(
    key_id: uuid.UUID,
    req: ApiKeyUpdate,
    session: AsyncSession = Depends(get_db_session),
    principal: TenantPrincipal = Depends(get_current_tenant_user),
    settings: Settings = Depends(get_settings),
) -> dict[str, Any]:
    if req.role is not None and req.role not in ("operator", "runner"):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="role must be 'operator' or 'runner'.",
        )
    team_id: uuid.UUID | None = None
    if req.team_id is not None:
        await _require_team_rbac(settings, session)
        _require_admin(principal)
        team_id = uuid.UUID(req.team_id)
    expires_at: datetime | None = None
    if req.expires_at:
        expires_at = datetime.fromisoformat(req.expires_at)
    try:
        async with session.begin():
            await set_rls_org(session, principal.organisation_id)
            await set_rls_user_context(session, principal.account_id, principal.org_role)
            key = await update_api_key(
                session,
                key_id,
                principal.organisation_id,
                name=req.name,
                role=req.role,
                team_id=team_id,
                expires_at=expires_at,
            )
    except IntegrityError:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="A resource with this value already exists",
        ) from None
    except ProgrammingError:
        raise HTTPException(
            status_code=status.HTTP_501_NOT_IMPLEMENTED,
            detail="API keys are not available. Run database migrations to enable this feature.",
        ) from None
    except SQLAlchemyError:
        logger.warning(
            "update_api_key SQLAlchemyError",
            extra={"org_id": str(principal.organisation_id), "key_id": str(key_id)},
        )
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Database temporarily unavailable. Please try again.",
        ) from None
    except HTTPException:
        raise
    except Exception:
        logger.exception("Unexpected error in update_api_key_endpoint")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Internal server error",
        ) from None
    if key is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="API key not found")
    return {
        "id": str(key.id),
        "name": key.name,
        "role": key.role,
        "team_id": str(key.team_id) if key.team_id else None,
        "expires_at": key.expires_at.isoformat() if key.expires_at else None,
    }


@handle_db_errors("api_keys.revoke_api_key_endpoint")
@router.delete("/{key_id}", response_model=ApiKeyRevokeResponse)
async def revoke_api_key_endpoint(
    key_id: uuid.UUID,
    session: AsyncSession = Depends(get_db_session),
    principal: TenantPrincipal = Depends(get_current_tenant_user),
) -> ApiKeyRevokeResponse:
    try:
        async with session.begin():
            await set_rls_org(session, principal.organisation_id)
            await set_rls_user_context(session, principal.account_id, principal.org_role)
            revoked = await revoke_api_key(session, key_id, principal.organisation_id)
    except IntegrityError:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="A resource with this value already exists",
        ) from None
    except ProgrammingError:
        raise HTTPException(
            status_code=status.HTTP_501_NOT_IMPLEMENTED,
            detail="API keys are not available. Run database migrations to enable this feature.",
        ) from None
    except SQLAlchemyError:
        logger.warning(
            "revoke_api_key SQLAlchemyError",
            extra={"org_id": str(principal.organisation_id), "key_id": str(key_id)},
        )
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Database temporarily unavailable. Please try again.",
        ) from None
    except HTTPException:
        raise
    except Exception:
        logger.exception("Unexpected error in revoke_api_key_endpoint")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Internal server error",
        ) from None
    if not revoked:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="API key not found")
    return ApiKeyRevokeResponse(id=key_id, revoked=True)


@handle_db_errors("api_keys.mcp_config_endpoint")
@router.get("/mcp-config", response_model=McpConfigResponse)
async def mcp_config_endpoint(
    settings: Settings = Depends(get_settings),
    _: str = Depends(get_current_tenant_user),
) -> McpConfigResponse:
    """Return the MCP server URL and config snippet for Claude Desktop / Cursor."""
    try:
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
    except Exception:
        logger.exception("Unexpected error in mcp_config_endpoint")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Internal server error",
        ) from None
