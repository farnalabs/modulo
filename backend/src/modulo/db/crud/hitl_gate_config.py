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

0. STAMP (FAR-634) — for parseable gate ids (``hitl_gate_<source>_<target>``)
   the executor stamps the resolved config onto ``hitl_claims.
   gate_config_json`` at fire time, so a fired gate's config is read in ONE
   claim-row lookup. Manual-node ids skip the lookup (no claim row exists for
   them). The walk below remains the fallback for legacy rows (no stamp),
   gates that never fired, and fire-time stamp failures (NULL config).
1. PRIMARY — the run's immutable ``PipelineSnapshot``: walk ``graph_json``
   edges, compute ``hitl_gate_{source}_{target}`` for every edge carrying a
   ``hitl_gate_config`` (both ``source``/``target`` and the persisted
   ``source_node_id``/``target_node_id`` key styles), and return the config of
   the edge whose derived id equals the gate id. The snapshot is the graph the
   run actually executes, so it is authoritative for the gate that fired.
   If no edge matches, walk the snapshot's NODES for a FAR-402 HITL node
   whose outgoing edges derive the gate id — HITL nodes carry ``hitl_config``
   on the NODE and the compiler injects it onto edges at build time only, so
   node-level gates have no edge-level config in the persisted definition.
2. FALLBACK — snapshot missing or legacy (gate config absent from the
   snapshot graph): parse source/target out of the gate id and look the edge
   up in the LIVE ``pipeline_edges`` table, filtered to normal edges — a
   reject/conditional edge may share the gate's (source, target) topology
   (``uq_pipeline_edges_path`` includes ``edge_type``) and only normal edges
   carry gate config. Node ids are UUIDs (hyphens, no
   underscores), so the gate id splits from the RIGHT into exactly two
   segments; live edge node-id columns are UUIDs, so non-UUID node ids cannot
   match and the fallback yields None. If the live edge has no config either,
   consult the live pipeline's ``graph_nodes_json`` for the FAR-402 HITL-node
   config (same derivation as the snapshot node walk).

