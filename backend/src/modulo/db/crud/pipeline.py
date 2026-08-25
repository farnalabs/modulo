"""Org-scoped CRUD for Pipeline.

All functions assume the caller has set the RLS org context via set_rls_org()
before calling. The session must be within an active transaction.
"""

import copy
import logging
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Literal

from sqlalchemy import ColumnElement, Connection, delete, func, select, update
from sqlalchemy.exc import InvalidRequestError, ProgrammingError
from sqlalchemy.ext.asyncio import (
    AsyncConnection,
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import NullPool

from modulo.core.audit_logger import append_audit_event
from modulo.db.crud.base import PageResult, apply_updates
from modulo.db.crud.hitl_gate_guard import (
    HitlGateWeakeningDenied,
    apply_gated_edge_diff,
    build_gate_diff_payload,
    denial_detail,
    enforce_guardrail_binding_strip,
    resolve_effective_privilege,
)
from modulo.db.crud.pagination import CursorPaginator
from modulo.db.crud.run import count_active_runs_for_pipeline
from modulo.db.crud.team_scope import team_scope_clause
from modulo.db.models.pipeline import Pipeline
from modulo.db.models.pipeline_edge import PipelineEdge
from modulo.db.models.pipeline_snapshot import PipelineSnapshot
from modulo.db.models.snapshot_schema_pin import SnapshotSchemaPin
from modulo.db.rls import set_rls_org, set_rls_user_context
from modulo.util import sanitise_log_value as _sanitise_log_value

_log = logging.getLogger(__name__)


class PipelineHasActiveRunsError(Exception):
    """Ownership transfer is blocked while the pipeline has non-terminal runs.

    Raised by ``update_pipeline`` when ``owner_team_id`` changes while any run
    is in a non-terminal state. The route maps this to a structured 409
    ``pipeline_has_active_runs`` response (PRD §9.3 / ownership transfer).
    """

    def __init__(self, active_run_count: int) -> None:
        self.active_run_count = active_run_count
        super().__init__(f"{active_run_count} run(s) still in progress")


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
    session: AsyncSession,
    pipeline_id: uuid.UUID,
    *,
    include_deleted: bool = False,
    organisation_id: uuid.UUID | None = None,
) -> Pipeline | None:
    """Fetch a single pipeline by ID.

    Defence-in-depth: when *organisation_id* is provided, the query also
    filters on ``organisation_id`` so cross-tenant access is impossible even
    if RLS is misconfigured. RLS-based callers may omit it, but API-facing
    callers SHOULD pass it.
    """
    stmt = select(Pipeline).where(Pipeline.id == pipeline_id)
    if not include_deleted:
        stmt = stmt.where(Pipeline.deleted_at.is_(None))
    if organisation_id is not None:
        stmt = stmt.where(Pipeline.organisation_id == organisation_id)
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
    team_id: uuid.UUID | None = None,
) -> PageResult[Pipeline]:
    base = select(Pipeline)
    if not include_deleted:
        base = base.where(Pipeline.deleted_at.is_(None))
    if not include_archived:
        base = base.where(Pipeline.archived_at.is_(None))
    if folder_id is not None:
        base = base.where(Pipeline.folder_id == folder_id)
    if team_id is not None:
        # A team-scoped caller sees its own team's pipelines plus org-level
        # pipelines (no owner team) — the same boundary the MCP guard applies.
        base = base.where(team_scope_clause(Pipeline.owner_team_id, team_id))

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
        count_where: list[ColumnElement[bool]] = []
        if not include_deleted:
            count_where.append(Pipeline.deleted_at.is_(None))
        if not include_archived:
            count_where.append(Pipeline.archived_at.is_(None))
        if folder_id is not None:
            count_where.append(Pipeline.folder_id == folder_id)
        if team_id is not None:
            count_where.append(team_scope_clause(Pipeline.owner_team_id, team_id))
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
    *,
    org_id: uuid.UUID | None = None,
    account_id: uuid.UUID | None = None,
    request_id: str | None = None,
) -> Pipeline | None:
    """Update a pipeline, applying the PRD §9.3 ownership-transfer rules.

    When ``owner_team_id`` changes (reassign to a team, or clear back to
    org-wide) AND audit context is supplied (the REST route path), the transfer
    is blocked while any non-terminal run exists (``PipelineHasActiveRunsError``)
    and a ``resource_team_ownership_changed`` audit event is recorded. Callers
    that pass no audit context (internal tooling, MCP) are not affected.
    """
    pipeline = await get_pipeline(session, pipeline_id)
    if pipeline is None:
        return None
    old_team_id = pipeline.owner_team_id
    apply_updates(pipeline, updates)
    new_team_id = pipeline.owner_team_id
    if org_id is not None and account_id is not None and new_team_id != old_team_id:
        active_runs = await count_active_runs_for_pipeline(session, pipeline_id, include_pending=True)
        if active_runs:
            raise PipelineHasActiveRunsError(active_runs)
        await append_audit_event(
            session,
            org_id=org_id,
            event_type="resource_team_ownership_changed",
            actor_user_id=account_id,
            resource_type="pipeline",
            resource_id=pipeline_id,
            payload_json={
                "resource_type": "pipeline",
                "resource_id": str(pipeline_id),
                "old_team_id": str(old_team_id) if old_team_id is not None else None,
                "new_team_id": str(new_team_id) if new_team_id is not None else None,
                "changed_by": str(account_id),
            },
            request_id=request_id,
        )
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


