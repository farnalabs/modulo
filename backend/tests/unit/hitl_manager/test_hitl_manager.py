"""Unit tests for HITLManager using mocked AsyncSession."""

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from modulo.core.hitl_manager import (
    AlreadyClaimedError,
    ClaimTokenExpiredError,
    ClaimTokenInvalidError,
    GateAlreadyDecidedError,
    GateNotFoundError,
    HITLManager,
    NotTeamMemberError,
)
from modulo.db.models.hitl_claim import HitlClaim
from modulo.db.models.team_membership import TeamMembership

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_ORG = uuid.uuid4()
_RUN = uuid.uuid4()
_PIPELINE = uuid.uuid4()
_USER = uuid.uuid4()
_GATE = "review-step"


def _gate(
    *,
    account_id: uuid.UUID | None = None,
    claim_token: str | None = None,
    expires_at: datetime | None = None,
    decision: str | None = None,
    claimed_at: datetime | None = None,
    required_team_id: uuid.UUID | None = None,
) -> HitlClaim:
    g = MagicMock(spec=HitlClaim)
    g.id = uuid.uuid4()
    g.run_id = _RUN
    g.gate_id = _GATE
    g.pipeline_id = _PIPELINE
    g.organisation_id = _ORG
    g.account_id = account_id
    g.claimed_at = claimed_at or (datetime.now(UTC) if account_id else None)
    g.claim_token = claim_token
    g.expires_at = expires_at
    g.decision = decision
    g.decision_at = None
    g.required_team_id = required_team_id
    return g


def _session_get(return_value: Any = None) -> AsyncMock:
    """Session whose execute() returns a result with scalar_one_or_none()."""
    session = AsyncMock()
    result = MagicMock()
    result.scalar_one_or_none.return_value = return_value
    scalars_result = MagicMock()
    scalars_result.__iter__ = lambda self: iter([return_value] if return_value else [])
    result.scalars.return_value = scalars_result
    session.execute = AsyncMock(return_value=result)
    session.add = MagicMock()
    session.flush = AsyncMock()
    return session


def _session_decide(
    *,
    update_returns_id: uuid.UUID | None = None,
    diagnosis_gate: HitlClaim | None = None,
    session_get_gate: HitlClaim | None = None,
) -> AsyncMock:
    """Session mock for HITLManager._decide().

    Call sequence in _decide():
      1. UPDATE … RETURNING id  → scalar_one_or_none() returns update_returns_id (or None)
      2. If None: _get() SELECT → scalar_one_or_none() returns diagnosis_gate
      If update_returns_id is not None: session.get() returns session_get_gate
    """
    session = AsyncMock()
    session.add = MagicMock()
    session.flush = AsyncMock()

    update_result = MagicMock()
    update_result.scalar_one_or_none.return_value = update_returns_id

    diag_result = MagicMock()
    diag_result.scalar_one_or_none.return_value = diagnosis_gate

    call_count = 0

    async def _execute(stmt: Any) -> Any:
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            return update_result
        return diag_result

    session.execute = _execute
    session.get = AsyncMock(return_value=session_get_gate)
    return session


def _session_update(
    *,
    rows_returned: int = 1,
    gate: HitlClaim | None = None,
    pre_check_gate: HitlClaim | None = None,
) -> AsyncMock:
    """Session that simulates a claim() flow with pre-check + UPDATE + refetch.

    Call sequence:
      1. Pre-check SELECT (returns ``pre_check_gate`` or falls back to ``gate``)
      2. UPDATE … RETURNING  (returns claimed id if rows_returned > 0)
      3. Re-fetch SELECT     (returns ``gate``)
    """
    session = AsyncMock()
    session.add = MagicMock()
    session.flush = AsyncMock()

    update_result = MagicMock()
    # For the claim() UPDATE RETURNING id
    update_result.scalar_one_or_none.return_value = uuid.uuid4() if rows_returned > 0 else None
    # For expire_stale() UPDATE RETURNING run_id, gate_id
    update_result.all.return_value = [(_RUN, _GATE)] * rows_returned

    get_result = MagicMock()
    get_result.scalar_one_or_none.return_value = gate

    pre_check_result = MagicMock()
    pre_check_result.scalar_one_or_none.return_value = pre_check_gate

    call_count = 0

    async def _execute(stmt: Any) -> Any:
        nonlocal call_count
        call_count += 1
        # First execute is the pre-check SELECT; second is the UPDATE; third is the re-fetch
        if call_count == 1:
            return pre_check_result if pre_check_gate is not None else get_result
        if call_count == 2:
            return update_result
        return get_result

    session.execute = _execute
    return session


