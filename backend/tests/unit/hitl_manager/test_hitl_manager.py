"""Unit tests for HITLManager using mocked AsyncSession."""

import asyncio
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from sqlalchemy.exc import IntegrityError

from modulo.core.hitl_manager import (
    AlreadyClaimedError,
    ClaimTokenExpiredError,
    ClaimTokenInvalidError,
    DecisionPayloadError,
    GateAlreadyDecidedError,
    GateNotFoundError,
    GateVanishedError,
    HITLError,
    HITLManager,
    NotTeamMemberError,
    RunNotAwaitingError,
)
from modulo.db.models.hitl_claim import HitlClaim
from modulo.db.models.team_membership import TeamMembership

from .conftest import _session_decide

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
    begin_nested_cm = AsyncMock()
    begin_nested_cm.__aenter__ = AsyncMock(return_value=None)
    begin_nested_cm.__aexit__ = AsyncMock(return_value=False)
    session.begin_nested = MagicMock(return_value=begin_nested_cm)
    return session


def _run_mock(status: str | None) -> MagicMock:
    """A runs-row mock for claim()'s org-scoped run-status SELECT (FAR-612)."""
    run = MagicMock()
    run.status = status
    return run


def _runs_result(status: str = "awaiting_human") -> MagicMock:
    """Execute-result for the run-status SELECT (scalar_one_or_none -> run mock)."""
    r = MagicMock()
    r.scalar_one_or_none.return_value = _run_mock(status)
    return r


def _is_runs_select(stmt: Any) -> bool:
    """Whether the statement SELECTs from ``runs`` (not ``hitl_claims``)."""
    sql = str(stmt)
    return "runs" in sql and "hitl_claims" not in sql


def _session_update(
    *,
    rows_returned: int = 1,
    gate: HitlClaim | None = None,
    pre_check_gate: HitlClaim | None = None,
    run_status: str | None = "awaiting_human",
) -> AsyncMock:
    """Session that simulates a claim() flow with pre-check + UPDATE + refetch.

    Call sequence:
      1. Pre-check SELECT (returns ``pre_check_gate`` or falls back to ``gate``)
      2. Run-status SELECT against ``runs`` (returns status ``run_status``;
         ``None`` simulates a missing run row)
      3. UPDATE … RETURNING  (returns claimed id if rows_returned > 0)
      4. Re-fetch SELECT     (returns ``gate``)

    The ``runs`` SELECT is dispatched by statement shape, not position, so the
    gate/response sequencing above is unaffected by where the run check fires.
    """
    session = AsyncMock()
    session.add = MagicMock()
    session.flush = AsyncMock()

    update_result = MagicMock()
    # For the claim() UPDATE RETURNING id
    update_result.scalar_one_or_none.return_value = uuid.uuid4() if rows_returned > 0 else None
    # For expire_stale() UPDATE RETURNING run_id, gate_id
    row = type("Row", (), {"run_id": _RUN, "gate_id": _GATE})
    update_result.all.return_value = [row()] * rows_returned

    run_result = MagicMock()
    run_result.scalar_one_or_none.return_value = _run_mock(run_status) if run_status is not None else None

    get_result = MagicMock()
    get_result.scalar_one_or_none.return_value = gate

    pre_check_result = MagicMock()
    pre_check_result.scalar_one_or_none.return_value = pre_check_gate

    gate_call_count = 0

    async def _execute(stmt: Any) -> Any:
        nonlocal gate_call_count
        if _is_runs_select(stmt):
            return run_result
        gate_call_count += 1
        # First gate execute is the pre-check SELECT; second is the UPDATE; third is the re-fetch
        if gate_call_count == 1:
            return pre_check_result if pre_check_gate is not None else get_result
        if gate_call_count == 2:
            return update_result
        return get_result

    session.execute = _execute
    session.get = AsyncMock(return_value=gate)
    begin_nested_cm = AsyncMock()
    begin_nested_cm.__aenter__ = AsyncMock(return_value=None)
    begin_nested_cm.__aexit__ = AsyncMock(return_value=False)
    session.begin_nested = MagicMock(return_value=begin_nested_cm)
    return session


def _session_decide_capture(update_returns_id: uuid.UUID | None, gate: HitlClaim | None) -> tuple[AsyncMock, list[Any]]:
    """Session mock capturing the UPDATE statement so tests can assert values.

    Mirrors ``_session_decide`` from conftest but records every statement
    passed to ``execute``. Returns ``(session, captured_stmts)``.
    """
    session = AsyncMock()
    session.add = MagicMock()
    session.flush = AsyncMock()

    update_result = MagicMock()
    update_result.scalar_one_or_none.return_value = update_returns_id
    diag_result = MagicMock()
    # Like conftest._session_decide, the post-UPDATE diagnosis SELECT returns
    # no chain head (None) so the audit logger's _get_chain_head_locked is a
    # no-op instead of tripping over a gate mock.
    diag_result.scalar_one_or_none.return_value = None
    captured: list[Any] = []
    call_count = 0

    async def _execute(stmt: Any) -> Any:
        nonlocal call_count
        call_count += 1
        captured.append(stmt)
        if call_count == 1:
            return update_result
        return diag_result

    session.execute = _execute
    session.get = AsyncMock(return_value=gate)
    begin_nested_cm = AsyncMock()
    begin_nested_cm.__aenter__ = AsyncMock(return_value=None)
    begin_nested_cm.__aexit__ = AsyncMock(return_value=False)
    session.begin_nested = MagicMock(return_value=begin_nested_cm)
    return session, captured


def _update_values(stmt: Any) -> dict[str, Any]:
    """Extract the UPDATE .values() dict from a captured SQLAlchemy statement."""
    return {col.name: expr.value for col, expr in stmt._values.items()}


def _assert_decode_scope_args(mock_decode: MagicMock) -> None:
    """Assert _decode_claim_jwt was called with the run/gate scope bound to the token."""
    mock_decode.assert_called_once()
    call_kwargs = mock_decode.call_args.kwargs
    assert call_kwargs["run_id"] == str(_RUN)
    assert call_kwargs["gate_id"] == _GATE
    assert mock_decode.call_args.args[0] == "aaa.bbb.ccc"
    assert mock_decode.call_args.args[1] == "test-secret-key-with-enough-length"


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


async def test_create_gate_integrity_error_returns_existing_row():
    """A concurrent insert racing our own insert falls back to the existing row."""
    existing = _gate()
    session = AsyncMock()
    session.add = MagicMock()
    session.flush = AsyncMock(side_effect=IntegrityError("stmt", {}, Exception("duplicate key")))

    calls = 0

    async def _execute(stmt: Any) -> Any:
        nonlocal calls
        calls += 1
        # First _get() finds nothing (we try to insert), second finds the winner
        r = MagicMock()
        r.scalar_one_or_none.return_value = existing if calls > 1 else None
        return r

    session.execute = _execute
    begin_nested_cm = AsyncMock()
    begin_nested_cm.__aenter__ = AsyncMock(return_value=None)
    begin_nested_cm.__aexit__ = AsyncMock(return_value=False)
    session.begin_nested = MagicMock(return_value=begin_nested_cm)

    mgr = HITLManager()
    result = await mgr.create_gate(session, run_id=_RUN, gate_id=_GATE, pipeline_id=_PIPELINE, org_id=_ORG)
    assert result is existing


async def test_create_gate_integrity_error_lost_race_raises():
    """If the concurrent winner vanishes between our insert and re-fetch, raise."""
    session = AsyncMock()
    session.add = MagicMock()
    session.flush = AsyncMock(side_effect=IntegrityError("stmt", {}, Exception("duplicate key")))

    async def _execute(stmt: Any) -> Any:
        r = MagicMock()
        r.scalar_one_or_none.return_value = None
        return r

    session.execute = _execute
    begin_nested_cm = AsyncMock()
    begin_nested_cm.__aenter__ = AsyncMock(return_value=None)
    begin_nested_cm.__aexit__ = AsyncMock(return_value=False)
    session.begin_nested = MagicMock(return_value=begin_nested_cm)

    mgr = HITLManager()
    with pytest.raises(RuntimeError, match="Concurrent gate creation lost race"):
        await mgr.create_gate(session, run_id=_RUN, gate_id=_GATE, pipeline_id=_PIPELINE, org_id=_ORG)


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


async def test_claim_success_emits_hitl_claimed_audit():
    """A successful claim fires the PRD §8.12 ``hitl_claimed`` audit event.

    The audit event records the actor, the claim resource, and the run/gate the
    claim targets — the acquisition side of the lifecycle that previously only
    had the ``hitl.claim_expired`` event (the claim itself was never audited).
    """
    pre_check = _gate(account_id=None)
    claimed_gate = _gate(
        account_id=_USER,
        claim_token="tok",
        expires_at=datetime.now(UTC) + timedelta(minutes=15),
    )
    session = _session_update(rows_returned=1, gate=claimed_gate, pre_check_gate=pre_check)
    mgr = HITLManager()
    audit = AsyncMock(return_value=MagicMock())
    with patch("modulo.core.hitl_manager.append_audit_event", new=audit):
        result = await mgr.claim(session, run_id=_RUN, gate_id=_GATE, org_id=_ORG, claimant_id=_USER)

    assert result is claimed_gate
    audit.assert_awaited_once()
    kwargs = audit.await_args.kwargs
    assert kwargs["event_type"] == "hitl_claimed"
    assert kwargs["org_id"] == _ORG
    assert kwargs["actor_user_id"] == _USER
    assert kwargs["resource_type"] == "hitl_claim"
    assert kwargs["payload_json"]["pipeline_run_id"] == str(_RUN)
    assert kwargs["payload_json"]["node_id"] == _GATE
    assert kwargs["payload_json"]["team_id"] is None
    assert kwargs["payload_json"]["expiry_minutes"] == 15


async def test_claim_audit_failure_does_not_block_claim():
    """A broken audit append must not fail the claim (failure isolation)."""
    pre_check = _gate(account_id=None)
    claimed_gate = _gate(
        account_id=_USER,
        claim_token="tok",
        expires_at=datetime.now(UTC) + timedelta(minutes=15),
    )
    session = _session_update(rows_returned=1, gate=claimed_gate, pre_check_gate=pre_check)
    mgr = HITLManager()

    async def _raise_audit(*_a: Any, **_k: Any) -> Any:
        raise RuntimeError("audit boom")

    with patch("modulo.core.hitl_manager.append_audit_event", side_effect=_raise_audit):
        result = await mgr.claim(session, run_id=_RUN, gate_id=_GATE, org_id=_ORG, claimant_id=_USER)
    assert result is claimed_gate


async def test_claim_with_secret_key_uses_jwt_token():
    """claim() with a configured secret_key mints a signed JWT claim token."""
    pre_check = _gate(account_id=None)
    claimed_gate = _gate(
        account_id=_USER,
        claim_token="signed.token.value",
        expires_at=datetime.now(UTC) + timedelta(minutes=15),
    )
    session = _session_update(rows_returned=1, gate=claimed_gate, pre_check_gate=pre_check)
    mgr = HITLManager(secret_key="test-secret-key-with-enough-length")

    with patch("modulo.core.hitl_manager._create_claim_jwt", return_value="signed.token.value") as mock_jwt:
        result = await mgr.claim(session, run_id=_RUN, gate_id=_GATE, org_id=_ORG, claimant_id=_USER)

    assert result is claimed_gate
    mock_jwt.assert_called_once()
    call_kwargs = mock_jwt.call_args.kwargs
    assert call_kwargs["run_id"] == str(_RUN)
    assert call_kwargs["gate_id"] == _GATE
    assert call_kwargs["client_id"] == str(_USER)
    assert call_kwargs["expiry_minutes"] == 15


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


