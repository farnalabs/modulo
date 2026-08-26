"""CRUD for pipeline snapshot versioning — list, tag, rollback, diff."""

import copy
import uuid
from collections.abc import Awaitable, Callable
from typing import Any, Literal

from sqlalchemy import delete as sa_delete
from sqlalchemy import func, select
from sqlalchemy.exc import ProgrammingError
from sqlalchemy.ext.asyncio import AsyncSession

from modulo.core.audit_logger import append_audit_event
from modulo.core.pipeline_impact import (
    check_port_change_breaking,
    compute_port_change_impact,
    diff_edge_ports,
    diff_node_ports,
    normalise_edge_port_delta,
)
from modulo.db.crud.hitl_gate_guard import (
    HitlGateWeakeningDenied,
    apply_gated_edge_diff,
    build_gate_diff_payload,
    denial_detail,
    enforce_guardrail_binding_strip,
    resolve_effective_privilege,
)
from modulo.db.crud.pipeline_snapshot import create_snapshot_from_live_graph
from modulo.db.models.pipeline import Pipeline
from modulo.db.models.pipeline_edge import PipelineEdge
from modulo.db.models.pipeline_snapshot import PipelineSnapshot


async def delete_snapshot(
    session: AsyncSession,
    snapshot_id: uuid.UUID,
) -> bool:
    """Delete a historical snapshot. Refuses to delete the latest snapshot."""
    target = await get_snapshot(session, snapshot_id)
    if target is None:
        return False

    result = await session.execute(
        select(PipelineSnapshot)
        .where(PipelineSnapshot.pipeline_id == target.pipeline_id)
        .order_by(PipelineSnapshot.snapshot_version.desc())
        .limit(1)
    )
    latest = result.scalar_one_or_none()
    if latest is not None and latest.id == target.id:
        return False

    await session.delete(target)
    await session.flush()
    return True


async def get_snapshot_detail(
    session: AsyncSession,
    snapshot_id: uuid.UUID,
    *,
    organisation_id: uuid.UUID | None = None,
    pipeline_id: uuid.UUID | None = None,
) -> PipelineSnapshot | None:
    """Get a single snapshot with full graph detail.

    Explicit org/pipeline scoping is defense-in-depth on top of RLS: when the
    caller has tenant/pipeline context, pass it so a snapshot belonging to
    another org (or another pipeline) can never be returned even if RLS
    context is missing or misconfigured.
    """
    stmt = select(PipelineSnapshot).where(PipelineSnapshot.id == snapshot_id)
    if organisation_id is not None:
        stmt = stmt.where(PipelineSnapshot.organisation_id == organisation_id)
    if pipeline_id is not None:
        stmt = stmt.where(PipelineSnapshot.pipeline_id == pipeline_id)
    result = await session.execute(stmt)
    return result.scalar_one_or_none()


async def list_snapshots(
    session: AsyncSession,
    pipeline_id: uuid.UUID,
    page: int = 1,
    page_size: int = 20,
) -> tuple[list[PipelineSnapshot], int]:
    """List snapshots for a pipeline ordered by version descending.

    Returns (snapshots, total_count) where total_count is the total
    number of snapshots for the pipeline across all pages.
    """
    offset = (page - 1) * page_size
    result = await session.execute(
        select(PipelineSnapshot)
        .join(Pipeline, PipelineSnapshot.pipeline_id == Pipeline.id)
        .where(
            PipelineSnapshot.pipeline_id == pipeline_id,
            Pipeline.deleted_at.is_(None),
        )
        .order_by(PipelineSnapshot.snapshot_version.desc())
        .offset(offset)
        .limit(page_size)
    )
    snapshots = list(result.scalars())

    # Get total count
    try:
        count_result = await session.execute(
            select(func.count())
            .select_from(PipelineSnapshot)
            .join(Pipeline, PipelineSnapshot.pipeline_id == Pipeline.id)
            .where(
                PipelineSnapshot.pipeline_id == pipeline_id,
                Pipeline.deleted_at.is_(None),
            )
        )
        total = count_result.scalar() or 0
    except ProgrammingError:
        return [], 0

    return snapshots, total


