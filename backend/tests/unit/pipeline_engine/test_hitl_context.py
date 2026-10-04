"""Unit tests for FAR-613 fire-time HITL gate context capture.

Covers ``build_hitl_review_context`` and its helpers: the briefing bundle
(shape, trigger kinds, condition/node-id extraction, bounded artifacts,
reason extraction, determinism), the FAR-688 matched-condition evidence
(``condition_result`` as PRIMARY evidence), the failure-isolation contract
(a capture defect never raises — it yields ``None``), and the
legacy-snapshot fallback (minimal bundle when the gate config cannot be
resolved, trigger reported as ``unknown`` rather than guessed).
"""

import json
import logging
import uuid
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from modulo.core.pipeline_engine import graph_cache
from modulo.core.pipeline_engine.hitl_context import (
    _NAME_FIELD_MAX_CHARS,
    _TEXT_FIELD_MAX_CHARS,
    ARTIFACTS_BUDGET_CHARS,
    REASON_ABSENT,
    TRUNCATION_MARKER,
    _resolve_consequences,
    build_hitl_review_context,
    extract_condition_node_ids,
    serialize_value,
    slice_with_marker,
)
from modulo.db.crud.hitl_review_config import make_review_id

_UUID_SRC = "550e8400-e29b-41d4-a716-446655440000"
_UUID_TGT = "660e8400-e29b-41d4-a716-446655440001"
_UUID_OTHER = "770e8400-e29b-41d4-a716-446655440002"
_EDGE_A = "880e8400-e29b-41d4-a716-446655440010"
_EDGE_B = "880e8400-e29b-41d4-a716-446655440011"
_REVIEW_ID = f"hitl_review_{_UUID_SRC}_{_UUID_TGT}"
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
        "edges": [{"source": _UUID_SRC, "target": _UUID_TGT, "type": "normal", "hitl_review_config": config}],
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
    review_id: str = _REVIEW_ID,
    completed_node_outputs: dict[str, Any] | None = None,
    pipeline_name: str | None = "PR Reviewer",
    condition_result: dict[str, Any] | None = None,
    subject: str | None = None,
    subject_parent: dict[str, Any] | None = None,
    subject_leaf_key: str | None = None,
) -> dict[str, Any] | None:
    session = _make_session(graph_json)
    with (
        patch("modulo.core.pipeline_engine.hitl_context.get_run", AsyncMock(return_value=_run_mock(uuid.uuid4()))),
    ):
        return await build_hitl_review_context(
            session,
            run_id=_RUN_ID,
            review_id=review_id,
            org_id=_ORG_ID,
            pipeline_name=pipeline_name,
            completed_node_outputs=completed_node_outputs or {},
            condition_result=condition_result,
            subject=subject,
            subject_parent=subject_parent,
            subject_leaf_key=subject_leaf_key,
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
            context = await build_hitl_review_context(
                session,
                run_id=_RUN_ID,
                review_id=_REVIEW_ID,
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
            context = await build_hitl_review_context(
                session,
                run_id=_RUN_ID,
                review_id=_REVIEW_ID,
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
            context = await build_hitl_review_context(
                session,
                run_id=_RUN_ID,
                review_id=_REVIEW_ID,
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
        """FAR-688: the per-entry slice in _artifact_entry is marked too —
        marker-WITHIN-cap, so a sliced summary is exactly the entry cap."""
        output = {"blob": "y" * 5000}
        config = {"description": "Approve the deploy.", "condition": f"node_id=='{_UUID_OTHER}'"}
        context = await _build(
            _edge_graph(config),
            completed_node_outputs={_UUID_OTHER: output},
        )
        assert context is not None
        assert context["artifacts"][0]["summary"].endswith(TRUNCATION_MARKER)
        assert len(context["artifacts"][0]["summary"]) == 1200

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


@pytest.mark.parametrize("review_id", ["hitl_review_not-a-gate", "some_node", "hitl_review_onlyone"])
async def test_non_topology_review_ids_still_build_minimal_bundle(review_id):
    graph = {"nodes": [], "edges": []}
    context = await _build(graph, review_id=review_id)
    assert context is not None
    assert context["source_node_id"] is None


class TestSliceWithMarker:
    """FAR-688: the ONE truncation idiom — marker-WITHIN-cap semantics."""

    def test_short_string_unchanged_without_marker(self):
        text = "a short string"
        assert slice_with_marker(text, 100) == text

    def test_string_exactly_at_cap_unchanged(self):
        text = "e" * 50
        assert slice_with_marker(text, 50) == text

    def test_long_string_never_exceeds_cap_and_ends_with_marker(self):
        text = "x" * 5000
        sliced = slice_with_marker(text, 1200)
        assert len(sliced) == 1200
        assert sliced.endswith(TRUNCATION_MARKER)
        assert sliced.startswith("x" * (1200 - len(TRUNCATION_MARKER)))

    def test_cap_respected_for_very_long_strings(self):
        text = "z" * 100_000
        assert len(slice_with_marker(text, 2048)) == 2048

    def test_empty_string_unchanged(self):
        assert not slice_with_marker("", 100)

    def test_degenerate_cap_never_exceeds_it(self):
        """A cap smaller than the marker still cannot produce an over-cap
        string — the guard slices without the marker instead."""
        sliced = slice_with_marker("abcdef", 4)
        assert len(sliced) == 4


class TestSerializeValue:
    """The shared deterministic serializer behind the briefing bundle."""

    def test_sorts_keys_and_stringifies_non_json_values(self):
        assert serialize_value({"b": 1, "a": uuid.UUID("00000000-0000-0000-0000-000000000001")}) == (
            '{"a": "00000000-0000-0000-0000-000000000001", "b": 1}'
        )

    def test_matches_dumps_sort_keys_default_str(self):
        value = {"k": 2, "z": [1, 2], "n": None}
        assert serialize_value(value) == json.dumps(value, sort_keys=True, default=str, ensure_ascii=False)


class TestSubjectCapture:
    """FAR-859: the subject under review (subject_path resolution)."""

    async def test_subject_present_when_provided(self):
        config = {"description": "Approve the comments.", "condition": f"node_id=='{_UUID_OTHER}'"}
        context = await _build(
            _edge_graph(config),
            completed_node_outputs={_UUID_OTHER: {"comment": "ship it"}},
            subject='{"comment":"ship it"}',
        )
        assert context is not None
        assert context["subject"] == '{"comment":"ship it"}'

    async def test_subject_absent_when_not_provided(self):
        config = {"description": "Approve the comments.", "condition": f"node_id=='{_UUID_OTHER}'"}
        context = await _build(_edge_graph(config))
        assert context is not None
        assert "subject" not in context or context.get("subject") is None

    async def test_subject_bounded_to_text_field_max(self):
        config = {"description": "Approve the comments.", "condition": f"node_id=='{_UUID_OTHER}'"}
        context = await _build(
            _edge_graph(config),
            subject="s" * 5000,
        )
        assert context is not None
        assert len(context["subject"]) <= _TEXT_FIELD_MAX_CHARS
        assert context["subject"].endswith(TRUNCATION_MARKER)

    async def test_subject_redacted_for_credentials(self):
        """Subject derived from node output is redacted (FAR-188)."""
        config = {"description": "Approve the comments.", "condition": f"node_id=='{_UUID_OTHER}'"}
        context = await _build(
            _edge_graph(config),
            subject=f"deployed with ghp_{'a' * 30} token",
        )
        assert context is not None
        assert "ghp_" not in context["subject"]
        assert "<redacted>" in context["subject"]

    async def test_legacy_gate_without_subject_omits_field(self):
        """Legacy gates that never declared subject_path have no subject."""
        config = {"description": "Approve the comments."}
        context = await _build(_edge_graph(config))
        assert context is not None
        # total=False TypedDict: key absent or None
        assert context.get("subject") is None

    # FAR-862: the parent container holding the subject is captured so the
    # frontend can reconstruct a shape-preserving modified_output.

    async def test_subject_parent_captured_with_leaf_key(self):
        config = {"description": "Approve the comments.", "condition": f"node_id=='{_UUID_OTHER}'"}
        parent = {"body": "Review this comment.", "priority": "high"}
        context = await _build(
            _edge_graph(config),
            subject='"Review this comment."',
            subject_parent=parent,
            subject_leaf_key="body",
        )
        assert context is not None
        # Sibling keys survive the capture (shape-preserving reconstruction
        # depends on them).
        assert context["subject_parent"] == parent
        assert context["subject_leaf_key"] == "body"

    async def test_subject_parent_redacted_for_credentials(self):
        """The parent is node-output content, so it is redacted (FAR-188)."""
        config = {"description": "Approve the comments.", "condition": f"node_id=='{_UUID_OTHER}'"}
        context = await _build(
            _edge_graph(config),
            subject="ship it",
            subject_parent={"body": f"ghp_{'a' * 30}", "priority": "high"},
            subject_leaf_key="body",
        )
        assert context is not None
        parent = context["subject_parent"]
        assert parent is not None
        assert "ghp_" not in json.dumps(parent)
        assert parent["priority"] == "high"

    async def test_subject_parent_over_budget_is_dropped_not_partially_captured(self):
        """Fail-safe: a parent whose serialised form exceeds the budget cannot
        be bounded into valid JSON, so it is dropped entirely (None) rather
        than captured as a lossy partial dict."""
        config = {"description": "Approve the comments.", "condition": f"node_id=='{_UUID_OTHER}'"}
        context = await _build(
            _edge_graph(config),
            subject="s" * (ARTIFACTS_BUDGET_CHARS + 100),
            subject_parent={"body": "s" * (ARTIFACTS_BUDGET_CHARS + 100)},
            subject_leaf_key="body",
        )
        assert context is not None
        assert context["subject_parent"] is None
        assert context["subject_leaf_key"] is None

    async def test_subject_parent_absent_when_leaf_key_missing(self):
        """No leaf key means no shape-preserving reconstruction is possible, so
        the parent is not captured (fail-safe)."""
        config = {"description": "Approve the comments.", "condition": f"node_id=='{_UUID_OTHER}'"}
        context = await _build(
            _edge_graph(config),
            subject="ship it",
            subject_parent={"body": "ship it"},
        )
        assert context is not None
        assert context["subject_parent"] is None
        assert context["subject_leaf_key"] is None


class TestConsequences:
    """FAR-859: approve/reject routing from the snapshot graph."""

    async def test_approve_and_reject_targets_resolved(self):
        config = {
            "description": "Approve the comments.",
            "condition": f"node_id=='{_UUID_SRC}'",
            "reject_target": _UUID_OTHER,
        }
        graph = {
            "nodes": [
                {"id": _UUID_SRC, "label": "Comment Gen"},
                {"id": _UUID_TGT, "label": "Poster"},
                {"id": _UUID_OTHER, "label": "Fixer"},
            ],
            "edges": [
                {"source": _UUID_SRC, "target": _UUID_TGT, "type": "normal", "hitl_review_config": config},
            ],
        }
        context = await _build(graph, completed_node_outputs={_UUID_SRC: {"ok": True}})
        assert context is not None
        consequences = context["consequences"]
        assert consequences is not None
        assert consequences["approve"]["node_id"] == _UUID_TGT
        assert consequences["approve"]["label"] == "Poster"
        assert consequences["reject"]["node_id"] == _UUID_OTHER
        assert consequences["reject"]["label"] == "Fixer"

    async def test_approve_only_when_no_reject_target(self):
        config = {
            "description": "Approve the comments.",
            "condition": f"node_id=='{_UUID_SRC}'",
        }
        graph = {
            "nodes": [
                {"id": _UUID_SRC, "label": "Generator"},
                {"id": _UUID_TGT, "label": "Next"},
            ],
            "edges": [
                {"source": _UUID_SRC, "target": _UUID_TGT, "type": "normal", "hitl_review_config": config},
            ],
        }
        context = await _build(graph, completed_node_outputs={_UUID_SRC: {"ok": True}})
        assert context is not None
        consequences = context["consequences"]
        assert consequences is not None
        assert "approve" in consequences
        assert "reject" not in consequences

    async def test_reject_target_resolved_from_reject_edge_when_config_has_none(self):
        """FAR-1486: an edge-wired reject route must resolve a reject
        consequence even when the gate config declares no ``reject_target``.

        The graph compiler resolves the reject destination from gate config OR
        a reject-typed edge (graph_cache._build_reject_targets, consulted in
        _add_hitl_review_edge), so the reviewer briefing must show the same
        consequence for a pipeline whose reject route is wired as an edge.
        """
        config = {
            "description": "Approve the comments.",
            "condition": f"node_id=='{_UUID_SRC}'",
        }
        graph = {
            "nodes": [
                {"id": _UUID_SRC, "label": "Comment Gen"},
                {"id": _UUID_TGT, "label": "Poster"},
                {"id": _UUID_OTHER, "label": "Fixer"},
            ],
            "edges": [
                {"source": _UUID_SRC, "target": _UUID_TGT, "type": "normal", "hitl_review_config": config},
                {"source": _UUID_SRC, "target": _UUID_OTHER, "type": "reject"},
            ],
        }
        context = await _build(graph, completed_node_outputs={_UUID_SRC: {"ok": True}})
        assert context is not None
        consequences = context["consequences"]
        assert consequences is not None
        assert consequences["approve"]["node_id"] == _UUID_TGT
        assert consequences["reject"]["node_id"] == _UUID_OTHER
        assert consequences["reject"]["label"] == "Fixer"

    async def test_config_reject_target_wins_over_reject_edge(self):
        """Config ``reject_target`` keeps precedence over a reject-typed edge,
        mirroring the compiler's order (config reject_target > reject edge)."""
        config = {
            "description": "Approve the comments.",
            "condition": f"node_id=='{_UUID_SRC}'",
            "reject_target": _UUID_OTHER,
        }
        edge_wired_target = "880e8400-e29b-41d4-a716-446655440003"
        graph = {
            "nodes": [
                {"id": _UUID_SRC, "label": "Comment Gen"},
                {"id": _UUID_TGT, "label": "Poster"},
                {"id": _UUID_OTHER, "label": "Fixer"},
                {"id": edge_wired_target, "label": "Edge Wired"},
            ],
            "edges": [
                {"source": _UUID_SRC, "target": _UUID_TGT, "type": "normal", "hitl_review_config": config},
                {"source": _UUID_SRC, "target": edge_wired_target, "type": "reject"},
            ],
        }
        context = await _build(graph, completed_node_outputs={_UUID_SRC: {"ok": True}})
        assert context is not None
        consequences = context["consequences"]
        assert consequences is not None
        assert consequences["reject"]["node_id"] == _UUID_OTHER

    async def test_reject_edge_from_another_source_is_not_adopted(self):
        """A reject-typed edge whose source is NOT this gate's source must not
        be mistaken for this gate's reject route."""
        config = {
            "description": "Approve the comments.",
            "condition": f"node_id=='{_UUID_SRC}'",
        }
        graph = {
            "nodes": [
                {"id": _UUID_SRC, "label": "Comment Gen"},
                {"id": _UUID_TGT, "label": "Poster"},
                {"id": _UUID_OTHER, "label": "Fixer"},
            ],
            "edges": [
                {"source": _UUID_SRC, "target": _UUID_TGT, "type": "normal", "hitl_review_config": config},
                {"source": _UUID_OTHER, "target": _UUID_TGT, "type": "reject"},
            ],
        }
        context = await _build(graph, completed_node_outputs={_UUID_SRC: {"ok": True}})
        assert context is not None
        consequences = context["consequences"]
        assert consequences is not None
        assert "approve" in consequences
        assert "reject" not in consequences

    async def test_no_consequences_when_no_graph(self):
        context = await _build(None)
        assert context is not None
        assert context.get("consequences") is None

    def test_edge_not_matching_review_id_yields_no_approve_target(self):
        """An edge present in the graph but not the gate's own edge must not be
        mistaken for the approve target: the topology-derived review_id gate
        skips non-matching edges and falls through with no consequences."""
        graph = {
            "nodes": [{"id": _UUID_SRC}, {"id": _UUID_TGT}],
            "edges": [{"source": _UUID_OTHER, "target": _UUID_TGT, "type": "normal"}],
        }
        assert _resolve_consequences(graph, _REVIEW_ID, None, None) is None

    async def test_no_consequences_when_gate_not_on_edge(self):
        """HITL node gates have no edge with hitl_review_config → no approve target."""
        graph = _hitl_node_graph({"description": "Human review."})
        context = await _build(graph)
        assert context is not None
        # Node gates: no edge with review_id → no approve target resolved
        # (the node itself is the gate, not an edge target)
        consequences = context.get("consequences")
        if consequences is not None:
            # approve target may be None if no edge carries this review_id
            assert consequences.get("approve") is None or "node_id" in consequences.get("approve", {})

    async def test_consequences_node_labels_resolved_from_snapshot(self):
        config = {
            "description": "Approve.",
            "condition": f"node_id=='{_UUID_SRC}'",
            "reject_target": _UUID_OTHER,
        }
        graph = {
            "nodes": [
                {"id": _UUID_SRC},
                {"id": _UUID_TGT, "label": "Deploy Step"},
                {"id": _UUID_OTHER, "label": "Rollback"},
            ],
            "edges": [
                {"source": _UUID_SRC, "target": _UUID_TGT, "type": "normal", "hitl_review_config": config},
            ],
        }
        context = await _build(graph, completed_node_outputs={_UUID_SRC: {"ok": True}})
        assert context is not None
        assert context["consequences"]["approve"]["label"] == "Deploy Step"
        assert context["consequences"]["reject"]["label"] == "Rollback"


# ---------------------------------------------------------------------------
# FAR-1486 reject-consequence agreement tripwire + resilience
# ---------------------------------------------------------------------------


class _RecordingGraph:
    """Minimal StateGraph stand-in: records the wiring the compiler emits."""

    def __init__(self) -> None:
        self.plain_edges: list[tuple[str, str]] = []
        self.conditional: list[tuple[str, Any]] = []

    def add_node(self, _node_id: str, _fn: Any) -> None:
        pass

    def add_edge(self, source: str, target: str) -> None:
        self.plain_edges.append((source, target))

    def add_conditional_edges(self, _node_id: str, router: Any) -> None:
        self.conditional.append((_node_id, router))


def _compile_gate(edges: list[dict[str, Any]], config: dict[str, Any]) -> tuple[str | None, _RecordingGraph]:
    """Drive the REAL compiler path and report where a rejection routes.

    Runs ``graph_cache._add_hitl_review_edge`` (the single place the compiler
    decides config-vs-edge precedence and wires, or declines to wire, the
    kick-back router) against a recording graph, then drives the router with a
    matching rejected decision.

    Returns ``(reject_target, graph)``: ``reject_target is None`` means the
    compiled gate has NO reject router and a rejection falls through to the
    plain normal edge.
    """
    graph = _RecordingGraph()
    graph_cache._add_hitl_review_edge(
        graph,
        _UUID_SRC,
        _UUID_TGT,
        dict(config),
        target_ids=set(),
        gate_node_ids=set(),
        reject_targets_by_source=graph_cache._build_reject_targets(edges),
        eval_definitions_by_node=None,
        session_factory=None,
        org_id=None,
        node_type_map={},
    )
    if not graph.conditional:
        return None, graph
    _node_id, router = graph.conditional[0]
    decision = {"review_id": make_review_id(_UUID_SRC, _UUID_TGT), "action": "rejected"}
    return str(router({"_hitl_decision": decision})), graph


def _resolved_reject(graph: dict[str, Any], config: dict[str, Any] | None) -> str | None:
    consequences = _resolve_consequences(graph, _REVIEW_ID, config, _UUID_SRC)
    if consequences is None:
        return None
    reject = consequences.get("reject")
    return None if reject is None else str(reject["node_id"])


def _gate_edge(config: dict[str, Any]) -> dict[str, Any]:
    return {"source": _UUID_SRC, "target": _UUID_TGT, "type": "normal", "hitl_review_config": config}


def _graph_with(edges: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "nodes": [
            {"id": _UUID_SRC, "label": "Comment Gen"},
            {"id": _UUID_TGT, "label": "Poster"},
            {"id": _UUID_OTHER, "label": "Fixer"},
            {"id": _EDGE_A, "label": "Edge A"},
            {"id": _EDGE_B, "label": "Edge B"},
        ],
        "edges": edges,
    }


# (case, gate config, reject edges, expected reject target)
_AGREEMENT_MATRIX = [
    ("config-only", {"reject_target": _UUID_OTHER}, [], _UUID_OTHER),
    (
        "edge-only",
        {},
        [{"source": _UUID_SRC, "target": _EDGE_A, "type": "reject"}],
        _EDGE_A,
    ),
    (
        "both",
        {"reject_target": _UUID_OTHER},
        [{"source": _UUID_SRC, "target": _EDGE_A, "type": "reject"}],
        _UUID_OTHER,
    ),
    ("neither", {}, [], None),
    (
        "multiple reject edges last-wins",
        {},
        [
            {"source": _UUID_SRC, "target": _EDGE_A, "type": "reject"},
            {"source": _UUID_SRC, "target": _EDGE_B, "type": "reject"},
        ],
        _EDGE_B,
    ),
    (
        "foreign-source ignored",
        {},
        [{"source": _UUID_OTHER, "target": _EDGE_A, "type": "reject"}],
        None,
    ),
]


class TestRejectConsequenceAgreement:
    """FAR-1486 tripwire: the briefing, the compiler and the reject-edge map
    must resolve the SAME reject route — or the same absence of one."""

    @pytest.mark.parametrize(
        ("case", "config", "reject_edges", "expected"),
        _AGREEMENT_MATRIX,
        ids=[row[0] for row in _AGREEMENT_MATRIX],
    )
    def test_resolver_agrees_with_compiler_and_reject_edge_map(
        self,
        case: str,
        config: dict[str, Any],
        reject_edges: list[dict[str, Any]],
        expected: str | None,
    ) -> None:
        """The reviewer briefing must show the reject route the compiled graph
        ACTUALLY takes — not merely a plausible one.

        Three independent readers of the same fixture must agree:
        ``_resolve_consequences`` (briefing), the real compiler wiring
        (``_add_hitl_review_edge`` + its router), and
        ``graph_cache._build_reject_targets`` (the edge contribution).
        """
        del case  # ids only — the case name is in the parametrize ids
        edges = [_gate_edge(config), *reject_edges]
        graph = _graph_with(edges)

        resolved = _resolved_reject(graph, config)
        compiled, compiled_graph = _compile_gate(edges, config)

        assert resolved == expected
        assert compiled == expected
        if expected is None:
            # No reject route wired at all: the gate falls through to the
            # plain normal edge (run continues), there is NO router.
            assert not compiled_graph.conditional
            assert compiled_graph.plain_edges == [(_UUID_SRC, _REVIEW_ID), (_REVIEW_ID, _UUID_TGT)]
        else:
            assert compiled_graph.conditional

        # With no config value, the resolver must adopt EXACTLY what the
        # compiler's per-source reject-edge map holds (dict build → last wins).
        if config.get("reject_target") is None:
            assert resolved == graph_cache._build_reject_targets(edges).get(_UUID_SRC)

    def test_falsy_reject_target_yields_no_reject_consequence(self) -> None:
        """FAR-1486 F8a: a falsy config ``reject_target`` must NOT become a
        phantom ``{"node_id": "False"}`` / ``{"node_id": ""}`` consequence.

        The compiler treats it as absent for WIRING (``if reject_target:``)
        while its ``is None`` check still blocks the reject-edge fallback, so
        the compiled gate has no reject route at all — the briefing agrees.
        """
        reject_edges = [{"source": _UUID_SRC, "target": _EDGE_A, "type": "reject"}]
        graph = _graph_with([_gate_edge({"reject_target": ""}), *reject_edges])
        assert _resolved_reject(graph, {"reject_target": ""}) is None

        graph = _graph_with([_gate_edge({"reject_target": False}), *reject_edges])
        assert _resolved_reject(graph, {"reject_target": False}) is None

        compiled, compiled_graph = _compile_gate(reject_edges, {"reject_target": ""})
        assert compiled is None
        assert not compiled_graph.conditional

    def test_source_node_id_fallback_when_approve_edge_is_not_matched(self) -> None:
        """No edge carries this review_id (node gate / snapshot drift): the
        reject route still resolves from the gate's ``source_node_id``."""
        reject_edges = [{"source": _UUID_SRC, "target": _EDGE_A, "type": "reject"}]
        graph = _graph_with(reject_edges)

        consequences = _resolve_consequences(graph, _REVIEW_ID, {}, _UUID_SRC)
        assert consequences is not None
        assert "approve" not in consequences
        assert consequences["reject"]["node_id"] == _EDGE_A
        assert _resolve_consequences(graph, _REVIEW_ID, {}, None) is None

    def test_legacy_edge_keys_resolve_like_canonical_ones(self) -> None:
        """Legacy persisted edge shapes (``edge_type`` +
        ``source_node_id``/``target_node_id``) resolve identically to the
        canonical keys — for the briefing AND the compiler."""
        config = {"description": "Approve."}
        edges = [
            {
                "source_node_id": _UUID_SRC,
                "target_node_id": _UUID_TGT,
                "edge_type": "normal",
                "hitl_review_config": config,
            },
            {"source_node_id": _UUID_SRC, "target_node_id": _EDGE_A, "edge_type": "reject"},
        ]
        graph = _graph_with(edges)

        consequences = _resolve_consequences(graph, _REVIEW_ID, config, _UUID_SRC)
        assert consequences is not None
        assert consequences["approve"]["node_id"] == _UUID_TGT
        assert consequences["reject"]["node_id"] == _EDGE_A

        compiled, _compiled_graph = _compile_gate(edges[1:], config)
        assert compiled == _EDGE_A
        assert graph_cache._build_reject_targets(edges[1:]) == {_UUID_SRC: _EDGE_A}

    @pytest.mark.parametrize("bad_edges", [None, 42], ids=["null", "scalar"])
    async def test_malformed_snapshot_edges_degrade_without_nulling_briefing(
        self,
        bad_edges: Any,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """FAR-1486 F8b: a snapshot whose ``edges`` is not a list must degrade
        to no consequences (WITH a log) — never raise out and null the whole
        briefing."""
        graph = {"nodes": [{"id": _UUID_SRC, "label": "Generator"}], "edges": bad_edges}
        with caplog.at_level(logging.WARNING):
            context = await _build(graph)
        assert context is not None
        assert context["consequences"] is None
        assert context["trigger"] == "unknown"
        assert context["source_node_id"] == _UUID_SRC
        assert "hitl_review.malformed_snapshot_edges" in caplog.text

    async def test_resolver_failure_degrades_to_no_consequences_with_log(
        self,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """FAR-1486 F8b: whatever makes the resolver raise, the CALL SITE must
        degrade to ``consequences = None`` WITH a logged warning — if the
        exception escaped, the outer capture guard would null the ENTIRE
        briefing (every other field lost) instead of this one enrichment."""
        config = {"description": "Approve the comments."}
        graph = _graph_with([_gate_edge(config)])

        def _boom(*_args: Any, **_kwargs: Any) -> dict[str, Any]:
            msg = "malformed snapshot graph"
            raise RuntimeError(msg)

        with (
            patch("modulo.core.pipeline_engine.hitl_context._resolve_consequences", _boom),
            caplog.at_level(logging.WARNING),
        ):
            context = await _build(graph)

        assert context is not None
        assert context["consequences"] is None
        assert context["description"] == "Approve the comments."
        assert context["trigger"] == "condition"
        assert context["source_node_id"] == _UUID_SRC
        assert "hitl_review.consequences_resolution_failed" in caplog.text
