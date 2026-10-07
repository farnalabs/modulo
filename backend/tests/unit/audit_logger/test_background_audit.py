"""Unit tests for the background-path audit writers (FAR-1549).

``append_background_audit_event`` and ``record_run_state_change_audits`` are
the two appends reachable from non-request write paths (SAQ crons, reconcilers,
retention sweeps). These tests pin their contract without a database:

* blank ``event_type`` / ``actor_source`` are rejected before any write;
* the SYSTEM actor markers are stamped and never a fabricated user id;
* a failed append is logged under the caller's ``log_key`` and swallowed
  (fail open with a log), never raised into the already-committed mutation;
* the batch writer groups entries per organisation, drops runs whose live
  status is not what the caller expects (phantom-event guard), and isolates
  one organisation's failure from the next.
"""

from __future__ import annotations

import asyncio
import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from modulo.core.audit_logger import background
from modulo.core.audit_logger.labels import SYSTEM_ACTOR

_ORG_A = uuid.UUID("00000000-0000-0000-0000-0000000000aa")
_ORG_B = uuid.UUID("00000000-0000-0000-0000-0000000000bb")
_RUN_ONE = uuid.UUID("00000000-0000-0000-0000-000000000011")
_RUN_TWO = uuid.UUID("00000000-0000-0000-0000-000000000012")

_LOG_KEY = "test.background.audit_failed"


def _factory(session: MagicMock | None = None) -> MagicMock:
    """A sessionmaker mock usable as ``async with factory() as s, s.begin():``."""
    if session is None:
        session = MagicMock()
    begin_cm = MagicMock()
    begin_cm.__aenter__ = AsyncMock(return_value=session)
    begin_cm.__aexit__ = AsyncMock(return_value=False)
    session.begin.return_value = begin_cm
    session_cm = MagicMock()
    session_cm.__aenter__ = AsyncMock(return_value=session)
    session_cm.__aexit__ = AsyncMock(return_value=False)
    factory = MagicMock()
    factory.return_value = session_cm
    return factory


def _run(run_id: uuid.UUID, org_id: uuid.UUID, status: str = "failed") -> SimpleNamespace:
    return SimpleNamespace(
        id=run_id,
        organisation_id=org_id,
        pipeline_id=uuid.UUID("00000000-0000-0000-0000-000000000099"),
        status=status,
        error_code="harness.heartbeat_stale",
        error_detail="Slot reconciliation: heartbeat stale past threshold.",
    )


def _session_returning(runs: list[SimpleNamespace]) -> MagicMock:
    session = MagicMock()
    result = MagicMock()
    result.scalars.return_value.all.return_value = runs
    session.execute = AsyncMock(return_value=result)
    return session


# ---------------------------------------------------------------------------
# append_background_audit_event
# ---------------------------------------------------------------------------


async def test_blank_event_type_rejected_before_any_write() -> None:
    factory = _factory()

    with pytest.raises(ValueError, match="non-empty event_type"):
        await background.append_background_audit_event(
            factory,
            org_id=_ORG_A,
            event_type="   ",
            resource_type="run",
            actor_source="retention_cleanup",
            log_key=_LOG_KEY,
        )

    factory.assert_not_called()


async def test_blank_actor_source_rejected_before_any_write() -> None:
    """Provenance must be STATED — an empty actor_source is not a provenance."""
    factory = _factory()

    with pytest.raises(ValueError, match="non-empty actor_source"):
        await background.append_background_audit_event(
            factory,
            org_id=_ORG_A,
            event_type="run_retention_purge",
            resource_type="run",
            actor_source="  ",
            log_key=_LOG_KEY,
        )

    factory.assert_not_called()


async def test_success_stamps_system_actor_and_returns_true() -> None:
    session = MagicMock()
    factory = _factory(session)

    with (
        patch.object(background, "set_rls_org", new=AsyncMock()) as set_org,
        patch.object(background, "append_audit_event", new=AsyncMock()) as append,
    ):
        ok = await background.append_background_audit_event(
            factory,
            org_id=_ORG_A,
            event_type="run_retention_purge",
            resource_type="run",
            resource_id=_RUN_ONE,
            actor_source="retention_cleanup",
            payload_json={"purged_runs": 7},
            log_key=_LOG_KEY,
        )

    assert ok is True
    set_org.assert_awaited_once_with(session, _ORG_A)
    kwargs = append.await_args.kwargs
    assert kwargs["event_type"] == "run_retention_purge"
    # No actor is ever fabricated: the column stays NULL and the payload
    # carries the SYSTEM marker plus how the write was caused.
    assert kwargs["actor_user_id"] is None
    assert kwargs["resource_id"] == _RUN_ONE
    assert kwargs["payload_json"]["actor"] == SYSTEM_ACTOR
    assert kwargs["payload_json"]["actor_source"] == "retention_cleanup"
    assert kwargs["payload_json"]["purged_runs"] == 7


