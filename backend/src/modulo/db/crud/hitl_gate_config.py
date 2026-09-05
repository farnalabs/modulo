"""Resolve a HITL gate's ``hitl_gate_config`` from the gate's actual edge.

Shared by the REST HITL decision routes (``api/routes/hitl.py``) and the MCP
``review_hitl`` tool (``api/mcp_server.py``). Gate ids are derived from the
gated edge's topology — ``hitl_gate_<source>_<target>`` (see
``graph_cache._make_gate_id``) — so a gate's config is found by matching that
topology, never by positional edge order.

FAR-610: the MCP human-only check used to select the pipeline's edges with no
source/target filter and read ``.scalars().first()`` — the FIRST edge in
arbitrary order. On the PR Reviewer pipeline the first edge carried no
``hitl_gate_config``, so the check passed and API-key clients approved a
``human_only`` gate 22+ times. This module is the single resolution point so
both surfaces agree on which edge a gate id refers to.

Resolution order (callers already set the RLS org context; the queries here
add explicit ``organisation_id`` filters as defence in depth):

1. PRIMARY — the run's immutable ``PipelineSnapshot``: walk ``graph_json``
   edges, compute ``hitl_gate_{source}_{target}`` for every edge carrying a
   ``hitl_gate_config`` (both ``source``/``target`` and the persisted
   ``source_node_id``/``target_node_id`` key styles), and return the config of
   the edge whose derived id equals the gate id. The snapshot is the graph the
   run actually executes, so it is authoritative for the gate that fired.
2. FALLBACK — snapshot missing or legacy (gate config absent from the
   snapshot graph): parse source/target out of the gate id and look the edge
   up in the LIVE ``pipeline_edges`` table. Node ids are UUIDs (hyphens, no
   underscores), so the gate id splits from the RIGHT into exactly two
   segments; live edge node-id columns are UUIDs, so non-UUID node ids cannot
   match and the fallback yields None.

Unresolvable gates return ``None`` (fail-open, preserving the historical
behaviour for gates whose config cannot be located). Callers decide the
enforcement policy.
"""

import uuid
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from modulo.db.crud.run import get_run
from modulo.db.models.pipeline_edge import PipelineEdge
from modulo.db.models.pipeline_snapshot import PipelineSnapshot
from modulo.db.models.run import Run

_GATE_ID_PREFIX = "hitl_gate_"


def edge_source_or_target(edge: dict[str, Any], key: str) -> str | None:
    """Resolve an edge's source/target node id (canonical + persisted keys).

    Snapshot graphs store ``source``/``target``; some legacy/persisted shapes
    use ``source_node_id``/``target_node_id``. Mirrors
    ``graph_cache._get_edge_val`` but returns None instead of raising when the
    edge omits the field, so a malformed snapshot edge never breaks resolution.
    """
    value = edge.get(key) or edge.get(f"{key}_node_id")
    return str(value) if value is not None else None


def parse_hitl_gate_id(gate_id: str) -> tuple[str, str] | None:
    """Split ``hitl_gate_<source>_<target>`` into ``(source, target)``.

    Node ids are UUIDs (hyphenated, no underscores), so the remainder splits
    from the RIGHT into exactly two segments. Returns None for non-gate ids
    (e.g. manual-node ids), malformed ids, or node ids containing underscores
    (ambiguous — the snapshot primary path is authoritative for those).
    """
    if not gate_id.startswith(_GATE_ID_PREFIX):
        return None
    parts = gate_id[len(_GATE_ID_PREFIX) :].rsplit("_", 2)
    if len(parts) != 2:
        return None
    source, target = parts
    if not source or not target:
        return None
    return source, target


def _config_from_graph(graph_json: dict[str, Any], gate_id: str) -> dict[str, Any] | None:
    """Find the edge whose derived gate id matches ``gate_id`` in a snapshot graph."""
    for edge in graph_json.get("edges", []):
        if not isinstance(edge, dict):
            continue
        config = edge.get("hitl_gate_config")
        if not isinstance(config, dict):
            continue
        source = edge_source_or_target(edge, "source")
        target = edge_source_or_target(edge, "target")
        if source is not None and target is not None and f"{_GATE_ID_PREFIX}{source}_{target}" == gate_id:
            return config
    return None


async def _config_from_live_edges(
    session: AsyncSession,
    pipeline_id: uuid.UUID,
    org_id: uuid.UUID,
    gate_id: str,
) -> dict[str, Any] | None:
    """Look the gate's edge up in the LIVE ``pipeline_edges`` table (fallback)."""
    parsed = parse_hitl_gate_id(gate_id)
    if parsed is None:
        return None
    source, target = parsed
    try:
        source_uuid = uuid.UUID(source)
        target_uuid = uuid.UUID(target)
    except ValueError:
        # Live pipeline_edges store UUID node ids; a gate id built from
        # non-UUID node ids cannot match a live edge.
        return None
    edge = (
        await session.execute(
            select(PipelineEdge).where(
                PipelineEdge.pipeline_id == pipeline_id,
                PipelineEdge.organisation_id == org_id,
                PipelineEdge.source_node_id == source_uuid,
                PipelineEdge.target_node_id == target_uuid,
            )
        )
    ).scalar_one_or_none()
    if edge is not None and isinstance(edge.hitl_gate_config, dict):
        return dict(edge.hitl_gate_config)
    return None


async def resolve_hitl_gate_config(
    session: AsyncSession,
    *,
    run_id: uuid.UUID,
    gate_id: str,
    org_id: uuid.UUID,
    run: Run | None = None,
) -> dict[str, Any] | None:
    """Return the gate's ``hitl_gate_config`` dict, or None when unresolvable.

    Callers already set ``set_rls_org``; the snapshot and live-edge queries add
    an explicit ``organisation_id`` filter as defence in depth. Pass the
    already-loaded ``run`` to skip the redundant run read (the MCP flow loads
    the run for its team-scope boundary check anyway).
    """
    if run is None:
        run = await get_run(session, run_id, organisation_id=org_id)
        if run is None:
            return None

    snapshot_id = run.snapshot_id
    if snapshot_id is not None:
        snapshot = (
            await session.execute(
                select(PipelineSnapshot).where(
                    PipelineSnapshot.id == snapshot_id,
                    PipelineSnapshot.organisation_id == org_id,
                )
            )
        ).scalar_one_or_none()
        if snapshot is not None and isinstance(snapshot.graph_json, dict):
            config = _config_from_graph(snapshot.graph_json, gate_id)
            if config is not None:
                return config

    return await _config_from_live_edges(session, run.pipeline_id, org_id, gate_id)
