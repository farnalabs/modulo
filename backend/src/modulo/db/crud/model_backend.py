"""Org-scoped CRUD for ModelBackend.

All functions require RLS org context to be set by the caller.
"""

import logging
import uuid
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.exc import ProgrammingError
from sqlalchemy.ext.asyncio import AsyncSession

from modulo.db.crud.base import PageResult, apply_updates
from modulo.db.models.agent import Agent
from modulo.db.models.model_backend import ModelBackend
from modulo.db.models.pipeline import Pipeline

_log = logging.getLogger(__name__)


async def create_model_backend(
    session: AsyncSession,
    *,
    org_id: uuid.UUID,
    name: str,
    display_name: str,
    provider: str,
    model_id: str,
    credentials_ciphertext: bytes,
    account_id: uuid.UUID,
    default_params: dict[str, Any] | None = None,
    visibility: str = "org",
    owner_team_id: uuid.UUID | None = None,
    fallback_backend_ids: list[str] | None = None,
    tier: str = "native",
) -> ModelBackend:
    mb = ModelBackend(
        organisation_id=org_id,
        name=name,
        display_name=display_name,
        provider=provider,
        model_id=model_id,
        credentials_ciphertext=credentials_ciphertext,
        account_id=account_id,
        default_params=default_params or {},
        visibility=visibility,
        owner_team_id=owner_team_id,
        fallback_backend_ids=fallback_backend_ids,
        tier=tier,
    )
    session.add(mb)
    await session.flush()
    return mb


async def get_model_backend(session: AsyncSession, model_backend_id: uuid.UUID) -> ModelBackend | None:
    result = await session.execute(select(ModelBackend).where(ModelBackend.id == model_backend_id))
    return result.scalar_one_or_none()


async def list_model_backends(
    session: AsyncSession,
    *,
    org_id: uuid.UUID,
    page: int = 1,
    page_size: int = 20,
    excluded_tiers: list[str] | None = None,
) -> PageResult[ModelBackend]:
    if excluded_tiers is None:
        excluded_tiers = ["in_dev"]
    offset = (page - 1) * page_size
    try:
        total_query = select(func.count()).select_from(ModelBackend).where(ModelBackend.organisation_id == org_id)
        if excluded_tiers:
            total_query = total_query.where(~ModelBackend.tier.in_(excluded_tiers))
        total = (await session.execute(total_query)).scalar_one()
    except ProgrammingError:
        return PageResult(items=[], total=0, page=page, page_size=page_size)
    try:
        items_stmt = (
            select(ModelBackend)
            .where(ModelBackend.organisation_id == org_id)
            .order_by(ModelBackend.created_at.desc())
            .offset(offset)
            .limit(page_size)
        )
        if excluded_tiers:
            items_stmt = items_stmt.where(~ModelBackend.tier.in_(excluded_tiers))
        items = list((await session.execute(items_stmt)).scalars())
    except ProgrammingError:
        return PageResult(items=[], total=0, page=page, page_size=page_size)
    return PageResult(items=items, total=total, page=page, page_size=page_size)


async def update_model_backend(
    session: AsyncSession,
    model_backend_id: uuid.UUID,
    updates: dict[str, Any],
) -> ModelBackend | None:
    mb = await get_model_backend(session, model_backend_id)
    if mb is None:
        return None
    apply_updates(mb, updates)
    await session.flush()
    return mb


async def delete_model_backend(session: AsyncSession, model_backend_id: uuid.UUID) -> bool:
    mb = await get_model_backend(session, model_backend_id)
    if mb is None:
        return False
    await session.delete(mb)
    await session.flush()
    return True


