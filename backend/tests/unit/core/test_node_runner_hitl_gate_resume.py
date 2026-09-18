"""Unit tests for the HITL gate resume seam (``_hitl_gate_resume_result``).

FAR-541: the gate node must fail closed on an action-less decision AND on a
decision not stamped for THIS gate. Only recognized actions stamped with the
gate's own id resume — ``approved`` (the approve-with-modification API
submits ``approved`` plus a ``modified_output`` member, so it is covered by
``approved``), ``rejected``, and ``deliver_manual``. Anything else (an empty
``{}``, an unknown action value, a non-dict decision, a decision stamped for
a DIFFERENT gate) is ignored — the gate falls through to the
condition/eval/autonomy path and re-interrupts rather than treating the
malformed or foreign decision as an approval.

All DB-adjacent collaborators are inert here: the approve/reject path calls
``_dispatch_reject_correction_best_effort`` with ``session_factory=None`` /
``org_id=None``, which no-ops.
"""

from __future__ import annotations

import logging
from typing import Any

import pytest

import modulo.core.pipeline_engine.node_runner as nr

_GATE_ID = "gate_a_b"
_FOREIGN_GATE_ID = "gate_c_d"


def _resume(decision: Any) -> tuple[bool, dict[str, Any] | None]:
    return nr._hitl_gate_resume_result(
        decision,
        _GATE_ID,
        state={},
        hitl_gate_config={},
        session_factory=None,
        org_id=None,
    )


# --- Fail-closed: malformed decisions must never resume (FAR-541) ---------


async def test_none_decision_does_not_resume() -> None:
    """First invocation (no decision in state) -> fall through, not resumed."""
    resumed, result = await _resume(None)
    assert resumed is False
    assert result is None


async def test_empty_dict_decision_fails_closed() -> None:
    """THE FIX (FAR-541): an empty ``{}`` decision (injected by an
    empty-resume dispatch) is NOT an approval — the gate fails closed."""
    resumed, result = await _resume({})
    assert resumed is False
    assert result is None


async def test_unknown_action_fails_closed() -> None:
    """An unrecognized action value is ignored, never treated as an approval."""
    resumed, result = await _resume({"action": "weird", "gate_id": _GATE_ID})
    assert resumed is False
    assert result is None


async def test_missing_action_fails_closed() -> None:
    """A dict without an ``action`` member carries no human verdict."""
    resumed, result = await _resume({"notes": "just notes", "gate_id": _GATE_ID})
    assert resumed is False
    assert result is None


async def test_non_dict_decision_fails_closed() -> None:
    """A non-dict decision (e.g. a bare string) is not a human verdict — the
    old code treated any non-None non-deliver_manual decision as approved."""
    resumed, result = await _resume("approved")
    assert resumed is False
    assert result is None


async def test_malformed_decision_logs_warning(caplog: pytest.LogCaptureFixture) -> None:
    """The fail-closed branch emits ``hitl_gate.malformed_decision_ignored``
    with the gate id and the decision's type/action (never payload content)."""
    with caplog.at_level(logging.WARNING, logger="modulo.core.pipeline_engine.node_runner"):
        await _resume({"action": "weird", "gate_id": _GATE_ID, "secret_payload": "do-not-log"})
    records = [r for r in caplog.records if r.getMessage() == "hitl_gate.malformed_decision_ignored"]
    assert len(records) == 1
    assert records[0].gate_id == _GATE_ID
    assert records[0].decision_type == "dict"
    assert records[0].action == "weird"


# --- Fail-closed: foreign decisions must never resume (FAR-541 iteration 2) --


async def test_foreign_stamped_decision_does_not_resume() -> None:
    """THE C1 REGRESSION (FAR-541 iteration 2): a decision stamped for a
    DIFFERENT gate (decided at gate A, replayed at gate B by a stale-state
    resume) must NOT resolve gate B — decisions are per-RUN but consumers are
    per-gate, and ``_hitl_decision`` is never cleared from state."""
    resumed, result = await _resume({"action": "approved", "gate_id": _FOREIGN_GATE_ID})
    assert resumed is False
    assert result is None


async def test_foreign_rejected_decision_does_not_resume() -> None:
    """A foreign REJECTION is equally inert — it must not kick gate B to its
    reject target nor dispatch a reject correction for a decision B never
    received."""
    resumed, result = await _resume({"action": "rejected", "gate_id": _FOREIGN_GATE_ID, "reason": "gate A"})
    assert resumed is False
    assert result is None


async def test_unstamped_decision_does_not_resume() -> None:
    """A missing stamp fails closed: every live writer (API routes, MCP,
    dispatcher reconcile) stamps the gate id — an unstamped decision is not a
    verdict for this gate."""
    resumed, result = await _resume({"action": "approved"})
    assert resumed is False
    assert result is None


async def test_foreign_decision_logs_warning_without_payload(caplog: pytest.LogCaptureFixture) -> None:
    """``hitl_gate.foreign_decision_ignored`` carries the gate id and the
    decision's stamped gate id ONLY — never payload content."""
    with caplog.at_level(logging.WARNING, logger="modulo.core.pipeline_engine.node_runner"):
        await _resume({"action": "approved", "gate_id": _FOREIGN_GATE_ID, "notes": "do-not-log-me", "output": {"x": 1}})
    records = [r for r in caplog.records if r.getMessage() == "hitl_gate.foreign_decision_ignored"]
    assert len(records) == 1
    assert records[0].gate_id == _GATE_ID
    assert records[0].decision_gate_id == _FOREIGN_GATE_ID
    assert "do-not-log-me" not in caplog.text
    assert "output" not in caplog.text


# --- Recognized actions resume ---------------------------------------------


async def test_approved_decision_resumes() -> None:
    resumed, result = await _resume({"action": "approved", "gate_id": _GATE_ID})
    assert resumed is True
    assert result is not None
    artifact = result["artifacts"][0]
    assert artifact["node_id"] == _GATE_ID
    assert artifact["result"] == "approved"


async def test_approved_with_modification_resumes_with_output() -> None:
    """The approve-with-modification API submits ``approved`` plus
    ``modified_output`` — the modification flows into the run output."""
    modified = {"pr_title": "human edited"}
    resumed, result = await _resume({"action": "approved", "gate_id": _GATE_ID, "modified_output": modified})
    assert resumed is True
    assert result is not None
    assert result["output"] == modified


async def test_rejected_decision_resumes() -> None:
    resumed, result = await _resume({"action": "rejected", "gate_id": _GATE_ID, "reason": "not good enough"})
    assert resumed is True
    assert result is not None
    assert result["artifacts"][0]["result"] == "rejected"


async def test_deliver_manual_decision_resumes() -> None:
    manual_output = {"answer": 42}
    resumed, result = await _resume({"action": "deliver_manual", "gate_id": _GATE_ID, "output": manual_output})
    assert resumed is True
    assert result is not None
    assert result["artifacts"][0]["result"] == "delivered_manual"
    assert result["output"] == manual_output