# ---------------------------------------------------------------------------
# create_gate
# ---------------------------------------------------------------------------


async def test_create_gate_inserts_new_row():
    session = _session_get(return_value=None)
    mgr = HITLManager()
    _gate = await mgr.create_gate(session, run_id=_RUN, gate_id=_GATE, pipeline_id=_PIPELINE, org_id=_ORG)
    session.add.assert_called_once()
    session.flush.assert_called_once()
    added = session.add.call_args[0][0]
    assert added.run_id == _RUN
    assert added.gate_id == _GATE
    assert added.account_id is None


async def test_create_gate_idempotent_if_exists():
    existing = _gate()
    session = _session_get(return_value=existing)
    mgr = HITLManager()
    result = await mgr.create_gate(session, run_id=_RUN, gate_id=_GATE, pipeline_id=_PIPELINE, org_id=_ORG)
    assert result is existing
    session.add.assert_not_called()


# ---------------------------------------------------------------------------
# claim
# ---------------------------------------------------------------------------


async def test_claim_success_sets_token_and_expiry():
    pre_check = _gate(account_id=None)
    claimed_gate = _gate(
        account_id=_USER,
        claim_token="tok",
        expires_at=datetime.now(UTC) + timedelta(minutes=15),
    )
    session = _session_update(rows_returned=1, gate=claimed_gate, pre_check_gate=pre_check)
    mgr = HITLManager()
    result = await mgr.claim(session, run_id=_RUN, gate_id=_GATE, org_id=_ORG, claimant_id=_USER)
    assert result is claimed_gate


async def test_claim_already_claimed_raises():
    existing = _gate(account_id=uuid.uuid4(), claim_token="tok")
    session = _session_update(rows_returned=0, gate=existing, pre_check_gate=existing)
    mgr = HITLManager()
    with pytest.raises(AlreadyClaimedError):
        await mgr.claim(session, run_id=_RUN, gate_id=_GATE, org_id=_ORG, claimant_id=_USER)


async def test_claim_gate_not_found_raises():
    session = _session_update(rows_returned=0, gate=None, pre_check_gate=None)
    mgr = HITLManager()
    with pytest.raises(GateNotFoundError):
        await mgr.claim(session, run_id=_RUN, gate_id=_GATE, org_id=_ORG, claimant_id=_USER)


async def test_claim_custom_expiry_minutes_is_applied():
    """claim() with custom expiry_minutes passes the right interval to the UPDATE."""
    pre_check = _gate(account_id=None)
    claimed_gate = _gate(
        account_id=_USER,
        claim_token="tok",
        expires_at=datetime.now(UTC) + timedelta(minutes=60),
    )
    session = _session_update(rows_returned=1, gate=claimed_gate, pre_check_gate=pre_check)
    mgr = HITLManager()
    result = await mgr.claim(session, run_id=_RUN, gate_id=_GATE, org_id=_ORG, claimant_id=_USER, expiry_minutes=60)
    assert result is claimed_gate


# ---------------------------------------------------------------------------
# Team-scoped gates
# ---------------------------------------------------------------------------

_TEAM = uuid.uuid4()


async def test_create_gate_with_required_team_id():
    session = _session_get(return_value=None)
    mgr = HITLManager()
    result = await mgr.create_gate(
        session, run_id=_RUN, gate_id=_GATE, pipeline_id=_PIPELINE, org_id=_ORG, required_team_id=_TEAM
    )
    session.add.assert_called_once()
    assert result.required_team_id == _TEAM


