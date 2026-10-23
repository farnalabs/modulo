"""Unit tests for graph cache and graph compilation."""

import uuid
from collections.abc import Callable
from typing import Any
from unittest.mock import patch

import pytest

from modulo.core.pipeline_engine.graph_cache import (
    _CACHE,
    _MAX_SIZE,
    _make_gate_kickback_router,
    _make_node_fn,
    build_graph_from_json,
    compute_eval_defs_hash,
    evict,
    get_or_compile,
    struct_hash_with_eval_defs,
)
from modulo.core.pipeline_engine.node_runner import _connector_inputs


@pytest.fixture(autouse=True)
def _auto_clear_cache() -> None:
    _CACHE.clear()


def _k(pid: uuid.UUID, sid: uuid.UUID, timeout: int, h: str = "") -> tuple:
    """Build a cache key tuple (matches graph_cache.CacheKey arity)."""
    return (pid, sid, timeout, h)


# ---------------------------------------------------------------------------
# get_or_compile
# ---------------------------------------------------------------------------


def test_get_or_compile_calls_factory_once():
    pid, sid = uuid.uuid4(), uuid.uuid4()
    call_count = 0

    def factory() -> str:
        nonlocal call_count
        call_count += 1
        return "compiled"

    result = get_or_compile(pid, sid, factory)
    assert result == "compiled"
    assert call_count == 1

    # Second call: cache hit, factory not called again
    result2 = get_or_compile(pid, sid, factory)
    assert result2 == "compiled"
    assert call_count == 1


def test_get_or_compile_different_pipeline_calls_factory():
    sid = uuid.uuid4()
    calls: list[str] = []

    get_or_compile(uuid.uuid4(), sid, lambda: calls.append("a") or "a")
    get_or_compile(uuid.uuid4(), sid, lambda: calls.append("b") or "b")

    assert calls == ["a", "b"]


def test_evict_removes_entry():
    pid, sid = uuid.uuid4(), uuid.uuid4()
    get_or_compile(pid, sid, lambda: "cached")
    assert _k(pid, sid, 300) in _CACHE

    evict(pid, sid)
    assert _k(pid, sid, 300) not in _CACHE


def test_cache_evicts_oldest_when_full():
    base_sid = uuid.uuid4()
    for i in range(_MAX_SIZE):
        get_or_compile(uuid.uuid4(), base_sid, lambda: "v")
    # Each entry has a unique pipeline_id, so they fill all slots.
    first_key = next(iter(_CACHE))

    # One more entry should evict the oldest (least recently used).
    extra_pid = uuid.uuid4()
    get_or_compile(extra_pid, base_sid, lambda: "new")
    assert first_key not in _CACHE
    assert _k(extra_pid, base_sid, 300) in _CACHE


def test_evict_does_not_affect_other_pipelines():
    pid1, pid2, sid = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    get_or_compile(pid1, sid, lambda: "1")
    get_or_compile(pid2, sid, lambda: "2")

    evict(pid1, sid)
    assert _k(pid1, sid, 300) not in _CACHE
    assert _k(pid2, sid, 300) in _CACHE


def test_lru_moves_entry_on_access():
    sid = uuid.uuid4()
    keys = [uuid.uuid4() for _ in range(3)]
    for k in keys:
        get_or_compile(k, sid, lambda: "v")
    # Access the first key, making it recently used
    get_or_compile(keys[0], sid, lambda: "v")
    assert next(iter(_CACHE)) == _k(keys[1], sid, 300)


def test_get_or_compile_distinguishes_node_timeout_values():
    """PATCHing pipeline.node_timeout_seconds must recompile, not serve a stale graph."""
    pid, sid = uuid.uuid4(), uuid.uuid4()
    calls: list[int] = []

    def factory_for(timeout: int) -> Callable[[], str]:
        def factory() -> str:
            calls.append(timeout)
            return f"compiled-{timeout}"

        return factory

    first = get_or_compile(pid, sid, factory_for(100), pipeline_node_timeout_seconds=100)
    second = get_or_compile(pid, sid, factory_for(200), pipeline_node_timeout_seconds=200)
    cached = get_or_compile(pid, sid, factory_for(100), pipeline_node_timeout_seconds=100)

    assert first == "compiled-100"
    assert second == "compiled-200"
    assert cached == "compiled-100"
    # Recompiled once for the new timeout; the original timeout is served from cache.
    assert calls == [100, 200]


def test_evict_removes_all_node_timeout_variants():
    pid, sid = uuid.uuid4(), uuid.uuid4()
    get_or_compile(pid, sid, lambda: "a", pipeline_node_timeout_seconds=100)
    get_or_compile(pid, sid, lambda: "b", pipeline_node_timeout_seconds=200)
    assert _k(pid, sid, 100) in _CACHE
    assert _k(pid, sid, 200) in _CACHE

    evict(pid, sid)
    assert _k(pid, sid, 100) not in _CACHE
    assert _k(pid, sid, 200) not in _CACHE