Unresolvable gates return ``None``. Callers enforce the human_only policy
from the resolved config; for the residual unresolvable case they consult
:func:`hitl_gate_exists_but_unresolved` so a gate that FIRED but whose
config cannot be located is treated as policy-unverifiable (fail closed)
rather than silently allowed.
"""

import uuid
from collections.abc import Sequence
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from modulo.db.crud.run import get_run
from modulo.db.models.hitl_claim import HitlClaim
from modulo.db.models.pipeline import Pipeline
from modulo.db.models.pipeline_edge import PipelineEdge
from modulo.db.models.pipeline_snapshot import PipelineSnapshot
from modulo.db.models.run import Run

GATE_ID_PREFIX = "hitl_gate_"


def make_gate_id(source: str, target: str) -> str:
    """Derive a gate id from an edge's topology: ``hitl_gate_<source>_<target>``.

    Byte-identical mirror of ``graph_cache._make_gate_id`` (the executor stamps
    gate ids with that format). Single-source the derivation so enforcement
    surfaces (REST + MCP) and label building construct the same id the
    executor fired.
    """
    return f"{GATE_ID_PREFIX}{source}_{target}"


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
    if not gate_id.startswith(GATE_ID_PREFIX):
        return None
    parts = gate_id[len(GATE_ID_PREFIX) :].rsplit("_", 2)
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
        if source is not None and target is not None and make_gate_id(source, target) == gate_id:
            return config
    return None


def _config_from_hitl_nodes(graph_json: dict[str, Any], gate_id: str) -> dict[str, Any] | None:
    """Resolve a FAR-402 HITL-NODE gate's config from a graph's nodes.

    HITL nodes carry ``hitl_config`` on the NODE; the compiler injects
    ``{**hitl_config, "gate_id": ...}`` onto the node's outgoing edges at
    build time only, so the persisted definition (snapshot ``graph_json`` and
    live pipeline rows alike) has NO edge-level ``hitl_gate_config`` for
    node-level gates. Mirror the compiler's derivation: a hitl node's gate
    ids are ``hitl_gate_<node_id>_<target>`` over its outgoing edges. Only
    ``node_type == "hitl"`` nodes are consulted — ``hitl_config`` on any other
    node type is inert at runtime (the compiler ignores it), so honouring it
    here could over-block.
    """
    edges = graph_json.get("edges", [])
    for node in graph_json.get("nodes", []):
        if not isinstance(node, dict) or node.get("node_type") != "hitl":
            continue
        config = node.get("hitl_config")
        if not isinstance(config, dict):
            continue
        node_id = node.get("id")
        if node_id is None:
            continue
        node_id = str(node_id)
        for edge in edges:
            if not isinstance(edge, dict):
                continue
            source = edge_source_or_target(edge, "source")
            target = edge_source_or_target(edge, "target")
            if source == node_id and target is not None and make_gate_id(node_id, target) == gate_id:
                return dict(config)
    return None


def snapshot_gate_config_map(graph_json: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """Map gate_id -> gate config for EVERY gate in a snapshot graph.

    Covers both gate shapes: edge-level ``hitl_gate_config`` and FAR-402
    node-level ``hitl_config`` (walked over each HITL node's outgoing edges,
    the same derivation the compiler uses). The pending-gate endpoints use
    this to resolve labels and descriptions for a whole run in ONE walk
    instead of a per-gate config resolution.
    """
    configs: dict[str, dict[str, Any]] = {}
    edges = graph_json.get("edges", [])
    for edge in edges:
        if not isinstance(edge, dict):
            continue
        config = edge.get("hitl_gate_config")
        if not isinstance(config, dict):
            continue
        source = edge_source_or_target(edge, "source")
        target = edge_source_or_target(edge, "target")
        if source is not None and target is not None:
            configs[make_gate_id(source, target)] = config
    for node in graph_json.get("nodes", []):
        if not isinstance(node, dict) or node.get("node_type") != "hitl":
            continue
        config = node.get("hitl_config")
        if not isinstance(config, dict):
            continue
        node_id = node.get("id")
        if node_id is None:
            continue
        node_id = str(node_id)
        for edge in edges:
            if not isinstance(edge, dict):
                continue
            target = edge_source_or_target(edge, "target")
            source = edge_source_or_target(edge, "source")
            if source == node_id and target is not None:
                configs.setdefault(make_gate_id(node_id, target), dict(config))
    return configs


def normalize_gate_description(config: dict[str, Any] | None) -> str | None:
    """The config's human description, or None when unusable (FAR-613).

    Usable = a string whose non-empty trimmed form survives (whitespace-only
    or non-string values carry no decision context). Both pending endpoints
    and the MCP gate resource share this normalisation so every surface
    renders the same "muted no-description fallback" for the same gates.
    """
    if not isinstance(config, dict):
        return None
    description = config.get("description")
    if not isinstance(description, str) or not description.strip():
        return None
    return description.strip()


async def resolve_gate_descriptions(
    session: AsyncSession,
    *,
    gates: Sequence[HitlClaim],
    org_id: uuid.UUID,
) -> dict[tuple[uuid.UUID, str], str | None]:
    """Resolve each gate's description from its run's snapshot graph (FAR-613).

    The org-level pending surfaces (REST ``GET /api/v1/hitl/pending`` and the
    MCP ``list_pending_hitl`` tool) share this — batched, two IN queries over
    the pending gates' runs + snapshots, never a per-gate snapshot walk.
    Keys are ``(run_id, gate_id)``. A gate whose snapshot is missing or whose
    config carries no usable description maps to None (the UI renders the
    muted no-description fallback). The queries add explicit
    ``organisation_id`` filters as defence in depth (callers already set the
    RLS org context).
    """
    description_by_gate: dict[tuple[uuid.UUID, str], str | None] = {}
    if not gates:
        return description_by_gate
    run_rows = await session.execute(
        select(Run.id, Run.snapshot_id).where(
            Run.id.in_({g.run_id for g in gates}),
            Run.organisation_id == org_id,
        )
    )
    snapshot_id_by_run: dict[uuid.UUID, uuid.UUID] = {row[0]: row[1] for row in run_rows.all() if row[1] is not None}
    graph_by_snapshot: dict[uuid.UUID, dict[str, Any]] = {}
    if snapshot_id_by_run:
        snap_rows = await session.execute(
            select(PipelineSnapshot.id, PipelineSnapshot.graph_json).where(
                PipelineSnapshot.id.in_(set(snapshot_id_by_run.values())),
                PipelineSnapshot.organisation_id == org_id,
            )
        )
        for row in snap_rows.all():
            if isinstance(row[1], dict):
                graph_by_snapshot[row[0]] = row[1]
    for gate in gates:
        snapshot_id = snapshot_id_by_run.get(gate.run_id)
        graph = graph_by_snapshot.get(snapshot_id) if snapshot_id is not None else None
        config = snapshot_gate_config_map(graph).get(gate.gate_id) if isinstance(graph, dict) else None
        description_by_gate[(gate.run_id, gate.gate_id)] = normalize_gate_description(config)
    return description_by_gate


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
                # Gated edges are always normal edges, and uq_pipeline_edges_path
                # includes edge_type — a reject/conditional edge may share the
                # same (source, target) pair, so without this filter two rows
                # match and scalar_one_or_none raises MultipleResultsFound.
                PipelineEdge.edge_type == "normal",
            )
        )
    ).scalar_one_or_none()
    if edge is not None and isinstance(edge.hitl_gate_config, dict):
        return dict(edge.hitl_gate_config)
    return None


async def _config_from_live_nodes(
    session: AsyncSession,
    pipeline_id: uuid.UUID,
    org_id: uuid.UUID,
    gate_source: str,
) -> dict[str, Any] | None:
    """Look a FAR-402 HITL-node gate's config up in the LIVE pipeline graph.

    The live ``pipelines.graph_nodes_json`` is consulted only after the live
    edge missed, so this query runs solely on the node-gate fallback path.
    The pipeline row is matched on id + organisation_id (RLS defence in
    depth); the source node id was already parsed from the gate id by the
    caller.
    """
    nodes = (
        await session.execute(
            select(Pipeline.graph_nodes_json).where(
                Pipeline.id == pipeline_id,
                Pipeline.organisation_id == org_id,
            )
        )
    ).scalar_one_or_none()
    if not isinstance(nodes, list):
        return None
    for node in nodes:
        if (
            isinstance(node, dict)
            and node.get("node_type") == "hitl"
            and node.get("id") is not None
            and str(node["id"]) == gate_source
        ):
            config = node.get("hitl_config")
            if isinstance(config, dict):
                return dict(config)
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

    FAR-634 claim-stamped fast path: the executor's interrupt handler stamps
    the resolved config onto the ``hitl_claims.gate_config_json`` column at
    fire time, so for gates that FIRED the config is read in ONE claim-row
    lookup instead of the snapshot/live walk below. The stamp check runs only
    for parseable gate ids (``hitl_gate_<source>_<target>``) — manual-node ids
    never have a claim row, so they skip the lookup entirely (submit-manual
    decisions pay zero extra queries). The snapshot/live walk REMAINS the
    fallback for legacy rows that fired before the stamp column existed, for
    gates with no claim row (never fired), and when the stamp is NULL (a
    fire-time stamp failure is failure-isolated by contract). Unresolvable
    gates still return None — callers enforce the fail-closed
    ``hitl_gate_exists_but_unresolved`` semantics unchanged.
    """
    # FAR-634: claim-stamped config first (O(1) — the row is one indexed
    # lookup on the unique (run_id, gate_id) pair). The org filter is defence
    # in depth (callers already set the RLS org context).
    if parse_hitl_gate_id(gate_id) is not None:
        stamped = (
            await session.execute(
                select(HitlClaim.gate_config_json).where(
                    HitlClaim.run_id == run_id,
                    HitlClaim.gate_id == gate_id,
                    HitlClaim.organisation_id == org_id,
                )
            )
        ).scalar_one_or_none()
        if isinstance(stamped, dict):
            return dict(stamped)

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
            if config is None:
                # FAR-402 node-level gates carry their config on the NODE, not
                # the edge — consult the snapshot's HITL nodes before falling
                # back to the live definition.
                config = _config_from_hitl_nodes(snapshot.graph_json, gate_id)
            if config is not None:
                return config

    config = await _config_from_live_edges(session, run.pipeline_id, org_id, gate_id)
    if config is not None:
        return config
    parsed = parse_hitl_gate_id(gate_id)
    if parsed is None:
        return None
    return await _config_from_live_nodes(session, run.pipeline_id, org_id, parsed[0])


