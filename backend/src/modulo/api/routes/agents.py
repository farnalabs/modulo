"""Agent CRUD REST API."""

import json
import logging
import uuid
from datetime import datetime
from typing import Any, ClassVar, cast

from fastapi import APIRouter, Depends, HTTPException, Query, status
from langchain_core.messages import BaseMessage
from pydantic import BaseModel, Field, model_validator
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError, ProgrammingError, SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from modulo.api.constants import MSG_FEATURE_NOT_AVAILABLE, MSG_RESOURCE_ALREADY_EXISTS
from modulo.api.db_error_handling import handle_db_errors
from modulo.api.dependencies import get_db_session, require_permission
from modulo.auth.jwt import TenantPrincipal
from modulo.core.line_diff import iter_line_diffs
from modulo.core.prompt_optimizer import OptimizationFailedError, PromptOptimizer
from modulo.core.secrets_backend import create_secrets_backend
from modulo.db.crud.agent import (
    add_prompt_version,
    create_agent,
    delete_agent,
    get_agent,
    get_eval_results_with_defs,
    get_prompt_version,
    list_agents,
    rollback_prompt_version,
    update_agent,
)
from modulo.db.models.model_backend import ModelBackend
from modulo.db.rls import set_rls_org, set_rls_user_context
from modulo.settings import get_settings
from modulo.util import sanitise_log_value as _sanitise_log_value

_CODE_AGENT_LIST = "agent.list"
_MSG_DATABASE_OPERATION_FAILED = "Database operation failed"
_MSG_DATABASE_OPERATION_FAILED_PLEASE = "Database operation failed. Please try again."
_MSG_UNEXPECTED_ERROR_OCCURRED_PLEASE = "An unexpected error occurred. Please try again."
_MSG_AGENT_NOT_FOUND = "Agent not found"
_CODE_AGENT_UPDATE = "agent.update"
_CODE_AGENTS_UPDATE_AGENT_ENDPOINT = "agents.update_agent_endpoint"
_CODE_AGENTS_OPTIMIZE_PROMPT = "agents.optimize_prompt"


_log = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/agents", tags=["agents"])


def _resolve_prompt_template(agent: Any, version: str) -> str | None:
    """Resolve a prompt version label to its template.

    ``"current"`` resolves to the agent's active ``prompt_template``. Any other
    label must exist in ``prompt_version_history``; an unknown label returns
    ``None``. Empty templates resolve to ``""`` (kept distinct from unknown).
    """
    if version == "current":
        return cast("str | None", agent.prompt_template)
    for entry in agent.prompt_version_history or []:
        if entry.get("version") == version:
            tpl = cast("str | None", entry.get("template"))
            return tpl or ""
    return None


class AgentCreate(BaseModel):
    name: str = Field(min_length=1)
    description: str | None = None
    is_executable: bool = True
    input_schema_id: uuid.UUID
    input_schema_version: str | None = None
    output_schema_id: uuid.UUID
    output_schema_version: str | None = None
    prompt_template: str = Field(min_length=1)
    model_backend_id: uuid.UUID
    connector_type_refs: ClassVar[list[dict[str, Any]]] = []
    evals: ClassVar[list[dict[str, Any]]] = []
    retry_policy: ClassVar[dict[str, Any]] = {}
    token_budget: int | None = Field(default=None, ge=0)
    max_input_length: int | None = Field(default=None, ge=0)
    library_id: uuid.UUID | None = None
    prompt_always_visible: bool = False
    required_environment_capabilities: list[str]
    template_id: str | None
    agent_command: str | None = Field(default=None)
    agent_commands: list[str] | None = Field(default=None)

    @model_validator(mode="after")
    def validate_command_fields(self) -> "AgentCreate":
        if self.agent_command is not None and self.agent_commands:
            raise ValueError("Cannot specify both 'command' and 'commands' — use 'commands' as an array")
        return self


