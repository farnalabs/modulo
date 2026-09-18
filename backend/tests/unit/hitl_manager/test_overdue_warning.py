"""Unit tests for overdue_warning.get_overdue_claims and dispatch_overdue_notifications."""

import asyncio
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from modulo.core.hitl_manager.overdue_warning import (
    DEFAULT_ESCALATION_HOURS,
    DEFAULT_WARNING_HOURS,
    dispatch_overdue_notifications,
    get_overdue_claims,
)
from modulo.db.models.hitl_claim import HitlClaim

_ORG = uuid.uuid4()


def _claim(
    *,
    claimed_at: datetime | None = None,
    run_id: uuid.UUID | None = None,
    gate_id: str = "review-step",
    account_id: uuid.UUID | None = None,
    decision: str | None = None,
) -> MagicMock:
    g = MagicMock(spec=HitlClaim)
    g.id = uuid.uuid4()
    g.run_id = run_id or uuid.uuid4()
    g.gate_id = gate_id
    g.organisation_id = _ORG
    g.claimed_at = claimed_at or (datetime.now(UTC) if account_id else None)
    g.pipeline_id = uuid.uuid4()
    g.decision = decision
    g.account_id = account_id
    return g


def _mock_session(claims: list) -> AsyncMock:
    """Build an AsyncMock session where execute → scalars → all → claims."""
    scalars_mock = MagicMock()
    scalars_mock.all.return_value = claims

    result_mock = MagicMock()
    result_mock.scalars.return_value = scalars_mock

    session = AsyncMock()
    session.execute = AsyncMock(return_value=result_mock)
    return session


async def test_returns_claims_older_than_warning_threshold() -> None:
    now = datetime.now(UTC)
    mock_old = _claim(claimed_at=now - timedelta(hours=10), account_id=uuid.uuid4())

    session = _mock_session([mock_old])
    result = await get_overdue_claims(session, _ORG, warning_hours=4)

    assert len(result) == 1
    assert result[0]["id"] == str(mock_old.id)
    assert result[0]["pipeline_run_id"] == str(mock_old.run_id)
    assert result[0]["node_id"] == mock_old.gate_id
    assert result[0]["age_hours"] >= 9.9
    assert result[0]["status"] == "warning"


async def test_respects_warning_hours_threshold() -> None:
    now = datetime.now(UTC)
    mock_oldish = _claim(claimed_at=now - timedelta(hours=6), account_id=uuid.uuid4())

    session = _mock_session([mock_oldish])
    result = await get_overdue_claims(session, _ORG, warning_hours=4)

    assert len(result) == 1
    assert result[0]["status"] == "warning"


async def test_escalates_claims_older_than_escalation_threshold() -> None:
    now = datetime.now(UTC)
    mock_warning = _claim(claimed_at=now - timedelta(hours=8), account_id=uuid.uuid4())
    mock_escalated = _claim(claimed_at=now - timedelta(hours=48), account_id=uuid.uuid4())

    session = _mock_session([mock_warning, mock_escalated])
    result = await get_overdue_claims(session, _ORG, warning_hours=4)

    assert len(result) == 2
    statuses = {r["id"]: r["status"] for r in result}
    assert statuses[str(mock_warning.id)] == "warning"
    assert statuses[str(mock_escalated.id)] == "escalated"


async def test_ignores_decided_claims() -> None:
    now = datetime.now(UTC)
    mock_pending = _claim(claimed_at=now - timedelta(hours=10), account_id=uuid.uuid4())
    mock_decided = _claim(
        claimed_at=now - timedelta(hours=10),
        account_id=uuid.uuid4(),
        decision="approved",
    )

    # The DB query includes decision IS NULL in its WHERE clause
    # (see overdue_warning.py line 54). The mock simulates that
    # by only including pending claims.
    session = _mock_session([mock_pending])
    result = await get_overdue_claims(session, _ORG, warning_hours=4)

    assert len(result) == 1
    assert result[0]["id"] == str(mock_pending.id)
    assert result[0]["id"] != str(mock_decided.id)


