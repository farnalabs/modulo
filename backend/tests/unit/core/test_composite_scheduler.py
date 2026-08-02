"""Unit tests for the composite beat scheduler (modulo.core.composite_scheduler).

Applies QA lenses (correctness, bugs, maintainability) to the scheduler that
fans out to the cron, polling, and report sub-schedulers.

The cron/polling/report sub-schedulers each carry their own unit tests
(``tests/unit/cron_scheduler/``, ``tests/unit/trigger_engine/``, and the report
scheduler BDD coverage); this module covers the composition logic itself — the
``CompositeScheduler`` constructor order, schedule merging, tick delegation,
``max_interval`` derivation, and the ``StaleRecoveryEntry`` beat entry used for
the 5-minute stale-run recovery sweep.
"""

from datetime import UTC, datetime, timedelta
from unittest.mock import MagicMock, patch

from celery.beat import Scheduler

from modulo.core.composite_scheduler import CompositeScheduler, StaleRecoveryEntry


class TestStaleRecoveryEntry:
    def test_static_properties(self) -> None:
        entry = StaleRecoveryEntry()
        assert entry.name == "stale_run_recovery"
        assert entry.task == "modulo.pipeline.stale_run_recovery"
        assert entry.schedule is entry
        assert entry.args == []
        assert entry.kwargs == {}
        assert entry.options == {}

    def test_repr(self) -> None:
        entry = StaleRecoveryEntry()
        assert repr(entry) == "<StaleRecoveryEntry: every 5 min>"

    def test_first_call_is_due_and_stamps_last_run(self) -> None:
        entry = StaleRecoveryEntry()
        due, delay = entry.is_due()
        assert due is True
        assert delay == timedelta(seconds=0)
        assert entry._last_run is not None

    def test_within_interval_reports_remaining_seconds(self) -> None:
        entry = StaleRecoveryEntry()
        entry._last_run = datetime.now(UTC) - timedelta(seconds=60)
        due, delay = entry.is_due()
        assert due is False
        assert 239 < delay.total_seconds() <= 240

    def test_at_exactly_interval_is_due(self) -> None:
        entry = StaleRecoveryEntry()
        entry._last_run = datetime.now(UTC) - timedelta(seconds=300)
        due, delay = entry.is_due()
        assert due is True
        assert delay == timedelta(seconds=0)

    def test_past_interval_is_due_and_refreshes_stamp(self) -> None:
        entry = StaleRecoveryEntry()
        entry._last_run = datetime.now(UTC) - timedelta(seconds=600)
        due, delay = entry.is_due()
        assert due is True
        assert delay == timedelta(seconds=0)
        refreshed = entry._last_run
        assert refreshed is not None
        entry._last_run = datetime.now(UTC) - timedelta(seconds=600)
        assert entry.is_due()[0] is True
        assert entry._last_run != refreshed


class TestCompositeSchedulerInit:
    def test_constructs_all_three_sub_schedulers_with_app(self) -> None:
        app = MagicMock()
        with (
            patch("modulo.core.composite_scheduler.DatabaseCronScheduler") as cron_cls,
            patch("modulo.core.composite_scheduler.DatabasePollingScheduler") as poll_cls,
            patch("modulo.core.composite_scheduler.DatabaseReportScheduler") as report_cls,
        ):
            cron_cls.return_value.max_interval = 30
            poll_cls.return_value.max_interval = 60
            report_cls.return_value.max_interval = 45
            cron_cls.return_value._schedule = {}
            poll_cls.return_value._schedule = {}
            report_cls.return_value._schedule = {}

            scheduler = CompositeScheduler(app)

        cron_cls.assert_called_once_with(app)
        poll_cls.assert_called_once_with(app)
        report_cls.assert_called_once_with(app)
        assert scheduler._stale_entry is None

    def test_init_populates_merged_schedule_via_setup(self) -> None:
        app = MagicMock()
        with (
            patch("modulo.core.composite_scheduler.DatabaseCronScheduler") as cron_cls,
            patch("modulo.core.composite_scheduler.DatabasePollingScheduler") as poll_cls,
            patch("modulo.core.composite_scheduler.DatabaseReportScheduler") as report_cls,
        ):
            cron_cls.return_value.max_interval = 30
            poll_cls.return_value.max_interval = 60
            report_cls.return_value.max_interval = 45
            cron_cls.return_value._schedule = {"cron-1": "cron"}
            poll_cls.return_value._schedule = {"poll-1": "poll"}
            report_cls.return_value._schedule = {"report-1": "report"}

            scheduler = CompositeScheduler(app)

        assert scheduler._schedule["cron-1"] == "cron"
        assert scheduler._schedule["poll-1"] == "poll"
        assert scheduler._schedule["report-1"] == "report"
        assert isinstance(scheduler._schedule["stale-run-recovery"], StaleRecoveryEntry)


