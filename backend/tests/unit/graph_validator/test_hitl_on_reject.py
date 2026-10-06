"""Unit tests for the FAR-1487 HITL ``on_reject`` validation.

Covers ``GraphValidator._check_hitl_on_reject`` (``HITL_ON_REJECT_INVALID``)
via both the save-time entry point ``validate_definition`` and direct
static-method calls. Both gate shapes are exercised: EDGE-level
``hitl_review_config`` and FAR-402 node-level ``hitl_config`` (which bypasses
Pydantic, so this check is its only save-time gate).
"""

from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from modulo.core.graph_validator import GraphValidator, ValidationResult

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


def _gated_edge_graph(on_reject: Any = "__absent__") -> dict[str, Any]:
    config: dict = {"label": "Review gate", "description": _VALID_DESCRIPTION}
    if on_reject != "__absent__":
        config["on_reject"] = on_reject
    return {
        "nodes": [{"id": _UUID_A}, {"id": _UUID_B}],
        "edges": [{"source": _UUID_A, "target": _UUID_B, "type": "normal", "hitl_review_config": config}],
    }


def _hitl_node_graph(on_reject: Any = "__absent__") -> dict[str, Any]:
    config: dict = {"description": _VALID_DESCRIPTION}
    if on_reject != "__absent__":
        config["on_reject"] = on_reject
    return {
        "nodes": [
            {"id": _UUID_A, "node_type": "hitl", "hitl_config": config},
            {"id": _UUID_B},
        ],
        "edges": [{"source": _UUID_A, "target": _UUID_B, "type": "normal"}],
    }


@pytest.mark.parametrize("graph_builder", [_gated_edge_graph, _hitl_node_graph])
@pytest.mark.parametrize("on_reject", ["terminate", "proceed", "__absent__"])
async def test_valid_on_reject_accepted(graph_builder, on_reject):
    result = await GraphValidator().validate_definition(graph_builder(on_reject), _session_returning())
    assert not any(i.code == "HITL_ON_REJECT_INVALID" for i in result.issues)


@pytest.mark.parametrize("graph_builder", [_gated_edge_graph, _hitl_node_graph])
async def test_invalid_on_reject_rejected(graph_builder):
    result = await GraphValidator().validate_definition(graph_builder("typo"), _session_returning())
    issues = [i for i in result.issues if i.code == "HITL_ON_REJECT_INVALID"]
    assert len(issues) == 1


async def test_node_level_invalid_on_reject_names_the_node():
    result = await GraphValidator().validate_definition(_hitl_node_graph("typo"), _session_returning())
    issues = [i for i in result.issues if i.code == "HITL_ON_REJECT_INVALID"]
    assert len(issues) == 1
    assert f"node '{_UUID_A}'" in issues[0].message


async def test_edge_level_invalid_on_reject_names_the_edge():
    result = await GraphValidator().validate_definition(_gated_edge_graph("typo"), _session_returning())
    issues = [i for i in result.issues if i.code == "HITL_ON_REJECT_INVALID"]
    assert len(issues) == 1
    assert f"edge '{_UUID_A}->{_UUID_B}'" in issues[0].message


def _codes(result: ValidationResult) -> set[str]:
    return {i.code for i in result.issues}


class TestCheckHitlOnReject:
    """Direct unit tests for ``GraphValidator._check_hitl_on_reject``."""

    def test_none_config_skipped(self):
        result = ValidationResult()
        graph: dict[str, Any] = {"nodes": [{"id": _UUID_A, "node_type": "hitl"}], "edges": []}
        GraphValidator._check_hitl_on_reject(graph, result)
        assert not result.issues

    def test_non_dict_config_skipped(self):
        result = ValidationResult()
        graph: dict[str, Any] = {
            "nodes": [{"id": _UUID_A, "node_type": "hitl", "hitl_config": ["not", "a", "dict"]}],
            "edges": [],
        }
        GraphValidator._check_hitl_on_reject(graph, result)
        assert not result.issues

    def test_valid_terminate(self):
        result = ValidationResult()
        graph = {"nodes": [], "edges": [{"hitl_review_config": {"on_reject": "terminate"}}]}
        GraphValidator._check_hitl_on_reject(graph, result)
        assert not result.issues

    def test_valid_proceed(self):
        result = ValidationResult()
        graph = {"nodes": [], "edges": [{"hitl_review_config": {"on_reject": "proceed"}}]}
        GraphValidator._check_hitl_on_reject(graph, result)
        assert not result.issues

    def test_absent_on_reject_skipped(self):
        result = ValidationResult()
        graph = {"nodes": [], "edges": [{"hitl_review_config": {"label": "g"}}]}
        GraphValidator._check_hitl_on_reject(graph, result)
        assert not result.issues

    def test_invalid_on_reject_rejected(self):
        result = ValidationResult()
        graph = {"nodes": [], "edges": [{"hitl_review_config": {"on_reject": "abort"}}]}
        GraphValidator._check_hitl_on_reject(graph, result)
        assert _codes(result) == {"HITL_ON_REJECT_INVALID"}
