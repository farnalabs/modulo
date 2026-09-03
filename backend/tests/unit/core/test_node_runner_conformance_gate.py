"""Unit tests for the FAR-215 node-start conformance gate (node_runner).

Covers the node_runner seam ``_run_conformance_gate``:
  - no conformance ctx -> fast path (no DB, no interrupt)
  - resume of THIS node's conformance block (per-node marker) with ``approved``
    -> documented override, marker cleared, node continues
  - resume with ``rejected`` -> the run FAILS CLOSED (``GuardrailBlockedError``),
    the node never executes with the blocked capability
  - a foreign resume decision (``_hitl_decision`` from a different gate, no
    marker) never skips the live check -> block still blocks (fail-closed)
  - block-action absent/unknown -> audit + interrupt (never fail open)
  - warn/observe advisory -> audit only, continue (never blocks)
  - present -> continue with no audit
  - audit appends are summary-only payloads and failure-isolated (never raise)

All DB access is mocked; the seam is exercised with real decision objects.
"""

from __future__ import annotations

import uuid
from typing import Any, Self
from unittest.mock import AsyncMock, MagicMock

import pytest

import modulo.core.pipeline_engine.node_runner as nr
from modulo.core.guardrails.conformance import ConformanceRecheckResult

_ORG_ID = uuid.uuid4()
_PIPE_ID = uuid.uuid4()
_NODE_ID = "node-1"
_RUN_ID = uuid.uuid4()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


class _FakeSession:
    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, *exc: object) -> bool:
        return False

    def begin(self) -> Self:
        return self


def _fake_factory() -> Any:
    def _factory() -> _FakeSession:
        return _FakeSession()

    return _factory


def _set_ctx(
    monkeypatch: pytest.MonkeyPatch,
    *,
    session_factory: Any = None,
    org_id: Any = _ORG_ID,
    env_profile: Any = None,
    pipeline_id: Any = _PIPE_ID,
    claimed_guardrails: list[Any] | None = None,
    claims_load_failed: bool = False,
) -> None:
    ctx = (
        session_factory or _fake_factory(),
        org_id,
        env_profile,
        pipeline_id,
        claimed_guardrails,
        claims_load_failed,
    )
    monkeypatch.setattr(nr, "get_conformance_ctx", lambda: ctx)


def _patch_check_node_start(monkeypatch: pytest.MonkeyPatch, result: ConformanceRecheckResult | None) -> AsyncMock:
    import modulo.core.guardrails.conformance as conf

    mock = AsyncMock()
    if result is not None:
        mock.return_value = result
    monkeypatch.setattr(conf, "check_node_start", mock)
    return mock


def _patch_audit(monkeypatch: pytest.MonkeyPatch) -> AsyncMock:
    audit = AsyncMock()
    monkeypatch.setattr(nr, "_append_conformance_audit", audit)
    return audit


def _patch_interrupt(monkeypatch: pytest.MonkeyPatch) -> MagicMock:
    interrupt = MagicMock()
    monkeypatch.setattr(nr, "interrupt", interrupt)
    return interrupt


# ---------------------------------------------------------------------------
# Fast path: no conformance context
# ---------------------------------------------------------------------------


async def test_gate_no_ctx_fast_path(monkeypatch: pytest.MonkeyPatch):
    """No run-scoped conformance ctx -> continue without touching anything."""
    monkeypatch.setattr(nr, "get_conformance_ctx", lambda: None)
    check = _patch_check_node_start(monkeypatch, None)
    audit = _patch_audit(monkeypatch)
    interrupt = _patch_interrupt(monkeypatch)

    blocked = await nr._run_conformance_gate({}, node_id=_NODE_ID)

    assert blocked is False
    check.assert_not_awaited()
    audit.assert_not_awaited()
    interrupt.assert_not_called()


# ---------------------------------------------------------------------------
# Resume after a human reviewed the conformance block
# ---------------------------------------------------------------------------