class AgentUpdate(BaseModel):
    name: str | None = None
    description: str | None = None
    is_executable: bool | None = None
    prompt_template: str | None = None
    model_backend_id: uuid.UUID | None = None
    connector_type_refs: list[dict[str, Any]] | None = None
    evals: list[dict[str, Any]] | None = None
    retry_policy: dict[str, Any] | None = None
    token_budget: int | None = Field(default=None, ge=0)
    max_input_length: int | None = Field(default=None, ge=0)
    prompt_always_visible: bool | None = None
    required_environment_capabilities: list[str]
    template_id: str | None
    agent_command: str | None = None
    agent_commands: list[str] | None = Field(default=None)

    @model_validator(mode="after")
    def validate_command_fields(self) -> "AgentUpdate":
        if self.agent_command is not None and self.agent_commands:
            raise ValueError("Cannot specify both 'command' and 'commands' — use 'commands' as an array")
        return self


class AgentResponse(BaseModel):
    id: uuid.UUID
    organisation_id: uuid.UUID
    name: str
    description: str | None
    is_executable: bool
    input_schema_id: uuid.UUID | None
    input_schema_version: str | None
    output_schema_id: uuid.UUID | None
    output_schema_version: str | None
    prompt_template: str
    prompt_version_history: list[dict[str, Any]]
    model_backend_id: uuid.UUID | None
    connector_type_refs: list[dict[str, Any]]
    evals: list[dict[str, Any]] | None
    retry_policy: dict[str, Any]
    token_budget: int | None
    max_input_length: int | None
    library_id: uuid.UUID | None
    prompt_always_visible: bool
    required_environment_capabilities: list[str]
    template_id: str | None
    agent_command: str | None
    agent_commands: list[str] | None
    created_by: uuid.UUID = Field(validation_alias="account_id")
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}


class AgentListResponse(BaseModel):
    items: list[AgentResponse]
    total: int
    page: int
    page_size: int


class PromptOptimizeRequest(BaseModel):
    eval_result_ids: list[uuid.UUID] = Field(min_length=1)
    model_backend_id: uuid.UUID | None = None


class PromptOptimizeResponse(BaseModel):
    suggested_prompt: str
    rationale: str
    analysis: str
    version: str


class ApplyOptimizedPromptRequest(BaseModel):
    suggested_prompt: str = Field(min_length=1)
    rationale: str | None = None
    optimize_version: str | None = None
    eval_result_ids: list[uuid.UUID] | None = None


class PromptVersionListEntry(BaseModel):
    version: str
    created_at: str
    notes: str
    optimized_from: str | None = None
    eval_result_ids: ClassVar[list[str]] = []


class PromptVersionDetail(BaseModel):
    version: str
    template: str
    created_at: str
    notes: str
    optimized_from: str | None = None
    eval_result_ids: ClassVar[list[str]] = []


class PromptDiffRequest(BaseModel):
    version_a: str
    version_b: str


class DiffLine(BaseModel):
    type: str  # "added" | "removed" | "unchanged"
    content: str
    line_number_a: int | None = None
    line_number_b: int | None = None


class PromptDiffResponse(BaseModel):
    version_a: str
    version_b: str
    lines: list[DiffLine]


class PromptRollbackResponse(BaseModel):
    agent: AgentResponse
    message: str


