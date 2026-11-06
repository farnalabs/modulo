"""Deterministic System Maps discovery eval contract and adapter."""

from __future__ import annotations

from copy import deepcopy

import pytest

from modulo.core.eval_engine.discovery_eval import (
    DISCOVERY_CONTRACT_VERSION,
    DISCOVERY_EVALUATOR_VERSION,
    DiscoveryEvalError,
    evaluate_discovery_case,
    normalise_discovery_output,
    parse_discovery_input,
)
from modulo.core.system_maps_discovery import discover_draft

_HAND_AUTHORED_EXPECTED_GRAPH = {
    "nodes": [
        {
            "node_key": "folder:f1",
            "node_type": "external_system",
            "display_name": "Delivery",
            "config_json": {"source_kind": "folder", "source_id": "f1", "folder_id": "f1"},
            "pipeline_id": None,
            "connector_instance_id": None,
            "owner_team_id": None,
            "position_x": 0,
            "position_y": 0,
        },
        {
            "node_key": "pipeline:p1",
            "node_type": "pipeline",
            "display_name": "Build",
            "config_json": {"source_kind": "pipeline", "source_id": "p1"},
            "pipeline_id": None,
            "connector_instance_id": None,
            "owner_team_id": None,
            "position_x": 0,
            "position_y": 0,
        },
        {
            "node_key": "trigger:t1",
            "node_type": "pipeline",
            "display_name": "Push",
            "config_json": {"source_kind": "trigger", "source_id": "t1", "trigger_type": "webhook"},
            "pipeline_id": None,
            "connector_instance_id": None,
            "owner_team_id": None,
            "position_x": 0,
            "position_y": 0,
        },
    ],
    "relationships": [
        {
            "source_node_key": "folder:f1",
            "target_node_key": "pipeline:p1",
            "relationship_type": "flow",
            "config_json": {"suggested": True, "reason": "pipeline explicitly belongs to folder"},
        },
        {
            "source_node_key": "trigger:t1",
            "target_node_key": "pipeline:p1",
            "relationship_type": "trigger",
            "config_json": {"suggested": True, "reason": "trigger explicitly targets pipeline"},
        },
    ],
    "warnings": [],
    "skipped": [],
}


def _case(sources: list[dict[str, object]]) -> tuple[dict[str, object], dict[str, object]]:
    payload = {"sources": sources}
    expected = normalise_discovery_output(discover_draft(parse_discovery_input(payload)))
    return payload, expected


def _source(kind: str, identifier: str, name: str, **extra: object) -> dict[str, object]:
    return {"kind": kind, "identifier": identifier, "name": name, **extra}


def test_contract_is_versioned_and_matches_hand_authored_graph() -> None:
    payload = {
        "sources": [
            _source("pipeline", "p1", "Build", folder_id="f1"),
            _source("folder", "f1", "Delivery"),
            _source("trigger", "t1", "Push", pipeline_id="p1", trigger_type="webhook"),
        ]
    }

    result = evaluate_discovery_case(payload, _HAND_AUTHORED_EXPECTED_GRAPH)

    assert result.passed is True
    assert not result.diff
    assert result.provenance["evaluator_version"] == DISCOVERY_EVALUATOR_VERSION
    assert result.provenance["case_contract_version"] == DISCOVERY_CONTRACT_VERSION


def test_all_seeded_discovery_scenarios_are_repeatable() -> None:
    scenarios = [
        [_source("pipeline", "p1", "Build"), _source("trigger", "t1", "Push", pipeline_id="p1")],
        [_source("folder", "f1", "Delivery"), _source("pipeline", "p1", "Build", folder_id="f1")],
        [_source("trigger", "t1", "Manual")],
        [_source("trigger", "t1", "Orphan", pipeline_id="missing")],
        [_source("pipeline", "p1", "Build", folder_id="missing")],
        [_source("connector", "c1", "GitHub", connector_type_id="github")],
        [_source("pipeline", "p1", "improve-tests")],
        [_source("unsupported", "x", "Unsupported")],
        [],
        [
            _source("pipeline", "p1", "Build"),
            _source("trigger", "t1", "Push", pipeline_id="p1"),
            _source("trigger", "t1", "Push duplicate", pipeline_id="p1"),
        ],
    ]
    for sources in scenarios:
        payload, expected = _case(sources)
        first = evaluate_discovery_case(payload, expected)
        reversed_result = evaluate_discovery_case({"sources": list(reversed(sources))}, expected)
        assert first.passed is True
        assert reversed_result.passed is True
        assert first.actual == reversed_result.actual


def test_duplicate_relationships_are_canonicalised() -> None:
    payload, expected = _case(
        [
            _source("pipeline", "p1", "Build"),
            _source("trigger", "t1", "Push", pipeline_id="p1"),
            _source("trigger", "t1", "Push duplicate", pipeline_id="p1"),
        ]
    )
    assert len(expected["relationships"]) == 1
    assert evaluate_discovery_case(payload, expected).passed is True


def test_mismatch_contains_human_detail_and_structured_diff() -> None:
    payload, expected = _case([_source("pipeline", "p1", "Build")])
    wrong = deepcopy(expected)
    wrong["nodes"][0]["display_name"] = "Wrong"

    result = evaluate_discovery_case(payload, wrong)

    assert result.passed is False
    assert "mismatch" in result.detail
    assert result.diff[0]["path"] == ["nodes", 0, "display_name"]
    assert result.diff[0]["expected"] == "Wrong"
    assert result.diff[0]["actual"] == "Build"


@pytest.mark.parametrize(
    ("payload", "expected_message"),
    [
        ({}, "invalid discovery input"),
        ({"sources": "not-a-list"}, "invalid discovery input"),
        ({"sources": [{"kind": "pipeline", "identifier": 1, "name": "Build"}]}, "invalid discovery input"),
        (
            {"sources": [{"kind": "pipeline", "identifier": "p", "name": "Build", "unknown": True}]},
            "invalid discovery input",
        ),
    ],
)
def test_malformed_input_is_rejected_without_coercion(payload: object, expected_message: str) -> None:
    with pytest.raises(DiscoveryEvalError, match=expected_message):
        parse_discovery_input(payload)


def test_malformed_case_returns_structured_error() -> None:
    result = evaluate_discovery_case({"sources": []}, None)

    assert result.passed is False
    assert result.error is True
    assert result.diff
    assert result.provenance["evaluator_version"] == DISCOVERY_EVALUATOR_VERSION


def test_expected_output_rejects_unknown_and_missing_fields() -> None:
    _payload, expected = _case([_source("pipeline", "p1", "Build")])

    with pytest.raises(DiscoveryEvalError, match="unknown fields"):
        normalise_discovery_output({**expected, "extra": []})
    with pytest.raises(DiscoveryEvalError, match="missing fields"):
        normalise_discovery_output({"nodes": []})