@dataclass
class _CloneSourceSnapshot:
    """Plain-data snapshot of the source pipeline taken in the clone's short
    step-(a) transaction, so the slower clone work (step b) never depends on a
    lock or on live reads of the source (hitl-gate-removal-guard-plan.md §3 item 3).
    """

    name: str
    description: str | None
    visibility: str
    owner_team_id: uuid.UUID | None
    max_concurrent_runs: int
    lock_wait_timeout_seconds: int
    node_timeout_seconds: int
    run_context_defaults: dict[str, Any]
    graph_nodes_json: list[dict[str, Any]]
    default_autonomy_level: str
    stale_run_timeout_minutes: int
    edges: list[dict[str, Any]]
    snapshots: list[dict[str, Any]]


async def clone_pipeline(
    session: AsyncSession,
    *,
    org_id: uuid.UUID,
    pipeline_id: uuid.UUID,
    account_id: uuid.UUID,
    org_role: str | None = None,
    new_name: str | None = None,
    _read_session_factory: Callable[[], AsyncSession] | None = None,
    _on_step_a_held: Callable[[], Awaitable[None]] | None = None,
    _on_step_a_committed: Callable[[], Awaitable[None]] | None = None,
) -> Pipeline | None:
    """Deep-copy a pipeline and its graph (nodes + first-class edges + snapshots).

    Returns the *new* Pipeline, or *None* if the source does not exist.
    Connector bindings are preserved by reference so users can rebind later.
    SnapshotSchemaPins are also copied for each cloned snapshot.

    Torn-read fix (plan §3 item 3): the source reads (``FOR SHARE`` on the
    pipeline row + nodes/edges/snapshots into plain data) run in a short,
    separate transaction on a read session that commits immediately. The
    slower clone work then runs on the caller's session using only the
    plain-data snapshot, with no further lock dependency — a concurrent
    ``replace_pipeline_graph``'s ``FOR UPDATE`` on the same row can proceed as
    soon as the step-(a) transaction commits. All cloned gated edges emit ONE
    batched ``edge_created_with_gate`` audit event.

    The step-(a) read session is a separate connection, so ``set_rls_org`` is
    not enough: pipelines are team-scoped, so their RLS policy also checks the
    caller's ``app.user_id`` / ``app.org_role``, which must be re-applied via
    ``set_rls_user_context`` for the source to be visible. Pass ``org_role``
    (the caller's org role) and the read session re-applies the caller's full
    RLS context; ``account_id`` doubles as the caller's user.
    """
    _log.info(
        "Cloning pipeline %s (org=%s, requested_name=%s)",
        _sanitise_log_value(pipeline_id),
        _sanitise_log_value(org_id),
        _sanitise_log_value(new_name),
    )

    snapshot = await _read_clone_source_snapshot(
        session,
        org_id=org_id,
        pipeline_id=pipeline_id,
        user_id=account_id,
        org_role=org_role,
        read_factory=_read_session_factory,
        on_step_a_held=_on_step_a_held,
    )
    if snapshot is None:
        _log.warning("Clone aborted: source pipeline %s not found", pipeline_id)
        return None

    if _on_step_a_committed is not None:
        await _on_step_a_committed()

    cloned = await _clone_pipeline_config(
        session,
        snapshot,
        org_id=org_id,
        account_id=account_id,
        pipeline_id=pipeline_id,
        new_name=new_name,
    )
    edge_count = await _clone_edges(
        session,
        snapshot.edges,
        source_id=pipeline_id,
        cloned_id=cloned.id,
        org_id=org_id,
        account_id=account_id,
    )
    node_count = len(snapshot.graph_nodes_json)
    snap_count = await _clone_snapshots(
        session,
        snapshot.snapshots,
        source_id=pipeline_id,
        cloned_id=cloned.id,
        org_id=org_id,
    )

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


