"""API key management — create, list, revoke. Returns MCP config snippet."""

import logging
import uuid
from datetime import UTC, datetime
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy import func, or_, select
from sqlalchemy.exc import IntegrityError, ProgrammingError, SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from modulo.api.constants import MSG_INTERNAL_SERVER_ERROR, MSG_RESOURCE_ALREADY_EXISTS
from modulo.api.db_error_handling import handle_db_errors
from modulo.api.dependencies import deny_break_glass_mint, get_db_session, require_permission
from modulo.auth.api_key import (
    _UNSET,
    KEY_SCOPES,
    ApiKeyScopeError,
    create_api_key,
    list_api_keys,
    revoke_api_key,
    update_api_key,
)
from modulo.auth.dependencies import get_current_tenant_user, resolve_role_from_membership
from modulo.auth.jwt import TenantPrincipal
from modulo.auth.team_rbac import ORG_ROLE_HIERARCHY, org_role_level
from modulo.core.audit_logger import append_audit_event_isolated
from modulo.core.feature_flags import get_registry, resolve_plan_context
from modulo.db.models.account import Account
from modulo.db.models.api_key import OrgApiKey
from modulo.db.rls import set_rls_org, set_rls_user_context
from modulo.settings import Settings, get_settings

_CODE_API_KEYS_CREATE_API = "api_keys.create_api_key_endpoint"
_MSG_API_KEYS_NOT_AVAILABLE = "API keys are not available. Run database migrations to enable this feature."
_MSG_DATABASE_TEMPORARILY_UNAVAILABLE_PLEASE = "Database temporarily unavailable. Please try again."
_CODE_API_KEYS_UPDATE_API = "api_keys.update_api_key_endpoint"
_CODE_API_KEYS_REVOKE_API = "api_keys.revoke_api_key_endpoint"

# FAR-620: the org-level feature flag gating user-scoped MCP key minting.
_FLAG_USER_SCOPED_MCP_KEYS = "user_scoped_mcp_keys"
# Per-(account, org) quota of ACTIVE user-scoped keys: revoked_at IS NULL AND
# not expired (expires_at IS NULL OR expires_at > now) — an expired key is
# unusable and must not consume quota.
_USER_KEY_ACTIVE_QUOTA = 10


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
    # FAR-620: optional caller scope. None/'org' = org-level key (today's
    # behaviour); 'user' = per-user key (flag-gated + quota'd). The scope is
    # stamped at mint and IMMUTABLE afterwards.
    scope: str | None = None


class ApiKeyUpdate(BaseModel):
    name: str | None = Field(None, min_length=1)
    role: str | None = Field(None, min_length=1)
    team_id: str | None = None
    expires_at: str | None = None
    # FAR-620: present ONLY so an explicit payload field can be rejected —
    # the caller scope is immutable post-mint.
    scope: str | None = None


class ApiKeyCreatedResponse(BaseModel):
    id: uuid.UUID
    name: str
    role: str
    key_value: str
    lookup_prefix: str
    created_at: datetime
    team_id: str | None = None
    scope: str = "org"

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


async def _user_keys_flag_enabled(org_id: uuid.UUID) -> bool:
    """Resolve the per-org ``user_scoped_mcp_keys`` flag (FAR-620).

    Fail-closed: any resolution error is treated as OFF so a broken flag read
    can never enable user-scoped key minting. Mirrors the ``remy.py``
    ``resolve_flag`` precedent (org ``settings_json.feature_overrides`` wins
    over the catalog default).
    """
    try:
        return bool(await get_registry().resolve_flag(_FLAG_USER_SCOPED_MCP_KEYS, org_id=org_id))
    except Exception:
        logger.warning("feature_flag.user_scoped_mcp_keys_read_failed", exc_info=True)
        return False


async def _enforce_user_key_quota(
    session: AsyncSession,
    principal: TenantPrincipal,
) -> None:
    """Enforce the per-(account, org) quota of ACTIVE user-scoped keys.

    FAR-620: at most ``_USER_KEY_ACTIVE_QUOTA`` (10) user-scoped keys per
    account per org, counting only ACTIVE keys — ``revoked_at IS NULL`` AND
    not expired (``expires_at IS NULL OR expires_at > now``): a revoked or
    expired key is unusable and does not consume quota. The account row is
    locked ``FOR UPDATE`` (the me.py pattern) so two concurrent mints serialise
    on the same row — the second re-counts after the first commits, closing
    the TOCTOU window. Distinct error shape from the role mint-cap (429 vs 403).
    """
    account = await session.get(Account, principal.account_id, with_for_update=True)
    if account is None:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Active organisation membership required to manage API keys",
        )
    active = (
        await session.execute(
            select(func.count())
            .select_from(OrgApiKey)
            .where(
                OrgApiKey.organisation_id == principal.organisation_id,
                OrgApiKey.account_id == principal.account_id,
                OrgApiKey.scope == "user",
                OrgApiKey.revoked_at.is_(None),
                or_(OrgApiKey.expires_at.is_(None), OrgApiKey.expires_at > datetime.now(UTC)),
            )
        )
    ).scalar_one()
    if active >= _USER_KEY_ACTIVE_QUOTA:
        logger.warning(
            "api_keys.user_key_quota_exceeded",
            extra={
                "org_id": str(principal.organisation_id),
                "account_id": str(principal.account_id),
                "active_user_keys": active,
                "quota": _USER_KEY_ACTIVE_QUOTA,
            },
        )
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail=(
                f"User-scoped API key quota exceeded: at most {_USER_KEY_ACTIVE_QUOTA} "
                "active user-scoped keys per account. Revoke one first."
            ),
        )


