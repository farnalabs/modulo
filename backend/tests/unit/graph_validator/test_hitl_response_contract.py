"""Unit tests for FAR-860 HITL response_contract validation.

Covers ``_check_hitl_response_contract`` (response_contract validation) via
the save-time entry point ``validate_definition``. Both gate shapes are
exercised: EDGE-level ``hitl_gate_config`` and node-level ``hitl_config``.
"""

from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from modulo.core.graph_validator import GraphValidator

_UUID_A = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
_UUID_B = "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb"
_VALID_DESCRIPTION = "Approve the deploy only after a human has reviewed the plan."


def _session_returning() -> AsyncMock:
    session = AsyncMock()
    scalars_result = MagicMock()
    scalars_result.all.return_value = []
    execute_result = MagicMock()
    execute_result.scalars.return_value = scalars_result
    session.execute = AsyncMock(return_value=execute_result)
    return session


def _gated_edge_graph(rc: dict[str, Any] | None = None) -> dict[str, Any]:
    config: dict[str, Any] = {"label": "Review gate", "description": _VALID_DESCRIPTION}
    if rc is not None:
        config["response_contract"] = rc
    return {
        "nodes": [{"id": _UUID_A}, {"id": _UUID_B}],
        "edges": [{"source": _UUID_A, "target": _UUID_B, "type": "normal", "hitl_gate_config": config}],
    }


def _hitl_node_graph(rc: dict[str, Any] | None = None) -> dict[str, Any]:
    config: dict[str, Any] = {"description": _VALID_DESCRIPTION}
    if rc is not None:
        config["response_contract"] = rc
    return {
        "nodes": [
            {"id": _UUID_A, "node_type": "hitl", "hitl_config": config},
            {"id": _UUID_B},
        ],
        "edges": [{"source": _UUID_A, "target": _UUID_B, "type": "normal"}],
    }


# --- kind: approval (valid) ---


@pytest.mark.parametrize("graph_builder", [_gated_edge_graph, _hitl_node_graph])
async def test_approval_contract_passes(graph_builder):
    result = await GraphValidator().validate_definition(graph_builder({"kind": "approval"}), _session_returning())
    issues = [i for i in result.issues if "RESPONSE_CONTRACT" in i.code]
    assert issues == []


# --- kind: choice (valid) ---


@pytest.mark.parametrize("graph_builder", [_gated_edge_graph, _hitl_node_graph])
async def test_choice_contract_with_valid_options_passes(graph_builder):
    rc = {
        "kind": "choice",
        "options": [
            {"id": "approve", "label": "Approve"},
            {"id": "reject", "label": "Reject with feedback"},
        ],
    }
    result = await GraphValidator().validate_definition(graph_builder(rc), _session_returning())
    issues = [i for i in result.issues if "RESPONSE_CONTRACT" in i.code]
    assert issues == []


# --- kind: choice without options ---


@pytest.mark.parametrize("graph_builder", [_gated_edge_graph, _hitl_node_graph])
async def test_choice_without_options_rejected(graph_builder):
    rc = {"kind": "choice"}
    result = await GraphValidator().validate_definition(graph_builder(rc), _session_returning())
    issues = [i for i in result.issues if i.code == "HITL_RESPONSE_CONTRACT_CHOICE_REQUIRES_OPTIONS"]
    assert len(issues) == 1


# --- unknown kind ---


@pytest.mark.parametrize("graph_builder", [_gated_edge_graph, _hitl_node_graph])
async def test_unknown_kind_rejected(graph_builder):
    rc = {"kind": "unknown_kind"}
    result = await GraphValidator().validate_definition(graph_builder(rc), _session_returning())
    issues = [i for i in result.issues if i.code == "HITL_RESPONSE_CONTRACT_UNKNOWN_KIND"]
    assert len(issues) == 1
    assert "unknown_kind" in issues[0].message


# --- invalid response_contract shape ---


@pytest.mark.parametrize("graph_builder", [_gated_edge_graph, _hitl_node_graph])
async def test_invalid_rc_shape_rejected(graph_builder):
    rc = "not_a_dict"
    result = await GraphValidator().validate_definition(graph_builder(rc), _session_returning())
    issues = [i for i in result.issues if i.code == "HITL_RESPONSE_CONTRACT_INVALID"]
    assert len(issues) == 1


# --- option missing id ---


@pytest.mark.parametrize("graph_builder", [_gated_edge_graph, _hitl_node_graph])
async def test_option_missing_id_rejected(graph_builder):
    rc = {
        "kind": "choice",
        "options": [{"label": "Approve"}],
    }
    result = await GraphValidator().validate_definition(graph_builder(rc), _session_returning())
    issues = [i for i in result.issues if i.code == "HITL_RESPONSE_CONTRACT_OPTION_MISSING_ID"]
    assert len(issues) == 1


# --- option missing label ---


@pytest.mark.parametrize("graph_builder", [_gated_edge_graph, _hitl_node_graph])
async def test_option_missing_label_rejected(graph_builder):
    rc = {
        "kind": "choice",
        "options": [{"id": "approve"}],
    }
    result = await GraphValidator().validate_definition(graph_builder(rc), _session_returning())
    issues = [i for i in result.issues if i.code == "HITL_RESPONSE_CONTRACT_OPTION_MISSING_LABEL"]
    assert len(issues) == 1


# --- duplicate option ids ---


@pytest.mark.parametrize("graph_builder", [_gated_edge_graph, _hitl_node_graph])
async def test_duplicate_option_ids_rejected(graph_builder):
    rc = {
        "kind": "choice",
        "options": [
            {"id": "approve", "label": "Approve"},
            {"id": "approve", "label": "Also approve"},
        ],
    }
    result = await GraphValidator().validate_definition(graph_builder(rc), _session_returning())
    issues = [i for i in result.issues if i.code == "HITL_RESPONSE_CONTRACT_DUPLICATE_OPTION_ID"]
    assert len(issues) == 1


# --- no response_contract (backward compatible) ---


@pytest.mark.parametrize("graph_builder", [_gated_edge_graph, _hitl_node_graph])
async def test_no_response_contract_passes(graph_builder):
    result = await GraphValidator().validate_definition(graph_builder(), _session_returning())
    issues = [i for i in result.issues if "RESPONSE_CONTRACT" in i.code]
    assert issues == []
