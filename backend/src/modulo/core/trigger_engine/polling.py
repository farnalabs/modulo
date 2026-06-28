"""Polling trigger — connector-driven condition evaluation and run creation.

Fire logic lives in ``fire_polling_trigger()`` — used by both Celery beat
(``PollingFireTask`` / ``DatabasePollingScheduler``) and the in-process
scheduler (``InProcessPollingScheduler`` in ``modulo.core.in_process_scheduler``).
"""

import datetime
import hashlib
import json
import logging
import uuid
from typing import Any

import jmespath
from sqlalchemy import select, text, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from modulo.connectors.base import ConnectorBase, ConnectorQuery, ConnectorResult
from modulo.core.secrets_backend import create_secrets_backend
from modulo.db.crud.run import create_run
from modulo.db.models.connector_instance import ConnectorInstance
from modulo.db.models.run import Run
from modulo.db.models.trigger import Trigger
from modulo.db.models.trigger_event import TriggerEvent
from modulo.settings import get_settings

try:
    from celery import Celery, Task
    from celery.beat import ScheduleEntry, Scheduler
except ImportError:
    import typing

    if typing.TYPE_CHECKING:
        from celery import Celery, Task  # noqa: F401
        from celery.beat import ScheduleEntry, Scheduler  # noqa: F401
    Celery = Task = ScheduleEntry = Scheduler = object

_log = logging.getLogger(__name__)

_ACTIVE_STATUSES = ("pending", "running", "awaiting_human", "claimed", "waiting_for_lock")

_engine = None


def _get_engine() -> Any:
    global _engine
    if _engine is None:
        from modulo.settings import get_settings

        _engine = create_async_engine(get_settings().database_url)
    return _engine


# ---------------------------------------------------------------------------
# Connector builder (standalone copy to avoid circular imports)
# ---------------------------------------------------------------------------


def _build_polling_connector(type_id: str, config: dict[str, Any], creds: dict[str, Any]) -> ConnectorBase:
    """Build a one-shot connector for polling queries.

    Mirrors ``modulo.core.connector_hub._build_connector()`` but does not
    wrap in a ``_TracedConnector`` since polling runs outside a normal run context.
    """
    from modulo.connectors.filesystem import FilesystemConnector
    from modulo.connectors.github import GitHubConnector
    from modulo.connectors.gitlab import GitLabConnector
    from modulo.connectors.jira import JiraConnector
    from modulo.connectors.linear import LinearConnector
    from modulo.connectors.slack import SlackConnector

    match type_id:
        case "filesystem":
            base_path = config.get("base_path")
            if not base_path:
                raise ValueError("FilesystemConnector requires 'base_path' in config_json")
            return FilesystemConnector(base_path=base_path)
        case "github":
            return GitHubConnector(token=creds["token"])
        case "gitlab":
            return GitLabConnector(token=creds["token"])
        case "linear":
            return LinearConnector(api_key=creds["api_key"])
        case "jira":
            instance = config.get("instance", config.get("base_url", ""))
            if not instance:
                raise ValueError("JiraConnector requires 'instance' in config_json")
            return JiraConnector(instance=instance, creds=creds)
        case "slack":
            return SlackConnector(bot_token=creds["bot_token"])
        case _:
            raise ValueError(f"Unsupported connector type for polling: {type_id!r}")


# ---------------------------------------------------------------------------
# JMESPath condition evaluation
# ---------------------------------------------------------------------------


def evaluate_condition(
    result: ConnectorResult,
    condition_expression: str | None,
) -> bool:
    """Evaluate a JMESPath *condition_expression* against a connector result.

    If *condition_expression* is ``None`` or empty, any non-empty result set
    is treated as a match.

    The expression is evaluated against the ``records`` list of the result.
    Returns ``True`` if the expression yields a truthy value (non-empty list,
    non-zero number, ``True`` boolean, or a non-null value).
    """
    if not condition_expression:
        return len(result.records) > 0

    try:
        compiled = jmespath.compile(condition_expression)
    except Exception as exc:
        raise ValueError(f"Invalid JMESPath expression: {exc}") from exc

    value = compiled.search(result.records)
    if value is None:
        return False
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value != 0
    if isinstance(value, (list, dict)):
        return len(value) > 0
    if isinstance(value, str):
        return len(value) > 0
    return True


# ---------------------------------------------------------------------------
# Celery task — fire one polling trigger
# ---------------------------------------------------------------------------

def get_celery_app() -> Celery:
    from modulo.celery_app import get_celery_app as _get_celery_app

    return _get_celery_app()