async def _clone_pipeline_config(
    session: AsyncSession,
    snapshot: _CloneSourceSnapshot,
    *,
    org_id: uuid.UUID,
    account_id: uuid.UUID,
    pipeline_id: uuid.UUID,
    new_name: str | None,
) -> Pipeline:
    """Build and flush the cloned ``Pipeline`` row from the plain-data snapshot.

    The clone deliberately copies the source's config fields (deep-copying the
    mutable JSON blobs so the clone never shares references with the source),
    giving it a fresh id before any dependent rows (edges, snapshots) are added.
    """
    name = new_name or f"Copy of {snapshot.name}"
    _log.info(
        "Copying pipeline config for %s -> '%s'",
        _sanitise_log_value(pipeline_id),
        _sanitise_log_value(name),
    )
    cloned = Pipeline(
        organisation_id=org_id,
        name=name,
        account_id=account_id,
        description=snapshot.description,
        visibility=snapshot.visibility,
        owner_team_id=snapshot.owner_team_id,
        max_concurrent_runs=snapshot.max_concurrent_runs,
        lock_wait_timeout_seconds=snapshot.lock_wait_timeout_seconds,
        node_timeout_seconds=snapshot.node_timeout_seconds,
        run_context_defaults=copy.deepcopy(snapshot.run_context_defaults),
        graph_nodes_json=copy.deepcopy(snapshot.graph_nodes_json),
        default_autonomy_level=snapshot.default_autonomy_level,
        stale_run_timeout_minutes=snapshot.stale_run_timeout_minutes,
    )
    session.add(cloned)
    await session.flush()
    _log.info("Pipeline config copied: new id=%s", cloned.id)
    return cloned


async def _clone_edges(
    session: AsyncSession,
    edges: list[dict[str, Any]],
    *,
    source_id: uuid.UUID,
    cloned_id: uuid.UUID,
    org_id: uuid.UUID,
    account_id: uuid.UUID,
) -> int:
    """Copy the source's first-class edges onto the clone.

    Returns the number of edges copied. Any gated edge (non-None
    ``hitl_gate_config``) is collected and, if at least one exists, emitted as a
    single batched ``edge_created_with_gate`` audit event after the flush.
    """
    _log.info("Copying edges for pipeline %s -> %s", source_id, cloned_id)
    gated_cloned_edges: list[PipelineEdge] = []
    for edge in edges:
        cloned_edge = PipelineEdge(
            organisation_id=org_id,
            pipeline_id=cloned_id,
            source_node_id=edge["source_node_id"],
            target_node_id=edge["target_node_id"],
            edge_type=edge["edge_type"],
            hitl_gate_config=copy.deepcopy(edge["hitl_gate_config"]),
            source_port=edge.get("source_port", "out"),
            target_port=edge.get("target_port", "in"),
        )
        session.add(cloned_edge)
        if edge["hitl_gate_config"] is not None:
            gated_cloned_edges.append(cloned_edge)
    await session.flush()
    if gated_cloned_edges:
        await append_audit_event(
            session,
            org_id=org_id,
            event_type="edge_created_with_gate",
            actor_user_id=account_id,
            resource_type="pipeline",
            resource_id=cloned_id,
            payload_json={"edge_ids": [str(e.id) for e in gated_cloned_edges]},
        )
    return len(edges)