async def hitl_gate_exists_but_unresolved(
    session: AsyncSession,
    *,
    run_id: uuid.UUID,
    gate_id: str,
    org_id: uuid.UUID,
) -> bool:
    """Return True when the gate FIRED but its config could not be resolved.

    Fail-closed signal for decision enforcement (FAR-610 review): a fired
    gate always has a ``hitl_claims`` row (created by the executor's interrupt
    handler) and its id is always the topology-derived
    ``hitl_gate_<source>_<target>`` (``graph_cache._make_gate_id`` — the
    compiler stamps it, so user input cannot override). When the resolver
    returns None for such a gate, the human_only policy cannot be verified
    and callers deny non-browser decisions instead of allowing them.

    Non-gate ids (manual-node ids on ``submit_manual``/``deliver_manual``)
    can never have a claim row, so they short-circuit to False and the
    fail-closed check cannot over-block manual output delivery. Callers
    already set the RLS org context; the query adds an explicit
    ``organisation_id`` filter as defence in depth.
    """
    if parse_hitl_gate_id(gate_id) is None:
        return False
    row = (
        await session.execute(
            select(HitlClaim.id).where(
                HitlClaim.run_id == run_id,
                HitlClaim.gate_id == gate_id,
                HitlClaim.organisation_id == org_id,
            )
        )
    ).scalar_one_or_none()
    return row is not None