async def _validate_create_request(req: ApiKeyCreate, principal: TenantPrincipal) -> str:
    """Validate role/scope on a create payload and resolve the requested scope.

    FAR-620: 'org' (or omitted) keeps the pre-flag behaviour; 'user' is gated
    by the org flag (OFF ⇒ 422, never a silent downgrade to an org key).
    """
    if req.role not in ("operator", "runner"):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="role must be 'operator' or 'runner'. admin keys are prohibited.",
        )
    requested_scope = req.scope if req.scope is not None else "org"
    if requested_scope not in KEY_SCOPES:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=f"scope must be one of {sorted(KEY_SCOPES)}",
        )
    if requested_scope == "user" and not await _user_keys_flag_enabled(principal.organisation_id):
        logger.warning(
            "api_keys.user_key_mint_denied_flag_off",
            extra={"org_id": str(principal.organisation_id), "account_id": str(principal.account_id)},
        )
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="User-scoped API keys are not enabled for this organisation",
        )
    return requested_scope


async def _resolve_new_team_id(
    team_id_raw: str | None,
    settings: Settings,
    session: AsyncSession,
    principal: TenantPrincipal,
) -> uuid.UUID | None:
    """Resolve the team scope on a create payload (team-tier + admin gated)."""
    if team_id_raw is None:
        return None
    await _require_team_rbac(settings, session)
    _require_admin(principal)
    return uuid.UUID(team_id_raw)


def _parse_future_expires_at(value: str | None) -> datetime | None:
    """Parse an optional ISO expiry, rejecting values that are not in the future."""
    if not value:
        return None
    expires_at = _parse_expires_at(value)
    if expires_at <= datetime.now(UTC):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="expires_at must be in the future",
        )
    return expires_at


async def _create_key_tx(
    session: AsyncSession,
    principal: TenantPrincipal,
    name: str,
    role: str,
    team_id: uuid.UUID | None,
    expires_at: datetime | None,
    requested_scope: str,
) -> tuple[OrgApiKey, str]:
    """Mint the key in one transaction: RLS context, role cap, quota, create."""
    async with session.begin():
        await set_rls_org(session, principal.organisation_id)
        await set_rls_user_context(session, principal.account_id, principal.org_role)
        await _enforce_mint_cap(session, principal, role)
        if requested_scope == "user":
            await _enforce_user_key_quota(session, principal)
        return await create_api_key(
            session,
            org_id=principal.organisation_id,
            name=name,
            role=role,
            account_id=principal.account_id,
            team_id=team_id,
            expires_at=expires_at,
            scope=requested_scope,
        )


async def _mint_api_key(
    session: AsyncSession,
    principal: TenantPrincipal,
    name: str,
    role: str,
    team_id: uuid.UUID | None,
    expires_at: datetime | None,
    requested_scope: str,
) -> tuple[OrgApiKey, str]:
    """Mint the key, mapping DB errors to the route's HTTP error contract."""
    try:
        return await _create_key_tx(session, principal, name, role, team_id, expires_at, requested_scope)
    except ApiKeyScopeError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=str(exc),
        ) from None
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
    requested_scope = await _validate_create_request(req, principal)
    name = _normalise_name(req.name)
    if not name:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="API key name must not be blank",
        )
    team_id = await _resolve_new_team_id(req.team_id, settings, session, principal)
    expires_at = _parse_future_expires_at(req.expires_at)
    key, full_key = await _mint_api_key(session, principal, name, req.role, team_id, expires_at, requested_scope)

    # PRD §8.12 ``api_key_created``: key minting was never audited. Written in a
    # fresh transaction (the create above already committed) and failure-isolated
    # so a broken audit append never blocks a successful key creation. RLS context
    # (SET LOCAL) reverts on COMMIT, so it must be re-established in this fresh
    # transaction or the STRICT-RLS audit INSERT is rejected (see admin_create_team).
    #
    # FAR-620 payload stamps (shape parity with the MCP surface): ``auth_type``
    # (REST is JWT-only), ``key_scope`` and the masked lookup prefix.
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
            "auth_type": "jwt",
            "key_scope": key.scope if isinstance(key.scope, str) else requested_scope,
            "lookup_prefix": f"mk_{key.lookup_prefix}****",
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
        scope=key.scope if isinstance(key.scope, str) else requested_scope,
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