async def _clone_snapshots(
    session: AsyncSession,
    snapshots: list[dict[str, Any]],
    *,
    source_id: uuid.UUID,
    cloned_id: uuid.UUID,
    org_id: uuid.UUID,
) -> int:
    """Copy the source's snapshots (and their schema pins) onto the clone.

    Returns the number of snapshots copied. The JSON blobs are deep-copied so
    the cloned snapshots are fully independent of the source.
    """
    _log.info("Copying snapshots for pipeline %s -> %s", source_id, cloned_id)
    snap_count = 0
    for snap in snapshots:
        cloned_snap = PipelineSnapshot(
            organisation_id=org_id,
            pipeline_id=cloned_id,
            snapshot_version=snap["snapshot_version"],
            account_id=snap["account_id"],
            environment_profile_id=snap["environment_profile_id"],
            graph_json=copy.deepcopy(snap["graph_json"]),
            connector_bindings_json=copy.deepcopy(snap["connector_bindings_json"]),
            schema_pins_json=copy.deepcopy(snap["schema_pins_json"]),
            prompt_pins_json=copy.deepcopy(snap["prompt_pins_json"]),
            model_backend_pins_json=copy.deepcopy(snap["model_backend_pins_json"]),
            composite_bindings_json=copy.deepcopy(snap["composite_bindings_json"]),
            parameter_bindings_json=copy.deepcopy(snap["parameter_bindings_json"]),
            tag=snap["tag"],
            notes=snap["notes"],
            default_autonomy_level=snap["default_autonomy_level"],
            config_json=copy.deepcopy(snap["config_json"]),
            run_context_defaults=copy.deepcopy(snap["run_context_defaults"]),
        )
        session.add(cloned_snap)
        await session.flush()

        for pin in snap["pins"]:
            session.add(
                SnapshotSchemaPin(
                    organisation_id=org_id,
                    snapshot_id=cloned_snap.id,
                    node_id=pin["node_id"],
                    direction=pin["direction"],
                    schema_id=pin["schema_id"],
                    schema_version=pin["schema_version"],
                )
            )
        snap_count += 1
    return snap_count


def _edge_to_plain_dict(e: PipelineEdge) -> dict[str, Any]:
    """Flatten a ``PipelineEdge`` row into the plain-data shape used by both
    the clone snapshot and the graph-replace read path."""
    return {
        "source_node_id": e.source_node_id,
        "target_node_id": e.target_node_id,
        "edge_type": e.edge_type,
        "hitl_gate_config": copy.deepcopy(e.hitl_gate_config),
        "source_port": e.source_port,
        "target_port": e.target_port,
    }


def _snapshot_pin_to_dict(p: SnapshotSchemaPin) -> dict[str, Any]:
    """Flatten a ``SnapshotSchemaPin`` row into plain data."""
    return {
        "node_id": p.node_id,
        "direction": p.direction,
        "schema_id": p.schema_id,
        "schema_version": p.schema_version,
    }


def _snapshot_to_dict(snap: PipelineSnapshot, pins: list[dict[str, Any]]) -> dict[str, Any]:
    """Flatten a ``PipelineSnapshot`` row into plain data, deep-copying its JSON
    blobs so the clone never shares references with the source."""
    return {
        "snapshot_version": snap.snapshot_version,
        "account_id": snap.account_id,
        "environment_profile_id": snap.environment_profile_id,
        "graph_json": copy.deepcopy(snap.graph_json),
        "connector_bindings_json": copy.deepcopy(snap.connector_bindings_json),
        "schema_pins_json": copy.deepcopy(snap.schema_pins_json),
        "prompt_pins_json": copy.deepcopy(snap.prompt_pins_json),
        "model_backend_pins_json": copy.deepcopy(snap.model_backend_pins_json),
        "composite_bindings_json": copy.deepcopy(snap.composite_bindings_json),
        "parameter_bindings_json": copy.deepcopy(snap.parameter_bindings_json),
        "tag": snap.tag,
        "notes": snap.notes,
        "default_autonomy_level": snap.default_autonomy_level,
        "config_json": copy.deepcopy(snap.config_json),
        "run_context_defaults": copy.deepcopy(snap.run_context_defaults),
        "pins": pins,
    }