async def test_gate_resume_approved_clears_marker_and_continues(monkeypatch: pytest.MonkeyPatch):
    """Resume of THIS node's conformance block with an ``approved`` decision
    (stamped for the block's gate) is the documented human override: the
    markers are cleared and the node continues WITHOUT re-running the live
    check."""
    _set_ctx(monkeypatch)
    check = _patch_check_node_start(monkeypatch, None)
    audit = _patch_audit(monkeypatch)
    interrupt = _patch_interrupt(monkeypatch)
    state = {
        "_conformance_blocked_node": _NODE_ID,
        "_conformance_blocked_gate": "guardrail_conformance_g_block",
        "_hitl_decision": {"action": "approved", "gate_id": "guardrail_conformance_g_block"},
    }

    blocked = await nr._run_conformance_gate(state, node_id=_NODE_ID)

    assert blocked is False
    assert state["_conformance_blocked_node"] is None
    assert state["_conformance_blocked_gate"] is None
    check.assert_not_awaited()
    audit.assert_not_awaited()
    interrupt.assert_not_called()


async def test_gate_resume_approved_stamped_with_node_id_clears_marker(monkeypatch: pytest.MonkeyPatch):
    """FAR-541: a decision stamped with the blocked NODE id (not the guardrail
    gate id) is equally valid — the block's consumer accepts either stamp."""
    _set_ctx(monkeypatch)
    check = _patch_check_node_start(monkeypatch, None)
    _patch_audit(monkeypatch)
    interrupt = _patch_interrupt(monkeypatch)
    state = {
        "_conformance_blocked_node": _NODE_ID,
        "_conformance_blocked_gate": "guardrail_conformance_g_block",
        "_hitl_decision": {"action": "approved", "gate_id": _NODE_ID},
    }

    blocked = await nr._run_conformance_gate(state, node_id=_NODE_ID)

    assert blocked is False
    assert state["_conformance_blocked_node"] is None
    check.assert_not_awaited()
    interrupt.assert_not_called()


async def test_gate_resume_rejected_fails_closed(monkeypatch: pytest.MonkeyPatch):
    """Rejecting THIS node's conformance block FAILS CLOSED: the node must not
    execute with the capability the guardrail protected, so the run is denied
    (``GuardrailBlockedError`` -> terminal ``eval_failed``), never resumed."""
    import modulo.core.guardrails as gr

    _set_ctx(monkeypatch)
    check = _patch_check_node_start(monkeypatch, None)
    audit = _patch_audit(monkeypatch)
    interrupt = _patch_interrupt(monkeypatch)
    state = {
        "_conformance_blocked_node": _NODE_ID,
        "_conformance_blocked_gate": "guardrail_conformance_g_block",
        "_hitl_decision": {
            "action": "rejected",
            "gate_id": "guardrail_conformance_g_block",
            "reason": "capability genuinely revoked",
        },
    }

    with pytest.raises(gr.GuardrailBlockedError):
        await nr._run_conformance_gate(state, node_id=_NODE_ID)

    check.assert_not_awaited()
    audit.assert_not_awaited()
    interrupt.assert_not_called()


# ---------------------------------------------------------------------------
# Operator break-glass via recover-node (FAR-541 iteration 3, FIX 3 + FIX 4)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("action", ["skip", "replay"])
async def test_gate_resume_operator_break_glass_clears_marker(monkeypatch: pytest.MonkeyPatch, action: str):
    """FAR-541 FIX 3: recover-node skip/replay is the documented operator
    break-glass for a conformance block — the recover route stamps the recovery
    payload with the pending claim row's gate id (the block's own guardrail
    gate id) and the consumer honours it: the markers are cleared so the node
    re-runs."""
    _set_ctx(monkeypatch)
    check = _patch_check_node_start(monkeypatch, None)
    audit = _patch_audit(monkeypatch)
    interrupt = _patch_interrupt(monkeypatch)
    state = {
        "_conformance_blocked_node": _NODE_ID,
        "_conformance_blocked_gate": "guardrail_conformance_g_block",
        "_hitl_decision": {"action": action, "gate_id": "guardrail_conformance_g_block"},
    }

    blocked = await nr._run_conformance_gate(state, node_id=_NODE_ID)

    assert blocked is False
    assert state["_conformance_blocked_node"] is None
    assert state["_conformance_blocked_gate"] is None
    check.assert_not_awaited()
    audit.assert_not_awaited()
    interrupt.assert_not_called()