def _validate_update_payload(req: ApiKeyUpdate) -> None:
    """Reject invalid roles and immutable-scope mutations on an update payload."""
    if req.role is not None and req.role not in ("operator", "runner"):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="role must be 'operator' or 'runner'.",
        )
    # FAR-620: the caller scope is IMMUTABLE post-mint — an update payload
    # carrying an explicit ``scope`` is rejected (422), never applied or
    # silently ignored.
    if "scope" in req.model_fields_set:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="scope is immutable: an API key's caller scope cannot be changed after mint",
        )


def _resolve_update_name(req: ApiKeyUpdate) -> str | None:
    """Normalise an update payload's name, rejecting blank values."""
    if req.name is None:
        return None
    name = _normalise_name(req.name)
    if not name:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="API key name must not be blank",
        )
    return name


async def _resolve_update_team_id(
    req: ApiKeyUpdate,
    settings: Settings,
    session: AsyncSession,
    principal: TenantPrincipal,
) -> uuid.UUID | object | None:
    """Resolve the team scope on an update payload (_UNSET = leave unchanged)."""
    if "team_id" not in req.model_fields_set:
        return _UNSET
    if req.team_id is not None:
        await _require_team_rbac(settings, session)
        _require_admin(principal)
        return uuid.UUID(req.team_id)
    # Explicitly clearing the team scope is an admin operation, same as
    # setting one — but it needs no team-tier feature check (removing
    # scope never enables a team feature).
    _require_admin(principal)
    return None


async def _apply_key_update_tx(
    session: AsyncSession,
    key_id: uuid.UUID,
    principal: TenantPrincipal,
    role: str | None,
    name: str | None,
    team_id: uuid.UUID | object | None,
    expires_at: datetime | None,
) -> OrgApiKey | None:
    """Apply the key update in one transaction: RLS context, role cap, update."""
    async with session.begin():
        await set_rls_org(session, principal.organisation_id)
        await set_rls_user_context(session, principal.account_id, principal.org_role)
        if role is not None:
            await _enforce_mint_cap(session, principal, role)
        return await update_api_key(
            session,
            key_id,
            principal.organisation_id,
            name=name,
            role=role,
            team_id=team_id,
            expires_at=expires_at,
        )


async def _apply_key_update(
    session: AsyncSession,
    key_id: uuid.UUID,
    principal: TenantPrincipal,
    role: str | None,
    name: str | None,
    team_id: uuid.UUID | object | None,
    expires_at: datetime | None,
) -> OrgApiKey | None:
    """Apply the key update, mapping DB errors to the route's HTTP error contract."""
    try:
        return await _apply_key_update_tx(session, key_id, principal, role, name, team_id, expires_at)
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


@router.put("/{key_id}", dependencies=[Depends(deny_break_glass_mint)])
@handle_db_errors(_CODE_API_KEYS_UPDATE_API)
async def update_api_key_endpoint(
    key_id: uuid.UUID,
    req: ApiKeyUpdate,
    session: AsyncSession = Depends(get_db_session),
    principal: TenantPrincipal = require_permission("api_key.revoke"),
    settings: Settings = Depends(get_settings),
) -> dict[str, Any]:
    _validate_update_payload(req)
    name = _resolve_update_name(req)
    team_id = await _resolve_update_team_id(req, settings, session, principal)
    expires_at = _parse_future_expires_at(req.expires_at)
    key = await _apply_key_update(session, key_id, principal, req.role, name, team_id, expires_at)
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
            revoked_key = await revoke_api_key(session, key_id, principal.organisation_id)
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
    if not revoked_key:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="API key not found")

    # PRD §8.12 ``api_key_revoked``: key revocation was never audited. Written in
    # a fresh transaction (the revoke above already committed) and failure-isolated
    # so a broken audit append never fails a completed revocation. RLS context
    # (SET LOCAL) reverts on COMMIT, so it must be re-established in this fresh
    # transaction or the STRICT-RLS audit INSERT is rejected (see admin_create_team).
    #
    # FAR-620 payload stamps (shape parity with the MCP surface).
    await append_audit_event_isolated(
        session,
        principal,
        resource_type="api_key",
        event_type="api_key_revoked",
        resource_id=key_id,
        payload={
            "revoked_by": str(principal.account_id),
            "auth_type": "jwt",
            "key_scope": revoked_key.scope if isinstance(revoked_key.scope, str) else "org",
            "lookup_prefix": f"mk_{revoked_key.lookup_prefix}****",
        },
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