async def test_returns_empty_when_no_overdue_claims() -> None:
    session = _mock_session([])
    result = await get_overdue_claims(session, _ORG, warning_hours=4)

    assert result == []


async def test_rejects_negative_warning_hours() -> None:
    session = AsyncMock()
    with pytest.raises(ValueError, match="warning_hours must be non-negative"):
        await get_overdue_claims(session, _ORG, warning_hours=-1)


async def test_rejects_negative_escalation_hours() -> None:
    session = AsyncMock()
    with pytest.raises(ValueError, match="escalation_hours must be non-negative"):
        await get_overdue_claims(session, _ORG, escalation_hours=-5)


async def test_rejects_escalation_hours_not_greater_than_warning() -> None:
    session = AsyncMock()
    with pytest.raises(ValueError, match=r"escalation_hours .* must exceed warning_hours"):
        await get_overdue_claims(session, _ORG, warning_hours=6, escalation_hours=6)


async def test_returns_empty_when_query_fails() -> None:
    session = AsyncMock()
    session.execute = AsyncMock(side_effect=RuntimeError("db down"))
    result = await get_overdue_claims(session, _ORG, warning_hours=4)

    assert result == []


# ---------------------------------------------------------------------------
# dispatch_overdue_notifications
# ---------------------------------------------------------------------------


def _mock_begin(session: AsyncMock) -> None:
    begin_cm = AsyncMock()
    begin_cm.__aenter__ = AsyncMock(return_value=None)
    begin_cm.__aexit__ = AsyncMock(return_value=False)
    session.begin = MagicMock(return_value=begin_cm)


def _mock_session_factory(session: AsyncMock) -> MagicMock:
    cm = AsyncMock()
    cm.__aenter__ = AsyncMock(return_value=session)
    cm.__aexit__ = AsyncMock(return_value=False)
    return MagicMock(return_value=cm)


def _overdue_row(
    *,
    claim_id: uuid.UUID | None = None,
    run_id: uuid.UUID | None = None,
    gate_id: str = "review-step",
    claimed_at: datetime,
    pipeline_name: str = "My Pipeline",
) -> tuple[object, str]:
    claim = type(
        "Claim",
        (),
        {
            "id": claim_id or uuid.uuid4(),
            "run_id": run_id or uuid.uuid4(),
            "gate_id": gate_id,
            "claimed_at": claimed_at,
        },
    )()
    return (claim, pipeline_name)


def _org_list_session() -> AsyncMock:
    session = AsyncMock()
    result = MagicMock()
    result.scalars.return_value = [_ORG]
    session.execute = AsyncMock(return_value=result)
    _mock_begin(session)
    return session


def _tx_session(
    rows: list[tuple[object, str]],
    *,
    lock_acquired: bool = True,
    lock_failure: Exception | None = None,
) -> AsyncMock:
    """Per-org transaction session: lock query (two args) then overdue rows (one arg)."""
    tx_session = AsyncMock(name="tx_session")
    _mock_begin(tx_session)

    lock_result = MagicMock()
    lock_result.scalar_one.return_value = lock_acquired
    rows_result = MagicMock()
    rows_result.all.return_value = rows

    execute_results: list[MagicMock] = [lock_result, rows_result]
    execute_call_count = 0

    async def _execute(stmt: object, *args: object) -> MagicMock:
        nonlocal execute_call_count
        if lock_failure is not None and execute_call_count == 0:
            execute_call_count += 1
            raise lock_failure
        idx = execute_call_count
        execute_call_count += 1
        return execute_results[idx]

    tx_session.execute = _execute
    return tx_session


def _stamp_session() -> AsyncMock:
    session = AsyncMock(name="stamp_session")
    _mock_begin(session)
    session.execute = AsyncMock(return_value=MagicMock())
    return session


