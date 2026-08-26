"""In-memory LRU cache for compiled LangGraph StateGraphs.

Cache key is (pipeline_id, snapshot_id, pipeline_node_timeout_seconds) — not
snapshot_id alone, because two pipelines can share snapshot version numbers
(they're per-pipeline sequences). The node timeout is part of the key because
build_graph_from_json bakes the pipeline value into every node with a null
timeout_seconds; keying on it means PATCHing node_timeout_seconds takes effect
immediately instead of serving a stale graph until eviction.
Eviction is true LRU via OrderedDict.

The compilation factory is synchronous (build_graph_from_json) so it blocks
the event loop — no thundering-herd risk in asyncio. A threading lock is
kept for correctness if compilation becomes async in the future.
"""

from __future__ import annotations

import asyncio
import threading
import uuid
from collections import OrderedDict, defaultdict
from collections.abc import Callable, Coroutine
from typing import Annotated, Any, cast

import jmespath
from langgraph.graph import StateGraph

from modulo.core.eval_engine import EvalDefinition
from modulo.core.node_output_split import DEFAULT_NODE_TYPE
from modulo.core.pipeline_engine.jmespath_eval import evaluate_jmespath_condition
from modulo.core.pipeline_engine.node_runner import (
    make_connector_fn,
    make_hitl_gate_fn,
    make_manual_node_fn,
    make_node_fn,
    make_router_node_fn,
    make_sandbox_agent_fn,
)
from modulo.core.pipeline_engine.port_resolver import (
    synthesize_node_ports,
)
from modulo.core.pipeline_engine.scatter_join import (
    run_join_node,
    run_scatter_node,
    validate_scatter_join_node,
)

# Cache key: (pipeline_id, snapshot_id, pipeline_node_timeout_seconds). The
# third element matters because the compiled graph bakes the effective per-node
# timeout in — without it, PATCHing node_timeout_seconds would be a no-op until
# LRU eviction/restart. The fourth element is a deterministic structural hash of
# the graph's port topology (FAR-416 / F1): it forces a recompile when ports or
# node types change, even though the (pipeline_id, snapshot_id, timeout) triple
# is unchanged.
CacheKey = tuple[uuid.UUID, uuid.UUID, int, str]

# OrderedDict-based LRU cache. Accessing an entry moves it to the end;
# when full, the least-recently-used entry (first in order) is evicted.
_CACHE: OrderedDict[CacheKey, Any] = OrderedDict()
_MAX_SIZE = 256

# Per-key lock to prevent double compilation if factory becomes async.
_compile_locks: dict[CacheKey, threading.Lock] = {}


def get_or_compile(
    pipeline_id: uuid.UUID,
    snapshot_id: uuid.UUID,
    factory: Callable[[], Any],
    *,
    pipeline_node_timeout_seconds: int = 300,
    graph_struct_hash: str = "",
) -> Any:
    """Return cached compiled graph or call factory() and cache the result.

    The cache key includes ``pipeline_node_timeout_seconds`` because the
    compiled graph embeds the effective per-node timeout (the pipeline value is
    used for every node with a null ``timeout_seconds``). Keying on it means a
    PATCH to the pipeline setting takes effect immediately. ``graph_struct_hash``
    (FAR-416 / F1) folds the port topology into the key so a port change forces
    a fresh compile rather than serving a stale cached graph.

    Uses a per-key lock so concurrent calls for the same uncached key
    compile only once.
    """
    key = (pipeline_id, snapshot_id, pipeline_node_timeout_seconds, graph_struct_hash)
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


def _make_router_pass_fn(node_id: str) -> Callable[[dict[str, Any]], Coroutine[Any, Any, dict[str, Any]]]:
    """A pass-through node function for Router nodes.

    A Router node makes no tool/agent call — it exists solely to host the
    outgoing conditional-edges router (built in ``build_graph_from_json``). It
    returns an empty update so the merged state is unchanged.
    """

    async def _pass(state: dict[str, Any]) -> dict[str, Any]:
        return {}

    _pass.__name__ = f"router_pass_{node_id}"
    return _pass


class RouterConfigError(ValueError):
    """Typed compile-time error for an invalid Router node configuration."""