def _validate_generic_agent(
    name: str,
    is_executable: bool,
    description: str | None,
    evals: list[dict[str, Any]],
    library_id: uuid.UUID | None,
) -> None:
    """Validate criteria for generic (non-library) agents.

    Library-sourced agents (those with a ``library_id``) inherit trust and
    documentation from their source — they bypass generic-agent checks.

    Generic user-defined agents are experimental per PRD §8.2 and must
    satisfy the following criteria before they can execute in a pipeline:
      - An executable generic agent MUST have a ``description`` so other
        pipeline authors can understand its purpose.
      - A non-executable agent (template or blueprint) MUST also have a
        ``description``, since it serves as documentation for future agents.
      - Executable generic agents with *novel schema pairs* (no matching
        library primitive) SHOULD define at least one eval for quality
        assurance.  In alpha this is a logged advisory; in production it
        becomes a hard requirement (see PRD §15 — "require eval rubric
        before production promotion").
    """
    if library_id is not None:
        return

    if is_executable and not description:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=(
                f"Generic agent '{name}' has no description. "
                "User-defined executable agents must include a description "
                "so that pipeline authors can understand the agent's purpose."
            ),
        )

    if not is_executable and not description:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=(
                f"Non-executable agent '{name}' has no description. "
                "Template and blueprint agents must include a description "
                "that documents the intended use of the agent."
            ),
        )

    if is_executable and not evals:
        _log.warning(
            "Generic executable agent '%s' has no eval definitions. "
            "Per PRD §8.2, generic agents are experimental and require "
            "an eval rubric before production promotion. "
            "Consider adding at least one eval before deploying this agent "
            "in a production pipeline.",
            _sanitise_log_value(name),
        )


@router.get("")
@handle_db_errors("agents.list_agents_endpoint")
async def list_agents_endpoint(
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
    session: AsyncSession = Depends(get_db_session),
    principal: TenantPrincipal = require_permission(_CODE_AGENT_LIST),
) -> AgentListResponse:
    try:
        async with session.begin():
            await set_rls_org(session, principal.organisation_id)
            result = await list_agents(session, page=page, page_size=page_size)
    except ProgrammingError:
        _log.exception("agents.list_agents_endpoint")
        raise HTTPException(
            status_code=status.HTTP_501_NOT_IMPLEMENTED,
            detail=MSG_FEATURE_NOT_AVAILABLE,
        ) from None
    except SQLAlchemyError:
        _log.exception(_MSG_DATABASE_OPERATION_FAILED)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=_MSG_DATABASE_OPERATION_FAILED_PLEASE,
        ) from None
    except HTTPException:
        raise
    except Exception:
        _log.exception("Unexpected error listing agents")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=_MSG_UNEXPECTED_ERROR_OCCURRED_PLEASE,
        ) from None
    return AgentListResponse(
        items=[AgentResponse.model_validate(a) for a in result.items],
        total=result.total,
        page=result.page,
        page_size=result.page_size,
    )


@router.post("", status_code=status.HTTP_201_CREATED)
@handle_db_errors("agents.create_agent_endpoint")
async def create_agent_endpoint(
    req: AgentCreate,
    session: AsyncSession = Depends(get_db_session),
    principal: TenantPrincipal = require_permission("agent.create"),
) -> AgentResponse:
    _validate_generic_agent(
        name=req.name,
        is_executable=req.is_executable,
        description=req.description,
        evals=req.evals,
        library_id=req.library_id,
    )
    try:
        async with session.begin():
            await set_rls_org(session, principal.organisation_id)
            input_ver = req.input_schema_version or "latest"
            output_ver = req.output_schema_version or "latest"
            agent = await create_agent(
                session,
                org_id=principal.organisation_id,
                name=req.name,
                account_id=principal.account_id,
                input_schema_id=req.input_schema_id,
                input_schema_version=input_ver,
                output_schema_id=req.output_schema_id,
                output_schema_version=output_ver,
                template_id=req.template_id,
                agent_command=req.agent_command,
                prompt_template=req.prompt_template,
                model_backend_id=req.model_backend_id,
                is_executable=req.is_executable,
                description=req.description,
                connector_type_refs=req.connector_type_refs,
                evals=req.evals,
                retry_policy=req.retry_policy,
                token_budget=req.token_budget,
                max_input_length=req.max_input_length,
                library_id=req.library_id,
                prompt_always_visible=req.prompt_always_visible,
                required_environment_capabilities=req.required_environment_capabilities,
            )
    except IntegrityError:
        _log.exception("agents.create_agent_endpoint")
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="Referenced schema version or model backend not found. Verify the IDs are correct.",
        ) from None
    except ProgrammingError:
        _log.exception("agents.create_agent_endpoint")
        raise HTTPException(
            status_code=status.HTTP_501_NOT_IMPLEMENTED,
            detail=MSG_FEATURE_NOT_AVAILABLE,
        ) from None
    except SQLAlchemyError:
        _log.exception("Database operation failed during agent creation")
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=_MSG_DATABASE_OPERATION_FAILED_PLEASE,
        ) from None
    except HTTPException:
        raise
    except Exception:
        _log.exception("Unexpected error creating agent")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=_MSG_UNEXPECTED_ERROR_OCCURRED_PLEASE,
        ) from None
    return AgentResponse.model_validate(agent)