def _session_race(
    *,
    pre_check_gate: HitlClaim | None,
    race_gate: HitlClaim | None,
    pre_check_run_status: str = "awaiting_human",
    race_run_status: str = "awaiting_human",
    update_rows: int = 0,
) -> AsyncMock:
    """Session mock for claim()'s race-window re-read (FAR-645).

    Simulates a claim where the atomic UPDATE matched 0 rows (``update_rows=0``)
    and the manager re-reads the run and the gate to name the real cause. The
    pre-check run-status SELECT and the race-window run SELECT can differ, so
    the pre-check can pass on an ``awaiting_human`` run while the re-read sees a
    terminal run that the EXISTS predicate refused to claim.
    """
    session = AsyncMock()
    session.add = MagicMock()
    session.flush = AsyncMock()

    update_result = MagicMock()
    update_result.scalar_one_or_none.return_value = uuid.uuid4() if update_rows > 0 else None

    pre_check_result = MagicMock()
    pre_check_result.scalar_one_or_none.return_value = pre_check_gate

    pre_check_run_result = MagicMock()
    pre_check_run_result.scalar_one_or_none.return_value = _run_mock(pre_check_run_status)

    race_run_result = MagicMock()
    race_run_result.scalar_one_or_none.return_value = _run_mock(race_run_status)

    race_gate_result = MagicMock()
    race_gate_result.scalar_one_or_none.return_value = race_gate

    runs_calls = 0
    gate_calls = 0

    async def _execute(stmt: Any) -> Any:
        nonlocal runs_calls, gate_calls
        if _is_runs_select(stmt):
            runs_calls += 1
            return pre_check_run_result if runs_calls == 1 else race_run_result
        gate_calls += 1
        if gate_calls == 1:
            return pre_check_result
        if gate_calls == 2:
            return update_result
        return race_gate_result

    session.execute = _execute
    session.get = AsyncMock(return_value=race_gate or pre_check_gate)
    begin_nested_cm = AsyncMock()
    begin_nested_cm.__aenter__ = AsyncMock(return_value=None)
    begin_nested_cm.__aexit__ = AsyncMock(return_value=False)
    session.begin_nested = MagicMock(return_value=begin_nested_cm)
    return session


async def test_claim_race_run_went_terminal_raises_run_not_awaiting():
    """FAR-645: UPDATE matched 0 rows because the run went terminal in the
    window; the re-read must distinguish this from a generic already-claimed and
    raise RunNotAwaitingError naming the terminal status."""
    pre_check = _gate(account_id=None)
    session = _session_race(
        pre_check_gate=pre_check,
        race_gate=pre_check,
        pre_check_run_status="awaiting_human",
        race_run_status="completed",
    )
    mgr = HITLManager()
    with pytest.raises(RunNotAwaitingError):
        await mgr.claim(session, run_id=_RUN, gate_id=_GATE, org_id=_ORG, claimant_id=_USER)


async def test_claim_race_gate_decided_raises_gate_already_decided():
    """UPDATE matched 0 rows because the gate was DECIDED in the window; the
    re-read must raise GateAlreadyDecidedError (not AlreadyClaimedError)."""
    pre_check = _gate(account_id=None)
    decided_gate = _gate(account_id=None, decision="approved")
    session = _session_race(
        pre_check_gate=pre_check,
        race_gate=decided_gate,
        pre_check_run_status="awaiting_human",
        race_run_status="awaiting_human",
    )
    mgr = HITLManager()
    with pytest.raises(GateAlreadyDecidedError):
        await mgr.claim(session, run_id=_RUN, gate_id=_GATE, org_id=_ORG, claimant_id=_USER)


async def test_claim_race_another_account_claimed_raises_already_claimed():
    """UPDATE matched 0 rows because another operator claimed the gate; the
    re-read must raise AlreadyClaimedError (the pre-check passed because at
    pre-check time the gate was still unclaimed)."""
    pre_check = _gate(account_id=None)
    other_claimed = _gate(account_id=uuid.uuid4())
    session = _session_race(
        pre_check_gate=pre_check,
        race_gate=other_claimed,
        pre_check_run_status="awaiting_human",
        race_run_status="awaiting_human",
    )
    mgr = HITLManager()
    with pytest.raises(AlreadyClaimedError):
        await mgr.claim(session, run_id=_RUN, gate_id=_GATE, org_id=_ORG, claimant_id=_USER)


async def test_claim_race_gate_vanished_raises_gate_not_found():
    """UPDATE matched 0 rows because the gate row disappeared in the window;
    the re-read must raise GateNotFoundError."""
    pre_check = _gate(account_id=None)
    session = _session_race(
        pre_check_gate=pre_check,
        race_gate=None,
        pre_check_run_status="awaiting_human",
        race_run_status="awaiting_human",
    )
    mgr = HITLManager()
    with pytest.raises(GateNotFoundError):
        await mgr.claim(session, run_id=_RUN, gate_id=_GATE, org_id=_ORG, claimant_id=_USER)


async def test_claim_custom_expiry_minutes_is_applied():
    """claim() with custom expiry_minutes binds a matching expires_at to the UPDATE."""
    pre_check = _gate(account_id=None)
    claimed_gate = _gate(
        account_id=_USER,
        claim_token="tok",
        expires_at=datetime.now(UTC) + timedelta(minutes=60),
    )
    captured: list[Any] = []

    session = AsyncMock()
    session.add = MagicMock()
    session.flush = AsyncMock()

    update_result = MagicMock()
    update_result.scalar_one_or_none.return_value = uuid.uuid4()
    get_result = MagicMock()
    get_result.scalar_one_or_none.return_value = pre_check
    pre_check_result = MagicMock()
    pre_check_result.scalar_one_or_none.return_value = pre_check
    run_result = MagicMock()
    run_result.scalar_one_or_none.return_value = _run_mock("awaiting_human")
    gate_call_count = 0

    async def _execute(stmt: Any) -> Any:
        nonlocal gate_call_count
        if _is_runs_select(stmt):
            return run_result
        gate_call_count += 1
        if gate_call_count == 1:
            return pre_check_result
        if gate_call_count == 2:
            captured.append(stmt)
            return update_result
        return get_result

    session.execute = _execute
    session.get = AsyncMock(return_value=claimed_gate)
    begin_nested_cm = AsyncMock()
    begin_nested_cm.__aenter__ = AsyncMock(return_value=None)
    begin_nested_cm.__aexit__ = AsyncMock(return_value=False)
    session.begin_nested = MagicMock(return_value=begin_nested_cm)

    mgr = HITLManager()
    result = await mgr.claim(session, run_id=_RUN, gate_id=_GATE, org_id=_ORG, claimant_id=_USER, expiry_minutes=60)
    assert result is claimed_gate

    assert len(captured) == 1
    expires_at = next(expr.value for col, expr in captured[0]._values.items() if col.name == "expires_at")
    remaining = expires_at - datetime.now(UTC)
    assert timedelta(minutes=59, seconds=55) < remaining <= timedelta(minutes=60, seconds=5)


async def test_claim_non_positive_expiry_raises():
    """claim() with a non-positive expiry_minutes is rejected before touching the DB."""
    session = _session_update(rows_returned=1, gate=None, pre_check_gate=None)
    mgr = HITLManager()
    with pytest.raises(HITLError, match="expiry_minutes must be positive"):
        await mgr.claim(session, run_id=_RUN, gate_id=_GATE, org_id=_ORG, claimant_id=_USER, expiry_minutes=0)


async def test_claim_already_decided_raises():
    """claim() on a gate that already has a decision raises GateAlreadyDecidedError."""
    decided = _gate(decision="approved")
    session = _session_update(rows_returned=0, gate=decided, pre_check_gate=decided)
    mgr = HITLManager()
    with pytest.raises(GateAlreadyDecidedError):
        await mgr.claim(session, run_id=_RUN, gate_id=_GATE, org_id=_ORG, claimant_id=_USER)


async def test_claim_gate_vanished_after_update_raises():
    """claim() whose gate row disappears after the UPDATE raises GateVanishedError."""
    unclaimed = _gate(account_id=None)
    session = _session_update(rows_returned=1, gate=None, pre_check_gate=unclaimed)
    mgr = HITLManager()
    with pytest.raises(GateVanishedError):
        await mgr.claim(session, run_id=_RUN, gate_id=_GATE, org_id=_ORG, claimant_id=_USER)


async def test_claim_update_race_raises():
    """claim() whose atomic UPDATE returns no id (concurrent claim) raises AlreadyClaimedError."""
    unclaimed = _gate(account_id=None)
    session = _session_update(rows_returned=0, gate=unclaimed, pre_check_gate=unclaimed)
    mgr = HITLManager()
    with pytest.raises(AlreadyClaimedError):
        await mgr.claim(session, run_id=_RUN, gate_id=_GATE, org_id=_ORG, claimant_id=_USER)


async def test_claim_update_statement_carries_run_status_exists_predicate():
    """FAR-645: the atomic claim UPDATE folds the run-status guard INTO its
    WHERE clause.

    The gate-columns-only UPDATE left a ms-window where a run going terminal
    between the pre-check SELECT and the write still got claimed — stranding a
    claimed gate on a terminal run. The compiled statement must carry an
    EXISTS predicate over ``runs`` restricted to the claimable statuses
    (awaiting_human + hitl_parked, mirroring the pre-check) so the refusal is
    atomic with the claim.
    """
    pre_check = _gate(account_id=None)
    claimed_gate = _gate(account_id=_USER, claim_token="tok")
    captured: list[Any] = []

    session = AsyncMock()
    session.add = MagicMock()
    session.flush = AsyncMock()

    update_result = MagicMock()
    update_result.scalar_one_or_none.return_value = uuid.uuid4()
    pre_check_result = MagicMock()
    pre_check_result.scalar_one_or_none.return_value = pre_check
    run_result = MagicMock()
    run_result.scalar_one_or_none.return_value = _run_mock("awaiting_human")

    gate_call_count = 0

    async def _execute(stmt: Any) -> Any:
        nonlocal gate_call_count
        if _is_runs_select(stmt):
            return run_result
        gate_call_count += 1
        if gate_call_count == 1:
            return pre_check_result
        if gate_call_count == 2:
            captured.append(stmt)
        return update_result

    session.execute = _execute
    session.get = AsyncMock(return_value=claimed_gate)
    begin_nested_cm = AsyncMock()
    begin_nested_cm.__aenter__ = AsyncMock(return_value=None)
    begin_nested_cm.__aexit__ = AsyncMock(return_value=False)
    session.begin_nested = MagicMock(return_value=begin_nested_cm)

    mgr = HITLManager()
    await mgr.claim(session, run_id=_RUN, gate_id=_GATE, org_id=_ORG, claimant_id=_USER)

    assert len(captured) == 1
    update_sql = str(captured[0].compile(compile_kwargs={"literal_binds": True})).lower()
    assert "exists" in update_sql
    assert "runs" in update_sql
    assert "awaiting_human" in update_sql
    assert "hitl_parked" in update_sql


