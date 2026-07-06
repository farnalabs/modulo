"""Unit tests for HITL output delivery audit logging."""

import uuid
from datetime import UTC, datetime, timedelta
from unittest.mock import ANY, AsyncMock, MagicMock, patch

import pytest

from modulo.core.hitl_manager import (
    ClaimTokenExpiredError,
    ClaimTokenInvalidError,
    GateAlreadyDecidedError,
    GateNotFoundError,
    HITLManager,
)
from modulo.db.models.hitl_claim import HitlClaim

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

_ORG = uuid.uuid4()
_RUN = uuid.uuid4()
_PIPELINE = uuid.uuid4()
_USER = uuid.uuid4()
_GATE = "review-step"
_TEAM = uuid.uuid4()


def _gate(
    *,
    claimed_by: uuid.UUID | None = None,
    claim_token: str | None = None,
    expires_at: datetime | None = None,
    decision: str | None = None,
    claimed_at: datetime | None = None,
    required_team_id: uuid.UUID | None = None,
    delivered_at: datetime | None = None,
) -> HitlClaim:
    g = MagicMock(spec=HitlClaim)
    g.id = uuid.uuid4()
    g.run_id = _RUN
    g.gate_id = _GATE
    g.pipeline_id = _PIPELINE
    g.organisation_id = _ORG
    g.claimed_by = claimed_by
    g.claimed_at = claimed_at or (datetime.now(UTC) if claimed_by else None)
    g.claim_token = claim_token
    g.expires_at = expires_at
    g.decision = decision
    g.decision_at = None
    g.required_team_id = required_team_id
    g.delivered_at = delivered_at
    return g


def _session_decide(
    *,
    update_returns_id: uuid.UUID | None = None,
    diagnosis_gate: HitlClaim | None = None,
    session_get_gate: HitlClaim | None = None,
) -> AsyncMock:
    """Session mock replicating the _decide() call sequence."""
    session = AsyncMock()
    session.add = MagicMock()
    session.flush = AsyncMock()

    update_result = MagicMock()
    update_result.scalar_one_or_none.return_value = update_returns_id

    diag_result = MagicMock()
    diag_result.scalar_one_or_none.return_value = diagnosis_gate

    call_count = 0

    async def _execute(stmt) -> MagicMock:
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            return update_result
        return diag_result

    session.execute = _execute
    session.get = AsyncMock(return_value=session_get_gate)
    begin_nested_cm = AsyncMock()
    begin_nested_cm.__aenter__ = AsyncMock(return_value=None)
    begin_nested_cm.__aexit__ = AsyncMock(return_value=False)
    session.begin_nested = MagicMock(return_value=begin_nested_cm)
    return session


# ---------------------------------------------------------------------------
# Tests: audit event created on approval
# ---------------------------------------------------------------------------


async def test_approve_logs_output_delivered_audit_event():
    """Approving a claim creates a hitl.output_delivered audit event."""
    future = datetime.now(UTC) + timedelta(minutes=5)
    gate = _gate(claimed_by=_USER, claim_token="good-token", expires_at=future, required_team_id=_TEAM)
    gate_decided = _gate(
        claimed_by=None,
        claim_token=None,
        expires_at=None,
        decision="approved",
        required_team_id=_TEAM,
    )
    session = _session_decide(update_returns_id=gate.id, session_get_gate=gate_decided)

    with patch("modulo.core.hitl_manager.append_audit_event", new_callable=AsyncMock) as mock_audit:
        mgr = HITLManager()
        result = await mgr.approve(
            session,
            run_id=_RUN,
            gate_id=_GATE,
            org_id=_ORG,
            claim_token="good-token",
            actor_id=_USER,
        )

    assert result.decision == "approved"
    mock_audit.assert_awaited_once_with(
        session,
        org_id=_ORG,
        event_type="hitl.output_delivered",
        actor_user_id=_USER,
        resource_type="hitl_claim",
        resource_id=gate_decided.id,
        payload_json={
            "pipeline_run_id": str(_RUN),
            "node_id": _GATE,
            "decision": "approved",
            "team_id": str(_TEAM),
        },
    )


async def test_approve_without_actor_id_omits_actor_in_audit():
    """approve() with actor_id=None still logs audit with actor_user_id=None."""
    future = datetime.now(UTC) + timedelta(minutes=5)
    gate = _gate(claimed_by=_USER, claim_token="tok", expires_at=future)
    gate_decided = _gate(claimed_by=None, claim_token=None, expires_at=None, decision="approved")
    session = _session_decide(update_returns_id=gate.id, session_get_gate=gate_decided)

    with patch("modulo.core.hitl_manager.append_audit_event", new_callable=AsyncMock) as mock_audit:
        mgr = HITLManager()
        await mgr.approve(
            session,
            run_id=_RUN,
            gate_id=_GATE,
            org_id=_ORG,
            claim_token="tok",
        )

    mock_audit.assert_awaited_once_with(
        session,
        org_id=_ORG,
        event_type="hitl.output_delivered",
        actor_user_id=None,
        resource_type="hitl_claim",
        resource_id=gate_decided.id,
        payload_json=ANY,
    )


