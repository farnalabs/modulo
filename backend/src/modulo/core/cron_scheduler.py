"""Celery beat scheduler that reads cron trigger rows from the database.

Architecture
------------
*DatabaseCronScheduler* is a custom Celery beat ``Scheduler`` that queries
the ``triggers`` table for rows where ``trigger_type = 'cron'``,
``active = true``, and ``next_fire_at <= now()``.

On each tick it creates a Celery ``Huey``-like entry per matching trigger,
firing a task that:
  1. Re-reads the trigger row (with ``FOR UPDATE`` to serialise)
  2. Checks concurrency limits (``max_concurrent_runs``)
  3. Creates a ``Run`` via ``create_run()``
  4. Logs a ``TriggerEvent``
  5. Updates ``last_fired_at`` and ``next_fire_at``

The scheduler runs inside the ``celery beat`` process and does **not** hold
an open DB session between ticks — it opens a new connection per tick.
"""

import datetime
import logging
import uuid
from typing import Any

from celery import Celery, Task
from celery.beat import ScheduleEntry, Scheduler
from croniter import croniter
from sqlalchemy import func, select, text, update
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine

from modulo.db.crud.run import create_run
from modulo.db.models.run import Run
from modulo.db.models.trigger import Trigger
from modulo.db.models.trigger_event import TriggerEvent
from modulo.settings import get_settings

_log = logging.getLogger(__name__)

_ENGINE: AsyncEngine | None = None


def _get_engine() -> AsyncEngine:
    global _ENGINE
    if _ENGINE is None:
        _ENGINE = create_async_engine(get_settings().database_url)
    return _ENGINE


_ACTIVE_STATUSES = ("pending", "running", "awaiting_human", "claimed", "waiting_for_lock")


# ---------------------------------------------------------------------------
# Validation helpers
# ---------------------------------------------------------------------------


def validate_cron_expression(expression: str, timezone: str = "UTC") -> str | None:
    """Validate a cron expression.

    Returns ``None`` if valid, or an error message string if invalid.
    """
    try:
        croniter(expression)
    except (ValueError, KeyError) as exc:
        return str(exc)
    try:
        import zoneinfo

        zoneinfo.ZoneInfo(timezone)
    except (ValueError, KeyError, TypeError) as exc:
        return f"Invalid timezone: {exc}"
    return None


def compute_next_fire(cron_expression: str, after: datetime.datetime | None = None) -> datetime.datetime:
    """Compute the next fire time for a cron expression.

    If *after* is None, uses the current UTC time.
    """
    base = after or datetime.datetime.now(datetime.UTC)
    cron = croniter(cron_expression, base)
    next_dt = cron.get_next(datetime.datetime)
    if not isinstance(next_dt, datetime.datetime):
        msg = f"croniter returned unexpected type: {type(next_dt)}"
        raise TypeError(msg)
    return next_dt


# ---------------------------------------------------------------------------
# Celery task — fire one cron trigger
# ---------------------------------------------------------------------------

celery_app_global: Celery | None = None


def get_celery_app() -> Celery:
    global celery_app_global
    if celery_app_global is None:
        from modulo.celery_app import celery_app as _app

        celery_app_global = _app
    return celery_app_global