async def test_claim_update_race_on_terminal_run_raises_run_not_awaiting():
    """FAR-645: when the atomic UPDATE refuses the claim because the run went
    terminal inside the pre-check→write window, the error names the run's
    actual status (RunNotAwaitingError) instead of a generic already-claimed."""
    pre_check = _gate(account_id=None)

    session = AsyncMock()
    session.add = MagicMock()
    session.flush = AsyncMock()

    update_result = MagicMock()
    update_result.scalar_one_or_none.return_value = None
    pre_check_result = MagicMock()
    pre_check_result.scalar_one_or_none.return_value = pre_check
    # First runs-SELECT (pre-check) sees awaiting_human; the race re-read
    # after the refused UPDATE sees the run already terminal.
    run_results = [MagicMock() for _ in range(2)]
    for r, status in zip(run_results, ["awaiting_human", "complete"], strict=True):
        r.scalar_one_or_none.return_value = _run_mock(status)

    run_calls = 0
    gate_calls = 0

    async def _execute(stmt: Any) -> Any:
        nonlocal run_calls, gate_calls
        if _is_runs_select(stmt):
            run_calls += 1
            return run_results[run_calls - 1]
        gate_calls += 1
        if gate_calls == 1:
            return pre_check_result
        return update_result

    session.execute = _execute
    session.get = AsyncMock(return_value=pre_check)
    begin_nested_cm = AsyncMock()
    begin_nested_cm.__aenter__ = AsyncMock(return_value=None)
    begin_nested_cm.__aexit__ = AsyncMock(return_value=False)
    session.begin_nested = MagicMock(return_value=begin_nested_cm)

    mgr = HITLManager()
    with pytest.raises(RunNotAwaitingError, match="status: complete"):
        await mgr.claim(session, run_id=_RUN, gate_id=_GATE, org_id=_ORG, claimant_id=_USER)


async def test_claim_update_race_on_decided_gate_raises_gate_already_decided():
    """FAR-645: when the atomic UPDATE refuses the claim because the gate was
    DECIDED inside the pre-check→write window (while the run stayed claimable),
    the error names the decision (GateAlreadyDecidedError) instead of a generic
    already-claimed -- the typed problem the frontend maps to its
    already-decided banner."""
    pre_check = _gate(account_id=None)
    decided = _gate(account_id=_USER, decision="approved")

    session = AsyncMock()
    session.add = MagicMock()
    session.flush = AsyncMock()

    update_result = MagicMock()
    update_result.scalar_one_or_none.return_value = None
    pre_check_result = MagicMock()
    pre_check_result.scalar_one_or_none.return_value = pre_check
    # Both runs-SELECTs (pre-check + race re-read) see the run still claimable:
    # the UPDATE was refused by the gate columns, not the run status.
    run_result = MagicMock()
    run_result.scalar_one_or_none.return_value = _run_mock("awaiting_human")
    decided_result = MagicMock()
    decided_result.scalar_one_or_none.return_value = decided

    run_calls = 0
    gate_calls = 0

    async def _execute(stmt: Any) -> Any:
        nonlocal run_calls, gate_calls
        if _is_runs_select(stmt):
            run_calls += 1
            return run_result
        gate_calls += 1
        if gate_calls == 1:
            return pre_check_result
        if gate_calls == 2:
            return update_result
        # Third gate statement is the race re-read: the concurrent operator's
        # decision is now visible.
        return decided_result

    session.execute = _execute
    session.get = AsyncMock(return_value=pre_check)
    begin_nested_cm = AsyncMock()
    begin_nested_cm.__aenter__ = AsyncMock(return_value=None)
    begin_nested_cm.__aexit__ = AsyncMock(return_value=False)
    session.begin_nested = MagicMock(return_value=begin_nested_cm)

    mgr = HITLManager()
    with pytest.raises(GateAlreadyDecidedError):
        await mgr.claim(session, run_id=_RUN, gate_id=_GATE, org_id=_ORG, claimant_id=_USER)


@pytest.mark.parametrize("run_status", ["complete", "failed", "running", "cancelled"])
async def test_claim_on_non_awaiting_run_raises(run_status: str):
    """FAR-612: claiming a gate whose run is not awaiting_human is refused.

    The route maps this to 409 with the run's actual status in the detail, so
    a stale/orphaned gate can never flip a terminal run to "claimed".
    """
    gate = _gate(account_id=None)
    session = _session_update(rows_returned=1, gate=gate, pre_check_gate=gate, run_status=run_status)
    mgr = HITLManager()
    with pytest.raises(RunNotAwaitingError, match=f"status: {run_status}"):
        await mgr.claim(session, run_id=_RUN, gate_id=_GATE, org_id=_ORG, claimant_id=_USER)


async def test_claim_on_parked_run_succeeds():
    """FAR-604/FAR-612: a gate on a ``hitl_parked`` run is actionable and claimable.

    ``hitl_parked`` is in ``HITL_ACTIONABLE_RUN_STATUSES`` (deliberately — parked
    runs' gates stay listed), so claim() must succeed exactly as on ``awaiting_human``
    rather than raising ``RunNotAwaitingError``.
    """
    pre_check = _gate(account_id=None)
    claimed_gate = _gate(
        account_id=_USER,
        claim_token="tok",
        expires_at=datetime.now(UTC) + timedelta(minutes=15),
    )
    session = _session_update(rows_returned=1, gate=claimed_gate, pre_check_gate=pre_check, run_status="hitl_parked")
    mgr = HITLManager()
    result = await mgr.claim(session, run_id=_RUN, gate_id=_GATE, org_id=_ORG, claimant_id=_USER)
    assert result is claimed_gate


async def test_claim_when_run_row_missing_raises_gate_not_found():
    """An undecided gate whose run row is gone (org-scoped lookup misses) is 404 data, not claimable."""
    gate = _gate(account_id=None)
    session = _session_update(rows_returned=1, gate=gate, pre_check_gate=gate, run_status=None)
    mgr = HITLManager()
    with pytest.raises(GateNotFoundError):
        await mgr.claim(session, run_id=_RUN, gate_id=_GATE, org_id=_ORG, claimant_id=_USER)


async def test_claim_run_lookup_is_org_scoped():
    """FAR-612: the run-status SELECT filters by organisation_id (never probes another org's run)."""
    gate = _gate(account_id=None)
    session = _session_update(rows_returned=1, gate=gate, pre_check_gate=gate)
    run_select: list[Any] = []

    original_execute = session.execute

    async def _capture(stmt: Any) -> Any:
        if _is_runs_select(stmt):
            run_select.append(stmt)
        return await original_execute(stmt)

    session.execute = _capture
    mgr = HITLManager()
    await mgr.claim(session, run_id=_RUN, gate_id=_GATE, org_id=_ORG, claimant_id=_USER)
    assert len(run_select) == 1
    sql = str(run_select[0].compile(compile_kwargs={"literal_binds": True}))
    assert "organisation_id" in sql


async def test_claim_after_expiry_reset_succeeds():
    """Re-claim works after claim expiry: the expiry sweep resets the gate to
    unclaimed AND the run back to ``awaiting_human`` (hitl_manager.expiry_job
    step 4), so the FAR-612 run-status precondition holds again."""
    reset_gate = _gate(account_id=None)  # post-expiry state written by expire_stale_claims
    claimed = _gate(
        account_id=_USER,
        claim_token="tok",
        expires_at=datetime.now(UTC) + timedelta(minutes=15),
    )
    session = _session_update(rows_returned=1, gate=claimed, pre_check_gate=reset_gate, run_status="awaiting_human")
    mgr = HITLManager()
    result = await mgr.claim(session, run_id=_RUN, gate_id=_GATE, org_id=_ORG, claimant_id=_USER)
    assert result is claimed


# ---------------------------------------------------------------------------
# claim — same-account re-claim (FAR-686)
# ---------------------------------------------------------------------------


async def test_claim_same_account_reclaim_issues_fresh_token():
    """FAR-686: a reviewer who reloaded the page lost their claim token (it
    lives only in frontend state) — the SAME account may re-claim, which
    re-issues a fresh token and refreshes claimed_at/expires_at while the
    account stays unchanged."""
    old_claimed_at = datetime.now(UTC) - timedelta(minutes=10)
    held = _gate(
        account_id=_USER,
        claim_token="stale-token",
        claimed_at=old_claimed_at,
        expires_at=datetime.now(UTC) + timedelta(minutes=5),
    )
    reclaimed = _gate(
        account_id=_USER,
        claim_token="fresh-token",
        claimed_at=datetime.now(UTC),
        expires_at=datetime.now(UTC) + timedelta(minutes=15),
    )
    session = AsyncMock()
    session.add = MagicMock()
    session.flush = AsyncMock()
    captured: list[Any] = []

    async def _execute(stmt: Any) -> Any:
        captured.append(stmt)
        r = MagicMock()
        if _is_runs_select(stmt):
            # FAR-612 run-status check — the run still awaits a human
            r.scalar_one_or_none.return_value = _run_mock("awaiting_human")
        elif len(captured) == 1:
            # Gate pre-check SELECT — the gate is held by the SAME account
            r.scalar_one_or_none.return_value = held
        else:
            # Claim UPDATE ... RETURNING and any later reads
            r.scalar_one_or_none.return_value = uuid.uuid4()
        return r

    session.execute = _execute
    session.get = AsyncMock(return_value=reclaimed)
    begin_nested_cm = AsyncMock()
    begin_nested_cm.__aenter__ = AsyncMock(return_value=None)
    begin_nested_cm.__aexit__ = AsyncMock(return_value=False)
    session.begin_nested = MagicMock(return_value=begin_nested_cm)

    mgr = HITLManager()
    with patch("modulo.core.hitl_manager.append_audit_event", new=AsyncMock()):
        result = await mgr.claim(session, run_id=_RUN, gate_id=_GATE, org_id=_ORG, claimant_id=_USER)

    assert result is reclaimed
    assert result.account_id == _USER
    assert result.claim_token != "stale-token"

    # Gate statements in ``captured``: pre-check SELECT, claim UPDATE
    # (the runs SELECT is dispatched above and never stored). FAR-612's
    # run-status check fires between them, so the UPDATE is captured[2].
    update_stmt = next(s for s in captured if hasattr(s, "_values"))
    assert len(captured) >= 3
    values = _update_values(update_stmt)
    assert values["account_id"] == _USER
    assert values["claim_token"] != "stale-token"
    assert values["claimed_at"] is not None
    assert values["claimed_at"] > old_claimed_at
    assert values["expires_at"] - values["claimed_at"] == timedelta(minutes=15)


async def test_claim_same_account_reclaim_team_scoped_gate():
    """Same-account re-claim also works on a team-scoped gate — membership is
    re-checked on both sides of the claim UPDATE as usual (FAR-686)."""
    held = _gate(account_id=_USER, claim_token="old-token", required_team_id=_TEAM)
    reclaimed = _gate(account_id=_USER, claim_token="new-token", required_team_id=_TEAM)
    membership = MagicMock(spec=TeamMembership)
    membership.team_id = _TEAM
    membership.account_id = _USER

    session = AsyncMock()
    session.add = MagicMock()
    session.flush = AsyncMock()
    gate_call_no = 0

    async def _execute(stmt: Any) -> Any:
        nonlocal gate_call_no
        if _is_runs_select(stmt):
            # FAR-612 run-status check — the run still awaits a human
            r_run = MagicMock()
            r_run.scalar_one_or_none.return_value = _run_mock("awaiting_human")
            return r_run
        gate_call_no += 1
        r = MagicMock()
        if gate_call_no in (1, 2):
            # Gate pre-check SELECT + FOR UPDATE row lock (same-account hold passes)
            r.scalar_one_or_none.return_value = held
        elif gate_call_no in (3, 5):
            # Membership pre-check + post-claim TOCTOU re-verification
            r.scalar_one_or_none.return_value = membership
        else:
            # Claim UPDATE
            r.scalar_one_or_none.return_value = uuid.uuid4()
        return r

    session.execute = _execute
    session.get = AsyncMock(return_value=reclaimed)
    mgr = HITLManager()
    with patch("modulo.core.hitl_manager.append_audit_event", new=AsyncMock()):
        result = await mgr.claim(session, run_id=_RUN, gate_id=_GATE, org_id=_ORG, claimant_id=_USER)

    assert result is reclaimed
    assert result.claim_token == "new-token"