@router.get("/{agent_id}")
@handle_db_errors("agents.get_agent_endpoint")
async def get_agent_endpoint(
    agent_id: uuid.UUID,
    session: AsyncSession = Depends(get_db_session),
    principal: TenantPrincipal = require_permission(_CODE_AGENT_LIST),
) -> AgentResponse:
    try:
        async with session.begin():
            await set_rls_org(session, principal.organisation_id)
            agent = await get_agent(session, agent_id)
    except ProgrammingError:
        _log.exception("agents.get_agent_endpoint")
        raise HTTPException(
            status_code=status.HTTP_501_NOT_IMPLEMENTED,
            detail=MSG_FEATURE_NOT_AVAILABLE,
        ) from None
    except SQLAlchemyError:
        _log.exception(_MSG_DATABASE_OPERATION_FAILED)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=_MSG_DATABASE_OPERATION_FAILED_PLEASE,
        ) from None
    except HTTPException:
        raise
    except Exception:
        _log.exception("Unexpected error getting agent")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=_MSG_UNEXPECTED_ERROR_OCCURRED_PLEASE,
        ) from None
    if agent is None or agent.organisation_id != principal.organisation_id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=_MSG_AGENT_NOT_FOUND)
    return AgentResponse.model_validate(agent)


@router.patch("/{agent_id}")
@handle_db_errors(_CODE_AGENTS_UPDATE_AGENT_ENDPOINT)
async def update_agent_endpoint(
    agent_id: uuid.UUID,
    req: AgentUpdate,
    session: AsyncSession = Depends(get_db_session),
    principal: TenantPrincipal = require_permission(_CODE_AGENT_UPDATE),
) -> AgentResponse:
    try:
        async with session.begin():
            await set_rls_org(session, principal.organisation_id)
            agent = await get_agent(session, agent_id)
            if agent is None or agent.organisation_id != principal.organisation_id:
                raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not found")
    except ProgrammingError:
        _log.exception(_CODE_AGENTS_UPDATE_AGENT_ENDPOINT)
        raise HTTPException(
            status_code=status.HTTP_501_NOT_IMPLEMENTED,
            detail=MSG_FEATURE_NOT_AVAILABLE,
        ) from None
    except SQLAlchemyError:
        _log.exception(_MSG_DATABASE_OPERATION_FAILED)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=_MSG_DATABASE_OPERATION_FAILED_PLEASE,
        ) from None
    except HTTPException:
        raise
    except Exception:
        _log.exception("Unexpected error updating agent")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=_MSG_UNEXPECTED_ERROR_OCCURRED_PLEASE,
        ) from None
    if agent is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=_MSG_AGENT_NOT_FOUND)

    merged_name = req.name if req.name is not None else agent.name
    merged_is_executable = req.is_executable if req.is_executable is not None else agent.is_executable
    merged_description = req.description if req.description is not None else agent.description
    merged_evals = req.evals if req.evals is not None else (agent.evals or [])
    _validate_generic_agent(
        name=merged_name,
        is_executable=merged_is_executable,
        description=merged_description,
        evals=merged_evals,
        library_id=agent.library_id,
    )

    updates = req.model_dump(exclude_unset=True)
    try:
        async with session.begin():
            await set_rls_org(session, principal.organisation_id)
            updated = await update_agent(session, agent_id, updates)
            if updated is None:
                raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=_MSG_AGENT_NOT_FOUND)
            await session.refresh(updated)
            response = AgentResponse.model_validate(updated)
    except IntegrityError:
        _log.exception(_CODE_AGENTS_UPDATE_AGENT_ENDPOINT)
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=MSG_RESOURCE_ALREADY_EXISTS,
        ) from None
    except ProgrammingError:
        _log.exception(_CODE_AGENTS_UPDATE_AGENT_ENDPOINT)
        raise HTTPException(
            status_code=status.HTTP_501_NOT_IMPLEMENTED,
            detail=MSG_FEATURE_NOT_AVAILABLE,
        ) from None
    except SQLAlchemyError:
        _log.exception(_MSG_DATABASE_OPERATION_FAILED)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=_MSG_DATABASE_OPERATION_FAILED_PLEASE,
        ) from None
    except HTTPException:
        raise
    except Exception:
        _log.exception("Unexpected error updating agent (write path)")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=_MSG_UNEXPECTED_ERROR_OCCURRED_PLEASE,
        ) from None
    return response


