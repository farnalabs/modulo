"""Unit tests for the FAR-613 HITL gate-description requirement.

Covers ``_check_hitl_review_descriptions`` (HITL_REVIEW_DESCRIPTION_REQUIRED) via
the save-time entry point ``validate_definition``. Both gate shapes are
exercised: EDGE-level ``hitl_review_config`` and FAR-402 node-level
``hitl_config``. Run-start validation (``validate_for_run``) must NOT enforce
the requirement — legacy pipelines whose gates predate the rule keep running;
their briefing UI renders the muted no-description fallback instead.
"""

from typing import Any
from unittest.mock import AsyncMock

import pytest

from modulo.core.graph_validator import (
    HITL_DESCRIPTION_MIN_LENGTH,
    GraphValidator,
    check_hitl_review_descriptions,
)
from tests.unit.graph_validator._helpers import (
    UUID_A as _UUID_A,
)
from tests.unit.graph_validator._helpers import (
    UUID_B as _UUID_B,
)
from tests.unit.graph_validator._helpers import (
    VALID_DESCRIPTION as _VALID_DESCRIPTION,
)
from tests.unit.graph_validator._helpers import (
    gated_edge_graph as _gated_edge_shape,
)
from tests.unit.graph_validator._helpers import (
    hitl_node_graph as _hitl_node_shape,
)
from tests.unit.graph_validator._helpers import (
    session_returning as _session_returning_rows,
)


def _session_returning() -> AsyncMock:
    """Mock session whose execute() returns no rows (no DB-backed checks fire)."""
    return _session_returning_rows([])


def _gated_edge_graph(description: str | None = None) -> dict[str, Any]:
    config: dict = {"label": "Review gate"}
    if description is not None:
        config["description"] = description
    return _gated_edge_shape(config)


def _hitl_node_graph(description: str | None = None) -> dict:
    config: dict = {}
    if description is not None:
        config["description"] = description
    return _hitl_node_shape(config)


@pytest.mark.parametrize("graph_builder", [_gated_edge_graph, _hitl_node_graph])
async def test_gate_without_description_rejected(graph_builder):
    result = await GraphValidator().validate_definition(graph_builder(), _session_returning())
    assert not result.is_valid
    issues = [i for i in result.issues if i.code == "HITL_REVIEW_DESCRIPTION_REQUIRED"]
    assert len(issues) == 1
    assert f"min {HITL_DESCRIPTION_MIN_LENGTH}" in issues[0].message
    assert "explaining why this gate exists" in issues[0].message


async def test_edge_gate_without_description_names_the_edge_topology():
    result = await GraphValidator().validate_definition(_gated_edge_graph(), _session_returning())
    issue = next(i for i in result.issues if i.code == "HITL_REVIEW_DESCRIPTION_REQUIRED")
    assert f"edge '{_UUID_A}->{_UUID_B}'" in issue.message


async def test_node_gate_without_description_names_the_node():
    result = await GraphValidator().validate_definition(_hitl_node_graph(), _session_returning())
    issue = next(i for i in result.issues if i.code == "HITL_REVIEW_DESCRIPTION_REQUIRED")
    assert f"node '{_UUID_A}'" in issue.message


@pytest.mark.parametrize("graph_builder", [_gated_edge_graph, _hitl_node_graph])
async def test_gate_with_short_description_rejected(graph_builder):
    result = await GraphValidator().validate_definition(graph_builder("too short"), _session_returning())
    assert any(i.code == "HITL_REVIEW_DESCRIPTION_REQUIRED" for i in result.issues)


async def test_gate_with_whitespace_only_description_rejected():
    result = await GraphValidator().validate_definition(
        _gated_edge_graph("                     "), _session_returning()
    )
    assert any(i.code == "HITL_REVIEW_DESCRIPTION_REQUIRED" for i in result.issues)


async def test_gate_with_non_string_description_rejected():
    graph = _gated_edge_graph()
    graph["edges"][0]["hitl_review_config"]["description"] = 42
    result = await GraphValidator().validate_definition(graph, _session_returning())
    assert any(i.code == "HITL_REVIEW_DESCRIPTION_REQUIRED" for i in result.issues)


async def test_description_of_exactly_min_length_passes():
    result = await GraphValidator().validate_definition(
        _gated_edge_graph("x" * HITL_DESCRIPTION_MIN_LENGTH), _session_returning()
    )
    assert not any(i.code == "HITL_REVIEW_DESCRIPTION_REQUIRED" for i in result.issues)


