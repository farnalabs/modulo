"""Unit tests for the HITL gate reject→correction dispatch seam (FAR-210 follow-up).

The ``_hitl_gate`` node builder (``node_runner.make_hitl_gate_fn``) is where a
HITL reject decision lands during a graph resume. When the gate config declares
a ``correction_target`` and the human REJECTED the gate, the node dispatches the
single-node correction for the blocked node via
``FeedbackManager.dispatch_reject_correction`` (the automated reject→correction
edge). Gates WITHOUT a ``correction_target`` keep the existing behaviour — they
kick back to the plain ``reject_target`` with no correction dispatch.
"""

import uuid
from unittest.mock import AsyncMock, patch

import pytest

from modulo.core.pipeline_engine.node_runner import make_hitl_gate_fn


def _gate_fn(correction_target: str | None = None, *, org_id: uuid.UUID | None = None):
    config: dict = {
        "gate_id": "hitl_gate_a_b",
        "label": "review",
        "description": "",
        "human_only": True,
        "claim_expiry_minutes": 60,
        "reject_target": str(uuid.uuid4()),
    }
    if correction_target is not None:
        config["correction_target"] = correction_target
    return make_hitl_gate_fn(config, session_factory=AsyncMock(), org_id=org_id or uuid.uuid4())


def _state(**overrides: dict) -> dict:
    state: dict = {
        "_hitl_decision": {"action": "rejected", "gate_id": "hitl_gate_a_b", "reason": "secret detected"},
        "_run_id": uuid.uuid4(),
        "output": {"body": "secret: hunter2"},
    }
    state.update(overrides)
    return state


@pytest.mark.asyncio
async def test_reject_with_correction_target_dispatches_correction():
    """A rejected gate with a ``correction_target`` dispatches the correction."""
    target = str(uuid.uuid4())
    gate = _gate_fn(correction_target=target)
    with patch(
        "modulo.core.feedback_manager.dispatch_reject_correction",
        new=AsyncMock(return_value={"verdict": "resolved"}),
    ) as dispatch:
        result = await gate(_state())

    dispatch.assert_awaited_once()
    kwargs = dispatch.await_args.kwargs
    assert str(kwargs["node_id"]) == target
    assert kwargs["node_input"] == {"body": "secret: hunter2"}
    assert kwargs["rejection_reason"] == "secret detected"
    # The gate still reports the rejection downstream (correction is a side path).
    assert result["artifacts"][0]["result"] == "rejected"


@pytest.mark.asyncio
async def test_reject_without_correction_target_does_not_dispatch():
    """A rejected gate WITHOUT a ``correction_target`` never dispatches a correction."""
    gate = _gate_fn(correction_target=None)
    with patch(
        "modulo.core.feedback_manager.dispatch_reject_correction",
        new=AsyncMock(return_value=None),
    ) as dispatch:
        result = await gate(_state())

    dispatch.assert_not_awaited()
    assert result["artifacts"][0]["result"] == "rejected"


@pytest.mark.asyncio
async def test_approve_with_correction_target_does_not_dispatch():
    """Approving a gate (even with a ``correction_target``) never dispatches a correction."""
    gate = _gate_fn(correction_target=str(uuid.uuid4()))
    with patch(
        "modulo.core.feedback_manager.dispatch_reject_correction",
        new=AsyncMock(return_value=None),
    ) as dispatch:
        result = await gate(_state(_hitl_decision={"action": "approved", "gate_id": "hitl_gate_a_b"}))

    dispatch.assert_not_awaited()
    assert result["artifacts"][0]["result"] == "approved"


@pytest.mark.asyncio
async def test_correction_dispatch_failure_does_not_crash_reject_path():
    """A correction dispatch failure is failure-isolated — the reject path survives."""
    gate = _gate_fn(correction_target=str(uuid.uuid4()))
    with patch(
        "modulo.core.feedback_manager.dispatch_reject_correction",
        new=AsyncMock(side_effect=RuntimeError("boom")),
    ):
        result = await gate(_state())

    assert result["artifacts"][0]["result"] == "rejected"
