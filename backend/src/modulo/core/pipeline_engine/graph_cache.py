"""In-memory LRU cache for compiled LangGraph StateGraphs.

Cache key is (pipeline_id, snapshot_id) — not snapshot_id alone, because two
pipelines can share snapshot version numbers (they're per-pipeline sequences).
Eviction is true LRU via OrderedDict.

The compilation factory is synchronous (build_graph_from_json) so it blocks
the event loop — no thundering-herd risk in asyncio. A threading lock is
kept for correctness if compilation becomes async in the future.
"""

import threading
import uuid
from collections import OrderedDict, defaultdict
from collections.abc import Callable
from typing import Any

import jmespath
from langgraph.graph import StateGraph

from modulo.core.pipeline_engine.node_runner import (
    make_hitl_gate_fn,
    make_manual_node_fn,
    make_node_fn,
)

# OrderedDict-based LRU cache. Accessing an entry moves it to the end;
# when full, the least-recently-used entry (first in order) is evicted.
_CACHE: OrderedDict[tuple[uuid.UUID, uuid.UUID], Any] = OrderedDict()
_MAX_SIZE = 256

# Per-key lock to prevent double compilation if factory becomes async.
_compile_locks: dict[tuple[uuid.UUID, uuid.UUID], threading.Lock] = {}


def get_or_compile(
    pipeline_id: uuid.UUID,
    snapshot_id: uuid.UUID,
    factory: Callable[[], Any],
) -> Any:
    """Return cached compiled graph or call factory() and cache the result.

    Uses a per-key lock so concurrent calls for the same uncached key
    compile only once.
    """
    key = (pipeline_id, snapshot_id)
    if key in _CACHE:
        _CACHE.move_to_end(key)
        return _CACHE[key]

    lock = _compile_locks.setdefault(key, threading.Lock())
    with lock:
        if key in _CACHE:
            return _CACHE[key]
        if len(_CACHE) >= _MAX_SIZE:
            _CACHE.popitem(last=False)
        result = factory()
        _CACHE[key] = result
    return result


def _get_edge_val(edge: dict[str, Any], canonical: str, stored: str) -> str:
    value = edge.get(canonical, edge.get(stored))
    if value is None:
        raise ValueError(f"graph edge missing {canonical}")
    return str(value)


def _make_gate_id(source: str, target: str) -> str:
    return f"hitl_gate_{source}_{target}"


# ---------------------------------------------------------------------------
# Conditional edge routing
# ---------------------------------------------------------------------------


def _is_truthy(value: Any) -> bool:
    """Match the truthiness semantics used by polling.py JMESPath evaluation."""
    if value is None:
        return False
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value != 0
    if isinstance(value, (list, dict)):
        return len(value) > 0
    if isinstance(value, str):
        return len(value) > 0
    return True


def _make_conditional_router(
    conditional_edges: list[dict[str, Any]],
    normal_targets: list[str],
    default_target: str | None,
) -> Callable[[dict[str, Any]], str]:
    """Build a router function for conditional outgoing edges from a source node.

    Each conditional edge carries a ``condition_expression`` (JMESPath) that
    is evaluated against the full state dict.  The first matching edge's
    target is returned.

    If no condition matches, the first *normal_target* is returned.
    If there are no normal targets, *default_target* (or the last conditional
    edge's target) is used as the fallback.
    """
    compiled: list[tuple[Any, str]] = []
    for edge in conditional_edges:
        expr: str = edge.get("condition_expression", "")
        target = _get_edge_val(edge, "target", "target_node_id")
        compiled.append((jmespath.compile(expr), target))

    def _router(state: dict[str, Any]) -> str:
        for compiled_expr, target in compiled:
            result = compiled_expr.search(state)  # type: ignore[union-attr]
            if _is_truthy(result):
                return target
        if normal_targets:
            return normal_targets[0]
        if default_target:
            return default_target
        if compiled:
            return compiled[-1][1]
        raise ValueError("no edges to route through")

    return _router


# ---------------------------------------------------------------------------
# Graph builder
# ---------------------------------------------------------------------------


