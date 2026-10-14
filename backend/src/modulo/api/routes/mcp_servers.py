"""MCP-server registry REST API — inventory + tool-definition pinning (FAR-1692 S1+S2).

CRUD over the ``mcp_servers`` registry (the customer-side MCP servers their
agents consume) plus the S2 pinning surface:

* ``POST /{id}/observe-tools`` — record the server's observed tool definitions,
  recompute the canonical ``tools_hash`` and apply the drift decision. A hash
  that differs from the recorded ``approved_hash`` flips the pin to
  ``approved-stale`` and raises an operator notification + audit event. It NEVER
  auto-blocks (the client is outside Modulo's control — ADR 055,
  detect-and-surface).
* ``POST /{id}/approve-tools`` — approve the CURRENTLY observed tool set,
  recording ``approved_hash``/``approved_by``/``approved_at``.

Credentials, where held, are Fernet-encrypted at rest and never returned — only
``has_credentials``. Org-scoped (RLS) with team visibility (DB
``rls_team_isolation`` policy is the team boundary; no request-time team gate).
"""

import logging
import uuid
from datetime import UTC, datetime
from typing import Any, Literal

from cryptography.fernet import Fernet
from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field
from sqlalchemy.exc import IntegrityError, ProgrammingError, SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from modulo.api.constants import (
    MSG_DATABASE_TEMPORARILY_UNAVAILABLE,
    MSG_FEATURE_NOT_AVAILABLE,
    MSG_UNEXPECTED_ERROR,
)
from modulo.api.db_error_handling import handle_db_errors, raise_session_contract_error
from modulo.api.dependencies import get_db_session, require_permission
from modulo.api.models.team_visibility import TeamVisibilityMixin
from modulo.api.team_scope import validate_owner_team_for_create, validate_team_transition_for_update
from modulo.auth.dependencies import get_current_tenant_user
from modulo.auth.jwt import TenantPrincipal
from modulo.core.audit_coverage import audited
from modulo.core.mcp.tool_pinning import (
    ToolPinningError,
    apply_tool_observation,
    compute_tools_hash,
)
from modulo.db.crud.mcp_server import (
    create_mcp_server,
    get_mcp_server,
    list_mcp_servers,
    soft_delete_mcp_server,
    update_mcp_server,
)
from modulo.db.models.mcp_server import APPROVAL_STATUS_APPROVED
from modulo.db.rls import set_rls_org, set_rls_user_context
from modulo.settings import Settings, get_settings

_CODE_MCP_SERVERS_LIST = "mcp_servers.list_mcp_servers_endpoint"
_CODE_MCP_SERVERS_CREATE = "mcp_servers.create_mcp_server_endpoint"
_CODE_MCP_SERVERS_GET = "mcp_servers.get_mcp_server_endpoint"
_CODE_MCP_SERVERS_UPDATE = "mcp_servers.update_mcp_server_endpoint"
_CODE_MCP_SERVERS_DELETE = "mcp_servers.delete_mcp_server_endpoint"
_CODE_MCP_SERVERS_OBSERVE = "mcp_servers.observe_tools_endpoint"
_CODE_MCP_SERVERS_APPROVE = "mcp_servers.approve_tools_endpoint"

_PERM_LIST = "mcp_server.list"
_PERM_CREATE = "mcp_server.create"
_PERM_UPDATE = "mcp_server.update"
_PERM_DELETE = "mcp_server.delete"
_PERM_APPROVE = "mcp_server.approve"

_MSG_NOT_FOUND = "MCP server not found"

_log = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/mcp-servers", tags=["mcp_servers"])

_Transport = Literal["stdio", "sse", "http", "websocket"]


class McpServerCreate(TeamVisibilityMixin):
    name: str = Field(min_length=1, max_length=255)
    transport: _Transport
    endpoint: str | None = Field(default=None, max_length=2000)
    command: str | None = Field(default=None, max_length=2000)
    declared_scopes: list[str] = Field(default_factory=list)
    # A literal credential to be encrypted at rest; never echoed back.
    credentials: str | None = Field(default=None, min_length=1)
    visibility: str = Field(default="org", pattern=r"^(org|team)$")