def _validate_router_config(router_config: dict[str, Any], node_id: str) -> None:
    """Enforce Router-node compile-time invariants (FAR-402 P1 / F2-A).

    A Router node MUST declare a ``default`` rule so every run has a defined
    terminal hop. This is enforced ONLY for new Router nodes (backward-compat:
    legacy ``conditional`` edges without a default remain valid — they fall
    back to the first normal target via ``_make_conditional_router``).
    """
    rules = router_config.get("rules") or []
    has_default = any(rule.get("default") for rule in rules)
    if not has_default and router_config.get("mode") != "classifier":
        raise RouterConfigError(
            f"Router node {node_id!r} has no 'default' rule; every Router node must declare an explicit default target."
        )


def _make_gate_kickback_router(
    normal_target: str,
    reject_target_str: str,
) -> Callable[[dict[str, Any]], str]:
    """Build a router that kicks back to reject_target on HITL rejection.

    FAR-210 (reject→correction edge): a gate config MAY declare a
    ``correction_target``. The router still kicks a rejection back to the plain
    ``reject_target`` — the correction DISPATCH happens in the gate node itself
    (``node_runner._hitl_gate``), which invokes
    ``FeedbackManager.dispatch_reject_correction`` on a rejected gate that
    declares a ``correction_target`` (the automated reject→correction path).
    Routing a rejection to the ``correction_target`` here would be dead routing
    state; T1's ``recover_node`` override stays the break-glass path.
    """

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
    compiled: list[tuple[str, str]] = []
    for edge in conditional_edges:
        expr: str = edge.get("condition_expression", "")
        target = _get_edge_val(edge, "target", "target_node_id")
        compiled.append((expr, target))

    def _router(state: dict[str, Any]) -> str:
        for expr, target in compiled:
            if evaluate_jmespath_condition(state, expr):
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


def _make_loop_counter_id(source: str, target: str) -> str:
    """Return the deterministic synthetic node id for a loop edge's counter."""
    return f"__loop_counter_{source}_{target}"


def make_loop_counter_fn(loop_key: str) -> Any:
    """Return an async node function that increments a loop edge's counter.

    LangGraph discards in-place mutations of the state dict performed by
    routers (the state dict is shallow-copied per superstep), so a router
    cannot persist ``_iteration_counts`` across iterations and
    ``max_iterations`` never trips — the run loops until GraphRecursionError.

    The idiomatic fix is a real NODE that returns the incremented counter as
    a state update. This node does no LLM/sandbox work: it reads the current
    counts, bumps the one for *loop_key*, and returns
    ``{"_iteration_counts": counts}`` (a genuine update the reducer persists).
    """

    async def _counter_node(state: dict[str, Any]) -> dict[str, Any]:
        counts = dict(state.get("_iteration_counts") or {})
        counts[loop_key] = int(counts.get(loop_key, 0)) + 1
        await asyncio.sleep(0)
        return {"_iteration_counts": counts}

    _counter_node.__name__ = f"loop_counter_{loop_key}"
    return _counter_node


def _hit_max_iterations(state: dict[str, Any], loop_key: str, max_iterations: int) -> bool:
    """Return True when the loop counter for *loop_key* has reached *max_iterations*."""
    if max_iterations <= 0:
        return False
    counts = state.get("_iteration_counts")
    count = int(counts.get(loop_key, 0)) if isinstance(counts, dict) else 0
    return count >= max_iterations


def _make_loop_counter_router(
    loop_key: str,
    target: str,
    default_target: str,
    max_iterations: int,
    condition_expression: str | None,
) -> Callable[[dict[str, Any]], str]:
    """Build a read-only router for a loop edge's counter node.

    The counter node just returned the incremented count as a real state
    update, so this router reads ``_iteration_counts`` and decides whether to
    continue looping or exit to *default_target*. It never mutates state.

    Router logic (first match wins):
    1. If *max_iterations* > 0 and counter >= max_iterations → exit to default_target
    2. If *condition_expression* is set and truthy → loop back to *target*
    3. If *condition_expression* is set and falsy → exit to default_target
    4. If neither condition nor max_iterations → always loop back to *target*
       (relies on RunawayGuard for infinite-loop protection)
    """
    compiled_expr = jmespath.compile(condition_expression) if condition_expression else None

    def _router(state: dict[str, Any]) -> str:
        if _hit_max_iterations(state, loop_key, max_iterations):
            return default_target
        if compiled_expr is not None:
            if evaluate_jmespath_condition(state, condition_expression):
                return target
            return default_target
        return target

    return _router


