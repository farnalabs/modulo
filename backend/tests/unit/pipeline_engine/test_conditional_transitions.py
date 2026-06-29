"""Unit tests for conditional transition logic in graph_cache.

Tests here focus on pure functions (routers, truthiness) and graph compilation
scenarios (parallel branches, eval-before-interrupt flow) that are complementary
to the integration-level tests in test_graph_cache.py.
"""

import uuid
from typing import Any

import pytest

from modulo.core.pipeline_engine.graph_cache import (
    _is_truthy,
    _make_conditional_router,
    _make_gate_kickback_router,
    build_graph_from_json,
)

# ---------------------------------------------------------------------------
# _is_truthy — JMESPath truthiness semantics
# ---------------------------------------------------------------------------


def test_is_truthy_none():
    assert _is_truthy(None) is False


def test_is_truthy_bool():
    assert _is_truthy(True) is True
    assert _is_truthy(False) is False


def test_is_truthy_numeric():
    assert _is_truthy(0) is False
    assert _is_truthy(0.0) is False
    assert _is_truthy(1) is True
    assert _is_truthy(-1) is True
    assert _is_truthy(3.14) is True


def test_is_truthy_collections():
    assert _is_truthy([]) is False
    assert _is_truthy([1]) is True
    assert _is_truthy({}) is False
    assert _is_truthy({"key": "val"}) is True


def test_is_truthy_string():
    assert _is_truthy("") is False
    assert _is_truthy("hello") is True


# ---------------------------------------------------------------------------
# _make_conditional_router — pure function edge cases
# ---------------------------------------------------------------------------


def test_conditional_router_no_match_no_normal_no_default_no_compiled():
    """Raises ValueError when nothing can route."""
    router = _make_conditional_router([], [], None)
    with pytest.raises(ValueError, match="no edges"):
        router({"status": "unknown"})


def test_conditional_router_no_match_has_normal():
    """Falls back to first normal target when no condition matches."""
    router = _make_conditional_router(
        [{"condition_expression": "status == 'active'", "target": "active-node"}],
        ["fallback-node"],
        None,
    )
    result = router({"status": "inactive"})
    assert result == "fallback-node"


def test_conditional_router_no_match_has_default():
    """Falls back to default_target when no condition matches and no normal edges."""
    router = _make_conditional_router(
        [{"condition_expression": "status == 'active'", "target": "active-node"}],
        [],
        "default-node",
    )
    result = router({"status": "inactive"})
    assert result == "default-node"


def test_conditional_router_no_match_no_normal_no_default_has_compiled():
    """Falls back to last conditional target when no match and no fallback configured."""
    router = _make_conditional_router(
        [
            {"condition_expression": "status == 'active'", "target": "active-node"},
            {"condition_expression": "status == 'pending'", "target": "pending-node"},
        ],
        [],
        None,
    )
    result = router({"status": "unknown"})
    assert result == "pending-node"


def test_conditional_router_first_match_wins():
    """First declared condition that matches is used, even if later conditions also match."""
    router = _make_conditional_router(
        [
            {"condition_expression": "score >= `50`", "target": "high"},
            {"condition_expression": "score >= `0`", "target": "low"},
        ],
        [],
        None,
    )
    result = router({"score": 75})
    assert result == "high"


def test_conditional_router_with_persisted_names():
    """Conditional edges work with persisted naming convention."""
    router = _make_conditional_router(
        [
            {
                "condition_expression": "artifacts[0].status == 'ok'",
                "target_node_id": "target-a",
            }
        ],
        [],
        None,
    )
    result = router(
        {"artifacts": [{"node_id": "prev", "status": "ok"}]}
    )
    assert result == "target-a"