class TestSetupSchedule:
    def _make_scheduler(self) -> CompositeScheduler:
        scheduler = object.__new__(CompositeScheduler)
        scheduler._cron_scheduler = MagicMock()
        scheduler._polling_scheduler = MagicMock()
        scheduler._report_scheduler = MagicMock()
        scheduler._merge_schedules = MagicMock()
        return scheduler

    def test_syncs_every_sub_scheduler_then_merges(self) -> None:
        scheduler = self._make_scheduler()
        scheduler.setup_schedule()
        scheduler._cron_scheduler.setup_schedule.assert_called_once_with()
        scheduler._polling_scheduler.setup_schedule.assert_called_once_with()
        scheduler._report_scheduler.setup_schedule.assert_called_once_with()
        scheduler._merge_schedules.assert_called_once_with()


class TestTick:
    def test_syncs_all_sub_schedulers_merges_and_defers_to_parent(self) -> None:
        scheduler = object.__new__(CompositeScheduler)
        scheduler._cron_scheduler = MagicMock()
        scheduler._polling_scheduler = MagicMock()
        scheduler._report_scheduler = MagicMock()
        scheduler._schedule = {}
        scheduler.app = MagicMock()
        scheduler.data = {}

        order: list[str] = []

        def _parent_tick() -> float:
            order.append("parent")
            return 42.0

        with (
            patch.object(scheduler, "_merge_schedules", side_effect=lambda: order.append("merge")),
            patch.object(Scheduler, "tick", side_effect=_parent_tick),
        ):
            result = scheduler.tick()

        assert result == 42.0
        scheduler._cron_scheduler._sync_with_db.assert_called_once_with()
        scheduler._polling_scheduler._sync_with_db.assert_called_once_with()
        scheduler._report_scheduler._sync_with_db.assert_called_once_with()
        assert order == ["merge", "parent"]


class TestMergeSchedules:
    def _make_scheduler(self) -> CompositeScheduler:
        scheduler = object.__new__(CompositeScheduler)
        scheduler._cron_scheduler = MagicMock()
        scheduler._polling_scheduler = MagicMock()
        scheduler._report_scheduler = MagicMock()
        return scheduler

    def test_merges_entries_from_every_source(self) -> None:
        scheduler = self._make_scheduler()
        scheduler._cron_scheduler._schedule = {"cron-1": "cron", "shared": "cron"}
        scheduler._polling_scheduler._schedule = {"poll-1": "poll", "shared": "poll"}
        scheduler._report_scheduler._schedule = {"report-1": "report", "shared": "report"}

        scheduler._merge_schedules()

        assert scheduler._schedule["cron-1"] == "cron"
        assert scheduler._schedule["poll-1"] == "poll"
        assert scheduler._schedule["report-1"] == "report"

    def test_colliding_keys_resolve_cron_polling_report_order(self) -> None:
        scheduler = self._make_scheduler()
        scheduler._cron_scheduler._schedule = {"shared": "cron"}
        scheduler._polling_scheduler._schedule = {"shared": "poll"}
        scheduler._report_scheduler._schedule = {"shared": "report"}

        scheduler._merge_schedules()

        assert scheduler._schedule["shared"] == "report"

    def test_always_adds_fresh_stale_run_recovery_entry(self) -> None:
        scheduler = self._make_scheduler()
        scheduler._cron_scheduler._schedule = {}
        scheduler._polling_scheduler._schedule = {}
        scheduler._report_scheduler._schedule = {}

        scheduler._merge_schedules()
        first = scheduler._schedule["stale-run-recovery"]
        scheduler._merge_schedules()
        second = scheduler._schedule["stale-run-recovery"]

        assert isinstance(first, StaleRecoveryEntry)
        assert isinstance(second, StaleRecoveryEntry)
        assert first is not second


class TestMaxInterval:
    def _make_scheduler(self) -> CompositeScheduler:
        scheduler = object.__new__(CompositeScheduler)
        scheduler._cron_scheduler = MagicMock()
        scheduler._polling_scheduler = MagicMock()
        scheduler._report_scheduler = MagicMock()
        return scheduler

    def test_returns_smallest_sub_scheduler_interval(self) -> None:
        scheduler = self._make_scheduler()
        scheduler._cron_scheduler.max_interval = 30
        scheduler._polling_scheduler.max_interval = 15
        scheduler._report_scheduler.max_interval = 60

        assert scheduler.max_interval == 15

    def test_setter_is_ignored(self) -> None:
        scheduler = self._make_scheduler()
        scheduler._cron_scheduler.max_interval = 30
        scheduler._polling_scheduler.max_interval = 30
        scheduler._report_scheduler.max_interval = 30

        scheduler.max_interval = 5

        assert scheduler.max_interval == 30