def test_port_topology_hash_forces_recompile():
    """FAR-416 / F1: a distinct port topology must recompile, not serve a stale graph."""
    from modulo.core.pipeline_engine.port_resolver import compute_port_topology_hash

    pid, sid = uuid.uuid4(), uuid.uuid4()
    calls: list[str] = []

    base_graph = {
        "nodes": [
            {"id": "a", "node_type": "agent", "outputs": [{"port": "out"}]},
            {"id": "b", "node_type": "agent", "inputs": [{"port": "in"}]},
        ],
        "edges": [{"source": "a", "target": "b", "type": "normal"}],
    }
    mutated_graph = {
        "nodes": [
            {"id": "a", "node_type": "agent", "outputs": [{"port": "out"}]},
            {"id": "b", "node_type": "agent", "inputs": [{"port": "different"}]},
        ],
        "edges": [{"source": "a", "target": "b", "type": "normal"}],
    }
    h1 = compute_port_topology_hash(base_graph)
    h2 = compute_port_topology_hash(mutated_graph)
    assert h1 != h2

    def factory_for(h: str) -> Callable[[], str]:
        def factory() -> str:
            calls.append(h)
            return f"compiled-{h}"

        return factory

    first = get_or_compile(pid, sid, factory_for(h1), graph_struct_hash=h1)
    second = get_or_compile(pid, sid, factory_for(h2), graph_struct_hash=h2)
    cached = get_or_compile(pid, sid, factory_for(h1), graph_struct_hash=h1)

    assert first == f"compiled-{h1}"
    assert second == f"compiled-{h2}"
    assert cached == f"compiled-{h1}"
    # Recompiled once for the new port topology; original hash served from cache.
    assert calls == [h1, h2]


# ---------------------------------------------------------------------------
# build_graph_from_json
# ---------------------------------------------------------------------------

_SIMPLE_GRAPH: dict[str, Any] = {
    "nodes": [
        {"id": "node-a", "role": None},
        {"id": "node-b", "role": None},
    ],
    "edges": [
        {"source": "node-a", "target": "node-b", "type": "normal"},
    ],
}


def test_build_graph_compiles_successfully():
    compiled = build_graph_from_json(_SIMPLE_GRAPH)
    assert compiled is not None


def test_build_graph_accepts_persisted_edge_endpoint_names():
    graph_json = {
        "nodes": [{"id": "node-a"}, {"id": "node-b"}],
        "edges": [
            {
                "source_node_id": "node-a",
                "target_node_id": "node-b",
                "edge_type": "normal",
            }
        ],
    }

    assert build_graph_from_json(graph_json) is not None


def test_build_graph_empty_nodes_raises():
    with pytest.raises(ValueError, match="no nodes"):
        build_graph_from_json({"nodes": [], "edges": []})


def test_build_graph_cycle_detection():
    # Two nodes that both point to each other — no entry point exists.
    graph_json: dict[str, Any] = {
        "nodes": [{"id": "a"}, {"id": "b"}],
        "edges": [
            {"source": "a", "target": "b"},
            {"source": "b", "target": "a"},
        ],
    }
    with pytest.raises(ValueError, match="cycle or no entry"):
        build_graph_from_json(graph_json)


def test_node_null_timeout_uses_pipeline_default():
    """A node with null timeout_seconds resolves to pipeline_node_timeout_seconds."""
    graph_json: dict[str, Any] = {
        "nodes": [{"id": "node-a", "role": None, "timeout_seconds": None}],
        "edges": [],
    }
    with patch("modulo.core.pipeline_engine.graph_cache.make_node_fn") as mock_make:
        mock_make.return_value = lambda state: state
        build_graph_from_json(graph_json, pipeline_node_timeout_seconds=1234)
        _, kwargs = mock_make.call_args
    assert kwargs["timeout"] == 1234


def test_node_explicit_timeout_wins_over_pipeline_default():
    """An explicit per-node timeout_seconds is honoured over the pipeline default."""
    graph_json: dict[str, Any] = {
        "nodes": [{"id": "node-a", "role": None, "timeout_seconds": 77}],
        "edges": [],
    }
    with patch("modulo.core.pipeline_engine.graph_cache.make_node_fn") as mock_make:
        mock_make.return_value = lambda state: state
        build_graph_from_json(graph_json, pipeline_node_timeout_seconds=1234)
        _, kwargs = mock_make.call_args
    assert kwargs["timeout"] == 77


async def test_built_graph_executes_simple_pipeline():
    compiled = build_graph_from_json(_SIMPLE_GRAPH)
    initial_state: dict[str, Any] = {
        "run_context": {"cancelled": False, "input": {}},
        "artifacts": [],
    }
    config = {"configurable": {"thread_id": str(uuid.uuid4())}}
    result = await compiled.ainvoke(initial_state, config)
    # Both nodes should have appended to artifacts
    assert len(result["artifacts"]) == 2
    node_ids = [a["node_id"] for a in result["artifacts"]]
    assert "node-a" in node_ids
    assert "node-b" in node_ids


# ---------------------------------------------------------------------------
# Conditional edges
# ---------------------------------------------------------------------------


_CONDITIONAL_GRAPH: dict[str, Any] = {
    "nodes": [
        {"id": "decider", "role": None},
        {"id": "pass-branch", "role": None},
        {"id": "fail-branch", "role": None},
    ],
    "edges": [
        {
            "source": "decider",
            "target": "pass-branch",
            "type": "conditional",
            "condition_expression": "artifacts[0].status == 'passed'",
        },
        {
            "source": "decider",
            "target": "fail-branch",
            "type": "conditional",
            "condition_expression": "artifacts[0].status == 'failed'",
        },
    ],
}


def test_conditional_graph_compiles():
    compiled = build_graph_from_json(_CONDITIONAL_GRAPH)
    assert compiled is not None


async def test_conditional_routes_to_pass_branch():
    compiled = build_graph_from_json(_CONDITIONAL_GRAPH)
    initial_state: dict[str, Any] = {
        "run_context": {"cancelled": False, "input": {}},
        "artifacts": [{"node_id": "prev", "status": "passed"}],
    }
    config = {"configurable": {"thread_id": str(uuid.uuid4())}}
    result = await compiled.ainvoke(initial_state, config)
    node_ids = [a["node_id"] for a in result["artifacts"]]
    assert "decider" in node_ids
    assert "pass-branch" in node_ids
    assert "fail-branch" not in node_ids