@router.post("/{agent_id}/prompts/{version}/optimize")
@handle_db_errors(_CODE_AGENTS_OPTIMIZE_PROMPT)
async def optimize_prompt(
    agent_id: uuid.UUID,
    version: str,
    req: PromptOptimizeRequest,
    session: AsyncSession = Depends(get_db_session),
    principal: TenantPrincipal = require_permission(_CODE_AGENT_UPDATE),
) -> PromptOptimizeResponse:
    if not req.eval_result_ids:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="At least one eval_result_id is required",
        )

    try:
        async with session.begin():
            await set_rls_org(session, principal.organisation_id)
            agent = await get_agent(session, agent_id)
    except ProgrammingError:
        _log.exception(_CODE_AGENTS_OPTIMIZE_PROMPT)
        raise HTTPException(
            status_code=status.HTTP_501_NOT_IMPLEMENTED,
            detail=MSG_FEATURE_NOT_AVAILABLE,
        ) from None
    except SQLAlchemyError:
        _log.exception(_MSG_DATABASE_OPERATION_FAILED)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=_MSG_DATABASE_OPERATION_FAILED_PLEASE,
        ) from None

    if agent is None or agent.organisation_id != principal.organisation_id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=_MSG_AGENT_NOT_FOUND)

    source_template = _resolve_prompt_template(agent, version)
    if source_template is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Prompt version {version} not found",
        )

    try:
        async with session.begin():
            await set_rls_org(session, principal.organisation_id)
            eval_results, eval_defs = await get_eval_results_with_defs(
                session, req.eval_result_ids, principal.organisation_id
            )
    except ProgrammingError:
        _log.exception(_CODE_AGENTS_OPTIMIZE_PROMPT)
        raise HTTPException(
            status_code=status.HTTP_501_NOT_IMPLEMENTED,
            detail=MSG_FEATURE_NOT_AVAILABLE,
        ) from None
    except SQLAlchemyError:
        _log.exception(_MSG_DATABASE_OPERATION_FAILED)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=_MSG_DATABASE_OPERATION_FAILED_PLEASE,
        ) from None

    if not eval_results:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="No eval results found for the given IDs",
        )

    backend_id = req.model_backend_id or agent.model_backend_id

    try:
        async with session.begin():
            await set_rls_org(session, principal.organisation_id)
            await set_rls_user_context(session, principal.account_id, principal.org_role)
            mb_result = await session.execute(
                select(ModelBackend).where(
                    ModelBackend.id == backend_id,
                    ModelBackend.organisation_id == principal.organisation_id,
                )
            )
    except ProgrammingError:
        _log.exception(_CODE_AGENTS_OPTIMIZE_PROMPT)
        raise HTTPException(
            status_code=status.HTTP_501_NOT_IMPLEMENTED,
            detail=MSG_FEATURE_NOT_AVAILABLE,
        ) from None
    except SQLAlchemyError:
        _log.exception(_MSG_DATABASE_OPERATION_FAILED)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=_MSG_DATABASE_OPERATION_FAILED_PLEASE,
        ) from None
    mb = mb_result.scalar_one_or_none()
    if mb is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Model backend not found",
        )

    settings = get_settings()
    secrets_backend = create_secrets_backend(fernet_key=settings.fernet_key, session=session)
    try:
        raw_creds = await secrets_backend.get_secret(str(mb.id))
    except KeyError:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to decrypt model backend credentials",
        ) from None

    creds: dict[str, Any] = json.loads(raw_creds)
    from modulo.core.model_backend_hub import _build_backend

    backend = _build_backend(mb.provider, mb.model_id, creds, mb.default_params or {})

    async def _llm_call(messages: list[BaseMessage]) -> str:
        reply = await backend.invoke(messages)
        content = reply.content
        if isinstance(content, list):
            texts = [p.get("text", "") if isinstance(p, dict) else str(p) for p in content]
            return "".join(texts)
        return str(content)

    try:
        optimizer = PromptOptimizer(_llm_call)
        result = await optimizer.optimize(source_template, eval_results, eval_defs)
    except OptimizationFailedError:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Prompt optimization failed: LLM call failed after retries",
        ) from None
    except HTTPException:
        raise
    except Exception:
        _log.exception("Unexpected error during prompt optimization")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Prompt optimization failed unexpectedly",
        ) from None

    history = list(agent.prompt_version_history or [])
    next_version = f"v{len(history) + 1}"

    return PromptOptimizeResponse(
        suggested_prompt=result.suggested_prompt,
        rationale=result.rationale,
        analysis=result.analysis,
        version=next_version,
    )


