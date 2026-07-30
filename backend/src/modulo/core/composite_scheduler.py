"""Composite beat scheduler that handles cron, polling, and report schedules.

Delegates to ``DatabaseCronScheduler``, ``DatabasePollingScheduler``, and
``DatabaseReportScheduler`` as sub-schedulers. On each tick it syncs all
three with the database, merging their entries into a single in-memory
schedule for Celery beat.

Usage in Celery config::

    beat_scheduler = "modulo.core.composite_scheduler:CompositeScheduler"
"""

import logging
from datetime import UTC, datetime, timedelta
from typing import Any

from celery import Celery
from celery.beat import ScheduleEntry, Scheduler

from modulo.core.cron_scheduler import DatabaseCronScheduler
from modulo.core.reports.scheduler import DatabaseReportScheduler
from modulo.core.trigger_engine.polling import DatabasePollingScheduler

_log = logging.getLogger(__name__)

_STALE_RECOVERY_INTERVAL = 300  # 5 minutes


class StaleRecoveryEntry(ScheduleEntry):  # type: ignore[misc]
    """Celery beat entry for the stale-running recovery sweep (every 5 min)."""

    def __init__(self) -> None:
        self._last_run: datetime | None = None

    @property
    def name(self) -> str:
        return "stale_run_recovery"

    @property
    def task(self) -> str:
        return "modulo.pipeline.stale_run_recovery"

    @property
    def schedule(self) -> Any:
        return self

    @property
    def args(self) -> list[Any]:
        return []

    @property
    def kwargs(self) -> dict[str, Any]:
        return {}

    @property
    def options(self) -> dict[str, Any]:
        return {}

    def is_due(self) -> tuple[bool, timedelta]:
        now = datetime.now(UTC)
        if self._last_run is None:
            self._last_run = now
            return (True, timedelta(seconds=0))
        delta = now - self._last_run
        if delta.total_seconds() >= 300:
            self._last_run = now
            return (True, timedelta(seconds=0))
        remaining = 300 - delta.total_seconds()
        return (False, timedelta(seconds=max(remaining, 0)))

    def __repr__(self) -> str:
        return "<StaleRecoveryEntry: every 5 min>"


class CompositeScheduler(Scheduler):  # type: ignore[misc]
    """Celery beat scheduler that handles both cron and polling triggers.

    Maintains a ``DatabaseCronScheduler`` and a ``DatabasePollingScheduler``
    as sub-schedulers. On each tick it syncs both with the database and
    merges their entries into a unified schedule.
    """

    def __init__(self, app: Celery, **kwargs: Any) -> None:
        self._schedule: dict[str, Any] = {}
        # Init sub-schedulers BEFORE super().__init__(), because the base
        # class constructor calls `self.max_interval`, which reads
        # `self._cron_scheduler.max_interval`.
        self._cron_scheduler = DatabaseCronScheduler(app, **kwargs)
        self._polling_scheduler = DatabasePollingScheduler(app, **kwargs)
        self._report_scheduler = DatabaseReportScheduler(app, **kwargs)
        super().__init__(app, **kwargs)
        self._stale_entry: StaleRecoveryEntry | None = None

    def setup_schedule(self) -> None:
        """Populate the schedule from all trigger and report sources."""
        self._cron_scheduler.setup_schedule()
        self._polling_scheduler.setup_schedule()
        self._report_scheduler.setup_schedule()
        self._merge_schedules()

    def tick(self) -> float:
        """Sync all schedulers with DB and return seconds until next tick."""
        self._cron_scheduler._sync_with_db()
        self._polling_scheduler._sync_with_db()
        self._report_scheduler._sync_with_db()
        self._merge_schedules()
        return float(super().tick())

    def _merge_schedules(self) -> None:
        """Merge entries from all sub-schedulers into ``self._schedule``."""
        merged: dict[str, Any] = {}
        merged.update(self._cron_scheduler._schedule)
        merged.update(self._polling_scheduler._schedule)
        merged.update(self._report_scheduler._schedule)
        merged["stale-run-recovery"] = StaleRecoveryEntry()
        self._schedule = merged

    @property
    def max_interval(self) -> int:
        """Return the smallest of all sub-schedulers' max intervals."""
        return min(
            self._cron_scheduler.max_interval,
            self._polling_scheduler.max_interval,
            self._report_scheduler.max_interval,
        )