async def test_approve_team_id_none_in_audit_payload():
    """When required_team_id is None, team_id is None in the audit payload."""
    future = datetime.now(UTC) + timedelta(minutes=5)
    gate = _gate(claimed_by=_USER, claim_token="tok", expires_at=future)
    gate_decided = _gate(claimed_by=None, claim_token=None, expires_at=None, decision="approved")
    session = _session_decide(update_returns_id=gate.id, session_get_gate=gate_decided)

    with patch("modulo.core.hitl_manager.append_audit_event", new_callable=AsyncMock) as mock_audit:
        mgr = HITLManager()
        await mgr.approve(session, run_id=_RUN, gate_id=_GATE, org_id=_ORG, claim_token="tok", actor_id=_USER)

    payload = mock_audit.await_args.kwargs["payload_json"]
    assert payload["team_id"] is None


# ---------------------------------------------------------------------------
# Tests: delivered_at is set on approval
# ---------------------------------------------------------------------------


async def test_approve_sets_delivered_at():
    """Approving a claim sets delivered_at on the gate."""
    future = datetime.now(UTC) + timedelta(minutes=5)
    gate = _gate(claimed_by=_USER, claim_token="good-token", expires_at=future)
    gate_decided = _gate(claimed_by=None, claim_token=None, expires_at=None, decision="approved")
    session = _session_decide(update_returns_id=gate.id, session_get_gate=gate_decided)

    with patch("modulo.core.hitl_manager.append_audit_event", new_callable=AsyncMock):
        mgr = HITLManager()
        result = await mgr.approve(
            session, run_id=_RUN, gate_id=_GATE, org_id=_ORG, claim_token="good-token", actor_id=_USER
        )

    assert result.delivered_at is not None
    assert isinstance(result.delivered_at, datetime)


async def test_delivered_at_is_recent():
    """delivered_at should be set to approximately now."""
    future = datetime.now(UTC) + timedelta(minutes=5)
    gate = _gate(claimed_by=_USER, claim_token="tok", expires_at=future)
    gate_decided = _gate(claimed_by=None, claim_token=None, expires_at=None, decision="approved")
    session = _session_decide(update_returns_id=gate.id, session_get_gate=gate_decided)

    with patch("modulo.core.hitl_manager.append_audit_event", new_callable=AsyncMock):
        mgr = HITLManager()
        before = datetime.now(UTC)
        result = await mgr.approve(session, run_id=_RUN, gate_id=_GATE, org_id=_ORG, claim_token="tok", actor_id=_USER)
        after = datetime.now(UTC)

    assert before <= result.delivered_at <= after


# ---------------------------------------------------------------------------
# Tests: existing approve errors still propagate
# ---------------------------------------------------------------------------


async def test_approve_wrong_token_raises_and_no_audit():
    """If _decide() fails, no audit event is logged."""
    future = datetime.now(UTC) + timedelta(minutes=5)
    gate = _gate(claimed_by=_USER, claim_token="correct", expires_at=future)
    session = _session_decide(update_returns_id=None, diagnosis_gate=gate)

    with patch("modulo.core.hitl_manager.append_audit_event", new_callable=AsyncMock) as mock_audit:
        mgr = HITLManager()
        with pytest.raises(ClaimTokenInvalidError):
            await mgr.approve(session, run_id=_RUN, gate_id=_GATE, org_id=_ORG, claim_token="wrong", actor_id=_USER)

    mock_audit.assert_not_awaited()


async def test_approve_expired_token_raises_and_no_audit():
    past = datetime.now(UTC) - timedelta(minutes=1)
    gate = _gate(claimed_by=_USER, claim_token="tok", expires_at=past)
    session = _session_decide(update_returns_id=None, diagnosis_gate=gate)

    with patch("modulo.core.hitl_manager.append_audit_event", new_callable=AsyncMock) as mock_audit:
        mgr = HITLManager()
        with pytest.raises(ClaimTokenExpiredError):
            await mgr.approve(session, run_id=_RUN, gate_id=_GATE, org_id=_ORG, claim_token="tok", actor_id=_USER)

    mock_audit.assert_not_awaited()


async def test_approve_gate_not_found_raises_and_no_audit():
    session = _session_decide(update_returns_id=None, diagnosis_gate=None)

    with patch("modulo.core.hitl_manager.append_audit_event", new_callable=AsyncMock) as mock_audit:
        mgr = HITLManager()
        with pytest.raises(GateNotFoundError):
            await mgr.approve(session, run_id=_RUN, gate_id=_GATE, org_id=_ORG, claim_token="tok", actor_id=_USER)

    mock_audit.assert_not_awaited()


async def test_approve_already_decided_raises_and_no_audit():
    gate = _gate(decision="approved")
    session = _session_decide(update_returns_id=None, diagnosis_gate=gate)

    with patch("modulo.core.hitl_manager.append_audit_event", new_callable=AsyncMock) as mock_audit:
        mgr = HITLManager()
        with pytest.raises(GateAlreadyDecidedError):
            await mgr.approve(session, run_id=_RUN, gate_id=_GATE, org_id=_ORG, claim_token="tok", actor_id=_USER)

    mock_audit.assert_not_awaited()


