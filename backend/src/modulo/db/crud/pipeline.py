"""Org-scoped CRUD for Pipeline.

All functions assume the caller has set the RLS org context via set_rls_org()
before calling. The session must be within an active transaction.
"""

import copy
import logging
import uuid
from typing import Any

from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from modulo.db.crud.base import PageResult, apply_updates
from modulo.db.crud.pagination import CursorPaginator
from modulo.db.models.pipeline import Pipeline
from modulo.db.models.pipeline_edge import PipelineEdge

_log = logging.getLogger(__name__)


async def create_pipeline(
    session: AsyncSession,
    *,
    org_id: uuid.UUID,
    name: str,
    account_id: uuid.UUID,
    description: str | None = None,
    visibility: str = "org",
    owner_team_id: uuid.UUID | None = None,
    max_concurrent_runs: int = 5,
    lock_wait_timeout_seconds: int = 300,
    node_timeout_seconds: int = 300,
    run_context_defaults: dict[str, Any] | None = None,
    default_autonomy_level: str = "manual_approval",
) -> Pipeline:
    pipeline = Pipeline(
        organisation_id=org_id,
        name=name,
        created_by=account_id,
        description=description,
        visibility=visibility,
        owner_team_id=owner_team_id,
        max_concurrent_runs=max_concurrent_runs,
        lock_wait_timeout_seconds=lock_wait_timeout_seconds,
        node_timeout_seconds=node_timeout_seconds,
        run_context_defaults=run_context_defaults or {},
        default_autonomy_level=default_autonomy_level,
    )
    session.add(pipeline)
    await session.flush()
    return pipeline


async def get_pipeline(session: AsyncSession, pipeline_id: uuid.UUID) -> Pipeline | None:
    result = await session.execute(select(Pipeline).where(Pipeline.id == pipeline_id))
    return result.scalar_one_or_none()


async def check_pipeline_name_available(
    session: AsyncSession,
    org_id: uuid.UUID,
    name: str,
) -> bool:
    """Return True if no pipeline with *name* exists in the given org."""
    result = await session.execute(
        select(Pipeline).where(
            Pipeline.organisation_id == org_id,
            Pipeline.name == name,
        )
    )
    return result.scalar_one_or_none() is None


async def list_pipelines(
    session: AsyncSession,
    *,
    page: int = 1,
    page_size: int = 20,
    cursor: str | None = None,
) -> PageResult[Pipeline]:
    if cursor is not None:
        paginator = CursorPaginator()
        cp = await paginator.paginate(
            session,
            select(Pipeline),
            cursor=cursor,
            limit=page_size,
            model=Pipeline,
            compute_total=True,
        )
        return PageResult(
            items=cp.items,
            total=cp.total or 0,
            page=page,
            page_size=page_size,
            next_cursor=cp.next_cursor,
            has_more=cp.has_more,
        )

    offset = (page - 1) * page_size
    total = (await session.execute(select(func.count()).select_from(Pipeline))).scalar_one()
    items = list(
        (
            await session.execute(select(Pipeline).order_by(Pipeline.created_at.desc()).offset(offset).limit(page_size))
        ).scalars()
    )
    return PageResult(items=items, total=total, page=page, page_size=page_size)


async def update_pipeline(
    session: AsyncSession,
    pipeline_id: uuid.UUID,
    updates: dict[str, Any],
) -> Pipeline | None:
    pipeline = await get_pipeline(session, pipeline_id)
    if pipeline is None:
        return None
    apply_updates(pipeline, updates)
    await session.flush()
    return pipeline


async def delete_pipeline(session: AsyncSession, pipeline_id: uuid.UUID) -> bool:
    pipeline = await get_pipeline(session, pipeline_id)
    if pipeline is None:
        return False
    await session.delete(pipeline)
    await session.flush()
    return True