class CronFireTask(Task):  # type: ignore[misc]
    """Task that fires a single cron trigger — creates a Run and logs a TriggerEvent.

    Runs inside a Celery worker process.
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
        snapshot_id: str,
        cron_expression: str,
    ) -> dict[str, Any]:
        """Fire a cron trigger — creates a run in the database.

        This is a synchronous task because Celery classic tasks are sync.
        We use ``asyncio.run()`` to drive the async DB operations.
        """
        import asyncio

        return asyncio.run(
            fire_cron_trigger(
                trigger_id=uuid.UUID(trigger_id),
                org_id=uuid.UUID(org_id),
                pipeline_id=uuid.UUID(pipeline_id),
                snapshot_id=uuid.UUID(snapshot_id),
                cron_expression=cron_expression,
            )
        )


async def fire_cron_trigger(
    *,
    trigger_id: uuid.UUID,
    org_id: uuid.UUID,
    pipeline_id: uuid.UUID,
    snapshot_id: uuid.UUID,
    cron_expression: str,
) -> dict[str, Any]:
    """Core fire logic — runs inside asyncio.run() inside the Celery task."""
    engine = _get_engine()
    factory = async_sessionmaker(engine, expire_on_commit=False)

    async with factory() as session:
        async with session.begin():
            await _set_rls_org(session, org_id)

            # Re-read trigger with FOR UPDATE to serialise concurrent fires
            result = await session.execute(
                select(Trigger).where(Trigger.id == trigger_id, Trigger.organisation_id == org_id).with_for_update()
            )
            trigger = result.scalar_one_or_none()
            if trigger is None or not trigger.active:
                return {"status": "skipped", "reason": "trigger_inactive_or_missing"}

            # Concurrency check
            active_count = await _count_active_runs(session, pipeline_id)
            if active_count >= trigger.max_concurrent_runs:
                await _log_event(
                    session,
                    trigger=trigger,
                    org_id=org_id,
                    result="concurrency_limit_reached",
                    error_detail=(f"Active runs: {active_count}, limit: {trigger.max_concurrent_runs}"),
                )
                return {
                    "status": "skipped",
                    "reason": "concurrency_limit",
                    "active_runs": active_count,
                }

            # Daily spend limit check
            spend_limit = trigger.daily_spend_limit
            if spend_limit is not None:
                today_start = datetime.datetime.now(datetime.UTC).replace(hour=0, minute=0, second=0, microsecond=0)
                cost_result = await session.execute(
                    select(func.coalesce(func.sum(Run.total_cost_usd), 0)).where(
                        Run.trigger_id == trigger_id,
                        Run.organisation_id == org_id,
                        Run.created_at >= today_start,
                    )
                )
                today_cost = cost_result.scalar_one()
                if today_cost >= spend_limit:  # type: ignore[operator]
                    await _log_event(
                        session,
                        trigger=trigger,
                        org_id=org_id,
                        result="spend_limit_reached",
                        error_detail=(f"Daily spend limit {trigger.daily_spend_limit} reached (today: {today_cost})"),
                    )
                    return {
                        "status": "skipped",
                        "reason": "spend_limit",
                        "daily_spend_limit": str(spend_limit),
                        "today_cost": str(today_cost),
                    }

            # Build input payload from config
            config = trigger.config_json or {}
            input_payload = config.get("input_template", {})

            # Create the run
            run = await create_run(
                session,
                org_id=org_id,
                pipeline_id=pipeline_id,
                snapshot_id=snapshot_id,
                trigger_type="cron",
                trigger_id=trigger_id,
                input_payload=input_payload,
            )

            # Log TriggerEvent
            event = await _log_event(
                session,
                trigger=trigger,
                org_id=org_id,
                result="accepted",
                run_id=run.id,
            )

            # Update last_fired_at and next_fire_at
            now = datetime.datetime.now(datetime.UTC)
            next_fire = compute_next_fire(cron_expression, after=now)
            await session.execute(
                update(Trigger).where(Trigger.id == trigger_id).values(last_fired_at=now, next_fire_at=next_fire)
            )

            _log.info(
                "Cron trigger %s fired → run %s (next fire: %s)",
                trigger_id,
                run.id,
                next_fire.isoformat(),
            )

            return {
                "status": "fired",
                "run_id": str(run.id),
                "event_id": str(event.id),
                "next_fire_at": next_fire.isoformat(),
            }


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
        snapshot_id: uuid.UUID,
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
            str(self._snapshot_id),
            self._cron_expression,
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
        super().__init__(app, **kwargs)
        self._schedule: dict[str, DatabaseCronEntry] = {}

    def setup_schedule(self) -> None:
        """Populate the schedule from the database."""
        self._sync_with_db()

    def tick(self) -> float:
        """Called periodically by Celery beat. Syncs with DB and returns seconds until next tick."""
        self._sync_with_db()
        return float(super().tick())

    def _sync_with_db(self) -> None:
        """Query the database and update the in-memory schedule."""
        import asyncio

        rows = asyncio.run(self._fetch_due_triggers())

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

    async def _fetch_due_triggers(self) -> list[dict[str, Any]]:
        """Async query for cron triggers due to fire."""
        try:
            factory = async_sessionmaker(_get_engine(), expire_on_commit=False)

            async with factory() as session:
                now = datetime.datetime.now(datetime.UTC)
                result = await session.execute(
                    select(
                        Trigger.id,
                        Trigger.organisation_id,
                        Trigger.pipeline_id,
                        Trigger.config_json,
                        Trigger.cron_expression,
                        Trigger.next_fire_at,
                    ).where(
                        Trigger.trigger_type == "cron",
                        Trigger.active == True,  # noqa: E712
                        Trigger.next_fire_at <= now,
                        Trigger.cron_expression.isnot(None),
                    )
                )
                rows = result.all()

                triggers: list[dict[str, Any]] = []
                for row in rows:
                    config = row.config_json or {}
                    snapshot_id_str = config.get("snapshot_id")
                    try:
                        snapshot_id = uuid.UUID(snapshot_id_str) if snapshot_id_str else uuid.uuid4()
                    except (ValueError, TypeError):
                        snapshot_id = uuid.uuid4()

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
        except Exception:
            _log.exception("Failed to fetch cron triggers from database")
            return []

    @property
    def max_interval(self) -> int:
        """Maximum sleep between ticks — 30 seconds."""
        return 30


# ---------------------------------------------------------------------------
# RLS + helpers (standalone copies to avoid circular imports at module level)
# ---------------------------------------------------------------------------


async def _set_rls_org(session: AsyncSession, org_id: uuid.UUID) -> None:
    """Set RLS organisation context inside the current transaction."""
    await session.execute(
        text("SELECT set_config('app.organisation_id', :val, true)"),
        {"val": str(org_id)},
    )


async def _count_active_runs(session: AsyncSession, pipeline_id: uuid.UUID) -> int:
    from sqlalchemy import func

    result = await session.execute(
        select(func.count()).where(
            Run.pipeline_id == pipeline_id,
            Run.status.in_(_ACTIVE_STATUSES),
        )
    )
    return result.scalar_one()


async def _log_event(
    session: AsyncSession,
    *,
    trigger: Trigger,
    org_id: uuid.UUID,
    result: str,
    run_id: uuid.UUID | None = None,
    error_detail: str | None = None,
) -> TriggerEvent:
    import hashlib

    payload_hash = hashlib.sha256(b"cron").hexdigest()
    event = TriggerEvent(
        organisation_id=org_id,
        trigger_id=trigger.id,
        trigger_type="cron",
        raw_payload_hash=payload_hash,
        validation_result=result,
        run_id=run_id,
        error_detail=error_detail,
    )
    session.add(event)
    await session.flush()
    return event
