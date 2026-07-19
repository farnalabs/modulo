"""Unit tests for LLM routing mode in pipeline graphs."""

import uuid
from typing import Any

import pytest

from modulo.core.graph_validator._types import ValidationResult
from modulo.core.pipeline_engine.graph_cache import (
    _make_llm_router,
    build_graph_from_json,
)

# ---------------------------------------------------------------------------
# _make_llm_router — pure function tests
# ---------------------------------------------------------------------------


def test_llm_router_routes_by_label():
    """Router returns the target whose routing_label matches _llm_next_node."""
    routing_edges = [
        {"routing_label": "approve", "target": "approve-node"},
        {"routing_label": "revise", "target": "revise-node"},
    ]
    router = _make_llm_router(routing_edges, [], None)
    assert router({"_llm_next_node": "approve"}) == "approve-node"
    assert router({"_llm_next_node": "revise"}) == "revise-node"


def test_llm_router_falls_back_to_default_target():
    """Router uses default_target when _llm_next_node doesn't match any label."""
    routing_edges = [
        {"routing_label": "approve", "target": "approve-node"},
    ]
    router = _make_llm_router(routing_edges, [], "default-node")
    assert router({"_llm_next_node": "unknown"}) == "default-node"


def test_llm_router_falls_back_to_normal_target():
    """Router uses first normal target when no label match and no default_target."""
    routing_edges = [
        {"routing_label": "approve", "target": "approve-node"},
    ]
    router = _make_llm_router(routing_edges, ["normal-node"], None)
    assert router({"_llm_next_node": "unknown"}) == "normal-node"


def test_llm_router_falls_back_to_last_routing_target():
    """Router uses last routing target when nothing else works."""
    routing_edges = [
        {"routing_label": "a", "target": "node-a"},
        {"routing_label": "b", "target": "node-b"},
    ]
    router = _make_llm_router(routing_edges, [], None)
    assert router({"_llm_next_node": "unknown"}) == "node-b"


def test_llm_router_raises_on_no_edges():
    """Router raises ValueError when no edges exist to route through."""
    router = _make_llm_router([], [], None)
    with pytest.raises(ValueError, match="no edges"):
        router({"_llm_next_node": "x"})


def test_llm_router_no_llm_next_node_in_state():
    """Router uses default_target when _llm_next_node is not in state."""
    routing_edges = [
        {"routing_label": "a", "target": "node-a"},
    ]
    router = _make_llm_router(routing_edges, [], "default-node")
    assert router({}) == "default-node"


def test_llm_router_uses_persisted_names():
    """Router works with persisted naming (target_node_id)."""
    routing_edges = [
        {"routing_label": "yes", "target_node_id": "yes-node"},
    ]
    router = _make_llm_router(routing_edges, [], None)
    assert router({"_llm_next_node": "yes"}) == "yes-node"


# ---------------------------------------------------------------------------
# build_graph_from_json — LLM routing graph compilation
# ---------------------------------------------------------------------------


def test_llm_routing_graph_compiles():
    """A graph with an LLM routing node compiles successfully."""
    graph: dict[str, Any] = {
        "nodes": [
            {
                "id": "decider",
                "role": None,
                "routing_mode": "llm",
                "routing_prompt": "Choose a path",
                "default_target": "fallback",
            },
            {"id": "path-a", "role": None},
            {"id": "path-b", "role": None},
            {"id": "fallback", "role": None},
        ],
        "edges": [
            {"source": "decider", "target": "path-a", "type": "conditional", "routing_label": "route_a"},
            {"source": "decider", "target": "path-b", "type": "conditional", "routing_label": "route_b"},
            {"source": "decider", "target": "fallback", "type": "conditional", "routing_label": "fallback"},
        ],
    }
    compiled = build_graph_from_json(graph)
    assert compiled is not None