# ---------------------------------------------------------------------------
# Tests: delivery failure is logged
# ---------------------------------------------------------------------------


async def test_delivery_failure_logs_failed_event():
    """When append_audit_event raises, hitl.output_delivery_failed is logged."""
    future = datetime.now(UTC) + timedelta(minutes=5)
    gate = _gate(claimed_by=_USER, claim_token="good-token", expires_at=future)
    gate_decided = _gate(claimed_by=None, claim_token=None, expires_at=None, decision="approved")
    session = _session_decide(update_returns_id=gate.id, session_get_gate=gate_decided)

    with (
        patch(
            "modulo.core.hitl_manager.append_audit_event",
            new_callable=AsyncMock,
            side_effect=[RuntimeError("audit db down"), None],
        ) as mock_audit,
        patch.object(session, "flush", new_callable=AsyncMock),
    ):
        mgr = HITLManager()
        with pytest.raises(RuntimeError, match="audit db down"):
            await mgr.approve(
                session, run_id=_RUN, gate_id=_GATE, org_id=_ORG, claim_token="good-token", actor_id=_USER
            )

        # First call: hitl.output_delivered (failed)
        # Second call: hitl.output_delivery_failed
        assert mock_audit.await_count == 2

        first_call = mock_audit.await_args_list[0]
        assert first_call.kwargs["event_type"] == "hitl.output_delivered"

        second_call = mock_audit.await_args_list[1]
        assert second_call.kwargs["event_type"] == "hitl.output_delivery_failed"
        assert second_call.kwargs["actor_user_id"] == _USER
        assert second_call.kwargs["resource_type"] == "hitl_claim"
        assert second_call.kwargs["resource_id"] == gate_decided.id
        assert second_call.kwargs["payload_json"]["pipeline_run_id"] == str(_RUN)
        assert second_call.kwargs["payload_json"]["node_id"] == _GATE
        assert second_call.kwargs["payload_json"]["decision"] == "approved"


async def test_delivery_failure_does_not_set_delivered_at():
    """When audit fails, delivered_at is NOT set on the gate."""
    future = datetime.now(UTC) + timedelta(minutes=5)
    gate = _gate(claimed_by=_USER, claim_token="tok", expires_at=future)
    gate_decided = _gate(claimed_by=None, claim_token=None, expires_at=None, decision="approved")
    session = _session_decide(update_returns_id=gate.id, session_get_gate=gate_decided)

    with (
        patch(
            "modulo.core.hitl_manager.append_audit_event",
            new_callable=AsyncMock,
            side_effect=RuntimeError("audit db down"),
        ),
        patch.object(session, "flush", new_callable=AsyncMock),
    ):
        mgr = HITLManager()
        with pytest.raises(RuntimeError):
            await mgr.approve(session, run_id=_RUN, gate_id=_GATE, org_id=_ORG, claim_token="tok", actor_id=_USER)

    assert gate_decided.delivered_at is None


async def test_delivery_failure_propagates_original_error():
    """The original error from append_audit_event propagates to the caller."""
    future = datetime.now(UTC) + timedelta(minutes=5)
    gate = _gate(claimed_by=_USER, claim_token="tok", expires_at=future)
    gate_decided = _gate(claimed_by=None, claim_token=None, expires_at=None, decision="approved")
    session = _session_decide(update_returns_id=gate.id, session_get_gate=gate_decided)

    with (
        patch(
            "modulo.core.hitl_manager.append_audit_event",
            new_callable=AsyncMock,
            side_effect=ConnectionError("database connection lost"),
        ),
        patch.object(session, "flush", new_callable=AsyncMock),
    ):
        mgr = HITLManager()
        with pytest.raises(ConnectionError, match="database connection lost"):
            await mgr.approve(session, run_id=_RUN, gate_id=_GATE, org_id=_ORG, claim_token="tok", actor_id=_USER)


async def test_delivery_failure_logged_even_when_failed_event_also_fails():
    """If both audit attempts fail, the original error still propagates."""
    future = datetime.now(UTC) + timedelta(minutes=5)
    gate = _gate(claimed_by=_USER, claim_token="tok", expires_at=future)
    gate_decided = _gate(claimed_by=None, claim_token=None, expires_at=None, decision="approved")
    session = _session_decide(update_returns_id=gate.id, session_get_gate=gate_decided)

    with (
        patch(
            "modulo.core.hitl_manager.append_audit_event",
            new_callable=AsyncMock,
            side_effect=[RuntimeError("first fail"), RuntimeError("second fail too")],
        ),
        patch.object(session, "flush", new_callable=AsyncMock),
    ):
        mgr = HITLManager()
        with pytest.raises(RuntimeError, match="first fail"):
            await mgr.approve(session, run_id=_RUN, gate_id=_GATE, org_id=_ORG, claim_token="tok", actor_id=_USER)