async def get_snapshot(
    session: AsyncSession,
    snapshot_id: uuid.UUID,
) -> PipelineSnapshot | None:
    """Get a single snapshot by ID."""
    result = await session.execute(select(PipelineSnapshot).where(PipelineSnapshot.id == snapshot_id))
    return result.scalar_one_or_none()


async def resolve_snapshot_for_channel(
    session: AsyncSession,
    *,
    pipeline_id: uuid.UUID,
    channel: str,
    organisation_id: uuid.UUID | None = None,
) -> PipelineSnapshot | None:
    """Resolve the LATEST snapshot that was created under a release channel.

    FAR-402 P6 release-channel hook: a trigger binding a channel resolves to the
    latest snapshot for that channel, so a canary-tagged run executes the
    newest canary version rather than the live graph. Returns ``None`` when no
    snapshot is tagged for the channel (or the pipeline/org scoping excludes it)
    — the caller then falls back to pinning the live graph.
    """
    stmt = (
        select(PipelineSnapshot)
        .where(
            PipelineSnapshot.pipeline_id == pipeline_id,
            PipelineSnapshot.channel == channel,
            PipelineSnapshot.draft.is_(False),
        )
        .order_by(PipelineSnapshot.snapshot_version.desc())
        .limit(1)
    )
    if organisation_id is not None:
        stmt = stmt.where(PipelineSnapshot.organisation_id == organisation_id)
    result = await session.execute(stmt)
    return result.scalar_one_or_none()


async def tag_snapshot(
    session: AsyncSession,
    snapshot_id: uuid.UUID,
    tag: str | None = None,
    notes: str | None = None,
) -> PipelineSnapshot | None:
    """Set or clear tag and notes on a snapshot."""
    snapshot = await get_snapshot(session, snapshot_id)
    if snapshot is None:
        return None
    if tag is not None:
        snapshot.tag = tag
    if notes is not None:
        snapshot.notes = notes
    await session.flush()
    return snapshot


