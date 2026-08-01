"""Org-scoped CRUD for Pipeline.

All functions assume the caller has set the RLS org context via set_rls_org()
before calling. The session must be within an active transaction.
"""

import copy
import logging
import uuid
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import delete, func, select, update
from sqlalchemy.exc import ProgrammingError
from sqlalchemy.ext.asyncio import AsyncSession

from modulo.db.crud.base import PageResult, apply_updates
from modulo.db.crud.pagination import CursorPaginator
from modulo.db.models.pipeline import Pipeline
from modulo.db.models.pipeline_edge import PipelineEdge
from modulo.db.models.pipeline_snapshot import PipelineSnapshot
from modulo.db.models.snapshot_schema_pin import SnapshotSchemaPin

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
    max_duration_seconds: int | None = None,
    stale_run_timeout_minutes: int = 30,
    folder_id: uuid.UUID | None = None,
) -> Pipeline:
    if folder_id is not None:
        from modulo.db.models.pipeline_folder import PipelineFolder

        folder = await session.execute(
            select(PipelineFolder).where(
                PipelineFolder.id == folder_id,
                PipelineFolder.organisation_id == org_id,
            )
        )
        if folder.scalar_one_or_none() is None:
            raise ValueError(f"Folder not found in this organisation: {folder_id}")
    pipeline = Pipeline(
        organisation_id=org_id,
        name=name,
        account_id=account_id,
        description=description,
        visibility=visibility,
        owner_team_id=owner_team_id,
        max_concurrent_runs=max_concurrent_runs,
        lock_wait_timeout_seconds=lock_wait_timeout_seconds,
        node_timeout_seconds=node_timeout_seconds,
        run_context_defaults=run_context_defaults or {},
        default_autonomy_level=default_autonomy_level,
        max_duration_seconds=max_duration_seconds,
        stale_run_timeout_minutes=stale_run_timeout_minutes,
        folder_id=folder_id,
    )
    session.add(pipeline)
    await session.flush()
    return pipeline


async def get_pipeline(
    session: AsyncSession, pipeline_id: uuid.UUID, *, include_deleted: bool = False
) -> Pipeline | None:
    stmt = select(Pipeline).where(Pipeline.id == pipeline_id)
    if not include_deleted:
        stmt = stmt.where(Pipeline.deleted_at.is_(None))
    result = await session.execute(stmt)
    return result.scalar_one_or_none()


async def check_pipeline_name_available(
    session: AsyncSession,
    org_id: uuid.UUID,
    name: str,
) -> bool:
    """Return True if no pipeline with *name* exists in the given org."""
    result = await session.execute(
        select(Pipeline)
        .where(
            Pipeline.organisation_id == org_id,
            Pipeline.name == name,
            Pipeline.deleted_at.is_(None),
        )
        .with_for_update()
    )
    return result.scalar_one_or_none() is None