async def test_claim_same_account_reclaim_on_claimed_run_succeeds():
    """FAR-686, real lifecycle: the claim route flips the run to ``claimed``
    via update_run_status on every successful claim, so during the
    claimed-but-undecided window the run status IS ``claimed`` — a page
    reload's re-claim must still succeed for the SAME account (token
    recovery), not raise RunNotAwaitingError."""
    old_claimed_at = datetime.now(UTC) - timedelta(minutes=10)
    held = _gate(
        account_id=_USER,
        claim_token="stale-token",
        claimed_at=old_claimed_at,
        expires_at=datetime.now(UTC) + timedelta(minutes=5),
    )
    reclaimed = _gate(
        account_id=_USER,
        claim_token="fresh-token",
        claimed_at=datetime.now(UTC),
        expires_at=datetime.now(UTC) + timedelta(minutes=15),
    )
    session = AsyncMock()
    session.add = MagicMock()
    session.flush = AsyncMock()
    captured: list[Any] = []

    async def _execute(stmt: Any) -> Any:
        captured.append(stmt)
        r = MagicMock()
        if _is_runs_select(stmt):
            # The run is ``claimed`` because this very claim flipped it
            r.scalar_one_or_none.return_value = _run_mock("claimed")
        elif len(captured) == 1:
            # Gate pre-check SELECT — held by the SAME account
            r.scalar_one_or_none.return_value = held
        else:
            # Claim UPDATE ... RETURNING and any later reads
            r.scalar_one_or_none.return_value = uuid.uuid4()
        return r

    session.execute = _execute
    session.get = AsyncMock(return_value=reclaimed)
    begin_nested_cm = AsyncMock()
    begin_nested_cm.__aenter__ = AsyncMock(return_value=None)
    begin_nested_cm.__aexit__ = AsyncMock(return_value=False)
    session.begin_nested = MagicMock(return_value=begin_nested_cm)

    mgr = HITLManager()
    with patch("modulo.core.hitl_manager.append_audit_event", new=AsyncMock()):
        result = await mgr.claim(session, run_id=_RUN, gate_id=_GATE, org_id=_ORG, claimant_id=_USER)

    assert result is reclaimed
    assert result.account_id == _USER
    assert result.claim_token != "stale-token"

    update_stmt = next(s for s in captured if hasattr(s, "_values"))
    values = _update_values(update_stmt)
    assert values["account_id"] == _USER
    assert values["claim_token"] != "stale-token"
    assert values["claimed_at"] > old_claimed_at


async def test_claim_cross_account_on_claimed_run_still_blocked():
    """FAR-686 composition: on a run already in ``claimed`` status, a
    DIFFERENT account is still blocked — the claimed-by-other check fires
    BEFORE the run-status check, so the widened ``claimed`` arm can never be
    reached by a cross-account claimant."""
    existing = _gate(account_id=uuid.uuid4(), claim_token="tok")
    session = _session_update(rows_returned=0, gate=existing, pre_check_gate=existing, run_status="claimed")
    mgr = HITLManager()
    with pytest.raises(AlreadyClaimedError):
        await mgr.claim(session, run_id=_RUN, gate_id=_GATE, org_id=_ORG, claimant_id=_USER)


@pytest.mark.parametrize("run_status", ["complete", "failed", "running", "cancelled"])
async def test_claim_same_account_on_terminal_run_still_blocked(run_status: str):
    """FAR-686 composition: the FAR-612 data-rot guard survives for every
    non-claimed status — even a same-account claimant cannot claim a gate on
    a terminal (or still-executing) run, so the widened arm cannot corrupt a
    finished run."""
    held = _gate(account_id=_USER, claim_token="tok")
    session = _session_update(rows_returned=1, gate=held, pre_check_gate=held, run_status=run_status)
    mgr = HITLManager()
    with pytest.raises(RunNotAwaitingError, match=f"status: {run_status}"):
        await mgr.claim(session, run_id=_RUN, gate_id=_GATE, org_id=_ORG, claimant_id=_USER)


async def test_claim_cross_account_on_claimed_gate_raises():
    """FAR-686 keeps the cross-account guard: another account's live claim is
    never overridden — AlreadyClaimedError still fires for a different user."""
    existing = _gate(account_id=uuid.uuid4(), claim_token="tok")
    session = _session_update(rows_returned=0, gate=existing, pre_check_gate=existing)
    mgr = HITLManager()
    with pytest.raises(AlreadyClaimedError):
        await mgr.claim(session, run_id=_RUN, gate_id=_GATE, org_id=_ORG, claimant_id=_USER)


async def test_claim_update_where_allows_unclaimed_or_same_account_only():
    """The claim UPDATE's WHERE admits unclaimed gates and same-account
    re-claims (FAR-686) but still requires the gate to be undecided."""
    _mgr, _session, captured = await _claim_capture()
    update_stmt = captured[
        2
    ]  # execute calls: pre-check SELECT, run-status SELECT, then the UPDATE ... RETURNING (FAR-612)
    sql = str(update_stmt.compile())
    assert "account_id IS NULL" in sql
    assert "decision IS NULL" in sql
    bound = {str(v) for v in update_stmt.compile().params.values()}
    assert str(_USER) in bound, "same-account re-claim arm missing from the claim UPDATE WHERE"


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
    team_check_count = 0

    async def _execute(stmt: Any) -> Any:
        nonlocal call_no, team_check_count
        if _is_runs_select(stmt):
            return _runs_result()
        call_no += 1
        if call_no == 1:
            # Pre-check SELECT
            r = MagicMock()
            r.scalar_one_or_none.return_value = unclaimed
            return r
        if call_no == 2:
            # FOR UPDATE row lock
            r = MagicMock()
            r.scalar_one_or_none.return_value = unclaimed
            return r
        if call_no == 3:
            # Team membership check before claiming
            team_check_count += 1
            r = MagicMock()
            r.scalar_one_or_none.return_value = membership
            return r
        if call_no == 4:
            # UPDATE
            r = MagicMock()
            r.scalar_one_or_none.return_value = uuid.uuid4()
            return r
        if call_no == 5:
            # Re-check membership after claiming to close the TOCTOU window
            team_check_count += 1
            r = MagicMock()
            r.scalar_one_or_none.return_value = membership
            return r
        raise AssertionError(f"Unexpected execute call #{call_no}")

    session.execute = _execute
    session.get = AsyncMock(return_value=claimed)
    mgr = HITLManager()
    result = await mgr.claim(session, run_id=_RUN, gate_id=_GATE, org_id=_ORG, claimant_id=_USER)
    assert result is claimed
    assert team_check_count == 2


async def test_claim_team_membership_query_restricts_to_runner_or_operator_role():
    """Both team-membership checks restrict claims to ``runner``/``operator`` team roles.

    A team-scoped gate is a decision surface — a ``viewer`` membership grants
    read-only visibility and must never satisfy the claim-time membership check.
    The claim queries (pre-check AND the post-claim TOCTOU re-verification) must
    carry ``role IN ('runner', 'operator')`` in the WHERE clause.
    """
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
    membership.role = "operator"

    session = AsyncMock()
    membership_stmts: list[Any] = []
    call_no = 0

    async def _execute(stmt: Any) -> Any:
        nonlocal call_no
        if _is_runs_select(stmt):
            return _runs_result()
        call_no += 1
        if call_no in (1, 2):
            # Gate pre-check SELECT + FOR UPDATE row lock
            r = MagicMock()
            r.scalar_one_or_none.return_value = unclaimed
            return r
        if call_no in (3, 5):
            # Membership pre-check + post-claim TOCTOU re-verification
            membership_stmts.append(stmt)
            r = MagicMock()
            r.scalar_one_or_none.return_value = membership
            return r
        if call_no == 4:
            # Claim UPDATE
            r = MagicMock()
            r.scalar_one_or_none.return_value = uuid.uuid4()
            return r
        raise AssertionError(f"Unexpected execute call #{call_no}")

    session.execute = _execute
    session.get = AsyncMock(return_value=claimed)
    mgr = HITLManager()
    result = await mgr.claim(session, run_id=_RUN, gate_id=_GATE, org_id=_ORG, claimant_id=_USER)
    assert result is claimed
    assert len(membership_stmts) == 2, "Both membership queries must be executed"
    for stmt in membership_stmts:
        sql = str(stmt.compile(compile_kwargs={"literal_binds": True}))
        assert "team_memberships.role IN ('runner', 'operator')" in sql


async def test_claim_team_viewer_role_denied():
    """A team member holding only the ``viewer`` role cannot claim a team gate.

    The role filter is applied inside the membership query, so a viewer's
    membership row simply never matches — the claim must fail with
    NotTeamMemberError before any claim UPDATE.
    """
    gate = _gate(account_id=None, required_team_id=_TEAM)

    session = AsyncMock()
    call_no = 0
    membership_check_hit = False

    async def _execute(stmt: Any) -> Any:
        nonlocal call_no, membership_check_hit
        if _is_runs_select(stmt):
            return _runs_result()
        call_no += 1
        if call_no in (1, 2):
            # Gate pre-check SELECT + FOR UPDATE row lock
            r = MagicMock()
            r.scalar_one_or_none.return_value = gate
            return r
        if call_no == 3:
            # Membership pre-check — a viewer row is filtered out by the role
            # predicate, so the query yields no matching membership.
            membership_check_hit = True
            sql = str(stmt.compile(compile_kwargs={"literal_binds": True}))
            assert "team_memberships.role IN ('runner', 'operator')" in sql
            r = MagicMock()
            r.scalar_one_or_none.return_value = None
            return r
        raise AssertionError(f"Unexpected execute call #{call_no}")

    session.execute = _execute
    mgr = HITLManager()
    with pytest.raises(NotTeamMemberError):
        await mgr.claim(session, run_id=_RUN, gate_id=_GATE, org_id=_ORG, claimant_id=_USER)
    assert membership_check_hit, "Team membership check was not performed"
    assert call_no == 3, "Claim must fail before the claim UPDATE"