class McpServerUpdate(TeamVisibilityMixin):
    name: str | None = Field(default=None, min_length=1, max_length=255)
    transport: _Transport | None = None
    endpoint: str | None = Field(default=None, max_length=2000)
    command: str | None = Field(default=None, max_length=2000)
    declared_scopes: list[str] | None = None
    # Omitted/None leaves the stored credential untouched (never nulled by a
    # partial update that does not carry a new secret).
    credentials: str | None = Field(default=None, min_length=1)
    visibility: str | None = Field(default=None, pattern=r"^(org|team)$")


class McpServerToolsObservation(BaseModel):
    """A reported MCP tool-definition set (each entry a JSON object with a name)."""

    tools: list[dict[str, Any]] = Field(default_factory=list)


class McpServerResponse(BaseModel):
    id: uuid.UUID
    organisation_id: uuid.UUID
    name: str
    transport: str
    endpoint: str | None = None
    command: str | None = None
    declared_scopes: list[str]
    owner_account_id: uuid.UUID | None = None
    has_credentials: bool
    tools_json: list[dict[str, Any]]
    tools_hash: str | None = None
    approval_status: str
    approved_by: uuid.UUID | None = None
    approved_at: datetime | None = None
    approved_hash: str | None = None
    last_observed_at: datetime | None = None
    visibility: str
    owner_team_id: uuid.UUID | None = None
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}


class McpServerListResponse(BaseModel):
    items: list[McpServerResponse]
    total: int
    page: int
    page_size: int


class McpServerObservationResponse(BaseModel):
    server_id: uuid.UUID
    tools_hash: str
    approval_status: str
    drift_detected: bool
    newly_drifted: bool
    previous_tools_hash: str | None = None


def _to_response(server: Any) -> McpServerResponse:
    return McpServerResponse(
        id=server.id,
        organisation_id=server.organisation_id,
        name=server.name,
        transport=server.transport,
        endpoint=server.endpoint,
        command=server.command,
        declared_scopes=list(server.declared_scopes or []),
        owner_account_id=server.owner_account_id,
        has_credentials=bool(server.credentials_ciphertext),
        tools_json=list(server.tools_json or []),
        tools_hash=server.tools_hash,
        approval_status=server.approval_status,
        approved_by=server.approved_by,
        approved_at=server.approved_at,
        approved_hash=server.approved_hash,
        last_observed_at=server.last_observed_at,
        visibility=server.visibility,
        owner_team_id=server.owner_team_id,
        created_at=server.created_at,
        updated_at=server.updated_at,
    )


def _encrypt_credentials(credentials: str, fernet_key: str) -> bytes:
    return Fernet(fernet_key.encode()).encrypt(credentials.encode())


@router.get("")
@handle_db_errors(_CODE_MCP_SERVERS_LIST)
async def list_mcp_servers_endpoint(
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
    session: AsyncSession = Depends(get_db_session),
    principal: TenantPrincipal = require_permission(_PERM_LIST),
) -> McpServerListResponse:
    try:
        async with session.begin():
            await set_rls_org(session, principal.organisation_id)
            await set_rls_user_context(session, principal.account_id, principal.org_role)
            result = await list_mcp_servers(
                session, organisation_id=principal.organisation_id, page=page, page_size=page_size
            )
    except ProgrammingError as exc:
        _log.exception(_CODE_MCP_SERVERS_LIST)
        raise HTTPException(status_code=status.HTTP_501_NOT_IMPLEMENTED, detail=MSG_FEATURE_NOT_AVAILABLE) from exc
    except SQLAlchemyError as exc:
        raise_session_contract_error(exc, _CODE_MCP_SERVERS_LIST)
        _log.exception(_CODE_MCP_SERVERS_LIST)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=MSG_DATABASE_TEMPORARILY_UNAVAILABLE,
        ) from exc
    except HTTPException:
        raise
    except Exception:
        _log.exception("mcp_servers.list")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=MSG_UNEXPECTED_ERROR,
        ) from None
    return McpServerListResponse(
        items=[_to_response(s) for s in result.items],
        total=result.total,
        page=result.page,
        page_size=result.page_size,
    )


