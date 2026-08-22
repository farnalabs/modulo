"""API key management — create, list, revoke. Returns MCP config snippet."""

import logging
import uuid
from datetime import UTC, datetime
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy.exc import IntegrityError, ProgrammingError, SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from modulo.api.constants import MSG_INTERNAL_SERVER_ERROR, MSG_RESOURCE_ALREADY_EXISTS
from modulo.api.db_error_handling import handle_db_errors
from modulo.api.dependencies import deny_break_glass_mint, get_db_session, require_permission
from modulo.auth.api_key import _UNSET, create_api_key, list_api_keys, revoke_api_key, update_api_key
from modulo.auth.dependencies import get_current_tenant_user, resolve_role_from_membership
from modulo.auth.jwt import TenantPrincipal
from modulo.auth.team_rbac import ORG_ROLE_HIERARCHY, org_role_level
from modulo.core.audit_logger import append_audit_event_isolated
from modulo.core.feature_flags import resolve_plan_context
from modulo.db.rls import set_rls_org, set_rls_user_context
from modulo.settings import Settings, get_settings

_CODE_API_KEYS_CREATE_API = "api_keys.create_api_key_endpoint"
_MSG_API_KEYS_NOT_AVAILABLE = "API keys are not available. Run database migrations to enable this feature."
_MSG_DATABASE_TEMPORARILY_UNAVAILABLE_PLEASE = "Database temporarily unavailable. Please try again."
_CODE_API_KEYS_UPDATE_API = "api_keys.update_api_key_endpoint"
_CODE_API_KEYS_REVOKE_API = "api_keys.revoke_api_key_endpoint"


logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/api-keys", tags=["api-keys"])


def _normalise_name(name: str) -> str:
    """Strip surrounding whitespace from an API key name."""
    return name.strip()


def _parse_expires_at(value: str) -> datetime:
    """Parse an ISO datetime, normalising naive values to UTC."""
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed


def _require_runner(principal: TenantPrincipal, permission: str) -> None:
    """Thin compatibility wrapper: require the org role for a runner-level permission.

    Endpoints now use the `require_permission` dependency; this wrapper is kept
    for direct-call tests and documents the runner floor for API-key ops.
    """
    from fastapi import HTTPException

    from modulo.auth.permissions import PermissionDenied, assert_org_role, resolve_required

    try:
        assert_org_role(principal.org_role, resolve_required(permission), permission)
    except PermissionDenied as exc:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=str(exc),
        ) from exc


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


async def _enforce_mint_cap(session: AsyncSession, principal: TenantPrincipal, requested_role: str) -> None:
    """Enforce the API-key role-cap: never mint above the caller's LIVE role.

    ``get_current_tenant_user`` already re-reads the live membership role
    (ADR 017), but this explicit ``resolve_role_from_membership`` read is the
    cap's own authoritative source — a runner cannot mint an operator key, an
    operator can mint operator/runner, and a removed/deactivated member's live
    role is None so minting is denied outright.
    """
    live_role = await resolve_role_from_membership(
        session,
        str(principal.account_id),
        str(principal.organisation_id),
    )
    if live_role is None:
        logger.warning(
            "permission.api_key_role_cap",
            extra={"requested_role": requested_role, "live_role": None},
        )
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Active organisation membership required to manage API keys",
        )
    if org_role_level(requested_role) > org_role_level(live_role):
        logger.warning(
            "permission.api_key_role_cap",
            extra={"requested_role": requested_role, "live_role": live_role},
        )
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=(f"Cannot use role '{requested_role}' for an API key while your live role is '{live_role}'"),
        )