@router.post("/{agent_id}/prompts/{version}/apply")
@handle_db_errors("agents.apply_optimized_prompt")
async def apply_optimized_prompt(
    agent_id: uuid.UUID,
    version: str,
    req: ApplyOptimizedPromptRequest,
    session: AsyncSession = Depends(get_db_session),
    principal: TenantPrincipal = require_permission(_CODE_AGENT_UPDATE),
) -> AgentResponse:
    try:
        async with session.begin():
            await set_rls_org(session, principal.organisation_id)
            existing_agent = await get_agent(session, agent_id)
            if existing_agent is None or existing_agent.organisation_id != principal.organisation_id:
                raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not found")
            agent = await add_prompt_version(
                session,
                agent_id,
                new_template=req.suggested_prompt,
                notes=req.rationale,
                version_label=version,
                optimized_from=req.optimize_version,
                eval_result_ids=req.eval_result_ids,
            )
    except IntegrityError:
        _log.exception("agents.apply_optimized_prompt")
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=MSG_RESOURCE_ALREADY_EXISTS,
        ) from None
    except ProgrammingError:
        _log.exception("agents.apply_optimized_prompt")
        raise HTTPException(
            status_code=status.HTTP_501_NOT_IMPLEMENTED,
            detail=MSG_FEATURE_NOT_AVAILABLE,
        ) from None
    except SQLAlchemyError:
        _log.exception(_MSG_DATABASE_OPERATION_FAILED)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=_MSG_DATABASE_OPERATION_FAILED_PLEASE,
        ) from None
    except HTTPException:
        raise
    except Exception:
        _log.exception("Unexpected error applying optimized prompt")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=_MSG_UNEXPECTED_ERROR_OCCURRED_PLEASE,
        ) from None
    if agent is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=_MSG_AGENT_NOT_FOUND)
    return AgentResponse.model_validate(agent)