@pytest.mark.parametrize("graph_builder", [_gated_edge_graph, _hitl_node_graph])
async def test_gate_with_valid_description_accepted(graph_builder):
    result = await GraphValidator().validate_definition(graph_builder(_VALID_DESCRIPTION), _session_returning())
    assert not any(i.code == "HITL_REVIEW_DESCRIPTION_REQUIRED" for i in result.issues)


async def test_node_gate_config_injected_on_its_outgoing_edge_not_double_reported():
    """A node-level gate whose config appears on BOTH the node and its
    outgoing edge (composite-expanded / compiled shape) is reported ONCE —
    under the node pass, never duplicated as an edge error."""
    graph = _hitl_node_graph()
    graph["edges"][0]["hitl_review_config"] = dict(graph["nodes"][0]["hitl_config"])
    result = await GraphValidator().validate_definition(graph, _session_returning())
    issues = [i for i in result.issues if i.code == "HITL_REVIEW_DESCRIPTION_REQUIRED"]
    assert len(issues) == 1
    assert f"node '{_UUID_A}'" in issues[0].message


async def test_valid_edge_gate_and_valid_node_gate_graph_passes():
    node_id = "cccccccc-cccc-cccc-cccc-cccccccccccc"
    graph = _gated_edge_graph(_VALID_DESCRIPTION)
    graph["nodes"].append({"id": node_id, "node_type": "hitl", "hitl_config": {"description": _VALID_DESCRIPTION}})
    graph["edges"].append({"source": node_id, "target": _UUID_B, "type": "normal"})
    result = await GraphValidator().validate_definition(graph, _session_returning())
    assert not any(i.code == "HITL_REVIEW_DESCRIPTION_REQUIRED" for i in result.issues)


async def test_gateless_edge_not_flagged():
    graph = {
        "nodes": [{"id": _UUID_A}, {"id": _UUID_B}],
        "edges": [{"source": _UUID_A, "target": _UUID_B, "type": "normal"}],
    }
    result = await GraphValidator().validate_definition(graph, _session_returning())
    assert not any(i.code == "HITL_REVIEW_DESCRIPTION_REQUIRED" for i in result.issues)


async def test_inert_hitl_config_on_non_hitl_node_not_flagged():
    """``hitl_config`` on a non-hitl node is inert at runtime (the compiler
    ignores it) — the description check must not fire for it."""
    graph = {
        "nodes": [{"id": _UUID_A, "node_type": "agent", "hitl_config": {}}],
        "edges": [],
    }
    result = await GraphValidator().validate_definition(graph, _session_returning())
    assert not any(i.code == "HITL_REVIEW_DESCRIPTION_REQUIRED" for i in result.issues)


# ---------------------------------------------------------------------------
# Public seam — check_hitl_review_descriptions (sessionless write surfaces)
# ---------------------------------------------------------------------------


def test_seam_passes_a_compliant_graph():
    """The sessionless seam returns no issues for a gate with a full description."""
    issues = check_hitl_review_descriptions(_gated_edge_graph(_VALID_DESCRIPTION))
    assert not [i for i in issues if i.code == "HITL_REVIEW_DESCRIPTION_REQUIRED"]


def test_seam_flags_edge_gate_without_description():
    """The seam rejects (returns HITL_REVIEW_DESCRIPTION_REQUIRED for) a gated
    edge whose config omits the description — no session required, which is
    the whole point: the MCP update_pipeline_graph write surface cannot run
    the full validate_definition."""
    issues = check_hitl_review_descriptions(_gated_edge_graph(None))
    issues = [i for i in issues if i.code == "HITL_REVIEW_DESCRIPTION_REQUIRED"]
    assert len(issues) == 1
    assert f"edge '{_UUID_A}->{_UUID_B}'" in issues[0].message


def test_seam_flags_node_gate_without_description():
    """The seam also covers the FAR-402 node-level ``hitl_config`` shape."""
    issues = check_hitl_review_descriptions(_hitl_node_graph(None))
    issues = [i for i in issues if i.code == "HITL_REVIEW_DESCRIPTION_REQUIRED"]
    assert len(issues) == 1
    assert f"node '{_UUID_A}'" in issues[0].message


def test_seam_short_circuits_on_gateless_graph():
    """A graph with no gates at all returns no description issues."""
    graph = {
        "nodes": [{"id": _UUID_A}, {"id": _UUID_B}],
        "edges": [{"source": _UUID_A, "target": _UUID_B, "type": "normal"}],
    }
    issues = check_hitl_review_descriptions(graph)
    assert not [i for i in issues if i.code == "HITL_REVIEW_DESCRIPTION_REQUIRED"]