# ---------------------------------------------------------------------------
# Graph builder
# ---------------------------------------------------------------------------


# Keys whose values should be CONCATENATED (not replaced) when multiple nodes
# write to the same channel in the same superstep (e.g. parallel branches).
# Ordering: LangGraph applies a reducer once per task completion in the
# superstep's completion order, so the concatenation order == completion order.
# NOTE: only LIST-valued keys actually concatenate — dict-valued keys here
# (none today; `_iteration_counts` is dict-valued but written by loop counters)
# fall through to whole-key replacement. See `_pipeline_state_reducer`.
_CONCAT_KEYS: frozenset[str] = frozenset({"artifacts", "_hitl_gates", "_run_context_write_log", "_iteration_counts"})


def _should_concat_list(current: dict[str, Any], key: str, value: Any) -> bool:
    """True when *value* should be appended to the existing list for *key*."""
    return key in _CONCAT_KEYS and key in current and isinstance(current[key], list) and isinstance(value, list)


def _should_merge_run_context(current: dict[str, Any], key: str, value: Any) -> bool:
    """True when *value* is a run_context dict to merge per-key over the current one."""
    return key == "run_context" and isinstance(value, dict) and isinstance(current.get(key), dict)


def _pipeline_state_reducer(current: dict[str, Any], update: dict[str, Any]) -> dict[str, Any]:
    """Merge a single state update, concatenating list-valued keys for parallel writes.

    Two distinct parallel-write semantics (§8.18 run-context last-write-wins):

    - ``_CONCAT_KEYS`` (lists): every writer's list is appended, in superstep
      completion order. Each parallel branch contributes its own entries, so
      ``artifacts`` / ``_run_context_write_log`` / ``_hitl_gates`` never
      clobber each other.
    - ``run_context`` (dict): merged per-key with LAST-WRITE-WINS — each write
      applies only the keys it carries onto the current ``run_context``; when
      two parallel context-setters write the SAME key, the write whose reducer
      application lands last (superstep completion order) wins. This preserves
      seeded keys (``cancelled``, ``input``, ``_pipeline_default_autonomy``)
      and parallel writes to DISJOINT keys both land. The outcome is
      deterministic for a given run but order-dependent, which is why
      same-key parallel writes are flagged as a pipeline validation warning at
      save time (see ``GraphValidator._check_parallel_run_context_writes``).

    Known limitation — ``_iteration_counts`` is NOT list-valued: the loop-edge
    counter node returns a DICT (``make_loop_counter_fn``), so it never matches
    the ``_CONCAT_KEYS`` concat branch and falls through to whole-key
    replacement. This is correct for a single loop, but two loops running in
    the same superstep (a fan-out whose branches each carry a loop edge) would
    last-write-wins clobber each other's counters. Mitigation: the compile path
    routes ALL outgoing edges of a loop source through the counter (single
    target), so parallel loops require a deliberate multi-loop fan-out — an
    uncommon shape that should be revisited with a per-key merge if needed.

    A non-dict ``run_context`` update falls back to whole-key replacement
    (legacy behaviour).
    """
    result = dict(current)
    for k, v in update.items():
        if _should_concat_list(result, k, v):
            result[k] = result[k] + v
        elif _should_merge_run_context(result, k, v):
            result[k] = {**result[k], **v}
        else:
            result[k] = v
    return result


def _count_sandbox_nodes(nodes: list[dict[str, Any]]) -> bool:
    """Return True when the graph has exactly one sandbox_agent node (FAR-228)."""
    return sum(1 for n in nodes if str(n.get("node_type", "")).strip() == "sandbox_agent") == 1