@pytest.mark.parametrize("action", ["manual_output", "weird", None])
async def test_gate_resume_unknown_override_action_fails_closed(monkeypatch: pytest.MonkeyPatch, action: str | None):
    """FAR-541 FIX 4: the override is restricted to the recognized allowlist
    (``approved``/``deliver_manual``/``skip``/``replay``) — any other stamped
    action (a ``manual_output`` decision meant for a manual node, garbage, or a
    missing action) does NOT clear the block: warn + re-interrupt (the block
    stands)."""
    _set_ctx(monkeypatch)
    check = _patch_check_node_start(monkeypatch, None)
    audit = _patch_audit(monkeypatch)
    interrupt = _patch_interrupt(monkeypatch)
    state = {
        "_conformance_blocked_node": _NODE_ID,
        "_conformance_blocked_gate": "guardrail_conformance_g_block",
        "_hitl_decision": {"action": action, "gate_id": "guardrail_conformance_g_block", "output": {"a": 1}},
    }

    blocked = await nr._run_conformance_gate(state, node_id=_NODE_ID)

    assert blocked is True
    assert state["_conformance_blocked_node"] == _NODE_ID
    assert state["_conformance_blocked_gate"] == "guardrail_conformance_g_block"
    check.assert_not_awaited()
    audit.assert_not_awaited()
    interrupt.assert_called_once()


async def test_gate_resume_foreign_decision_marker_block_not_cleared(monkeypatch: pytest.MonkeyPatch):
    """FAR-541 (THE C1 consumer regression): a decision stamped for a
    DIFFERENT gate must never clear THIS node's conformance block — the block
    stands and the node re-interrupts (stays awaiting_human) instead of
    proceeding under a foreign override."""
    _set_ctx(monkeypatch)
    check = _patch_check_node_start(monkeypatch, None)
    audit = _patch_audit(monkeypatch)
    interrupt = _patch_interrupt(monkeypatch)
    state = {
        "_conformance_blocked_node": _NODE_ID,
        "_conformance_blocked_gate": "guardrail_conformance_g_block",
        "_hitl_decision": {"action": "approved", "gate_id": "some_other_gate"},
    }

    blocked = await nr._run_conformance_gate(state, node_id=_NODE_ID)

    assert blocked is True
    assert state["_conformance_blocked_node"] == _NODE_ID
    check.assert_not_awaited()
    audit.assert_not_awaited()
    interrupt.assert_called_once()


async def test_gate_resume_replayed_decision_is_rechecked_and_clears_block(monkeypatch: pytest.MonkeyPatch):
    """FAR-541 iteration 4 (F-6): when the re-interrupt's resume value REPLAYS
    in the same execution pass (the human decided after the foreign-resume
    re-interrupt), the replayed decision is re-checked instead of falling
    through to ``True`` — a fall-through would terminalize the run with
    ``GuardrailBlockedError`` while the block is still standing. Mirrors the
    gate node's recursive re-entry."""
    _set_ctx(monkeypatch)
    check = _patch_check_node_start(monkeypatch, None)
    _patch_audit(monkeypatch)
    interrupt = _patch_interrupt(monkeypatch)
    interrupt.return_value = {"action": "approved", "gate_id": "guardrail_conformance_g_block"}
    state = {
        "_conformance_blocked_node": _NODE_ID,
        "_conformance_blocked_gate": "guardrail_conformance_g_block",
        "_hitl_decision": {"action": "approved", "gate_id": "some_other_gate"},
    }

    blocked = await nr._run_conformance_gate(state, node_id=_NODE_ID)

    assert blocked is False
    assert state["_conformance_blocked_node"] is None
    assert state["_conformance_blocked_gate"] is None
    check.assert_not_awaited()
    interrupt.assert_called_once()


