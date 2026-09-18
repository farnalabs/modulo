"""Unit tests for the FAR-859 HITL subject_path validation.

Covers ``_check_hitl_subject_path`` (HITL_SUBJECT_PATH_*) and
``_check_hitl_gate_subject_paths`` via the save-time entry point
``validate_definition``. Both gate shapes are exercised: EDGE-level
``hitl_gate_config`` and FAR-402 node-level ``hitl_config``.
"""

from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from modulo.core.graph_validator import (
    HITL_SUBJECT_PATH_MAX_LENGTH,
    GraphValidator,
)

_UUID_A = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
_UUID_B = "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb"
_VALID_DESCRIPTION = "Approve the deploy only after a human has reviewed the plan."
_VALID_SUBJECT_PATH = "result.summary"


def _session_returning() -> AsyncMock:
    """Mock session whose execute() returns no rows (no DB-backed checks fire)."""
    session = AsyncMock()
    scalars_result = MagicMock()
    scalars_result.all.return_value = []
    execute_result = MagicMock()
    execute_result.scalars.return_value = scalars_result
    session.execute = AsyncMock(return_value=execute_result)
    return session


def _gated_edge_graph(
    subject_path: str | None = None,
    description: str = _VALID_DESCRIPTION,
) -> dict[str, Any]:
    config: dict = {"label": "Review gate", "description": description}
    if subject_path is not None:
        config["subject_path"] = subject_path
    return {
        "nodes": [{"id": _UUID_A}, {"id": _UUID_B}],
        "edges": [{"source": _UUID_A, "target": _UUID_B, "type": "normal", "hitl_gate_config": config}],
    }


def _hitl_node_graph(
    subject_path: str | None = None,
    description: str = _VALID_DESCRIPTION,
) -> dict:
    config: dict = {"description": description}
    if subject_path is not None:
        config["subject_path"] = subject_path
    return {
        "nodes": [
            {"id": _UUID_A, "node_type": "hitl", "hitl_config": config},
            {"id": _UUID_B},
        ],
        "edges": [{"source": _UUID_A, "target": _UUID_B, "type": "normal"}],
    }


# ---------------------------------------------------------------------------
# Valid cases — must not produce HITL_SUBJECT_PATH_* issues
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("graph_builder", [_gated_edge_graph, _hitl_node_graph])
async def test_valid_subject_path_accepted(graph_builder):
    result = await GraphValidator().validate_definition(
        graph_builder(subject_path=_VALID_SUBJECT_PATH), _session_returning()
    )
    assert not any(i.code.startswith("HITL_SUBJECT_PATH") for i in result.issues)


async def test_absent_subject_path_passes():
    result = await GraphValidator().validate_definition(_gated_edge_graph(), _session_returning())
    assert not any(i.code.startswith("HITL_SUBJECT_PATH") for i in result.issues)


async def test_none_subject_path_passes():
    """Explicitly-set None is treated as absent."""
    result = await GraphValidator().validate_definition(_gated_edge_graph(subject_path=None), _session_returning())
    assert not any(i.code.startswith("HITL_SUBJECT_PATH") for i in result.issues)


async def test_empty_string_subject_path_passes():
    """Empty or whitespace-only subject_path is treated as absent."""
    result = await GraphValidator().validate_definition(_gated_edge_graph(subject_path="   "), _session_returning())
    assert not any(i.code.startswith("HITL_SUBJECT_PATH") for i in result.issues)


async def test_max_length_subject_path_accepted():
    result = await GraphValidator().validate_definition(
        _gated_edge_graph(subject_path="x" * HITL_SUBJECT_PATH_MAX_LENGTH),
        _session_returning(),
    )
    assert not any(i.code.startswith("HITL_SUBJECT_PATH") for i in result.issues)


# ---------------------------------------------------------------------------
# Invalid JMESPath — must produce HITL_SUBJECT_PATH_INVALID_JMESPATH
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("graph_builder", [_gated_edge_graph, _hitl_node_graph])
async def test_invalid_jmespath_rejected(graph_builder):
    result = await GraphValidator().validate_definition(
        graph_builder(subject_path="this is {not valid jmes[[["), _session_returning()
    )
    issues = [i for i in result.issues if i.code == "HITL_SUBJECT_PATH_INVALID_JMESPATH"]
    assert len(issues) == 1
    assert "invalid JMESPath" in issues[0].message


# ---------------------------------------------------------------------------
# Over-length — must produce HITL_SUBJECT_PATH_TOO_LONG
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("graph_builder", [_gated_edge_graph, _hitl_node_graph])
async def test_over_length_subject_path_rejected(graph_builder):
    result = await GraphValidator().validate_definition(
        graph_builder(subject_path="a" * (HITL_SUBJECT_PATH_MAX_LENGTH + 1)),
        _session_returning(),
    )
    issues = [i for i in result.issues if i.code == "HITL_SUBJECT_PATH_TOO_LONG"]
    assert len(issues) == 1
    assert "exceeds" in issues[0].message


