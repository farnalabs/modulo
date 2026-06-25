"""Agent CRUD REST API."""

import difflib
import json
import uuid
from datetime import datetime
from typing import Any, cast

from fastapi import APIRouter, Depends, HTTPException, Query, status
from langchain_core.messages import BaseMessage
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from modulo.api.dependencies import get_db_session
from modulo.auth.dependencies import get_current_user
from modulo.auth.jwt import AuthenticatedPrincipal
from modulo.core.prompt_optimizer import PromptOptimizer
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
from modulo.db.rls import set_rls_org
from modulo.settings import get_settings

router = APIRouter(prefix="/api/v1/agents", tags=["agents"])


class AgentCreate(BaseModel):
    name: str = Field(min_length=1)
    description: str | None = None
    input_schema_id: uuid.UUID
    input_schema_version: str
    output_schema_id: uuid.UUID
    output_schema_version: str
    prompt_template: str = Field(min_length=1)
    model_backend_id: uuid.UUID
    connector_type_refs: list[dict[str, Any]] = []
    evals: list[dict[str, Any]] = []
    retry_policy: dict[str, Any] = {}
    token_budget: int | None = Field(default=None, ge=0)
    library_id: uuid.UUID | None = None


class AgentUpdate(BaseModel):
    name: str | None = None
    description: str | None = None
    prompt_template: str | None = None
    model_backend_id: uuid.UUID | None = None
    connector_type_refs: list[dict[str, Any]] | None = None
    evals: list[dict[str, Any]] | None = None
    retry_policy: dict[str, Any] | None = None
    token_budget: int | None = Field(default=None, ge=0)


class AgentResponse(BaseModel):
    id: uuid.UUID
    organisation_id: uuid.UUID
    name: str
    description: str | None
    input_schema_id: uuid.UUID
    input_schema_version: str
    output_schema_id: uuid.UUID
    output_schema_version: str
    prompt_template: str
    prompt_version_history: list[dict[str, Any]]
    model_backend_id: uuid.UUID
    connector_type_refs: list[dict[str, Any]]
    evals: list[dict[str, Any]] | None
    retry_policy: dict[str, Any]
    token_budget: int | None
    library_id: uuid.UUID | None
    created_by: uuid.UUID
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
    eval_result_ids: list[str] = []


class PromptVersionDetail(BaseModel):
    version: str
    template: str
    created_at: str
    notes: str
    optimized_from: str | None = None
    eval_result_ids: list[str] = []


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


@router.get("", response_model=AgentListResponse)
async def list_agents_endpoint(
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
    session: AsyncSession = Depends(get_db_session),
    principal: AuthenticatedPrincipal = Depends(get_current_user),
) -> AgentListResponse:
    async with session.begin():
        await set_rls_org(session, principal.organisation_id)
        result = await list_agents(session, page=page, page_size=page_size)
    return AgentListResponse(
        items=[AgentResponse.model_validate(a) for a in result.items],
        total=result.total,
        page=result.page,
        page_size=result.page_size,
    )


@router.post("", response_model=AgentResponse, status_code=status.HTTP_201_CREATED)
async def create_agent_endpoint(
    body: AgentCreate,
    session: AsyncSession = Depends(get_db_session),
    principal: AuthenticatedPrincipal = Depends(get_current_user),
) -> AgentResponse:
    async with session.begin():
        await set_rls_org(session, principal.organisation_id)
        agent = await create_agent(
            session,
            org_id=principal.organisation_id,
            name=body.name,
            created_by=principal.user_id,
            input_schema_id=body.input_schema_id,
            input_schema_version=body.input_schema_version,
            output_schema_id=body.output_schema_id,
            output_schema_version=body.output_schema_version,
            prompt_template=body.prompt_template,
            model_backend_id=body.model_backend_id,
            description=body.description,
            connector_type_refs=body.connector_type_refs,
            evals=body.evals,
            retry_policy=body.retry_policy,
            token_budget=body.token_budget,
            library_id=body.library_id,
        )
    return AgentResponse.model_validate(agent)