async def test_gate_resume_second_foreign_replay_reinterrupts_keeps_waiting(monkeypatch: pytest.MonkeyPatch):
    """FAR-541 iteration 4 (F-6): a SECOND foreign replay re-interrupts (the
    block stands, the run keeps waiting) instead of falling through to
    ``True`` — the recursion advances one human decision per pass, exactly
    like the gate node."""

    class _GraphInterruptError(Exception):
        """Stands in for LangGraph's pause on the next interrupt call."""

    _set_ctx(monkeypatch)
    check = _patch_check_node_start(monkeypatch, None)
    _patch_audit(monkeypatch)
    interrupt = MagicMock(
        side_effect=[
            {"action": "approved", "gate_id": "some_other_gate"},  # 1st replay: still foreign
            _GraphInterruptError(),  # 2nd interrupt: the graph pauses again
        ]
    )
    monkeypatch.setattr(nr, "interrupt", interrupt)
    state = {
        "_conformance_blocked_node": _NODE_ID,
        "_conformance_blocked_gate": "guardrail_conformance_g_block",
        "_hitl_decision": {"action": "approved", "gate_id": "yet_another_gate"},
    }

    with pytest.raises(_GraphInterruptError):
        await nr._run_conformance_gate(state, node_id=_NODE_ID)

    assert state["_conformance_blocked_node"] == _NODE_ID
    check.assert_not_awaited()
    assert interrupt.call_count == 2


async def test_gate_resume_unstamped_decision_block_not_cleared(monkeypatch: pytest.MonkeyPatch):
    """FAR-541: a decision without a stamp cannot be attributed to this block
    — the block stands and the node re-interrupts."""
    _set_ctx(monkeypatch)
    check = _patch_check_node_start(monkeypatch, None)
    _patch_audit(monkeypatch)
    interrupt = _patch_interrupt(monkeypatch)
    state = {
        "_conformance_blocked_node": _NODE_ID,
        "_conformance_blocked_gate": "guardrail_conformance_g_block",
        "_hitl_decision": {"action": "approved"},
    }

    blocked = await nr._run_conformance_gate(state, node_id=_NODE_ID)

    assert blocked is True
    assert state["_conformance_blocked_node"] == _NODE_ID
    check.assert_not_awaited()
    interrupt.assert_called_once()


async def test_gate_resume_foreign_decision_runs_real_check(monkeypatch: pytest.MonkeyPatch):
    """A resume decision for a DIFFERENT gate (``_hitl_decision`` present but no
    per-node marker) must NEVER skip this node's conformance check — the
    decision persists in state for the whole run, so a foreign decision must
    not disable the safety gate. The real live check still runs (fail-closed)."""
    _set_ctx(monkeypatch)
    check = _patch_check_node_start(
        monkeypatch,
        ConformanceRecheckResult(blocked=False, gate_id=None, detail="", state="present", warned=False, claimed=True),
    )
    audit = _patch_audit(monkeypatch)
    interrupt = _patch_interrupt(monkeypatch)

    blocked = await nr._run_conformance_gate(
        {"_hitl_decision": {"action": "approved", "gate_id": "some_other_gate"}},
        node_id=_NODE_ID,
    )

    assert blocked is False
    check.assert_awaited_once()
    audit.assert_not_awaited()
    interrupt.assert_not_called()


async def test_gate_resume_foreign_decision_block_still_blocks(monkeypatch: pytest.MonkeyPatch):
    """Even under a foreign resume decision the live check still enforces the
    block — a capability that is absent/unknown at node start blocks, never
    fail-open because a different gate was resumed earlier in the run."""
    _set_ctx(monkeypatch)
    _patch_check_node_start(
        monkeypatch,
        ConformanceRecheckResult(
            blocked=True,
            gate_id="guardrail_conformance_g_block",
            detail="capability missing",
            state="absent",
            warned=False,
            claimed=True,
        ),
    )
    audit = _patch_audit(monkeypatch)
    interrupt = _patch_interrupt(monkeypatch)
    state = {"_hitl_decision": {"action": "approved", "gate_id": "some_other_gate"}}

    blocked = await nr._run_conformance_gate(state, node_id=_NODE_ID)

    assert blocked is True
    assert state["_conformance_blocked_node"] == _NODE_ID
    audit.assert_awaited_once()
    interrupt.assert_called_once()


