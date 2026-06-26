"""Unit tests for graph cache and graph compilation."""

import uuid
from typing import Any

import pytest

from modulo.core.pipeline_engine.graph_cache import (
    _CACHE,
    _MAX_SIZE,
    _make_gate_kickback_router,
    build_graph_from_json,
    evict,
    get_or_compile,
)


def _clear_cache() -> None:
    _CACHE.clear()


# ---------------------------------------------------------------------------
# get_or_compile
# ---------------------------------------------------------------------------


def test_get_or_compile_calls_factory_once():
    _clear_cache()
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
    _clear_cache()
    sid = uuid.uuid4()
    calls: list[str] = []

    get_or_compile(uuid.uuid4(), sid, lambda: calls.append("a") or "a")
    get_or_compile(uuid.uuid4(), sid, lambda: calls.append("b") or "b")

    assert calls == ["a", "b"]


def test_evict_removes_entry():
    _clear_cache()
    pid, sid = uuid.uuid4(), uuid.uuid4()
    get_or_compile(pid, sid, lambda: "cached")
    assert (pid, sid) in _CACHE

    evict(pid, sid)
    assert (pid, sid) not in _CACHE


def test_cache_evicts_oldest_when_full():
    _clear_cache()
    base_sid = uuid.uuid4()
    for i in range(_MAX_SIZE):
        get_or_compile(uuid.uuid4(), base_sid, lambda: "v")
    # Each entry has a unique pipeline_id, so they fill all slots.
    first_key = next(iter(_CACHE))

    # One more entry should evict the oldest (least recently used).
    extra_pid = uuid.uuid4()
    get_or_compile(extra_pid, base_sid, lambda: "new")
    assert first_key not in _CACHE
    assert (extra_pid, base_sid) in _CACHE


def test_evict_does_not_affect_other_pipelines():
    _clear_cache()
    pid1, pid2, sid = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    get_or_compile(pid1, sid, lambda: "1")
    get_or_compile(pid2, sid, lambda: "2")

    evict(pid1, sid)
    assert (pid1, sid) not in _CACHE
    assert (pid2, sid) in _CACHE


def test_lru_moves_entry_on_access():
    _clear_cache()
    sid = uuid.uuid4()
    keys = [uuid.uuid4() for _ in range(3)]
    for k in keys:
        get_or_compile(k, sid, lambda: "v")
    # Access the first key, making it recently used
    get_or_compile(keys[0], sid, lambda: "v")
    assert next(iter(_CACHE)) == (keys[1], sid)


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
        {"source": "decider", "target": "pass-branch", "type": "conditional",
         "condition_expression": "artifacts[0].status == 'passed'"},
        {"source": "decider", "target": "fail-branch", "type": "conditional",
         "condition_expression": "artifacts[0].status == 'failed'"},
    ],
}


async def test_conditional_graph_compiles():
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
            {"source": "decider", "target": "pass-branch", "type": "conditional",
             "condition_expression": "artifacts[0].status == 'passed'",
             "default_target": "else-branch"},
            {"source": "decider", "target": "else-branch", "type": "conditional",
             "condition_expression": "artifacts[0].status == 'unknown'"},
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
            {"source": "router", "target": "high", "type": "conditional",
             "condition_expression": "artifacts[?node_id=='score'].score | [0] | @ > `75`"},
            {"source": "router", "target": "low", "type": "conditional",
             "condition_expression": "artifacts[?node_id=='score'].score | [0] | @ <= `75`"},
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
            {"source": "decider", "target": "special", "type": "conditional",
             "condition_expression": "artifacts[0].flag == true"},
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
            {"source": "router", "target": "high", "type": "conditional",
             "condition_expression": "artifacts[0].score > `50`"},
            {"source": "router", "target": "low", "type": "conditional",
             "condition_expression": "artifacts[0].score <= `50`"},
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
            {"source_node_id": "router", "target_node_id": "target",
             "edge_type": "conditional",
             "condition_expression": "artifacts[0].status == 'ok'"},
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
    router = _make_gate_kickback_router("normal_target", "reject_target")
    assert router({"_hitl_decision": {"action": "rejected"}}) == "reject_target"


def test_gate_kickback_router_routes_to_normal_on_approval():
    """Router returns normal_target when _hitl_decision action is approved."""
    router = _make_gate_kickback_router("normal_target", "reject_target")
    assert router({"_hitl_decision": {"action": "approved"}}) == "normal_target"


def test_gate_kickback_router_falls_back_to_normal_without_decision():
    """Router returns normal_target when no _hitl_decision is in state."""
    router = _make_gate_kickback_router("normal_target", "reject_target")
    assert router({}) == "normal_target"
    assert router({"some_key": "value"}) == "normal_target"


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
                "hitl_gate_config": {
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
                "hitl_gate_config": {
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
                "hitl_gate_config": {
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
                "hitl_gate_config": {
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