async def test_conditional_routes_to_fail_branch():
    compiled = build_graph_from_json(_CONDITIONAL_GRAPH)
    initial_state: dict[str, Any] = {
        "run_context": {"cancelled": False, "input": {}},
        "artifacts": [{"node_id": "prev", "status": "failed"}],
    }
    config = {"configurable": {"thread_id": str(uuid.uuid4())}}
    result = await compiled.ainvoke(initial_state, config)
    node_ids = [a["node_id"] for a in result["artifacts"]]
    assert "decider" in node_ids
    assert "fail-branch" in node_ids
    assert "pass-branch" not in node_ids


async def test_conditional_falls_back_to_default_target():
    """When no condition matches, the default_target edge is followed."""
    graph: dict[str, Any] = {
        "nodes": [
            {"id": "decider", "role": None},
            {"id": "pass-branch", "role": None},
            {"id": "else-branch", "role": None},
        ],
        "edges": [
            {
                "source": "decider",
                "target": "pass-branch",
                "type": "conditional",
                "condition_expression": "artifacts[0].status == 'passed'",
                "default_target": "else-branch",
            },
            {
                "source": "decider",
                "target": "else-branch",
                "type": "conditional",
                "condition_expression": "artifacts[0].status == 'unknown'",
            },
        ],
    }
    compiled = build_graph_from_json(graph)
    initial_state: dict[str, Any] = {
        "run_context": {"cancelled": False, "input": {}},
        "artifacts": [{"node_id": "prev", "status": "timeout"}],
    }
    config = {"configurable": {"thread_id": str(uuid.uuid4())}}
    result = await compiled.ainvoke(initial_state, config)
    node_ids = [a["node_id"] for a in result["artifacts"]]
    assert "else-branch" in node_ids
    assert "pass-branch" not in node_ids


async def test_conditional_routes_using_artifact_field_values():
    """JMESPath can drill into nested artifact fields to decide routing."""
    graph: dict[str, Any] = {
        "nodes": [
            {"id": "router", "role": None},
            {"id": "high", "role": None},
            {"id": "low", "role": None},
        ],
        "edges": [
            {
                "source": "router",
                "target": "high",
                "type": "conditional",
                "condition_expression": "artifacts[?node_id=='score'].score | [0] | @ > `75`",
            },
            {
                "source": "router",
                "target": "low",
                "type": "conditional",
                "condition_expression": "artifacts[?node_id=='score'].score | [0] | @ <= `75`",
            },
        ],
    }
    compiled = build_graph_from_json(graph)
    initial_state: dict[str, Any] = {
        "run_context": {"cancelled": False, "input": {}},
        "artifacts": [{"node_id": "score", "score": 92}],
    }
    config = {"configurable": {"thread_id": str(uuid.uuid4())}}
    result = await compiled.ainvoke(initial_state, config)
    node_ids = [a["node_id"] for a in result["artifacts"]]
    assert "high" in node_ids
    assert "low" not in node_ids


async def test_conditional_with_normal_fallback():
    """Normal edges from the same source serve as fallback when no condition matches."""
    graph: dict[str, Any] = {
        "nodes": [
            {"id": "decider", "role": None},
            {"id": "special", "role": None},
            {"id": "default-path", "role": None},
        ],
        "edges": [
            {
                "source": "decider",
                "target": "special",
                "type": "conditional",
                "condition_expression": "artifacts[0].flag == true",
            },
            {"source": "decider", "target": "default-path", "type": "normal"},
        ],
    }
    compiled = build_graph_from_json(graph)
    initial_state: dict[str, Any] = {
        "run_context": {"cancelled": False, "input": {}},
        "artifacts": [{"node_id": "prev", "flag": False}],
    }
    config = {"configurable": {"thread_id": str(uuid.uuid4())}}
    result = await compiled.ainvoke(initial_state, config)
    node_ids = [a["node_id"] for a in result["artifacts"]]
    assert "default-path" in node_ids
    assert "special" not in node_ids


async def test_conditional_first_matching_wins():
    """When multiple conditions match, the first declared edge is taken."""
    graph: dict[str, Any] = {
        "nodes": [
            {"id": "router", "role": None},
            {"id": "high", "role": None},
            {"id": "low", "role": None},
        ],
        "edges": [
            {
                "source": "router",
                "target": "high",
                "type": "conditional",
                "condition_expression": "artifacts[0].score > `50`",
            },
            {
                "source": "router",
                "target": "low",
                "type": "conditional",
                "condition_expression": "artifacts[0].score <= `50`",
            },
        ],
    }
    compiled = build_graph_from_json(graph)
    initial_state: dict[str, Any] = {
        "run_context": {"cancelled": False, "input": {}},
        "artifacts": [{"node_id": "prev", "score": 92}],
    }
    config = {"configurable": {"thread_id": str(uuid.uuid4())}}
    result = await compiled.ainvoke(initial_state, config)
    node_ids = [a["node_id"] for a in result["artifacts"]]
    assert "high" in node_ids
    assert "low" not in node_ids


async def test_conditional_accepts_persisted_naming():
    """Conditional edges work with persisted (edge_type/source_node_id/target_node_id) naming."""
    graph: dict[str, Any] = {
        "nodes": [
            {"id": "router", "role": None},
            {"id": "target", "role": None},
        ],
        "edges": [
            {
                "source_node_id": "router",
                "target_node_id": "target",
                "edge_type": "conditional",
                "condition_expression": "artifacts[0].status == 'ok'",
            },
        ],
    }
    compiled = build_graph_from_json(graph)
    initial_state: dict[str, Any] = {
        "run_context": {"cancelled": False, "input": {}},
        "artifacts": [{"node_id": "prev", "status": "ok"}],
    }
    config = {"configurable": {"thread_id": str(uuid.uuid4())}}
    result = await compiled.ainvoke(initial_state, config)
    assert "target" in [a["node_id"] for a in result["artifacts"]]