@router.post(
    "",
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(audited("mcp_server_created", "mcp_server", principal_dep=get_current_tenant_user))],
)
@handle_db_errors(_CODE_MCP_SERVERS_CREATE)
async def create_mcp_server_endpoint(
    req: McpServerCreate,
    session: AsyncSession = Depends(get_db_session),
    principal: TenantPrincipal = require_permission(_PERM_CREATE),
    settings: Settings = Depends(get_settings),
) -> McpServerResponse:
    ciphertext = _encrypt_credentials(req.credentials, settings.fernet_key) if req.credentials else None
    try:
        async with session.begin():
            await set_rls_org(session, principal.organisation_id)
            await set_rls_user_context(session, principal.account_id, principal.org_role)
            await validate_owner_team_for_create(session, principal, req.owner_team_id)
            server = await create_mcp_server(
                session,
                org_id=principal.organisation_id,
                name=req.name,
                transport=req.transport,
                endpoint=req.endpoint,
                command=req.command,
                declared_scopes=req.declared_scopes,
                owner_account_id=principal.account_id,
                credentials_ciphertext=ciphertext,
                visibility=req.visibility,
                owner_team_id=req.owner_team_id,
            )
    except ProgrammingError as exc:
        _log.exception(_CODE_MCP_SERVERS_CREATE)
        raise HTTPException(status_code=status.HTTP_501_NOT_IMPLEMENTED, detail=MSG_FEATURE_NOT_AVAILABLE) from exc
    except IntegrityError as exc:
        _log.exception(_CODE_MCP_SERVERS_CREATE)
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="MCP server cannot be created — a constraint violation occurred.",
        ) from exc
    except SQLAlchemyError as exc:
        raise_session_contract_error(exc, _CODE_MCP_SERVERS_CREATE)
        _log.exception(_CODE_MCP_SERVERS_CREATE)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=MSG_DATABASE_TEMPORARILY_UNAVAILABLE,
        ) from exc
    except HTTPException:
        raise
    except Exception:
        _log.exception("mcp_servers.create")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=MSG_UNEXPECTED_ERROR,
        ) from None
    return _to_response(server)


@router.get("/{server_id}")
@handle_db_errors(_CODE_MCP_SERVERS_GET)
async def get_mcp_server_endpoint(
    server_id: uuid.UUID,
    session: AsyncSession = Depends(get_db_session),
    principal: TenantPrincipal = require_permission(_PERM_LIST),
) -> McpServerResponse:
    try:
        async with session.begin():
            await set_rls_org(session, principal.organisation_id)
            await set_rls_user_context(session, principal.account_id, principal.org_role)
            server = await get_mcp_server(session, server_id, organisation_id=principal.organisation_id)
    except ProgrammingError as exc:
        _log.exception(_CODE_MCP_SERVERS_GET)
        raise HTTPException(status_code=status.HTTP_501_NOT_IMPLEMENTED, detail=MSG_FEATURE_NOT_AVAILABLE) from exc
    except SQLAlchemyError as exc:
        raise_session_contract_error(exc, _CODE_MCP_SERVERS_GET)
        _log.exception(_CODE_MCP_SERVERS_GET)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=MSG_DATABASE_TEMPORARILY_UNAVAILABLE,
        ) from exc
    except HTTPException:
        raise
    except Exception:
        _log.exception("mcp_servers.get")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=MSG_UNEXPECTED_ERROR,
        ) from None
    if server is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=_MSG_NOT_FOUND)
    return _to_response(server)