# ---------------------------------------------------------------------------
# Block-action absent/unknown -> audit + interrupt (never fail open)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "result",
    [
        ConformanceRecheckResult(
            blocked=True,
            gate_id="guardrail_conformance_g_block",
            detail=(
                "guardrail 'g_block' requires capabilities ['sandbox.e2b'] which are no longer present (state=absent)"
            ),
            state="absent",
            warned=False,
            claimed=True,
        ),
        ConformanceRecheckResult(
            blocked=True,
            gate_id="guardrail_conformance_g_block",
            detail="capability source could not be read (state=unknown)",
            state="unknown",
            warned=False,
            claimed=True,
        ),
    ],
)
async def test_gate_blocked_interrupts(monkeypatch: pytest.MonkeyPatch, result: ConformanceRecheckResult):
    _set_ctx(monkeypatch)
    _patch_check_node_start(monkeypatch, result)
    audit = _patch_audit(monkeypatch)
    interrupt = _patch_interrupt(monkeypatch)
    state: dict[str, Any] = {"_run_id": _RUN_ID}

    blocked = await nr._run_conformance_gate(state, node_id=_NODE_ID)

    assert blocked is True
    # The per-node marker is stamped so the resume path can route the human
    # decision (approve -> override, reject -> fail closed) for THIS node.
    assert state["_conformance_blocked_node"] == _NODE_ID
    interrupt.assert_called_once_with(
        {
            "gate_id": result.gate_id,
            "reason": result.detail,
            "node_id": _NODE_ID,
            "conformance_state": result.state,
            "conformance_blocked": True,
        }
    )
    audit.assert_awaited_once()
    call = audit.await_args
    assert call.kwargs["event_type"] == "guardrail.conformance_blocked_midrun"
    assert call.kwargs["run_id"] == _RUN_ID
    assert call.kwargs["node_id"] == _NODE_ID
    assert call.kwargs["detail"] == result.detail
    assert call.kwargs["state"] == result.state


# ---------------------------------------------------------------------------
# Warn/observe advisory -> audit only, continue
# ---------------------------------------------------------------------------


async def test_gate_warned_advisory_audits_and_continues(monkeypatch: pytest.MonkeyPatch):
    _set_ctx(monkeypatch)
    _patch_check_node_start(
        monkeypatch,
        ConformanceRecheckResult(
            blocked=False,
            gate_id=None,
            detail=(
                "guardrail 'g_warn' requires capabilities ['sandbox.e2b'] which are no longer present (state=absent)"
            ),
            state="absent",
            warned=True,
            claimed=True,
        ),
    )
    audit = _patch_audit(monkeypatch)
    interrupt = _patch_interrupt(monkeypatch)

    blocked = await nr._run_conformance_gate({"_run_id": _RUN_ID}, node_id=_NODE_ID)

    assert blocked is False
    audit.assert_awaited_once()
    assert audit.await_args.kwargs["event_type"] == "guardrail.conformance_warned_midrun"
    interrupt.assert_not_called()


async def test_gate_present_continues_no_audit(monkeypatch: pytest.MonkeyPatch):
    _set_ctx(monkeypatch)
    _patch_check_node_start(
        monkeypatch,
        ConformanceRecheckResult(blocked=False, gate_id=None, detail="", state="present", warned=False, claimed=True),
    )
    audit = _patch_audit(monkeypatch)
    interrupt = _patch_interrupt(monkeypatch)

    blocked = await nr._run_conformance_gate({"_run_id": _RUN_ID}, node_id=_NODE_ID)

    assert blocked is False
    audit.assert_not_awaited()
    interrupt.assert_not_called()


