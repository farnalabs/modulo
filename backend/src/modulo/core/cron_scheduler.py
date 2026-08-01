"""Celery beat scheduler that reads cron trigger rows from the database.

PR B-2: the fire logic (``fire_cron_trigger`` + helpers + ``compute_next_fire`` +
``validate_cron_expression``) relocated to :mod:`modulo.core.cron_helpers` (the
SAQ home, plan F1) and is re-exported here for backward compatibility until PR C
deletes the Celery beat path. Celery beat is GATED OFF by the entrypoint when
``SAQ_ENABLED=false`` (shadow), so ``CronFireTask`` is the fallback path only.

Architecture
------------
*DatabaseCronScheduler* is a custom Celery beat ``Scheduler`` that queries
the ``triggers`` table for rows where ``trigger_type = 'cron'``,
``active = true``, and ``next_fire_at <= now()``.
"""

import datetime
import logging
import uuid
from typing import Any

try:
    from celery import Celery, Task
    from celery.beat import ScheduleEntry, Scheduler
except ImportError:
    import typing

    if typing.TYPE_CHECKING:
        from celery import Celery, Task
        from celery.beat import ScheduleEntry, Scheduler
    Celery = Task = ScheduleEntry = Scheduler = object

from sqlalchemy import select, text
from sqlalchemy.exc import InterfaceError, OperationalError, TimeoutError

from modulo.core.cron_helpers import (  # noqa: F401  (re-exported for legacy importers)
    _count_active_runs,
    _log_event,
    _set_rls_org,
    compute_next_fire,
    fire_cron_trigger,
    validate_cron_expression,
)
from modulo.core.dispatch import dispatch_run_sync
from modulo.core.pipeline_execution import SchedulerDBError

_log = logging.getLogger(__name__)

_ACTIVE_STATUSES = ("running", "pending", "awaiting_human", "claimed", "waiting_for_lock")


# ---------------------------------------------------------------------------
# Celery task — fire one cron trigger
# ---------------------------------------------------------------------------


class CronFireTask(Task):  # type: ignore[misc]
    """Task that fires a single cron trigger — creates a Run and logs a TriggerEvent.

    Runs inside a Celery worker process. Delegates to
    :func:`modulo.core.cron_helpers.fire_cron_trigger` with
    ``advance_next_fire_at=True`` (the legacy Celery behaviour advances
    ``next_fire_at`` at fire time; the SAQ path advances at enqueue time).
    """

    name = "modulo.cron.fire_trigger"
    autoretry_for = (Exception,)
    max_retries = 3
    default_retry_delay = 60

    def run(
        self,
        trigger_id: str,
        org_id: str,
        pipeline_id: str,
        cron_expression: str,
        snapshot_id: str = "",
    ) -> dict[str, Any]:
        """Fire a cron trigger — creates a run and dispatches it to Celery."""
        import asyncio

        _snap = uuid.UUID(snapshot_id) if snapshot_id else None

        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            result = asyncio.run(
                fire_cron_trigger(
                    trigger_id=uuid.UUID(trigger_id),
                    org_id=uuid.UUID(org_id),
                    pipeline_id=uuid.UUID(pipeline_id),
                    snapshot_id=_snap,
                    cron_expression=cron_expression,
                    advance_next_fire_at=True,
                )
            )
        else:
            coro = fire_cron_trigger(
                trigger_id=uuid.UUID(trigger_id),
                org_id=uuid.UUID(org_id),
                pipeline_id=uuid.UUID(pipeline_id),
                snapshot_id=_snap,
                cron_expression=cron_expression,
                advance_next_fire_at=True,
            )
            future = asyncio.run_coroutine_threadsafe(coro, loop)
            result = future.result()

        if result.get("status") == "fired" and result.get("run_id"):
            dispatch_run_sync(
                result["run_id"],
                org_id,
                queue="runs",
                celery_queue="runs_automated",
            )

        return result


# ---------------------------------------------------------------------------
# Database-backed beat scheduler
# ---------------------------------------------------------------------------


class DatabaseCronEntry(ScheduleEntry):  # type: ignore[misc]
    """A single schedule entry representing one cron trigger row."""

    def __init__(
        self,
        trigger_id: uuid.UUID,
        org_id: uuid.UUID,
        pipeline_id: uuid.UUID,
        snapshot_id: uuid.UUID | None,
        cron_expression: str,
        next_fire_at: datetime.datetime,
    ) -> None:
        self._trigger_id = trigger_id
        self._org_id = org_id
        self._pipeline_id = pipeline_id
        self._snapshot_id = snapshot_id
        self._cron_expression = cron_expression
        self._next_fire_at = next_fire_at

    @property
    def name(self) -> str:
        return f"cron-{self._trigger_id}"

    @property
    def task(self) -> str:
        return CronFireTask.name

    @property
    def schedule(self) -> Any:
        return self

    @property
    def args(self) -> list[str]:
        return [
            str(self._trigger_id),
            str(self._org_id),
            str(self._pipeline_id),
            self._cron_expression,
            str(self._snapshot_id) if self._snapshot_id is not None else "",
        ]

    @property
    def kwargs(self) -> dict[str, Any]:
        return {}

    @property
    def options(self) -> dict[str, Any]:
        return {"task_id": f"cron-{self._trigger_id}-{self._next_fire_at.timestamp():.0f}"}

    def is_due(self) -> tuple[bool, datetime.timedelta]:
        now = datetime.datetime.now(datetime.UTC)
        if self._next_fire_at <= now:
            return (True, datetime.timedelta(seconds=0))
        delay = (self._next_fire_at - now).total_seconds()
        return (False, datetime.timedelta(seconds=max(delay, 0)))

    def __repr__(self) -> str:
        return f"<DatabaseCronEntry trigger={self._trigger_id} next={self._next_fire_at.isoformat()}>"