async def test_append_failure_logs_and_returns_false(caplog: pytest.LogCaptureFixture) -> None:
    factory = _factory()

    with (
        patch.object(background, "set_rls_org", new=AsyncMock()),
        patch.object(background, "append_audit_event", new=AsyncMock(side_effect=RuntimeError("db down"))),
        caplog.at_level("WARNING"),
    ):
        ok = await background.append_background_audit_event(
            factory,
            org_id=_ORG_A,
            event_type="run_retention_purge",
            resource_type="run",
            actor_source="retention_cleanup",
            log_key=_LOG_KEY,
        )

    assert ok is False
    assert any(_LOG_KEY in record.message for record in caplog.records)


async def test_cancellation_is_never_swallowed() -> None:
    factory = _factory()

    with (
        patch.object(background, "set_rls_org", new=AsyncMock()),
        patch.object(background, "append_audit_event", new=AsyncMock(side_effect=asyncio.CancelledError())),
        pytest.raises(asyncio.CancelledError),
    ):
        await background.append_background_audit_event(
            factory,
            org_id=_ORG_A,
            event_type="run_retention_purge",
            resource_type="run",
            actor_source="retention_cleanup",
            log_key=_LOG_KEY,
        )


# ---------------------------------------------------------------------------
# record_run_state_change_audits
# ---------------------------------------------------------------------------


async def test_empty_entries_never_open_a_session() -> None:
    factory = _factory()

    recorded = await background.record_run_state_change_audits(
        factory,
        [],
        event_type="run.sweep_terminalised",
        expected_statuses={"failed"},
        actor_source="dispatcher_reconcile",
        log_key=_LOG_KEY,
        summary_prefix="Run terminalised by background sweep:",
    )

    assert recorded == 0
    factory.assert_not_called()


async def test_blank_event_type_and_statuses_are_rejected() -> None:
    factory = _factory()

    with pytest.raises(ValueError, match="non-empty event_type"):
        await background.record_run_state_change_audits(
            factory,
            [(_RUN_ONE, _ORG_A)],
            event_type=" ",
            expected_statuses={"failed"},
            actor_source="dispatcher_reconcile",
            log_key=_LOG_KEY,
            summary_prefix="x",
        )

    with pytest.raises(ValueError, match="at least one expected status"):
        await background.record_run_state_change_audits(
            factory,
            [(_RUN_ONE, _ORG_A)],
            event_type="run.sweep_terminalised",
            expected_statuses=set(),
            actor_source="dispatcher_reconcile",
            log_key=_LOG_KEY,
            summary_prefix="x",
        )

    factory.assert_not_called()


async def test_run_without_the_expected_status_is_skipped() -> None:
    """Phantom-event guard: an id collected from a rolled-back transaction is
    not evidence the change landed, so the live status decides."""
    session = _session_returning([_run(_RUN_ONE, _ORG_A, status="running")])
    factory = _factory(session)

    with (
        patch.object(background, "set_rls_org", new=AsyncMock()),
        patch.object(background, "append_audit_event", new=AsyncMock()) as append,
    ):
        recorded = await background.record_run_state_change_audits(
            factory,
            [(_RUN_ONE, _ORG_A)],
            event_type="run.sweep_terminalised",
            expected_statuses={"failed", "complete", "cancelled"},
            actor_source="slot_reconciliation",
            log_key=_LOG_KEY,
            summary_prefix="Run terminalised by background sweep:",
        )

    assert recorded == 0
    append.assert_not_awaited()


async def test_matching_run_is_recorded_with_system_actor() -> None:
    session = _session_returning([_run(_RUN_ONE, _ORG_A)])
    factory = _factory(session)

    with (
        patch.object(background, "set_rls_org", new=AsyncMock()) as set_org,
        patch.object(background, "append_audit_event", new=AsyncMock()) as append,
    ):
        recorded = await background.record_run_state_change_audits(
            factory,
            [(_RUN_ONE, _ORG_A)],
            event_type="run.sweep_terminalised",
            expected_statuses={"failed"},
            actor_source="stale_run_recovery",
            log_key=_LOG_KEY,
            summary_prefix="Run terminalised by background sweep:",
        )

    assert recorded == 1
    set_org.assert_awaited_once_with(session, _ORG_A)
    kwargs = append.await_args.kwargs
    assert kwargs["event_type"] == "run.sweep_terminalised"
    assert kwargs["actor_user_id"] is None
    assert kwargs["resource_type"] == "run"
    assert kwargs["resource_id"] == _RUN_ONE
    payload = kwargs["payload_json"]
    assert payload["actor"] == SYSTEM_ACTOR
    assert payload["actor_source"] == "stale_run_recovery"
    assert payload["pipeline_run_id"] == str(_RUN_ONE)
    assert payload["run_status"] == "failed"
    assert payload["error_code"] == "harness.heartbeat_stale"