async def list_pipelines(
    session: AsyncSession,
    *,
    page: int = 1,
    page_size: int = 20,
    cursor: str | None = None,
    include_archived: bool = False,
    include_deleted: bool = False,
    folder_id: uuid.UUID | None = None,
) -> PageResult[Pipeline]:
    base = select(Pipeline)
    if not include_deleted:
        base = base.where(Pipeline.deleted_at.is_(None))
    if not include_archived:
        base = base.where(Pipeline.archived_at.is_(None))
    if folder_id is not None:
        base = base.where(Pipeline.folder_id == folder_id)

    if cursor is not None:
        paginator = CursorPaginator()
        cp = await paginator.paginate(
            session,
            base,
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
    try:
        count_where = []
        if not include_deleted:
            count_where.append(Pipeline.deleted_at.is_(None))
        if not include_archived:
            count_where.append(Pipeline.archived_at.is_(None))
        total = (await session.execute(select(func.count()).select_from(Pipeline).where(*count_where))).scalar_one()
    except ProgrammingError:
        return PageResult(items=[], total=0, page=page, page_size=page_size)
    items = list(
        (await session.execute(base.order_by(Pipeline.created_at.desc()).offset(offset).limit(page_size))).scalars()
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


async def soft_delete_pipeline(session: AsyncSession, pipeline_id: uuid.UUID) -> Pipeline | None:
    """Mark a pipeline as deleted (soft delete). Returns None if not found or already deleted."""
    result = await session.execute(
        update(Pipeline)
        .where(Pipeline.id == pipeline_id, Pipeline.deleted_at.is_(None))
        .values(deleted_at=func.now())
        .returning(Pipeline)
    )
    await session.flush()
    return result.scalar_one_or_none()


async def restore_pipeline(session: AsyncSession, pipeline_id: uuid.UUID) -> Pipeline | None:
    """Restore a soft-deleted pipeline. Returns None if not found."""
    result = await session.execute(
        update(Pipeline)
        .where(Pipeline.id == pipeline_id, Pipeline.deleted_at.is_not(None))
        .values(deleted_at=None)
        .returning(Pipeline)
    )
    await session.flush()
    return result.scalar_one_or_none()


async def delete_pipeline(session: AsyncSession, pipeline_id: uuid.UUID) -> bool:
    """Hard-delete a pipeline. Only call from admin cleanup, not from user-facing API."""
    pipeline = await get_pipeline(session, pipeline_id, include_deleted=True)
    if pipeline is None:
        return False
    await session.delete(pipeline)
    await session.flush()
    return True


async def archive_pipeline(session: AsyncSession, pipeline_id: uuid.UUID) -> Pipeline | None:
    pipeline = await get_pipeline(session, pipeline_id)
    if pipeline is None:
        return None
    pipeline.archived_at = datetime.now(UTC)
    await session.flush()
    return pipeline


async def unarchive_pipeline(session: AsyncSession, pipeline_id: uuid.UUID) -> Pipeline | None:
    pipeline = await get_pipeline(session, pipeline_id)
    if pipeline is None:
        return None
    pipeline.archived_at = None
    await session.flush()
    return pipeline


async def count_pipelines(
    session: AsyncSession,
    *,
    org_id: uuid.UUID,
    include_archived: bool = False,
    include_deleted: bool = False,
) -> int:
    query = select(func.count()).select_from(Pipeline).where(Pipeline.organisation_id == org_id)
    if not include_deleted:
        query = query.where(Pipeline.deleted_at.is_(None))
    if not include_archived:
        query = query.where(Pipeline.archived_at.is_(None))
    result = await session.execute(query)
    return result.scalar_one() or 0


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
    """Deep-copy a pipeline and its graph (nodes + first-class edges + snapshots).

    Returns the *new* Pipeline, or *None* if the source does not exist.
    Connector bindings are preserved by reference so users can rebind later.
    SnapshotSchemaPins are also copied for each cloned snapshot.
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
        account_id=account_id,
        description=source.description,
        visibility=source.visibility,
        owner_team_id=source.owner_team_id,
        max_concurrent_runs=source.max_concurrent_runs,
        lock_wait_timeout_seconds=source.lock_wait_timeout_seconds,
        node_timeout_seconds=source.node_timeout_seconds,
        run_context_defaults=copy.deepcopy(source.run_context_defaults),
        graph_nodes_json=copy.deepcopy(source.graph_nodes_json),
        default_autonomy_level=source.default_autonomy_level,
        stale_run_timeout_minutes=source.stale_run_timeout_minutes,
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
    _log.info("Copying snapshots for pipeline %s -> %s", pipeline_id, cloned.id)
    snapshots = list(
        (
            await session.execute(
                select(PipelineSnapshot)
                .where(PipelineSnapshot.pipeline_id == pipeline_id)
                .order_by(PipelineSnapshot.snapshot_version)
            )
        ).scalars()
    )
    snap_count = 0
    for snap in snapshots:
        cloned_snap = PipelineSnapshot(
            organisation_id=org_id,
            pipeline_id=cloned.id,
            snapshot_version=snap.snapshot_version,
            account_id=snap.account_id,
            environment_profile_id=snap.environment_profile_id,
            graph_json=copy.deepcopy(snap.graph_json),
            connector_bindings_json=copy.deepcopy(snap.connector_bindings_json),
            schema_pins_json=copy.deepcopy(snap.schema_pins_json),
            prompt_pins_json=copy.deepcopy(snap.prompt_pins_json),
            model_backend_pins_json=copy.deepcopy(snap.model_backend_pins_json),
            composite_bindings_json=copy.deepcopy(snap.composite_bindings_json),
            parameter_bindings_json=copy.deepcopy(snap.parameter_bindings_json),
            tag=snap.tag,
            notes=snap.notes,
            default_autonomy_level=snap.default_autonomy_level,
            config_json=copy.deepcopy(snap.config_json),
            run_context_defaults=copy.deepcopy(snap.run_context_defaults),
        )
        session.add(cloned_snap)
        await session.flush()

        old_pins = list(
            (await session.execute(select(SnapshotSchemaPin).where(SnapshotSchemaPin.snapshot_id == snap.id))).scalars()
        )
        for pin in old_pins:
            session.add(
                SnapshotSchemaPin(
                    organisation_id=org_id,
                    snapshot_id=cloned_snap.id,
                    node_id=pin.node_id,
                    direction=pin.direction,
                    schema_id=pin.schema_id,
                    schema_version=pin.schema_version,
                )
            )
        snap_count += 1

    await session.flush()
    _log.info(
        "Clone complete: %s -> %s (%d edges, %d nodes, %d snapshots)",
        pipeline_id,
        cloned.id,
        edge_count,
        node_count,
        snap_count,
    )
    return cloned


async def replace_pipeline_graph(
    session: AsyncSession,
    *,
    pipeline_id: uuid.UUID,
    org_id: uuid.UUID,
    nodes: list[dict[str, Any]],
    edges: list[dict[str, Any]],
    is_privileged: bool,
) -> tuple[list[dict[str, Any]], list[PipelineEdge]] | None:
    """Atomically replace an editable graph while preserving first-class edges.

    ADR 017 service-layer backstop: explicit is_privileged marks this write as
    privileged-capable. The HITL gate guard (hitl-gate-removal-guard-plan.md)
    consumes this to block gate-weakening by non-privileged callers.
    """
    result = await session.execute(
        select(Pipeline).where(Pipeline.id == pipeline_id, Pipeline.deleted_at.is_(None)).with_for_update()
    )
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
            hitl_gate_config=edge.get("hitl_gate_config"),
        )
        for edge in edges
    ]
    session.add_all(persisted_edges)
    await session.flush()
    return list(pipeline.graph_nodes_json), persisted_edges