async def test_claim_team_member_can_claim():
    """Team member can claim a team-scoped gate."""
    unclaimed = _gate(account_id=None, required_team_id=_TEAM)
    claimed = _gate(
        account_id=_USER,
        claim_token="tok",
        expires_at=datetime.now(UTC) + timedelta(minutes=15),
        required_team_id=_TEAM,
    )
    membership = MagicMock(spec=TeamMembership)
    membership.team_id = _TEAM
    membership.account_id = _USER

    session = AsyncMock()
    session.add = MagicMock()
    session.flush = AsyncMock()

    call_no = 0
    team_check_hit = False

    async def _execute(stmt: Any) -> Any:
        nonlocal call_no, team_check_hit
        call_no += 1
        if call_no == 1:
            # Pre-check SELECT
            r = MagicMock()
            r.scalar_one_or_none.return_value = unclaimed
            return r
        if call_no == 2:
            # Team membership check
            team_check_hit = True
            r = MagicMock()
            r.scalar_one_or_none.return_value = membership
            return r
        if call_no == 3:
            # UPDATE
            r = MagicMock()
            r.scalar_one_or_none.return_value = uuid.uuid4()
            return r
        # Re-fetch
        r = MagicMock()
        r.scalar_one_or_none.return_value = claimed
        return r

    session.execute = _execute
    mgr = HITLManager()
    result = await mgr.claim(session, run_id=_RUN, gate_id=_GATE, org_id=_ORG, claimant_id=_USER)
    assert result is claimed
    assert team_check_hit, "Team membership check was not performed"


async def test_claim_non_team_member_raises():
    """Non-team member gets NotTeamMemberError on a team-scoped gate."""
    gate = _gate(account_id=None, required_team_id=_TEAM)

    session = AsyncMock()
    session.add = MagicMock()
    session.flush = AsyncMock()

    call_no = 0
    team_check_hit = False

    async def _execute(stmt: Any) -> Any:
        nonlocal call_no, team_check_hit
        call_no += 1
        if call_no == 1:
            # Pre-check SELECT
            r = MagicMock()
            r.scalar_one_or_none.return_value = gate
            return r
        if call_no == 2:
            # Team membership check — no membership found
            team_check_hit = True
            r = MagicMock()
            r.scalar_one_or_none.return_value = None
            return r
        raise AssertionError("Should not reach UPDATE or refetch")

    session.execute = _execute
    mgr = HITLManager()
    with pytest.raises(NotTeamMemberError):
        await mgr.claim(session, run_id=_RUN, gate_id=_GATE, org_id=_ORG, claimant_id=_USER)
    assert team_check_hit, "Team membership check was not performed"


async def test_claim_no_required_team_still_works():
    """Gate without required_team_id still allows existing claim behavior."""
    unclaimed = _gate(account_id=None)
    claimed = _gate(
        account_id=_USER,
        claim_token="tok",
        expires_at=datetime.now(UTC) + timedelta(minutes=15),
    )
    session = AsyncMock()
    session.add = MagicMock()
    session.flush = AsyncMock()

    call_no = 0

    async def _execute(stmt: Any) -> Any:
        nonlocal call_no
        call_no += 1
        if call_no == 1:
            # Pre-check SELECT — gate exists, no required_team_id
            r = MagicMock()
            r.scalar_one_or_none.return_value = unclaimed
            return r
        if call_no == 2:
            # UPDATE
            r = MagicMock()
            r.scalar_one_or_none.return_value = uuid.uuid4()
            return r
        # Re-fetch
        r = MagicMock()
        r.scalar_one_or_none.return_value = claimed
        return r

    session.execute = _execute
    mgr = HITLManager()
    result = await mgr.claim(session, run_id=_RUN, gate_id=_GATE, org_id=_ORG, claimant_id=_USER)
    assert result is claimed


# ---------------------------------------------------------------------------
# approve
# ---------------------------------------------------------------------------


async def test_approve_valid_token_records_decision():
    future = datetime.now(UTC) + timedelta(minutes=5)
    gate = _gate(account_id=_USER, claim_token="good-token", expires_at=future)
    gate_decided = _gate(account_id=None, claim_token=None, expires_at=None, decision="approved")
    session = _session_decide(update_returns_id=gate.id, session_get_gate=gate_decided)
    mgr = HITLManager()
    result = await mgr.approve(session, run_id=_RUN, gate_id=_GATE, org_id=_ORG, claim_token="good-token")
    assert result.decision == "approved"
    assert result.claim_token is None
    assert result.account_id is None


async def test_approve_wrong_token_raises():
    future = datetime.now(UTC) + timedelta(minutes=5)
    gate = _gate(account_id=_USER, claim_token="correct", expires_at=future)
    session = _session_decide(update_returns_id=None, diagnosis_gate=gate)
    mgr = HITLManager()
    with pytest.raises(ClaimTokenInvalidError):
        await mgr.approve(session, run_id=_RUN, gate_id=_GATE, org_id=_ORG, claim_token="wrong")