async def test_entries_are_grouped_one_session_per_organisation() -> None:
    session = _session_returning([_run(_RUN_ONE, _ORG_A, status="hitl_parked")])
    factory = _factory(session)

    with (
        patch.object(background, "set_rls_org", new=AsyncMock()) as set_org,
        patch.object(background, "append_audit_event", new=AsyncMock()),
    ):
        recorded = await background.record_run_state_change_audits(
            factory,
            [(_RUN_ONE, _ORG_A), (_RUN_TWO, _ORG_A)],
            event_type="hitl.run_parked",
            expected_statuses={"hitl_parked"},
            actor_source="hitl_park_sweep",
            log_key=_LOG_KEY,
            summary_prefix="Run parked by",
        )

    assert recorded == 1
    # ONE session/transaction per org regardless of how many runs it holds.
    assert factory.call_count == 1
    set_org.assert_awaited_once_with(session, _ORG_A)


async def test_one_organisation_failure_never_blocks_the_next() -> None:
    bad_session = MagicMock()
    bad_begin = MagicMock()
    bad_begin.__aenter__ = AsyncMock(side_effect=RuntimeError("org A down"))
    bad_begin.__aexit__ = AsyncMock(return_value=False)
    bad_session.begin.return_value = bad_begin

    good_session = _session_returning([_run(_RUN_TWO, _ORG_B)])
    sessions = iter([bad_session, good_session])

    def _factory_fn() -> MagicMock:
        session = next(sessions)
        cm = MagicMock()
        cm.__aenter__ = AsyncMock(return_value=session)
        cm.__aexit__ = AsyncMock(return_value=False)
        return cm

    factory = MagicMock(side_effect=_factory_fn)

    with (
        patch.object(background, "set_rls_org", new=AsyncMock()),
        patch.object(background, "append_audit_event", new=AsyncMock()) as append,
    ):
        recorded = await background.record_run_state_change_audits(
            factory,
            [(_RUN_ONE, _ORG_A), (_RUN_TWO, _ORG_B)],
            event_type="run.sweep_terminalised",
            expected_statuses={"failed"},
            actor_source="dispatcher_reconcile",
            log_key=_LOG_KEY,
            summary_prefix="Run terminalised by background sweep:",
        )

    # Org B still recorded despite org A's failure.
    assert recorded == 1
    assert append.await_count == 1
    assert append.await_args.kwargs["org_id"] == _ORG_B


async def test_one_event_failure_keeps_its_siblings(caplog: pytest.LogCaptureFixture) -> None:
    session = _session_returning(
        [_run(_RUN_ONE, _ORG_A, status="hitl_parked"), _run(_RUN_TWO, _ORG_A, status="hitl_parked")]
    )
    factory = _factory(session)

    with (
        patch.object(background, "set_rls_org", new=AsyncMock()),
        patch.object(
            background,
            "append_audit_event",
            new=AsyncMock(side_effect=[RuntimeError("chain locked"), None]),
        ),
        caplog.at_level("WARNING"),
    ):
        recorded = await background.record_run_state_change_audits(
            factory,
            [(_RUN_ONE, _ORG_A), (_RUN_TWO, _ORG_A)],
            event_type="hitl.run_parked",
            expected_statuses={"hitl_parked"},
            actor_source="hitl_park_sweep",
            log_key=_LOG_KEY,
            summary_prefix="Run parked by",
        )

    assert recorded == 1
    assert any(_LOG_KEY in record.message for record in caplog.records)


async def test_cancellation_from_an_organisation_pass_propagates() -> None:
    session = _session_returning([_run(_RUN_ONE, _ORG_A)])
    factory = _factory(session)

    with (
        patch.object(background, "set_rls_org", new=AsyncMock()),
        patch.object(background, "append_audit_event", new=AsyncMock(side_effect=asyncio.CancelledError())),
        pytest.raises(asyncio.CancelledError),
    ):
        await background.record_run_state_change_audits(
            factory,
            [(_RUN_ONE, _ORG_A)],
            event_type="run.sweep_terminalised",
            expected_statuses={"failed"},
            actor_source="dispatcher_reconcile",
            log_key=_LOG_KEY,
            summary_prefix="Run terminalised by background sweep:",
        )