def _make_node_fn(
    node_def: dict[str, Any],
    *,
    timeout: int,
    session_factory: Callable[..., Any] | None,
    single_sandbox_node: bool,
) -> Any:
    """Build the LangGraph node function for a single node def (no graph add)."""
    node_id: str = str(node_def["id"])
    role: str | None = node_def.get("role")
    node_type: str = node_def.get("node_type", "agent")
    max_input_length: int | None = node_def.get("max_input_length")
    token_budget: int | None = node_def.get("token_budget")

    if node_type not in ("agent", "manual", "connector", "sandbox_agent", "router", "hitl"):
        raise ValueError(f"Unknown node_type {node_type!r} for node {node_id!r}")

    connector_binding = node_def.get("connector_binding")

    if node_type == "sandbox_agent":
        return make_sandbox_agent_fn(
            node_def,
            timeout=timeout,
            session_factory=session_factory,
            single_sandbox_node=single_sandbox_node,
        )
    if connector_binding and not (node_type == "agent" and node_def.get("agent_id")):
        return make_connector_fn(node_def, timeout=timeout)
    if node_type == "manual":
        return make_manual_node_fn(node_def, timeout=timeout)
    if node_type == "router":
        # Router node: a pure decision node. It produces no artifact of its
        # own — it simply passes state through so the conditional-edges router
        # (built in build_graph_from_json) can pick the next hop.
        return _make_router_pass_fn(node_id)
    if node_type == "hitl":
        # HITL node: produces output like a normal node (agent / connector /
        # manual) and its OUTGOING edges are gated by the node's hitl_config
        # (injected at compile time in build_graph_from_json). The gate path is
        # identical to the legacy edge-level HITL gate.
        if node_def.get("agent_id"):
            return make_node_fn(
                node_def,
                role=role,
                timeout=timeout,
                max_input_length=max_input_length,
                token_budget=token_budget,
            )
        if connector_binding:
            return make_connector_fn(node_def, timeout=timeout)
        return make_manual_node_fn(node_def, timeout=timeout)
    # agent nodes (with or without a frozen agent_id) and connector nodes
    # without a binding default to the general agent node factory.
    return make_node_fn(
        node_def,
        role=role,
        timeout=timeout,
        max_input_length=max_input_length,
        token_budget=token_budget,
    )


def make_scatter_node_fn(
    node_def: dict[str, Any],
    *,
    timeout: int,
    session_factory: Callable[..., Any] | None = None,
    single_sandbox_node: bool = False,
) -> Any:
    """Build the runtime node function for a scatter (fan-out) node.

    At execution the split source port is read from ``state``; it is expanded
    into N child branches (each a unique ``child_id``) which are executed
    sequentially by the standard node factory in P3. Per-child outputs are
    written back into state keyed by their child id, plus a
    ``__scatter_manifest__`` map so a downstream join can locate them. An empty
    split source succeeds vacuously (no child calls).
    """
    parent_id = str(node_def["id"])
    fan_out = node_def["fan_out"]
    split = fan_out.get("split") if isinstance(fan_out, dict) else fan_out.split

    async def _fn(state: dict[str, Any]) -> dict[str, Any]:
        items = state.get(split) or [] if split is not None else []
        status_map: dict[str, str] = {}

        async def execute_child(child_def: dict[str, Any]) -> Any:
            child_fn = _make_node_fn(
                child_def,
                timeout=timeout,
                session_factory=session_factory,
                single_sandbox_node=single_sandbox_node,
            )
            child_id = str(child_def["id"])
            item = child_def.get("scatter_item")
            # Deliver the per-item payload to the child so each branch processes
            # its OWN item rather than an identical copy of the parent state.
            # The item is injected as the child's run_context input (the
            # conventional agent input channel, rendered into the prompt as
            # ``{{ input }}``) and mirrored under ``__scatter_item__`` for direct
            # template reference (``{{ state.__scatter_item__ }}``).
            child_state = dict(state)
            run_ctx = dict(child_state.get("run_context") or {})
            run_ctx["input"] = item
            child_state["run_context"] = run_ctx
            child_state["__scatter_item__"] = item
            try:
                out = await child_fn(child_state)
                status_map[child_id] = "succeeded"
                return out
            except Exception as exc:  # record per-branch failure
                # A child failure must NOT abort the whole scatter: record the
                # failed branch so a downstream join can apply its partial-failure
                # policy (``join_partial_policy="fail"`` raises; the default
                # ``collect_and_proceed`` aggregates the partial result). This is
                # what makes ``__scatter_read`` statuses real at runtime (FAR-402
                # §4 B).
                status_map[child_id] = "failed"
                return {"error": str(exc), "_scatter_child_failed": True}

        results = await run_scatter_node(node_def, items=list(items), execute_child=execute_child)
        manifest = [str(k) for k in results]
        update: dict[str, Any] = dict(results)
        if manifest:
            update["__scatter_manifest__"] = {parent_id: manifest}
        if status_map:
            update["__scatter_status__"] = status_map
        return update

    return _fn