async def test_claim_team_role_lost_between_check_and_update_undoes_claim():
    """If the claimant's team role drops below runner/operator after the pre-check,
    the claim is undone and NotTeamMemberError is raised.

    The TOCTOU re-verification carries the same role predicate, so a member
    demoted from ``operator`` to ``viewer`` between the pre-check and the claim
    UPDATE has the claim reverted.
    """
    gate = _gate(account_id=None, required_team_id=_TEAM)
    membership = MagicMock(spec=TeamMembership)
    membership.team_id = _TEAM
    membership.account_id = _USER
    membership.role = "operator"

    session = AsyncMock()
    call_no = 0
    undo_stmt: list[Any] = []

    async def _execute(stmt: Any) -> Any:
        nonlocal call_no
        if _is_runs_select(stmt):
            return _runs_result()
        call_no += 1
        if call_no in (1, 2):
            r = MagicMock()
            r.scalar_one_or_none.return_value = gate
            return r
        if call_no == 3:
            # Membership pre-check passes (member was operator at the time)
            r = MagicMock()
            r.scalar_one_or_none.return_value = membership
            return r
        if call_no == 4:
            r = MagicMock()
            r.scalar_one_or_none.return_value = uuid.uuid4()
            return r
        if call_no == 5:
            # TOCTOU re-verification — role demoted to viewer, query yields none
            sql = str(stmt.compile(compile_kwargs={"literal_binds": True}))
            assert "team_memberships.role IN ('runner', 'operator')" in sql
            r = MagicMock()
            r.scalar_one_or_none.return_value = None
            return r
        if call_no == 6:
            # Undo UPDATE — claim is released
            undo_stmt.append(stmt)
            return MagicMock()
        raise AssertionError(f"Unexpected execute call #{call_no}")

    session.execute = _execute
    mgr = HITLManager()
    with pytest.raises(NotTeamMemberError):
        await mgr.claim(session, run_id=_RUN, gate_id=_GATE, org_id=_ORG, claimant_id=_USER)

    assert len(undo_stmt) == 1, "Claim should be undone when team role drops below runner/operator"
    undo_values = {col.name: expr.value for col, expr in undo_stmt[0]._values.items()}
    assert undo_values["account_id"] is None
    assert undo_values["claimed_at"] is None
    assert undo_values["claim_token"] is None
    assert undo_values["expires_at"] is not None, "Undo UPDATE should settle expires_at (non-null) to release the claim"


async def test_claim_membership_lost_between_check_and_update_undoes_claim():
    """If the claimant loses team membership after the pre-check, claim() is undone.

    The UPDATE is followed by a second membership check (closing the TOCTOU
    window).  When that check fails, the claim must be reverted and
    NotTeamMemberError raised.
    """
    gate = _gate(account_id=None, required_team_id=_TEAM)
    membership = MagicMock(spec=TeamMembership)
    membership.team_id = _TEAM
    membership.account_id = _USER

    session = AsyncMock()
    session.add = MagicMock()
    session.flush = AsyncMock()

    call_no = 0
    undo_stmt: list[Any] = []

    async def _execute(stmt: Any) -> Any:
        nonlocal call_no
        if _is_runs_select(stmt):
            return _runs_result()
        call_no += 1
        if call_no == 1:
            r = MagicMock()
            r.scalar_one_or_none.return_value = gate
            return r
        if call_no == 2:
            r = MagicMock()
            r.scalar_one_or_none.return_value = gate
            return r
        if call_no == 3:
            r = MagicMock()
            r.scalar_one_or_none.return_value = membership
            return r
        if call_no == 4:
            r = MagicMock()
            r.scalar_one_or_none.return_value = uuid.uuid4()
            return r
        if call_no == 5:
            # Membership vanished between the check and the UPDATE
            r = MagicMock()
            r.scalar_one_or_none.return_value = None
            return r
        if call_no == 6:
            # Undo UPDATE — claim is released
            undo_stmt.append(stmt)
            return MagicMock()
        raise AssertionError(f"Unexpected execute call #{call_no}")

    session.execute = _execute
    mgr = HITLManager()
    with pytest.raises(NotTeamMemberError):
        await mgr.claim(session, run_id=_RUN, gate_id=_GATE, org_id=_ORG, claimant_id=_USER)

    assert len(undo_stmt) == 1, "Claim should be undone when membership is lost post-check"
    undo_values = {col.name: expr.value for col, expr in undo_stmt[0]._values.items()}
    assert undo_values["account_id"] is None
    assert undo_values["claimed_at"] is None
    assert undo_values["claim_token"] is None
    assert undo_values["expires_at"] is not None, "Undo UPDATE should settle expires_at (non-null) to release the claim"


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
        if _is_runs_select(stmt):
            return _runs_result()
        call_no += 1
        if call_no == 1:
            # Pre-check SELECT
            r = MagicMock()
            r.scalar_one_or_none.return_value = gate
            return r
        if call_no == 2:
            # FOR UPDATE row lock
            r = MagicMock()
            r.scalar_one_or_none.return_value = gate
            return r
        if call_no == 3:
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


async def test_claim_locked_gate_vanished_raises():
    """Team-scoped claim whose FOR UPDATE lock finds no row raises GateNotFoundError."""
    gate = _gate(account_id=None, required_team_id=_TEAM)

    session = AsyncMock()
    session.add = MagicMock()
    session.flush = AsyncMock()

    call_no = 0

    async def _execute(stmt: Any) -> Any:
        nonlocal call_no
        if _is_runs_select(stmt):
            return _runs_result()
        call_no += 1
        if call_no == 1:
            # Pre-check SELECT
            r = MagicMock()
            r.scalar_one_or_none.return_value = gate
            return r
        if call_no == 2:
            # FOR UPDATE row lock — row vanished
            r = MagicMock()
            r.scalar_one_or_none.return_value = None
            return r
        raise AssertionError(f"Unexpected execute call #{call_no}")

    session.execute = _execute
    mgr = HITLManager()
    with pytest.raises(GateNotFoundError):
        await mgr.claim(session, run_id=_RUN, gate_id=_GATE, org_id=_ORG, claimant_id=_USER)


async def test_claim_locked_gate_already_decided_raises():
    """Team-scoped claim whose FOR UPDATE lock sees a decided gate raises GateAlreadyDecidedError."""
    gate = _gate(account_id=None, required_team_id=_TEAM)
    locked_decided = _gate(account_id=None, required_team_id=_TEAM, decision="approved")

    session = AsyncMock()
    session.add = MagicMock()
    session.flush = AsyncMock()

    call_no = 0

    async def _execute(stmt: Any) -> Any:
        nonlocal call_no
        if _is_runs_select(stmt):
            return _runs_result()
        call_no += 1
        if call_no == 1:
            r = MagicMock()
            r.scalar_one_or_none.return_value = gate
            return r
        if call_no == 2:
            r = MagicMock()
            r.scalar_one_or_none.return_value = locked_decided
            return r
        raise AssertionError(f"Unexpected execute call #{call_no}")

    session.execute = _execute
    mgr = HITLManager()
    with pytest.raises(GateAlreadyDecidedError):
        await mgr.claim(session, run_id=_RUN, gate_id=_GATE, org_id=_ORG, claimant_id=_USER)


async def test_claim_locked_gate_already_claimed_raises():
    """Team-scoped claim whose FOR UPDATE lock sees a claimed gate raises AlreadyClaimedError."""
    gate = _gate(account_id=None, required_team_id=_TEAM)
    locked_claimed = _gate(account_id=uuid.uuid4(), required_team_id=_TEAM)

    session = AsyncMock()
    session.add = MagicMock()
    session.flush = AsyncMock()

    call_no = 0

    async def _execute(stmt: Any) -> Any:
        nonlocal call_no
        if _is_runs_select(stmt):
            return _runs_result()
        call_no += 1
        if call_no == 1:
            r = MagicMock()
            r.scalar_one_or_none.return_value = gate
            return r
        if call_no == 2:
            r = MagicMock()
            r.scalar_one_or_none.return_value = locked_claimed
            return r
        raise AssertionError(f"Unexpected execute call #{call_no}")

    session.execute = _execute
    mgr = HITLManager()
    with pytest.raises(AlreadyClaimedError):
        await mgr.claim(session, run_id=_RUN, gate_id=_GATE, org_id=_ORG, claimant_id=_USER)


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
        if _is_runs_select(stmt):
            return _runs_result()
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
    session.get = AsyncMock(return_value=claimed)
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


async def test_approve_gate_vanished_after_update_raises():
    """approve() whose gate row disappears after the UPDATE raises GateVanishedError."""
    future = datetime.now(UTC) + timedelta(minutes=5)
    gate = _gate(account_id=_USER, claim_token="tok", expires_at=future)
    session = _session_decide(update_returns_id=gate.id, session_get_gate=None)
    mgr = HITLManager()
    with pytest.raises(GateVanishedError):
        await mgr.approve(session, run_id=_RUN, gate_id=_GATE, org_id=_ORG, claim_token="tok")


async def test_approve_jwt_expired_signature_raises_expired():
    """A JWT claim token whose signature has expired maps to ClaimTokenExpiredError."""
    from jwt import ExpiredSignatureError

    future = datetime.now(UTC) + timedelta(minutes=5)
    gate = _gate(account_id=_USER, claim_token="aaa.bbb.ccc", expires_at=future)
    session = _session_decide(update_returns_id=None, diagnosis_gate=gate)
    mgr = HITLManager(secret_key="test-secret-key-with-enough-length")

    with (
        patch(
            "modulo.core.hitl_manager._decode_claim_jwt",
            side_effect=ExpiredSignatureError("token expired"),
        ) as mock_decode,
        pytest.raises(ClaimTokenExpiredError),
    ):
        await mgr.approve(session, run_id=_RUN, gate_id=_GATE, org_id=_ORG, claim_token="aaa.bbb.ccc")

    _assert_decode_scope_args(mock_decode)


async def test_approve_jwt_invalid_signature_raises_invalid():
    """A JWT claim token with a bad signature/scope maps to ClaimTokenInvalidError."""
    from jwt import InvalidTokenError

    future = datetime.now(UTC) + timedelta(minutes=5)
    gate = _gate(account_id=_USER, claim_token="aaa.bbb.ccc", expires_at=future)
    session = _session_decide(update_returns_id=None, diagnosis_gate=gate)
    mgr = HITLManager(secret_key="test-secret-key-with-enough-length")

    with (
        patch(
            "modulo.core.hitl_manager._decode_claim_jwt",
            side_effect=InvalidTokenError("bad signature"),
        ) as mock_decode,
        pytest.raises(ClaimTokenInvalidError),
    ):
        await mgr.approve(session, run_id=_RUN, gate_id=_GATE, org_id=_ORG, claim_token="aaa.bbb.ccc")

    _assert_decode_scope_args(mock_decode)


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


async def test_approve_existing_claim_without_token_raises_expired():
    """A gate that lost its claim token between claim and decide is treated as expired."""
    future = datetime.now(UTC) + timedelta(minutes=5)
    gate = _gate(account_id=_USER, claim_token=None, expires_at=future)
    session = _session_decide(update_returns_id=None, diagnosis_gate=gate)
    mgr = HITLManager()
    with pytest.raises(ClaimTokenExpiredError):
        await mgr.approve(session, run_id=_RUN, gate_id=_GATE, org_id=_ORG, claim_token="tok")


async def test_approve_audit_cancellation_propagates():
    """Cancellation while logging the audit event propagates instead of being swallowed."""
    future = datetime.now(UTC) + timedelta(minutes=5)
    gate = _gate(account_id=_USER, claim_token="good-token", expires_at=future)
    gate_decided = _gate(account_id=None, claim_token=None, expires_at=None, decision="approved")
    session = _session_decide(update_returns_id=gate.id, session_get_gate=gate_decided)
    mgr = HITLManager()

    with (
        patch(
            "modulo.core.hitl_manager.append_audit_event",
            new=AsyncMock(side_effect=asyncio.CancelledError()),
        ),
        pytest.raises(asyncio.CancelledError),
    ):
        await mgr.approve(session, run_id=_RUN, gate_id=_GATE, org_id=_ORG, claim_token="good-token")


# ---------------------------------------------------------------------------
# decision_payload persistence (B1)
# ---------------------------------------------------------------------------