async def test_approve_expired_token_raises():
    past = datetime.now(UTC) - timedelta(minutes=1)
    gate = _gate(account_id=_USER, claim_token="tok", expires_at=past)
    session = _session_decide(update_returns_id=None, diagnosis_gate=gate)
    mgr = HITLManager()
    with pytest.raises(ClaimTokenExpiredError):
        await mgr.approve(session, run_id=_RUN, gate_id=_GATE, org_id=_ORG, claim_token="tok")


async def test_approve_gate_not_found_raises():
    session = _session_decide(update_returns_id=None, diagnosis_gate=None)
    mgr = HITLManager()
    with pytest.raises(GateNotFoundError):
        await mgr.approve(session, run_id=_RUN, gate_id=_GATE, org_id=_ORG, claim_token="tok")


async def test_approve_already_decided_raises():
    gate = _gate(decision="approved")
    session = _session_decide(update_returns_id=None, diagnosis_gate=gate)
    mgr = HITLManager()
    with pytest.raises(GateAlreadyDecidedError):
        await mgr.approve(session, run_id=_RUN, gate_id=_GATE, org_id=_ORG, claim_token="tok")


async def test_approve_null_expires_at_raises_expired():
    """expires_at=None on a claimed gate (defensive guard) → ClaimTokenExpiredError."""
    # This state is unreachable via normal API flow but guard is defensive.
    gate = _gate(account_id=_USER, claim_token="tok", expires_at=None)
    session = _session_decide(update_returns_id=None, diagnosis_gate=gate)
    mgr = HITLManager()
    with pytest.raises(ClaimTokenExpiredError):
        await mgr.approve(session, run_id=_RUN, gate_id=_GATE, org_id=_ORG, claim_token="tok")


# ---------------------------------------------------------------------------
# approve_with_modification
# ---------------------------------------------------------------------------


async def test_approve_with_modification_valid_token_and_audit():
    """approve_with_modification records decision, logs audit, and sets delivered_at."""
    future = datetime.now(UTC) + timedelta(minutes=5)
    gate = _gate(account_id=_USER, claim_token="good-token", expires_at=future)
    gate_decided = _gate(account_id=None, claim_token=None, expires_at=None, decision="approved")
    session = _session_decide(update_returns_id=gate.id, session_get_gate=gate_decided)
    modified = {"summary": "Modified output from human review"}

    mgr = HITLManager()
    result = await mgr.approve_with_modification(
        session,
        run_id=_RUN,
        gate_id=_GATE,
        org_id=_ORG,
        claim_token="good-token",
        modified_output=modified,
        actor_id=_USER,
    )
    assert result.decision == "approved"
    assert result.claim_token is None
    assert result.account_id is None

    # Verify that two audit events were appended: output_modified + output_delivered
    assert session.add.call_count >= 2


async def test_approve_with_modification_wrong_token_raises():
    future = datetime.now(UTC) + timedelta(minutes=5)
    gate = _gate(account_id=_USER, claim_token="correct", expires_at=future)
    session = _session_decide(update_returns_id=None, diagnosis_gate=gate)
    mgr = HITLManager()
    with pytest.raises(ClaimTokenInvalidError):
        await mgr.approve_with_modification(
            session,
            run_id=_RUN,
            gate_id=_GATE,
            org_id=_ORG,
            claim_token="wrong",
            modified_output={"data": "x"},
        )


async def test_approve_with_modification_expired_token_raises():
    past = datetime.now(UTC) - timedelta(minutes=1)
    gate = _gate(account_id=_USER, claim_token="tok", expires_at=past)
    session = _session_decide(update_returns_id=None, diagnosis_gate=gate)
    mgr = HITLManager()
    with pytest.raises(ClaimTokenExpiredError):
        await mgr.approve_with_modification(
            session,
            run_id=_RUN,
            gate_id=_GATE,
            org_id=_ORG,
            claim_token="tok",
            modified_output={"data": "x"},
        )


async def test_approve_with_modification_gate_not_found_raises():
    session = _session_decide(update_returns_id=None, diagnosis_gate=None)
    mgr = HITLManager()
    with pytest.raises(GateNotFoundError):
        await mgr.approve_with_modification(
            session,
            run_id=_RUN,
            gate_id=_GATE,
            org_id=_ORG,
            claim_token="tok",
            modified_output={"data": "x"},
        )


