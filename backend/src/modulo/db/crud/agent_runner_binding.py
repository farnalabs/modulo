"""Org-scoped CRUD for AgentRunnerBinding (FAR-592 / D6).

All functions require RLS org context to be set by the caller.
"""

import logging
import uuid
from typing import Any

from fastapi import HTTPException, status
from sqlalchemy import delete, func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from modulo.db.models.agent import Agent
from modulo.db.models.agent_runner_binding import AgentRunnerBinding
from modulo.db.models.model_backend import ModelBackend
from modulo.db.runner_binding_constraints import BindingValidationError, validate_binding_pair

_log = logging.getLogger(__name__)


async def list_bindings_for_agent(session: AsyncSession, agent_id: uuid.UUID) -> list[AgentRunnerBinding]:
    result = await session.execute(
        select(AgentRunnerBinding)
        .where(AgentRunnerBinding.agent_id == agent_id)
        .order_by(AgentRunnerBinding.created_at.asc())
    )
    return list(result.scalars())


def _validate_specs(
    bindings_specs: list[dict[str, Any]],
    backends_by_id: dict[uuid.UUID, ModelBackend],
) -> list[tuple[str, str]]:
    """Validate every (target_env_var, source_field) pair against its backend.

    Raises:
        HTTPException 400: the referenced backend is missing or not
            org-visible (bindings accept org-visible backends only).
        HTTPException 409: the same canonical target_env_var appears twice.
        HTTPException 422: a validator rejects the var/field shape
            (BindingValidationError is a ValueError — surfacing it raw would
            render as a 500).
    """
    validated: list[tuple[str, str]] = []
    seen_targets: set[str] = set()
    for spec in bindings_specs:
        backend = backends_by_id.get(spec["_backend_id"])
        if backend is None:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Model backend not found or not org-visible: bindings accept org-visible backends only.",
            )
        try:
            target, _source = validate_binding_pair(
                target_env_var=str(spec["target_env_var"]),
                source_field=str(spec["source_field"]),
                provider=backend.provider,
            )
        except BindingValidationError as exc:
            # FAR-592 (D6 F7): the validator is ValueError-shaped and would
            # otherwise escape as a 500 — map to the honest 422 with the
            # validator message.
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                detail=str(exc),
            ) from exc
        if target in seen_targets:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=f"target_env_var '{target}' is bound more than once",
            )
        seen_targets.add(target)
        validated.append((target, _source))
    return validated


async def replace_agent_bindings(
    session: AsyncSession,
    *,
    org_id: uuid.UUID,
    agent_id: uuid.UUID,
    bindings_specs: list[dict[str, Any]],
) -> list[AgentRunnerBinding]:
    """Replace an agent's binding set wholesale (save-replace semantics).

    Every spec carries ``target_env_var``, ``source_field``,
    ``model_backend_id`` (``_backend_id`` key), and ``account_id``
    (``_account_id``). Each backend must exist AND be visible to the org
    (``visibility = 'org'``) — save-time validation surface. Duplicate or
    reserved targets surface as 400/409; IntegrityError maps to the typed 409.

    Concurrency guard (FAR-592 qa fixes / F6): the agent row is locked
    (``SELECT ... FOR UPDATE``) BEFORE the delete-all + inserts, so two
    concurrent replace calls serialise — each sees the other's committed
    state and the result is LAST-WRITER-WINS, not a union of both var sets.
    """
    backend_ids = {spec["_backend_id"] for spec in bindings_specs}
    backends_rows = await session.execute(
        select(ModelBackend).where(
            ModelBackend.organisation_id == org_id,
            ModelBackend.id.in_(backend_ids),
            ModelBackend.visibility == "org",
        )
    )
    backends_by_id = {row.id: row for row in backends_rows.scalars()}

    validated = _validate_specs(bindings_specs, backends_by_id)

    # Serialise concurrent saves on the agent row (transaction-scoped).
    await session.execute(select(Agent.id).where(Agent.id == agent_id).with_for_update())

    # Replace wholesale: delete-then-insert keeps the save contract simple and
    # the UNIQUE (org, agent, target_env_var) constraint honest.
    await delete_all_bindings_for_agent(session, agent_id=agent_id)
    created: list[AgentRunnerBinding] = []
    for spec, (target, source) in zip(bindings_specs, validated, strict=True):
        try:
            binding = AgentRunnerBinding(
                organisation_id=org_id,
                agent_id=agent_id,
                model_backend_id=spec["_backend_id"],
                target_env_var=target,
                source_field=source,
                account_id=spec["_account_id"],
            )
            session.add(binding)
            await session.flush()
        except IntegrityError as exc:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=f"target_env_var '{target}' is not unique for this agent",
            ) from exc
        created.append(binding)
    return created


async def delete_all_bindings_for_agent(session: AsyncSession, agent_id: uuid.UUID) -> int:
    """Delete every binding row for an agent (agent delete + save-replace)."""
    result = await session.execute(delete(AgentRunnerBinding).where(AgentRunnerBinding.agent_id == agent_id))
    return int(getattr(result, "rowcount", 0) or 0)


async def delete_binding(session: AsyncSession, *, binding_id: uuid.UUID, agent_id: uuid.UUID) -> bool:
    """Delete one binding row, SCOPED to the agent it belongs to (FAR-592 qa F8).

    The lookup verifies ``binding.agent_id == agent_id`` — a binding row from
    a DIFFERENT agent under the same org is a 404-shaped miss (returns
    False), never a cross-agent delete. The CALLER decides 404 vs raise and
    appends the audit event only for a True result (no phantom audit rows).
    """
    result = await session.execute(
        select(AgentRunnerBinding).where(
            AgentRunnerBinding.id == binding_id,
            AgentRunnerBinding.agent_id == agent_id,
        )
    )
    binding = result.scalar_one_or_none()
    if binding is None:
        return False
    await session.delete(binding)
    await session.flush()
    return True


async def count_bindings_for_backend(session: AsyncSession, *, model_backend_id: uuid.UUID, org_id: uuid.UUID) -> int:
    """Pre-delete inventory count: "in use by N agents"."""
    result = await session.execute(
        select(func.count())
        .select_from(AgentRunnerBinding)
        .where(
            AgentRunnerBinding.model_backend_id == model_backend_id,
            AgentRunnerBinding.organisation_id == org_id,
        )
    )
    return int(result.scalar_one() or 0)


async def delete_org_binding_rows(session: AsyncSession, org_id: uuid.UUID) -> int:
    """Org teardown: delete ALL binding rows BEFORE the hard org delete.

    ``model_backends`` is ON DELETE RESTRICT — a hard ``session.delete(org)``
    cascades agents (which would cascade-remove most binding rows through the
    agent FK), but a binding row referencing a still-live org backend would
    RESTRICT-abort teardown first. Explicit delete-first ordering keeps the
    org teardown unconditional.
    """
    result = await session.execute(delete(AgentRunnerBinding).where(AgentRunnerBinding.organisation_id == org_id))
    _log.info(
        "agent_runner_bindings.org_teardown_deleted",
        extra={"count": getattr(result, "rowcount", 0)},
    )
    await session.flush()
    return int(getattr(result, "rowcount", 0) or 0)
