"""Wiring tests: background sweeps actually emit their audit events (FAR-1549).

Each test proves the PRODUCTION call site, not the helper in isolation — a
compensating audit record with zero callers is a silent critical, so these
tests patch the shared writer and assert the sweep reaches it with the right
event type, actor source and entries.
"""

from __future__ import annotations

import asyncio
import uuid
from contextlib import ExitStack
from types import SimpleNamespace
from typing import Any, Self
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from modulo.core import run_admission as ra
from modulo.core import saq_worker as sw
from modulo.core.audit_logger import background
from modulo.db.models.run import HITL_PARKED_STATUS, TERMINAL_STATUSES

_ORG = uuid.UUID("00000000-0000-0000-0000-0000000000aa")
_ORG_B = uuid.UUID("00000000-0000-0000-0000-0000000000bb")
_RUN = uuid.UUID("00000000-0000-0000-0000-000000000011")
_RUN_B = uuid.UUID("00000000-0000-0000-0000-000000000012")
_PIPELINE = uuid.UUID("00000000-0000-0000-0000-000000000099")


def _sessionmaker_like(session: MagicMock) -> tuple[MagicMock, MagicMock]:
    """A sessionmaker mock usable as ``async with factory() as s, s.begin():``
    — returns ``(factory, session)``."""
    begin_cm = MagicMock()
    begin_cm.__aenter__ = AsyncMock(return_value=session)
    begin_cm.__aexit__ = AsyncMock(return_value=False)
    session.begin.return_value = begin_cm
    session_cm = MagicMock()
    session_cm.__aenter__ = AsyncMock(return_value=session)
    session_cm.__aexit__ = AsyncMock(return_value=False)
    factory = MagicMock()
    factory.return_value = session_cm
    return factory, session


def _parked_row(run_id: uuid.UUID = _RUN, org_id: uuid.UUID = _ORG) -> SimpleNamespace:
    return SimpleNamespace(
        id=run_id,
        organisation_id=org_id,
        pipeline_id=_PIPELINE,
        status=HITL_PARKED_STATUS,
        error_code=None,
        error_detail=None,
    )


# ---------------------------------------------------------------------------
# run_terminal_advance.advance_terminalised_run — stale-run + slot sweeps
# ---------------------------------------------------------------------------


class TestAdvanceTerminalisedRunAudit:
    async def test_records_audit_tagged_with_the_calling_sweep(self) -> None:
        """Both raw terminalising sweeps funnelling through the ONE shared
        orchestrator means one audit call site covers four terminaliser
        branches — proven here by the ``source`` the caller threads through."""
        from modulo.core import run_terminal_advance as rta

        engine = MagicMock()
        with (
            patch.object(rta, "advance_journeys_from_stored_refs", new=AsyncMock()),
            patch.object(rta, "record_terminal_failed_fact", new=AsyncMock()),
            patch.object(background, "record_run_state_change_audits", new=AsyncMock()) as record,
        ):
            await rta.advance_terminalised_run(engine, _RUN, _ORG, source="stale_run_recovery")

        kwargs = record.await_args.kwargs
        assert record.await_args.args[1] == [(_RUN, _ORG)]
        assert kwargs["event_type"] == "run.sweep_terminalised"
        assert kwargs["expected_statuses"] == TERMINAL_STATUSES
        assert kwargs["actor_source"] == "stale_run_recovery"

    async def test_slot_reconciliation_passes_its_own_source(self) -> None:
        from modulo.core import run_terminal_advance as rta

        engine = MagicMock()
        with (
            patch.object(rta, "advance_journeys_from_stored_refs", new=AsyncMock()),
            patch.object(rta, "record_terminal_failed_fact", new=AsyncMock()),
            patch.object(background, "record_run_state_change_audits", new=AsyncMock()) as record,
        ):
            await rta.advance_terminalised_run(engine, _RUN, _ORG, source="slot_reconciliation")

        assert record.await_args.kwargs["actor_source"] == "slot_reconciliation"

    async def test_audit_failure_never_fails_the_sweep(self, caplog: pytest.LogCaptureFixture) -> None:
        from modulo.core import run_terminal_advance as rta

        engine = MagicMock()
        with (
            patch.object(rta, "advance_journeys_from_stored_refs", new=AsyncMock()),
            patch.object(rta, "record_terminal_failed_fact", new=AsyncMock()),
            patch.object(
                background,
                "record_run_state_change_audits",
                new=AsyncMock(side_effect=RuntimeError("audit db down")),
            ),
            caplog.at_level("WARNING"),
        ):
            # No exception escapes: the terminal write already committed.
            await rta.advance_terminalised_run(engine, _RUN, _ORG, source="stale_run_recovery")

        assert any("run_terminal_advance.audit_failed" in entry.message for entry in caplog.records)

    async def test_cancellation_is_never_swallowed(self) -> None:
        """CancelledError is the one exception the fail-open helper must NOT
        swallow — a cancelled sweep must actually stop."""
        from modulo.core import run_terminal_advance as rta

        engine = MagicMock()
        with (
            patch.object(rta, "advance_journeys_from_stored_refs", new=AsyncMock()),
            patch.object(rta, "record_terminal_failed_fact", new=AsyncMock()),
            patch.object(
                background,
                "record_run_state_change_audits",
                new=AsyncMock(side_effect=asyncio.CancelledError()),
            ),
            pytest.raises(asyncio.CancelledError),
        ):
            await rta.advance_terminalised_run(engine, _RUN, _ORG, source="stale_run_recovery")