def make_join_node_fn(
    node_def: dict[str, Any],
) -> Any:
    """Build the runtime node function for a join (fan-in) node.

    Gathers each collected branch's output from ``state`` (located via the
    ``__scatter_manifest__`` written by the upstream scatter node), then
    aggregates them per the node's ``aggregate`` spec. The aggregated value is
    written back under the join node's own id.
    """
    node_id = str(node_def["id"])
    collect = node_def.get("collect") or []

    def _fn(state: dict[str, Any]) -> dict[str, Any]:
        manifest_map = state.get("__scatter_manifest__", {})
        status_map = state.get("__scatter_status__", {})
        collected: list[dict[str, Any]] = []
        for spec in collect:
            parent_id = spec.get("node") if isinstance(spec, dict) else spec.node
            child_ids = manifest_map.get(parent_id, [])
            for child_id in child_ids:
                collected.append(
                    {
                        "node_id": child_id,
                        "output": state.get(child_id),
                        "status": status_map.get(child_id, "succeeded"),
                    }
                )
        result = run_join_node(node_def, collected=collected)
        return {node_id: result.get("aggregated")}

    return _fn


def _add_node_to_graph(
    graph: StateGraph[Any],
    node_def: dict[str, Any],
    *,
    timeout: int,
    session_factory: Callable[..., Any] | None,
    single_sandbox_node: bool,
) -> None:
    """Add a single node to the graph based on its type and config."""
    node_type: str = node_def.get("node_type", "agent")
    node_id: str = str(node_def["id"])

    # FAR-402 P3 / FAR-417: compile-time fail-closed validation of scatter/join
    # configuration (typed error surfaces before any execution).
    validate_scatter_join_node(node_def)

    if node_type == "join":
        # FAR-402 P3 / FAR-417: a join node is a pure convergence node. Its child
        # branches are executed by the upstream scatter node (or are real graph
        # nodes); the join simply aggregates their collected outputs from state.
        graph.add_node(node_id, make_join_node_fn(node_def))
        return

    if node_def.get("fan_out") is not None and node_type in ("agent", "sandbox_agent"):
        # FAR-402 P3 / FAR-417: a scatter node expands its split source into N
        # child branches at runtime, each with a unique correlation id.
        # Both agent and sandbox_agent nodes are executable as children, so
        # fan_out is honored for both (composite is rejected at validation
        # because it has no runtime child factory).
        graph.add_node(
            node_id,
            make_scatter_node_fn(
                node_def,
                timeout=timeout,
                session_factory=session_factory,
                single_sandbox_node=single_sandbox_node,
            ),
        )
        return

    graph.add_node(
        node_id,
        _make_node_fn(
            node_def,
            timeout=timeout,
            session_factory=session_factory,
            single_sandbox_node=single_sandbox_node,
        ),
    )


def _build_reject_targets(edges: list[dict[str, Any]]) -> dict[str, str]:
    """Map each source that has a reject edge to that edge's target."""
    return {
        _get_edge_val(edge_def, "source", "source_node_id"): _get_edge_val(edge_def, "target", "target_node_id")
        for edge_def in edges
        if _get_edge_type(edge_def) == "reject"
    }


