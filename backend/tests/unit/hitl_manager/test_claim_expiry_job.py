"""Unit tests for ClaimExpiryJob."""

import asyncio
import uuid
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from modulo.core.hitl_manager.expiry_job import ClaimExpiryJob

_ORG = uuid.uuid4()
_CLAIM_ID_1 = uuid.uuid4()
_CLAIM_ID_2 = uuid.uuid4()
_RUN_1 = uuid.uuid4()
_RUN_2 = uuid.uuid4()
_USER_1 = uuid.uuid4()
_USER_2 = uuid.uuid4()
_GATE_A = "gate-a"
_GATE_B = "gate-b"


def _mock_session_factory(session: AsyncMock) -> MagicMock:
    """Build a factory that returns the given session from both __aenter__ calls."""
    cm = AsyncMock()
    cm.__aenter__ = AsyncMock(return_value=session)
    cm.__aexit__ = AsyncMock(return_value=False)

    return MagicMock(return_value=cm)


def _org_list_session() -> AsyncMock:
    """Session mock that returns a single org ID on execute."""
    session = AsyncMock()
    result = MagicMock()
    result.scalars.return_value = [_ORG]
    session.execute = AsyncMock(return_value=result)
    return session


def _stale_rows_2() -> list[object]:
    """Two stale claims as attribute-accessible objects (like SQLAlchemy Row)."""
    return [
        type("Row", (), {"id": _CLAIM_ID_1, "run_id": _RUN_1, "gate_id": _GATE_A, "account_id": _USER_1})(),
        type("Row", (), {"id": _CLAIM_ID_2, "run_id": _RUN_2, "gate_id": _GATE_B, "account_id": _USER_2})(),
    ]


def _tx_session(
    stale_rows: list[object],
    *,
    lock_acquired: bool = True,
    lock_failure: Exception | None = None,
) -> AsyncMock:
    """Per-org transaction session whose execute calls return the given stale rows.

    The advisory-lock query (the first execute call, two positional args) returns
    ``lock_acquired`` via ``scalar_one``, or raises ``lock_failure`` when set, so
    tests can exercise the lock-acquired, lock-denied, and lock-failure branches.
    """
    tx_session = AsyncMock(name="tx_session")
    tx_session.add = MagicMock()
    tx_session.flush = AsyncMock()
    begin_cm = AsyncMock()
    begin_cm.__aenter__ = AsyncMock(return_value=None)
    begin_cm.__aexit__ = AsyncMock(return_value=False)
    tx_session.begin = MagicMock(return_value=begin_cm)
    # Support begin_nested() for savepoint-based audit events
    begin_nested_cm = AsyncMock()
    begin_nested_cm.__aenter__ = AsyncMock(return_value=None)
    begin_nested_cm.__aexit__ = AsyncMock(return_value=False)
    tx_session.begin_nested = MagicMock(return_value=begin_nested_cm)

    lock_result = MagicMock()
    lock_result.scalar_one.return_value = lock_acquired

    stale_result = MagicMock()
    stale_result.all.return_value = stale_rows

    execute_results: list[MagicMock] = [
        lock_result,  # SELECT pg_try_advisory_xact_lock (first call)
        stale_result,  # SELECT stale claims
        MagicMock(),  # UPDATE claims
        MagicMock(),  # UPDATE runs
    ]

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


async def _run_expire_once(
    *,
    stale_rows: list[object],
    notifier: Any = None,
    dispatch_side_effect: Any = None,
    lock_acquired: bool = True,
    lock_failure: Exception | None = None,
    audit_side_effect: Any = None,
) -> tuple[list[dict[str, Any]], AsyncMock]:
    """Run one ClaimExpiryJob._expire_once() pass against fully mocked sessions.

    Returns ``(expired_claims, mock_audit)`` so tests can assert on both the
    returned rows and the audit events logged.
    """
    engine = MagicMock()
    job = ClaimExpiryJob(engine, notifier=notifier)
    if notifier is not None and dispatch_side_effect is not None:
        notifier.dispatch_event = AsyncMock(side_effect=dispatch_side_effect)

    org_session = _org_list_session()
    org_factory = _mock_session_factory(org_session)
    tx_session = _tx_session(stale_rows, lock_acquired=lock_acquired, lock_failure=lock_failure)

    factory_call_count = 0

    def _factory_side_effect() -> AsyncMock:
        nonlocal factory_call_count
        if factory_call_count == 0:
            factory_call_count += 1
            return org_factory()
        return _mock_session_factory(tx_session)()

    factory = MagicMock(side_effect=_factory_side_effect)

    with (
        patch.object(job, "_session_factory", factory),
        patch(
            "modulo.core.hitl_manager.expiry_job.append_audit_event",
            new=AsyncMock(side_effect=audit_side_effect),
        ) as mock_audit,
        patch("modulo.core.hitl_manager.expiry_job.set_rls_org", new=AsyncMock()),
    ):
        expired = await job._expire_once()

    return expired, mock_audit