@router.patch(
    "/{server_id}",
    dependencies=[Depends(audited("mcp_server_updated", "mcp_server", principal_dep=get_current_tenant_user))],
)
@handle_db_errors(_CODE_MCP_SERVERS_UPDATE)
async def update_mcp_server_endpoint(
    server_id: uuid.UUID,
    req: McpServerUpdate,
    session: AsyncSession = Depends(get_db_session),
    principal: TenantPrincipal = require_permission(_PERM_UPDATE),
    settings: Settings = Depends(get_settings),
) -> McpServerResponse:
    updates: dict[str, Any] = req.model_dump(exclude_unset=True)
    new_credentials: str | None = updates.pop("credentials", None)
    if new_credentials is not None:
        updates["credentials_ciphertext"] = _encrypt_credentials(new_credentials, settings.fernet_key)
    try:
        async with session.begin():
            await set_rls_org(session, principal.organisation_id)
            await set_rls_user_context(session, principal.account_id, principal.org_role)
            existing = await get_mcp_server(session, server_id, organisation_id=principal.organisation_id)
            if existing is None:
                raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=_MSG_NOT_FOUND)
            if "visibility" in updates or "owner_team_id" in updates:
                await validate_team_transition_for_update(
                    session,
                    principal,
                    current_owner_team_id=existing.owner_team_id,
                    current_visibility=existing.visibility,
                    new_owner_team_id=updates.get("owner_team_id", existing.owner_team_id),
                    new_visibility=updates.get("visibility", existing.visibility),
                )
            server = await update_mcp_server(session, server_id, updates, organisation_id=principal.organisation_id)
    except ProgrammingError as exc:
        _log.exception(_CODE_MCP_SERVERS_UPDATE)
        raise HTTPException(status_code=status.HTTP_501_NOT_IMPLEMENTED, detail=MSG_FEATURE_NOT_AVAILABLE) from exc
    except IntegrityError as exc:
        _log.exception(_CODE_MCP_SERVERS_UPDATE)
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="MCP server cannot be updated — a constraint violation occurred.",
        ) from exc
    except SQLAlchemyError as exc:
        raise_session_contract_error(exc, _CODE_MCP_SERVERS_UPDATE)
        _log.exception(_CODE_MCP_SERVERS_UPDATE)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=MSG_DATABASE_TEMPORARILY_UNAVAILABLE,
        ) from exc
    except HTTPException:
        raise
    except Exception:
        _log.exception("mcp_servers.update")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=MSG_UNEXPECTED_ERROR,
        ) from None
    if server is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=_MSG_NOT_FOUND)
    return _to_response(server)


@router.delete(
    "/{server_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    dependencies=[Depends(audited("mcp_server_deleted", "mcp_server", principal_dep=get_current_tenant_user))],
)
@handle_db_errors(_CODE_MCP_SERVERS_DELETE)
async def delete_mcp_server_endpoint(
    server_id: uuid.UUID,
    session: AsyncSession = Depends(get_db_session),
    principal: TenantPrincipal = require_permission(_PERM_DELETE),
) -> None:
    try:
        async with session.begin():
            await set_rls_org(session, principal.organisation_id)
            await set_rls_user_context(session, principal.account_id, principal.org_role)
            deleted = await soft_delete_mcp_server(session, server_id, organisation_id=principal.organisation_id)
    except ProgrammingError as exc:
        _log.exception(_CODE_MCP_SERVERS_DELETE)
        raise HTTPException(status_code=status.HTTP_501_NOT_IMPLEMENTED, detail=MSG_FEATURE_NOT_AVAILABLE) from exc
    except SQLAlchemyError as exc:
        raise_session_contract_error(exc, _CODE_MCP_SERVERS_DELETE)
        _log.exception(_CODE_MCP_SERVERS_DELETE)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=MSG_DATABASE_TEMPORARILY_UNAVAILABLE,
        ) from exc
    except HTTPException:
        raise
    except Exception:
        _log.exception("mcp_servers.delete")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=MSG_UNEXPECTED_ERROR,
        ) from None
    if not deleted:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=_MSG_NOT_FOUND)