class PollingFireTask(Task):  # type: ignore[misc]
    """Task that fires a single polling trigger.

    Runs the poll query through the configured connector, evaluates the
    JMESPath condition, and creates a Run when the condition is met.
    """

    name = "modulo.polling.fire_trigger"
    autoretry_for = (Exception,)
    max_retries = 2
    default_retry_delay = 30

    def run(
        self,
        trigger_id: str,
        org_id: str,
        pipeline_id: str,
        connector_instance_id: str,
        poll_query: str,
        condition_expression: str | None,
    ) -> dict[str, Any]:
        """Fire a polling trigger synchronously via ``asyncio.run()``."""
        import asyncio

        return asyncio.run(
            fire_polling_trigger(
                trigger_id=uuid.UUID(trigger_id),
                org_id=uuid.UUID(org_id),
                pipeline_id=uuid.UUID(pipeline_id),
                connector_instance_id=uuid.UUID(connector_instance_id),
                poll_query=poll_query,
                condition_expression=condition_expression,
            )
        )


async def fire_polling_trigger(
    *,
    trigger_id: uuid.UUID,
    org_id: uuid.UUID,
    pipeline_id: uuid.UUID,
    connector_instance_id: uuid.UUID,
    poll_query: str,
    condition_expression: str | None,
) -> dict[str, Any]:
    """Fire a polling trigger — runs the connector query and evaluates the condition.

    Shared between Celery beat tasks and the in-process asyncio scheduler.
    Opens its own DB connection so it can be called from both sync (Celery)
    and async contexts.
    """
    settings = get_settings()
    factory = async_sessionmaker(_get_engine(), expire_on_commit=False)

    async with factory() as session:
        async with session.begin():
            await _set_rls_org(session, org_id)

            # Re-read trigger with FOR UPDATE
            result = await session.execute(
                select(Trigger).where(Trigger.id == trigger_id, Trigger.organisation_id == org_id).with_for_update()
            )
            trigger = result.scalar_one_or_none()
            if trigger is None or not trigger.active:
                return {"status": "skipped", "reason": "trigger_inactive_or_missing"}
            if trigger.next_fire_at is not None and trigger.next_fire_at > datetime.datetime.now(datetime.UTC):
                return {"status": "skipped", "reason": "already_fired_this_cycle"}

            # Concurrency check
            active_count = await _count_active_runs(session, pipeline_id)
            if active_count >= trigger.max_concurrent_runs:
                await _log_poll_event(
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

            # Load connector instance
            conn_result = await session.execute(
                select(ConnectorInstance).where(
                    ConnectorInstance.id == connector_instance_id,
                    ConnectorInstance.organisation_id == org_id,
                )
            )
            connector_instance = conn_result.scalar_one_or_none()
            if connector_instance is None:
                await _log_poll_event(
                    session,
                    trigger=trigger,
                    org_id=org_id,
                    result="poll_error",
                    error_detail=f"Connector instance {connector_instance_id} not found",
                )
                return {"status": "error", "reason": "connector_not_found"}

            # Decrypt credentials and build connector
            try:
                secrets_backend = create_secrets_backend(fernet_key=settings.fernet_key, session=session)
                raw_creds = await secrets_backend.get_secret(str(connector_instance.id))
                creds: dict[str, Any] = json.loads(raw_creds)
                connector = _build_polling_connector(
                    connector_instance.connector_type_id,
                    connector_instance.config_json,
                    creds,
                )
            except Exception as exc:
                await _log_poll_event(
                    session,
                    trigger=trigger,
                    org_id=org_id,
                    result="poll_error",
                    error_detail=f"Failed to initialise connector: {exc}",
                )
                return {"status": "error", "reason": "connector_init_failed"}

            # Run poll query
            try:
                query = ConnectorQuery(resource=poll_query)
                query_result = await connector.query(query)
            except Exception as exc:
                await _log_poll_event(
                    session,
                    trigger=trigger,
                    org_id=org_id,
                    result="poll_error",
                    error_detail=f"Poll query failed: {exc}",
                )
                return {"status": "error", "reason": "query_failed", "error": str(exc)}

            # Evaluate condition
            try:
                condition_met = evaluate_condition(query_result, condition_expression)
            except Exception as exc:
                await _log_poll_event(
                    session,
                    trigger=trigger,
                    org_id=org_id,
                    result="poll_error",
                    error_detail=f"Condition evaluation failed: {exc}",
                )
                return {"status": "error", "reason": "condition_eval_failed", "error": str(exc)}

            if not condition_met:
                # Log no_match — condition not satisfied this cycle
                await _log_poll_event(
                    session,
                    trigger=trigger,
                    org_id=org_id,
                    result="no_match",
                )
                # Update next_fire_at even on no-match
                await _update_next_fire(session, trigger)
                return {"status": "no_match"}

            # Snapshot ID from trigger config
            config = trigger.config_json or {}
            snapshot_id_str = config.get("snapshot_id")
            try:
                snapshot_id = uuid.UUID(snapshot_id_str) if snapshot_id_str else uuid.uuid4()
            except (ValueError, TypeError):
                snapshot_id = uuid.uuid4()

            # Build input payload from query result
            input_payload: dict[str, Any] = {
                "records": query_result.records,
                "total": query_result.total,
                "poll_query": poll_query,
            }

            # Create the run
            run = await create_run(
                session,
                org_id=org_id,
                pipeline_id=pipeline_id,
                snapshot_id=snapshot_id,
                trigger_type="polling",
                trigger_id=trigger_id,
                input_payload=input_payload,
            )

            # Log TriggerEvent — condition_met
            event = await _log_poll_event(
                session,
                trigger=trigger,
                org_id=org_id,
                result="condition_met",
                run_id=run.id,
            )

            # Update last_fired_at and next_fire_at
            await _update_next_fire(session, trigger)

            _log.info(
                "Polling trigger %s fired → run %s (condition met)",
                trigger_id,
                run.id,
            )

            return {
                "status": "fired",
                "run_id": str(run.id),
                "event_id": str(event.id),
            }


async def _update_next_fire(session: AsyncSession, trigger: Trigger) -> None:
    """Compute and persist the next fire time based on poll_interval_seconds."""
    config = trigger.config_json or {}
    interval = config.get("poll_interval_seconds", 60)
    now = datetime.datetime.now(datetime.UTC)
    next_fire = now + datetime.timedelta(seconds=int(interval))
    await session.execute(
        update(Trigger).where(Trigger.id == trigger.id).values(last_fired_at=now, next_fire_at=next_fire)
    )


# ---------------------------------------------------------------------------
# Database-backed beat scheduler
# ---------------------------------------------------------------------------


class DatabasePollingEntry(ScheduleEntry):  # type: ignore[misc]
    """A single schedule entry representing one polling trigger row."""

    def __init__(
        self,
        trigger_id: uuid.UUID,
        org_id: uuid.UUID,
        pipeline_id: uuid.UUID,
        connector_instance_id: uuid.UUID,
        poll_query: str,
        condition_expression: str | None,
        next_fire_at: datetime.datetime,
    ) -> None:
        self._trigger_id = trigger_id
        self._org_id = org_id
        self._pipeline_id = pipeline_id
        self._connector_instance_id = connector_instance_id
        self._poll_query = poll_query
        self._condition_expression = condition_expression
        self._next_fire_at = next_fire_at

    @property
    def name(self) -> str:
        return f"polling-{self._trigger_id}"

    @property
    def task(self) -> str:
        return PollingFireTask.name

    @property
    def schedule(self) -> Any:
        return self

    @property
    def args(self) -> list[str]:
        return [
            str(self._trigger_id),
            str(self._org_id),
            str(self._pipeline_id),
            str(self._connector_instance_id),
            self._poll_query,
            self._condition_expression or "",
        ]

    @property
    def kwargs(self) -> dict[str, Any]:
        return {}

    @property
    def options(self) -> dict[str, Any]:
        return {"task_id": f"polling-{self._trigger_id}-{self._next_fire_at.timestamp():.0f}"}

    def is_due(self) -> tuple[bool, datetime.timedelta]:
        now = datetime.datetime.now(datetime.UTC)
        if self._next_fire_at <= now:
            return (True, datetime.timedelta(seconds=0))
        delay = (self._next_fire_at - now).total_seconds()
        return (False, datetime.timedelta(seconds=max(delay, 0)))

    def __repr__(self) -> str:
        return f"<DatabasePollingEntry trigger={self._trigger_id} next={self._next_fire_at.isoformat()}>"


class DatabasePollingScheduler(Scheduler):  # type: ignore[misc]
    """Celery beat scheduler that reads polling triggers from the database.

    Queries the ``triggers`` table for enabled polling rows whose
    ``next_fire_at <= now()`` and creates one ``DatabasePollingEntry`` per match.

    Used only when Celery + Redis are available. Falls back to the in-process
    ``InProcessPollingScheduler`` when Redis is not configured.
    """

    Entry = DatabasePollingEntry

    def __init__(self, app: Celery, **kwargs: Any) -> None:
        # _schedule must exist before super().__init__ because it calls
        # setup_schedule() → _sync_with_db() which accesses self._schedule.
        self._schedule: dict[str, DatabasePollingEntry] = {}
        super().__init__(app, **kwargs)
        # Re-set max_interval after super().__init__ since Celery's base
        # class may overwrite it with app.conf.beat_max_loop_interval.
        self.max_interval = 30

    def setup_schedule(self) -> None:
        """Populate the schedule from the database."""
        self._sync_with_db()

    def tick(self) -> float:
        """Called periodically by Celery beat. Syncs with DB."""
        self._sync_with_db()
        return super().tick()  # type: ignore[no-any-return]

    def _sync_with_db(self) -> None:
        """Query the database and update the in-memory schedule."""
        import asyncio

        rows = asyncio.run(self._fetch_due_triggers())

        current_ids = set(self._schedule.keys())
        db_ids: set[str] = set()

        for row in rows:
            entry_id = f"polling-{row['trigger_id']}"
            db_ids.add(entry_id)

            if entry_id in self._schedule:
                existing = self._schedule[entry_id]
                if existing._next_fire_at == row["next_fire_at"]:
                    continue

            entry = DatabasePollingEntry(
                trigger_id=row["trigger_id"],
                org_id=row["org_id"],
                pipeline_id=row["pipeline_id"],
                connector_instance_id=row["connector_instance_id"],
                poll_query=row["poll_query"],
                condition_expression=row["condition_expression"],
                next_fire_at=row["next_fire_at"],
            )
            self._schedule[entry_id] = entry

        stale = current_ids - db_ids
        for sid in stale:
            self._schedule.pop(sid, None)

    async def _fetch_due_triggers(self) -> list[dict[str, Any]]:
        """Async query for polling triggers due to fire."""
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
                        Trigger.next_fire_at,
                    ).where(
                        Trigger.trigger_type == "polling",
                        Trigger.active == True,  # noqa: E712
                        Trigger.next_fire_at <= now,
                    )
                )
                rows = result.all()

                triggers: list[dict[str, Any]] = []
                for row in rows:
                    config = row.config_json or {}
                    ci_id_str = config.get("connector_instance_id")
                    try:
                        connector_instance_id = uuid.UUID(ci_id_str) if ci_id_str else None
                    except (ValueError, TypeError):
                        connector_instance_id = None
                    if connector_instance_id is None:
                        _log.warning("Polling trigger %s has no connector_instance_id", row.id)
                        continue

                    triggers.append(
                        {
                            "trigger_id": row.id,
                            "org_id": row.organisation_id,
                            "pipeline_id": row.pipeline_id,
                            "connector_instance_id": connector_instance_id,
                            "poll_query": config.get("poll_query", ""),
                            "condition_expression": config.get("condition_expression"),
                            "next_fire_at": row.next_fire_at,
                        }
                    )
                return triggers
        except Exception:
            _log.exception("Failed to fetch polling triggers from database")
            return []