# ---------------------------------------------------------------------------
# Kick-back edges (HITL rejection routing)
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Kick-back edge router — pure function tests
# ---------------------------------------------------------------------------


def test_gate_kickback_router_routes_to_reject_on_rejection():
    """Router returns reject_target when _hitl_decision action is rejected."""
    router = _make_gate_kickback_router("normal_target", "reject_target", review_id="hitl_review_src_tgt")
    assert router({"_hitl_decision": {"action": "rejected", "review_id": "hitl_review_src_tgt"}}) == "reject_target"


def test_gate_kickback_router_routes_to_normal_on_approval():
    """Router returns normal_target when _hitl_decision action is approved."""
    router = _make_gate_kickback_router("normal_target", "reject_target", review_id="hitl_review_src_tgt")
    assert router({"_hitl_decision": {"action": "approved", "review_id": "hitl_review_src_tgt"}}) == "normal_target"


def test_gate_kickback_router_falls_back_to_normal_without_decision():
    """Router returns normal_target when no _hitl_decision is in state."""
    router = _make_gate_kickback_router("normal_target", "reject_target", review_id="hitl_review_src_tgt")
    assert router({}) == "normal_target"
    assert router({"some_key": "value"}) == "normal_target"


def test_gate_kickback_router_rejection_kicks_back_to_reject_no_correction_marker():
    """FAR-210 MAJOR-5 (Option B): the reject→correction router is dead routing
    state — no correction node exists in node_runner and nothing reads
    ``_correction_pending``. A rejection kicks back to the plain reject target
    and stamps NO ``_correction_pending`` marker."""
    router = _make_gate_kickback_router("normal_target", "reject_target", review_id="hitl_review_src_tgt")
    state: dict[str, Any] = {"_hitl_decision": {"action": "rejected", "review_id": "hitl_review_src_tgt"}}
    assert router(state) == "reject_target"
    assert "_correction_pending" not in state


def test_gate_kickback_router_without_correction_target_kicks_back_to_reject():
    """Back-compat: a rejection kicks back to reject_target and stamps no
    correction marker (T1's recover_node override stays the break-glass path)."""
    router = _make_gate_kickback_router("normal_target", "reject_target", review_id="hitl_review_src_tgt")
    state: dict[str, Any] = {"_hitl_decision": {"action": "rejected", "review_id": "hitl_review_src_tgt"}}
    assert router(state) == "reject_target"
    assert "_correction_pending" not in state


def test_gate_kickback_router_approval_routes_to_normal_target():
    """Approval (and absence of a decision) route to normal_target."""
    router = _make_gate_kickback_router("normal_target", "reject_target", review_id="hitl_review_src_tgt")
    assert router({"_hitl_decision": {"action": "approved", "review_id": "hitl_review_src_tgt"}}) == "normal_target"
    assert router({}) == "normal_target"


# ---------------------------------------------------------------------------
# Kick-back graph compilation — structural tests
# ---------------------------------------------------------------------------


def test_gate_with_reject_target_compiles():
    """A graph with a gate that has reject_target compiles without error."""
    graph: dict[str, Any] = {
        "nodes": [
            {"id": "source", "role": None},
            {"id": "target", "role": None},
            {"id": "kickback_target", "role": None},
        ],
        "edges": [
            {
                "source": "source",
                "target": "target",
                "type": "normal",
                "hitl_review_config": {
                    "label": "Review",
                    "description": "Gate",
                    "reject_target": "kickback_target",
                    "claim_expiry_minutes": 60,
                    "human_only": False,
                },
            },
        ],
    }
    compiled = build_graph_from_json(graph)
    assert compiled is not None


def test_gate_with_correction_target_compiles():
    """A gate whose hitl_review_config declares correction_target (the accepted API
    contract) compiles, but a rejection still kicks back to the plain reject
    target — the reject→correction dispatch seam is not wired (MAJOR-5 Option
    B), so there is no dead routing to a correction node."""
    graph: dict[str, Any] = {
        "nodes": [
            {"id": "source", "role": None},
            {"id": "target", "role": None},
            {"id": "kickback_target", "role": None},
            {"id": "correction_node", "role": None},
        ],
        "edges": [
            {
                "source": "source",
                "target": "target",
                "type": "normal",
                "hitl_review_config": {
                    "label": "Review",
                    "description": "Gate",
                    "reject_target": "kickback_target",
                    "correction_target": "correction_node",
                    "claim_expiry_minutes": 60,
                    "human_only": False,
                },
            },
        ],
    }
    compiled = build_graph_from_json(graph)
    assert compiled is not None


def test_gate_without_reject_target_compiles():
    """A gate without reject_target (no kickback) compiles normally."""
    graph: dict[str, Any] = {
        "nodes": [
            {"id": "source", "role": None},
            {"id": "target", "role": None},
        ],
        "edges": [
            {
                "source": "source",
                "target": "target",
                "type": "normal",
                "hitl_review_config": {
                    "label": "Review",
                    "description": "Gate",
                    "claim_expiry_minutes": 60,
                    "human_only": False,
                },
            },
        ],
    }
    compiled = build_graph_from_json(graph)
    assert compiled is not None