def _resolve_read_session_factory(
    session: AsyncSession,
    read_factory: Callable[[], AsyncSession] | None,
) -> tuple[Callable[[], AsyncSession], AsyncEngine | None]:
    """Return ``(factory, read_engine)`` for the clone's step-(a) read session.

    When the caller supplies a *read_factory* it is used as-is (no engine to
    dispose). Otherwise one is derived from the caller's session binding:
    ``session.bind`` is the ``AsyncEngine`` the session was created with;
    ``session.get_bind()`` returns the *sync* ``Engine`` (SQLAlchemy 2.0) which
    ``async_sessionmaker`` rejects ("AsyncEngine expected"). If no usable
    binding exists a ``RuntimeError`` is raised and *read_engine* is None.
    """
    if read_factory is not None:
        return read_factory, None

    bind = session.bind
    if isinstance(bind, AsyncEngine):
        return async_sessionmaker(bind, expire_on_commit=False, class_=AsyncSession), None

    if bind is None:
        try:
            raw = session.get_bind()
        except InvalidRequestError:
            raise RuntimeError("cannot derive an async read URL from the clone source session") from None
        async_url: Any = raw.engine.url if isinstance(raw, Connection) else raw.url
    elif isinstance(bind, AsyncConnection):
        conn = bind.sync_connection
        if conn is None:
            raise RuntimeError("AsyncConnection has no bound sync connection; cannot derive read URL")
        async_url = conn.engine.url
    else:
        async_url = None

    if async_url is None:
        raise RuntimeError("cannot derive an async read URL from the clone source session")
    read_engine = create_async_engine(async_url, poolclass=NullPool)
    return async_sessionmaker(read_engine, expire_on_commit=False, class_=AsyncSession), read_engine


async def _read_clone_source_snapshot(
    session: AsyncSession,
    *,
    org_id: uuid.UUID,
    pipeline_id: uuid.UUID,
    user_id: uuid.UUID | None,
    org_role: str | None,
    read_factory: Callable[[], AsyncSession] | None,
    on_step_a_held: Callable[[], Awaitable[None]] | None,
) -> _CloneSourceSnapshot | None:
    """Step (a): short transaction that FOR SHARE-locks the source pipeline row,
    reads nodes/edges/snapshots into plain data, and commits immediately.

    The read session is a separate connection/pool checkout, so it must
    re-apply the caller's full RLS context: ``set_rls_org`` scopes the org, and
    ``set_rls_user_context`` (when *org_role* is given) makes team-scoped
    pipelines visible via ``app.user_id`` / ``app.org_role``.
    """
    factory, read_engine = _resolve_read_session_factory(session, read_factory)

    try:
        async with factory() as read_session, read_session.begin():
            await set_rls_org(read_session, org_id)
            # The guard is intentionally more permissive than the endpoint,
            # which always passes a non-None org_role (TenantPrincipal). Non-API
            # callers and unit tests may omit it, so only re-apply the user
            # context when both identity parts are available.
            if user_id is not None and org_role is not None:
                await set_rls_user_context(read_session, user_id, org_role)
            src_result = await read_session.execute(
                select(Pipeline)
                .where(Pipeline.id == pipeline_id, Pipeline.deleted_at.is_(None))
                .with_for_update(read=True)
            )
            source = src_result.scalar_one_or_none()
            if source is None:
                return None
            if on_step_a_held is not None:
                await on_step_a_held()

            edges = [
                _edge_to_plain_dict(e)
                for e in (
                    await read_session.execute(
                        select(PipelineEdge)
                        .where(PipelineEdge.pipeline_id == pipeline_id)
                        .order_by(PipelineEdge.created_at, PipelineEdge.id)
                    )
                ).scalars()
            ]
            snap_rows = list(
                (
                    await read_session.execute(
                        select(PipelineSnapshot)
                        .where(PipelineSnapshot.pipeline_id == pipeline_id)
                        .order_by(PipelineSnapshot.snapshot_version)
                    )
                ).scalars()
            )
            snapshots: list[dict[str, Any]] = []
            for snap in snap_rows:
                pins = [
                    _snapshot_pin_to_dict(p)
                    for p in (
                        await read_session.execute(
                            select(SnapshotSchemaPin).where(SnapshotSchemaPin.snapshot_id == snap.id)
                        )
                    ).scalars()
                ]
                snapshots.append(_snapshot_to_dict(snap, pins))

            return _CloneSourceSnapshot(
                name=source.name,
                description=source.description,
                visibility=source.visibility,
                owner_team_id=source.owner_team_id,
                max_concurrent_runs=source.max_concurrent_runs,
                lock_wait_timeout_seconds=source.lock_wait_timeout_seconds,
                node_timeout_seconds=source.node_timeout_seconds,
                run_context_defaults=copy.deepcopy(source.run_context_defaults),
                graph_nodes_json=copy.deepcopy(list(source.graph_nodes_json or [])),
                default_autonomy_level=str(source.default_autonomy_level or "manual_approval"),
                stale_run_timeout_minutes=source.stale_run_timeout_minutes,
                edges=edges,
                snapshots=snapshots,
            )
    finally:
        if read_engine is not None:
            await read_engine.dispose()