async def _run_dispatch(
    *,
    rows: list[tuple[object, str]],
    notifier: Any = None,
    dispatch_side_effect: Any = None,
    lock_acquired: bool = True,
    lock_failure: Exception | None = None,
    warning_hours: int = DEFAULT_WARNING_HOURS,
) -> tuple[list[dict], AsyncMock | None]:
    """Run dispatch_overdue_notifications against fully mocked sessions.

    Returns ``(dispatched, notifier_mock)`` so tests can assert on the returned
    entries and the dispatch calls.
    """
    if notifier is not None and dispatch_side_effect is not None:
        notifier.dispatch_event = AsyncMock(side_effect=dispatch_side_effect)

    org_factory = _mock_session_factory(_org_list_session())
    tx_session = _tx_session(rows, lock_acquired=lock_acquired, lock_failure=lock_failure)
    stamp_session = _stamp_session()

    factory_call_count = 0

    def _factory_side_effect() -> AsyncMock:
        nonlocal factory_call_count
        factory_call_count += 1
        if factory_call_count == 1:
            return org_factory()
        if factory_call_count == 2:
            return _mock_session_factory(tx_session)()
        return _mock_session_factory(stamp_session)()

    factory = MagicMock(side_effect=_factory_side_effect)

    with patch("modulo.core.hitl_manager.overdue_warning.set_rls_org", new=AsyncMock()):
        dispatched = await dispatch_overdue_notifications(
            factory,
            notifier=notifier,
            warning_hours=warning_hours,
        )

    return dispatched, notifier


async def test_dispatch_sends_hitl_overdue_events() -> None:
    now = datetime.now(UTC)
    run_1, run_2 = uuid.uuid4(), uuid.uuid4()
    claim_1 = uuid.uuid4()
    rows = [
        _overdue_row(claim_id=claim_1, run_id=run_1, gate_id="gate-a", claimed_at=now - timedelta(hours=10)),
        _overdue_row(run_id=run_2, gate_id="gate-b", claimed_at=now - timedelta(hours=48)),
    ]
    notifier = AsyncMock()
    dispatched, notifier = await _run_dispatch(rows=rows, notifier=notifier)

    assert len(dispatched) == 2
    assert dispatched[0]["run_id"] == run_1
    assert dispatched[0]["gate_id"] == "gate-a"
    assert dispatched[0]["pipeline_name"] == "My Pipeline"
    assert dispatched[0]["minutes_overdue"] == 600
    assert dispatched[1]["minutes_overdue"] == 2880

    assert notifier.dispatch_event.call_count == 2
    call_1 = notifier.dispatch_event.call_args_list[0]
    assert call_1.kwargs["event_type"] == "hitl_overdue"
    assert call_1.kwargs["org_id"] == _ORG
    assert call_1.kwargs["run_id"] == str(run_1)
    assert call_1.kwargs["payload"] == {
        "run_id": str(run_1),
        "gate_id": "gate-a",
        "pipeline_name": "My Pipeline",
        "minutes_overdue": 600,
    }


async def test_dispatch_empty_when_none_overdue() -> None:
    notifier = AsyncMock()
    dispatched, notifier = await _run_dispatch(rows=[], notifier=notifier)

    assert dispatched == []
    notifier.dispatch_event.assert_not_called()


async def test_dispatch_no_notifier_skips() -> None:
    dispatched, _ = await _run_dispatch(rows=[_overdue_row(claimed_at=datetime.now(UTC) - timedelta(hours=10))])

    assert dispatched == []


async def test_dispatch_notifier_failure_does_not_crash() -> None:
    notifier = MagicMock()
    dispatched, _ = await _run_dispatch(
        rows=[_overdue_row(claimed_at=datetime.now(UTC) - timedelta(hours=10))],
        notifier=notifier,
        dispatch_side_effect=RuntimeError("network error"),
    )

    # Nothing was dispatched successfully, so nothing is stamped or returned.
    assert dispatched == []