async def list_backends_referencing_fallback(
    session: AsyncSession,
    *,
    org_id: uuid.UUID,
    backend_id: uuid.UUID,
) -> list[ModelBackend]:
    """Return the org's model backends whose fallback chain references ``backend_id``.

    ``fallback_backend_ids`` is a JSON column with no relational FK, so delete
    protection must scan the org's rows for a reference. Stored ids may be
    strings or UUIDs (older rows / round-tripped payloads), so the comparison
    normalises both sides to ``str``. The target backend itself is excluded: a
    self-referencing fallback (legacy data) must not permanently block its own
    deletion.
    """
    result = await session.execute(select(ModelBackend).where(ModelBackend.organisation_id == org_id))
    target = str(backend_id)
    referencing: list[ModelBackend] = []
    for mb in result.scalars():
        if mb.id == backend_id:
            continue
        raw = mb.fallback_backend_ids
        if not raw:
            continue
        if target in {str(fid) for fid in raw}:
            referencing.append(mb)
    return referencing


async def list_pipeline_references_for_backend(
    session: AsyncSession,
    *,
    org_id: uuid.UUID,
    backend_id: uuid.UUID,
    page: int = 1,
    page_size: int = 20,
) -> PageResult[dict[str, Any]]:
    """Return pipelines that reference ``backend_id`` in their graph configuration.

    Scans ``graph_nodes_json`` for direct node references (``model_backend_id``)
    and indirect agent references (``agent_id`` → Agent table lookup). Returns
    a paginated list of dicts with ``pipeline_id``, ``pipeline_name``,
    ``agent_name``, ``agent_id``, and ``reference_type``.
    """
    target = str(backend_id)
    pipelines_stmt = (
        select(Pipeline)
        .where(Pipeline.deleted_at.is_(None), Pipeline.organisation_id == org_id)
        .order_by(Pipeline.name.asc())
    )
    all_pipelines = list((await session.execute(pipelines_stmt)).scalars())

    # Collect agent IDs for batch lookup
    all_agent_ids: set[uuid.UUID] = set()
    pipeline_agent_map: dict[uuid.UUID, list[uuid.UUID]] = {}
    for pipeline in all_pipelines:
        nodes = pipeline.graph_nodes_json or []
        agent_ids: list[uuid.UUID] = []
        for node in nodes:
            agent_id_raw = node.get("agent_id")
            if agent_id_raw:
                try:
                    agent_ids.append(uuid.UUID(str(agent_id_raw)))
                except (ValueError, TypeError):
                    continue
        if agent_ids:
            pipeline_agent_map[pipeline.id] = agent_ids
            all_agent_ids.update(agent_ids)

    # Batch query agents that reference the target backend
    agent_backend_map: dict[uuid.UUID, str] = {}
    if all_agent_ids:
        agents_stmt = select(Agent).where(
            Agent.id.in_(all_agent_ids),
            Agent.model_backend_id == backend_id,
            Agent.organisation_id == org_id,
        )
        for agent in (await session.execute(agents_stmt)).scalars():
            agent_backend_map[agent.id] = agent.name

    # Build results
    all_refs: list[dict[str, Any]] = []
    seen_agent_keys: set[tuple[uuid.UUID, uuid.UUID]] = set()
    for pipeline in all_pipelines:
        nodes = pipeline.graph_nodes_json or []
        for node in nodes:
            # Direct node reference
            node_backend_raw = node.get("model_backend_id")
            if node_backend_raw and str(node_backend_raw) == target:
                all_refs.append(
                    {
                        "pipeline_id": pipeline.id,
                        "pipeline_name": pipeline.name,
                        "agent_name": None,
                        "agent_id": None,
                        "reference_type": "direct_node",
                    }
                )
        # Agent references
        agent_ids = pipeline_agent_map.get(pipeline.id, [])
        for aid in agent_ids:
            if aid in agent_backend_map:
                dedup_key = (pipeline.id, aid)
                if dedup_key in seen_agent_keys:
                    continue
                seen_agent_keys.add(dedup_key)
                all_refs.append(
                    {
                        "pipeline_id": pipeline.id,
                        "pipeline_name": pipeline.name,
                        "agent_name": agent_backend_map[aid],
                        "agent_id": aid,
                        "reference_type": "agent",
                    }
                )

    total = len(all_refs)
    start = (page - 1) * page_size
    end = start + page_size
    return PageResult(items=all_refs[start:end], total=total, page=page, page_size=page_size)