async def test_approve_with_modification_already_decided_raises():
    gate = _gate(decision="rejected")
    session = _session_decide(update_returns_id=None, diagnosis_gate=gate)
    mgr = HITLManager()
    with pytest.raises(GateAlreadyDecidedError):
        await mgr.approve_with_modification(
            session,
            run_id=_RUN,
            gate_id=_GATE,
            org_id=_ORG,
            claim_token="tok",
            modified_output={"data": "x"},
        )


# ---------------------------------------------------------------------------
# reject
# ---------------------------------------------------------------------------


async def test_reject_valid_token_records_decision():
    future = datetime.now(UTC) + timedelta(minutes=5)
    gate = _gate(account_id=_USER, claim_token="tok", expires_at=future)
    gate_decided = _gate(account_id=None, claim_token=None, decision="rejected")
    session = _session_decide(update_returns_id=gate.id, session_get_gate=gate_decided)
    mgr = HITLManager()
    result = await mgr.reject(session, run_id=_RUN, gate_id=_GATE, org_id=_ORG, claim_token="tok")
    assert result.decision == "rejected"
    assert result.claim_token is None


async def test_reject_wrong_token_raises():
    future = datetime.now(UTC) + timedelta(minutes=5)
    gate = _gate(account_id=_USER, claim_token="correct", expires_at=future)
    session = _session_decide(update_returns_id=None, diagnosis_gate=gate)
    mgr = HITLManager()
    with pytest.raises(ClaimTokenInvalidError):
        await mgr.reject(session, run_id=_RUN, gate_id=_GATE, org_id=_ORG, claim_token="wrong")


# ---------------------------------------------------------------------------
# deliver_manual
# ---------------------------------------------------------------------------


async def test_deliver_manual_valid_token_records_decision():
    """deliver_manual with valid token records decision and output in audit."""
    future = datetime.now(UTC) + timedelta(minutes=5)
    gate = _gate(account_id=_USER, claim_token="good-token", expires_at=future)
    gate_decided = _gate(account_id=None, claim_token=None, expires_at=None, decision="deliver_manual")
    session = _session_decide(update_returns_id=gate.id, session_get_gate=gate_decided)
    manual_output = {"summary": "Manually provided output", "status": "approved"}
    mgr = HITLManager()
    result = await mgr.deliver_manual(
        session,
        run_id=_RUN,
        gate_id=_GATE,
        org_id=_ORG,
        claim_token="good-token",
        output=manual_output,
        actor_id=_USER,
    )
    assert result.decision == "deliver_manual"
    assert result.claim_token is None
    assert result.account_id is None


async def test_deliver_manual_with_empty_output_accepts():
    """deliver_manual accepts an empty output dict (validation at API layer)."""
    future = datetime.now(UTC) + timedelta(minutes=5)
    gate = _gate(account_id=_USER, claim_token="tok", expires_at=future)
    gate_decided = _gate(account_id=None, claim_token=None, expires_at=None, decision="deliver_manual")
    session = _session_decide(update_returns_id=gate.id, session_get_gate=gate_decided)
    mgr = HITLManager()
    result = await mgr.deliver_manual(
        session,
        run_id=_RUN,
        gate_id=_GATE,
        org_id=_ORG,
        claim_token="tok",
        output={},
        actor_id=_USER,
    )
    assert result.decision == "deliver_manual"


async def test_deliver_manual_wrong_token_raises():
    future = datetime.now(UTC) + timedelta(minutes=5)
    gate = _gate(account_id=_USER, claim_token="correct", expires_at=future)
    session = _session_decide(update_returns_id=None, diagnosis_gate=gate)
    mgr = HITLManager()
    with pytest.raises(ClaimTokenInvalidError):
        await mgr.deliver_manual(
            session,
            run_id=_RUN,
            gate_id=_GATE,
            org_id=_ORG,
            claim_token="wrong",
            output={"data": "x"},
        )


async def test_deliver_manual_expired_token_raises():
    past = datetime.now(UTC) - timedelta(minutes=1)
    gate = _gate(account_id=_USER, claim_token="tok", expires_at=past)
    session = _session_decide(update_returns_id=None, diagnosis_gate=gate)
    mgr = HITLManager()
    with pytest.raises(ClaimTokenExpiredError):
        await mgr.deliver_manual(
            session,
            run_id=_RUN,
            gate_id=_GATE,
            org_id=_ORG,
            claim_token="tok",
            output={"data": "x"},
        )