async def test_dispatch_skips_org_when_lock_denied() -> None:
    notifier = AsyncMock()
    dispatched, notifier = await _run_dispatch(
        rows=[_overdue_row(claimed_at=datetime.now(UTC) - timedelta(hours=10))],
        notifier=notifier,
        lock_acquired=False,
    )

    assert dispatched == []
    notifier.dispatch_event.assert_not_called()


async def test_dispatch_proceeds_when_lock_query_fails() -> None:
    notifier = AsyncMock()
    dispatched, notifier = await _run_dispatch(
        rows=[_overdue_row(claimed_at=datetime.now(UTC) - timedelta(hours=10))],
        notifier=notifier,
        lock_failure=RuntimeError("pg_advisory_xact_lock unavailable"),
    )

    assert len(dispatched) == 1
    assert notifier.dispatch_event.call_count == 1


async def test_dispatch_lock_cancellation_propagates() -> None:
    with pytest.raises(asyncio.CancelledError):
        await _run_dispatch(
            rows=[_overdue_row(claimed_at=datetime.now(UTC) - timedelta(hours=10))],
            lock_failure=asyncio.CancelledError(),
        )


async def test_dispatch_cancellation_propagates() -> None:
    notifier = MagicMock()

    async def _cancel(*args: object, **kwargs: object) -> None:
        raise asyncio.CancelledError

    notifier.dispatch_event = _cancel
    with pytest.raises(asyncio.CancelledError):
        await _run_dispatch(
            rows=[_overdue_row(claimed_at=datetime.now(UTC) - timedelta(hours=10))],
            notifier=notifier,
        )


async def test_dispatch_rejects_negative_warning_hours() -> None:
    with pytest.raises(ValueError, match="warning_hours must be non-negative"):
        await _run_dispatch(rows=[], warning_hours=-1)


async def test_age_hours_floor_at_zero_for_future_claimed_at() -> None:
    """A future claimed_at (clock skew) must report age_hours 0, not negative."""
    now = datetime.now(UTC)
    future_claim = _claim(claimed_at=now + timedelta(hours=1), account_id=uuid.uuid4())

    session = _mock_session([future_claim])
    result = await get_overdue_claims(session, _ORG, warning_hours=4)

    assert len(result) == 1
    assert result[0]["age_hours"] == 0.0
    assert result[0]["status"] == "warning"


async def test_filters_claims_with_null_claimed_at() -> None:
    """Claims whose claimed_at is NULL are skipped by the query and never reported."""
    null_claim = _claim(claimed_at=datetime.now(UTC) - timedelta(hours=100), account_id=uuid.uuid4())
    null_claim.claimed_at = None

    session = _mock_session([null_claim])
    result = await get_overdue_claims(session, _ORG, warning_hours=4)

    assert result == []


async def test_query_filters_undecided_claimed_claims_for_org() -> None:
    """The SQL predicate scopes to org and only undecided, claimed, non-null claimed_at rows."""
    org_id = uuid.uuid4()
    session = _mock_session([])
    now = datetime.now(UTC)

    await get_overdue_claims(session, org_id, warning_hours=5, escalation_hours=10)

    stmt = session.execute.await_args.args[0]
    params = stmt.compile().params
    assert params["organisation_id_1"] == org_id
    cutoff = params["claimed_at_1"]
    assert abs((cutoff - (now - timedelta(hours=5))).total_seconds()) < 5
    # Predicate assertions must target the WHERE clause, not the full statement:
    # str(select(HitlClaim)) renders every mapped column, so column-name checks
    # would pass trivially even if the predicates were removed.
    where = str(stmt.whereclause)
    assert "account_id IS NOT NULL" in where
    assert "decision IS NULL" in where
    assert "claimed_at IS NOT NULL" in where


def test_default_thresholds_documented() -> None:
    """The shipped warning/escalation thresholds are the documented defaults."""
    assert DEFAULT_WARNING_HOURS == 4
    assert DEFAULT_ESCALATION_HOURS == 24
