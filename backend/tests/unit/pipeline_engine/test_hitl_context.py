"""Unit tests for FAR-613 fire-time HITL gate context capture.

Covers ``build_hitl_gate_context`` and its helpers: the briefing bundle
(shape, trigger kinds, condition/node-id extraction, bounded artifacts,
reason extraction, determinism), the failure-isolation contract (a capture
defect never raises — it yields ``None``), and the legacy-snapshot fallback
(minimal bundle when the gate config cannot be resolved).
"""

import json
import uuid
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from modulo.core.pipeline_engine.hitl_context import (
    ARTIFACTS_BUDGET_CHARS,
    REASON_ABSENT,
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
        minimal briefing (source node + pipeline name), never an empty dict."""
        graph = {"nodes": [{"id": _UUID_SRC, "label": "Comment Generator"}], "edges": []}
        context = await _build(graph)
        assert context is not None
        assert context["description"] is None
        assert context["trigger"] == "condition"
        assert context["source_node_id"] == _UUID_SRC
        assert context["pipeline_name"] == "PR Reviewer"

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

    async def test_deterministic_capture_same_inputs_same_bundle(self):
        config = {"description": "Approve the deploy.", "condition": f"node_id=='{_UUID_OTHER}'"}
        graph = _edge_graph(config)
        outputs = {_UUID_OTHER: {"n": 1, "blob": "y" * 5000}}
        first = await _build(graph, completed_node_outputs=outputs)
        second = await _build(graph, completed_node_outputs=outputs)
        assert json.dumps(first, sort_keys=True) == json.dumps(second, sort_keys=True)


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
