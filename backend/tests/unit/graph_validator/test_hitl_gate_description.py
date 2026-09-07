"""Unit tests for the FAR-613 HITL gate-description requirement.

Covers ``_check_hitl_gate_descriptions`` (HITL_GATE_DESCRIPTION_REQUIRED) via
the save-time entry point ``validate_definition``. Both gate shapes are
exercised: EDGE-level ``hitl_gate_config`` and FAR-402 node-level
``hitl_config``. Run-start validation (``validate_for_run``) must NOT enforce
the requirement — legacy pipelines whose gates predate the rule keep running;
their briefing UI renders the muted no-description fallback instead.
"""

from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from modulo.core.graph_validator import HITL_DESCRIPTION_MIN_LENGTH, GraphValidator

_UUID_A = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
_UUID_B = "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb"
_VALID_DESCRIPTION = "Approve the deploy only after a human has reviewed the plan."


def _session_returning() -> AsyncMock:
    """Mock session whose execute() returns no rows (no DB-backed checks fire)."""
    session = AsyncMock()
    scalars_result = MagicMock()
    scalars_result.all.return_value = []
    execute_result = MagicMock()
    execute_result.scalars.return_value = scalars_result
    session.execute = AsyncMock(return_value=execute_result)
    return session


def _gated_edge_graph(description: str | None = None) -> dict[str, Any]:
    config: dict = {"label": "Review gate"}
    if description is not None:
        config["description"] = description
    return {
        "nodes": [{"id": _UUID_A}, {"id": _UUID_B}],
        "edges": [{"source": _UUID_A, "target": _UUID_B, "type": "normal", "hitl_gate_config": config}],
    }


def _hitl_node_graph(description: str | None = None) -> dict:
    config: dict = {}
    if description is not None:
        config["description"] = description
    return {
        "nodes": [
            {"id": _UUID_A, "node_type": "hitl", "hitl_config": config},
            {"id": _UUID_B},
        ],
        "edges": [{"source": _UUID_A, "target": _UUID_B, "type": "normal"}],
    }


@pytest.mark.parametrize("graph_builder", [_gated_edge_graph, _hitl_node_graph])
async def test_gate_without_description_rejected(graph_builder):
    result = await GraphValidator().validate_definition(graph_builder(), _session_returning())
    assert not result.is_valid
    issues = [i for i in result.issues if i.code == "HITL_GATE_DESCRIPTION_REQUIRED"]
    assert len(issues) == 1
    assert f"min {HITL_DESCRIPTION_MIN_LENGTH}" in issues[0].message
    assert "explaining why this gate exists" in issues[0].message


async def test_edge_gate_without_description_names_the_edge_topology():
    result = await GraphValidator().validate_definition(_gated_edge_graph(), _session_returning())
    issue = next(i for i in result.issues if i.code == "HITL_GATE_DESCRIPTION_REQUIRED")
    assert f"edge '{_UUID_A}->{_UUID_B}'" in issue.message


async def test_node_gate_without_description_names_the_node():
    result = await GraphValidator().validate_definition(_hitl_node_graph(), _session_returning())
    issue = next(i for i in result.issues if i.code == "HITL_GATE_DESCRIPTION_REQUIRED")
    assert f"node '{_UUID_A}'" in issue.message


@pytest.mark.parametrize("graph_builder", [_gated_edge_graph, _hitl_node_graph])
async def test_gate_with_short_description_rejected(graph_builder):
    result = await GraphValidator().validate_definition(graph_builder("too short"), _session_returning())
    assert any(i.code == "HITL_GATE_DESCRIPTION_REQUIRED" for i in result.issues)


async def test_gate_with_whitespace_only_description_rejected():
    result = await GraphValidator().validate_definition(
        _gated_edge_graph("                     "), _session_returning()
    )
    assert any(i.code == "HITL_GATE_DESCRIPTION_REQUIRED" for i in result.issues)


async def test_gate_with_non_string_description_rejected():
    graph = _gated_edge_graph()
    graph["edges"][0]["hitl_gate_config"]["description"] = 42
    result = await GraphValidator().validate_definition(graph, _session_returning())
    assert any(i.code == "HITL_GATE_DESCRIPTION_REQUIRED" for i in result.issues)


async def test_description_of_exactly_min_length_passes():
    result = await GraphValidator().validate_definition(
        _gated_edge_graph("x" * HITL_DESCRIPTION_MIN_LENGTH), _session_returning()
    )
    assert not any(i.code == "HITL_GATE_DESCRIPTION_REQUIRED" for i in result.issues)


@pytest.mark.parametrize("graph_builder", [_gated_edge_graph, _hitl_node_graph])
async def test_gate_with_valid_description_accepted(graph_builder):
    result = await GraphValidator().validate_definition(graph_builder(_VALID_DESCRIPTION), _session_returning())
    assert not any(i.code == "HITL_GATE_DESCRIPTION_REQUIRED" for i in result.issues)


async def test_node_gate_config_injected_on_its_outgoing_edge_not_double_reported():
    """A node-level gate whose config appears on BOTH the node and its
    outgoing edge (composite-expanded / compiled shape) is reported ONCE —
    under the node pass, never duplicated as an edge error."""
    graph = _hitl_node_graph()
    graph["edges"][0]["hitl_gate_config"] = dict(graph["nodes"][0]["hitl_config"])
    result = await GraphValidator().validate_definition(graph, _session_returning())
    issues = [i for i in result.issues if i.code == "HITL_GATE_DESCRIPTION_REQUIRED"]
    assert len(issues) == 1
    assert f"node '{_UUID_A}'" in issues[0].message


async def test_valid_edge_gate_and_valid_node_gate_graph_passes():
    node_id = "cccccccc-cccc-cccc-cccc-cccccccccccc"
    graph = _gated_edge_graph(_VALID_DESCRIPTION)
    graph["nodes"].append({"id": node_id, "node_type": "hitl", "hitl_config": {"description": _VALID_DESCRIPTION}})
    graph["edges"].append({"source": node_id, "target": _UUID_B, "type": "normal"})
    result = await GraphValidator().validate_definition(graph, _session_returning())
    assert not any(i.code == "HITL_GATE_DESCRIPTION_REQUIRED" for i in result.issues)


async def test_gateless_edge_not_flagged():
    graph = {
        "nodes": [{"id": _UUID_A}, {"id": _UUID_B}],
        "edges": [{"source": _UUID_A, "target": _UUID_B, "type": "normal"}],
    }
    result = await GraphValidator().validate_definition(graph, _session_returning())
    assert not any(i.code == "HITL_GATE_DESCRIPTION_REQUIRED" for i in result.issues)


async def test_inert_hitl_config_on_non_hitl_node_not_flagged():
    """``hitl_config`` on a non-hitl node is inert at runtime (the compiler
    ignores it) — the description check must not fire for it."""
    graph = {
        "nodes": [{"id": _UUID_A, "node_type": "agent", "hitl_config": {}}],
        "edges": [],
    }
    result = await GraphValidator().validate_definition(graph, _session_returning())
    assert not any(i.code == "HITL_GATE_DESCRIPTION_REQUIRED" for i in result.issues)