async def test_approve_persists_decision_payload():
    """approve() persists the full resume payload into decision_payload.

    FAR-541 (iteration 3): the payload is stamped by _decide with the row's
    gate id when it does not already carry one."""
    future = datetime.now(UTC) + timedelta(minutes=5)
    gate = _gate(account_id=_USER, claim_token="good-token", expires_at=future)
    gate_decided = _gate(account_id=None, claim_token=None, expires_at=None, decision="approved")
    session, captured = _session_decide_capture(gate.id, gate_decided)
    mgr = HITLManager()
    payload: dict[str, Any] = {"action": "approved", "notes": "looks good"}
    await mgr.approve(
        session,
        run_id=_RUN,
        gate_id=_GATE,
        org_id=_ORG,
        claim_token="good-token",
        decision_payload=payload,
    )
    values = _update_values(captured[0])
    assert values["decision"] == "approved"
    assert values["decision_payload"] == {"action": "approved", "notes": "looks good", "gate_id": _GATE}
    # The caller's dict is copied, not mutated (it feeds the direct resume too).
    assert payload == {"action": "approved", "notes": "looks good"}


async def test_approve_without_payload_defaults_to_action():
    """approve() with no payload persists a faithful stamped
    ``{"action": "approved", "gate_id": <row gate>}`` (FAR-541 iteration 3:
    _decide is the stamp authority)."""
    future = datetime.now(UTC) + timedelta(minutes=5)
    gate = _gate(account_id=_USER, claim_token="good-token", expires_at=future)
    gate_decided = _gate(account_id=None, claim_token=None, expires_at=None, decision="approved")
    session, captured = _session_decide_capture(gate.id, gate_decided)
    mgr = HITLManager()
    await mgr.approve(session, run_id=_RUN, gate_id=_GATE, org_id=_ORG, claim_token="good-token")
    values = _update_values(captured[0])
    assert values["decision_payload"] == {"action": "approved", "gate_id": _GATE}


async def test_approve_with_modification_persists_modified_output_payload():
    """approve_with_modification() persists the modified_output into the
    payload (stamped with the row's gate id by _decide)."""
    future = datetime.now(UTC) + timedelta(minutes=5)
    gate = _gate(account_id=_USER, claim_token="good-token", expires_at=future)
    gate_decided = _gate(account_id=None, claim_token=None, expires_at=None, decision="approved")
    session, captured = _session_decide_capture(gate.id, gate_decided)
    modified = {"summary": "Rewritten by reviewer"}
    mgr = HITLManager()
    await mgr.approve_with_modification(
        session,
        run_id=_RUN,
        gate_id=_GATE,
        org_id=_ORG,
        claim_token="good-token",
        modified_output=modified,
    )
    values = _update_values(captured[0])
    assert values["decision_payload"] == {"action": "approved", "modified_output": modified, "gate_id": _GATE}


async def test_reject_persists_reason_payload():
    """reject() persists the reason into the decision payload (stamped)."""
    future = datetime.now(UTC) + timedelta(minutes=5)
    gate = _gate(account_id=_USER, claim_token="tok", expires_at=future)
    gate_decided = _gate(account_id=None, claim_token=None, decision="rejected")
    session, captured = _session_decide_capture(gate.id, gate_decided)
    mgr = HITLManager()
    await mgr.reject(session, run_id=_RUN, gate_id=_GATE, org_id=_ORG, claim_token="tok", reason="needs rework")
    values = _update_values(captured[0])
    assert values["decision"] == "rejected"
    assert values["decision_payload"] == {"action": "rejected", "reason": "needs rework", "gate_id": _GATE}


async def test_deliver_manual_persists_output_payload():
    """deliver_manual() persists the manual output into the decision payload
    (stamped with the row's gate id by _decide)."""
    future = datetime.now(UTC) + timedelta(minutes=5)
    gate = _gate(account_id=_USER, claim_token="good-token", expires_at=future)
    gate_decided = _gate(account_id=None, claim_token=None, expires_at=None, decision="deliver_manual")
    session, captured = _session_decide_capture(gate.id, gate_decided)
    manual = {"summary": "Human provided answer"}
    mgr = HITLManager()
    await mgr.deliver_manual(
        session,
        run_id=_RUN,
        gate_id=_GATE,
        org_id=_ORG,
        claim_token="good-token",
        output=manual,
    )
    values = _update_values(captured[0])
    assert values["decision"] == "deliver_manual"
    assert values["decision_payload"] == {"action": "deliver_manual", "output": manual, "gate_id": _GATE}


async def test_decide_accepts_payload_stamped_for_this_gate():
    """FAR-541 (iteration 3): a payload already stamped with the row's own
    gate id is persisted verbatim — no duplicate stamp, no error."""
    future = datetime.now(UTC) + timedelta(minutes=5)
    gate = _gate(account_id=_USER, claim_token="good-token", expires_at=future)
    gate_decided = _gate(account_id=None, claim_token=None, expires_at=None, decision="approved")
    session, captured = _session_decide_capture(gate.id, gate_decided)
    mgr = HITLManager()
    await mgr.approve(
        session,
        run_id=_RUN,
        gate_id=_GATE,
        org_id=_ORG,
        claim_token="good-token",
        decision_payload={"action": "approved", "gate_id": _GATE, "notes": "ok"},
    )
    values = _update_values(captured[0])
    assert values["decision_payload"] == {"action": "approved", "gate_id": _GATE, "notes": "ok"}


async def test_decide_rejects_foreign_stamped_payload():
    """FAR-541 (iteration 3): a payload stamped for a DIFFERENT gate is
    refused with DecisionPayloadError BEFORE any DB write — a foreign-stamped
    decision must never be persisted."""
    future = datetime.now(UTC) + timedelta(minutes=5)
    gate = _gate(account_id=_USER, claim_token="good-token", expires_at=future)
    session, _captured = _session_decide_capture(gate.id, None)
    mgr = HITLManager()
    with pytest.raises(DecisionPayloadError, match="stamped for gate 'some-other-gate'"):
        await mgr.approve(
            session,
            run_id=_RUN,
            gate_id=_GATE,
            org_id=_ORG,
            claim_token="good-token",
            decision_payload={"action": "approved", "gate_id": "some-other-gate"},
        )


async def test_decision_payload_must_be_dict():
    """A non-dict decision_payload is rejected with DecisionPayloadError."""
    future = datetime.now(UTC) + timedelta(minutes=5)
    gate = _gate(account_id=_USER, claim_token="good-token", expires_at=future)
    gate_decided = _gate(account_id=None, claim_token=None, expires_at=None, decision="approved")
    session, _ = _session_decide_capture(gate.id, gate_decided)
    mgr = HITLManager()
    with pytest.raises(DecisionPayloadError, match="must be a JSON object"):
        await mgr.approve(
            session,
            run_id=_RUN,
            gate_id=_GATE,
            org_id=_ORG,
            claim_token="good-token",
            decision_payload="not-a-dict",  # type: ignore[arg-type]
        )


async def test_decision_payload_output_must_be_dict():
    """A non-dict output member is rejected with DecisionPayloadError."""
    future = datetime.now(UTC) + timedelta(minutes=5)
    gate = _gate(account_id=_USER, claim_token="good-token", expires_at=future)
    gate_decided = _gate(account_id=None, claim_token=None, expires_at=None, decision="approved")
    session, _ = _session_decide_capture(gate.id, gate_decided)
    mgr = HITLManager()
    with pytest.raises(DecisionPayloadError, match="output must be a JSON object"):
        await mgr.approve(
            session,
            run_id=_RUN,
            gate_id=_GATE,
            org_id=_ORG,
            claim_token="good-token",
            decision_payload={"action": "approved", "output": ["not", "a", "dict"]},
        )


async def test_decision_payload_size_limited():
    """An oversized decision_payload is rejected with DecisionPayloadError."""
    future = datetime.now(UTC) + timedelta(minutes=5)
    gate = _gate(account_id=_USER, claim_token="good-token", expires_at=future)
    gate_decided = _gate(account_id=None, claim_token=None, expires_at=None, decision="approved")
    session, _ = _session_decide_capture(gate.id, gate_decided)
    mgr = HITLManager()
    huge = {"action": "deliver_manual", "output": {"blob": "x" * (256 * 1024 + 1)}}
    with pytest.raises(DecisionPayloadError, match="byte limit"):
        await mgr.deliver_manual(
            session,
            run_id=_RUN,
            gate_id=_GATE,
            org_id=_ORG,
            claim_token="good-token",
            output={"blob": "x" * (256 * 1024 + 1)},
            decision_payload=huge,
        )


def test_decision_payload_none_is_accepted():
    """A None decision payload is allowed (legacy/payload-less decisions)."""
    assert HITLManager._validate_decision_payload(None) is None


def test_decision_payload_valid_dict_is_accepted():
    """A well-formed dict decision payload passes validation."""
    assert (
        HITLManager._validate_decision_payload(
            {"action": "approved", "modified_output": {"blob": "ok"}, "nested": {"list": [1, 2, 3]}}
        )
        is None
    )


def test_decision_payload_unserialisable_rejected():
    """A dict that cannot be JSON-serialised is rejected with DecisionPayloadError."""
    payload: dict[str, Any] = {"action": "approved"}
    payload["self"] = payload
    with pytest.raises(DecisionPayloadError, match="must be JSON-serialisable"):
        HITLManager._validate_decision_payload(payload)


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


async def test_reject_expired_token_raises():
    past = datetime.now(UTC) - timedelta(minutes=1)
    gate = _gate(account_id=_USER, claim_token="tok", expires_at=past)
    session = _session_decide(update_returns_id=None, diagnosis_gate=gate)
    mgr = HITLManager()
    with pytest.raises(ClaimTokenExpiredError):
        await mgr.reject(session, run_id=_RUN, gate_id=_GATE, org_id=_ORG, claim_token="tok")


async def test_reject_gate_not_found_raises():
    session = _session_decide(update_returns_id=None, diagnosis_gate=None)
    mgr = HITLManager()
    with pytest.raises(GateNotFoundError):
        await mgr.reject(session, run_id=_RUN, gate_id=_GATE, org_id=_ORG, claim_token="tok")


async def test_reject_already_decided_raises():
    gate = _gate(decision="rejected")
    session = _session_decide(update_returns_id=None, diagnosis_gate=gate)
    mgr = HITLManager()
    with pytest.raises(GateAlreadyDecidedError):
        await mgr.reject(session, run_id=_RUN, gate_id=_GATE, org_id=_ORG, claim_token="tok")


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
    row = type("Row", (), {"run_id": _RUN, "gate_id": "gate-a"})
    row2 = type("Row", (), {"run_id": _RUN, "gate_id": "gate-b"})
    expired_result.all.return_value = [row(), row2()]
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


async def test_list_pending_returns_undecided_gates():
    """Both unclaimed and held (claimed) undecided gates pass through, so the UI
    can render the claimed state (FAR-612)."""
    unclaimed = _gate(account_id=None)
    held = _gate(account_id=_USER, claim_token="tok")
    session = AsyncMock()
    scalars = MagicMock()
    scalars.__iter__ = lambda self: iter([unclaimed, held])
    execute_result = MagicMock()
    execute_result.scalars.return_value = scalars
    session.execute = AsyncMock(return_value=execute_result)

    mgr = HITLManager()
    result = await mgr.list_pending(session, _ORG)
    assert result == [unclaimed, held]