async def rollback_to_snapshot(
    session: AsyncSession,
    pipeline_id: uuid.UUID,
    target_snapshot_id: uuid.UUID,
    account_id: uuid.UUID | None = None,
    *,
    is_privileged: bool,
    caller_type: Literal["rest", "mcp"],
    is_guardrail_admin: bool = False,
    _on_lock_acquired: Callable[[], Awaitable[None]] | None = None,
) -> PipelineSnapshot | None:
    """Create a new snapshot that restores the graph from a previous snapshot.

    ADR 017 service-layer backstop + hitl-gate-removal-guard-plan.md v19: the
    HITL gate guard runs here, under the row lock and BEFORE the graph mutation.
    A historical snapshot's missing/``None`` gate fields are fail-closed
    (treated as weakening) with the distinct reason code
    ``legacy-snapshot-ambiguous``. ``caller_type == "mcp"`` forces
    ``is_privileged`` to False.

    FAR-309 PR A review: the guardrail-binding strip guard runs here too, under
    the same row lock — a non-admin may not roll back to a snapshot whose graph
    LACKS a node that currently carries a bound guardrail (rolling back would
    strip the binding). ``is_guardrail_admin`` is the caller-supplied admin
    flag; for ``"rest"`` with ``account_id`` the live role is re-read under the
    lock.

    Does not affect in-flight runs (they continue on their original snapshot).
    Returns the new snapshot, or None if the target snapshot doesn't exist.
    """
    target = await get_snapshot(session, target_snapshot_id)
    if target is None or target.pipeline_id != pipeline_id:
        return None

    pipeline_result = await session.execute(select(Pipeline).where(Pipeline.id == pipeline_id).with_for_update())
    pipeline = pipeline_result.scalar_one_or_none()
    if pipeline is None:
        return None

    if _on_lock_acquired is not None:
        await _on_lock_acquired()

    effective_privileged = await resolve_effective_privilege(
        session,
        org_id=pipeline.organisation_id,
        account_id=account_id,
        is_privileged=is_privileged,
        caller_type=caller_type,
    )

    old_rows = list(
        (await session.execute(select(PipelineEdge).where(PipelineEdge.pipeline_id == pipeline_id))).scalars()
    )
    old_edges: list[dict[str, Any]] = [
        {
            "source_node_id": str(e.source_node_id),
            "target_node_id": str(e.target_node_id),
            "edge_type": e.edge_type,
            "hitl_gate_config": copy.deepcopy(e.hitl_gate_config),
        }
        for e in old_rows
    ]

    # FAR-309 PR A review: service-layer guardrail-binding strip guard. A
    # non-admin may not roll back to a snapshot that drops a guardrail-bound
    # node (that would strip the binding). Runs under the row lock, before any
    # graph mutation.
    snapshot_nodes = target.graph_json.get("nodes", []) if isinstance(target.graph_json, dict) else []
    await enforce_guardrail_binding_strip(
        session,
        pipeline_id=pipeline_id,
        org_id=pipeline.organisation_id,
        incoming_node_ids={str(node.get("id")) for node in snapshot_nodes if node.get("id")},
        is_guardrail_admin=is_guardrail_admin,
        caller_type=caller_type,
        account_id=account_id,
    )
    # Historical snapshots: missing/None fields are fail-closed — a snapshot
    # edge that omits a gate field (or carries None) is treated as a genuine
    # removal when the live edge carried a gate.
    new_edges: list[dict[str, Any]] = [
        {
            "source_node_id": edge_data.get("source") or edge_data.get("source_node_id", ""),
            "target_node_id": edge_data.get("target") or edge_data.get("target_node_id", ""),
            "edge_type": edge_data.get("edge_type", edge_data.get("type", "normal")),
            "hitl_gate_config": edge_data.get("hitl_gate_config"),
            "hitl_gate_config_present": True,
        }
        for edge_data in target.graph_json.get("edges", [])
    ]

    diff = await apply_gated_edge_diff(
        session,
        old_edges,
        new_edges,
        is_privileged=effective_privileged,
        caller_type=caller_type,
        legacy_snapshot=True,
    )
    if diff.denied:
        raise HitlGateWeakeningDenied(
            reason_code=diff.reason_code or "legacy-snapshot-ambiguous",
            correlation_keys=[w.correlation_key for w in diff.weakened_edges],
            weakening_types=sorted({t for w in diff.weakened_edges for t in w.weakening_types}),
            detail=denial_detail(diff),
            payload_json=build_gate_diff_payload(diff, caller_type),
        )
    if diff.has_weakening:
        await append_audit_event(
            session,
            org_id=pipeline.organisation_id,
            event_type="hitl_gate_removed",
            actor_user_id=account_id,
            resource_type="pipeline",
            resource_id=pipeline_id,
            payload_json=build_gate_diff_payload(diff, caller_type),
        )

    pipeline.graph_nodes_json = copy.deepcopy(target.graph_json.get("nodes", []))
    await session.execute(sa_delete(PipelineEdge).where(PipelineEdge.pipeline_id == pipeline_id))
    for edge_data in new_edges:
        new_edge = PipelineEdge(
            organisation_id=pipeline.organisation_id,
            pipeline_id=pipeline_id,
            source_node_id=edge_data["source_node_id"],
            target_node_id=edge_data["target_node_id"],
            edge_type=edge_data["edge_type"],
            hitl_gate_config=edge_data["hitl_gate_config"],
            source_port=edge_data.get("source_port", "out"),
            target_port=edge_data.get("target_port", "in"),
        )
        session.add(new_edge)
    await session.flush()

    new_snapshot = await create_snapshot_from_live_graph(
        session,
        pipeline_id=pipeline_id,
        account_id=account_id,
        # FAR-402 P6: a rollback produces a live-edit version that is
        # provenance-discriminated as a role-back pointer swap.
        version_kind="edit",
        created_kind="rollback",
    )
    if new_snapshot is not None:
        new_snapshot.tag = f"rollback-v{target.snapshot_version}"
        new_snapshot.notes = f"Rollback to snapshot version {target.snapshot_version}"
        await session.flush()

    return new_snapshot