async def test_deliver_manual_gate_not_found_raises():
    session = _session_decide(update_returns_id=None, diagnosis_gate=None)
    mgr = HITLManager()
    with pytest.raises(GateNotFoundError):
        await mgr.deliver_manual(
            session,
            run_id=_RUN,
            gate_id=_GATE,
            org_id=_ORG,
            claim_token="tok",
            output={"data": "x"},
        )


async def test_deliver_manual_already_decided_raises():
    gate = _gate(decision="approved")
    session = _session_decide(update_returns_id=None, diagnosis_gate=gate)
    mgr = HITLManager()
    with pytest.raises(GateAlreadyDecidedError):
        await mgr.deliver_manual(
            session,
            run_id=_RUN,
            gate_id=_GATE,
            org_id=_ORG,
            claim_token="tok",
            output={"data": "x"},
        )


async def test_deliver_manual_null_expires_at_raises_expired():
    """expires_at=None on a claimed gate (defensive guard) -> ClaimTokenExpiredError."""
    gate = _gate(account_id=_USER, claim_token="tok", expires_at=None)
    session = _session_decide(update_returns_id=None, diagnosis_gate=gate)
    mgr = HITLManager()
    with pytest.raises(ClaimTokenExpiredError):
        await mgr.deliver_manual(
            session,
            run_id=_RUN,
            gate_id=_GATE,
            org_id=_ORG,
            claim_token="tok",
            output={"data": "x"},
        )


# ---------------------------------------------------------------------------
# expire_stale
# ---------------------------------------------------------------------------


async def test_expire_stale_returns_expired_gates():
    session = AsyncMock()
    expired_result = MagicMock()
    expired_result.all.return_value = [(_RUN, "gate-a"), (_RUN, "gate-b")]
    session.execute = AsyncMock(return_value=expired_result)

    mgr = HITLManager()
    expired = await mgr.expire_stale(session, _ORG)
    assert len(expired) == 2
    assert expired[0] == {"run_id": _RUN, "gate_id": "gate-a"}
    assert expired[1] == {"run_id": _RUN, "gate_id": "gate-b"}


async def test_expire_stale_none_expired_returns_empty():
    session = AsyncMock()
    result = MagicMock()
    result.all.return_value = []
    session.execute = AsyncMock(return_value=result)

    mgr = HITLManager()
    expired = await mgr.expire_stale(session, _ORG)
    assert expired == []


# ---------------------------------------------------------------------------
# get_gate / list_pending
# ---------------------------------------------------------------------------


async def test_get_gate_returns_none_when_missing():
    session = _session_get(return_value=None)
    mgr = HITLManager()
    result = await mgr.get_gate(session, run_id=_RUN, gate_id=_GATE, org_id=_ORG)
    assert result is None


async def test_get_gate_returns_existing():
    gate = _gate()
    session = _session_get(return_value=gate)
    mgr = HITLManager()
    result = await mgr.get_gate(session, run_id=_RUN, gate_id=_GATE, org_id=_ORG)
    assert result is gate


async def test_list_pending_returns_unclaimed_gates():
    gate = _gate()
    session = AsyncMock()
    scalars = MagicMock()
    scalars.__iter__ = lambda self: iter([gate])
    execute_result = MagicMock()
    execute_result.scalars.return_value = scalars
    session.execute = AsyncMock(return_value=execute_result)

    mgr = HITLManager()
    result = await mgr.list_pending(session, _ORG)
    assert result == [gate]


# ---------------------------------------------------------------------------
# Overdue detection
# ---------------------------------------------------------------------------


async def test_list_overdue_returns_overdue_gates():
    from datetime import timedelta

    now = datetime.now(UTC)
    past = now - timedelta(minutes=45)
    gate = _gate(account_id=_USER, claim_token="tok", expires_at=past, claimed_at=past)

    session = AsyncMock()
    scalars = MagicMock()
    scalars.__iter__ = lambda self: iter([gate])
    execute_result = MagicMock()
    execute_result.scalars.return_value = scalars
    session.execute = AsyncMock(return_value=execute_result)

    mgr = HITLManager()
    overdue = await mgr.list_overdue(session, _ORG, threshold_minutes=30)
    assert len(overdue) == 1
    assert overdue[0]["gate_id"] == _GATE