# ---------------------------------------------------------------------------
# run_admission.park_expired_hitl_runs
# ---------------------------------------------------------------------------


class _Rows:
    def __init__(self, rows: list[Any]) -> None:
        self._rows = rows

    def all(self) -> list[Any]:
        return list(self._rows)


class _ParkConn:
    """Per-connection handle; all mutable state lives on the engine because the
    sweep opens a NEW connection for the org enumeration and one per org pass."""

    def __init__(self, engine: _ParkEngine) -> None:
        self._engine = engine

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, *args: object) -> bool:
        return False

    def begin(self) -> _ParkConn:
        return self

    async def execute(self, stmt: object, params: dict[str, object] | None = None) -> _Rows:
        sql = str(stmt)
        if "SELECT id FROM organisations" in sql:
            return _Rows([(org,) for org in self._engine.orgs])
        if "set_config" in sql:
            return _Rows([])
        if "UPDATE runs SET status" in sql:
            assert params is not None
            org_id = uuid.UUID(str(params["oid"]))
            if org_id == self._engine.fail_org:
                raise RuntimeError("db down")
            return _Rows(list(self._engine.parked_by_org.get(org_id, [])))
        return _Rows([])


class _ParkEngine:
    def __init__(
        self,
        orgs: list[uuid.UUID],
        parked_by_org: dict[uuid.UUID, list[SimpleNamespace]],
        fail_org: uuid.UUID | None = None,
    ) -> None:
        self.orgs = orgs
        self.parked_by_org = parked_by_org
        self.fail_org = fail_org

    def connect(self) -> _ParkConn:
        return _ParkConn(self)