def test_inner_cache_check_returns_prepopulated_value():
    """The lock-guarded double-check serves a value another writer cached
    between the outer fast-path check and lock acquisition."""
    pid, sid = uuid.uuid4(), uuid.uuid4()

    class _RaceCache:
        def __init__(self) -> None:
            self._checks = 0

        def __contains__(self, _key: object) -> bool:
            self._checks += 1
            # First check is the outer fast-path (miss); the lock-guarded
            # double-check sees the key populated by a "concurrent writer".
            return self._checks > 1

        def __getitem__(self, _key: object) -> str:
            return "race-value"

        def move_to_end(self, _key: object) -> None:
            pass

    with patch("modulo.core.pipeline_engine.graph_cache._CACHE", _RaceCache()):
        result = get_or_compile(pid, sid, lambda: "factory-value")
    assert result == "race-value"


def test_reject_edge_missing_source_raises():
    """A reject edge without a source/source_node_id fails the edge lookup."""
    graph: dict[str, Any] = {
        "nodes": [{"id": "a"}, {"id": "b"}],
        "edges": [{"edge_type": "reject", "target_node_id": "b"}],
    }
    with pytest.raises(ValueError, match="missing source"):
        build_graph_from_json(graph)


def test_unknown_node_type_raises():
    """An unrecognised node_type is rejected at compile time."""
    graph: dict[str, Any] = {"nodes": [{"id": "a", "node_type": "bogus"}], "edges": []}
    with pytest.raises(ValueError, match="Unknown node_type"):
        build_graph_from_json(graph)


def test_sandbox_agent_node_compiles():
    """A sandbox_agent node is added via make_sandbox_agent_fn."""
    graph: dict[str, Any] = {
        "nodes": [
            {
                "id": "agent",
                "node_type": "sandbox_agent",
                "agent_prompt": "summarise",
                "agent_commands": ["opencode run"],
            },
        ],
        "edges": [],
    }
    compiled = build_graph_from_json(graph)
    assert compiled is not None


def test_connector_node_compiles():
    """A connector node with a binding is added via make_connector_fn."""
    graph: dict[str, Any] = {
        "nodes": [
            {
                "id": "conn",
                "node_type": "connector",
                "connector_binding": {"instance_id": str(uuid.uuid4()), "type": "shell", "operation": "query"},
            },
        ],
        "edges": [],
    }
    compiled = build_graph_from_json(graph)
    assert compiled is not None


def test_connector_node_routes_its_binding_to_the_connector_fn():
    """FAR-2089: a ``connector`` node resolves to ``make_connector_fn``, query intact.

    ``test_connector_node_compiles`` proves compilation succeeds; this pins WHICH
    factory the type resolves to (an accepted-but-unrouted type would fall
    through to the LLM node factory) and that the saved ``resource`` /
    ``filters`` / ``data`` are what ``_connector_inputs`` hands the connector.
    """
    binding: dict[str, Any] = {
        "instance_id": str(uuid.uuid4()),
        "type": "github",
        "operation": "query",
        "resource": "pull",
        "filters": {"state": "open", "labels": ["agent"]},
        "data": {"note": "round-trip"},
    }
    node_def: dict[str, Any] = {"id": "conn", "node_type": "connector", "connector_binding": binding}
    sentinel = object()

    with patch(
        "modulo.core.pipeline_engine.graph_cache.make_connector_fn",
        return_value=sentinel,
    ) as make_connector:
        node_fn = _make_node_fn(node_def, timeout=30, session_factory=None, single_sandbox_node=False)

    assert node_fn is sentinel
    routed_def = make_connector.call_args.args[0]
    assert routed_def["node_type"] == "connector"
    resource, filters, data = _connector_inputs(routed_def["connector_binding"], {})
    assert resource == "pull"
    assert filters["state"] == "open"
    assert filters["labels"] == ["agent"]
    assert data["note"] == "round-trip"


def test_dispatch_node_compiles():
    """FAR-1141: a dispatch node with a binding compiles; bogus types still raise."""
    graph: dict[str, Any] = {
        "nodes": [
            {
                "id": "dispatch",
                "node_type": "dispatch",
                "connector_binding": {
                    "instance_id": str(uuid.uuid4()),
                    "type": "github_actions_ci",
                    "operation": "dispatch",
                    "dispatch_action": "trigger_run",
                },
            },
        ],
        "edges": [],
    }
    compiled = build_graph_from_json(graph)
    assert compiled is not None

    # the acceptance is ADDITIVE — an unknown node_type is still rejected
    bogus: dict[str, Any] = {"nodes": [{"id": "a", "node_type": "bogus"}], "edges": []}
    with pytest.raises(ValueError, match="Unknown node_type"):
        build_graph_from_json(bogus)


def test_loop_edge_defaults_to_first_normal_target():
    """A loop edge without default_target falls back to the first normal target."""
    graph: dict[str, Any] = {
        "nodes": [{"id": "loop"}, {"id": "a"}, {"id": "b"}],
        "edges": [
            {"source": "loop", "target": "a", "type": "loop", "max_iterations": 2},
            {"source": "loop", "target": "b", "type": "normal"},
        ],
    }
    compiled = build_graph_from_json(graph)
    assert compiled is not None


def test_gate_with_reject_edge_type_compiles():
    """A graph with a reject-type edge (as kickback source) compiles."""
    graph: dict[str, Any] = {
        "nodes": [
            {"id": "source", "role": None},
            {"id": "target", "role": None},
            {"id": "fallback_node", "role": None},
        ],
        "edges": [
            {
                "source": "source",
                "target": "target",
                "type": "normal",
                "hitl_review_config": {
                    "label": "Review",
                    "description": "Gate",
                    "claim_expiry_minutes": 60,
                    "human_only": False,
                },
            },
            {
                "source_node_id": "source",
                "target_node_id": "fallback_node",
                "edge_type": "reject",
            },
        ],
    }
    compiled = build_graph_from_json(graph)
    assert compiled is not None