async def test_list_pending_filters_to_actionable_run_statuses():
    """FAR-612: list_pending joins runs and keeps only gates whose run is in
    ``awaiting_human`` or ``claimed`` status — undecided gates on terminal runs
    (data rot) are excluded from the org pending list."""
    gate = _gate(account_id=None)
    session = AsyncMock()
    scalars = MagicMock()
    scalars.__iter__ = lambda self: iter([gate])
    execute_result = MagicMock()
    execute_result.scalars.return_value = scalars
    session.execute = AsyncMock(return_value=execute_result)

    mgr = HITLManager()
    await mgr.list_pending(session, _ORG)
    sql = str(session.execute.call_args[0][0].compile(compile_kwargs={"literal_binds": True}))
    assert "JOIN runs" in sql, f"list_pending must join runs, got: {sql}"
    assert "awaiting_human" in sql, f"run-status filter missing awaiting_human, got: {sql}"
    assert "claimed" in sql, f"run-status filter missing claimed, got: {sql}"
    assert "complete" not in sql, f"terminal status must not be an allowed value, got: {sql}"


async def test_list_pending_include_claimed_false_excludes_claimed_gates():
    """``include_claimed=False`` restricts list_pending() to unclaimed +
    undecided gates — the WHERE carries ``account_id IS NULL`` so a held
    claim never surfaces. (FAR-686 composed with FAR-612: the DEFAULT now
    includes held gates so consumers can render the claimed state; the
    exclusion is the explicit opt-out.)"""
    unclaimed = _gate(account_id=None)
    session = AsyncMock()
    scalars = MagicMock()
    scalars.__iter__ = lambda self: iter([unclaimed])
    execute_result = MagicMock()
    execute_result.scalars.return_value = scalars
    session.execute = AsyncMock(return_value=execute_result)

    mgr = HITLManager()
    result = await mgr.list_pending(session, _ORG, include_claimed=False)
    assert result == [unclaimed]
    sql = str(session.execute.call_args[0][0].compile())
    assert "decision IS NULL" in sql
    assert "account_id IS NULL" in sql


async def test_list_pending_include_claimed_returns_claimed_and_unclaimed():
    """FAR-686: ``include_claimed=True`` (the org review endpoint) also returns
    claimed-but-undecided gates so they stay visible and actionable."""
    claimed = _gate(account_id=_USER, claim_token="tok", claimed_at=datetime.now(UTC))
    unclaimed = _gate(account_id=None)
    session = AsyncMock()
    scalars = MagicMock()
    scalars.__iter__ = lambda self: iter([claimed, unclaimed])
    execute_result = MagicMock()
    execute_result.scalars.return_value = scalars
    session.execute = AsyncMock(return_value=execute_result)

    mgr = HITLManager()
    result = await mgr.list_pending(session, _ORG, include_claimed=True)
    assert result == [claimed, unclaimed]
    sql = str(session.execute.call_args[0][0].compile())
    assert "decision IS NULL" in sql
    assert "account_id IS NULL" not in sql


async def test_list_pending_never_returns_decided_gates():
    """Both list_pending() modes filter on ``decision IS NULL`` — a decided
    gate is history, never pending work."""
    session = AsyncMock()
    scalars = MagicMock()
    scalars.__iter__ = lambda self: iter([])
    execute_result = MagicMock()
    execute_result.scalars.return_value = scalars
    session.execute = AsyncMock(return_value=execute_result)

    mgr = HITLManager()
    await mgr.list_pending(session, _ORG)
    default_sql = str(session.execute.call_args[0][0].compile())
    await mgr.list_pending(session, _ORG, include_claimed=True)
    include_sql = str(session.execute.call_args[0][0].compile())
    assert "decision IS NULL" in default_sql
    assert "decision IS NULL" in include_sql


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
# Executor integration — GraphInterrupt handling
# ---------------------------------------------------------------------------


def test_looks_like_jwt_true_for_three_segments():
    """A token with exactly two dots (three base64 segments) is treated as a JWT."""
    assert HITLManager._looks_like_jwt("aaa.bbb.ccc") is True


def test_looks_like_jwt_false_for_opaque_token():
    """Opaque alpha tokens (no dots) are not treated as JWTs."""
    assert HITLManager._looks_like_jwt("opaque-token") is False
    assert HITLManager._looks_like_jwt("one.two.three.four") is False


def _mock_graph_validator() -> MagicMock:
    validation = MagicMock()
    validation.is_valid = True
    mock_cls = MagicMock()
    mock_cls.return_value.validate_for_run = AsyncMock(return_value=validation)
    return mock_cls


async def _bypass_capacity(mock_self: Any, **kwargs: Any) -> Any:
    run = MagicMock()
    run.status = "running"
    return run


async def test_executor_sets_awaiting_human_on_node_interrupt():
    """When astream_events raises GraphInterrupt, the executor transitions the
    run toward awaiting_human — but CANCEL-WINS (B6) finalises it ``cancelled``
    when the row carries ``cancellation_requested``."""
    from contextlib import asynccontextmanager

    from langgraph.errors import GraphInterrupt
    from langgraph.types import Interrupt

    from modulo.core.pipeline_engine.executor import PipelineExecutor

    run = MagicMock()
    run.id = uuid.uuid4()
    run.pipeline_id = uuid.uuid4()
    run.snapshot_id = uuid.uuid4()
    run.langgraph_thread_id = str(uuid.uuid4())
    run.status = "cancelled"

    final_run = MagicMock()
    final_run.status = "cancelled"

    snapshot = MagicMock()
    snapshot.graph_json = {"nodes": [{"id": "a"}], "edges": []}
    snapshot.run_context_defaults = {}

    session = AsyncMock()
    begin_cm = AsyncMock()
    begin_cm.__aenter__ = AsyncMock(return_value=None)
    begin_cm.__aexit__ = AsyncMock(return_value=False)
    session.begin = MagicMock(return_value=begin_cm)
    cancel_run = MagicMock()
    cancel_run.id = run.id
    cancel_run.status = "awaiting_human"
    cancel_run.cancellation_requested = True
    scalar_result = MagicMock()
    scalar_result.scalar_one.return_value = snapshot
    scalar_result.scalar_one_or_none.return_value = cancel_run
    session.execute = AsyncMock(return_value=scalar_result)

    @asynccontextmanager
    async def _ctx():
        yield session

    session_factory = MagicMock(side_effect=lambda: _ctx())

    async def _failing_stream(*args: Any, **kwargs: Any) -> Any:
        raise GraphInterrupt((Interrupt(value={"gate_id": "step-1"}),))
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
        patch("modulo.core.cost_controller.finalize.update_run_status", return_value=final_run) as mock_update,
        patch("modulo.core.pipeline_engine.executor.set_rls_org"),
        patch("modulo.core.pipeline_engine.executor.set_rls_execution_context"),
        patch("modulo.core.pipeline_engine.executor.get_or_compile", return_value=compiled),
        patch("modulo.core.pipeline_engine.executor.get_registry", return_value=registry),
        patch(
            "modulo.core.pipeline_engine.executor._checkpointer_scope",
            return_value=AsyncMock(),
        ),
        patch("modulo.core.pipeline_engine.executor.GraphValidator", new=_mock_graph_validator()),
        patch.object(PipelineExecutor, "_check_capacity", _bypass_capacity),
        patch("modulo.settings.get_settings", return_value=MagicMock(fernet_key="x" * 32)),
    ):
        executor = PipelineExecutor(MagicMock(), checkpointer_conn_string="a" * 32)
        result = await executor.execute(run_id=run.id, org_id=uuid.uuid4(), input_payload={})

    # CANCEL-WINS (B6): finalizing an awaiting_human run whose row carries
    # cancellation_requested writes "cancelled" instead — the executor's
    # finalization tail returns the re-fetched run whose status reflects the
    # DB transition.
    assert result is run
    assert result.status == "cancelled"
    final_update_call = mock_update.call_args_list[-1]
    assert final_update_call.args[2] == "cancelled"


# ---------------------------------------------------------------------------
# Atomicity / SQL-shape — the claim and decide UPDATEs carry atomic WHERE clauses
# ---------------------------------------------------------------------------


async def _claim_capture() -> tuple[HITLManager, AsyncMock, list[Any]]:
    """Run a bare claim() against a capturing session, returning the UPDATE stmt."""
    unclaimed = _gate(account_id=None)
    claimed_gate = _gate(
        account_id=_USER,
        claim_token="tok",
        expires_at=datetime.now(UTC) + timedelta(minutes=15),
    )
    session = AsyncMock()
    session.add = MagicMock()
    session.flush = AsyncMock()
    captured: list[Any] = []

    async def _execute(stmt: Any) -> Any:
        captured.append(stmt)
        if _is_runs_select(stmt):
            r = MagicMock()
            r.scalar_one_or_none.return_value = _run_mock("awaiting_human")
            return r
        if len(captured) == 1:
            r = MagicMock()
            r.scalar_one_or_none.return_value = unclaimed
            return r
        r = MagicMock()
        r.scalar_one_or_none.return_value = uuid.uuid4()
        return r

    session.execute = _execute
    session.get = AsyncMock(return_value=claimed_gate)
    begin_nested_cm = AsyncMock()
    begin_nested_cm.__aenter__ = AsyncMock(return_value=None)
    begin_nested_cm.__aexit__ = AsyncMock(return_value=False)
    session.begin_nested = MagicMock(return_value=begin_nested_cm)

    mgr = HITLManager()
    await mgr.claim(session, run_id=_RUN, gate_id=_GATE, org_id=_ORG, claimant_id=_USER)
    return mgr, session, captured


async def test_claim_update_has_atomic_where_clause():
    """The claim UPDATE atomically guards unclaimed + undecided in its WHERE, so
    a concurrent claimer cannot double-claim (no TOCTOU between check and update)."""
    _mgr, _session, captured = await _claim_capture()
    update_stmt = captured[2]  # gate statements: pre-check SELECT, run SELECT, then the UPDATE ... RETURNING
    sql = str(update_stmt.compile(compile_kwargs={"literal_binds": True}))
    assert "hitl_claims" in sql
    assert "account_id IS NULL" in sql
    assert "decision IS NULL" in sql


async def test_claim_update_returns_id_for_atomic_race_detection():
    """The claim UPDATE uses RETURNING id so the caller can detect a lost race:
    no returned id ⇒ someone else claimed first ⇒ AlreadyClaimedError."""
    _mgr, _session, captured = await _claim_capture()
    update_stmt = captured[2]  # gate statements: pre-check SELECT, run SELECT, then the UPDATE ... RETURNING
    sql = str(update_stmt.compile(compile_kwargs={"literal_binds": True}))
    assert "RETURNING" in sql, f"claim UPDATE missing RETURNING id, got: {sql}"
    assert "hitl_claims.id" in sql, f"claim UPDATE missing hitl_claims.id, got: {sql}"


async def test_decide_update_where_checks_expires_at_gt_now():
    """_decide()'s UPDATE WHERE includes expires_at > now — the DB is the
    authoritative TTL source, not a client-side token expiry check."""
    from sqlalchemy.sql.dml import Update as _SQLUpdate

    future = datetime.now(UTC) + timedelta(minutes=5)
    gate = _gate(account_id=_USER, claim_token="good-token", expires_at=future)
    gate_decided = _gate(account_id=None, claim_token=None, expires_at=None, decision="approved")
    session = _session_decide(update_returns_id=gate.id, session_get_gate=gate_decided)
    captured: list[Any] = []

    orig_execute = session.execute

    async def _capture_execute(stmt: Any) -> Any:
        captured.append(stmt)
        return await orig_execute(stmt)

    session.execute = _capture_execute
    mgr = HITLManager()
    await mgr.approve(session, run_id=_RUN, gate_id=_GATE, org_id=_ORG, claim_token="good-token")

    assert len(captured) >= 1
    stmt = captured[0]
    assert isinstance(stmt, _SQLUpdate)
    where_sql = str(stmt.whereclause)
    assert "expires_at" in where_sql, f"expected expires_at comparison in WHERE, got: {where_sql}"
    assert ">" in where_sql, f"expected > comparison in WHERE, got: {where_sql}"