@router.post(
    "/{server_id}/observe-tools",
    dependencies=[Depends(audited("mcp_server_tools_observed", "mcp_server", principal_dep=get_current_tenant_user))],
)
@handle_db_errors(_CODE_MCP_SERVERS_OBSERVE)
async def observe_tools_endpoint(
    server_id: uuid.UUID,
    req: McpServerToolsObservation,
    session: AsyncSession = Depends(get_db_session),
    principal: TenantPrincipal = require_permission(_PERM_UPDATE),
) -> McpServerObservationResponse:
    """Record the server's observed tool definitions and apply the drift decision."""
    try:
        async with session.begin():
            await set_rls_org(session, principal.organisation_id)
            await set_rls_user_context(session, principal.account_id, principal.org_role)
            server = await get_mcp_server(session, server_id, organisation_id=principal.organisation_id)
            if server is None:
                raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=_MSG_NOT_FOUND)
            result = await apply_tool_observation(
                session,
                server=server,
                org_id=principal.organisation_id,
                tools=req.tools,
                actor_user_id=principal.account_id,
            )
    except ToolPinningError as exc:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(exc)) from None
    except ProgrammingError as exc:
        _log.exception(_CODE_MCP_SERVERS_OBSERVE)
        raise HTTPException(status_code=status.HTTP_501_NOT_IMPLEMENTED, detail=MSG_FEATURE_NOT_AVAILABLE) from exc
    except SQLAlchemyError as exc:
        raise_session_contract_error(exc, _CODE_MCP_SERVERS_OBSERVE)
        _log.exception(_CODE_MCP_SERVERS_OBSERVE)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=MSG_DATABASE_TEMPORARILY_UNAVAILABLE,
        ) from exc
    except HTTPException:
        raise
    except Exception:
        _log.exception("mcp_servers.observe_tools")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=MSG_UNEXPECTED_ERROR,
        ) from None
    return McpServerObservationResponse(
        server_id=server.id,
        tools_hash=result.tools_hash,
        approval_status=result.approval_status,
        drift_detected=result.drifted,
        newly_drifted=result.newly_drifted,
        previous_tools_hash=result.previous_tools_hash,
    )


@router.post(
    "/{server_id}/approve-tools",
    dependencies=[Depends(audited("mcp_server_tools_approved", "mcp_server", principal_dep=get_current_tenant_user))],
)
@handle_db_errors(_CODE_MCP_SERVERS_APPROVE)
async def approve_tools_endpoint(
    server_id: uuid.UUID,
    session: AsyncSession = Depends(get_db_session),
    principal: TenantPrincipal = require_permission(_PERM_APPROVE),
) -> McpServerResponse:
    """Approve the CURRENTLY observed tool set, pinning its canonical hash."""
    try:
        async with session.begin():
            await set_rls_org(session, principal.organisation_id)
            await set_rls_user_context(session, principal.account_id, principal.org_role)
            server = await get_mcp_server(session, server_id, organisation_id=principal.organisation_id)
            if server is None:
                raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=_MSG_NOT_FOUND)
            tools = list(server.tools_json or [])
            if not tools:
                raise HTTPException(
                    status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                    detail="Cannot approve: no tool definitions have been observed for this server yet.",
                )
            approved_hash = compute_tools_hash(tools)
            server = await update_mcp_server(
                session,
                server_id,
                {
                    "approval_status": APPROVAL_STATUS_APPROVED,
                    "approved_by": principal.account_id,
                    "approved_at": datetime.now(UTC),
                    "approved_hash": approved_hash,
                },
                organisation_id=principal.organisation_id,
            )
    except ToolPinningError as exc:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(exc)) from None
    except ProgrammingError as exc:
        _log.exception(_CODE_MCP_SERVERS_APPROVE)
        raise HTTPException(status_code=status.HTTP_501_NOT_IMPLEMENTED, detail=MSG_FEATURE_NOT_AVAILABLE) from exc
    except SQLAlchemyError as exc:
        raise_session_contract_error(exc, _CODE_MCP_SERVERS_APPROVE)
        _log.exception(_CODE_MCP_SERVERS_APPROVE)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=MSG_DATABASE_TEMPORARILY_UNAVAILABLE,
        ) from exc
    except HTTPException:
        raise
    except Exception:
        _log.exception("mcp_servers.approve_tools")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=MSG_UNEXPECTED_ERROR,
        ) from None
    if server is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=_MSG_NOT_FOUND)
    return _to_response(server)