async def test_llm_routing_executes_routed_path():
    """The LLM-routed path executes based on _llm_next_node in state."""
    graph: dict[str, Any] = {
        "nodes": [
            {
                "id": "decider",
                "role": None,
                "routing_mode": "llm",
                "routing_prompt": "Choose",
                "default_target": "fallback",
            },
            {"id": "path-a", "role": None},
            {"id": "fallback", "role": None},
        ],
        "edges": [
            {"source": "decider", "target": "path-a", "type": "conditional", "routing_label": "route_a"},
            {"source": "decider", "target": "fallback", "type": "conditional", "routing_label": "fallback"},
        ],
    }
    compiled = build_graph_from_json(graph)
    initial_state: dict[str, Any] = {
        "run_context": {"cancelled": False, "input": {}},
        "artifacts": [],
        "_llm_next_node": "route_a",
    }
    config = {"configurable": {"thread_id": str(uuid.uuid4())}}
    result = await compiled.ainvoke(initial_state, config)
    node_ids = [a["node_id"] for a in result["artifacts"]]
    assert "decider" in node_ids
    assert "path-a" in node_ids
    assert "fallback" not in node_ids


async def test_llm_routing_falls_back_to_default():
    """When _llm_next_node doesn't match, the default_target is used."""
    graph: dict[str, Any] = {
        "nodes": [
            {
                "id": "decider",
                "role": None,
                "routing_mode": "llm",
                "routing_prompt": "Choose",
                "default_target": "fallback",
            },
            {"id": "path-a", "role": None},
            {"id": "fallback", "role": None},
        ],
        "edges": [
            {"source": "decider", "target": "path-a", "type": "conditional", "routing_label": "route_a"},
            {"source": "decider", "target": "fallback", "type": "conditional", "routing_label": "fallback"},
        ],
    }
    compiled = build_graph_from_json(graph)
    initial_state: dict[str, Any] = {
        "run_context": {"cancelled": False, "input": {}},
        "artifacts": [],
        "_llm_next_node": "unknown_route",
    }
    config = {"configurable": {"thread_id": str(uuid.uuid4())}}
    result = await compiled.ainvoke(initial_state, config)
    node_ids = [a["node_id"] for a in result["artifacts"]]
    assert "decider" in node_ids
    assert "fallback" in node_ids
    assert "path-a" not in node_ids


async def test_llm_routing_works_with_normal_edges_as_fallback():
    """Normal edges from the same source serve as fallback when no label matches."""
    graph: dict[str, Any] = {
        "nodes": [
            {"id": "decider", "role": None, "routing_mode": "llm", "routing_prompt": "Choose"},
            {"id": "special", "role": None},
            {"id": "default-path", "role": None},
        ],
        "edges": [
            {"source": "decider", "target": "special", "type": "conditional", "routing_label": "go_special"},
            {"source": "decider", "target": "default-path", "type": "normal"},
        ],
    }
    compiled = build_graph_from_json(graph)
    initial_state: dict[str, Any] = {
        "run_context": {"cancelled": False, "input": {}},
        "artifacts": [],
        "_llm_next_node": "unknown",
    }
    config = {"configurable": {"thread_id": str(uuid.uuid4())}}
    result = await compiled.ainvoke(initial_state, config)
    node_ids = [a["node_id"] for a in result["artifacts"]]
    assert "default-path" in node_ids
    assert "special" not in node_ids


# ---------------------------------------------------------------------------
# Graph validator — LLM routing
# ---------------------------------------------------------------------------


def _make_validator():
    from modulo.core.graph_validator import GraphValidator

    return GraphValidator()


def test_validator_accepts_valid_llm_routing():
    """Validator accepts a valid LLM routing configuration."""
    validator = _make_validator()
    result = ValidationResult()
    nodes = [
        {"id": "decider", "routing_mode": "llm", "routing_prompt": "Choose path", "default_target": "fallback"},
        {"id": "path-a"},
        {"id": "fallback"},
    ]
    edges = [
        {"source": "decider", "target": "path-a", "routing_label": "route_a"},
        {"source": "decider", "target": "fallback", "routing_label": "fallback"},
    ]
    node_ids = {"decider", "path-a", "fallback"}
    validator._check_llm_routing(nodes, edges, node_ids, result)
    assert result.is_valid