def _compute_node_changes(na: dict[str, Any], nb: dict[str, Any]) -> dict[str, Any]:
    changes: dict[str, Any] = {}
    for key in ("agent_id", "label", "node_type"):
        if na.get(key) != nb.get(key):
            changes[key] = {"old": na.get(key), "new": nb.get(key)}
    for key in ("output_schema_id",):
        if na.get(key) != nb.get(key):
            changes["schema_id"] = {"old": na.get(key), "new": nb.get(key)}
    if na.get("connector_binding") != nb.get("connector_binding"):
        changes["connector_binding"] = {
            "old": na.get("connector_binding"),
            "new": nb.get("connector_binding"),
        }
    if na.get("environment_binding") != nb.get("environment_binding"):
        changes["environment_binding"] = {
            "old": na.get("environment_binding"),
            "new": nb.get("environment_binding"),
        }
    # FAR-402 P6: surface PORT-SIGNATURE deltas (inputs/outputs port names +
    # schema_refs) alongside the scalar field changes.
    port_changes = diff_node_ports(na, nb)
    if port_changes:
        changes["ports"] = port_changes
    return changes


async def diff_snapshots(
    session: AsyncSession,
    snapshot_id_a: uuid.UUID,
    snapshot_id_b: uuid.UUID,
) -> dict[str, Any] | None:
    """Compare two snapshots and return structural differences with per-field changes."""
    a = await get_snapshot(session, snapshot_id_a)
    b = await get_snapshot(session, snapshot_id_b)
    if a is None or b is None:
        return None

    nodes_a = {n["id"]: n for n in a.graph_json.get("nodes", [])}
    nodes_b = {n["id"]: n for n in b.graph_json.get("nodes", [])}
    ids_a = set(nodes_a)
    ids_b = set(nodes_b)

    added_nodes = [nodes_b[nid] for nid in ids_b - ids_a]
    removed_nodes = [nodes_a[nid] for nid in ids_a - ids_b]
    modified_nodes = []
    for nid in ids_a & ids_b:
        na = nodes_a[nid]
        nb = nodes_b[nid]
        changes = _compute_node_changes(na, nb)
        if changes:
            modified_nodes.append({"node_id": nid, "changes": changes})

    edges_a = {}
    for e in a.graph_json.get("edges", []):
        key = (
            e.get("source") or e.get("source_node_id"),
            e.get("target") or e.get("target_node_id"),
        )
        edges_a[key] = e
    edges_b = {}
    for e in b.graph_json.get("edges", []):
        key = (
            e.get("source") or e.get("source_node_id"),
            e.get("target") or e.get("target_node_id"),
        )
        edges_b[key] = e

    keys_a = set(edges_a)
    keys_b = set(edges_b)
    added_edges = [edges_b[k] for k in keys_b - keys_a]
    removed_edges = [edges_a[k] for k in keys_a - keys_b]
    modified_edges = []
    for k in keys_a & keys_b:
        ea = edges_a[k]
        eb = edges_b[k]
        edge_changes = {}
        for ekey in ("edge_type", "type"):
            if ea.get(ekey) != eb.get(ekey):
                edge_changes["edge_type"] = {"old": ea.get(ekey), "new": eb.get(ekey)}
        if ea.get("hitl_gate_config") != eb.get("hitl_gate_config"):
            edge_changes["hitl_gate_config"] = {
                "old": ea.get("hitl_gate_config"),
                "new": eb.get("hitl_gate_config"),
            }
        # FAR-402 P6: surface source_port/target_port deltas.
        edge_ports = diff_edge_ports(ea, eb)
        if edge_ports:
            edge_changes["ports"] = edge_ports
        if edge_changes:
            source = k[0]
            target = k[1]
            modified_edges.append(
                {
                    "edge": {"source": source, "target": target},
                    "changes": edge_changes,
                }
            )

    def _rebuild_graph(snapshot: PipelineSnapshot) -> dict[str, Any]:
        graph = snapshot.graph_json
        return {
            "nodes": [
                {
                    "id": n["id"],
                    "agent_id": n.get("agent_id"),
                    "label": n.get("label"),
                    "node_type": n.get("node_type", "agent"),
                    "position": n.get("position", {"x": 0, "y": 0}),
                    "output_schema_id": n.get("output_schema_id"),
                    "connector_binding": n.get("connector_binding"),
                    "environment_binding": n.get("environment_binding"),
                    "inputs": n.get("inputs"),
                    "outputs": n.get("outputs"),
                }
                for n in graph.get("nodes", [])
            ],
            "edges": [
                {
                    "id": e.get("id"),
                    "source_node_id": e.get("source") or e.get("source_node_id", ""),
                    "target_node_id": e.get("target") or e.get("target_node_id", ""),
                    "edge_type": e.get("edge_type", e.get("type", "normal")),
                    "hitl_gate_config": e.get("hitl_gate_config"),
                    "source_port": e.get("source_port"),
                    "target_port": e.get("target_port"),
                }
                for e in graph.get("edges", [])
            ],
        }

    # FAR-402 P6: semantic layer — aggregate port-signature deltas across the
    # node/edge sets, then run the deterministic impact oracle. ``port_changes``
    # is the union of per-node and per-edge port deltas; ``impacted_nodes`` is
    # the downstream reachability projection (which nodes a port change breaks);
    # ``breaking_changes`` flags port changes that would drop/alter data read by
    # a downstream edge (severity ``block`` vs ``warning``).
    port_changes: list[dict[str, Any]] = []
    for m in modified_nodes:
        port_changes += m.get("changes", {}).get("ports", [])
    for m in modified_edges:
        # ``changes["ports"]`` is a ``diff_edge_ports`` delta dict (no node_id),
        # so normalise it to the node-change shape the oracle can consume.
        edge_port_delta: Any = m.get("changes", {}).get("ports")
        if edge_port_delta:
            port_changes += normalise_edge_port_delta(m["edge"], edge_port_delta)
    for node in added_nodes:
        port_changes += diff_node_ports({}, node)
    for edge in added_edges:
        delta = diff_edge_ports({}, edge)
        if delta:
            port_changes += normalise_edge_port_delta(edge, delta)

    graph_b_for_impact: dict[str, Any] = {
        "nodes": b.graph_json.get("nodes", []),
        "edges": b.graph_json.get("edges", []),
    }
    impacted_nodes: set[str] = set()
    breaking_changes: list[dict[str, Any]] = []
    if port_changes:
        impacted_nodes = compute_port_change_impact(graph_b_for_impact, port_changes)
        breaking_changes = check_port_change_breaking(graph_b_for_impact, port_changes)

    return {
        "snapshot_a": {
            "id": str(a.id),
            "version": a.snapshot_version,
            "tag": a.tag,
            "created_at": a.created_at.isoformat() if a.created_at else None,
            "graph": _rebuild_graph(a),
        },
        "snapshot_b": {
            "id": str(b.id),
            "version": b.snapshot_version,
            "tag": b.tag,
            "created_at": b.created_at.isoformat() if b.created_at else None,
            "graph": _rebuild_graph(b),
        },
        "nodes_added": added_nodes,
        "nodes_removed": removed_nodes,
        "nodes_modified": modified_nodes,
        "edges_added": added_edges,
        "edges_removed": removed_edges,
        "edges_modified": modified_edges,
        "semantic": {
            "port_changes": port_changes,
            "impacted_nodes": sorted(impacted_nodes),
            "breaking_changes": breaking_changes,
        },
    }