@router.post(
    "",
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(deny_break_glass_mint)],
)
@handle_db_errors(_CODE_API_KEYS_CREATE_API)
async def create_api_key_endpoint(
    req: ApiKeyCreate,
    session: AsyncSession = Depends(get_db_session),
    principal: TenantPrincipal = require_permission("api_key.create"),
    settings: Settings = Depends(get_settings),
) -> ApiKeyCreatedResponse:
    if req.role not in ("operator", "runner"):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="role must be 'operator' or 'runner'. admin keys are prohibited.",
        )
    name = _normalise_name(req.name)
    if not name:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="API key name must not be blank",
        )
    team_id: uuid.UUID | None = None
    if req.team_id is not None:
        await _require_team_rbac(settings, session)
        _require_admin(principal)
        team_id = uuid.UUID(req.team_id)
    expires_at: datetime | None = None
    if req.expires_at:
        expires_at = _parse_expires_at(req.expires_at)
        if expires_at <= datetime.now(UTC):
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                detail="expires_at must be in the future",
            )
    try:
        async with session.begin():
            await set_rls_org(session, principal.organisation_id)
            await set_rls_user_context(session, principal.account_id, principal.org_role)
            await _enforce_mint_cap(session, principal, req.role)
            key, full_key = await create_api_key(
                session,
                org_id=principal.organisation_id,
                name=name,
                role=req.role,
                account_id=principal.account_id,
                team_id=team_id,
                expires_at=expires_at,
            )
    except IntegrityError:
        logger.exception(_CODE_API_KEYS_CREATE_API)
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=MSG_RESOURCE_ALREADY_EXISTS,
        ) from None
    except ProgrammingError:
        logger.exception(_CODE_API_KEYS_CREATE_API)
        raise HTTPException(
            status_code=status.HTTP_501_NOT_IMPLEMENTED,
            detail=_MSG_API_KEYS_NOT_AVAILABLE,
        ) from None
    except SQLAlchemyError:
        logger.exception(_CODE_API_KEYS_CREATE_API)
        logger.warning("create_api_key SQLAlchemyError", extra={"org_id": str(principal.organisation_id)})
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=_MSG_DATABASE_TEMPORARILY_UNAVAILABLE_PLEASE,
        ) from None
    except HTTPException:
        raise
    except Exception:
        logger.exception("Unexpected error in create_api_key_endpoint")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=MSG_INTERNAL_SERVER_ERROR,
        ) from None

    # PRD §8.12 ``api_key_created``: key minting was never audited. Written in a
    # fresh transaction (the create above already committed) and failure-isolated
    # so a broken audit append never blocks a successful key creation. RLS context
    # (SET LOCAL) reverts on COMMIT, so it must be re-established in this fresh
    # transaction or the STRICT-RLS audit INSERT is rejected (see admin_create_team).
    await append_audit_event_isolated(
        session,
        principal,
        resource_type="api_key",
        event_type="api_key_created",
        resource_id=key.id,
        payload={
            "name": name,
            "role": req.role,
            "team_id": str(team_id) if team_id else None,
        },
        log_key="api_keys.create_audit_failed",
    )

    return ApiKeyCreatedResponse(
        id=key.id,
        name=key.name,
        role=key.role,
        key_value=full_key,
        lookup_prefix=f"mk_{key.lookup_prefix}****",
        created_at=key.created_at,
        team_id=str(key.team_id) if key.team_id else None,
    )


@router.get("")
@handle_db_errors("api_keys.list_api_keys_endpoint")
async def list_api_keys_endpoint(
    session: AsyncSession = Depends(get_db_session),
    principal: TenantPrincipal = require_permission("api_key.update"),
) -> list[dict[str, Any]]:
    # SECURITY (#1305): raise floor to operator — runners should not enumerate all org keys.
    if ORG_ROLE_HIERARCHY.get(principal.org_role, -1) < ORG_ROLE_HIERARCHY["operator"]:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Only admin or operator users can list API keys",
        )
    try:
        async with session.begin():
            await set_rls_org(session, principal.organisation_id)
            await set_rls_user_context(session, principal.account_id, principal.org_role)
            return await list_api_keys(session, principal.organisation_id)
    except ProgrammingError:
        logger.exception("api_keys.list_api_keys_endpoint")
        raise HTTPException(
            status_code=status.HTTP_501_NOT_IMPLEMENTED,
            detail=_MSG_API_KEYS_NOT_AVAILABLE,
        ) from None
    except SQLAlchemyError:
        logger.exception("api_keys.list_api_keys_endpoint")
        logger.warning("list_api_keys SQLAlchemyError", extra={"org_id": str(principal.organisation_id)})
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=_MSG_DATABASE_TEMPORARILY_UNAVAILABLE_PLEASE,
        ) from None
    except HTTPException:
        raise
    except Exception:
        logger.exception("Unexpected error in list_api_keys_endpoint")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=MSG_INTERNAL_SERVER_ERROR,
        ) from None


