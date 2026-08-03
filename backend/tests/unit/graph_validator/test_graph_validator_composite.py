"""Unit tests for composite sub-graph structural validation.

Covers the ``COMPOSITE_SUBGRAPH_*`` checks added to
``GraphValidator._check_composite_nodes``: unique sub-node ids, valid sub-edge
references, supported sub-node types, and sandbox sub-node config.
"""

import uuid
from typing import Any
from unittest.mock import AsyncMock, MagicMock

from modulo.core.graph_validator import GraphValidator, ValidationResult


def _codes(result: ValidationResult) -> set[str]:
    return {i.code for i in result.issues}


def _template(sub_graph: dict[str, Any] | None = None) -> MagicMock:
    template = MagicMock()
    template.id = uuid.uuid4()
    template.parameter_ports_json = []
    template.sub_pipeline_graph_json = sub_graph
    return template


def _node(nid: str, composite_ref: uuid.UUID) -> dict[str, Any]:
    return {"id": nid, "node_type": "composite", "composite_ref": str(composite_ref)}


def _session_with_templates(rows: list[MagicMock]) -> AsyncMock:
    session = AsyncMock()
    scalars = MagicMock()
    scalars.all.return_value = rows
    exc = MagicMock()
    exc.scalars.return_value = scalars
    session.execute = AsyncMock(return_value=exc)
    return session


async def _run(graph_json: dict[str, Any], session: AsyncMock) -> ValidationResult:
    result = ValidationResult()
    await GraphValidator()._check_composite_nodes(graph_json, session, result)
    return result


async def _run_for_sub_graph(sub_graph: dict[str, Any]) -> ValidationResult:
    template = _template(sub_graph)
    graph = {"nodes": [_node("n1", template.id)], "edges": []}
    return await _run(graph, _session_with_templates([template]))


async def test_valid_sub_graph_no_errors() -> None:
    sub_graph = {
        "nodes": [
            {"id": "a", "node_type": "agent"},
            {"id": "b", "node_type": "manual"},
            {"id": "c", "node_type": "composite", "composite_ref": str(uuid.uuid4())},
            {
                "id": "d",
                "node_type": "sandbox_agent",
                "agent_command": "opencode run",
                "template_id": "opencode",
            },
        ],
        "edges": [{"source": "a", "target": "b"}, {"source": "b", "target": "d"}],
    }
    result = await _run_for_sub_graph(sub_graph)
    assert result.is_valid
    assert not any(code.startswith("COMPOSITE_SUBGRAPH") for code in _codes(result))


async def test_empty_sub_graph_is_error() -> None:
    result = await _run_for_sub_graph({"nodes": [], "edges": []})
    assert "COMPOSITE_SUBGRAPH_EMPTY" in _codes(result)
    assert not result.is_valid


async def test_duplicate_sub_node_id_is_error() -> None:
    sub_graph = {
        "nodes": [{"id": "a", "node_type": "agent"}, {"id": "a", "node_type": "agent"}],
        "edges": [],
    }
    result = await _run_for_sub_graph(sub_graph)
    assert "COMPOSITE_SUBGRAPH_DUPLICATE_NODE_ID" in _codes(result)
    assert not result.is_valid


async def test_invalid_sub_node_type_is_error() -> None:
    sub_graph = {
        "nodes": [{"id": "a", "node_type": "transporter"}],
        "edges": [],
    }
    result = await _run_for_sub_graph(sub_graph)
    assert "COMPOSITE_SUBGRAPH_INVALID_TYPE" in _codes(result)
    assert not result.is_valid


async def test_sandbox_sub_node_missing_command_is_error() -> None:
    sub_graph = {
        "nodes": [{"id": "a", "node_type": "sandbox_agent", "template_id": "opencode"}],
        "edges": [],
    }
    result = await _run_for_sub_graph(sub_graph)
    assert "COMPOSITE_SUBGRAPH_SANDBOX_MISSING_COMMAND" in _codes(result)
    assert "COMPOSITE_SUBGRAPH_SANDBOX_MISSING_TEMPLATE" not in _codes(result)
    assert not result.is_valid


async def test_sandbox_sub_node_missing_template_is_error() -> None:
    sub_graph = {
        "nodes": [{"id": "a", "node_type": "sandbox_agent", "agent_command": "opencode run"}],
        "edges": [],
    }
    result = await _run_for_sub_graph(sub_graph)
    assert "COMPOSITE_SUBGRAPH_SANDBOX_MISSING_TEMPLATE" in _codes(result)
    assert not result.is_valid


async def test_sub_edge_bad_source_and_target_are_errors() -> None:
    sub_graph = {
        "nodes": [{"id": "a", "node_type": "agent"}],
        "edges": [
            {"source": "ghost", "target": "a"},
            {"source": "a", "target": "phantom"},
        ],
    }
    result = await _run_for_sub_graph(sub_graph)
    assert "COMPOSITE_SUBGRAPH_EDGE_BAD_SOURCE" in _codes(result)
    assert "COMPOSITE_SUBGRAPH_EDGE_BAD_TARGET" in _codes(result)
    assert not result.is_valid


async def test_non_dict_sub_graph_skipped() -> None:
    template = _template(None)
    graph = {"nodes": [_node("n1", template.id)], "edges": []}
    result = await _run(graph, _session_with_templates([template]))
    assert result.is_valid
    assert not any(code.startswith("COMPOSITE_SUBGRAPH") for code in _codes(result))


async def test_sub_edge_with_gate_config_is_error() -> None:
    sub_graph = {
        "nodes": [{"id": "a", "node_type": "agent"}, {"id": "b", "node_type": "agent"}],
        "edges": [{"source": "a", "target": "b", "hitl_gate_config": {"mode": "manual"}}],
    }
    result = await _run_for_sub_graph(sub_graph)
    assert "COMPOSITE_SUBGRAPH_GATE_UNSUPPORTED" in _codes(result)
    assert not result.is_valid


async def test_sub_edge_without_gate_config_is_valid() -> None:
    sub_graph = {
        "nodes": [{"id": "a", "node_type": "agent"}, {"id": "b", "node_type": "agent"}],
        "edges": [{"source": "a", "target": "b"}],
    }
    result = await _run_for_sub_graph(sub_graph)
    assert "COMPOSITE_SUBGRAPH_GATE_UNSUPPORTED" not in _codes(result)
    assert result.is_valid