# ---------------------------------------------------------------------------
# Org scoping — every HITL query carries the organisation_id filter
# ---------------------------------------------------------------------------


async def test_hitl_queries_are_org_scoped():
    """Every hitl_claims query (claim UPDATE, list_pending SELECT, decide UPDATE)
    is filtered by organisation_id so one org can never see another org's gates."""
    _mgr, _session, captured = await _claim_capture()
    claim_update = captured[2]
    claim_sql = str(claim_update.compile(compile_kwargs={"literal_binds": True}))
    assert "organisation_id" in claim_sql

    session = AsyncMock()
    row = _gate(account_id=None)
    result = MagicMock()
    result.scalars.return_value = [row]
    session.execute = AsyncMock(return_value=result)
    mgr = HITLManager()
    await mgr.list_pending(session, _ORG)
    list_sql = str(session.execute.call_args[0][0].compile(compile_kwargs={"literal_binds": True}))
    assert "organisation_id" in list_sql


# ---------------------------------------------------------------------------
# HITLManager is stateless — one instance serves concurrent sessions
# ---------------------------------------------------------------------------


async def test_manager_instance_is_stateless_across_concurrent_claims():
    """HITLManager holds no per-call state — a single instance can serve two
    concurrent claims on separate sessions/gates without cross-talk."""
    mgr = HITLManager()
    gate_a = _gate(account_id=None)
    gate_b = _gate(account_id=None)
    claimed_a = _gate(account_id=_USER, claim_token="tok-a", expires_at=datetime.now(UTC) + timedelta(minutes=15))
    claimed_b = _gate(account_id=_USER, claim_token="tok-b", expires_at=datetime.now(UTC) + timedelta(minutes=15))

    async def _run_claim(gate_id: str, gate: HitlClaim, claimed: HitlClaim) -> HitlClaim:
        session = AsyncMock()
        session.add = MagicMock()
        session.flush = AsyncMock()
        captured: list[Any] = []

        async def _execute(stmt: Any) -> Any:
            captured.append(stmt)
            if _is_runs_select(stmt):
                return _runs_result()
            if len(captured) == 1:
                r = MagicMock()
                r.scalar_one_or_none.return_value = gate
                return r
            r = MagicMock()
            r.scalar_one_or_none.return_value = uuid.uuid4()
            return r

        session.execute = _execute
        session.get = AsyncMock(return_value=claimed)
        begin_nested_cm = AsyncMock()
        begin_nested_cm.__aenter__ = AsyncMock(return_value=None)
        begin_nested_cm.__aexit__ = AsyncMock(return_value=False)
        session.begin_nested = MagicMock(return_value=begin_nested_cm)
        return await mgr.claim(session, run_id=_RUN, gate_id=gate_id, org_id=_ORG, claimant_id=_USER)

    result_a, result_b = await asyncio.gather(
        _run_claim("gate-a", gate_a, claimed_a),
        _run_claim("gate-b", gate_b, claimed_b),
    )
    assert result_a is claimed_a
    assert result_b is claimed_b


# ---------------------------------------------------------------------------
# HitlClaim model columns remain stable (backwards compatibility)
# ---------------------------------------------------------------------------


def test_hitl_claim_model_columns_stable():
    """The HitlClaim model exposes the established columns (expires_at,
    claim_token, decision, decision_payload, account_id) so downstream consumers
    built against alpha are not broken by a column rename."""
    from sqlalchemy import inspect as sa_inspect

    from modulo.db.models.hitl_claim import HitlClaim

    columns = {c.name for c in sa_inspect(HitlClaim).columns}
    expected_columns = (
        "run_id",
        "gate_id",
        "pipeline_id",
        "account_id",
        "claim_token",
        "expires_at",
        "decision",
        "decision_payload",
    )
    for expected in expected_columns:
        assert expected in columns, f"HitlClaim is missing expected column {expected!r}"


# ---------------------------------------------------------------------------
# required_team_id extraction — invalid UUID handled gracefully (logged, not raised)
# ---------------------------------------------------------------------------


async def _interrupt_payload(gate_config: dict[str, Any]) -> dict[str, Any]:
    """Run a hitl_gate node fn built from ``gate_config`` and return the interrupt payload.

    Mirrors ``test_node_runner_hitl.py``: ``interrupt`` is stubbed to raise
    ``GraphInterrupt`` carrying its value so the first-invocation payload can
    be inspected without a LangGraph runtime.
    """
    from langgraph.errors import GraphInterrupt
    from langgraph.types import Interrupt

    from modulo.core.pipeline_engine.node_runner import make_hitl_gate_fn

    def _raise_interrupt(value: Any) -> None:
        raise GraphInterrupt((Interrupt(value=value),))

    node_fn = make_hitl_gate_fn(gate_config)
    with (
        patch("modulo.core.pipeline_engine.node_runner.interrupt", side_effect=_raise_interrupt),
        pytest.raises(GraphInterrupt) as exc_info,
    ):
        await node_fn({"artifacts": [], "_hitl_gates": []})
    interrupt_list = exc_info.value.args[0]
    return interrupt_list[0].value


async def test_hitl_gate_invalid_required_team_id_is_logged_and_sanitized(caplog) -> None:
    """An unparseable ``required_team_id`` on a gate config is logged and
    normalised to None in the interrupt payload — the executor's
    ``uuid.UUID()`` conversion must never see an invalid string (logged, not
    raised)."""
    caplog.set_level("WARNING", logger="modulo.core.pipeline_engine.node_runner")
    payload = await _interrupt_payload(
        {"gate_id": "review-step", "human_only": False, "required_team_id": "not-a-uuid"}
    )
    assert payload["required_team_id"] is None
    assert "hitl_gate.invalid_required_team_id" in caplog.text


async def test_hitl_gate_valid_required_team_id_passes_through() -> None:
    """A valid UUID string is preserved in the interrupt payload unchanged."""
    team_id = uuid.uuid4()
    payload = await _interrupt_payload(
        {"gate_id": "review-step", "human_only": False, "required_team_id": str(team_id)}
    )
    assert payload["required_team_id"] == str(team_id)


async def test_hitl_gate_required_team_id_absent_is_none() -> None:
    """Gate without ``required_team_id`` still carries None (no restriction)."""
    payload = await _interrupt_payload({"gate_id": "review-step", "human_only": False})
    assert payload["required_team_id"] is None


# ---------------------------------------------------------------------------
# FAR-604 D3 — un-park on decision
# ---------------------------------------------------------------------------


def _session_decide_capturing(
    *,
    update_returns_id: uuid.UUID | None,
    session_get_gate: HitlClaim | None,
) -> tuple[AsyncMock, list[Any]]:
    """Session mock for _decide() that CAPTURES every issued statement.

    Call sequence in _decide():
      1. decision UPDATE … RETURNING id  (scalar_one_or_none → update_returns_id)
      2. un-park UPDATE (guarded hitl_parked → awaiting_human)
      3. session.get() → session_get_gate
    """
    session = AsyncMock()
    session.add = MagicMock()
    session.flush = AsyncMock()
    begin_nested_cm = AsyncMock()
    begin_nested_cm.__aenter__ = AsyncMock(return_value=None)
    begin_nested_cm.__aexit__ = AsyncMock(return_value=False)
    session.begin_nested = MagicMock(return_value=begin_nested_cm)
    stmts: list[Any] = []
    update_result = MagicMock()
    update_result.scalar_one_or_none.return_value = update_returns_id
    unpark_result = MagicMock()
    unpark_result.rowcount = 1

    async def _execute(stmt: Any) -> Any:
        stmts.append(stmt)
        if len(stmts) == 1:
            return update_result
        return unpark_result

    session.execute = _execute
    session.get = AsyncMock(return_value=session_get_gate)
    return session, stmts


def _bound_values(stmt: Any) -> set[str]:
    return {str(v) for v in stmt.compile().params.values()}


async def test_decide_unparks_parked_run():
    """FAR-604 D3: a decision commits an un-park (``hitl_parked`` →
    ``awaiting_human``) so the parked run re-enters normal admission — the
    API route's executor.resume picks it up immediately; the MCP flow's
    committed-decision reconcile path resumes it when a slot frees."""
    future = datetime.now(UTC) + timedelta(minutes=5)
    gate = _gate(account_id=_USER, claim_token="good-token", expires_at=future)
    gate_decided = _gate(account_id=None, claim_token=None, expires_at=None, decision="approved")
    session, stmts = _session_decide_capturing(update_returns_id=gate.id, session_get_gate=gate_decided)
    mgr = HITLManager()
    result = await mgr.approve(session, run_id=_RUN, gate_id=_GATE, org_id=_ORG, claim_token="good-token")
    assert result.decision == "approved"
    # The un-park is issued directly after the decision commit (before the
    # audit append) and is guarded to parked runs only.
    assert len(stmts) >= 2
    unpark_values = _bound_values(stmts[1])
    assert unpark_values == {"hitl_parked", "awaiting_human", str(_RUN), str(_ORG)}


async def test_decide_unpark_is_guarded_to_parked_status():
    """The un-park predicate matches ``status = 'hitl_parked'`` ONLY — a
    running/claimed/awaiting_human run is never touched by the write."""
    future = datetime.now(UTC) + timedelta(minutes=5)
    gate = _gate(account_id=_USER, claim_token="good-token", expires_at=future)
    gate_decided = _gate(account_id=None, claim_token=None, expires_at=None, decision="rejected")
    session, stmts = _session_decide_capturing(update_returns_id=gate.id, session_get_gate=gate_decided)
    mgr = HITLManager()
    await mgr.reject(session, run_id=_RUN, gate_id=_GATE, org_id=_ORG, claim_token="good-token", reason="no")
    assert len(stmts) >= 2
    unpark_values = _bound_values(stmts[1])
    # The guard value (hitl_parked) and target (awaiting_human) are both
    # bound — every other status is structurally excluded by the predicate.
    assert unpark_values == {"hitl_parked", "awaiting_human", str(_RUN), str(_ORG)}


async def test_decide_failure_does_not_unpark():
    """A refused decision (expired token) never issues the un-park write."""
    past = datetime.now(UTC) - timedelta(minutes=1)
    gate = _gate(account_id=_USER, claim_token="tok", expires_at=past)
    session = AsyncMock()
    session.add = MagicMock()
    session.flush = AsyncMock()
    begin_nested_cm = AsyncMock()
    begin_nested_cm.__aenter__ = AsyncMock(return_value=None)
    begin_nested_cm.__aexit__ = AsyncMock(return_value=False)
    session.begin_nested = MagicMock(return_value=begin_nested_cm)
    stmts: list[Any] = []
    update_result = MagicMock()
    update_result.scalar_one_or_none.return_value = None
    diag_result = MagicMock()
    diag_result.scalar_one_or_none.return_value = gate

    async def _execute(stmt: Any) -> Any:
        stmts.append(stmt)
        return update_result if len(stmts) == 1 else diag_result

    session.execute = _execute
    session.get = AsyncMock(return_value=None)
    mgr = HITLManager()
    with pytest.raises(ClaimTokenExpiredError):
        await mgr.approve(session, run_id=_RUN, gate_id=_GATE, org_id=_ORG, claim_token="tok")
    # The decision UPDATE + the diagnosis SELECT ran — NO un-park write.
    assert len(stmts) == 2
    for stmt in stmts:
        assert "hitl_parked" not in _bound_values(stmt)