class TestHitlParkSweepAudit:
    def _settings(self) -> MagicMock:
        return MagicMock(hitl_park_grace_seconds=86400)

    async def test_parked_runs_are_recorded_with_system_actor(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(ra, "get_settings", lambda: self._settings())
        engine = _ParkEngine([_ORG], {_ORG: [_parked_row()]})

        with patch.object(background, "record_run_state_change_audits", new=AsyncMock()) as record:
            result = await ra.park_expired_hitl_runs(engine)  # type: ignore[arg-type]

        assert result == {"parked": 1}
        kwargs = record.await_args.kwargs
        assert record.await_args.args[1] == [(_RUN, _ORG)]
        assert kwargs["event_type"] == "hitl.run_parked"
        assert kwargs["expected_statuses"] == {HITL_PARKED_STATUS}
        assert kwargs["actor_source"] == "hitl_park_sweep"
        assert kwargs["log_key"] == "run_admission.hitl_park_audit_failed"

    async def test_no_park_no_audit(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(ra, "get_settings", lambda: self._settings())

        with patch.object(background, "record_run_state_change_audits", new=AsyncMock()) as record:
            result = await ra.park_expired_hitl_runs(_ParkEngine([_ORG], {}))  # type: ignore[arg-type]

        assert result == {"parked": 0}
        record.assert_not_awaited()

    async def test_parks_in_earlier_orgs_are_audited_before_the_sweep_reraises(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A sweep that fails partway still changed run state — the landed
        parks must be recorded before ``HitlParkError`` propagates."""
        monkeypatch.setattr(ra, "get_settings", lambda: self._settings())
        engine = _ParkEngine(
            [_ORG, _ORG_B],
            {_ORG: [_parked_row()], _ORG_B: [_parked_row(_RUN_B, _ORG_B)]},
            fail_org=_ORG_B,
        )

        with (
            patch.object(background, "record_run_state_change_audits", new=AsyncMock()) as record,
            pytest.raises(ra.HitlParkError),
        ):
            await ra.park_expired_hitl_runs(engine)  # type: ignore[arg-type]

        record.assert_awaited_once()
        assert record.await_args.args[1] == [(_RUN, _ORG)]

    async def test_audit_failure_never_fails_the_sweep(
        self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        monkeypatch.setattr(ra, "get_settings", lambda: self._settings())
        engine = _ParkEngine([_ORG], {_ORG: [_parked_row()]})

        with (
            patch.object(
                background,
                "record_run_state_change_audits",
                new=AsyncMock(side_effect=RuntimeError("audit db down")),
            ),
            caplog.at_level("ERROR"),
        ):
            result = await ra.park_expired_hitl_runs(engine)  # type: ignore[arg-type]

        assert result == {"parked": 1}
        assert any("hitl_park.audit_failed" in entry.message for entry in caplog.records)

    async def test_cancellation_is_never_swallowed(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(ra, "get_settings", lambda: self._settings())
        engine = _ParkEngine([_ORG], {_ORG: [_parked_row()]})

        with (
            patch.object(
                background,
                "record_run_state_change_audits",
                new=AsyncMock(side_effect=asyncio.CancelledError()),
            ),
            pytest.raises(asyncio.CancelledError),
        ):
            await ra.park_expired_hitl_runs(engine)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# cron_helpers._record_terminalisation_audits — dispatcher_reconcile
# ---------------------------------------------------------------------------


class TestDispatcherTerminalisationAudit:
    async def test_passes_dispatcher_as_the_actor_source(self) -> None:
        from modulo.core import cron_helpers as ch

        with patch.object(background, "record_run_state_change_audits", new=AsyncMock()) as record:
            await ch._record_terminalisation_audits([(_RUN, _ORG)])

        kwargs = record.await_args.kwargs
        assert kwargs["event_type"] == "run.sweep_terminalised"
        assert kwargs["expected_statuses"] == TERMINAL_STATUSES
        assert kwargs["actor_source"] == "dispatcher_reconcile"
        assert kwargs["log_key"] == "cron_helpers.terminalized_audit_failed"

    async def test_empty_tick_never_opens_a_session(self) -> None:
        from modulo.core import cron_helpers as ch

        with patch.object(background, "record_run_state_change_audits", new=AsyncMock()) as record:
            await ch._record_terminalisation_audits([])

        record.assert_not_awaited()

    async def test_failure_is_logged_not_raised(self, caplog: pytest.LogCaptureFixture) -> None:
        from modulo.core import cron_helpers as ch

        with (
            patch.object(
                background,
                "record_run_state_change_audits",
                new=AsyncMock(side_effect=RuntimeError("audit db down")),
            ),
            caplog.at_level("WARNING"),
        ):
            await ch._record_terminalisation_audits([(_RUN, _ORG)])

        assert any("cron_helpers.terminalized_audits_failed" in entry.message for entry in caplog.records)

    async def test_cancellation_is_never_swallowed(self) -> None:
        from modulo.core import cron_helpers as ch

        with (
            patch.object(
                background,
                "record_run_state_change_audits",
                new=AsyncMock(side_effect=asyncio.CancelledError()),
            ),
            pytest.raises(asyncio.CancelledError),
        ):
            await ch._record_terminalisation_audits([(_RUN, _ORG)])


# ---------------------------------------------------------------------------
# runner_capacity.reconcile_runner_dispatch_markers
# ---------------------------------------------------------------------------


class TestRunnerMarkerSweepAudit:
    def test_emitter_reports_only_status_changing_outcomes(self) -> None:
        """d1/d3 merely clear a marker; only the d2 transition changes a
        status, and only that is audit-worthy."""
        from modulo.core import runner_capacity as rc

        org = _ORG
        cleared, transitioned, entries = rc._emit_sweep_outcomes(
            org,
            [
                (_RUN, "failed", "clear_terminal"),
                (_RUN_B, "failed", "transition_stale_running"),
            ],
        )

        assert cleared == 2
        assert transitioned == 1
        assert entries == [(_RUN_B, org)]

    async def test_transitioned_entries_are_recorded_with_the_sweep_source(self) -> None:
        from modulo.core import runner_capacity as rc

        factory = MagicMock()
        with patch.object(background, "record_run_state_change_audits", new=AsyncMock()) as record:
            await rc._record_marker_sweep_terminalisations(factory, [(_RUN, _ORG)])

        assert record.await_args.args[0] is factory
        assert record.await_args.args[1] == [(_RUN, _ORG)]
        kwargs = record.await_args.kwargs
        assert kwargs["event_type"] == "run.sweep_terminalised"
        assert kwargs["expected_statuses"] == TERMINAL_STATUSES
        assert kwargs["actor_source"] == "runner_marker_sweep"

    async def test_no_transitions_never_open_a_session(self) -> None:
        from modulo.core import runner_capacity as rc

        with patch.object(background, "record_run_state_change_audits", new=AsyncMock()) as record:
            await rc._record_marker_sweep_terminalisations(MagicMock(), [])

        record.assert_not_awaited()

    async def test_audit_failure_never_fails_the_sweep(self, caplog: pytest.LogCaptureFixture) -> None:
        from modulo.core import runner_capacity as rc

        with (
            patch.object(
                background,
                "record_run_state_change_audits",
                new=AsyncMock(side_effect=RuntimeError("audit db down")),
            ),
            caplog.at_level("ERROR"),
        ):
            await rc._record_marker_sweep_terminalisations(MagicMock(), [(_RUN, _ORG)])

        assert any("runner.capacity.marker_sweep_audit_failed" in entry.message for entry in caplog.records)

    async def test_cancellation_is_never_swallowed(self) -> None:
        from modulo.core import runner_capacity as rc

        with (
            patch.object(
                background,
                "record_run_state_change_audits",
                new=AsyncMock(side_effect=asyncio.CancelledError()),
            ),
            pytest.raises(asyncio.CancelledError),
        ):
            await rc._record_marker_sweep_terminalisations(MagicMock(), [(_RUN, _ORG)])


# ---------------------------------------------------------------------------
# saq_worker.retention_cleanup
# ---------------------------------------------------------------------------


def _retention_patches(
    *,
    deleted: int,
    targets: dict[uuid.UUID, int],
    system_factory: MagicMock,
    app_factory: MagicMock | None = None,
) -> list[Any]:
    return [
        patch.object(sw, "_make_system_session_factory", return_value=system_factory),
        patch.object(sw, "_make_session_factory", return_value=app_factory or system_factory),
        patch.object(sw, "_retention_purge_targets", new=AsyncMock(return_value=targets)),
        patch("modulo.db.crud.run.batch_delete_old_terminal_runs", new_callable=AsyncMock, return_value=deleted),
        patch(
            "modulo.db.crud.org_deletion.batch_delete_langgraph_checkpoints",
            new_callable=AsyncMock,
            return_value=0,
        ),
        patch(
            "modulo.core.artifacts.gc.delete_orphaned_run_artifacts",
            new_callable=AsyncMock,
            return_value={"orphan_runs": [], "files_deleted": 0},
        ),
    ]


class TestRetentionPurgeAudit:
    async def test_purge_records_one_event_per_affected_org(self) -> None:
        system_factory, _session = _sessionmaker_like(MagicMock())
        app_factory, _app_session = _sessionmaker_like(MagicMock())

        with ExitStack() as stack:
            for entry in _retention_patches(
                deleted=7, targets={_ORG: 7}, system_factory=system_factory, app_factory=app_factory
            ):
                stack.enter_context(entry)
            append = stack.enter_context(
                patch.object(background, "append_background_audit_event", new=AsyncMock(return_value=True))
            )
            result = await sw.retention_cleanup({})

        assert result["deleted"] == 7
        kwargs = append.await_args.kwargs
        assert kwargs["org_id"] == _ORG
        assert kwargs["event_type"] == "run_retention_purge"
        assert kwargs["resource_type"] == "run"
        assert kwargs["actor_source"] == "retention_cleanup"
        assert kwargs["payload_json"]["purged_runs"] == 7
        assert kwargs["payload_json"]["max_age_days"] == sw._RETENTION_MAX_AGE_DAYS

    async def test_zero_deletions_record_nothing(self) -> None:
        system_factory, _session = _sessionmaker_like(MagicMock())

        with ExitStack() as stack:
            for entry in _retention_patches(deleted=0, targets={}, system_factory=system_factory):
                stack.enter_context(entry)
            append = stack.enter_context(
                patch.object(background, "append_background_audit_event", new=AsyncMock(return_value=True))
            )
            await sw.retention_cleanup({})

        append.assert_not_awaited()

    async def test_missing_targets_are_loud_not_silent(self, caplog: pytest.LogCaptureFixture) -> None:
        """The count is the pre-image of the delete: if it went missing while
        rows were still deleted, that is a lost record and must be logged."""
        system_factory, _session = _sessionmaker_like(MagicMock())

        with ExitStack() as stack:
            for entry in _retention_patches(deleted=7, targets={}, system_factory=system_factory):
                stack.enter_context(entry)
            stack.enter_context(caplog.at_level("WARNING"))
            await sw.retention_cleanup({})

        assert any(getattr(entry, "stage", None) == "targets_missing" for entry in caplog.records)

    async def test_target_count_failure_never_skips_the_purge(self, caplog: pytest.LogCaptureFixture) -> None:
        system_factory, _session = _sessionmaker_like(MagicMock())

        with ExitStack() as stack:
            stack.enter_context(patch.object(sw, "_make_system_session_factory", return_value=system_factory))
            stack.enter_context(patch.object(sw, "_make_session_factory", return_value=system_factory))
            stack.enter_context(
                patch.object(sw, "_retention_purge_targets", new=AsyncMock(side_effect=RuntimeError("count failed")))
            )
            stack.enter_context(
                patch("modulo.db.crud.run.batch_delete_old_terminal_runs", new_callable=AsyncMock, return_value=7)
            )
            stack.enter_context(
                patch(
                    "modulo.db.crud.org_deletion.batch_delete_langgraph_checkpoints",
                    new_callable=AsyncMock,
                    return_value=0,
                )
            )
            stack.enter_context(
                patch(
                    "modulo.core.artifacts.gc.delete_orphaned_run_artifacts",
                    new_callable=AsyncMock,
                    return_value={"orphan_runs": [], "files_deleted": 0},
                )
            )
            stack.enter_context(caplog.at_level("WARNING"))
            result = await sw.retention_cleanup({})

        # The purge still ran — retention is not held hostage by its own record.
        assert result["deleted"] == 7
        assert any(sw._RETENTION_AUDIT_LOG_KEY in entry.message for entry in caplog.records)

    async def test_target_count_cancellation_is_never_swallowed(self) -> None:
        """The fail-open count guard must re-raise CancelledError — a cancelled
        retention tick must not be turned into a completed purge."""
        system_factory, _session = _sessionmaker_like(MagicMock())

        with (
            patch.object(sw, "_make_system_session_factory", return_value=system_factory),
            patch.object(sw, "_retention_purge_targets", new=AsyncMock(side_effect=asyncio.CancelledError())),
            pytest.raises(asyncio.CancelledError),
        ):
            await sw.retention_cleanup({})


class TestRetentionPurgeTargetCount:
    """The pre-image count itself: ``_retention_purge_targets`` reads the rows
    the purge is about to delete, so its predicate and its zero-filter are the
    contract the audit event's count rests on."""

    async def test_returns_per_org_counts_and_drops_empty_orgs(self) -> None:
        org_with_zero = uuid.UUID("00000000-0000-0000-0000-0000000000cc")
        result = MagicMock()
        result.all.return_value = [(_ORG, 5), (org_with_zero, 0)]
        factory, session = _sessionmaker_like(MagicMock())
        session.execute = AsyncMock(return_value=result)

        targets = await sw._retention_purge_targets(factory)

        assert targets == {_ORG: 5}
        session.execute.assert_awaited_once()


class TestRetentionPurgeRecordHelper:
    """Direct helper tests for the branches ``retention_cleanup`` cannot reach:
    it only calls the recorder when rows were actually deleted."""

    async def test_empty_targets_with_zero_deletions_returns_silently(self, caplog: pytest.LogCaptureFixture) -> None:
        with caplog.at_level("WARNING"):
            await sw._record_retention_purge_audits({}, 0)

        assert not any(sw._RETENTION_AUDIT_LOG_KEY in entry.message for entry in caplog.records)

    async def test_unrecorded_append_is_not_counted(self) -> None:
        """An append that reports False (logged-and-swallowed write failure)
        must not inflate the recorded tally."""
        with (
            patch.object(
                background,
                "append_background_audit_event",
                new=AsyncMock(return_value=False),
            ) as append,
            patch.object(sw, "_make_session_factory", return_value=MagicMock()),
        ):
            await sw._record_retention_purge_audits({_ORG: 3}, 3)

        append.assert_awaited_once()
        assert append.await_args.kwargs["payload_json"]["purged_runs"] == 3


# ---------------------------------------------------------------------------
# audit_logger.background.record_suite_run_audit — SuiteRun lifecycle (FAR-1561)
# ---------------------------------------------------------------------------


def _suite_run(*, state: str = "pending", run_id: uuid.UUID = _RUN, org_id: uuid.UUID = _ORG) -> SimpleNamespace:
    return SimpleNamespace(
        id=run_id,
        organisation_id=org_id,
        suite_id=uuid.uuid4(),
        dataset_id=uuid.uuid4(),
        state=state,
        total_cases=3,
        passed_cases=2,
        failed_cases=1,
        excluded_case_count=0,
        error_detail=None,
    )


def _suite_factory(run: SimpleNamespace | None) -> tuple[MagicMock, MagicMock]:
    """Sessionmaker whose re-select returns *run* (``None`` = row missing)."""
    session = MagicMock()
    result = MagicMock()
    result.scalar_one_or_none = MagicMock(return_value=run)
    session.execute = AsyncMock(return_value=result)
    factory, session = _sessionmaker_like(session)
    return factory, session


class TestSuiteRunAudit:
    async def test_appends_when_the_reselected_state_matches(self) -> None:
        factory, _ = _suite_factory(_suite_run(state="pending"))
        with patch.object(background, "append_audit_event", new=AsyncMock()) as append:
            recorded = await background.record_suite_run_audit(
                factory,
                suite_run_id=_RUN,
                org_id=_ORG,
                event_type="suite_run_created",
                expected_states=background.SUITE_RUN_PENDING_STATES,
                actor_source="fire_suite_run_trigger",
                log_key="test.suite_run_audit_failed",
                summary_prefix="SuiteRun created by",
            )

        assert recorded is True
        kwargs = append.await_args.kwargs
        assert kwargs["org_id"] == _ORG
        assert kwargs["event_type"] == "suite_run_created"
        assert kwargs["actor_user_id"] is None
        assert kwargs["resource_type"] == "suite_run"
        assert kwargs["resource_id"] == _RUN
        payload = kwargs["payload_json"]
        assert payload["actor"] == "system"
        assert payload["actor_source"] == "fire_suite_run_trigger"
        assert payload["summary"] == "SuiteRun created by fire_suite_run_trigger"
        assert payload["state"] == "pending"
        assert payload["passed_cases"] == 2

    async def test_extra_payload_json_is_merged_over_row_fields(self) -> None:
        """A caller-supplied ``payload_json`` (the fire path's ``trigger_id`` /
        ``pipeline_id``) is merged over the row-derived fields — the event
        carries both, and caller keys win on collision."""
        factory, _ = _suite_factory(_suite_run(state="pending"))
        with patch.object(background, "append_audit_event", new=AsyncMock()) as append:
            recorded = await background.record_suite_run_audit(
                factory,
                suite_run_id=_RUN,
                org_id=_ORG,
                event_type="suite_run_created",
                expected_states=background.SUITE_RUN_PENDING_STATES,
                actor_source="fire_suite_run_trigger",
                log_key="test.suite_run_audit_failed",
                summary_prefix="SuiteRun created by",
                payload_json={"trigger_id": "trigger-1", "state": "caller-wins"},
            )

        assert recorded is True
        payload = append.await_args.kwargs["payload_json"]
        assert payload["trigger_id"] == "trigger-1"
        assert payload["state"] == "caller-wins"
        assert payload["actor_source"] == "fire_suite_run_trigger"

    async def test_missing_row_is_skipped_not_invented(self, caplog: pytest.LogCaptureFixture) -> None:
        """A row that is gone (or cross-org) proves the change never landed —
        the phantom-event guard must skip, never fabricate the event."""
        factory, _ = _suite_factory(None)
        with (
            patch.object(background, "append_audit_event", new=AsyncMock()) as append,
            caplog.at_level("WARNING"),
        ):
            recorded = await background.record_suite_run_audit(
                factory,
                suite_run_id=_RUN,
                org_id=_ORG,
                event_type="suite_run_completed",
                expected_states=background.SUITE_RUN_TERMINAL_STATES,
                actor_source="execute_suite_run",
                log_key="test.suite_run_audit_failed",
                summary_prefix="SuiteRun terminalised by",
            )

        assert recorded is False
        append.assert_not_awaited()
        reasons = [getattr(entry, "reason", None) for entry in caplog.records]
        assert "missing_or_cross_org" in reasons

    async def test_unexpected_state_is_skipped(self, caplog: pytest.LogCaptureFixture) -> None:
        """``suite_run_started`` must not record a run still ``pending`` — the
        start did not land (its transaction rolled back)."""
        factory, _ = _suite_factory(_suite_run(state="pending"))
        with (
            patch.object(background, "append_audit_event", new=AsyncMock()) as append,
            caplog.at_level("WARNING"),
        ):
            recorded = await background.record_suite_run_audit(
                factory,
                suite_run_id=_RUN,
                org_id=_ORG,
                event_type="suite_run_started",
                expected_states=background.SUITE_RUN_NON_PENDING_STATES,
                actor_source="execute_suite_run",
                log_key="test.suite_run_audit_failed",
                summary_prefix="SuiteRun started by",
            )

        assert recorded is False
        append.assert_not_awaited()
        reasons = [getattr(entry, "reason", None) for entry in caplog.records]
        assert "unexpected_state" in reasons

    async def test_append_failure_is_logged_not_raised(self, caplog: pytest.LogCaptureFixture) -> None:
        factory, _ = _suite_factory(_suite_run(state="failed"))
        with (
            patch.object(background, "append_audit_event", new=AsyncMock(side_effect=RuntimeError("audit db down"))),
            caplog.at_level("WARNING"),
        ):
            recorded = await background.record_suite_run_audit(
                factory,
                suite_run_id=_RUN,
                org_id=_ORG,
                event_type="suite_run_completed",
                expected_states=background.SUITE_RUN_TERMINAL_STATES,
                actor_source="execute_suite_run",
                log_key="test.suite_run_audit_failed",
                summary_prefix="SuiteRun terminalised by",
            )

        assert recorded is False
        assert any("test.suite_run_audit_failed" in entry.message for entry in caplog.records)

    async def test_cancellation_is_never_swallowed(self) -> None:
        factory, _ = _suite_factory(_suite_run())
        with (
            patch.object(background, "append_audit_event", new=AsyncMock(side_effect=asyncio.CancelledError())),
            pytest.raises(asyncio.CancelledError),
        ):
            await background.record_suite_run_audit(
                factory,
                suite_run_id=_RUN,
                org_id=_ORG,
                event_type="suite_run_created",
                expected_states=background.SUITE_RUN_PENDING_STATES,
                actor_source="fire_suite_run_trigger",
                log_key="test.suite_run_audit_failed",
                summary_prefix="SuiteRun created by",
            )

    @pytest.mark.parametrize(
        ("event_type", "actor_source", "expected_states"),
        [
            ("", "execute_suite_run", {"pending"}),
            ("suite_run_created", "", {"pending"}),
            ("suite_run_created", "x", set()),
        ],
    )
    async def test_missing_arguments_fail_closed(
        self, event_type: str, actor_source: str, expected_states: set[str]
    ) -> None:
        factory, _ = _suite_factory(_suite_run())
        with pytest.raises(ValueError, match="record_suite_run_audit requires"):
            await background.record_suite_run_audit(
                factory,
                suite_run_id=_RUN,
                org_id=_ORG,
                event_type=event_type,
                expected_states=expected_states,
                actor_source=actor_source,
                log_key="test.suite_run_audit_failed",
                summary_prefix="SuiteRun created by",
            )


class TestSuiteRunAuditCallSites:
    """The SAQ job wrappers actually reach the shared helper with the right
    event vocabulary (FAR-1561)."""

    async def test_fire_path_records_created(self) -> None:
        with (
            patch.object(sw, "_make_session_factory", return_value=MagicMock()),
            patch.object(background, "record_suite_run_audit", new=AsyncMock(return_value=True)) as record,
        ):
            await sw._record_suite_run_created_audit(
                str(_RUN),
                org_id=str(_ORG),
                trigger_id=str(_PIPELINE),
                pipeline_id=str(_PIPELINE),
            )

        record.assert_awaited_once()
        kwargs = record.await_args.kwargs
        assert kwargs["event_type"] == "suite_run_created"
        assert kwargs["suite_run_id"] == _RUN
        assert kwargs["org_id"] == _ORG
        assert kwargs["expected_states"] == background.SUITE_RUN_PENDING_STATES
        assert kwargs["actor_source"] == "fire_suite_run_trigger"
        assert kwargs["payload_json"]["trigger_id"] == str(_PIPELINE)

    async def test_execute_path_records_start_then_terminal(self) -> None:
        with patch.object(background, "record_suite_run_audit", new=AsyncMock(return_value=True)) as record:
            await sw._record_suite_run_execution_audits(MagicMock(), _RUN, _ORG)

        assert record.await_count == 2
        first, second = (call.kwargs for call in record.await_args_list)
        assert first["event_type"] == "suite_run_started"
        assert first["expected_states"] == background.SUITE_RUN_NON_PENDING_STATES
        assert second["event_type"] == "suite_run_completed"
        assert second["expected_states"] == background.SUITE_RUN_TERMINAL_STATES
        for kwargs in (first, second):
            assert kwargs["suite_run_id"] == _RUN
            assert kwargs["org_id"] == _ORG
            assert kwargs["actor_source"] == "execute_suite_run"

    def test_state_constant_partitions_the_lifecycle(self) -> None:
        """The three guards are disjoint and together cover every SuiteRun
        state — a new state cannot fall through all three un-audited."""
        from modulo.db.models.eval_suite_run import SuiteRunState

        all_states = {state.value for state in SuiteRunState}
        assert all_states == (background.SUITE_RUN_PENDING_STATES | background.SUITE_RUN_NON_PENDING_STATES)
        assert not background.SUITE_RUN_PENDING_STATES & background.SUITE_RUN_NON_PENDING_STATES
        assert background.SUITE_RUN_TERMINAL_STATES <= background.SUITE_RUN_NON_PENDING_STATES
        assert "pending" not in background.SUITE_RUN_TERMINAL_STATES
        assert "running" not in background.SUITE_RUN_TERMINAL_STATES