def test_validator_rejects_missing_routing_prompt():
    """Validator rejects an LLM routing node without a routing_prompt."""
    validator = _make_validator()
    result = ValidationResult()
    nodes = [
        {"id": "decider", "routing_mode": "llm", "default_target": "fallback"},
        {"id": "fallback"},
    ]
    edges = [
        {"source": "decider", "target": "fallback", "routing_label": "fallback"},
    ]
    node_ids = {"decider", "fallback"}
    validator._check_llm_routing(nodes, edges, node_ids, result)
    assert not result.is_valid
    codes = [i.code for i in result.issues]
    assert "LLM_ROUTING_MISSING_PROMPT" in codes


def test_validator_rejects_missing_default_target():
    """Validator rejects an LLM routing node without a default_target."""
    validator = _make_validator()
    result = ValidationResult()
    nodes = [
        {"id": "decider", "routing_mode": "llm", "routing_prompt": "Choose"},
        {"id": "fallback"},
    ]
    edges = [
        {"source": "decider", "target": "fallback", "routing_label": "fallback"},
    ]
    node_ids = {"decider", "fallback"}
    validator._check_llm_routing(nodes, edges, node_ids, result)
    assert not result.is_valid
    codes = [i.code for i in result.issues]
    assert "LLM_ROUTING_MISSING_DEFAULT" in codes


def test_validator_rejects_default_target_not_a_node():
    """Validator rejects a default_target that doesn't reference a valid node."""
    validator = _make_validator()
    result = ValidationResult()
    nodes = [
        {"id": "decider", "routing_mode": "llm", "routing_prompt": "Choose", "default_target": "nonexistent"},
    ]
    edges: list[dict[str, Any]] = []
    node_ids = {"decider"}
    validator._check_llm_routing(nodes, edges, node_ids, result)
    assert not result.is_valid
    codes = [i.code for i in result.issues]
    assert "LLM_ROUTING_DEFAULT_NOT_FOUND" in codes


def test_validator_rejects_missing_routing_label():
    """Validator rejects an edge from LLM routing node without a routing_label."""
    validator = _make_validator()
    result = ValidationResult()
    nodes = [
        {"id": "decider", "routing_mode": "llm", "routing_prompt": "Choose", "default_target": "fallback"},
        {"id": "fallback"},
    ]
    edges = [
        {"source": "decider", "target": "fallback"},
    ]
    node_ids = {"decider", "fallback"}
    validator._check_llm_routing(nodes, edges, node_ids, result)
    assert not result.is_valid
    codes = [i.code for i in result.issues]
    assert "LLM_ROUTING_MISSING_LABEL" in codes


def test_validator_rejects_duplicate_routing_labels():
    """Validator rejects duplicate routing_labels on edges from the same node."""
    validator = _make_validator()
    result = ValidationResult()
    nodes = [
        {"id": "decider", "routing_mode": "llm", "routing_prompt": "Choose", "default_target": "fallback"},
        {"id": "path-a"},
        {"id": "path-b"},
        {"id": "fallback"},
    ]
    edges = [
        {"source": "decider", "target": "path-a", "routing_label": "route_dup"},
        {"source": "decider", "target": "path-b", "routing_label": "route_dup"},
        {"source": "decider", "target": "fallback", "routing_label": "fallback"},
    ]
    node_ids = {"decider", "path-a", "path-b", "fallback"}
    validator._check_llm_routing(nodes, edges, node_ids, result)
    assert not result.is_valid
    codes = [i.code for i in result.issues]
    assert "LLM_ROUTING_DUPLICATE_LABEL" in codes