@router.get("/{agent_id}", response_model=AgentResponse)
async def get_agent_endpoint(
    agent_id: uuid.UUID,
    session: AsyncSession = Depends(get_db_session),
    principal: AuthenticatedPrincipal = Depends(get_current_user),
) -> AgentResponse:
    async with session.begin():
        await set_rls_org(session, principal.organisation_id)
        agent = await get_agent(session, agent_id)
    if agent is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Agent not found")
    return AgentResponse.model_validate(agent)


@router.patch("/{agent_id}", response_model=AgentResponse)
async def update_agent_endpoint(
    agent_id: uuid.UUID,
    body: AgentUpdate,
    session: AsyncSession = Depends(get_db_session),
    principal: AuthenticatedPrincipal = Depends(get_current_user),
) -> AgentResponse:
    updates = {k: v for k, v in body.model_dump().items() if v is not None}
    async with session.begin():
        await set_rls_org(session, principal.organisation_id)
        agent = await update_agent(session, agent_id, updates)
    if agent is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Agent not found")
    return AgentResponse.model_validate(agent)


@router.post("/{agent_id}/prompts/{version}/optimize", response_model=PromptOptimizeResponse)
async def optimize_prompt(
    agent_id: uuid.UUID,
    version: str,
    body: PromptOptimizeRequest,
    session: AsyncSession = Depends(get_db_session),
    principal: AuthenticatedPrincipal = Depends(get_current_user),
) -> PromptOptimizeResponse:
    if not body.eval_result_ids:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="At least one eval_result_id is required",
        )

    async with session.begin():
        await set_rls_org(session, principal.organisation_id)
        agent = await get_agent(session, agent_id)

    if agent is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Agent not found")

    eval_results, eval_defs = await get_eval_results_with_defs(
        session, body.eval_result_ids, principal.organisation_id
    )

    if not eval_results:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="No eval results found for the given IDs",
        )

    backend_id = body.model_backend_id or agent.model_backend_id

    mb_result = await session.execute(
        select(ModelBackend).where(
            ModelBackend.id == backend_id,
            ModelBackend.organisation_id == principal.organisation_id,
        )
    )
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

    optimizer = PromptOptimizer(_llm_call)
    result = await optimizer.optimize(agent.prompt_template, eval_results, eval_defs)

    history = list(agent.prompt_version_history or [])
    next_version = f"v{len(history) + 1}"

    return PromptOptimizeResponse(
        suggested_prompt=result.suggested_prompt,
        rationale=result.rationale,
        analysis=result.analysis,
        version=next_version,
    )


@router.post("/{agent_id}/prompts/{version}/apply", response_model=AgentResponse)
async def apply_optimized_prompt(
    agent_id: uuid.UUID,
    version: str,
    body: ApplyOptimizedPromptRequest,
    session: AsyncSession = Depends(get_db_session),
    principal: AuthenticatedPrincipal = Depends(get_current_user),
) -> AgentResponse:
    async with session.begin():
        await set_rls_org(session, principal.organisation_id)
        agent = await add_prompt_version(
            session,
            agent_id,
            new_template=body.suggested_prompt,
            notes=body.rationale,
            version_label=version,
            optimized_from=body.optimize_version,
            eval_result_ids=body.eval_result_ids,
        )
    if agent is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Agent not found")
    return AgentResponse.model_validate(agent)


@router.get("/{agent_id}/prompts", response_model=list[PromptVersionListEntry])
async def list_prompt_versions(
    agent_id: uuid.UUID,
    session: AsyncSession = Depends(get_db_session),
    principal: AuthenticatedPrincipal = Depends(get_current_user),
) -> list[PromptVersionListEntry]:
    async with session.begin():
        await set_rls_org(session, principal.organisation_id)
        agent = await get_agent(session, agent_id)
    if agent is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Agent not found")

    history = list(agent.prompt_version_history or [])
    entries = [PromptVersionListEntry(
        version=e["version"],
        created_at=e["created_at"],
        notes=e.get("notes", ""),
        optimized_from=e.get("optimized_from"),
        eval_result_ids=e.get("eval_result_ids", []),
    ) for e in reversed(history)]
    return entries