def _preserve_omitted_gate_config(
    edge: dict[str, Any],
    old_by_key: dict[tuple[str, str, str], Any],
) -> Any:
    """Resolve the ``hitl_gate_config`` to persist for a proposed edge.

    Mirrors ``hitl_gate_guard._normalize_edge`` presence semantics: when the
    client omits the ``hitl_gate_config`` key (or sends
    ``hitl_gate_config_present=False``) for an edge whose topology key matches
    a pre-existing gated edge, the stored value is preserved; for an edge with
    no prior gate the omission persists ``None``. Any ``hitl_gate_config`` value
    carried alongside an omission signal is ignored, exactly as the guard
    ignores it — a client cannot sneak a gate value past an explicit
    ``present=False``. The delete+reinsert write path must honour the guard's
    "omission = preserve" contract (hitl-gate-removal-guard-plan.md §3 item 6)
    — otherwise a client that simply omits the key would silently wipe the
    gate with zero audit.
    """
    present = edge.get("hitl_gate_config_present", "hitl_gate_config" in edge)
    if present:
        return edge.get("hitl_gate_config")
    key = (
        str(edge["source_node_id"]),
        str(edge["target_node_id"]),
        str(edge["edge_type"]),
    )
    return old_by_key.get(key)


async def replace_pipeline_graph(
    session: AsyncSession,
    *,
    pipeline_id: uuid.UUID,
    org_id: uuid.UUID,
    nodes: list[dict[str, Any]],
    edges: list[dict[str, Any]],
    is_privileged: bool,
    caller_type: Literal["rest", "mcp"],
    account_id: uuid.UUID | None = None,
    is_guardrail_admin: bool = False,
    _on_lock_acquired: Callable[[], Awaitable[None]] | None = None,
) -> tuple[list[dict[str, Any]], list[PipelineEdge]] | None:
    """Atomically replace an editable graph while preserving first-class edges.

    ADR 017 service-layer backstop + hitl-gate-removal-guard-plan.md v19: the
    HITL gate guard runs here, under the row lock and BEFORE any delete/insert.
    ``caller_type`` is required (no default); ``"mcp"`` forces ``is_privileged``
    to False with no live-role query. For ``"rest"`` with ``account_id`` the
    caller's live org role is re-read under the lock (fail-closed on DB error,
    no retry). A gate-weakening write by a non-privileged caller raises
    ``HitlGateWeakeningDenied`` before the delete/insert executes.

    FAR-309 PR A review: the guardrail-binding strip guard
    (``enforce_guardrail_binding_strip``) runs here too, under the same row
    lock — a non-admin cannot strip a guardrail binding by removing a
    guardrail-bound node. ``is_guardrail_admin`` is the caller-supplied admin
    flag (admin-level, the ``guardrail.manage`` privilege — distinct from the
    operator+ ``is_privileged``); for ``"rest"`` with ``account_id`` the live
    role is re-read under the lock.
    """
    result = await session.execute(
        select(Pipeline).where(Pipeline.id == pipeline_id, Pipeline.deleted_at.is_(None)).with_for_update()
    )
    pipeline = result.scalar_one_or_none()
    if pipeline is None:
        return None

    if _on_lock_acquired is not None:
        await _on_lock_acquired()

    effective_privileged = await resolve_effective_privilege(
        session,
        org_id=org_id,
        account_id=account_id,
        is_privileged=is_privileged,
        caller_type=caller_type,
    )

    # Snapshot current edges into plain data BEFORE any write (defense in depth).
    old_rows = list(
        (await session.execute(select(PipelineEdge).where(PipelineEdge.pipeline_id == pipeline_id))).scalars()
    )
    old_edges: list[dict[str, Any]] = [
        {
            "source_node_id": str(e.source_node_id),
            "target_node_id": str(e.target_node_id),
            "edge_type": e.edge_type,
            "hitl_gate_config": copy.deepcopy(e.hitl_gate_config),
            "source_port": getattr(e, "source_port", "out"),
            "target_port": getattr(e, "target_port", "in"),
        }
        for e in old_rows
    ]

    # FAR-309 PR A review: service-layer guardrail-binding strip guard. A
    # non-admin may not remove a guardrail-bound node (that would drop the
    # binding). Runs under the row lock, before any graph mutation.
    await enforce_guardrail_binding_strip(
        session,
        pipeline_id=pipeline_id,
        org_id=org_id,
        incoming_node_ids={str(node.get("id")) for node in nodes if node.get("id")},
        is_guardrail_admin=is_guardrail_admin,
        caller_type=caller_type,
        account_id=account_id,
    )

    diff = await apply_gated_edge_diff(
        session,
        old_edges,
        edges,
        is_privileged=effective_privileged,
        caller_type=caller_type,
    )
    if diff.denied:
        raise HitlGateWeakeningDenied(
            reason_code=diff.reason_code or "insufficient-role",
            correlation_keys=[w.correlation_key for w in diff.weakened_edges],
            weakening_types=sorted({t for w in diff.weakened_edges for t in w.weakening_types}),
            detail=denial_detail(diff),
            payload_json=build_gate_diff_payload(diff, caller_type),
        )
    if diff.has_weakening:
        await append_audit_event(
            session,
            org_id=org_id,
            event_type="hitl_gate_removed",
            actor_user_id=account_id,
            resource_type="pipeline",
            resource_id=pipeline_id,
            payload_json=build_gate_diff_payload(diff, caller_type),
        )

    pipeline.graph_nodes_json = nodes
    await session.execute(delete(PipelineEdge).where(PipelineEdge.pipeline_id == pipeline_id))
    old_by_key = {
        (str(e["source_node_id"]), str(e["target_node_id"]), str(e["edge_type"])): e["hitl_gate_config"]
        for e in old_edges
        if e.get("hitl_gate_config") is not None
    }
    # Coerce edge id/source/target to uuid.UUID objects. The REST Pydantic path
    # already does this, but MCP passes raw dicts with string ids — and SQLAlchemy's
    # insertmanyvalues sentinel matching (INSERT ... RETURNING) requires UUID
    # objects, not strings, to match the returned sentinel. Without coercion a
    # 2+ edge graph save raises InvalidRequestError (MCP update_pipeline_graph
    # internal_error).
    persisted_edges = [
        PipelineEdge(
            id=uuid.UUID(str(edge["id"])),
            organisation_id=org_id,
            pipeline_id=pipeline_id,
            source_node_id=uuid.UUID(str(edge["source_node_id"])),
            target_node_id=uuid.UUID(str(edge["target_node_id"])),
            edge_type=edge["edge_type"],
            hitl_gate_config=_preserve_omitted_gate_config(edge, old_by_key),
            source_port=edge.get("source_port", "out"),
            target_port=edge.get("target_port", "in"),
        )
        for edge in edges
    ]
    session.add_all(persisted_edges)
    await session.flush()
    return list(pipeline.graph_nodes_json), persisted_edges