# ---------------------------------------------------------------------------
# FAR-502: eval-defs cache-key folding (replay/resume eval staleness)
# ---------------------------------------------------------------------------


def _eval_def(
    config: dict[str, Any] | None = None,
    eval_id: uuid.UUID | None = None,
    org_id: uuid.UUID | None = None,
    pipeline_id: uuid.UUID | None = None,
) -> Any:
    from modulo.core.pipeline_engine.eval_persist_order import EvalDefDTO

    return EvalDefDTO(
        id=eval_id or uuid.uuid4(),
        org_id=org_id or uuid.uuid4(),
        pipeline_id=pipeline_id or uuid.uuid4(),
        node_id="A",
        name="gate-eval",
        eval_type="regex",
        config=config or {"pattern": "v1"},
        failure_behaviour="warn",
        version=1,
    )


def test_compute_eval_defs_hash_stable_and_sensitive():
    shared_org, shared_pipeline, shared_id = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    e1 = _eval_def(eval_id=shared_id, org_id=shared_org, pipeline_id=shared_pipeline)
    # Same content -> same hash (deterministic).
    assert compute_eval_defs_hash({"A": [e1]}) == compute_eval_defs_hash({"A": [e1]})
    # created_at is excluded: the DTO stamps datetime.now() on every load, so
    # two loads of identical eval rows must hash identically.
    e1_reloaded = _eval_def(config=dict(e1.config), eval_id=shared_id, org_id=shared_org, pipeline_id=shared_pipeline)
    assert compute_eval_defs_hash({"A": [e1]}) == compute_eval_defs_hash({"A": [e1_reloaded]})
    # A content change -> different hash.
    e2 = _eval_def(config={"pattern": "v2"}, eval_id=shared_id, org_id=shared_org, pipeline_id=shared_pipeline)
    assert compute_eval_defs_hash({"A": [e1]}) != compute_eval_defs_hash({"A": [e2]})
    # Per-node list order (DB return order) does not affect the hash.
    assert compute_eval_defs_hash({"A": [e1, e2]}) == compute_eval_defs_hash({"A": [e2, e1]})
    # No eval defs -> empty hash (call site leaves the struct hash unchanged).
    assert not compute_eval_defs_hash(None)
    assert not compute_eval_defs_hash({})


def test_struct_hash_with_eval_defs_folds_and_preserves_base():
    base = "topo-hash"
    shared_org, shared_pipeline, shared_id = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    defs = {"A": [_eval_def(eval_id=shared_id, org_id=shared_org, pipeline_id=shared_pipeline)]}
    folded = struct_hash_with_eval_defs(base, defs)
    assert folded != base
    assert folded.startswith(f"{base}:")
    # No eval defs -> unchanged base hash (graphs without evals share a key).
    assert struct_hash_with_eval_defs(base, None) == base
    assert struct_hash_with_eval_defs(base, {}) == base
    # Changed eval definitions change the folded hash.
    changed = struct_hash_with_eval_defs(
        base,
        {"A": [_eval_def(config={"pattern": "v2"}, eval_id=shared_id, org_id=shared_org, pipeline_id=shared_pipeline)]},
    )
    assert changed != folded


def test_get_or_compile_recompiles_when_eval_defs_change():
    """FAR-502 core mechanism: same (pipeline, snapshot, timeout) with CHANGED
    eval definitions folded into struct_hash -> cache miss -> recompile, while
    unchanged eval definitions keep hitting the cached graph."""
    pid, sid = uuid.uuid4(), uuid.uuid4()
    e1 = _eval_def(config={"pattern": "v1"})
    e2 = _eval_def(config={"pattern": "v2"})
    h1 = struct_hash_with_eval_defs("topo", {"A": [e1]})
    h2 = struct_hash_with_eval_defs("topo", {"A": [e2]})
    assert h1 != h2

    compiled1 = get_or_compile(pid, sid, lambda: "graph-e1", graph_struct_hash=h1)
    # Replay with CHANGED eval defs: different key -> fresh compile with E2.
    compiled2 = get_or_compile(pid, sid, lambda: "graph-e2", graph_struct_hash=h2)
    assert compiled2 == "graph-e2"
    assert compiled2 != compiled1
    # Replay with UNCHANGED eval defs: same key -> cached graph.
    assert get_or_compile(pid, sid, lambda: "should-not-run", graph_struct_hash=h1) is compiled1


# ---------------------------------------------------------------------------
# FAR-939 chunk 12 (M7) — the dispatch classifier, the validator and the
# compiler agree on "which edges compile a gate"
# ---------------------------------------------------------------------------


def _predicted_gate_nodes(graph_json: dict[str, Any]) -> set[str]:
    """Predicted gate node ids from the SHARED classifier.

    This is the exact predicate the chunk-12 human-mode validation reads
    (``_effective_gate_edges_for_source`` delegates to
    ``classify_source_dispatch``), grouped exactly as ``_assemble_edges``
    groups the compile — so it must equal what the compiler actually
    inserts, otherwise a desync silently reopens the no-pause hole §3.3
    exists to close.
    """
    from modulo.core.pipeline_engine.graph_cache import (
        _group_forwarding_edges,
        _make_review_id,
        classify_source_dispatch,
    )

    nodes_by_id = {str(n["id"]): n for n in graph_json["nodes"]}
    predicted: set[str] = set()
    for source, src_edges in _group_forwarding_edges(graph_json["edges"]).items():
        dispatch = classify_source_dispatch(src_edges, nodes_by_id.get(source, {}))
        for gate_edge in dispatch.gate_edges:
            target = str(gate_edge.get("target") or gate_edge.get("target_node_id"))
            predicted.add(_make_review_id(source, target))
    return predicted


