"""External-agent registry REST API (FAR-1692 S1).

CRUD over the ``external_agents`` inventory — non-Modulo agents (Claude Code,
Cursor, opencode, ...). This is a record, not enforcement (ADR 055): the routes
never dispatch or police the agent; they maintain the inventory that later
ingestion slices (S3/S4) and policies read from.

Org-scoped (RLS) with team visibility; the DB ``rls_team_isolation`` policy is
the team boundary (no request-time team gate is wired — the core team-scoped
tables' DB-RLS-only posture, FAR-1514).

TODO(FAR-1692 S3/S4): the model/CRUD carry ``first_seen``/``last_seen`` but no
create/update request field sets them yet — they are populated by the ingestion
slice, not manual CRUD.
"""

import logging
import uuid
from datetime import datetime
from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field
from sqlalchemy.exc import IntegrityError, ProgrammingError, SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from modulo.api.constants import (
    MSG_DATABASE_TEMPORARILY_UNAVAILABLE,
    MSG_FEATURE_NOT_AVAILABLE,
    MSG_RESOURCE_ALREADY_EXISTS,
    MSG_UNEXPECTED_ERROR,
)
from modulo.api.db_error_handling import handle_db_errors, raise_session_contract_error
from modulo.api.dependencies import get_db_session, require_permission
from modulo.api.models.team_visibility import TeamVisibilityMixin
from modulo.api.team_scope import validate_owner_team_for_create, validate_team_transition_for_update
from modulo.auth.dependencies import get_current_tenant_user
from modulo.auth.jwt import TenantPrincipal
from modulo.core.audit_coverage import audited
from modulo.db.crud.external_agent import (
    create_external_agent,
    get_external_agent,
    list_external_agents,
    soft_delete_external_agent,
    update_external_agent,
)
from modulo.db.rls import set_rls_org, set_rls_user_context

_CODE_EXTERNAL_AGENTS_LIST = "external_agents.list_external_agents_endpoint"
_CODE_EXTERNAL_AGENTS_CREATE = "external_agents.create_external_agent_endpoint"
_CODE_EXTERNAL_AGENTS_GET = "external_agents.get_external_agent_endpoint"
_CODE_EXTERNAL_AGENTS_UPDATE = "external_agents.update_external_agent_endpoint"
_CODE_EXTERNAL_AGENTS_DELETE = "external_agents.delete_external_agent_endpoint"

_PERM_LIST = "external_agent.list"
_PERM_CREATE = "external_agent.create"
_PERM_UPDATE = "external_agent.update"
_PERM_DELETE = "external_agent.delete"

_MSG_NOT_FOUND = "External agent not found"

_log = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/external-agents", tags=["external_agents"])


class ExternalAgentCreate(TeamVisibilityMixin):
    name: str = Field(min_length=1, max_length=255)
    runtime_kind: str = Field(default="other", pattern=r"^(claude-code|cursor|opencode|other)$")
    host_descriptor: str | None = Field(default=None, max_length=500)
    status: Literal["active", "inactive", "unknown"] = "active"
    discovery_provenance: Literal["manual", "mcp", "webhook", "forge"] = "manual"
    visibility: str = Field(default="org", pattern=r"^(org|team)$")


class ExternalAgentUpdate(TeamVisibilityMixin):
    name: str | None = Field(default=None, min_length=1, max_length=255)
    runtime_kind: str | None = Field(default=None, pattern=r"^(claude-code|cursor|opencode|other)$")
    host_descriptor: str | None = Field(default=None, max_length=500)
    status: Literal["active", "inactive", "unknown"] | None = None
    discovery_provenance: Literal["manual", "mcp", "webhook", "forge"] | None = None
    visibility: str | None = Field(default=None, pattern=r"^(org|team)$")


class ExternalAgentResponse(BaseModel):
    id: uuid.UUID
    organisation_id: uuid.UUID
    name: str
    runtime_kind: str
    host_descriptor: str | None = None
    status: str
    discovery_provenance: str
    first_seen: datetime | None = None
    last_seen: datetime | None = None
    visibility: str
    owner_team_id: uuid.UUID | None = None
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}


class ExternalAgentListResponse(BaseModel):
    items: list[ExternalAgentResponse]
    total: int
    page: int
    page_size: int