class DatabaseCronScheduler(Scheduler):  # type: ignore[misc]
    """Celery beat scheduler that reads cron triggers from the database.

    On each tick (default every 30 s via ``max_interval``), the scheduler
    queries the ``triggers`` table for enabled cron rows whose
    ``next_fire_at <= now()`` and creates one ``DatabaseCronEntry`` per match.
    """

    Entry = DatabaseCronEntry

    def __init__(self, app: Celery, **kwargs: Any) -> None:
        self._schedule: dict[str, DatabaseCronEntry] = {}
        super().__init__(app, **kwargs)

    def setup_schedule(self) -> None:
        """Populate the schedule from the database."""
        self._sync_with_db()

    def tick(self) -> float:
        """Called periodically by Celery beat. Syncs with DB and returns seconds until next tick."""
        self._sync_with_db()
        return float(super().tick())

    def _sync_with_db(self) -> None:
        """Query the database and update the in-memory schedule."""
        try:
            rows = self._fetch_due_triggers()
        except SchedulerDBError:
            return  # Beat will retry on next tick

        current_ids = set(self._schedule.keys())
        db_ids: set[str] = set()

        for row in rows:
            entry_id = f"cron-{row['trigger_id']}"
            db_ids.add(entry_id)

            if entry_id in self._schedule:
                existing = self._schedule[entry_id]
                if existing._next_fire_at == row["next_fire_at"]:
                    continue

            entry = DatabaseCronEntry(
                trigger_id=row["trigger_id"],
                org_id=row["org_id"],
                pipeline_id=row["pipeline_id"],
                snapshot_id=row["snapshot_id"],
                cron_expression=row["cron_expression"],
                next_fire_at=row["next_fire_at"],
            )
            self._schedule[entry_id] = entry

        # Remove entries no longer in the DB
        stale = current_ids - db_ids
        for sid in stale:
            self._schedule.pop(sid, None)

    def _fetch_due_triggers(self) -> list[dict[str, Any]]:
        """Sync query for cron triggers due to fire — runs inside Celery beat."""
        try:
            from datetime import UTC, datetime

            from modulo.core.pipeline_execution import get_beat_sync_session
            from modulo.db.models.trigger import Trigger

            session = get_beat_sync_session()
            try:
                now = datetime.now(UTC)
                rows = session.execute(
                    select(
                        Trigger.id,
                        Trigger.organisation_id,
                        Trigger.pipeline_id,
                        Trigger.config_json,
                        Trigger.cron_expression,
                        Trigger.next_fire_at,
                    ).where(
                        Trigger.trigger_type == "cron",
                        Trigger.active,
                        Trigger.next_fire_at <= now,
                        Trigger.cron_expression.isnot(None),
                    )
                ).all()

                triggers: list[dict[str, Any]] = []
                pipelines_needing_snapshots: set[uuid.UUID] = set()
                for row in rows:
                    config = row.config_json or {}
                    if not config.get("snapshot_id"):
                        pipelines_needing_snapshots.add(row.pipeline_id)

                latest_snapshots: dict[uuid.UUID, uuid.UUID] = {}
                if pipelines_needing_snapshots:
                    pids = list(pipelines_needing_snapshots)
                    snap_result = session.execute(
                        text(
                            "SELECT DISTINCT ON (pipeline_id) pipeline_id, id "
                            "FROM pipeline_snapshots "
                            "WHERE pipeline_id = ANY(:pids) "
                            "ORDER BY pipeline_id, created_at DESC"
                        ),
                        {"pids": pids},
                    )
                    for pipeline_id, snapshot_id in snap_result:
                        latest_snapshots[pipeline_id] = snapshot_id

                for row in rows:
                    config = row.config_json or {}
                    snapshot_id_str = config.get("snapshot_id")
                    if snapshot_id_str:
                        try:
                            snapshot_id = uuid.UUID(snapshot_id_str)
                        except (ValueError, TypeError):
                            _log.warning(
                                "Cron trigger %s has invalid snapshot_id in config — will auto-create on fire", row.id
                            )
                            snapshot_id = None
                    else:
                        snapshot_id = latest_snapshots.get(row.pipeline_id)
                        if snapshot_id is None:
                            _log.info("Cron trigger %s has no snapshots — will auto-create on fire", row.id)

                    triggers.append(
                        {
                            "trigger_id": row.id,
                            "org_id": row.organisation_id,
                            "pipeline_id": row.pipeline_id,
                            "snapshot_id": snapshot_id,
                            "cron_expression": row.cron_expression,
                            "next_fire_at": row.next_fire_at,
                        }
                    )
                return triggers
            finally:
                session.close()
        except (
            OperationalError,
            InterfaceError,
            TimeoutError,
        ):
            _log.exception("Failed to fetch cron triggers from database")
            raise SchedulerDBError("Cron scheduler DB query failed") from None

    # max_interval: class attribute (not @property) so Celery can set it
    max_interval: int = 30
