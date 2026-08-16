"""Unit tests for the FAR-215 node-start conformance gate (node_runner).

Covers the node_runner seam ``_run_conformance_gate``:
  - no conformance ctx -> fast path (no DB, no interrupt)
  - resume after a human reviewed the conformance block (``_hitl_decision``)
    -> check skipped entirely
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
) -> None:
    ctx = (session_factory or _fake_factory(), org_id, env_profile, pipeline_id)
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


async def test_gate_resume_skips_check(monkeypatch: pytest.MonkeyPatch):
    """State carrying ``_hitl_decision`` (resumed after human review) skips the
    re-check entirely — the block was already routed to a human and reviewed."""
    _set_ctx(monkeypatch)
    check = _patch_check_node_start(monkeypatch, None)
    audit = _patch_audit(monkeypatch)
    interrupt = _patch_interrupt(monkeypatch)

    blocked = await nr._run_conformance_gate(
        {"_hitl_decision": {"approved": True, "gate_id": "guardrail_conformance_g_block"}},
        node_id=_NODE_ID,
    )

    assert blocked is False
    check.assert_not_awaited()
    audit.assert_not_awaited()
    interrupt.assert_not_called()


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

    blocked = await nr._run_conformance_gate({"_run_id": _RUN_ID}, node_id=_NODE_ID)

    assert blocked is True
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
# _append_conformance_audit: summary-only payload, failure-isolated
# ---------------------------------------------------------------------------


async def test_append_conformance_audit_summary_payload(monkeypatch: pytest.MonkeyPatch):
    import modulo.core.audit_logger as audit_logger
    import modulo.db.rls as rls

    append_mock = AsyncMock()
    monkeypatch.setattr(audit_logger, "append_audit_event", append_mock)
    rls_mock = AsyncMock()
    monkeypatch.setattr(rls, "set_rls_org", rls_mock)

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