@router.get("")
@handle_db_errors(_CODE_EXTERNAL_AGENTS_LIST)
async def list_external_agents_endpoint(
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
    session: AsyncSession = Depends(get_db_session),
    principal: TenantPrincipal = require_permission(_PERM_LIST),
) -> ExternalAgentListResponse:
    try:
        async with session.begin():
            await set_rls_org(session, principal.organisation_id)
            await set_rls_user_context(session, principal.account_id, principal.org_role)
            result = await list_external_agents(
                session,
                organisation_id=principal.organisation_id,
                page=page,
                page_size=page_size,
            )
    except ProgrammingError as exc:
        _log.exception(_CODE_EXTERNAL_AGENTS_LIST)
        raise HTTPException(status_code=status.HTTP_501_NOT_IMPLEMENTED, detail=MSG_FEATURE_NOT_AVAILABLE) from exc
    except SQLAlchemyError as exc:
        raise_session_contract_error(exc, _CODE_EXTERNAL_AGENTS_LIST)
        _log.exception(_CODE_EXTERNAL_AGENTS_LIST)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=MSG_DATABASE_TEMPORARILY_UNAVAILABLE,
        ) from exc
    except HTTPException:
        raise
    except Exception:
        _log.exception("external_agents.list")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=MSG_UNEXPECTED_ERROR,
        ) from None
    return ExternalAgentListResponse(
        items=[ExternalAgentResponse.model_validate(a) for a in result.items],
        total=result.total,
        page=result.page,
        page_size=result.page_size,
    )


@router.post(
    "",
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(audited("external_agent_created", "external_agent", principal_dep=get_current_tenant_user))],
)
@handle_db_errors(_CODE_EXTERNAL_AGENTS_CREATE)
async def create_external_agent_endpoint(
    req: ExternalAgentCreate,
    session: AsyncSession = Depends(get_db_session),
    principal: TenantPrincipal = require_permission(_PERM_CREATE),
) -> ExternalAgentResponse:
    try:
        async with session.begin():
            await set_rls_org(session, principal.organisation_id)
            await set_rls_user_context(session, principal.account_id, principal.org_role)
            await validate_owner_team_for_create(session, principal, req.owner_team_id)
            agent = await create_external_agent(
                session,
                org_id=principal.organisation_id,
                name=req.name,
                runtime_kind=req.runtime_kind,
                host_descriptor=req.host_descriptor,
                status=req.status,
                discovery_provenance=req.discovery_provenance,
                visibility=req.visibility,
                owner_team_id=req.owner_team_id,
            )
    except ProgrammingError as exc:
        _log.exception(_CODE_EXTERNAL_AGENTS_CREATE)
        raise HTTPException(status_code=status.HTTP_501_NOT_IMPLEMENTED, detail=MSG_FEATURE_NOT_AVAILABLE) from exc
    except IntegrityError as exc:
        _log.exception(_CODE_EXTERNAL_AGENTS_CREATE)
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=MSG_RESOURCE_ALREADY_EXISTS) from exc
    except SQLAlchemyError as exc:
        raise_session_contract_error(exc, _CODE_EXTERNAL_AGENTS_CREATE)
        _log.exception(_CODE_EXTERNAL_AGENTS_CREATE)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=MSG_DATABASE_TEMPORARILY_UNAVAILABLE,
        ) from exc
    except HTTPException:
        raise
    except Exception:
        _log.exception("external_agents.create")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=MSG_UNEXPECTED_ERROR,
        ) from None
    return ExternalAgentResponse.model_validate(agent)


@router.get("/{agent_id}")
@handle_db_errors(_CODE_EXTERNAL_AGENTS_GET)
async def get_external_agent_endpoint(
    agent_id: uuid.UUID,
    session: AsyncSession = Depends(get_db_session),
    principal: TenantPrincipal = require_permission(_PERM_LIST),
) -> ExternalAgentResponse:
    try:
        async with session.begin():
            await set_rls_org(session, principal.organisation_id)
            await set_rls_user_context(session, principal.account_id, principal.org_role)
            agent = await get_external_agent(session, agent_id, organisation_id=principal.organisation_id)
    except ProgrammingError as exc:
        _log.exception(_CODE_EXTERNAL_AGENTS_GET)
        raise HTTPException(status_code=status.HTTP_501_NOT_IMPLEMENTED, detail=MSG_FEATURE_NOT_AVAILABLE) from exc
    except SQLAlchemyError as exc:
        raise_session_contract_error(exc, _CODE_EXTERNAL_AGENTS_GET)
        _log.exception(_CODE_EXTERNAL_AGENTS_GET)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=MSG_DATABASE_TEMPORARILY_UNAVAILABLE,
        ) from exc
    except HTTPException:
        raise
    except Exception:
        _log.exception("external_agents.get")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=MSG_UNEXPECTED_ERROR,
        ) from None
    if agent is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=_MSG_NOT_FOUND)
    return ExternalAgentResponse.model_validate(agent)