async def test_expire_once_resets_stale_claims() -> None:
    expired, mock_audit = await _run_expire_once(stale_rows=_stale_rows_2())

    assert len(expired) == 2
    assert expired[0]["run_id"] == _RUN_1
    assert expired[0]["gate_id"] == _GATE_A
    assert expired[0]["claimed_by"] == _USER_1
    assert expired[1]["run_id"] == _RUN_2
    assert expired[1]["gate_id"] == _GATE_B
    assert expired[1]["claimed_by"] == _USER_2

    # Verify audit events were logged for each expired claim
    assert mock_audit.call_count == 2
    audit_call_1 = mock_audit.call_args_list[0]
    assert audit_call_1.kwargs["event_type"] == "hitl.claim_expired"
    assert audit_call_1.kwargs["resource_id"] == _CLAIM_ID_1
    assert audit_call_1.kwargs["org_id"] == _ORG

    audit_call_2 = mock_audit.call_args_list[1]
    assert audit_call_2.kwargs["resource_id"] == _CLAIM_ID_2


async def test_expire_once_empty_when_none_stale() -> None:
    expired, mock_audit = await _run_expire_once(stale_rows=[])

    assert expired == []
    mock_audit.assert_not_called()


async def test_expire_once_dispatches_notifications() -> None:
    """When a notifier is provided, claim_expired events are dispatched."""
    notifier = AsyncMock()
    expired, _ = await _run_expire_once(stale_rows=_stale_rows_2(), notifier=notifier)

    assert len(expired) == 2
    assert notifier.dispatch_event.call_count == 2

    # First notification
    call_1 = notifier.dispatch_event.call_args_list[0]
    assert call_1.kwargs["event_type"] == "claim_expired"
    assert call_1.kwargs["org_id"] == _ORG
    assert call_1.kwargs["payload"]["gate_id"] == _GATE_A

    # Second notification
    call_2 = notifier.dispatch_event.call_args_list[1]
    assert call_2.kwargs["payload"]["gate_id"] == _GATE_B


async def test_expire_once_no_notifier_skips_dispatch() -> None:
    """When notifier is None, no dispatch happens."""
    expired, _ = await _run_expire_once(stale_rows=_stale_rows_2())

    assert len(expired) == 2


async def test_expire_once_handles_notifier_failure() -> None:
    """Notifier failure should not crash the expiry loop."""
    notifier = MagicMock()
    expired, _ = await _run_expire_once(
        stale_rows=_stale_rows_2(),
        notifier=notifier,
        dispatch_side_effect=RuntimeError("network error"),
    )

    assert len(expired) == 2


async def test_start_and_stop_lifecycle() -> None:
    engine = MagicMock()
    job = ClaimExpiryJob(engine)

    await job.start()
    assert job._task is not None
    assert not job._task.done()

    await job.stop()
    assert job._task is None


async def test_notifier_passed_through_constructor() -> None:
    """Notifier is stored as _notifier on the job."""
    engine = MagicMock()
    notifier = object()
    job = ClaimExpiryJob(engine, notifier=notifier)
    assert job._notifier is notifier


async def test_no_notifier_defaults_to_none() -> None:
    """When notifier is not provided, _notifier is None."""
    engine = MagicMock()
    job = ClaimExpiryJob(engine)
    assert job._notifier is None


async def test_expire_once_skips_org_when_lock_denied() -> None:
    """When the advisory lock is not acquired, the org is skipped entirely."""
    expired, mock_audit = await _run_expire_once(stale_rows=_stale_rows_2(), lock_acquired=False)

    assert expired == []
    mock_audit.assert_not_called()


async def test_expire_once_proceeds_when_lock_query_fails() -> None:
    """A failing lock query logs a warning but still processes stale claims."""
    expired, mock_audit = await _run_expire_once(
        stale_rows=_stale_rows_2(),
        lock_failure=RuntimeError("pg_advisory_xact_lock unavailable"),
    )

    assert len(expired) == 2
    assert mock_audit.call_count == 2