# ---------------------------------------------------------------------------
# RLS + helpers (standalone copies, same pattern as cron_scheduler.py)
# ---------------------------------------------------------------------------


async def _set_rls_org(session: AsyncSession, org_id: uuid.UUID) -> None:
    await session.execute(
        text("SELECT set_config('app.organisation_id', :val, true)"),
        {"val": str(org_id)},
    )


async def _count_active_runs(session: AsyncSession, pipeline_id: uuid.UUID) -> int:
    from sqlalchemy import func as sa_func

    result = await session.execute(
        select(sa_func.count()).where(
            Run.pipeline_id == pipeline_id,
            Run.status.in_(_ACTIVE_STATUSES),
        )
    )
    return result.scalar_one()


async def _log_poll_event(
    session: AsyncSession,
    *,
    trigger: Trigger,
    org_id: uuid.UUID,
    result: str,
    run_id: uuid.UUID | None = None,
    error_detail: str | None = None,
) -> TriggerEvent:
    payload_hash = hashlib.sha256(b"polling").hexdigest()
    event = TriggerEvent(
        organisation_id=org_id,
        trigger_id=trigger.id,
        trigger_type="polling",
        raw_payload_hash=payload_hash,
        validation_result=result,
        run_id=run_id,
        error_detail=error_detail,
    )
    session.add(event)
    await session.flush()
    return event


# Backward-compatible alias — tests import the old private name.
_fire_polling_trigger = fire_polling_trigger