@router.patch(
    "/{agent_id}",
    dependencies=[Depends(audited("external_agent_updated", "external_agent", principal_dep=get_current_tenant_user))],
)
@handle_db_errors(_CODE_EXTERNAL_AGENTS_UPDATE)
async def update_external_agent_endpoint(
    agent_id: uuid.UUID,
    req: ExternalAgentUpdate,
    session: AsyncSession = Depends(get_db_session),
    principal: TenantPrincipal = require_permission(_PERM_UPDATE),
) -> ExternalAgentResponse:
    updates: dict[str, Any] = req.model_dump(exclude_unset=True)
    try:
        async with session.begin():
            await set_rls_org(session, principal.organisation_id)
            await set_rls_user_context(session, principal.account_id, principal.org_role)
            existing = await get_external_agent(session, agent_id, organisation_id=principal.organisation_id)
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
            agent = await update_external_agent(session, agent_id, updates, organisation_id=principal.organisation_id)
    except ProgrammingError as exc:
        _log.exception(_CODE_EXTERNAL_AGENTS_UPDATE)
        raise HTTPException(status_code=status.HTTP_501_NOT_IMPLEMENTED, detail=MSG_FEATURE_NOT_AVAILABLE) from exc
    except IntegrityError as exc:
        _log.exception(_CODE_EXTERNAL_AGENTS_UPDATE)
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=MSG_RESOURCE_ALREADY_EXISTS) from exc
    except SQLAlchemyError as exc:
        raise_session_contract_error(exc, _CODE_EXTERNAL_AGENTS_UPDATE)
        _log.exception(_CODE_EXTERNAL_AGENTS_UPDATE)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=MSG_DATABASE_TEMPORARILY_UNAVAILABLE,
        ) from exc
    except HTTPException:
        raise
    except Exception:
        _log.exception("external_agents.update")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=MSG_UNEXPECTED_ERROR,
        ) from None
    if agent is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=_MSG_NOT_FOUND)
    return ExternalAgentResponse.model_validate(agent)


@router.delete(
    "/{agent_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    dependencies=[Depends(audited("external_agent_deleted", "external_agent", principal_dep=get_current_tenant_user))],
)
@handle_db_errors(_CODE_EXTERNAL_AGENTS_DELETE)
async def delete_external_agent_endpoint(
    agent_id: uuid.UUID,
    session: AsyncSession = Depends(get_db_session),
    principal: TenantPrincipal = require_permission(_PERM_DELETE),
) -> None:
    try:
        async with session.begin():
            await set_rls_org(session, principal.organisation_id)
            await set_rls_user_context(session, principal.account_id, principal.org_role)
            deleted = await soft_delete_external_agent(session, agent_id, organisation_id=principal.organisation_id)
    except ProgrammingError as exc:
        _log.exception(_CODE_EXTERNAL_AGENTS_DELETE)
        raise HTTPException(status_code=status.HTTP_501_NOT_IMPLEMENTED, detail=MSG_FEATURE_NOT_AVAILABLE) from exc
    except SQLAlchemyError as exc:
        raise_session_contract_error(exc, _CODE_EXTERNAL_AGENTS_DELETE)
        _log.exception(_CODE_EXTERNAL_AGENTS_DELETE)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=MSG_DATABASE_TEMPORARILY_UNAVAILABLE,
        ) from exc
    except HTTPException:
        raise
    except Exception:
        _log.exception("external_agents.delete")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=MSG_UNEXPECTED_ERROR,
        ) from None
    if not deleted:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=_MSG_NOT_FOUND)