# ---------------------------------------------------------------------------
# Node-level vs edge-level routing
# ---------------------------------------------------------------------------


async def test_node_level_invalid_subject_path_names_the_node():
    result = await GraphValidator().validate_definition(_hitl_node_graph(subject_path="bad[["), _session_returning())
    issues = [i for i in result.issues if i.code == "HITL_SUBJECT_PATH_INVALID_JMESPATH"]
    assert len(issues) == 1
    assert f"node '{_UUID_A}'" in issues[0].message


async def test_edge_level_invalid_subject_path_names_the_edge():
    result = await GraphValidator().validate_definition(_gated_edge_graph(subject_path="bad[["), _session_returning())
    issues = [i for i in result.issues if i.code == "HITL_SUBJECT_PATH_INVALID_JMESPATH"]
    assert len(issues) == 1
    assert f"edge '{_UUID_A}->{_UUID_B}'" in issues[0].message


async def test_node_gate_config_injected_on_edge_not_double_reported():
    """A node-level gate whose config appears on BOTH the node and its
    outgoing edge (composite-expanded / compiled shape) is reported ONCE —
    under the node pass, never duplicated as an edge error."""
    graph = _hitl_node_graph(subject_path="bad[[")
    graph["edges"][0]["hitl_gate_config"] = dict(graph["nodes"][0]["hitl_config"])
    result = await GraphValidator().validate_definition(graph, _session_returning())
    issues = [i for i in result.issues if i.code == "HITL_SUBJECT_PATH_INVALID_JMESPATH"]
    assert len(issues) == 1
    assert f"node '{_UUID_A}'" in issues[0].message


# ---------------------------------------------------------------------------
# Non-dict / non-hitl nodes not flagged
# ---------------------------------------------------------------------------


async def test_non_hitl_node_with_hitl_config_not_checked():
    """``hitl_config`` on a non-hitl node is inert — must not be checked."""
    graph = {
        "nodes": [{"id": _UUID_A, "node_type": "agent", "hitl_config": {"subject_path": "bad[["}}],
        "edges": [],
    }
    result = await GraphValidator().validate_definition(graph, _session_returning())
    assert not any(i.code.startswith("HITL_SUBJECT_PATH") for i in result.issues)


async def test_gateless_edge_not_flagged():
    graph = {
        "nodes": [{"id": _UUID_A}, {"id": _UUID_B}],
        "edges": [{"source": _UUID_A, "target": _UUID_B, "type": "normal"}],
    }
    result = await GraphValidator().validate_definition(graph, _session_returning())
    assert not any(i.code.startswith("HITL_SUBJECT_PATH") for i in result.issues)


# ---------------------------------------------------------------------------
# Unit-level static method tests
# ---------------------------------------------------------------------------


def _codes(result) -> set[str]:
    return {i.code for i in result.issues}


class TestCheckHitlSubjectPath:
    """Direct unit tests for ``GraphValidator._check_hitl_subject_path``."""

    def test_none_config_skipped(self):
        from modulo.core.graph_validator import ValidationResult

        result = ValidationResult()
        GraphValidator._check_hitl_subject_path(None, "node 'x'", result)
        assert not result.issues

    def test_no_subject_path_skipped(self):
        from modulo.core.graph_validator import ValidationResult

        result = ValidationResult()
        GraphValidator._check_hitl_subject_path({"label": "g"}, "node 'x'", result)
        assert not result.issues

    def test_valid_jmespath(self):
        from modulo.core.graph_validator import ValidationResult

        result = ValidationResult()
        GraphValidator._check_hitl_subject_path({"subject_path": "result.summary"}, "node 'x'", result)
        assert not result.issues

    def test_invalid_jmespath(self):
        from modulo.core.graph_validator import ValidationResult

        result = ValidationResult()
        GraphValidator._check_hitl_subject_path({"subject_path": "bad[["}, "node 'x'", result)
        assert "HITL_SUBJECT_PATH_INVALID_JMESPATH" in _codes(result)

    def test_over_length(self):
        from modulo.core.graph_validator import ValidationResult

        result = ValidationResult()
        GraphValidator._check_hitl_subject_path(
            {"subject_path": "a" * (HITL_SUBJECT_PATH_MAX_LENGTH + 1)},
            "node 'x'",
            result,
        )
        assert "HITL_SUBJECT_PATH_TOO_LONG" in _codes(result)

    def test_empty_string_skipped(self):
        from modulo.core.graph_validator import ValidationResult

        result = ValidationResult()
        GraphValidator._check_hitl_subject_path({"subject_path": "  "}, "node 'x'", result)
        assert not result.issues

    def test_non_string_skipped(self):
        from modulo.core.graph_validator import ValidationResult

        result = ValidationResult()
        GraphValidator._check_hitl_subject_path({"subject_path": 42}, "node 'x'", result)
        assert not result.issues