def test_conditional_router_jmespath_nested():
    """JMESPath expression drills into nested state fields."""
    router = _make_conditional_router(
        [
            {
                "condition_expression": "artifacts[?node_id=='check'].passed | [0]",
                "target": "verified",
            }
        ],
        [],
        None,
    )
    result = router(
        {
            "artifacts": [
                {"node_id": "check", "passed": True},
            ]
        }
    )
    assert result == "verified"


# ---------------------------------------------------------------------------
# _make_gate_kickback_router — edge cases
# ---------------------------------------------------------------------------


def test_gate_kickback_router_empty_hitl_decision():
    """Empty _hitl_decision dict routes to normal_target."""
    router = _make_gate_kickback_router("normal", "reject")
    assert router({"_hitl_decision": {}}) == "normal"


def test_gate_kickback_router_non_dict_hitl_decision():
    """Non-dict _hitl_decision value routes to normal_target."""
    router = _make_gate_kickback_router("normal", "reject")
    assert router({"_hitl_decision": "garbage"}) == "normal"


def test_gate_kickback_router_no_action_key():
    """_hitl_decision without 'action' key routes to normal_target."""
    router = _make_gate_kickback_router("normal", "reject")
    assert router({"_hitl_decision": {"status": "pending"}}) == "normal"


def test_gate_kickback_router_non_rejected_action():
    """_hitl_decision with non-'rejected' action routes to normal_target."""
    router = _make_gate_kickback_router("normal", "reject")
    assert router({"_hitl_decision": {"action": "approved"}}) == "normal"
    assert router({"_hitl_decision": {"action": "escalated"}}) == "normal"


def test_gate_kickback_router_rejected_action():
    """_hitl_decision with 'rejected' action routes to reject_target."""
    router = _make_gate_kickback_router("normal", "reject")
    assert router({"_hitl_decision": {"action": "rejected"}}) == "reject"


# ---------------------------------------------------------------------------
# build_graph_from_json — parallel branches
# ---------------------------------------------------------------------------


def test_parallel_branches_compile():
    """A source with multiple normal outgoing edges compiles successfully."""
    graph: dict[str, Any] = {
        "nodes": [
            {"id": "fanout", "role": None},
            {"id": "branch-a", "role": None},
            {"id": "branch-b", "role": None},
        ],
        "edges": [
            {"source": "fanout", "target": "branch-a", "type": "normal"},
            {"source": "fanout", "target": "branch-b", "type": "normal"},
        ],
    }
    compiled = build_graph_from_json(graph)
    assert compiled is not None


async def test_parallel_branches_both_execute():
    """Both parallel branches execute when the fanout node runs."""
    graph: dict[str, Any] = {
        "nodes": [
            {"id": "fanout", "role": None},
            {"id": "branch-a", "role": None},
            {"id": "branch-b", "role": None},
        ],
        "edges": [
            {"source": "fanout", "target": "branch-a", "type": "normal"},
            {"source": "fanout", "target": "branch-b", "type": "normal"},
        ],
    }
    compiled = build_graph_from_json(graph)
    initial_state: dict[str, Any] = {
        "run_context": {"cancelled": False, "input": {}},
        "artifacts": [],
    }
    config = {"configurable": {"thread_id": str(uuid.uuid4())}}
    result = await compiled.ainvoke(initial_state, config)
    node_ids = [a["node_id"] for a in result["artifacts"]]
    assert "fanout" in node_ids
    assert "branch-a" in node_ids
    assert "branch-b" in node_ids