@router.get("/{agent_id}/prompts/{version}", response_model=PromptVersionDetail)
async def get_prompt_version_endpoint(
    agent_id: uuid.UUID,
    version: str,
    session: AsyncSession = Depends(get_db_session),
    principal: AuthenticatedPrincipal = Depends(get_current_user),
) -> PromptVersionDetail:
    async with session.begin():
        await set_rls_org(session, principal.organisation_id)
        entry = await get_prompt_version(session, agent_id, version)
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


@router.put("/{agent_id}/prompts/rollback/{version}", response_model=PromptRollbackResponse)
async def rollback_prompt(
    agent_id: uuid.UUID,
    version: str,
    session: AsyncSession = Depends(get_db_session),
    principal: AuthenticatedPrincipal = Depends(get_current_user),
) -> PromptRollbackResponse:
    async with session.begin():
        await set_rls_org(session, principal.organisation_id)
        agent = await rollback_prompt_version(session, agent_id, version)
    if agent is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Agent or version not found",
        )
    return PromptRollbackResponse(
        agent=AgentResponse.model_validate(agent),
        message=f"Rolled back to {version}",
    )


@router.post("/{agent_id}/prompts/diff", response_model=PromptDiffResponse)
async def diff_prompt_versions(
    agent_id: uuid.UUID,
    body: PromptDiffRequest,
    session: AsyncSession = Depends(get_db_session),
    principal: AuthenticatedPrincipal = Depends(get_current_user),
) -> PromptDiffResponse:
    async with session.begin():
        await set_rls_org(session, principal.organisation_id)
        agent = await get_agent(session, agent_id)
    if agent is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Agent not found")

    history = list(agent.prompt_version_history or [])

    def _get_template(version: str) -> str | None:
        if version == "current":
            return agent.prompt_template
        for entry in history:
            if entry.get("version") == version:
                tpl = cast(str | None, entry.get("template"))
                return tpl or ""
        return None

    template_a = _get_template(body.version_a)
    template_b = _get_template(body.version_b)

    if template_a is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Version {body.version_a} not found",
        )
    if template_b is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Version {body.version_b} not found",
        )

    lines_a = template_a.splitlines(keepends=True)
    lines_b = template_b.splitlines(keepends=True)

    differ = difflib.SequenceMatcher(None, lines_a, lines_b)
    diff_lines: list[DiffLine] = []
    line_a = 1
    line_b = 1

    for op, i1, i2, j1, j2 in differ.get_opcodes():
        if op == "equal":
            for _ in range(i2 - i1):
                diff_lines.append(
                    DiffLine(
                        type="unchanged", content=lines_a[i1].rstrip("\n"),
                        line_number_a=line_a, line_number_b=line_b,
                    )
                )
                line_a += 1
                line_b += 1
            i1, j1 = i2, j2
        elif op == "replace":
            for _ in range(i2 - i1):
                diff_lines.append(
                    DiffLine(
                        type="removed", content=lines_a[i1].rstrip("\n"),
                        line_number_a=line_a, line_number_b=None,
                    )
                )
                line_a += 1
                i1 += 1
            for _ in range(j2 - j1):
                diff_lines.append(
                    DiffLine(
                        type="added", content=lines_b[j1].rstrip("\n"),
                        line_number_a=None, line_number_b=line_b,
                    )
                )
                line_b += 1
                j1 += 1
        elif op == "delete":
            for _ in range(i2 - i1):
                diff_lines.append(
                    DiffLine(
                        type="removed", content=lines_a[i1].rstrip("\n"),
                        line_number_a=line_a, line_number_b=None,
                    )
                )
                line_a += 1
                i1 += 1
        elif op == "insert":
            for _ in range(j2 - j1):
                diff_lines.append(
                    DiffLine(
                        type="added", content=lines_b[j1].rstrip("\n"),
                        line_number_a=None, line_number_b=line_b,
                    )
                )
                line_b += 1
                j1 += 1

    return PromptDiffResponse(version_a=body.version_a, version_b=body.version_b, lines=diff_lines)


@router.delete("/{agent_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_agent_endpoint(
    agent_id: uuid.UUID,
    session: AsyncSession = Depends(get_db_session),
    principal: AuthenticatedPrincipal = Depends(get_current_user),
) -> None:
    async with session.begin():
        await set_rls_org(session, principal.organisation_id)
        deleted = await delete_agent(session, agent_id)
    if not deleted:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Agent not found")
