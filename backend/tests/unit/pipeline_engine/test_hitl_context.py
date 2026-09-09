"""Unit tests for FAR-613 fire-time HITL gate context capture.

Covers ``build_hitl_gate_context`` and its helpers: the briefing bundle
(shape, trigger kinds, condition/node-id extraction, bounded artifacts,
reason extraction, determinism), the FAR-688 matched-condition evidence
(``condition_result`` as PRIMARY evidence), the failure-isolation contract
(a capture defect never raises — it yields ``None``), and the
legacy-snapshot fallback (minimal bundle when the gate config cannot be
resolved, trigger reported as ``unknown`` rather than guessed).
"""

import json
import uuid
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from modulo.core.pipeline_engine.hitl_context import (
    _NAME_FIELD_MAX_CHARS,
    _TEXT_FIELD_MAX_CHARS,
    ARTIFACTS_BUDGET_CHARS,
    REASON_ABSENT,
    TRUNCATION_MARKER,
    build_hitl_gate_context,
    extract_condition_node_ids,
)

_UUID_SRC = "550e8400-e29b-41d4-a716-446655440000"
_UUID_TGT = "660e8400-e29b-41d4-a716-446655440001"
_UUID_OTHER = "770e8400-e29b-41d4-a716-446655440002"
_GATE_ID = f"hitl_gate_{_UUID_SRC}_{_UUID_TGT}"
_ORG_ID = uuid.uuid4()
_RUN_ID = uuid.uuid4()


def _make_session(graph_json: dict[str, Any] | None) -> AsyncMock:
    """Mock AsyncSession: get_run patched separately; execute() returns the snapshot."""
    session = AsyncMock()
    execute_result = MagicMock()
    execute_result.scalar_one_or_none.return_value = graph_json
    session.execute = AsyncMock(return_value=execute_result)
    return session


def _run_mock(snapshot_id: uuid.UUID | None) -> MagicMock:
    run = MagicMock()
    run.snapshot_id = snapshot_id
    return run


def _edge_graph(config: dict[str, Any]) -> dict[str, Any]:
    return {
        "nodes": [{"id": _UUID_SRC, "label": "Comment Generator"}, {"id": _UUID_TGT}],
        "edges": [{"source": _UUID_SRC, "target": _UUID_TGT, "type": "normal", "hitl_gate_config": config}],
    }


def _hitl_node_graph(config: dict[str, Any]) -> dict[str, Any]:
    return {
        "nodes": [
            {"id": _UUID_SRC, "node_type": "hitl", "hitl_config": config, "label": "Human Review"},
            {"id": _UUID_TGT},
        ],
        "edges": [{"source": _UUID_SRC, "target": _UUID_TGT, "type": "normal"}],
    }