@router.get("/{agent_id}/prompts")
@handle_db_errors("agents.list_prompt_versions")
async def list_prompt_versions(
    agent_id: uuid.UUID,
    session: AsyncSession = Depends(get_db_session),
    principal: TenantPrincipal = require_permission(_CODE_AGENT_LIST),
) -> list[PromptVersionListEntry]:
    try:
        async with session.begin():
            await set_rls_org(session, principal.organisation_id)
            agent = await get_agent(session, agent_id)
    except ProgrammingError:
        _log.exception("agents.list_prompt_versions")
        raise HTTPException(
            status_code=status.HTTP_501_NOT_IMPLEMENTED,
            detail=MSG_FEATURE_NOT_AVAILABLE,
        ) from None
    except SQLAlchemyError:
        _log.exception(_MSG_DATABASE_OPERATION_FAILED)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=_MSG_DATABASE_OPERATION_FAILED_PLEASE,
        ) from None
    except HTTPException:
        raise
    except Exception:
        _log.exception("Unexpected error listing prompt versions")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=_MSG_UNEXPECTED_ERROR_OCCURRED_PLEASE,
        ) from None
    if agent is None or agent.organisation_id != principal.organisation_id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=_MSG_AGENT_NOT_FOUND)

    history = list(agent.prompt_version_history or [])
    return [
        PromptVersionListEntry(
            version=e["version"],
            created_at=e["created_at"],
            notes=e.get("notes", ""),
            optimized_from=e.get("optimized_from"),
            eval_result_ids=e.get("eval_result_ids", []),
        )
        for e in reversed(history)
    ]


@router.get("/{agent_id}/prompts/{version}")
@handle_db_errors("agents.get_prompt_version_endpoint")
async def get_prompt_version_endpoint(
    agent_id: uuid.UUID,
    version: str,
    session: AsyncSession = Depends(get_db_session),
    principal: TenantPrincipal = require_permission(_CODE_AGENT_LIST),
) -> PromptVersionDetail:
    try:
        async with session.begin():
            await set_rls_org(session, principal.organisation_id)
            agent = await get_agent(session, agent_id)
            if agent is None or agent.organisation_id != principal.organisation_id:
                raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=_MSG_AGENT_NOT_FOUND)
            entry = await get_prompt_version(session, agent_id, version)
    except ProgrammingError:
        _log.exception("agents.get_prompt_version_endpoint")
        raise HTTPException(
            status_code=status.HTTP_501_NOT_IMPLEMENTED,
            detail=MSG_FEATURE_NOT_AVAILABLE,
        ) from None
    except SQLAlchemyError:
        _log.exception(_MSG_DATABASE_OPERATION_FAILED)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=_MSG_DATABASE_OPERATION_FAILED_PLEASE,
        ) from None
    except HTTPException:
        raise
    except Exception:
        _log.exception("Unexpected error getting prompt version")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=_MSG_UNEXPECTED_ERROR_OCCURRED_PLEASE,
        ) from None
    if entry is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Version not found")
    return PromptVersionDetail(
        version=entry["version"],
        template=entry.get("template", ""),
        created_at=entry.get("created_at", ""),
        notes=entry.get("notes", ""),
        optimized_from=entry.get("optimized_from"),
        eval_result_ids=entry.get("eval_result_ids", []),
    )


@router.put("/{agent_id}/prompts/rollback/{version}")
@handle_db_errors("agents.rollback_prompt")
async def rollback_prompt(
    agent_id: uuid.UUID,
    version: str,
    session: AsyncSession = Depends(get_db_session),
    principal: TenantPrincipal = require_permission(_CODE_AGENT_UPDATE),
) -> PromptRollbackResponse:
    try:
        async with session.begin():
            await set_rls_org(session, principal.organisation_id)
            existing_agent = await get_agent(session, agent_id)
            if existing_agent is None or existing_agent.organisation_id != principal.organisation_id:
                raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not found")
            agent = await rollback_prompt_version(session, agent_id, version)
    except IntegrityError:
        _log.exception("agents.rollback_prompt")
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=MSG_RESOURCE_ALREADY_EXISTS,
        ) from None
    except ProgrammingError:
        _log.exception("agents.rollback_prompt")
        raise HTTPException(
            status_code=status.HTTP_501_NOT_IMPLEMENTED,
            detail=MSG_FEATURE_NOT_AVAILABLE,
        ) from None
    except SQLAlchemyError:
        _log.exception(_MSG_DATABASE_OPERATION_FAILED)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=_MSG_DATABASE_OPERATION_FAILED_PLEASE,
        ) from None
    except HTTPException:
        raise
    except Exception:
        _log.exception("Unexpected error rolling back prompt")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=_MSG_UNEXPECTED_ERROR_OCCURRED_PLEASE,
        ) from None
    if agent is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Agent or version not found",
        )
    return PromptRollbackResponse(
        agent=AgentResponse.model_validate(agent),
        message=f"Rolled back to {version}",
    )