@router.put("/{key_id}", dependencies=[Depends(deny_break_glass_mint)])
@handle_db_errors(_CODE_API_KEYS_UPDATE_API)
async def update_api_key_endpoint(
    key_id: uuid.UUID,
    req: ApiKeyUpdate,
    session: AsyncSession = Depends(get_db_session),
    principal: TenantPrincipal = require_permission("api_key.revoke"),
    settings: Settings = Depends(get_settings),
) -> dict[str, Any]:
    if req.role is not None and req.role not in ("operator", "runner"):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="role must be 'operator' or 'runner'.",
        )
    name: str | None = None
    if req.name is not None:
        name = _normalise_name(req.name)
        if not name:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                detail="API key name must not be blank",
            )
    team_id: uuid.UUID | object | None = _UNSET
    if "team_id" in req.model_fields_set:
        if req.team_id is not None:
            await _require_team_rbac(settings, session)
            _require_admin(principal)
            team_id = uuid.UUID(req.team_id)
        else:
            # Explicitly clearing the team scope is an admin operation, same as
            # setting one — but it needs no team-tier feature check (removing
            # scope never enables a team feature).
            _require_admin(principal)
            team_id = None
    expires_at: datetime | None = None
    if req.expires_at:
        expires_at = _parse_expires_at(req.expires_at)
        if expires_at <= datetime.now(UTC):
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                detail="expires_at must be in the future",
            )
    try:
        async with session.begin():
            await set_rls_org(session, principal.organisation_id)
            await set_rls_user_context(session, principal.account_id, principal.org_role)
            if req.role is not None:
                await _enforce_mint_cap(session, principal, req.role)
            key = await update_api_key(
                session,
                key_id,
                principal.organisation_id,
                name=name,
                role=req.role,
                team_id=team_id,
                expires_at=expires_at,
            )
    except IntegrityError:
        logger.exception(_CODE_API_KEYS_UPDATE_API)
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=MSG_RESOURCE_ALREADY_EXISTS,
        ) from None
    except ProgrammingError:
        logger.exception(_CODE_API_KEYS_UPDATE_API)
        raise HTTPException(
            status_code=status.HTTP_501_NOT_IMPLEMENTED,
            detail=_MSG_API_KEYS_NOT_AVAILABLE,
        ) from None
    except SQLAlchemyError:
        logger.exception(_CODE_API_KEYS_UPDATE_API)
        logger.warning(
            "update_api_key SQLAlchemyError",
            extra={"org_id": str(principal.organisation_id), "key_id": str(key_id)},
        )
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=_MSG_DATABASE_TEMPORARILY_UNAVAILABLE_PLEASE,
        ) from None
    except HTTPException:
        raise
    except Exception:
        logger.exception("Unexpected error in update_api_key_endpoint")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=MSG_INTERNAL_SERVER_ERROR,
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


@router.delete("/{key_id}", dependencies=[Depends(deny_break_glass_mint)])
@handle_db_errors(_CODE_API_KEYS_REVOKE_API)
async def revoke_api_key_endpoint(
    key_id: uuid.UUID,
    session: AsyncSession = Depends(get_db_session),
    principal: TenantPrincipal = require_permission("api_key.revoke"),
) -> ApiKeyRevokeResponse:
    try:
        async with session.begin():
            await set_rls_org(session, principal.organisation_id)
            await set_rls_user_context(session, principal.account_id, principal.org_role)
            revoked = await revoke_api_key(session, key_id, principal.organisation_id)
    except IntegrityError:
        logger.exception(_CODE_API_KEYS_REVOKE_API)
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=MSG_RESOURCE_ALREADY_EXISTS,
        ) from None
    except ProgrammingError:
        logger.exception(_CODE_API_KEYS_REVOKE_API)
        raise HTTPException(
            status_code=status.HTTP_501_NOT_IMPLEMENTED,
            detail=_MSG_API_KEYS_NOT_AVAILABLE,
        ) from None
    except SQLAlchemyError:
        logger.exception(_CODE_API_KEYS_REVOKE_API)
        logger.warning(
            "revoke_api_key SQLAlchemyError",
            extra={"org_id": str(principal.organisation_id), "key_id": str(key_id)},
        )
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=_MSG_DATABASE_TEMPORARILY_UNAVAILABLE_PLEASE,
        ) from None
    except HTTPException:
        raise
    except Exception:
        logger.exception("Unexpected error in revoke_api_key_endpoint")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=MSG_INTERNAL_SERVER_ERROR,
        ) from None
    if not revoked:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="API key not found")

    # PRD §8.12 ``api_key_revoked``: key revocation was never audited. Written in
    # a fresh transaction (the revoke above already committed) and failure-isolated
    # so a broken audit append never fails a completed revocation. RLS context
    # (SET LOCAL) reverts on COMMIT, so it must be re-established in this fresh
    # transaction or the STRICT-RLS audit INSERT is rejected (see admin_create_team).
    await append_audit_event_isolated(
        session,
        principal,
        resource_type="api_key",
        event_type="api_key_revoked",
        resource_id=key_id,
        payload={"revoked_by": str(principal.account_id)},
        log_key="api_keys.revoke_audit_failed",
    )

    return ApiKeyRevokeResponse(id=key_id, revoked=True)


@router.get("/mcp-config")
@handle_db_errors("api_keys.mcp_config_endpoint")
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
                    "description": "Agent governance for your agentic SDLC",
                }
            }
        }
        return McpConfigResponse(mcp_url=mcp_url, config_snippet=snippet)
    except Exception:
        logger.exception("Unexpected error in mcp_config_endpoint")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=MSG_INTERNAL_SERVER_ERROR,
        ) from None
