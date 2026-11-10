"""FAR-2490(3): a HUMAN-supplied gate output is never scored as agent output.

``deliver_manual`` injects the human's payload into run state as ``output``.
That payload must never be treated as an agent's produced artifact:

* a HITL gate node is SYNTHETIC — its id is not in ``ctx.node_ids`` — so
  ``_handle_chain_end_event`` returns before capturing the gate's output as a
  node output and before the post-node eval seam runs. A model-judge eval bound
  to a real node therefore never scores the human's value; and
* the human decision itself never produces an ``EvalResult`` (the resolver
  returns ``eval_result_id=None``, pinned in
  ``tests/unit/core/eval_engine/test_policy_gate.py``).

This module pins the executor-side half: the skip is load-bearing, not
incidental. If a future change starts walking synthetic gate nodes through the
capture/eval path, a model judge could score human-supplied data as agent
performance — this test fails first.
"""

from __future__ import annotations

import uuid
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import modulo.core.pipeline_engine.executor as ex
from modulo.core.pipeline_engine.node_runner import (
    HITL_REVIEW_OUTPUT_PROVENANCE_STATE_KEY_PREFIX,
    hitl_output_provenance_marker,
)

_GATE_NODE_ID = "hitl_review_agent_node_next_node"
_REAL_NODE_ID = "agent_node"


def _executor() -> Any:
    """An executor instance without running ``__init__`` (the branch under test
    touches no instance state)."""
    executor = ex.PipelineExecutor.__new__(ex.PipelineExecutor)
    executor._otel_bridge = MagicMock()
    return executor


def _context(*, node_ids: set[str], eval_definitions_by_node: dict[str, Any] | None = None) -> ex._StreamContext:
    return ex._StreamContext(
        node_ids=node_ids,
        guard=None,
        completed_node_outputs={},
        run_trace_id=None,
        broker=MagicMock(),
        run_id=uuid.uuid4(),
        pipeline_id=None,
        org_id=None,
        node_token_budgets=None,
        eval_definitions_by_node=eval_definitions_by_node or {},
        node_type_map=None,
    )


def _gate_event() -> dict[str, Any]:
    """The ``on_chain_end`` event a resumed ``deliver_manual`` gate emits."""
    manual_output = {"answer": 42}
    # The real marker shape (FAR-2489), built with its own writer so this
    # fixture cannot drift from the shipped contract.
    provenance_key = f"{HITL_REVIEW_OUTPUT_PROVENANCE_STATE_KEY_PREFIX}{_GATE_NODE_ID}"
    return {
        "event": "on_chain_end",
        "name": _GATE_NODE_ID,
        "data": {
            "output": {
                "artifacts": [
                    {
                        "node_id": _GATE_NODE_ID,
                        "status": "interrupted",
                        "result": "delivered_manual",
                        "manual_output": manual_output,
                    }
                ],
                "output": manual_output,
                provenance_key: hitl_output_provenance_marker(_GATE_NODE_ID, manual_output),
            }
        },
    }


async def test_gate_output_is_never_captured_or_evaluated() -> None:
    """THE REGRESSION: a synthetic gate node's human-supplied output must not be
    captured into ``completed_node_outputs`` nor reach the post-node eval seam."""
    ctx = _context(node_ids={_REAL_NODE_ID})
    state = ex._StreamState()
    evaluate = AsyncMock()
    capture = MagicMock()

    with (
        patch.object(ex.PipelineExecutor, "_intercept_node_output_and_run_evals", new=evaluate),
        patch.object(ex, "_record_chain_end_output", new=capture),
    ):
        await _executor()._handle_chain_end_event(state=state, ctx=ctx, lg_event=_gate_event())

    assert not ctx.completed_node_outputs
    evaluate.assert_not_awaited()
    capture.assert_not_called()
    # Returning before the segment counter proves the skip is at the top of the
    # handler, not somewhere later that a future edit could reorder.
    assert state.segments_completed == 0


async def test_a_real_node_is_still_captured_and_evaluated() -> None:
    """The negative control: the skip is specific to synthetic gate ids. A real
    node id still reaches capture and the post-node eval seam — so the test
    above cannot pass vacuously."""
    ctx = _context(node_ids={_REAL_NODE_ID}, eval_definitions_by_node={_REAL_NODE_ID: [object()]})
    state = ex._StreamState()
    evaluate = AsyncMock(return_value={"output": {"from": "agent"}})
    capture = MagicMock(return_value={"from": "agent"})
    event = {
        "event": "on_chain_end",
        "name": _REAL_NODE_ID,
        "data": {"output": {"output": {"from": "agent"}}},
    }

    with (
        patch.object(ex.PipelineExecutor, "_intercept_node_output_and_run_evals", new=evaluate),
        patch.object(ex, "_record_chain_end_output", new=capture),
    ):
        await _executor()._handle_chain_end_event(state=state, ctx=ctx, lg_event=event)

    capture.assert_called_once()
    evaluate.assert_awaited_once()
    assert state.segments_completed == 1