MSG_HUMAN_ONLY_DENY = "human_only gate requires browser authentication; API-key clients cannot approve this gate"
MSG_HUMAN_ONLY_UNRESOLVED = "HITL gate configuration could not be resolved; decision requires browser authentication"


def human_only_denial(
    config: dict[str, Any] | None,
    *,
    non_browser_credential: bool,
    gate_fired: bool,
) -> str | None:
    """Return the denial message for a human_only gate decision, or None to allow.

    Pure verdict shared by the REST decision routes and the MCP ``review_hitl``
    tool so both surfaces enforce the SAME policy with the SAME wording
    (FAR-610 review: the policy was previously implemented twice with slightly
    different messages). Callers resolve the gate config first and compute
    ``gate_fired`` via :func:`hitl_gate_exists_but_unresolved` ONLY when the
    config is None (the claim query is wasted when the config resolved).

    Semantics:

    - ``non_browser_credential`` False → None. Browser credentials are always
      allowed — the UI is their enforcement surface, checked before any other
      logic.
    - ``config`` None → :data:`MSG_HUMAN_ONLY_UNRESOLVED` when ``gate_fired``
      else None. Fail closed only for gates that actually FIRED (a claim row
      exists): for those the policy cannot be verified, so non-browser
      decisions are denied rather than silently allowed. Unfired/non-gate ids
      stay allowed (manual delivery must not over-block).
    - ``config`` dict with a truthy ``human_only`` →
      :data:`MSG_HUMAN_ONLY_DENY` (the same ``config.get("human_only", False)``
      truthiness rule the enforcement sites have always used).
    - otherwise → None.
    """
    if not non_browser_credential:
        return None
    if config is None:
        if gate_fired:
            return MSG_HUMAN_ONLY_UNRESOLVED
        return None
    if config.get("human_only", False):
        return MSG_HUMAN_ONLY_DENY
    return None