async def test_parallel_branches_with_conditional_source():
    """Parallel branches work correctly alongside conditional routing from other sources."""
    graph: dict[str, Any] = {
        "nodes": [
            {"id": "decider", "role": None},
            {"id": "fanout", "role": None},
            {"id": "branch-a", "role": None},
            {"id": "branch-b", "role": None},
        ],
        "edges": [
            {
                "source": "decider",
                "target": "fanout",
                "type": "conditional",
                "condition_expression": "artifacts[0].status == 'ok'",
            },
            {"source": "fanout", "target": "branch-a", "type": "normal"},
            {"source": "fanout", "target": "branch-b", "type": "normal"},
        ],
    }
    compiled = build_graph_from_json(graph)
    initial_state: dict[str, Any] = {
        "run_context": {"cancelled": False, "input": {}},
        "artifacts": [{"node_id": "prev", "status": "ok"}],
    }
    config = {"configurable": {"thread_id": str(uuid.uuid4())}}
    result = await compiled.ainvoke(initial_state, config)
    node_ids = [a["node_id"] for a in result["artifacts"]]
    assert "decider" in node_ids
    assert "fanout" in node_ids
    assert "branch-a" in node_ids
    assert "branch-b" in node_ids


# ---------------------------------------------------------------------------
# build_graph_from_json — HITL gate with reject routing
# ---------------------------------------------------------------------------


async def test_gate_with_reject_edge_routes_on_rejection():
    """HITL rejection via reject edge routes state back to reject target."""
    graph: dict[str, Any] = {
        "nodes": [
            {"id": "source", "role": None},
            {"id": "target", "role": None},
            {"id": "fixup", "role": None},
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
                "target_node_id": "fixup",
                "edge_type": "reject",
            },
        ],
    }
    compiled = build_graph_from_json(graph)
    initial_state: dict[str, Any] = {
        "run_context": {"cancelled": False, "input": {}},
        "artifacts": [],
        "_hitl_decision": {"action": "rejected"},
    }
    config = {"configurable": {"thread_id": str(uuid.uuid4())}}
    result = await compiled.ainvoke(initial_state, config)
    node_ids = [a["node_id"] for a in result["artifacts"]]
    assert "source" in node_ids
    assert "fixup" in node_ids
    assert "target" not in node_ids


async def test_gate_with_reject_target_routes_on_rejection():
    """HITL rejection via gate config reject_target routes to that target."""
    graph: dict[str, Any] = {
        "nodes": [
            {"id": "source", "role": None},
            {"id": "target", "role": None},
            {"id": "fixup", "role": None},
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
                    "reject_target": "fixup",
                },
            },
        ],
    }
    compiled = build_graph_from_json(graph)
    initial_state: dict[str, Any] = {
        "run_context": {"cancelled": False, "input": {}},
        "artifacts": [],
        "_hitl_decision": {"action": "rejected"},
    }
    config = {"configurable": {"thread_id": str(uuid.uuid4())}}
    result = await compiled.ainvoke(initial_state, config)
    node_ids = [a["node_id"] for a in result["artifacts"]]
    assert "source" in node_ids
    assert "fixup" in node_ids
    assert "target" not in node_ids


# ---------------------------------------------------------------------------
# Conditional edge with persisted naming
# ---------------------------------------------------------------------------


async def test_conditional_graph_accepts_mixed_naming():
    """Conditional edges compile with a mix of canonical and persisted naming."""
    graph: dict[str, Any] = {
        "nodes": [
            {"id": "router", "role": None},
            {"id": "target-a", "role": None},
            {"id": "target-b", "role": None},
        ],
        "edges": [
            {
                "source_node_id": "router",
                "target_node_id": "target-a",
                "edge_type": "conditional",
                "condition_expression": "artifacts[0].type == 'a'",
            },
            {
                "source": "router",
                "target": "target-b",
                "type": "conditional",
                "condition_expression": "artifacts[0].type == 'b'",
            },
        ],
    }
    compiled = build_graph_from_json(graph)
    initial_state: dict[str, Any] = {
        "run_context": {"cancelled": False, "input": {}},
        "artifacts": [{"node_id": "prev", "type": "a"}],
    }
    config = {"configurable": {"thread_id": str(uuid.uuid4())}}
    result = await compiled.ainvoke(initial_state, config)
    node_ids = [a["node_id"] for a in result["artifacts"]]
    assert "router" in node_ids
    assert "target-a" in node_ids
    assert "target-b" not in node_ids