@router.post("/{agent_id}/prompts/diff")
@handle_db_errors("agents.diff_prompt_versions")
async def diff_prompt_versions(
    agent_id: uuid.UUID,
    req: PromptDiffRequest,
    session: AsyncSession = Depends(get_db_session),
    principal: TenantPrincipal = require_permission(_CODE_AGENT_LIST),
) -> PromptDiffResponse:
    try:
        async with session.begin():
            await set_rls_org(session, principal.organisation_id)
            agent = await get_agent(session, agent_id)
    except ProgrammingError:
        _log.exception("agents.diff_prompt_versions")
        raise HTTPException(
            status_code=status.HTTP_501_NOT_IMPLEMENTED,
            detail=MSG_FEATURE_NOT_AVAILABLE,
        ) from None
    except SQLAlchemyError:
        _log.exception(_MSG_DATABASE_OPERATION_FAILED)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=_MSG_DATABASE_OPERATION_FAILED_PLEASE,
        ) from None
    except HTTPException:
        raise
    except Exception:
        _log.exception("Unexpected error diffing prompt versions")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=_MSG_UNEXPECTED_ERROR_OCCURRED_PLEASE,
        ) from None
    if agent is None or agent.organisation_id != principal.organisation_id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=_MSG_AGENT_NOT_FOUND)

    template_a = _resolve_prompt_template(agent, req.version_a)
    template_b = _resolve_prompt_template(agent, req.version_b)

    if template_a is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Version {req.version_a} not found",
        )
    if template_b is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Version {req.version_b} not found",
        )

    lines_a = template_a.splitlines(keepends=True)
    lines_b = template_b.splitlines(keepends=True)

    diff_lines = [
        DiffLine(
            type=kind,
            content=content,
            line_number_a=line_a,
            line_number_b=line_b,
        )
        for kind, content, line_a, line_b in iter_line_diffs(lines_a, lines_b)
    ]

    return PromptDiffResponse(version_a=req.version_a, version_b=req.version_b, lines=diff_lines)


@router.delete("/{agent_id}", status_code=status.HTTP_204_NO_CONTENT)
@handle_db_errors("agents.delete_agent_endpoint")
async def delete_agent_endpoint(
    agent_id: uuid.UUID,
    session: AsyncSession = Depends(get_db_session),
    principal: TenantPrincipal = require_permission("agent.delete"),
) -> None:
    try:
        async with session.begin():
            await set_rls_org(session, principal.organisation_id)
            existing_agent = await get_agent(session, agent_id)
            if existing_agent is None or existing_agent.organisation_id != principal.organisation_id:
                raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not found")
            deleted = await delete_agent(session, agent_id)
    except IntegrityError:
        _log.exception("agents.delete_agent_endpoint")
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=MSG_RESOURCE_ALREADY_EXISTS,
        ) from None
    except ProgrammingError:
        _log.exception("agents.delete_agent_endpoint")
        raise HTTPException(
            status_code=status.HTTP_501_NOT_IMPLEMENTED,
            detail=MSG_FEATURE_NOT_AVAILABLE,
        ) from None
    except SQLAlchemyError:
        _log.exception(_MSG_DATABASE_OPERATION_FAILED)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=_MSG_DATABASE_OPERATION_FAILED_PLEASE,
        ) from None
    except HTTPException:
        raise
    except Exception:
        _log.exception("Unexpected error deleting agent")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=_MSG_UNEXPECTED_ERROR_OCCURRED_PLEASE,
        ) from None
    if not deleted:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=_MSG_AGENT_NOT_FOUND)