async def test_expire_once_lock_query_cancellation_propagates() -> None:
    """Cancellation during the advisory-lock query must propagate."""
    with pytest.raises(asyncio.CancelledError):
        await _run_expire_once(
            stale_rows=_stale_rows_2(),
            lock_failure=asyncio.CancelledError(),
        )


async def test_expire_once_audit_cancellation_propagates() -> None:
    """Cancellation while writing an audit event must not be swallowed."""
    with pytest.raises(asyncio.CancelledError):
        await _run_expire_once(
            stale_rows=_stale_rows_2(),
            audit_side_effect=asyncio.CancelledError(),
        )


async def test_expire_once_audit_failure_does_not_abort_org() -> None:
    """A single failed audit event is logged and the remaining claims still get audited."""
    calls = {"n": 0}

    def _audit_side_effect(*args: Any, **kwargs: Any) -> Any:
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("audit write failed")
        return MagicMock()

    expired, mock_audit = await _run_expire_once(stale_rows=_stale_rows_2(), audit_side_effect=_audit_side_effect)

    assert len(expired) == 2
    assert mock_audit.call_count == 2


async def test_expire_once_notifier_cancelled_error_propagates() -> None:
    """Cancellation while dispatching notifications must not be swallowed."""
    notifier = MagicMock()

    async def _cancel(*args: Any, **kwargs: Any) -> None:
        raise asyncio.CancelledError()

    notifier.dispatch_event = _cancel
    with pytest.raises(asyncio.CancelledError):
        await _run_expire_once(stale_rows=_stale_rows_2(), notifier=notifier)


async def test_start_is_noop_when_already_running() -> None:
    """Calling start() twice must not spawn a second polling task."""
    engine = MagicMock()
    job = ClaimExpiryJob(engine)
    await job.start()
    first_task = job._task
    assert first_task is not None

    await job.start()
    assert job._task is first_task

    await job.stop()


async def test_stop_is_noop_when_not_started() -> None:
    """Calling stop() before start() must not raise."""
    engine = MagicMock()
    job = ClaimExpiryJob(engine)
    assert job._task is None

    await job.stop()
    assert job._task is None


async def test_run_loop_stops_immediately_when_stop_event_set() -> None:
    """_run() returns immediately when the stop event is already set."""
    engine = MagicMock()
    job = ClaimExpiryJob(engine)
    job._stop_event.set()

    with patch.object(job, "_expire_once", new=AsyncMock()) as mock_expire:
        await job._run()

    mock_expire.assert_not_called()


async def test_run_loop_ticks_then_sleeps_and_logs_expired() -> None:
    """_run() calls _expire_once() and logs when claims were expired."""
    engine = MagicMock()
    job = ClaimExpiryJob(engine)
    job._expire_once = AsyncMock(return_value=[{"claim_id": _CLAIM_ID_1}])

    with patch("modulo.core.hitl_manager.expiry_job.asyncio.sleep", new=AsyncMock(side_effect=asyncio.CancelledError)):
        await job._run()

    job._expire_once.assert_awaited_once()


async def test_run_loop_survives_tick_failure() -> None:
    """A failing tick is logged and the loop continues to the next tick."""
    engine = MagicMock()
    job = ClaimExpiryJob(engine)
    job._expire_once = AsyncMock(side_effect=RuntimeError("db down"))
    ticks = {"n": 0}

    async def _sleep(delay: float) -> None:
        ticks["n"] += 1
        if ticks["n"] >= 2:
            job._stop_event.set()

    with patch("modulo.core.hitl_manager.expiry_job.asyncio.sleep", new=_sleep):
        await job._run()

    assert job._expire_once.await_count == 2


async def test_run_loop_stops_when_sleep_cancelled() -> None:
    """Cancellation during the sleep phase ends the loop cleanly."""
    engine = MagicMock()
    job = ClaimExpiryJob(engine)
    job._expire_once = AsyncMock(return_value=[])

    with patch("modulo.core.hitl_manager.expiry_job.asyncio.sleep", new=AsyncMock(side_effect=asyncio.CancelledError)):
        await job._run()

    job._expire_once.assert_awaited_once()


async def test_run_loop_stops_when_tick_cancelled() -> None:
    """Cancellation during the tick phase ends the loop cleanly."""
    engine = MagicMock()
    job = ClaimExpiryJob(engine)
    job._expire_once = AsyncMock(side_effect=asyncio.CancelledError)

    with patch("modulo.core.hitl_manager.expiry_job.asyncio.sleep", new=AsyncMock()) as mock_sleep:
        await job._run()

    job._expire_once.assert_awaited_once()
    mock_sleep.assert_not_awaited()