def _group_forwarding_edges(edges: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    """Group non-reject edges by their source node id."""
    source_edges: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for edge_def in edges:
        if _get_edge_type(edge_def) == "reject":
            continue
        source = _get_edge_val(edge_def, "source", "source_node_id")
        source_edges[source].append(edge_def)
    return source_edges


def _normal_targets_for(edges: list[dict[str, Any]], target_ids: set[str]) -> list[str]:
    """Collect a node's normal edge targets, registering each as a graph target."""
    normal_targets = []
    for edge_def in edges:
        tgt = _get_edge_val(edge_def, "target", "target_node_id")
        normal_targets.append(tgt)
        target_ids.add(tgt)
    return normal_targets


def _resolve_loop_default_target(loop_edge: dict[str, Any], normal_targets: list[str], source: str) -> str:
    """Resolve a loop edge's exit target: explicit, first normal target, or error."""
    default_target_raw = loop_edge.get("default_target")
    if default_target_raw:
        return str(default_target_raw)
    if normal_targets:
        return normal_targets[0]
    msg = f"loop edge from '{source}' requires default_target (no normal targets available)"
    raise ValueError(msg)


def _add_loop_edges(
    graph: StateGraph[Any],
    source: str,
    loop_edges: list[dict[str, Any]],
    normal: list[dict[str, Any]],
    target_ids: set[str],
) -> None:
    """Compile loop edges via synthetic counter nodes and read-only routers.

    The counter lives on a synthetic NODE, not the router: LangGraph discards
    router-side in-place state mutations across supersteps, so max_iterations
    would never trip and the run would hang until GraphRecursionError. The
    counter node returns the incremented count as a real state update, and a
    read-only router on it picks the next hop.
    """
    normal_targets = [_get_edge_val(edge_def, "target", "target_node_id") for edge_def in normal]
    target_ids.update(normal_targets)

    for loop_edge in loop_edges:
        target = _get_edge_val(loop_edge, "target", "target_node_id")
        max_iterations = int(loop_edge.get("max_iterations", 0))
        condition_expression = loop_edge.get("condition_expression")
        default_target_str = _resolve_loop_default_target(loop_edge, normal_targets, source)

        loop_key = f"{source}->{target}"
        counter_id = _make_loop_counter_id(source, target)
        graph.add_node(counter_id, make_loop_counter_fn(loop_key))
        graph.add_edge(source, counter_id)

        counter_router = _make_loop_counter_router(
            loop_key,
            target,
            default_target_str,
            max_iterations,
            condition_expression,
        )
        graph.add_conditional_edges(counter_id, counter_router)
        target_ids.add(target)
        target_ids.add(default_target_str)


def _find_conditional_default(conditional_edges: list[dict[str, Any]]) -> str | None:
    """Return the last explicit default_target declared on any conditional edge."""
    default_target: str | None = None
    for edge_def in conditional_edges:
        dft = edge_def.get("default_target")
        if dft:
            default_target = str(dft)
    return default_target


def _add_llm_routing(
    graph: StateGraph[Any],
    source: str,
    source_node_def: dict[str, Any],
    conditional: list[dict[str, Any]],
    normal: list[dict[str, Any]],
    target_ids: set[str],
) -> None:
    """Compile LLM-driven conditional routing for a source node.

    All outgoing edges from this node are handled by the LLM router.
    """
    llm_edges = conditional or normal
    normal_targets = _normal_targets_for(normal, target_ids)
    default_target: str | None = source_node_def.get("default_target")

    router = _make_llm_router(llm_edges, normal_targets, default_target)
    graph.add_conditional_edges(source, router)


def _add_conditional_routing(
    graph: StateGraph[Any],
    source: str,
    conditional: list[dict[str, Any]],
    normal: list[dict[str, Any]],
    target_ids: set[str],
) -> None:
    """Compile JMESPath conditional routing for a source node.

    All outgoing edges from this source are handled by the router.
    """
    normal_targets = _normal_targets_for(normal, target_ids)
    default_target = _find_conditional_default(conditional)

    router = _make_conditional_router(conditional, normal_targets, default_target)
    graph.add_conditional_edges(source, router)


def _add_hitl_gate_edge(
    graph: StateGraph[Any],
    source: str,
    target: str,
    hitl_config: dict[str, Any],
    *,
    target_ids: set[str],
    gate_node_ids: set[str],
    reject_targets_by_source: dict[str, str],
    eval_definitions_by_node: dict[str, list[EvalDefinition]] | None,
    session_factory: Callable[..., Any] | None,
    org_id: uuid.UUID | None,
    node_type_map: dict[str, str],
) -> None:
    """Insert a HITL gate node between *source* and *target*.

    Kick-back target priority: gate config ``reject_target`` first, then the
    source's reject edge target.
    """
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
            node_type_map=node_type_map,
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
        gate_router = _make_gate_kickback_router(
            target,
            reject_target_str,
        )
        graph.add_conditional_edges(gate_id, gate_router)
        target_ids.add(reject_target_str)
    else:
        graph.add_edge(gate_id, target)

    gate_node_ids.add(gate_id)
    target_ids.add(gate_id)


def _add_normal_edges(
    graph: StateGraph[Any],
    source: str,
    normal: list[dict[str, Any]],
    *,
    target_ids: set[str],
    gate_node_ids: set[str],
    reject_targets_by_source: dict[str, str],
    eval_definitions_by_node: dict[str, list[EvalDefinition]] | None,
    session_factory: Callable[..., Any] | None,
    org_id: uuid.UUID | None,
    node_type_map: dict[str, str],
) -> None:
    """Add direct edges (or HITL-gated edges) for a source's normal edges."""
    for edge_def in normal:
        target = _get_edge_val(edge_def, "target", "target_node_id")
        hitl_config = edge_def.get("hitl_gate_config")
        if hitl_config:
            _add_hitl_gate_edge(
                graph,
                source,
                target,
                hitl_config,
                target_ids=target_ids,
                gate_node_ids=gate_node_ids,
                reject_targets_by_source=reject_targets_by_source,
                eval_definitions_by_node=eval_definitions_by_node,
                session_factory=session_factory,
                org_id=org_id,
                node_type_map=node_type_map,
            )
        else:
            target_ids.add(target)
            graph.add_edge(source, target)


def build_graph_from_json(
    graph_json: dict[str, Any],
    *,
    eval_definitions_by_node: dict[str, list[EvalDefinition]] | None = None,
    session_factory: Callable[..., Any] | None = None,
    org_id: uuid.UUID | None = None,
    pipeline_node_timeout_seconds: int = 300,
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
    router — normal edges from that source serve as fallback targets (the
    router picks ONE target; this is NOT a fan-out).

    Parallel fan-out (FAR-171 / ``parallel_branches``): when a source has
    MULTIPLE normal (non-conditional, non-loop) outgoing edges and no
    conditional/llm-routing/loop edges, each edge is added directly via
    ``graph.add_edge(source, target)`` — LangGraph's native parallel fan-out.
    All downstream branches run in the same superstep (wall-clock ≈ max, not
    sum) and their state updates are merged by ``_pipeline_state_reducer``
    (list keys concatenate in completion order; ``run_context`` merges
    per-key last-write-wins). Single-outgoing-edge sources compile identically
    to before, so the semantics for graphs without fan-out are unchanged.

    Returns a compiled LangGraph that accepts dict[str, Any] state.
    """
    state_schema = cast("type[Any]", Annotated[dict[str, Any], _pipeline_state_reducer])
    graph: StateGraph[Any] = StateGraph(state_schema)

    # FAR-416 (FAR-402 F1): lazy backfill. Synthesize default out/in ports for
    # legacy (port-less) nodes at load/first-compile. Ports are ADDITIVE metadata
    # over the flat run_context/artifact dict — the runtime still reads/writes
    # the flat dict unchanged; this only ensures every node has a port model.
    nodes: list[dict[str, Any]] = [synthesize_node_ports(n) for n in graph_json.get("nodes", [])]
    edges: list[dict[str, Any]] = graph_json.get("edges", [])

    if not nodes:
        raise ValueError("graph_json has no nodes")

    # FAR-311: node type map for the HITL gate eval path — the gate validates
    # the source node's CONTRACT output (not the merged state), so it needs the
    # source node's type to split the envelope. Derived from the immutable
    # graph_json, so it is identical for every compiled snapshot.
    node_type_map = {str(n["id"]): (n.get("node_type") or DEFAULT_NODE_TYPE) for n in nodes if n.get("id")}

    # FAR-228: the idempotency gate is inert on multi-node graphs. Computed ONCE
    # here and threaded into the sandbox node builder so guard A (early skip)
    # can require it without re-deriving from the node's own def.
    single_sandbox_node = _count_sandbox_nodes(nodes)

    for node_def in nodes:
        timeout: int | None = node_def.get("timeout_seconds")
        if timeout is None:
            timeout = pipeline_node_timeout_seconds
        _add_node_to_graph(
            graph,
            node_def,
            timeout=timeout,
            session_factory=session_factory,
            single_sandbox_node=single_sandbox_node,
        )

    # Build reject-edge lookup for kick-back routing.
    reject_targets_by_source = _build_reject_targets(edges)

    # Group forwarding edges by source (skip reject).
    source_edges = _group_forwarding_edges(edges)

    target_ids: set[str] = set()
    gate_node_ids: set[str] = set()

    # Build a node-id-to-def lookup for quick access.
    nodes_by_id: dict[str, dict[str, Any]] = {str(n["id"]): n for n in nodes}

    # Router nodes route via `router_config` (not outgoing edges), so their rule
    # targets are never seen by the edge-driven `target_ids` population below.
    # Register every router rule/default target here so the entry-point selection
    # cannot pick a router's rule target as the pipeline entry node (FAR-415: a
    # router whose rule target sorts first in `nodes` would otherwise become the
    # entry, leaving the real entry + router dead).
    for _n in nodes:
        if (_n.get("node_type") or "agent") == "router":
            _rc = _n.get("router_config") or {}
            for _rule in _rc.get("rules", []) or []:
                _tgt = _rule.get("target") or _rule.get("target_port")
                if _tgt is not None:
                    target_ids.add(str(_tgt))

    for source, src_edges in source_edges.items():
        source_node_def = nodes_by_id.get(source, {})
        routing_mode: str | None = source_node_def.get("routing_mode")
        source_node_type: str = source_node_def.get("node_type", "agent")

        conditional = [e for e in src_edges if _get_edge_type(e) == "conditional"]
        loop_edges = [e for e in src_edges if _get_edge_type(e) == "loop"]
        normal = [e for e in src_edges if _get_edge_type(e) not in ("conditional", "loop")]

        if source_node_type == "router":
            # Router node (FAR-402 P1 / F2-A): lowers to the existing
            # conditional-edge compile path via the shared JMESPath evaluator.
            # Compile-time default-rule enforcement guards mis-configured graphs.
            router_config = dict(source_node_def.get("router_config") or {})
            _validate_router_config(router_config, source)
            graph.add_conditional_edges(source, make_router_node_fn(router_config, node_id=source))
        elif source_node_type == "hitl":
            # HITL node (FAR-402 P1 / F2-D): compile-equivalent to the legacy
            # edge-level HITL gate. Inject the node's hitl_config onto every
            # outgoing (normal) edge so the existing synthetic-gate path picks
            # it up unchanged.
            hitl_config = dict(source_node_def.get("hitl_config") or {})
            for edge in normal:
                gate_id = _make_gate_id(source, _get_edge_val(edge, "target", "target_node_id"))
                edge["hitl_gate_config"] = {**hitl_config, "gate_id": gate_id}
            _add_normal_edges(
                graph,
                source,
                normal,
                target_ids=target_ids,
                gate_node_ids=gate_node_ids,
                reject_targets_by_source=reject_targets_by_source,
                eval_definitions_by_node=eval_definitions_by_node,
                session_factory=session_factory,
                org_id=org_id,
                node_type_map=node_type_map,
            )
        elif loop_edges:
            _add_loop_edges(graph, source, loop_edges, normal, target_ids)
        elif routing_mode == "llm":
            _add_llm_routing(graph, source, source_node_def, conditional, normal, target_ids)
        elif conditional:
            _add_conditional_routing(graph, source, conditional, normal, target_ids)
        else:
            _add_normal_edges(
                graph,
                source,
                normal,
                target_ids=target_ids,
                gate_node_ids=gate_node_ids,
                reject_targets_by_source=reject_targets_by_source,
                eval_definitions_by_node=eval_definitions_by_node,
                session_factory=session_factory,
                org_id=org_id,
                node_type_map=node_type_map,
            )

    entry_candidates = [str(n["id"]) for n in nodes if str(n["id"]) not in target_ids]
    if not entry_candidates:
        raise ValueError("graph_json has a cycle or no entry node")
    graph.set_entry_point(entry_candidates[0])

    return graph.compile()


def evict(pipeline_id: uuid.UUID, snapshot_id: uuid.UUID) -> None:
    """Remove every cached entry for a (pipeline_id, snapshot_id) pair.

    A snapshot can hold multiple compiled graphs — one per
    pipeline_node_timeout_seconds — so all variants are removed.
    """
    for key in [k for k in _CACHE if k[0] == pipeline_id and k[1] == snapshot_id]:
        _CACHE.pop(key, None)
        _compile_locks.pop(key, None)
