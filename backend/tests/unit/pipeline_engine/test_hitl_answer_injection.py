"""Unit tests for FAR-860 answer injection and context capture.

Covers ``_inject_answer_state`` (state key injection) and the
``response_contract`` capture in ``HitlReviewContext``.
"""

import uuid
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

from modulo.core.pipeline_engine import node_runner
from modulo.core.pipeline_engine.hitl_context import (
    build_hitl_review_context,
)
from modulo.core.pipeline_engine.node_runner import (
    HITL_REVIEW_ANSWER_STATE_KEY_PREFIX,
    _inject_answer_state,
)

_UUID_SRC = "550e8400-e29b-41d4-a716-446655440000"
_UUID_TGT = "660e8400-e29b-41d4-a716-446655440001"
_REVIEW_ID = f"hitl_review_{_UUID_SRC}_{_UUID_TGT}"
_ORG_ID = uuid.uuid4()
_RUN_ID = uuid.uuid4()


class TestInjectAnswerState:
    def test_injects_choice_answer(self):
        decision = {"action": "approved", "review_id": _REVIEW_ID, "answer": {"kind": "choice", "option_id": "yes"}}
        gate_result: dict[str, Any] = {"artifacts": []}
        _inject_answer_state(_REVIEW_ID, decision, gate_result)
        key = f"{HITL_REVIEW_ANSWER_STATE_KEY_PREFIX}{_REVIEW_ID}"
        assert gate_result[key] == "yes"

    def test_transition_shim_skips_duplicate_write_when_prefixes_coincide(self, monkeypatch):
        """FAR-1104 transition shim: the legacy key is written only when it
        differs from the current key. When the two prefixes coincide (a future
        rename reusing the old spelling) the guard must skip the duplicate
        write rather than overwrite the same key twice."""
        monkeypatch.setattr(node_runner, "HITL_REVIEW_ANSWER_STATE_KEY_PREFIX", "hitl_answer_")
        decision = {"action": "approved", "review_id": _REVIEW_ID, "answer": {"kind": "choice", "option_id": "yes"}}
        gate_result: dict[str, Any] = {}
        _inject_answer_state(_REVIEW_ID, decision, gate_result)
        assert gate_result == {f"hitl_answer_{_REVIEW_ID}": "yes"}

    def test_no_answer_no_injection(self):
        decision = {"action": "approved", "review_id": _REVIEW_ID}
        gate_result: dict[str, Any] = {"artifacts": []}
        _inject_answer_state(_REVIEW_ID, decision, gate_result)
        key = f"{HITL_REVIEW_ANSWER_STATE_KEY_PREFIX}{_REVIEW_ID}"
        assert key not in gate_result

    def test_non_dict_decision_no_injection(self):
        gate_result: dict[str, Any] = {"artifacts": []}
        _inject_answer_state(_REVIEW_ID, "not_a_dict", gate_result)
        key = f"{HITL_REVIEW_ANSWER_STATE_KEY_PREFIX}{_REVIEW_ID}"
        assert key not in gate_result

    def test_empty_option_id_no_injection(self):
        decision = {"action": "approved", "review_id": _REVIEW_ID, "answer": {"kind": "choice", "option_id": ""}}
        gate_result: dict[str, Any] = {"artifacts": []}
        _inject_answer_state(_REVIEW_ID, decision, gate_result)
        key = f"{HITL_REVIEW_ANSWER_STATE_KEY_PREFIX}{_REVIEW_ID}"
        assert key not in gate_result

    def test_approval_answer_no_injection(self):
        decision = {"action": "approved", "review_id": _REVIEW_ID, "answer": {"kind": "approval"}}
        gate_result: dict[str, Any] = {"artifacts": []}
        _inject_answer_state(_REVIEW_ID, decision, gate_result)
        key = f"{HITL_REVIEW_ANSWER_STATE_KEY_PREFIX}{_REVIEW_ID}"
        assert key not in gate_result


def _make_session(graph_json: dict[str, Any] | None) -> AsyncMock:
    session = AsyncMock()
    execute_result = MagicMock()
    execute_result.scalar_one_or_none.return_value = graph_json
    session.execute = AsyncMock(return_value=execute_result)
    return session


def _run_mock(snapshot_id: uuid.UUID | None) -> MagicMock:
    run = MagicMock()
    run.snapshot_id = snapshot_id
    return run


def _edge_graph_with_rc(rc: dict[str, Any] | None = None) -> dict[str, Any]:
    config: dict[str, Any] = {"label": "Review gate", "description": "Approve or reject the deploy plan."}
    if rc is not None:
        config["response_contract"] = rc
    return {
        "nodes": [{"id": _UUID_SRC, "label": "Comment Generator"}, {"id": _UUID_TGT}],
        "edges": [{"source": _UUID_SRC, "target": _UUID_TGT, "type": "normal", "hitl_review_config": config}],
    }


async def _build(graph_json: dict[str, Any]) -> dict[str, Any] | None:
    session = _make_session(graph_json)
    snapshot_id = uuid.uuid4()
    with patch(
        "modulo.core.pipeline_engine.hitl_context.get_run",
        new_callable=AsyncMock,
        return_value=_run_mock(snapshot_id),
    ):
        return await build_hitl_review_context(
            session,
            run_id=_RUN_ID,
            review_id=_REVIEW_ID,
            org_id=_ORG_ID,
            pipeline_name="Test Pipeline",
            completed_node_outputs=None,
        )


class TestHitlReviewContextResponseContract:
    async def test_response_contract_captured(self):
        rc = {
            "kind": "choice",
            "options": [
                {"id": "approve", "label": "Approve"},
                {"id": "reject", "label": "Reject"},
            ],
        }
        ctx = await _build(_edge_graph_with_rc(rc))
        assert ctx is not None
        assert ctx["response_contract"] == rc

    async def test_no_response_contract(self):
        ctx = await _build(_edge_graph_with_rc(None))
        assert ctx is not None
        assert ctx.get("response_contract") is None

    async def test_approval_contract_captured(self):
        rc = {"kind": "approval"}
        ctx = await _build(_edge_graph_with_rc(rc))
        assert ctx is not None
        assert ctx["response_contract"] == rc