def build_graph_from_json(graph_json: dict[str, Any]) -> Any:
    """Compile a StateGraph from the serialised graph_json stored in a snapshot.

    graph_json schema:
        {
          "nodes": [{"id": "<uuid>", "agent_id": "<uuid>", "role": "..."}],
          "edges": [{"source": "<uuid>", "target": "<uuid>",
                      "type": "normal", "hitl_gate_config": {...}}]
        }

    For edges that carry a ``hitl_gate_config``, an intermediate gate node is
    inserted between the source and target.  At runtime the gate node checks
    the effective autonomy level (from pipeline default or run_context) and
    either interrupts for human review or auto-approves.

    Conditional edges (``type: "conditional"``) are compiled via
    ``add_conditional_edges`` with a JMESPath-based router.  If a source has
    any conditional edges, *all* of its outgoing edges are handled by the
    router — normal edges from that source serve as fallback targets.

    Returns a compiled LangGraph that accepts dict[str, Any] state.
    """
    graph: StateGraph = StateGraph(dict)

    nodes: list[dict[str, Any]] = graph_json.get("nodes", [])
    edges: list[dict[str, Any]] = graph_json.get("edges", [])

    if not nodes:
        raise ValueError("graph_json has no nodes")

    for node_def in nodes:
        node_id: str = str(node_def["id"])
        role: str | None = node_def.get("role")
        timeout: float | None = node_def.get("timeout_seconds")
        node_type: str = node_def.get("node_type", "agent")

        if node_type == "manual":
            graph.add_node(
                node_id,
                make_manual_node_fn(node_def, timeout=timeout),
            )
        else:
            graph.add_node(
                node_id,
                make_node_fn(node_def, role=role, timeout=timeout),
            )

    # Group forwarding edges by source (skip reject).
    source_edges: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for edge_def in edges:
        if edge_def.get("type", edge_def.get("edge_type", "")) == "reject":
            continue
        source = _get_edge_val(edge_def, "source", "source_node_id")
        source_edges[source].append(edge_def)

    target_ids: set[str] = set()
    gate_node_ids: set[str] = set()

    for source, src_edges in source_edges.items():
        conditional = [e for e in src_edges if e.get("type") == "conditional"]
        normal = [e for e in src_edges if e.get("type") != "conditional"]

        if conditional:
            # All outgoing edges from this source are handled by the router.
            normal_targets: list[str] = []
            for edge_def in normal:
                tgt = _get_edge_val(edge_def, "target", "target_node_id")
                normal_targets.append(tgt)
                target_ids.add(tgt)

            default_target: str | None = None
            # Check for an explicit default on any conditional edge.
            for edge_def in conditional:
                dft = edge_def.get("default_target")
                if dft:
                    default_target = str(dft)

            router = _make_conditional_router(conditional, normal_targets, default_target)
            graph.add_conditional_edges(source, router)
        else:
            for edge_def in normal:
                target = _get_edge_val(edge_def, "target", "target_node_id")
                hitl_config = edge_def.get("hitl_gate_config")
                if hitl_config:
                    gate_id = _make_gate_id(source, target)
                    hitl_config["gate_id"] = gate_id
                    graph.add_node(gate_id, make_hitl_gate_fn(hitl_config))
                    graph.add_edge(source, gate_id)
                    graph.add_edge(gate_id, target)
                    gate_node_ids.add(gate_id)
                    target_ids.add(gate_id)
                else:
                    target_ids.add(target)
                    graph.add_edge(source, target)

    entry_candidates = [str(n["id"]) for n in nodes if str(n["id"]) not in target_ids]
    if not entry_candidates:
        raise ValueError("graph_json has a cycle or no entry node")
    graph.set_entry_point(entry_candidates[0])

    return graph.compile()


def evict(pipeline_id: uuid.UUID, snapshot_id: uuid.UUID) -> None:
    """Remove the cached entry for a (pipeline_id, snapshot_id) pair."""
    _CACHE.pop((pipeline_id, snapshot_id), None)