def _compiled_gate_nodes(graph_json: dict[str, Any]) -> set[str]:
    compiled = build_graph_from_json(graph_json)
    return {name for name in compiled.get_graph().nodes if name.startswith("hitl_review_")}


_HTL_EDGE = {"source": "node-a", "target": "node-b", "type": "normal", "hitl_review_config": {"human_only": True}}

_M7_CASES: dict[str, dict[str, Any]] = {
    "normal_with_hitl_config": {
        "nodes": [{"id": "node-a", "role": None}, {"id": "node-b", "role": None}],
        "edges": [dict(_HTL_EDGE)],
    },
    "normal_without_config": {
        "nodes": [{"id": "node-a", "role": None}, {"id": "node-b", "role": None}],
        "edges": [{"source": "node-a", "target": "node-b", "type": "normal"}],
    },
    "hitl_node_stamps_its_normal_edges": {
        "nodes": [
            {"id": "node-a", "node_type": "hitl", "hitl_config": {"label": "review", "human_only": True}},
            {"id": "node-b", "role": None},
            {"id": "node-c", "role": None},
        ],
        "edges": [
            {"source": "node-a", "target": "node-b", "type": "normal"},
            {"source": "node-a", "target": "node-c", "type": "normal"},
        ],
    },
    "router_branch_never_compiles_gates": {
        "nodes": [
            {
                "id": "node-a",
                "node_type": "router",
                "router_config": {"rules": [{"default": True, "target": "node-b"}]},
            },
            {"id": "node-b", "role": None},
        ],
        "edges": [dict(_HTL_EDGE)],
    },
    "loop_branch_never_compiles_gates": {
        "nodes": [{"id": "node-a", "role": None}, {"id": "node-b", "role": None}],
        "edges": [
            {"source": "node-a", "target": "node-b", "type": "loop", "max_iterations": 3},
            dict(_HTL_EDGE),
        ],
    },
    "llm_branch_never_compiles_gates": {
        "nodes": [{"id": "node-a", "role": None, "routing_mode": "llm"}, {"id": "node-b", "role": None}],
        "edges": [dict(_HTL_EDGE)],
    },
    "conditional_branch_never_compiles_gates": {
        "nodes": [{"id": "node-a", "role": None}, {"id": "node-b", "role": None}],
        "edges": [
            {
                "source": "node-a",
                "target": "node-b",
                "type": "conditional",
                "condition_expression": "true",
                "hitl_review_config": {"human_only": True},
            }
        ],
    },
}


@pytest.mark.parametrize("case_name", sorted(_M7_CASES))
def test_dispatch_classifier_gate_edges_match_the_compiled_gates(case_name: str) -> None:
    """M7 lockstep: for EVERY dispatch branch, the shared classifier's
    ``gate_edges`` predict exactly the gate nodes the compiler inserts.

    The chunk-12 human-mode validation reads the classifier; the compiler
    runs ``_link_sources``. Before M7 these were two independent copies of
    the precedence — a change to one would silently desync the other and a
    human-mode gate could compile with no pause edge at all.
    """
    graph_json = _M7_CASES[case_name]
    predicted = _predicted_gate_nodes(graph_json)
    actual = _compiled_gate_nodes(graph_json)
    assert predicted == actual, (
        f"{case_name}: classifier predicted {sorted(predicted)}, compiler built {sorted(actual)}"
    )


def test_classifier_pinned_expected_gate_counts_per_branch() -> None:
    """Pin the per-branch expectations themselves (not only their agreement)
    so a change that removes gates from BOTH sides still fails here."""
    expectations = {
        "normal_with_hitl_config": 1,
        "normal_without_config": 0,
        "hitl_node_stamps_its_normal_edges": 2,
        "router_branch_never_compiles_gates": 0,
        "loop_branch_never_compiles_gates": 0,
        "llm_branch_never_compiles_gates": 0,
        "conditional_branch_never_compiles_gates": 0,
    }
    for case_name, expected in expectations.items():
        predicted = _predicted_gate_nodes(_M7_CASES[case_name])
        assert len(predicted) == expected, case_name


def test_effective_gate_edges_delegates_to_the_shared_classifier() -> None:
    """M7 structural pin: the validator's edge selection must CALL the shared
    classifier (not re-state the precedence). If a future edit re-implements
    the dispatch chain inside ``_effective_gate_edges_for_source``, this fails
    and the two sources of truth are forced back together."""
    from modulo.core.pipeline_engine.graph_cache import (
        SourceDispatch,
        _effective_gate_edges_for_source,
    )

    sentinel_edge = {"source": "node-a", "target": "node-b", "hitl_review_config": {}}
    dispatch = SourceDispatch(
        branch="normal",
        conditional=[],
        loop_edges=[],
        normal=[sentinel_edge],
        gate_edges=[sentinel_edge],
    )
    with patch(
        "modulo.core.pipeline_engine.graph_cache.classify_source_dispatch",
        return_value=dispatch,
    ) as classifier:
        result = _effective_gate_edges_for_source([sentinel_edge], {"node_type": "agent"})

    classifier.assert_called_once()
    assert result == [sentinel_edge]


# ---------------------------------------------------------------------------
# FAR-939 chunk 12 §3.3 — human-mode pause-edge validation
# ---------------------------------------------------------------------------