async def get_pipeline_graph(
    session: AsyncSession,
    pipeline_id: uuid.UUID,
) -> tuple[list[dict[str, Any]], list[PipelineEdge]] | None:
    """Return the editable live graph for an RLS-visible pipeline."""
    pipeline = await get_pipeline(session, pipeline_id)
    if pipeline is None:
        return None
    edges = list(
        (
            await session.execute(
                select(PipelineEdge)
                .where(PipelineEdge.pipeline_id == pipeline_id)
                .order_by(PipelineEdge.created_at, PipelineEdge.id)
            )
        ).scalars()
    )
    return list(pipeline.graph_nodes_json), edges


async def clone_pipeline(
    session: AsyncSession,
    *,
    org_id: uuid.UUID,
    pipeline_id: uuid.UUID,
    account_id: uuid.UUID,
    new_name: str | None = None,
) -> Pipeline | None:
    """Deep-copy a pipeline and its graph (nodes + first-class edges).

    Returns the *new* Pipeline, or *None* if the source does not exist.
    Connector bindings are preserved by reference so users can rebind later.
    """
    _log.info("Cloning pipeline %s (org=%s, requested_name=%s)", pipeline_id, org_id, new_name)

    source = await get_pipeline(session, pipeline_id)
    if source is None:
        _log.warning("Clone aborted: source pipeline %s not found", pipeline_id)
        return None

    name = new_name or f"Copy of {source.name}"
    _log.info("Copying pipeline config for %s -> '%s'", pipeline_id, name)
    cloned = Pipeline(
        organisation_id=org_id,
        name=name,
        created_by=account_id,
        description=source.description,
        visibility=source.visibility,
        owner_team_id=source.owner_team_id,
        max_concurrent_runs=source.max_concurrent_runs,
        lock_wait_timeout_seconds=source.lock_wait_timeout_seconds,
        node_timeout_seconds=source.node_timeout_seconds,
        run_context_defaults=copy.deepcopy(source.run_context_defaults),
        graph_nodes_json=copy.deepcopy(source.graph_nodes_json),
        default_autonomy_level=source.default_autonomy_level,
    )
    session.add(cloned)
    await session.flush()
    _log.info("Pipeline config copied: new id=%s", cloned.id)

    _log.info("Copying edges for pipeline %s -> %s", pipeline_id, cloned.id)
    edges = list(
        (
            await session.execute(
                select(PipelineEdge)
                .where(PipelineEdge.pipeline_id == pipeline_id)
                .order_by(PipelineEdge.created_at, PipelineEdge.id)
            )
        ).scalars()
    )
    edge_count = 0
    for edge in edges:
        cloned_edge = PipelineEdge(
            organisation_id=org_id,
            pipeline_id=cloned.id,
            source_node_id=edge.source_node_id,
            target_node_id=edge.target_node_id,
            edge_type=edge.edge_type,
            hitl_gate_config=copy.deepcopy(edge.hitl_gate_config),
        )
        session.add(cloned_edge)
        edge_count += 1
    await session.flush()
    node_count = len(source.graph_nodes_json)
    _log.info("Clone complete: %s -> %s (%d edges, %d nodes)", pipeline_id, cloned.id, edge_count, node_count)
    return cloned


async def replace_pipeline_graph(
    session: AsyncSession,
    *,
    pipeline_id: uuid.UUID,
    org_id: uuid.UUID,
    nodes: list[dict[str, Any]],
    edges: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], list[PipelineEdge]] | None:
    """Atomically replace an editable graph while preserving first-class edges."""
    result = await session.execute(select(Pipeline).where(Pipeline.id == pipeline_id).with_for_update())
    pipeline = result.scalar_one_or_none()
    if pipeline is None:
        return None

    pipeline.graph_nodes_json = nodes
    await session.execute(delete(PipelineEdge).where(PipelineEdge.pipeline_id == pipeline_id))
    persisted_edges = [
        PipelineEdge(
            id=edge["id"],
            organisation_id=org_id,
            pipeline_id=pipeline_id,
            source_node_id=edge["source_node_id"],
            target_node_id=edge["target_node_id"],
            edge_type=edge["edge_type"],
            hitl_gate_config=edge["hitl_gate_config"],
        )
        for edge in edges
    ]
    session.add_all(persisted_edges)
    await session.flush()
    return list(pipeline.graph_nodes_json), persisted_edges