async def test_list_overdue_below_threshold_returns_empty():

    # The DB WHERE clause (claimed_at < now - threshold) excludes the recent gate.
    # The mock simulates the DB returning no rows, as it would in production.
    session = AsyncMock()
    scalars = MagicMock()
    scalars.__iter__ = lambda self: iter([])
    execute_result = MagicMock()
    execute_result.scalars.return_value = scalars
    session.execute = AsyncMock(return_value=execute_result)

    mgr = HITLManager()
    overdue = await mgr.list_overdue(session, _ORG, threshold_minutes=30)
    assert overdue == []


async def test_count_overdue_returns_zero():
    session = AsyncMock()
    result = MagicMock()
    result.scalar.return_value = 0
    session.execute = AsyncMock(return_value=result)

    mgr = HITLManager()
    count = await mgr.count_overdue(session, _ORG, threshold_minutes=30)
    assert count == 0


# ---------------------------------------------------------------------------
# Executor integration — NodeInterrupt handling
# ---------------------------------------------------------------------------


def _mock_graph_validator() -> MagicMock:
    validation = MagicMock()
    validation.is_valid = True
    mock_cls = MagicMock()
    mock_cls.return_value.validate_for_run = AsyncMock(return_value=validation)
    return mock_cls


async def _bypass_capacity(
    mock_self: Any, *, run_id: Any, org_id: Any, pipeline_id: Any, max_concurrent: Any, lock_wait_seconds: Any
) -> Any:
    run = MagicMock()
    run.status = "running"
    return run


async def test_executor_sets_awaiting_human_on_node_interrupt():
    """When astream_events raises NodeInterrupt, the executor transitions run to awaiting_human."""
    from contextlib import asynccontextmanager

    from langgraph.errors import NodeInterrupt

    from modulo.core.pipeline_engine.executor import PipelineExecutor

    run = MagicMock()
    run.id = uuid.uuid4()
    run.pipeline_id = uuid.uuid4()
    run.snapshot_id = uuid.uuid4()
    run.langgraph_thread_id = str(uuid.uuid4())

    final_run = MagicMock()
    final_run.status = "awaiting_human"

    snapshot = MagicMock()
    snapshot.graph_json = {"nodes": [{"id": "a"}], "edges": []}
    snapshot.run_context_defaults = {}

    session = AsyncMock()
    begin_cm = AsyncMock()
    begin_cm.__aenter__ = AsyncMock(return_value=None)
    begin_cm.__aexit__ = AsyncMock(return_value=False)
    session.begin = MagicMock(return_value=begin_cm)
    scalar_result = MagicMock()
    scalar_result.scalar_one.return_value = snapshot
    session.execute = AsyncMock(return_value=scalar_result)

    @asynccontextmanager
    async def _ctx():
        yield session

    session_factory = MagicMock(side_effect=lambda: _ctx())

    async def _failing_stream(*args: Any, **kwargs: Any) -> Any:
        raise NodeInterrupt({"gate_id": "step-1"})
        yield  # pragma: no cover

    compiled = MagicMock()
    compiled.astream_events = _failing_stream

    broker = MagicMock()
    broker.publish = MagicMock()
    registry = MagicMock()
    registry.get_or_create.return_value = broker

    with (
        patch(
            "modulo.core.pipeline_engine.executor.async_sessionmaker",
            return_value=session_factory,
        ),
        patch("modulo.core.pipeline_engine.executor.get_run", return_value=run),
        patch("modulo.core.pipeline_engine.executor.update_run_status", return_value=final_run) as mock_update,
        patch("modulo.core.pipeline_engine.executor.set_rls_org"),
        patch("modulo.core.pipeline_engine.executor.get_or_compile", return_value=compiled),
        patch("modulo.core.pipeline_engine.executor.get_registry", return_value=registry),
        patch(
            "modulo.core.pipeline_engine.executor._checkpointer_scope",
            return_value=AsyncMock(),
        ),
        patch("modulo.core.pipeline_engine.executor.GraphValidator", new=_mock_graph_validator()),
        patch.object(PipelineExecutor, "_wait_for_capacity_or_fail", _bypass_capacity),
    ):
        executor = PipelineExecutor(MagicMock(), checkpointer_conn_string="a" * 32)
        result = await executor.execute(run_id=run.id, org_id=uuid.uuid4(), input_payload={})

    assert result is final_run
    final_update_call = mock_update.call_args_list[-1]
    assert final_update_call.args[2] == "awaiting_human"
