"""Unit tests for ClaimExpiryJob."""

import uuid
from unittest.mock import AsyncMock, MagicMock, patch

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

    factory = MagicMock(return_value=cm)
    return factory


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


async def test_expire_once_resets_stale_claims() -> None:
    engine = MagicMock()
    job = ClaimExpiryJob(engine)

    org_session = _org_list_session()
    org_factory = _mock_session_factory(org_session)

    # Per-org transaction session
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

    stale_rows = _stale_rows_2()
    stale_result = MagicMock()
    stale_result.all.return_value = stale_rows

    execute_results: list[MagicMock] = [
        stale_result,  # SELECT stale claims
        MagicMock(),  # UPDATE claims
        MagicMock(),  # UPDATE runs
    ]
    execute_call_count = 0

    async def _execute(stmt: object) -> MagicMock:
        nonlocal execute_call_count
        idx = execute_call_count
        execute_call_count += 1
        return execute_results[idx]

    tx_session.execute = _execute

    # First factory call returns org session, second returns tx session
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
        patch("modulo.core.hitl_manager.expiry_job.append_audit_event", new=AsyncMock()) as mock_audit,
        patch("modulo.core.hitl_manager.expiry_job.set_rls_org", new=AsyncMock()),
    ):
        expired = await job._expire_once()

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
    engine = MagicMock()
    job = ClaimExpiryJob(engine)

    org_session = _org_list_session()
    org_factory = _mock_session_factory(org_session)

    tx_session = AsyncMock(name="tx_session")
    begin_cm = AsyncMock()
    begin_cm.__aenter__ = AsyncMock(return_value=None)
    begin_cm.__aexit__ = AsyncMock(return_value=False)
    tx_session.begin = MagicMock(return_value=begin_cm)
    begin_nested_cm = AsyncMock()
    begin_nested_cm.__aenter__ = AsyncMock(return_value=None)
    begin_nested_cm.__aexit__ = AsyncMock(return_value=False)
    tx_session.begin_nested = MagicMock(return_value=begin_nested_cm)

    # First execute returns no stale claims
    empty_result = MagicMock()
    empty_result.all.return_value = []
    tx_session.execute = AsyncMock(return_value=empty_result)

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
        patch("modulo.core.hitl_manager.expiry_job.append_audit_event", new=AsyncMock()) as mock_audit,
        patch("modulo.core.hitl_manager.expiry_job.set_rls_org", new=AsyncMock()),
    ):
        expired = await job._expire_once()

    assert expired == []
    mock_audit.assert_not_called()


async def test_expire_once_dispatches_notifications() -> None:
    """When a notifier is provided, claim_expired events are dispatched."""
    engine = MagicMock()
    notifier = AsyncMock()
    job = ClaimExpiryJob(engine, notifier=notifier)

    org_session = _org_list_session()
    org_factory = _mock_session_factory(org_session)

    tx_session = AsyncMock(name="tx_session")
    tx_session.add = MagicMock()
    tx_session.flush = AsyncMock()
    begin_cm = AsyncMock()
    begin_cm.__aenter__ = AsyncMock(return_value=None)
    begin_cm.__aexit__ = AsyncMock(return_value=False)
    tx_session.begin = MagicMock(return_value=begin_cm)
    begin_nested_cm = AsyncMock()
    begin_nested_cm.__aenter__ = AsyncMock(return_value=None)
    begin_nested_cm.__aexit__ = AsyncMock(return_value=False)
    tx_session.begin_nested = MagicMock(return_value=begin_nested_cm)

    stale_rows = _stale_rows_2()
    stale_result = MagicMock()
    stale_result.all.return_value = stale_rows

    execute_results: list[MagicMock] = [
        stale_result,  # SELECT stale claims
        MagicMock(),  # UPDATE claims
        MagicMock(),  # UPDATE runs
    ]
    execute_call_count = 0

    async def _execute(stmt: object) -> MagicMock:
        nonlocal execute_call_count
        idx = execute_call_count
        execute_call_count += 1
        return execute_results[idx]

    tx_session.execute = _execute

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
        patch("modulo.core.hitl_manager.expiry_job.append_audit_event", new=AsyncMock()),
        patch("modulo.core.hitl_manager.expiry_job.set_rls_org", new=AsyncMock()),
    ):
        expired = await job._expire_once()

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
    engine = MagicMock()
    job = ClaimExpiryJob(engine)  # no notifier

    org_session = _org_list_session()
    org_factory = _mock_session_factory(org_session)

    tx_session = AsyncMock(name="tx_session")
    tx_session.add = MagicMock()
    begin_cm = AsyncMock()
    begin_cm.__aenter__ = AsyncMock(return_value=None)
    begin_cm.__aexit__ = AsyncMock(return_value=False)
    tx_session.begin = MagicMock(return_value=begin_cm)
    begin_nested_cm = AsyncMock()
    begin_nested_cm.__aenter__ = AsyncMock(return_value=None)
    begin_nested_cm.__aexit__ = AsyncMock(return_value=False)
    tx_session.begin_nested = MagicMock(return_value=begin_nested_cm)

    stale_rows = _stale_rows_2()
    stale_result = MagicMock()
    stale_result.all.return_value = stale_rows

    execute_results: list[MagicMock] = [
        stale_result,
        MagicMock(),
        MagicMock(),
    ]
    execute_call_count = 0

    async def _execute(stmt: object) -> MagicMock:
        nonlocal execute_call_count
        idx = execute_call_count
        execute_call_count += 1
        return execute_results[idx]

    tx_session.execute = _execute

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
        patch("modulo.core.hitl_manager.expiry_job.append_audit_event", new=AsyncMock()),
        patch("modulo.core.hitl_manager.expiry_job.set_rls_org", new=AsyncMock()),
    ):
        expired = await job._expire_once()

    assert len(expired) == 2


async def test_expire_once_handles_notifier_failure() -> None:
    """Notifier failure should not crash the expiry loop."""
    engine = MagicMock()
    notifier = MagicMock()
    notifier.dispatch_event = AsyncMock(side_effect=RuntimeError("network error"))
    job = ClaimExpiryJob(engine, notifier=notifier)

    org_session = _org_list_session()
    org_factory = _mock_session_factory(org_session)

    tx_session = AsyncMock(name="tx_session")
    tx_session.add = MagicMock()
    tx_session.flush = AsyncMock()
    begin_cm = AsyncMock()
    begin_cm.__aenter__ = AsyncMock(return_value=None)
    begin_cm.__aexit__ = AsyncMock(return_value=False)
    tx_session.begin = MagicMock(return_value=begin_cm)
    begin_nested_cm = AsyncMock()
    begin_nested_cm.__aenter__ = AsyncMock(return_value=None)
    begin_nested_cm.__aexit__ = AsyncMock(return_value=False)
    tx_session.begin_nested = MagicMock(return_value=begin_nested_cm)

    stale_rows = _stale_rows_2()
    stale_result = MagicMock()
    stale_result.all.return_value = stale_rows

    execute_results: list[MagicMock] = [
        stale_result,
        MagicMock(),
        MagicMock(),
    ]
    execute_call_count = 0

    async def _execute(stmt: object) -> MagicMock:
        nonlocal execute_call_count
        idx = execute_call_count
        execute_call_count += 1
        return execute_results[idx]

    tx_session.execute = _execute

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
        patch("modulo.core.hitl_manager.expiry_job.append_audit_event", new=AsyncMock()),
        patch("modulo.core.hitl_manager.expiry_job.set_rls_org", new=AsyncMock()),
    ):
        # Should not raise despite notifier failure
        expired = await job._expire_once()

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