async def _build(
    graph_json: dict[str, Any] | None,
    *,
    gate_id: str = _GATE_ID,
    completed_node_outputs: dict[str, Any] | None = None,
    pipeline_name: str | None = "PR Reviewer",
    condition_result: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    session = _make_session(graph_json)
    with (
        patch("modulo.core.pipeline_engine.hitl_context.get_run", AsyncMock(return_value=_run_mock(uuid.uuid4()))),
    ):
        return await build_hitl_gate_context(
            session,
            run_id=_RUN_ID,
            gate_id=gate_id,
            org_id=_ORG_ID,
            pipeline_name=pipeline_name,
            completed_node_outputs=completed_node_outputs or {},
            condition_result=condition_result,
        )


class TestEdgeGateContext:
    async def test_condition_gate_bundle_shape(self):
        config = {
            "description": "Approve the comments before posting.",
            "condition": f"node_id=='{_UUID_OTHER}'",
        }
        graph = _edge_graph(config)
        context = await _build(
            graph,
            completed_node_outputs={_UUID_OTHER: {"comment": "ship it"}, _UUID_SRC: {"ignored": True}},
        )
        assert context is not None
        assert context["description"] == "Approve the comments before posting."
        assert context["condition"] == config["condition"]
        assert context["condition_result"] is None
        assert context["trigger"] == "condition"
        assert context["source_node_id"] == _UUID_SRC
        assert context["source_node_label"] == "Comment Generator"
        assert context["pipeline_name"] == "PR Reviewer"
        # Only the node the condition references is excerpted, not the source node.
        assert [a["node_id"] for a in context["artifacts"]] == [_UUID_OTHER]
        assert '"comment"' in context["artifacts"][0]["summary"]

    async def test_condition_without_referenced_uuids_falls_back_to_source_node_artifact(self):
        config = {"description": "Approve the comments before posting.", "condition": "output.severity == 'high'"}
        graph = _edge_graph(config)
        context = await _build(graph, completed_node_outputs={_UUID_SRC: {"severity": "high"}})
        assert context is not None
        assert [a["node_id"] for a in context["artifacts"]] == [_UUID_SRC]

    async def test_missing_condition_node_output_yields_empty_artifacts(self):
        config = {"description": "Approve the comments before posting.", "condition": f"node_id=='{_UUID_OTHER}'"}
        context = await _build(_edge_graph(config), completed_node_outputs={})
        assert context is not None
        assert not context["artifacts"]


class TestConditionResultEvidence:
    """FAR-688: the matched condition value is the briefing's PRIMARY evidence."""

    async def test_matched_value_stored_with_expression_and_node(self):
        config = {"description": "Approve the comments before posting.", "condition": "output.review"}
        context = await _build(
            _edge_graph(config),
            completed_node_outputs={_UUID_SRC: {"review": {"score": 0.9}}},
            condition_result={"expression": "output.review", "value": '{"score": 0.9}'},
        )
        assert context is not None
        assert context["condition_result"] == {
            "expression": "output.review",
            "value": '{"score": 0.9}',
            "evaluated_at_node": _UUID_SRC,
        }

    async def test_legacy_payload_without_result_keeps_supplementary_path_unchanged(self):
        """A payload that predates the condition_result member (or a manual
        node interrupt) gets exactly the FAR-613 bundle: no evidence, and the
        regex-extracted artifacts remain the (supplementary) record."""
        config = {"description": "Approve the comments before posting.", "condition": f"node_id=='{_UUID_OTHER}'"}
        graph = _edge_graph(config)
        outputs = {_UUID_OTHER: {"comment": "ship it"}}
        with_evidence = await _build(
            graph,
            completed_node_outputs=outputs,
            condition_result={"expression": config["condition"], "value": "true"},
        )
        without_evidence = await _build(graph, completed_node_outputs=outputs, condition_result=None)
        assert with_evidence is not None
        assert without_evidence is not None
        assert without_evidence["condition_result"] is None
        assert with_evidence["artifacts"] == without_evidence["artifacts"]
        assert with_evidence["condition"] == without_evidence["condition"]
        assert with_evidence["trigger"] == without_evidence["trigger"]

    async def test_condition_result_recorded_even_when_config_unresolvable(self):
        """The payload IS the fire-time truth: when the snapshot cannot
        resolve the gate config (legacy/drift — trigger unknown), the matched
        value is still recorded so the briefing keeps its PRIMARY evidence."""
        context = await _build(
            {"nodes": [], "edges": []},
            condition_result={"expression": "output.review", "value": "true"},
        )
        assert context is not None
        assert context["trigger"] == "unknown"
        assert context["condition_result"] == {
            "expression": "output.review",
            "value": "true",
            "evaluated_at_node": _UUID_SRC,
        }

    async def test_non_dict_condition_result_tolerated(self):
        config = {"description": "Approve the comments before posting.", "condition": "ready"}
        context = await _build(_edge_graph(config), condition_result="garbage")
        assert context is not None
        assert context["condition_result"] is None

    async def test_condition_result_value_bounded(self):
        config = {"description": "Approve the comments before posting.", "condition": "blob"}
        context = await _build(
            _edge_graph(config),
            condition_result={"expression": "blob", "value": "v" * 5000},
        )
        assert context is not None
        evidence = context["condition_result"]
        assert evidence is not None
        assert len(evidence["value"]) <= _TEXT_FIELD_MAX_CHARS
        assert evidence["value"].endswith(TRUNCATION_MARKER)


class TestNodeGateContext:
    async def test_node_gate_bundle_carries_reason_and_own_output(self):
        graph = _hitl_node_graph({"description": "Human confirms the incident resolution."})
        context = await _build(
            graph,
            completed_node_outputs={_UUID_SRC: {"reason": "Two failed escalations in a row.", "summary": "..."}},
        )
        assert context is not None
        assert context["trigger"] == "node"
        assert context["description"] == "Human confirms the incident resolution."
        assert context["condition"] is None
        assert context["reason"] == "Two failed escalations in a row."
        assert [a["node_id"] for a in context["artifacts"]] == [_UUID_SRC]

    async def test_reason_extracted_from_envelope_output(self):
        graph = _hitl_node_graph({"description": "Human confirms the incident resolution."})
        context = await _build(
            graph,
            completed_node_outputs={_UUID_SRC: {"output": {"reason": "nested reason"}}},
        )
        assert context is not None
        assert context["reason"] == "nested reason"

    async def test_reason_absent_falls_back_to_documented_string(self):
        graph = _hitl_node_graph({"description": "Human confirms the incident resolution."})
        context = await _build(graph, completed_node_outputs={_UUID_SRC: {"summary": "no reason here"}})
        assert context is not None
        assert context["reason"] == REASON_ABSENT

    async def test_node_gate_without_completed_output_still_bundles_briefing(self):
        graph = _hitl_node_graph({"description": "Human confirms the incident resolution."})
        context = await _build(graph, completed_node_outputs={})
        assert context is not None
        assert context["reason"] is None
        assert not context["artifacts"]
        assert context["trigger"] == "node"


class TestLegacyFallback:
    async def test_unresolvable_config_persists_minimal_bundle(self):
        """A legacy snapshot without the gate config still yields a usable
        minimal briefing (source node + pipeline name), never an empty dict.
        The source node is visible and is NOT a HITL node, but no edge config
        resolved either — the trigger is genuinely undeterminable, so it is
        reported as ``unknown`` rather than guessed (FAR-688)."""
        graph = {"nodes": [{"id": _UUID_SRC, "label": "Comment Generator"}], "edges": []}
        context = await _build(graph)
        assert context is not None
        assert context["description"] is None
        assert context["trigger"] == "unknown"
        assert context["source_node_id"] == _UUID_SRC
        assert context["pipeline_name"] == "PR Reviewer"

    async def test_missing_snapshot_reports_unknown_trigger(self):
        """FAR-688: without a snapshot the node's type cannot be verified —
        the old fallback guessed "condition", misclassifying node gates."""
        context = await _build(None)
        assert context is not None
        assert context["trigger"] == "unknown"

    async def test_snapshot_hitl_node_still_reports_node_trigger(self):
        """When the snapshot IS present and the source node is a FAR-402
        HITL node, the trigger is still inferred as ``node``."""
        graph = {
            "nodes": [{"id": _UUID_SRC, "node_type": "hitl", "label": "Human Review"}],
            "edges": [{"source": _UUID_SRC, "target": _UUID_TGT, "type": "normal"}],
        }
        context = await _build(graph)
        assert context is not None
        assert context["trigger"] == "node"

    async def test_missing_run_yields_none(self):
        session = _make_session({"nodes": [], "edges": []})
        with patch("modulo.core.pipeline_engine.hitl_context.get_run", AsyncMock(return_value=None)):
            context = await build_hitl_gate_context(
                session,
                run_id=_RUN_ID,
                gate_id=_GATE_ID,
                org_id=_ORG_ID,
                pipeline_name="PR Reviewer",
                completed_node_outputs={},
            )
        assert context is None


class TestFailureIsolation:
    async def test_capture_failure_returns_none_and_never_raises(self):
        session = AsyncMock()
        with patch(
            "modulo.core.pipeline_engine.hitl_context.get_run",
            AsyncMock(side_effect=RuntimeError("db exploded")),
        ):
            context = await build_hitl_gate_context(
                session,
                run_id=_RUN_ID,
                gate_id=_GATE_ID,
                org_id=_ORG_ID,
                pipeline_name="PR Reviewer",
                completed_node_outputs={},
            )
        assert context is None

    async def test_snapshot_query_failure_returns_none(self):
        session = AsyncMock()
        session.execute = AsyncMock(side_effect=RuntimeError("snapshot read failed"))
        with patch(
            "modulo.core.pipeline_engine.hitl_context.get_run",
            AsyncMock(return_value=_run_mock(uuid.uuid4())),
        ):
            context = await build_hitl_gate_context(
                session,
                run_id=_RUN_ID,
                gate_id=_GATE_ID,
                org_id=_ORG_ID,
                pipeline_name="PR Reviewer",
                completed_node_outputs={},
            )
        assert context is None


class TestTruncationBounds:
    async def test_artifacts_serialised_within_budget(self):
        huge_output = {"blob": "x" * 100_000}
        config = {"description": "Approve the deploy.", "condition": f"node_id=='{_UUID_OTHER}'"}
        context = await _build(
            _edge_graph(config),
            completed_node_outputs={_UUID_OTHER: huge_output, _UUID_SRC: huge_output},
        )
        assert context is not None
        serialised = json.dumps(context["artifacts"], sort_keys=True, ensure_ascii=False)
        assert len(serialised) <= ARTIFACTS_BUDGET_CHARS
        assert len(context["artifacts"][0]["summary"]) <= ARTIFACTS_BUDGET_CHARS

    async def test_sliced_artifact_summary_carries_truncation_marker(self):
        """FAR-688: an entry summary sliced to fit the budget is marked, so a
        reviewer can tell a partial excerpt from a complete one."""
        huge_output = {"blob": "x" * 100_000}
        config = {"description": "Approve the deploy.", "condition": f"node_id=='{_UUID_OTHER}'"}
        context = await _build(
            _edge_graph(config),
            completed_node_outputs={_UUID_OTHER: huge_output},
        )
        assert context is not None
        summary = context["artifacts"][0]["summary"]
        assert summary.endswith(TRUNCATION_MARKER)

    async def test_oversized_entry_summary_carries_truncation_marker(self):
        """FAR-688: the per-entry slice in _artifact_entry is marked too."""
        output = {"blob": "y" * 5000}
        config = {"description": "Approve the deploy.", "condition": f"node_id=='{_UUID_OTHER}'"}
        context = await _build(
            _edge_graph(config),
            completed_node_outputs={_UUID_OTHER: output},
        )
        assert context is not None
        assert context["artifacts"][0]["summary"].endswith(TRUNCATION_MARKER)
        assert len(context["artifacts"][0]["summary"]) <= 1200 + len(TRUNCATION_MARKER)

    async def test_deterministic_capture_same_inputs_same_bundle(self):
        config = {"description": "Approve the deploy.", "condition": f"node_id=='{_UUID_OTHER}'"}
        graph = _edge_graph(config)
        outputs = {_UUID_OTHER: {"n": 1, "blob": "y" * 5000}}
        first = await _build(graph, completed_node_outputs=outputs)
        second = await _build(graph, completed_node_outputs=outputs)
        assert json.dumps(first, sort_keys=True) == json.dumps(second, sort_keys=True)

    async def test_source_node_label_bounded_to_name_field_max(self):
        """FAR-688: source_node_label gets a capture-side 255-char slice
        (matching the save contracts' name columns)."""
        long_label = "L" * 500
        graph = {
            "nodes": [{"id": _UUID_SRC, "label": long_label}, {"id": _UUID_TGT}],
            "edges": [{"source": _UUID_SRC, "target": _UUID_TGT, "type": "normal"}],
        }
        context = await _build(graph)
        assert context is not None
        assert len(context["source_node_label"]) <= _NAME_FIELD_MAX_CHARS

    async def test_pipeline_name_bounded_to_name_field_max(self):
        context = await _build({"nodes": [], "edges": []}, pipeline_name="P" * 500)
        assert context is not None
        assert len(context["pipeline_name"]) <= _NAME_FIELD_MAX_CHARS


class TestRedactionAndTextBounds:
    """FAR-188 redaction + capture-side text bounds (review findings)."""

    async def test_credential_bearing_artifact_summary_is_redacted(self):
        graph = _hitl_node_graph({"description": "Human confirms the incident resolution."})
        leaked = {"summary": f"deployed with ghp_{'a' * 30} token embedded"}
        context = await _build(graph, completed_node_outputs={_UUID_SRC: leaked})
        assert context is not None
        summary = context["artifacts"][0]["summary"]
        assert "ghp_" not in summary
        assert "<redacted>" in summary

    async def test_reason_is_redacted(self):
        graph = _hitl_node_graph({"description": "Human confirms the incident resolution."})
        leaked = {"reason": f"model call failed with key sk-{'x' * 12}"}
        context = await _build(graph, completed_node_outputs={_UUID_SRC: leaked})
        assert context is not None
        assert "sk-" not in context["reason"]
        assert "<redacted>" in context["reason"]

    async def test_reason_bounded_to_text_field_max(self):
        graph = _hitl_node_graph({"description": "Human confirms the incident resolution."})
        context = await _build(graph, completed_node_outputs={_UUID_SRC: {"reason": "r" * 5000}})
        assert context is not None
        assert len(context["reason"]) <= _TEXT_FIELD_MAX_CHARS

    async def test_node_gate_description_bounded(self):
        graph = _hitl_node_graph({"description": "d" * 5000})
        context = await _build(graph, completed_node_outputs={})
        assert context is not None
        assert len(context["description"]) <= _TEXT_FIELD_MAX_CHARS

    async def test_condition_bounded(self):
        config = {"description": "Approve the deploy.", "condition": "c" * 5000}
        context = await _build(_edge_graph(config), completed_node_outputs={})
        assert context is not None
        assert len(context["condition"]) <= _TEXT_FIELD_MAX_CHARS


class TestExtractConditionNodeIds:
    def test_extracts_uuids_in_order_and_dedupes(self):
        condition = f"node_id=='{_UUID_OTHER}' || node_id=='{_UUID_SRC}' || node_id=='{_UUID_OTHER}'"
        assert extract_condition_node_ids(condition) == [_UUID_OTHER, _UUID_SRC]

    def test_ignores_bare_word_keys(self):
        assert not extract_condition_node_ids("state['summary'] == 'high'")

    def test_empty_condition(self):
        assert not extract_condition_node_ids(None)
        assert not extract_condition_node_ids("")


@pytest.mark.parametrize("gate_id", ["hitl_gate_not-a-gate", "some_node", "hitl_gate_onlyone"])
async def test_non_topology_gate_ids_still_build_minimal_bundle(gate_id):
    graph = {"nodes": [], "edges": []}
    context = await _build(graph, gate_id=gate_id)
    assert context is not None
    assert context["source_node_id"] is None