async def test_gate_zero_claim_result_continues(monkeypatch: pytest.MonkeyPatch):
    """Zero conformance claims -> fast-path result -> continue, no audit."""
    _set_ctx(monkeypatch)
    _patch_check_node_start(
        monkeypatch,
        ConformanceRecheckResult(blocked=False, gate_id=None, detail="", state="present", warned=False, claimed=False),
    )
    audit = _patch_audit(monkeypatch)
    interrupt = _patch_interrupt(monkeypatch)

    blocked = await nr._run_conformance_gate({"_run_id": _RUN_ID}, node_id=_NODE_ID)

    assert blocked is False
    audit.assert_not_awaited()
    interrupt.assert_not_called()


async def test_gate_invalid_ctx_values_fast_path(monkeypatch: pytest.MonkeyPatch):
    """Unparseable org/pipeline in the ctx -> fast-path continue, no DB."""
    _set_ctx(monkeypatch, org_id="not-a-uuid", pipeline_id="also-not-a-uuid")
    check = _patch_check_node_start(monkeypatch, None)
    audit = _patch_audit(monkeypatch)
    interrupt = _patch_interrupt(monkeypatch)

    blocked = await nr._run_conformance_gate({}, node_id=_NODE_ID)

    assert blocked is False
    check.assert_not_awaited()
    audit.assert_not_awaited()
    interrupt.assert_not_called()


# ---------------------------------------------------------------------------
# Hoisted claim discovery (FAR-215 MINOR 2): the gate forwards the executor's
# precomputed claimed-guardrail list + fail-closed marker to check_node_start,
# so the per-node path pays zero guardrail-load queries.
# ---------------------------------------------------------------------------


async def test_gate_forwards_hoisted_claimed_guardrails(monkeypatch: pytest.MonkeyPatch):
    """The gate passes the executor's hoisted claimed-guardrail list through to
    ``check_node_start`` — the per-node path pays no guardrail-load query."""
    _set_ctx(monkeypatch, claimed_guardrails=["claim-a", "claim-b"], claims_load_failed=False)
    check = _patch_check_node_start(
        monkeypatch,
        ConformanceRecheckResult(blocked=False, gate_id=None, detail="", state="present", warned=False, claimed=True),
    )
    audit = _patch_audit(monkeypatch)
    interrupt = _patch_interrupt(monkeypatch)

    blocked = await nr._run_conformance_gate({"_run_id": _RUN_ID}, node_id=_NODE_ID)

    assert blocked is False
    check.assert_awaited_once()
    kwargs = check.await_args.kwargs
    assert kwargs["claimed_guardrails"] == ["claim-a", "claim-b"]
    assert kwargs["claims_load_failed"] is False
    audit.assert_not_awaited()
    interrupt.assert_not_called()


async def test_gate_forwards_claims_load_failed_marker(monkeypatch: pytest.MonkeyPatch):
    """A run-start claim-discovery failure marker reaches ``check_node_start``
    so the node fails CLOSED (unknown blocks), never skipping claims."""
    _set_ctx(monkeypatch, claimed_guardrails=None, claims_load_failed=True)
    check = _patch_check_node_start(
        monkeypatch,
        ConformanceRecheckResult(
            blocked=True,
            gate_id="guardrail_conformance_check_failed",
            detail="could not load bound guardrails; failing closed",
            state="unknown",
            warned=False,
            claimed=True,
        ),
    )
    audit = _patch_audit(monkeypatch)
    interrupt = _patch_interrupt(monkeypatch)
    state: dict[str, Any] = {"_run_id": _RUN_ID}

    blocked = await nr._run_conformance_gate(state, node_id=_NODE_ID)

    assert blocked is True
    check.assert_awaited_once()
    kwargs = check.await_args.kwargs
    assert kwargs["claimed_guardrails"] is None
    assert kwargs["claims_load_failed"] is True
    assert state["_conformance_blocked_node"] == _NODE_ID
    audit.assert_awaited_once()
    interrupt.assert_called_once()


