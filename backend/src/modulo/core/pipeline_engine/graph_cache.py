"""In-memory LRU cache for compiled LangGraph StateGraphs.

Cache key is (pipeline_id, snapshot_id) — not snapshot_id alone, because two
pipelines can share snapshot version numbers (they're per-pipeline sequences).
Eviction is true LRU via OrderedDict.

The compilation factory is synchronous (build_graph_from_json) so it blocks
the event loop — no thundering-herd risk in asyncio. A threading lock is
kept for correctness if compilation becomes async in the future.
"""

from __future__ import annotations

import threading
import uuid
from collections import OrderedDict, defaultdict
from collections.abc import Callable
from typing import Annotated, Any, cast

import jmespath
from langgraph.graph import StateGraph

from modulo.core.eval_engine import EvalDefinition
from modulo.core.pipeline_engine.node_runner import (
    _is_truthy,
    make_connector_fn,
    make_hitl_gate_fn,
    make_manual_node_fn,
    make_node_fn,
    make_sandbox_agent_fn,
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
            evicted_key = _CACHE.popitem(last=False)[0]
            _compile_locks.pop(evicted_key, None)
        result = factory()
        _CACHE[key] = result
    return result


def _get_edge_val(edge: dict[str, Any], canonical: str, persisted: str) -> str:
    value = edge.get(canonical, edge.get(persisted))
    if value is None:
        raise ValueError(f"graph edge missing {canonical}")
    return str(value)


def _get_edge_type(edge: dict[str, Any]) -> str:
    value = edge.get("type", edge.get("edge_type", ""))
    return str(value) if value is not None else ""


def _make_gate_id(source: str, target: str) -> str:
    return f"hitl_gate_{source}_{target}"


# ---------------------------------------------------------------------------
# Conditional edge routing
# ---------------------------------------------------------------------------


def _make_gate_kickback_router(
    normal_target: str,
    reject_target_str: str,
) -> Callable[[dict[str, Any]], str]:
    """Build a router that kicks back to reject_target on HITL rejection."""

    def _router(state: dict[str, Any]) -> str:
        decision = state.get("_hitl_decision")
        if decision and isinstance(decision, dict) and decision.get("action") == "rejected":
            return reject_target_str
        return normal_target

    return _router


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
            result = compiled_expr.search(state)
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


def _make_llm_router(
    routing_edges: list[dict[str, Any]],
    normal_targets: list[str],
    default_target: str | None,
) -> Callable[[dict[str, Any]], str]:
    """Build a router for LLM-driven conditional edges.

    Reads ``_llm_next_node`` from state (set by the LLM agent node) and
    returns the target of the first outgoing edge whose ``routing_label``
    matches.  If no match is found, returns *default_target*, then the first
    *normal_targets* entry, then the last routing edge's target, or raises.
    """
    label_to_target: dict[str, str] = {}
    for edge in routing_edges:
        label = edge.get("routing_label")
        if label:
            target = _get_edge_val(edge, "target", "target_node_id")
            label_to_target[str(label)] = target

    def _router(state: dict[str, Any]) -> str:
        next_node = state.get("_llm_next_node")
        if next_node and str(next_node) in label_to_target:
            return label_to_target[str(next_node)]
        if default_target:
            return default_target
        if normal_targets:
            return normal_targets[0]
        if label_to_target:
            return list(label_to_target.values())[-1]
        raise ValueError("no edges to route through")

    return _router


def _make_loop_router(
    source: str,
    target: str,
    default_target: str,
    max_iterations: int,
    condition_expression: str | None,
) -> Callable[[dict[str, Any]], str]:
    """Build a router for loop edges.

    Reads ``_iteration_counts`` from state, increments the counter for this
    loop edge's source->target pair, and decides whether to continue looping
    or exit to *default_target*.

    Router logic (first match wins):
    1. If *max_iterations* > 0 and counter >= max_iterations → exit to default_target
    2. If *condition_expression* is set and truthy → loop back to *target*
    3. If *condition_expression* is set and falsy → exit to default_target
    4. If neither condition nor max_iterations → always loop back to *target*
       (relies on RunawayGuard for infinite-loop protection)
    """
    loop_key = f"{source}->{target}"
    compiled_expr = jmespath.compile(condition_expression) if condition_expression else None

    def _router(state: dict[str, Any]) -> str:
        iteration_counts: dict[str, int] = state.get("_iteration_counts", {})
        count: int = iteration_counts.get(loop_key, 0) + 1
        iteration_counts[loop_key] = count

        if max_iterations > 0 and count >= max_iterations:
            return default_target
        if compiled_expr is not None:
            result = compiled_expr.search(state)
            if bool(result):
                return target
            return default_target
        return target

    return _router


# ---------------------------------------------------------------------------
# Graph builder
# ---------------------------------------------------------------------------


# Keys whose values should be concatenated (not replaced) when multiple nodes
# write to the same channel in the same step (e.g. parallel branches).
_CONCAT_KEYS: frozenset[str] = frozenset({"artifacts", "_hitl_gates", "_run_context_write_log", "_iteration_counts"})


def _pipeline_state_reducer(current: dict[str, Any], update: dict[str, Any]) -> dict[str, Any]:
    """Merge a single state update, concatenating list-valued keys for parallel writes."""
    result = dict(current)
    for k, v in update.items():
        if k in _CONCAT_KEYS and k in result and isinstance(result[k], list) and isinstance(v, list):
            result[k] = result[k] + v
        else:
            result[k] = v
    return result


def build_graph_from_json(
    graph_json: dict[str, Any],
    *,
    eval_definitions_by_node: dict[str, list[EvalDefinition]] | None = None,
    session_factory: Callable[..., Any] | None = None,
    org_id: uuid.UUID | None = None,
) -> Any:
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
    either interrupts for human review or auto-approves.  The gate node also
    supports:
      - Conditional gating via ``condition`` JMESPath expression on the gate
        config (evaluated against state before autonomy checks).
      - Eval-before-interrupt via ``eval_definitions_by_node`` keyed by the
        source node id.

    Conditional edges (``type: "conditional"``) are compiled via
    ``add_conditional_edges`` with a JMESPath-based router.  If a source has
    any conditional edges, *all* of its outgoing edges are handled by the
    router — normal edges from that source serve as fallback targets.

    Returns a compiled LangGraph that accepts dict[str, Any] state.
    """
    state_schema = cast(type[Any], Annotated[dict[str, Any], _pipeline_state_reducer])
    graph: StateGraph[Any] = StateGraph(state_schema)

    nodes: list[dict[str, Any]] = graph_json.get("nodes", [])
    edges: list[dict[str, Any]] = graph_json.get("edges", [])

    if not nodes:
        raise ValueError("graph_json has no nodes")

    for node_def in nodes:
        node_id: str = str(node_def["id"])
        role: str | None = node_def.get("role")
        timeout: float | None = node_def.get("timeout_seconds")
        node_type: str = node_def.get("node_type", "agent")
        max_input_length: int | None = node_def.get("max_input_length")
        token_budget: int | None = node_def.get("token_budget")

        if node_type not in ("agent", "manual", "connector", "sandbox_agent"):
            raise ValueError(f"Unknown node_type {node_type!r} for node {node_id!r}")

        connector_binding = node_def.get("connector_binding")

        if node_type == "sandbox_agent":
            graph.add_node(
                node_id,
                make_sandbox_agent_fn(
                    node_def,
                    timeout=timeout,
                ),
            )
        elif node_type == "agent" and node_def.get("agent_id"):
            graph.add_node(
                node_id,
                make_node_fn(
                    node_def,
                    role=role,
                    timeout=timeout,
                    max_input_length=max_input_length,
                    token_budget=token_budget,
                ),
            )
        elif connector_binding:
            graph.add_node(
                node_id,
                make_connector_fn(node_def, timeout=timeout),
            )
        elif node_type == "manual":
            graph.add_node(
                node_id,
                make_manual_node_fn(node_def, timeout=timeout),
            )
        else:
            graph.add_node(
                node_id,
                make_node_fn(
                    node_def,
                    role=role,
                    timeout=timeout,
                    max_input_length=max_input_length,
                    token_budget=token_budget,
                ),
            )

    # Build reject-edge lookup for kick-back routing.
    reject_targets_by_source: dict[str, str] = {}
    for edge_def in edges:
        etype = _get_edge_type(edge_def)
        if etype == "reject":
            src = _get_edge_val(edge_def, "source", "source_node_id")
            tgt = _get_edge_val(edge_def, "target", "target_node_id")
            reject_targets_by_source[src] = tgt

    # Group forwarding edges by source (skip reject).
    source_edges: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for edge_def in edges:
        if _get_edge_type(edge_def) == "reject":
            continue
        source = _get_edge_val(edge_def, "source", "source_node_id")
        source_edges[source].append(edge_def)

    target_ids: set[str] = set()
    gate_node_ids: set[str] = set()

    # Build a node-id-to-def lookup for quick access.
    nodes_by_id: dict[str, dict[str, Any]] = {str(n["id"]): n for n in nodes}

    for source, src_edges in source_edges.items():
        source_node_def = nodes_by_id.get(source, {})
        routing_mode: str | None = source_node_def.get("routing_mode")

        conditional = [e for e in src_edges if _get_edge_type(e) == "conditional"]
        loop_edges = [e for e in src_edges if _get_edge_type(e) == "loop"]
        normal = [e for e in src_edges if _get_edge_type(e) not in ("conditional", "loop")]

        if loop_edges:
            normal_targets: list[str] = []
            for edge_def in normal:
                tgt = _get_edge_val(edge_def, "target", "target_node_id")
                normal_targets.append(tgt)
                target_ids.add(tgt)

            for loop_edge in loop_edges:
                target = _get_edge_val(loop_edge, "target", "target_node_id")
                max_iterations = int(loop_edge.get("max_iterations", 0))
                condition_expression = loop_edge.get("condition_expression")
                default_target_raw = loop_edge.get("default_target")
                if default_target_raw:
                    default_target_str = str(default_target_raw)
                elif normal_targets:
                    default_target_str = normal_targets[0]
                else:
                    msg = f"loop edge from '{source}' requires default_target (no normal targets available)"
                    raise ValueError(msg)

                router = _make_loop_router(
                    source,
                    target,
                    default_target_str,
                    max_iterations,
                    condition_expression,
                )
                graph.add_conditional_edges(source, router)
                target_ids.add(target)
                target_ids.add(default_target_str)

            continue

        if routing_mode == "llm":
            # All outgoing edges from this node are handled by the LLM router.
            llm_edges = conditional or normal
            normal_targets = []
            for edge_def in normal:
                tgt = _get_edge_val(edge_def, "target", "target_node_id")
                normal_targets.append(tgt)
                target_ids.add(tgt)

            default_target: str | None = source_node_def.get("default_target")

            router = _make_llm_router(llm_edges, normal_targets, default_target)
            graph.add_conditional_edges(source, router)
        elif conditional:
            # All outgoing edges from this source are handled by the router.
            normal_targets = []
            for edge_def in normal:
                tgt = _get_edge_val(edge_def, "target", "target_node_id")
                normal_targets.append(tgt)
                target_ids.add(tgt)

            default_target = None
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
                    node_evals = eval_definitions_by_node.get(source) if eval_definitions_by_node is not None else None
                    graph.add_node(
                        gate_id,
                        make_hitl_gate_fn(
                            hitl_config,
                            eval_definitions=node_evals,
                            session_factory=session_factory,
                            org_id=org_id,
                        ),
                    )
                    graph.add_edge(source, gate_id)

                    # Determine kick-back target for HITL rejection routing.
                    # Priority: gate config reject_target > reject edge target.
                    reject_target: str | None = hitl_config.get("reject_target")
                    if reject_target is None:
                        reject_target = reject_targets_by_source.get(source)

                    if reject_target:
                        reject_target_str = str(reject_target)
                        gate_router = _make_gate_kickback_router(target, reject_target_str)
                        graph.add_conditional_edges(gate_id, gate_router)
                        target_ids.add(reject_target_str)
                    else:
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