def _human_mode_dto(node_id: str, *, mode: str = "human") -> Any:
    """A governed EvalDefDTO whose governing gate carries *mode*."""
    from modulo.core.pipeline_engine.eval_persist_order import EvalDefDTO

    return EvalDefDTO(
        id=uuid.uuid4(),
        org_id=uuid.uuid4(),
        node_id=node_id,
        name="human-gate-eval",
        eval_type="regex",
        config={},
        failure_behaviour="block",
        policy_gate_id=uuid.uuid4(),
        policy_gate_version=1,
        policy_gate_node_id=uuid.uuid4(),
        policy_gate_evaluation_mode=mode,
    )


_TWO_NODES: list[dict[str, Any]] = [{"id": "node-a", "role": None}, {"id": "node-b", "role": None}]


def test_validate_human_mode_returns_when_no_eval_is_human_governed() -> None:
    """An automatic-mode (or ungoverned) eval places NO pause-edge
    constraint on its node — a plain agent edge compiles without a gate and
    without a validation error. A validator that wrongly demanded a pause
    edge for automatic evals would refuse this graph at run start."""
    automatic = _human_mode_dto("node-a", mode="automatic")
    graph_json = {
        "nodes": list(_TWO_NODES),
        "edges": [{"source": "node-a", "target": "node-b", "type": "normal"}],
    }

    compiled = build_graph_from_json(graph_json, eval_definitions_by_node={"node-a": [automatic]})

    gate_nodes = {name for name in compiled.get_graph().nodes if name.startswith("hitl_review_")}
    assert not gate_nodes


def test_validate_human_mode_rejects_a_human_eval_with_no_gate_edge() -> None:
    """A human-governed eval whose node has no edge compiling an HITL gate
    would never be decided (the silent no-decision hole) — fail closed."""
    from modulo.core.pipeline_engine.graph_cache import _validate_human_mode_pause_edges

    human = _human_mode_dto("node-a", mode="human")

    with pytest.raises(ValueError, match="EXACTLY ONE"):
        _validate_human_mode_pause_edges(
            nodes=list(_TWO_NODES),
            edges=[{"source": "node-a", "target": "node-b", "type": "normal"}],
            eval_definitions_by_node={"node-a": [human]},
        )


def test_validate_human_mode_rejects_two_gate_edges() -> None:
    """An HITL-typed source with two normal edges compiles TWO gates for one
    evaluation — two interrupts for a single decision. Fail closed."""
    from modulo.core.pipeline_engine.graph_cache import _validate_human_mode_pause_edges

    human = _human_mode_dto("node-a", mode="human")
    nodes = [
        {"id": "node-a", "node_type": "hitl", "hitl_config": {"human_only": True}},
        {"id": "node-b", "role": None},
        {"id": "node-c", "role": None},
    ]
    edges = [
        {"source": "node-a", "target": "node-b", "type": "normal"},
        {"source": "node-a", "target": "node-c", "type": "normal"},
    ]

    with pytest.raises(ValueError, match="EXACTLY ONE"):
        _validate_human_mode_pause_edges(
            nodes=nodes,
            edges=edges,
            eval_definitions_by_node={"node-a": [human]},
        )


def test_validate_human_mode_accepts_exactly_one_human_only_gate_edge() -> None:
    """The legal shape: one outgoing edge whose review config is
    human-only — the gate is the eval's pause point, and the graph compiles
    with exactly that gate. A validator that rejected the legal shape would
    refuse this graph at run start."""
    human = _human_mode_dto("node-a", mode="human")
    graph_json = {
        "nodes": list(_TWO_NODES),
        "edges": [dict(_HTL_EDGE)],
    }

    compiled = build_graph_from_json(graph_json, eval_definitions_by_node={"node-a": [human]})

    gate_nodes = {name for name in compiled.get_graph().nodes if name.startswith("hitl_review_")}
    assert gate_nodes == {"hitl_review_node-a_node-b"}


def test_validate_human_mode_rejects_a_falsy_human_only_opt_out() -> None:
    """``human_only`` resolved falsy (``""`` here — the runtime's
    ``human_only_effective`` treats any falsy value as opt-out) is
    definitionally incompatible with a human-decided gate — fail closed."""
    from modulo.core.pipeline_engine.graph_cache import _validate_human_mode_pause_edges

    human = _human_mode_dto("node-a", mode="human")
    opted_out_edge = {
        "source": "node-a",
        "target": "node-b",
        "type": "normal",
        "hitl_review_config": {"human_only": ""},
    }

    with pytest.raises(ValueError, match="human_only"):
        _validate_human_mode_pause_edges(
            nodes=list(_TWO_NODES),
            edges=[opted_out_edge],
            eval_definitions_by_node={"node-a": [human]},
        )


def test_validate_human_mode_reads_node_level_config_when_the_edge_has_none() -> None:
    """An HITL-typed source stamps its node-level ``hitl_config`` onto its
    edges at compile time; when the edge carries no config the validator must
    validate the STAMP SOURCE — a node-level falsy ``human_only`` (the
    Pydantic-model bypass) is rejected the same way."""
    from modulo.core.pipeline_engine.graph_cache import _validate_human_mode_pause_edges

    human = _human_mode_dto("node-a", mode="human")
    nodes = [
        {"id": "node-a", "node_type": "hitl", "hitl_config": {"human_only": False}},
        {"id": "node-b", "role": None},
    ]
    edges = [{"source": "node-a", "target": "node-b", "type": "normal"}]

    with pytest.raises(ValueError, match="human_only"):
        _validate_human_mode_pause_edges(
            nodes=nodes,
            edges=edges,
            eval_definitions_by_node={"node-a": [human]},
        )
