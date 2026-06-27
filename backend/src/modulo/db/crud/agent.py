"""Org-scoped CRUD for Agent.

All functions require RLS org context to be set by the caller.
"""

import uuid
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from modulo.db.crud.base import PageResult, apply_updates
from modulo.db.models.agent import Agent
from modulo.db.models.eval_definition import EvalDefinition
from modulo.db.models.eval_result import EvalResult


async def create_agent(
    session: AsyncSession,
    *,
    org_id: uuid.UUID,
    name: str,
    created_by: uuid.UUID,
    input_schema_id: uuid.UUID,
    input_schema_version: str,
    output_schema_id: uuid.UUID,
    output_schema_version: str,
    prompt_template: str,
    model_backend_id: uuid.UUID,
    description: str | None = None,
    connector_type_refs: list[dict[str, Any]] | None = None,
    evals: list[dict[str, Any]] | None = None,
    retry_policy: dict[str, Any] | None = None,
    token_budget: int | None = None,
    max_input_length: int | None = None,
    library_id: uuid.UUID | None = None,
) -> Agent:
    agent = Agent(
        organisation_id=org_id,
        name=name,
        created_by=created_by,
        input_schema_id=input_schema_id,
        input_schema_version=input_schema_version,
        output_schema_id=output_schema_id,
        output_schema_version=output_schema_version,
        prompt_template=prompt_template,
        model_backend_id=model_backend_id,
        description=description,
        connector_type_refs=connector_type_refs or [],
        evals=evals,
        retry_policy=retry_policy or {},
        token_budget=token_budget,
        max_input_length=max_input_length,
        library_id=library_id,
    )
    session.add(agent)
    await session.flush()
    return agent


async def get_agent(session: AsyncSession, agent_id: uuid.UUID) -> Agent | None:
    result = await session.execute(select(Agent).where(Agent.id == agent_id))
    return result.scalar_one_or_none()


async def list_agents(
    session: AsyncSession,
    *,
    page: int = 1,
    page_size: int = 20,
) -> PageResult[Agent]:
    offset = (page - 1) * page_size
    total = (await session.execute(select(func.count()).select_from(Agent))).scalar_one()
    items = list(
        (
            await session.execute(select(Agent).order_by(Agent.created_at.desc()).offset(offset).limit(page_size))
        ).scalars()
    )
    return PageResult(items=items, total=total, page=page, page_size=page_size)


async def update_agent(
    session: AsyncSession,
    agent_id: uuid.UUID,
    updates: dict[str, Any],
) -> Agent | None:
    agent = await get_agent(session, agent_id)
    if agent is None:
        return None
    apply_updates(agent, updates)
    await session.flush()
    return agent


async def delete_agent(session: AsyncSession, agent_id: uuid.UUID) -> bool:
    agent = await get_agent(session, agent_id)
    if agent is None:
        return False
    await session.delete(agent)
    await session.flush()
    return True


async def add_prompt_version(
    session: AsyncSession,
    agent_id: uuid.UUID,
    *,
    new_template: str,
    notes: str | None = None,
    version_label: str | None = None,
    optimized_from: str | None = None,
    eval_result_ids: list[uuid.UUID] | None = None,
) -> Agent | None:
    """Append a new entry to the agent's prompt_version_history and update prompt_template.

    Returns None if the agent is not found.
    """
    agent = await get_agent(session, agent_id)
    if agent is None:
        return None

    history = list(agent.prompt_version_history or [])
    next_version = version_label or f"v{len(history) + 1}"

    history.append(
        {
            "version": next_version,
            "template": agent.prompt_template,
            "created_at": datetime.now(UTC).isoformat(),
            "notes": notes or "",
            "optimized_from": optimized_from,
            "eval_result_ids": [str(eid) for eid in (eval_result_ids or [])],
        }
    )

    agent.prompt_version_history = history
    agent.prompt_template = new_template
    await session.flush()
    return agent


async def get_prompt_version(
    session: AsyncSession,
    agent_id: uuid.UUID,
    version: str,
) -> dict[str, Any] | None:
    """Get a specific prompt version entry from history.

    Returns the version dict (version, template, created_at, notes, etc.)
    or None if agent or version not found.
    """
    agent = await get_agent(session, agent_id)
    if agent is None:
        return None

    if version == "current":
        return None

    history = list(agent.prompt_version_history or [])
    for entry in history:
        if entry.get("version") == version:
            return dict(entry)
    return None


async def rollback_prompt_version(
    session: AsyncSession,
    agent_id: uuid.UUID,
    target_version: str,
) -> Agent | None:
    """Rollback the agent's prompt to a specific historical version.

    Creates a new history entry (copying the current template) and sets
    the target version's template as the active prompt_template.
    Returns None if agent or target version not found.
    """
    agent = await get_agent(session, agent_id)
    if agent is None:
        return None

    history = list(agent.prompt_version_history or [])

    target_entry = None
    for entry in history:
        if entry.get("version") == target_version:
            target_entry = entry
            break

    if target_entry is None:
        return None

    target_template = target_entry.get("template", "")
    if not target_template:
        return None

    prev_version = agent.prompt_version_history[-1]["version"] if agent.prompt_version_history else "current"
    notes = f"Rolled back from {prev_version} to {target_version}"

    history.append(
        {
            "version": f"v{len(history) + 1}",
            "template": agent.prompt_template,
            "created_at": datetime.now(UTC).isoformat(),
            "notes": notes,
            "optimized_from": None,
            "eval_result_ids": [],
        }
    )

    agent.prompt_version_history = history
    agent.prompt_template = target_template
    await session.flush()
    return agent


async def get_eval_results_with_defs(
    session: AsyncSession,
    eval_result_ids: list[uuid.UUID],
    org_id: uuid.UUID,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Fetch eval results by ID (scoped to org) plus their definition maps.

    Returns (eval_results_list, eval_definitions_map) where definitions map
    is keyed by eval_id (as string).
    """
    er_result = await session.execute(
        select(EvalResult).where(
            EvalResult.id.in_(eval_result_ids),
            EvalResult.organisation_id == org_id,
        )
    )
    eval_results: list[EvalResult] = list(er_result.scalars().all())

    eval_def_ids = list({er.eval_id for er in eval_results})
    ed_result = await session.execute(select(EvalDefinition).where(EvalDefinition.id.in_(eval_def_ids)))
    definitions: dict[str, Any] = {}
    for ed in ed_result.scalars().all():
        definitions[str(ed.id)] = {
            "id": str(ed.id),
            "name": ed.name,
            "eval_type": ed.eval_type,
            "config_json": ed.config_json,
            "failure_behaviour": ed.failure_behaviour,
        }

    results_list = [
        {
            "id": str(er.id),
            "eval_id": str(er.eval_id),
            "run_id": str(er.run_id),
            "passed": er.passed,
            "score": er.score,
            "detail": er.detail,
        }
        for er in eval_results
    ]

    return results_list, definitions