async def test_gate_forwards_node_def_to_check_node_start(monkeypatch: pytest.MonkeyPatch):
    """The gate forwards the node's definition dict to ``check_node_start`` so a
    ``sandbox_agent`` node's mechanically-derived sandbox surface (egress
    certification; write/git-credential unknown — FAR-212 PR A) lands in the
    live manifest. Without this forwarding the sandbox surface would be absent
    and even the egress guarantee could not be certified."""
    _set_ctx(monkeypatch)
    check = _patch_check_node_start(
        monkeypatch,
        ConformanceRecheckResult(blocked=False, gate_id=None, detail="", state="present", warned=False, claimed=False),
    )
    audit = _patch_audit(monkeypatch)
    interrupt = _patch_interrupt(monkeypatch)

    node_def = {"id": _NODE_ID, "node_type": "sandbox_agent", "read_only": True, "egress_policy": "deny_all"}
    blocked = await nr._run_conformance_gate({"_run_id": _RUN_ID}, node_id=_NODE_ID, node_def=node_def)

    assert blocked is False
    check.assert_awaited_once()
    assert check.await_args.kwargs["node_def"] is node_def
    audit.assert_not_awaited()
    interrupt.assert_not_called()


async def test_gate_absent_node_def_forwards_none(monkeypatch: pytest.MonkeyPatch):
    """When the caller provides no node_def (non-sandbox node builders) the gate
    forwards ``None`` — the live manifest contributes no sandbox surface, so no
    sandbox capability can be falsely certified for a node that has none."""
    _set_ctx(monkeypatch)
    check = _patch_check_node_start(
        monkeypatch,
        ConformanceRecheckResult(blocked=False, gate_id=None, detail="", state="present", warned=False, claimed=False),
    )
    audit = _patch_audit(monkeypatch)
    interrupt = _patch_interrupt(monkeypatch)

    blocked = await nr._run_conformance_gate({"_run_id": _RUN_ID}, node_id=_NODE_ID)

    assert blocked is False
    check.assert_awaited_once()
    assert check.await_args.kwargs["node_def"] is None
    audit.assert_not_awaited()
    interrupt.assert_not_called()


# ---------------------------------------------------------------------------
# _append_conformance_audit: summary-only payload, failure-isolated
# ---------------------------------------------------------------------------


async def test_append_conformance_audit_summary_payload(monkeypatch: pytest.MonkeyPatch):
    import modulo.core.audit_logger as audit_logger
    import modulo.db.rls as rls

    append_mock = AsyncMock()
    monkeypatch.setattr(audit_logger, "append_audit_event", append_mock)
    rls_mock = AsyncMock()
    monkeypatch.setattr(rls, "set_rls_org", rls_mock)
    rls_exec_mock = AsyncMock()
    monkeypatch.setattr(rls, "set_rls_execution_context", rls_exec_mock)

    await nr._append_conformance_audit(
        _fake_factory(),
        org_id=_ORG_ID,
        run_id=_RUN_ID,
        node_id=_NODE_ID,
        detail="some detail",
        state="absent",
        event_type="guardrail.conformance_blocked_midrun",
    )

    rls_mock.assert_awaited_once()
    rls_exec_mock.assert_awaited_once()
    append_mock.assert_awaited_once()
    kwargs = append_mock.await_args.kwargs
    assert kwargs["org_id"] == _ORG_ID
    assert kwargs["event_type"] == "guardrail.conformance_blocked_midrun"
    assert kwargs["resource_type"] == "run"
    assert kwargs["resource_id"] == _RUN_ID
    payload = kwargs["payload_json"]
    assert set(payload.keys()) == {"node_id", "conformance_state", "detail"}
    assert payload["node_id"] == _NODE_ID
    assert payload["conformance_state"] == "absent"
    assert payload["detail"] == "some detail"


async def test_append_conformance_audit_never_raises(monkeypatch: pytest.MonkeyPatch):
    import modulo.core.audit_logger as audit_logger

    async def _boom(*args: Any, **kwargs: Any) -> None:
        raise RuntimeError("audit db down")

    monkeypatch.setattr(audit_logger, "append_audit_event", _boom)

    # Must not raise — the audit write is best-effort and failure-isolated.
    await nr._append_conformance_audit(
        _fake_factory(),
        org_id=_ORG_ID,
        run_id=_RUN_ID,
        node_id=_NODE_ID,
        detail="detail",
        state="absent",
        event_type="guardrail.conformance_blocked_midrun",
    )
