"""SAQ scheduler helpers — per-item fire jobs, fire_due_triggers, dispatcher_reconcile.

Plan F1 / F3c / F3d. This module is the SAQ fire scheduler (replaced Celery beat)
tasks (``CronFireTask`` / ``PollingFireTask`` / ``ReportFireTask``, all removed
in PR C). All fire logic is reimplemented async against the shared DB session
pattern.

Multi-machine safety (F1, the single most important invariant):
``fire_due_triggers`` (a system cron on EVERY machine) advances ``next_fire_at``
ATOMICALLY at enqueue time — a conditional ``UPDATE ... WHERE next_fire_at <= now()
RETURNING id`` — and enqueues a per-item fire job ONLY for returned rows. A second
machine's tick sees ``next_fire_at`` already advanced and skips. Per-item fire
jobs get unique dedupe keys (``fire:{trigger_id}:{fire_epoch}``) so SAQ dedupe
never suppresses a distinct fire. ``next_fire_at`` is NEVER advanced at per-item
job execution.

Lost-epoch on crash (documented accepted): if the process dies after the atomic
UPDATE commits but before enqueue, one epoch is missed and the trigger self-heals
on the next tick. Enqueue failures after the UPDATE ingest an ``error_event``
(source='saq') and rely on next-tick re-fire; the >=1h missed-fire alert is a
follow-up owned by the hold monitor.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import threading
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from croniter import croniter
from redis.asyncio import Redis as AsyncRedis
from saq.queue.redis import RedisQueue
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine

from modulo.settings import get_settings

_log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

SYSTEM_ORG_ID = uuid.UUID("00000000-0000-0000-0000-000000000000")

# Per-item fire job knobs (plan F5): timeout=300, retries=2 (ONE retry),
# heartbeat=30, ttl=300. Reports share the runs queue as bounded jobs.
FIRE_JOB_TIMEOUT = 300
FIRE_JOB_HEARTBEAT = 30
FIRE_JOB_RETRIES = 2
FIRE_JOB_TTL = 300

# Report delivery (plan F1): failure backs off next_send_at +5min; deactivate
# after 5 consecutive failures. NEVER re-enqueue every 30s.
REPORT_BACKOFF_SECONDS = 300
REPORT_MAX_CONSECUTIVE_FAILURES = 5
_REPORT_FAILURE_COUNTER_TTL = 6 * 3600  # 6h — long enough to count 5 x 5min

# dispatcher_reconcile (system cron) — every 60s.
RECONCILE_STALE_HEARTBEAT_FACTOR = 2  # 2 * SAQ_JOB_HEARTBEAT = 600s

_ACTIVE_STATUSES = ("running", "pending", "awaiting_human", "claimed", "waiting_for_lock")

_ENGINE: AsyncEngine | None = None
_ENGINE_LOCK = threading.Lock()


def _get_engine() -> AsyncEngine:
    global _ENGINE
    if _ENGINE is None:
        with _ENGINE_LOCK:
            if _ENGINE is None:
                settings = get_settings()
                kw: dict[str, Any] = {"url": settings.database_url}
                if settings.modulo_db.lower() == "postgres":
                    kw["connect_args"] = {"timeout": 10, "ssl": False}
                _ENGINE = create_async_engine(**kw)
    return _ENGINE


def _open_factory() -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(_get_engine(), expire_on_commit=False, autobegin=False)


# ---------------------------------------------------------------------------
# Validation + next-fire computation (relocated from cron_scheduler.py)
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


def compute_next_fire(
    cron_expression: str,
    after: datetime | None = None,
    *,
    timezone: str = "UTC",
) -> datetime:
    """Compute the next fire time in *timezone* and return canonical UTC.

    If *after* is ``None``, the current UTC time is used. Naive values are
    interpreted as UTC for backwards compatibility. ``croniter`` defines DST
    handling: nonexistent local times advance to the first valid instant and
    ambiguous local times use the first occurrence.
    """
    base = after or datetime.now(UTC)
    if base.tzinfo is None:
        base = base.replace(tzinfo=UTC)
    local_base = base.astimezone(ZoneInfo(timezone))
    cron = croniter(cron_expression, local_base)
    next_dt = cron.get_next(datetime)
    if not isinstance(next_dt, datetime):
        msg = f"croniter returned unexpected type: {type(next_dt)}"
        raise TypeError(msg)
    if next_dt.tzinfo is None:
        next_dt = next_dt.replace(tzinfo=local_base.tzinfo)
    return next_dt.astimezone(UTC)


def compute_next_send(cron_expression: str, after: datetime | None = None) -> datetime:
    """Compute the next send time for a report cron expression."""
    base = after or datetime.now(UTC)
    cron = croniter(cron_expression, base)
    next_dt = cron.get_next(datetime)
    if not isinstance(next_dt, datetime):
        msg = f"croniter returned unexpected type: {type(next_dt)}"
        raise TypeError(msg)
    return next_dt


# ---------------------------------------------------------------------------
# Shared helpers (relocated from cron_scheduler.py)
# ---------------------------------------------------------------------------


async def _set_rls_org(session: AsyncSession, org_id: uuid.UUID) -> None:
    dialect = session.get_bind().dialect.name
    if dialect == "postgresql":
        await session.execute(
            text("SELECT set_config('app.organisation_id', :val, true)"),
            {"val": str(org_id)},
        )
    else:
        session.info["organisation_id"] = org_id


async def _count_active_runs(session: AsyncSession, trigger_id: uuid.UUID) -> int:
    from sqlalchemy import func, or_

    from modulo.db.models.run import Run

    result = await session.execute(
        select(func.count()).where(
            Run.trigger_id == trigger_id,
            Run.status.in_(_ACTIVE_STATUSES),
            or_(Run.cancellation_requested.is_(False), Run.cancellation_requested.is_(None)),
        )
    )
    return int(result.scalar_one() or 0)


async def _log_event(
    session: AsyncSession,
    *,
    trigger: Any,
    org_id: uuid.UUID,
    result: str,
    run_id: uuid.UUID | None = None,
    error_detail: str | None = None,
) -> Any:
    from modulo.db.models.trigger_event import TriggerEvent

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


async def _log_poll_event(
    session: AsyncSession,
    *,
    trigger: Any,
    org_id: uuid.UUID,
    result: str,
    run_id: uuid.UUID | None = None,
    error_detail: str | None = None,
) -> Any:
    from modulo.db.models.trigger_event import TriggerEvent

    payload_hash = hashlib.sha256(f"polling:{trigger.id}:{result}".encode()).hexdigest()
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


async def _ingest_saq_error(
    session: AsyncSession,
    org_id: uuid.UUID,
    *,
    function: str,
    message: str,
    context: dict[str, Any] | None = None,
) -> None:
    """Ingest an error event with source='saq' (plan F3d/F1 enqueue-failure alert).

    Runs in its own session/transaction (the caller's transaction stays intact)
    and never raises — error ingestion must not crash the scheduler tick.
    """
    import os

    try:
        from modulo.core.error_tracking import ErrorIngestionService
        from modulo.db.rls import set_rls_org
        from modulo.version import get_version

        async with _open_factory()() as ingest_session, ingest_session.begin():
            await set_rls_org(ingest_session, org_id)
            await ErrorIngestionService().ingest(
                ingest_session,
                org_id,
                {
                    "level": "error",
                    "message": message,
                    "source": "saq",
                    "context_json": {"function": function, **(context or {})},
                    "environment": os.environ.get("MODULO_ENV", "development"),
                    "version": get_version(),
                },
            )
    except asyncio.CancelledError:
        raise
    except Exception:
        _log.exception("cron_helpers.ingest_saq_error_failed function=%s", function)
        # Never let error ingestion crash the scheduler tick.


# ---------------------------------------------------------------------------
# Per-item fire jobs (runs worker)
# ---------------------------------------------------------------------------


async def fire_cron_trigger(
    *,
    trigger_id: uuid.UUID,
    org_id: uuid.UUID,
    pipeline_id: uuid.UUID,
    cron_expression: str,
    snapshot_id: uuid.UUID | None = None,
    factory: async_sessionmaker[AsyncSession] | None = None,
    advance_next_fire_at: bool = False,
) -> dict[str, Any]:
    """Fire one cron trigger — create a run, log the TriggerEvent, dispatch it.

    SAQ per-item fire job (``advance_next_fire_at=False``, the default): the
    atomic next_fire_at advance already happened in ``fire_due_triggers`` at
    enqueue time; this job sets ``last_fired_at`` only.

    ``advance_next_fire_at=True`` preserves the legacy behaviour (CronFireTask,
    removed in PR C).
    """
    from sqlalchemy import update

    from modulo.core.connector_hub.locking import _uuid_to_lock_keys
    from modulo.db.crud.run import create_run
    from modulo.db.models.trigger import Trigger

    if factory is None:
        factory = _open_factory()

    async with factory() as session, session.begin():
        await _set_rls_org(session, org_id)

        key1, key2 = _uuid_to_lock_keys(trigger_id)
        lock_result = await session.execute(
            text("SELECT pg_try_advisory_xact_lock(:key1, :key2)"),
            {"key1": key1, "key2": key2},
        )
        if not lock_result.scalar_one():
            return {"status": "skipped", "reason": "trigger_busy"}
        result = await session.execute(
            select(Trigger).where(Trigger.id == trigger_id, Trigger.organisation_id == org_id)
        )
        trigger = result.scalar_one_or_none()
        if trigger is None or not trigger.active:
            return {"status": "skipped", "reason": "trigger_inactive_or_missing"}

        active_count = await _count_active_runs(session, trigger_id)
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

        spend_limit = trigger.daily_spend_limit
        if spend_limit is not None:
            from sqlalchemy import func

            from modulo.db.models.run import Run

            today_start = datetime.now(UTC).replace(hour=0, minute=0, second=0, microsecond=0)
            cost_result = await session.execute(
                select(func.coalesce(func.sum(Run.total_cost_usd), 0)).where(
                    Run.trigger_id == trigger_id,
                    Run.organisation_id == org_id,
                    Run.created_at >= today_start,
                )
            )
            today_cost = cost_result.scalar_one()
            if today_cost is not None and today_cost >= spend_limit:
                await _log_event(
                    session,
                    trigger=trigger,
                    org_id=org_id,
                    result="spend_limit_reached",
                    error_detail=(f"Daily spend limit {spend_limit} reached (today: {today_cost})"),
                )
                return {
                    "status": "skipped",
                    "reason": "spend_limit",
                    "daily_spend_limit": str(spend_limit),
                    "today_cost": str(today_cost),
                }

        if snapshot_id is None:
            from modulo.db.crud.pipeline_snapshot import create_snapshot_from_live_graph

            new_snapshot = await create_snapshot_from_live_graph(
                session,
                pipeline_id=pipeline_id,
                account_id=None,
            )
            if new_snapshot is None:
                await _log_event(
                    session,
                    trigger=trigger,
                    org_id=org_id,
                    result="no_pipeline",
                    error_detail="Pipeline not found when trying to auto-create snapshot",
                )
                return {"status": "skipped", "reason": "pipeline_not_found"}
            snapshot_id = new_snapshot.id
            _log.info("Auto-created snapshot %s for cron trigger %s", snapshot_id, trigger_id)

        config = trigger.config_json or {}
        input_payload = config.get("input_template", {})

        run = await create_run(
            session,
            org_id=org_id,
            pipeline_id=pipeline_id,
            snapshot_id=snapshot_id,
            trigger_type="cron",
            trigger_id=trigger_id,
            input_payload=input_payload,
        )

        event = await _log_event(
            session,
            trigger=trigger,
            org_id=org_id,
            result="accepted",
            run_id=run.id,
        )

        # last_fired_at reflects an actual fire (run created). next_fire_at is
        # advanced ONLY at enqueue time (fire_due_triggers) — or here for the
        # legacy path (pre-PR C).
        values: dict[str, Any] = {"last_fired_at": datetime.now(UTC)}
        if advance_next_fire_at:
            values["next_fire_at"] = compute_next_fire(
                cron_expression,
                after=datetime.now(UTC),
                timezone=trigger.cron_timezone or "UTC",
            )
        await session.execute(update(Trigger).where(Trigger.id == trigger_id).values(**values))

        _log.info("Cron trigger %s fired -> run %s", trigger_id, run.id)
        return {
            "status": "fired",
            "run_id": str(run.id),
            "event_id": str(event.id),
            "input_payload": input_payload,
        }

    return {"status": "error", "reason": "unexpected"}


async def fire_polling_trigger(
    *,
    trigger_id: uuid.UUID,
    org_id: uuid.UUID,
    pipeline_id: uuid.UUID,
    connector_instance_id: uuid.UUID,
    poll_query: str,
    condition_expression: str | None,
) -> dict[str, Any]:
    """Fire one polling trigger — run the poll query, evaluate, create run.

    SAQ per-item fire job. ``next_fire_at`` was already advanced at enqueue time
    by ``fire_due_triggers``; this job sets ``last_fired_at`` only when a run is
    created (condition met). It does NOT re-check ``next_fire_at`` (the advance
    is enqueue-time by design).
    """
    import json

    from sqlalchemy import update

    from modulo.core.connector_hub.locking import _uuid_to_lock_keys
    from modulo.core.secrets_backend import create_secrets_backend
    from modulo.core.trigger_engine.polling import _build_polling_connector, evaluate_condition
    from modulo.db.crud.run import create_run
    from modulo.db.models.connector_instance import ConnectorInstance
    from modulo.db.models.trigger import Trigger

    factory = _open_factory()

    async with factory() as session, session.begin():
        await _set_rls_org(session, org_id)

        key1, key2 = _uuid_to_lock_keys(trigger_id)
        lock_result = await session.execute(
            text("SELECT pg_try_advisory_xact_lock(:key1, :key2)"),
            {"key1": key1, "key2": key2},
        )
        if not lock_result.scalar_one():
            return {"status": "skipped", "reason": "trigger_busy"}
        result = await session.execute(
            select(Trigger).where(Trigger.id == trigger_id, Trigger.organisation_id == org_id)
        )
        trigger = result.scalar_one_or_none()
        if trigger is None or not trigger.active:
            return {"status": "skipped", "reason": "trigger_inactive_or_missing"}

        active_count = await _count_active_runs(session, trigger_id)
        if active_count >= trigger.max_concurrent_runs:
            await _log_poll_event(
                session,
                trigger=trigger,
                org_id=org_id,
                result="concurrency_limit_reached",
                error_detail=(f"Active runs: {active_count}, limit: {trigger.max_concurrent_runs}"),
            )
            return {"status": "skipped", "reason": "concurrency_limit", "active_runs": active_count}

        conn_result = await session.execute(
            select(ConnectorInstance).where(
                ConnectorInstance.id == connector_instance_id,
                ConnectorInstance.organisation_id == org_id,
            )
        )
        connector_instance = conn_result.scalar_one_or_none()
        if connector_instance is None:
            _log.warning("Connector instance %s not found for polling trigger %s", connector_instance_id, trigger_id)
            await _log_poll_event(
                session,
                trigger=trigger,
                org_id=org_id,
                result="poll_error",
                error_detail=f"Connector instance {connector_instance_id} not found",
            )
            return {"status": "error", "reason": "connector_not_found"}

        settings = get_settings()
        try:
            secrets_backend = create_secrets_backend(fernet_key=settings.fernet_key, session=session)
            raw_creds = await secrets_backend.get_secret(str(connector_instance.id))
            creds: dict[str, Any] = json.loads(raw_creds)
            connector = _build_polling_connector(
                connector_instance.connector_type_id,
                connector_instance.config_json,
                creds,
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            _log.warning("Failed to initialise connector for polling trigger %s: %s", trigger_id, str(exc)[:200])
            await _log_poll_event(
                session,
                trigger=trigger,
                org_id=org_id,
                result="poll_error",
                error_detail=f"Failed to initialise connector: {str(exc)[:200]}",
            )
            return {"status": "error", "reason": "connector_init_failed"}

        from modulo.connectors.base import ConnectorQuery

        try:
            query = ConnectorQuery(resource=poll_query)
            query_result = await asyncio.wait_for(connector.query(query), timeout=60)
        except TimeoutError:
            _log.warning("Poll query timed out for trigger %s", trigger_id)
            await _log_poll_event(
                session,
                trigger=trigger,
                org_id=org_id,
                result="poll_error",
                error_detail="Poll query timed out after 60s",
            )
            return {"status": "error", "reason": "query_timeout"}
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            _log.warning("Poll query failed for trigger %s: %s", trigger_id, str(exc)[:200])
            await _log_poll_event(
                session,
                trigger=trigger,
                org_id=org_id,
                result="poll_error",
                error_detail=f"Poll query failed: {str(exc)[:200]}",
            )
            return {"status": "error", "reason": "query_failed", "error": str(exc)[:200]}

        try:
            condition_met = evaluate_condition(query_result, condition_expression)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            _log.warning("Condition evaluation failed for trigger %s: %s", trigger_id, exc)
            await _log_poll_event(
                session,
                trigger=trigger,
                org_id=org_id,
                result="poll_error",
                error_detail=f"Condition evaluation failed: {str(exc)[:200]}",
            )
            return {"status": "error", "reason": "condition_eval_failed", "error": str(exc)}

        if not condition_met:
            await _log_poll_event(
                session,
                trigger=trigger,
                org_id=org_id,
                result="no_match",
            )
            return {"status": "no_match"}

        config = trigger.config_json or {}
        snapshot_id_str = config.get("snapshot_id")
        try:
            snapshot_id = uuid.UUID(str(snapshot_id_str)) if snapshot_id_str else uuid.UUID(int=0)
        except (ValueError, TypeError):
            snapshot_id = uuid.UUID(int=0)

        input_payload: dict[str, Any] = {
            "records": query_result.records,
            "total": query_result.total,
            "poll_query": poll_query,
        }

        run = await create_run(
            session,
            org_id=org_id,
            pipeline_id=pipeline_id,
            snapshot_id=snapshot_id,
            trigger_type="polling",
            trigger_id=trigger_id,
            input_payload=input_payload,
        )

        event = await _log_poll_event(
            session,
            trigger=trigger,
            org_id=org_id,
            result="condition_met",
            run_id=run.id,
        )

        # last_fired_at only — next_fire_at was advanced at enqueue time.
        await session.execute(update(Trigger).where(Trigger.id == trigger_id).values(last_fired_at=datetime.now(UTC)))

        _log.info("Polling trigger %s fired -> run %s (condition met)", trigger_id, run.id)
        return {"status": "fired", "run_id": str(run.id), "event_id": str(event.id)}

    return {"status": "error", "reason": "unexpected"}


async def fire_report_trigger(*, report_id: uuid.UUID, org_id: uuid.UUID) -> dict[str, Any]:
    """Fire one scheduled report — generate, format, deliver (SAQ bounded job).

    Plan F1 report delivery: timeout=300 / retries=2 SAQ knobs at enqueue; on
    failure the job backs off ``next_send_at`` by +5min (or deactivates after 5
    consecutive failures) so ``fire_due_triggers`` NEVER re-enqueues every 30s.
    ``next_send_at`` was already advanced at enqueue time; success sets
    ``last_sent_at`` only.
    """
    from sqlalchemy import update

    from modulo.core.reports.scheduler import (
        _deliver_via_config,
        get_deliverer,
        get_formatter,
        get_generator,
    )
    from modulo.db.models.scheduled_report import ScheduledReport

    factory = _open_factory()
    settings = get_settings()
    redis_client = AsyncRedis.from_url(settings.redis_url, socket_connect_timeout=5)
    try:
        async with factory() as session, session.begin():
            await _set_rls_org(session, org_id)
            now = datetime.now(UTC)
            result = await session.execute(
                select(ScheduledReport)
                .where(
                    ScheduledReport.id == report_id,
                    ScheduledReport.organisation_id == org_id,
                    ScheduledReport.active.is_(True),
                )
                .with_for_update()
            )
            report = result.scalar_one_or_none()
            if report is None:
                return {"status": "skipped", "reason": "report_inactive_or_missing"}

            generator = get_generator(report.report_type)
            if generator is None:
                _log.warning("No generator registered for report type %s", report.report_type)
                await _handle_report_failure(session, redis_client, report_id, now)
                return {"status": "failed", "reason": f"no_generator_for_{report.report_type}"}

            try:
                config = report.config_json or {}
                report_data = await generator(session, org_id, config)
                formatter = get_formatter(report.report_type)
                payload: Any = report_data
                if formatter is not None:
                    payload = formatter(report_data)
                deliverer = get_deliverer(report.report_type)
                recipient_config = report.recipient_config or {}
                if deliverer is not None:
                    delivery_results = await deliverer(payload, recipient_config)
                else:
                    delivery_results = await _deliver_via_config(payload, recipient_config)
            except asyncio.CancelledError:
                raise
            except Exception:
                _log.exception("Report %s (%s) generation or delivery failed", report_id, report.report_type)
                await _handle_report_failure(session, redis_client, report_id, now)
                return {"status": "failed", "reason": "generation_or_delivery_failed"}

            schedule_type = (report.config_json or {}).get("schedule_type")
            values: dict[str, Any] = {"last_sent_at": now}
            if schedule_type == "one_time":
                values["active"] = False
                values["next_send_at"] = None
            # next_send_at was already advanced to the next cron match at enqueue time.

            await session.execute(update(ScheduledReport).where(ScheduledReport.id == report_id).values(**values))
            await _clear_report_failure_counter(redis_client, report_id)
            _log.info("Report %s (%s) sent", report_id, report.report_type)
            return {
                "status": "sent",
                "report_id": str(report_id),
                "report_type": report.report_type,
                "delivery_results": delivery_results,
            }
    finally:
        with _suppress_aclose():
            await redis_client.aclose()

    return {"status": "error", "reason": "unexpected"}


async def _handle_report_failure(
    session: AsyncSession,
    redis_client: AsyncRedis,
    report_id: uuid.UUID,
    now: datetime,
) -> None:
    """Back off next_send_at +5min; deactivate after 5 consecutive failures."""
    from sqlalchemy import update

    from modulo.db.models.scheduled_report import ScheduledReport

    backoff = now + timedelta(seconds=REPORT_BACKOFF_SECONDS)
    await session.execute(update(ScheduledReport).where(ScheduledReport.id == report_id).values(next_send_at=backoff))
    try:
        key = _report_failure_counter_key(report_id)
        count = await redis_client.incr(key)
        await redis_client.expire(key, _REPORT_FAILURE_COUNTER_TTL)
        if count >= REPORT_MAX_CONSECUTIVE_FAILURES:
            await session.execute(update(ScheduledReport).where(ScheduledReport.id == report_id).values(active=False))
            _log.warning(
                "Report %s deactivated after %d consecutive failures",
                report_id,
                REPORT_MAX_CONSECUTIVE_FAILURES,
            )
    except asyncio.CancelledError:
        raise
    except Exception:
        _log.warning("cron_helpers.report_failure_counter_unavailable report=%s", report_id)
        # Best-effort counter — the next_send_at backoff alone already stops the
        # every-30s re-enqueue loop.


def _report_failure_counter_key(report_id: uuid.UUID) -> str:
    return f"saq:report:consecutive_failures:{report_id}"


async def _clear_report_failure_counter(redis_client: AsyncRedis, report_id: uuid.UUID) -> None:
    try:
        await redis_client.delete(_report_failure_counter_key(report_id))
    except asyncio.CancelledError:
        raise
    except Exception:
        _log.warning("cron_helpers.clear_report_failure_counter failed for %s", report_id)


def _suppress_aclose() -> Any:
    from contextlib import suppress

    return suppress(Exception)


# ---------------------------------------------------------------------------
# fire_due_triggers (system cron) — multi-machine-safe enqueue
# ---------------------------------------------------------------------------


async def _enqueue_fire_job_async(
    q: RedisQueue,
    function: str,
    key: str,
    **kwargs: Any,
) -> str | None:
    """Enqueue a per-item fire job with bounded knobs and a per-epoch dedupe key.

    Returns the job id, or ``None`` when SAQ deduped it (a concurrent machine
    already enqueued the same epoch — the atomic next_fire_at advance makes this
    the exceptional path).
    """
    job = await q.enqueue(
        function,
        key=key,
        timeout=FIRE_JOB_TIMEOUT,
        heartbeat=FIRE_JOB_HEARTBEAT,
        retries=FIRE_JOB_RETRIES,
        ttl=FIRE_JOB_TTL,
        **kwargs,
    )
    return job.id if job is not None else None


def _atomic_advance_stmt() -> Any:
    """Conditional next_fire_at advance — the multi-machine safety primitive.

    Only rows whose ``next_fire_at`` is still due (<= now, or never set) are
    advanced and RETURNED. A second machine's concurrent tick blocks on the row
    lock, re-evaluates the WHERE after the first commits, and returns nothing.
    """
    return text(
        "UPDATE triggers SET next_fire_at = :nf "
        "WHERE id = :tid "
        "  AND trigger_type = :ttype "
        "  AND active "
        "  AND (next_fire_at IS NULL OR next_fire_at <= now()) "
        "RETURNING id"
    )


async def _advance_cron_next_fire(
    session: AsyncSession,
    trigger_id: uuid.UUID,
    cron_expression: str,
    cron_timezone: str | None = None,
) -> bool:
    """Atomically advance a cron trigger's ``next_fire_at`` (multi-machine).

    The next fire is computed in the trigger's configured timezone
    (``cron_timezone``), matching the legacy ``CronFireTask`` behaviour; a
    non-UTC trigger must not fire on UTC schedules.
    """
    nf = compute_next_fire(cron_expression, after=datetime.now(UTC), timezone=cron_timezone or "UTC")
    r = await session.execute(
        _atomic_advance_stmt(),
        {"nf": nf, "tid": str(trigger_id), "ttype": "cron"},
    )
    return r.fetchone() is not None


async def _advance_polling_next_fire(session: AsyncSession, trigger_id: uuid.UUID, poll_interval: int) -> bool:
    nf = datetime.now(UTC) + timedelta(seconds=max(int(poll_interval), 1))
    r = await session.execute(
        _atomic_advance_stmt(),
        {"nf": nf, "tid": str(trigger_id), "ttype": "polling"},
    )
    return r.fetchone() is not None


async def _advance_report_next_send(session: AsyncSession, report_id: uuid.UUID, cron_expression: str) -> bool:
    ns = compute_next_send(cron_expression, after=datetime.now(UTC))
    r = await session.execute(
        text(
            "UPDATE scheduled_reports SET next_send_at = :ns "
            "WHERE id = :rid AND active "
            "AND (next_send_at IS NULL OR next_send_at <= now()) "
            "RETURNING id"
        ),
        {"ns": ns, "rid": str(report_id)},
    )
    return r.fetchone() is not None


async def fire_due_triggers() -> dict[str, Any]:
    """System cron — read due cron/polling/report rows and enqueue per-item fire jobs.

    Multi-machine safety (plan F1): each due row's ``next_fire_at`` is advanced
    ATOMICALLY (conditional ``UPDATE ... RETURNING id``) and a per-item fire job
    is enqueued ONLY for returned rows. Per-type isolation: an exception in one
    trigger type does not stop the others. Enqueue failures ingest an
    ``error_event`` (source='saq') and rely on next-tick re-fire.

    Runs per-org (RLS-safe): the org context is set per transaction so the
    scheduler sees all orgs under FORCE RLS (integration) and behaves
    identically in production.
    """
    from modulo.db.models.organisation import Organisation
    from modulo.db.models.scheduled_report import ScheduledReport
    from modulo.db.models.trigger import Trigger

    settings = get_settings()
    queue_name = settings.saq_runs_queue
    summary: dict[str, Any] = {
        "orgs_scanned": 0,
        "cron_due": 0,
        "cron_enqueued": 0,
        "polling_due": 0,
        "polling_enqueued": 0,
        "report_due": 0,
        "report_enqueued": 0,
        "enqueue_failures": 0,
    }

    factory = _open_factory()

    # Collect all org ids first (organisations is the root table — no RLS).
    async with factory() as session, session.begin():
        result = await session.execute(select(Organisation.id))
        org_ids: list[uuid.UUID] = list(result.scalars())

    if not org_ids:
        return summary

    redis_client = AsyncRedis.from_url(
        settings.redis_url,
        socket_connect_timeout=10,
        socket_keepalive=True,
        max_connections=settings.saq_redis_pool_size,
    )
    try:
        q = RedisQueue(redis_client, name=queue_name)
        for org_id in org_ids:
            summary["orgs_scanned"] += 1
            async with factory() as session, session.begin():
                await _set_rls_org(session, org_id)
                now = datetime.now(UTC)
                try:
                    # ---- cron triggers ----
                    cron_rows = (
                        await session.execute(
                            select(
                                Trigger.id,
                                Trigger.pipeline_id,
                                Trigger.config_json,
                                Trigger.cron_expression,
                                Trigger.cron_timezone,
                            ).where(
                                Trigger.trigger_type == "cron",
                                Trigger.active.is_(True),
                                Trigger.next_fire_at.isnot(None),
                                Trigger.next_fire_at <= now,
                                Trigger.cron_expression.isnot(None),
                            )
                        )
                    ).all()
                except asyncio.CancelledError:
                    raise
                except Exception:
                    _log.exception("fire_due_triggers: cron read failed (org %s)", org_id)
                    cron_rows = []

                pipelines_needing_snapshots = {
                    row.pipeline_id for row in cron_rows if not (row.config_json or {}).get("snapshot_id")
                }
                latest_snapshots: dict[uuid.UUID, uuid.UUID] = {}
                if pipelines_needing_snapshots:
                    pids = list(pipelines_needing_snapshots)
                    snap_result = await session.execute(
                        text(
                            "SELECT DISTINCT ON (pipeline_id) pipeline_id, id "
                            "FROM pipeline_snapshots "
                            "WHERE pipeline_id = ANY(:pids) "
                            "ORDER BY pipeline_id, created_at DESC"
                        ),
                        {"pids": [str(p) for p in pids]},
                    )
                    latest_snapshots = dict(snap_result)

                for row in cron_rows:
                    summary["cron_due"] += 1
                    try:
                        if not await _advance_cron_next_fire(session, row.id, row.cron_expression, row.cron_timezone):
                            continue  # another machine advanced this epoch
                    except asyncio.CancelledError:
                        raise
                    except Exception:
                        _log.exception("fire_due_triggers: cron advance failed %s", row.id)
                        continue
                    snapshot_id = _resolve_snapshot_id(row, latest_snapshots)
                    try:
                        job_id = await _enqueue_fire_job_async(
                            q,
                            "modulo.core.saq_worker.fire_cron_trigger",
                            f"fire:{row.id}:{int(now.timestamp())}",
                            trigger_id=str(row.id),
                            org_id=str(org_id),
                            pipeline_id=str(row.pipeline_id),
                            cron_expression=row.cron_expression,
                            snapshot_id=str(snapshot_id) if snapshot_id else "",
                        )
                        if job_id is not None:
                            summary["cron_enqueued"] += 1
                    except asyncio.CancelledError:
                        raise
                    except Exception:
                        summary["enqueue_failures"] += 1
                        _log.exception("fire_due_triggers: cron enqueue failed %s", row.id)
                        await _ingest_saq_error(
                            session,
                            org_id,
                            function="fire_due_triggers",
                            message=f"fire_due_triggers: enqueue failed for cron trigger {row.id}",
                            context={"trigger_id": str(row.id), "trigger_type": "cron"},
                        )

                try:
                    # ---- polling triggers ----
                    polling_rows = (
                        await session.execute(
                            select(
                                Trigger.id,
                                Trigger.pipeline_id,
                                Trigger.config_json,
                                Trigger.next_fire_at,
                            ).where(
                                Trigger.trigger_type == "polling",
                                Trigger.active.is_(True),
                                Trigger.next_fire_at.isnot(None),
                                Trigger.next_fire_at <= now,
                            )
                        )
                    ).all()
                except asyncio.CancelledError:
                    raise
                except Exception:
                    _log.exception("fire_due_triggers: polling read failed (org %s)", org_id)
                    polling_rows = []

                for row in polling_rows:
                    config = row.config_json or {}
                    ci_id_str = config.get("connector_instance_id")
                    try:
                        connector_instance_id = uuid.UUID(str(ci_id_str)) if ci_id_str else None
                    except (ValueError, TypeError):
                        connector_instance_id = None
                    interval = max(int(config.get("poll_interval_seconds") or 60), 1)
                    if connector_instance_id is None:
                        # Missing connector instance — log poll_error and advance
                        # (mirrors the legacy beat _fetch_due_triggers behaviour).
                        from modulo.db.models.trigger_event import TriggerEvent

                        summary["polling_due"] += 1
                        try:
                            await session.execute(
                                text(
                                    "UPDATE triggers SET next_fire_at = :nf "
                                    "WHERE id = :tid AND trigger_type = 'polling' AND active "
                                    "AND (next_fire_at IS NULL OR next_fire_at <= now())"
                                ),
                                {"nf": datetime.now(UTC) + timedelta(seconds=interval), "tid": str(row.id)},
                            )
                            session.add(
                                TriggerEvent(
                                    organisation_id=org_id,
                                    trigger_id=row.id,
                                    trigger_type="polling",
                                    raw_payload_hash=hashlib.sha256(
                                        f"polling:{row.id}:poll_error".encode()
                                    ).hexdigest(),
                                    validation_result="poll_error",
                                    error_detail="Polling trigger missing connector_instance_id in config_json",
                                )
                            )
                        except asyncio.CancelledError:
                            raise
                        except Exception:
                            _log.exception("fire_due_triggers: polling missing-connector handling failed %s", row.id)
                        continue

                    summary["polling_due"] += 1
                    try:
                        if not await _advance_polling_next_fire(session, row.id, interval):
                            continue
                    except asyncio.CancelledError:
                        raise
                    except Exception:
                        _log.exception("fire_due_triggers: polling advance failed %s", row.id)
                        continue
                    try:
                        job_id = await _enqueue_fire_job_async(
                            q,
                            "modulo.core.saq_worker.fire_polling_trigger",
                            f"fire:{row.id}:{int(now.timestamp())}",
                            trigger_id=str(row.id),
                            org_id=str(org_id),
                            pipeline_id=str(row.pipeline_id),
                            connector_instance_id=str(connector_instance_id),
                            poll_query=config.get("poll_query", ""),
                            condition_expression=config.get("condition_expression"),
                        )
                        if job_id is not None:
                            summary["polling_enqueued"] += 1
                    except asyncio.CancelledError:
                        raise
                    except Exception:
                        summary["enqueue_failures"] += 1
                        _log.exception("fire_due_triggers: polling enqueue failed %s", row.id)
                        await _ingest_saq_error(
                            session,
                            org_id,
                            function="fire_due_triggers",
                            message=f"fire_due_triggers: enqueue failed for polling trigger {row.id}",
                            context={"trigger_id": str(row.id), "trigger_type": "polling"},
                        )

                try:
                    # ---- scheduled reports ----
                    report_rows = (
                        await session.execute(
                            select(ScheduledReport.id, ScheduledReport.cron_expression).where(
                                ScheduledReport.active.is_(True),
                                ScheduledReport.next_send_at.isnot(None),
                                ScheduledReport.next_send_at <= now,
                            )
                        )
                    ).all()
                except asyncio.CancelledError:
                    raise
                except Exception:
                    _log.exception("fire_due_triggers: report read failed (org %s)", org_id)
                    report_rows = []

                for row in report_rows:
                    summary["report_due"] += 1
                    try:
                        if not await _advance_report_next_send(session, row.id, row.cron_expression):
                            continue
                    except asyncio.CancelledError:
                        raise
                    except Exception:
                        _log.exception("fire_due_triggers: report advance failed %s", row.id)
                        continue
                    try:
                        job_id = await _enqueue_fire_job_async(
                            q,
                            "modulo.core.saq_worker.fire_report_trigger",
                            f"fire:report:{row.id}:{int(now.timestamp())}",
                            report_id=str(row.id),
                            org_id=str(org_id),
                        )
                        if job_id is not None:
                            summary["report_enqueued"] += 1
                    except asyncio.CancelledError:
                        raise
                    except Exception:
                        summary["enqueue_failures"] += 1
                        _log.exception("fire_due_triggers: report enqueue failed %s", row.id)
                        await _ingest_saq_error(
                            session,
                            org_id,
                            function="fire_due_triggers",
                            message=f"fire_due_triggers: enqueue failed for report {row.id}",
                            context={"report_id": str(row.id), "trigger_type": "report"},
                        )
    finally:
        with _suppress_aclose():
            await redis_client.aclose()

    return summary


def _resolve_snapshot_id(row: Any, latest_snapshots: dict[uuid.UUID, uuid.UUID]) -> uuid.UUID | None:
    config = row.config_json or {}
    snapshot_id_str = config.get("snapshot_id")
    if snapshot_id_str:
        try:
            return uuid.UUID(str(snapshot_id_str))
        except (ValueError, TypeError):
            return None
    return latest_snapshots.get(row.pipeline_id)


# ---------------------------------------------------------------------------
# dispatcher_reconcile (system cron) — DB/queue reconciliation (plan F3c)
# ---------------------------------------------------------------------------


def _reconcile_capacity_marker_exclusion() -> Any:
    """Exclude capacity-block reason markers from re-dispatch.

    A capacity-blocked run (demoted to ``pending`` with ``error_code`` in
    (``org_capacity_limited``, ``pipeline_capacity``)) has a LIVE in-process
    retry accelerator (``_retry_pending``). If ``dispatcher_reconcile``
    re-enqueues it, a second worker claims it and spawns a SECOND
    ``_retry_pending`` loop — two loops can double-execute the same run when
    a slot frees. These runs are therefore NEVER re-dispatched here; the
    120-min ``capacity_timeout`` sweep is their backstop. Literal markers,
    matching the stale-run sweep in ``pipeline_execution.py``.
    """
    from sqlalchemy import or_

    from modulo.db.models.run import Run

    return or_(
        Run.error_code.is_(None),
        Run.error_code.not_in(("org_capacity_limited", "pipeline_capacity")),
    )


async def dispatcher_reconcile() -> dict[str, Any]:
    """System cron — re-dispatch runs whose SAQ job is missing (every 60s).

    Predicate (plan F3c): status IN ('pending','running') AND
    ``queue.job(run:{id})`` IS None AND staleness:

      * pending + dispatched_at IS NULL: capacity-deferred — matched on the
        run's CREATION path (SAQ mode only), NOT ``dispatcher='saq'``, because
        ``dispatch_run`` returns deferred BEFORE recording dispatched_at/
        dispatcher. NO staleness gate (re-dispatch when capacity frees).
      * pending + dispatched_at set: ``dispatcher='saq'``, stale by the
        re-enqueue window.
      * running: ``dispatcher='saq'``, heartbeat stale by 2*SAQ_JOB_HEARTBEAT.

    ``awaiting_human``/``claimed`` are NEVER re-dispatched (F6a review): a
    waiting run's ``execute_run`` job COMPLETED normally at the gate (its TTL
    expiry + stale heartbeat are the NORMAL waiting state), and the HITL
    approve/reject endpoints dispatch ``resume_run`` themselves when a human
    acts. Re-dispatching here would resume with an empty decision and silently
    auto-approve the gate (executor.aupdate_state({"_hitl_decision": {}})).

    On match: verify the Redis read, then PARTIAL-EVICTION repair — DEL the
    abort key, ZREM the incomplete zset, LREM queued/active (all keys derived
    from the configured queue name), then a normal ``queue.enqueue()``. The
    enqueue return is gated: a still-deduped result logs + alerts, never loops.

    Re-dispatch type: pending/running -> ``execute_run``. Capacity-deferred
    runs are re-dispatched only when their pipeline has free capacity.
    """
    from sqlalchemy import and_, or_

    from modulo.db.models.organisation import Organisation
    from modulo.db.models.run import Run

    settings = get_settings()
    queue_name = settings.saq_runs_queue
    reenqueue_window = int(settings.saq_reenqueue_window)
    stale_window = RECONCILE_STALE_HEARTBEAT_FACTOR * int(settings.saq_job_heartbeat)
    factory = _open_factory()
    summary: dict[str, Any] = {"scanned": 0, "repaired": 0, "skipped": 0, "redis_errors": 0, "deduped": 0}

    async with factory() as session, session.begin():
        result = await session.execute(select(Organisation.id))
        org_ids: list[uuid.UUID] = list(result.scalars())

    if not org_ids:
        return summary

    redis_client = AsyncRedis.from_url(
        settings.redis_url,
        socket_connect_timeout=10,
        socket_keepalive=True,
        max_connections=settings.saq_redis_pool_size,
    )
    try:
        q = RedisQueue(redis_client, name=queue_name)
        # Capacity-deferred branch: pending + never dispatched. dispatch_run
        # returns deferred BEFORE recording dispatched_at/dispatcher, so these
        # rows carry dispatcher NULL and must be matched on their creation path
        # (F3c). Post-cutover every dispatch goes through dispatch_run's
        # capacity gate, so the branch is always active.
        capacity_deferred = and_(
            Run.status == "pending",
            Run.dispatched_at.is_(None),
        )
        re_dispatch_predicate = or_(
            capacity_deferred,
            and_(
                Run.status == "pending",
                Run.dispatcher == "saq",
                Run.dispatched_at < func_now_minus(reenqueue_window),
            ),
            and_(
                Run.status == "running",
                Run.dispatcher == "saq",
                Run.heartbeat_at < func_now_minus(stale_window),
            ),
        )
        for org_id in org_ids:
            async with factory() as session, session.begin():
                await _set_rls_org(session, org_id)
                try:
                    rows = (
                        await session.execute(
                            select(
                                Run.id,
                                Run.pipeline_id,
                                Run.status,
                                Run.dispatched_at,
                                Run.heartbeat_at,
                            ).where(
                                Run.organisation_id == org_id,
                                Run.status.in_(("pending", "running")),
                                re_dispatch_predicate,
                                _reconcile_capacity_marker_exclusion(),
                            )
                        )
                    ).all()
                except asyncio.CancelledError:
                    raise
                except Exception:
                    _log.exception("dispatcher_reconcile: read failed (org %s)", org_id)
                    continue

                for row in rows:
                    summary["scanned"] += 1
                    if row.status in ("awaiting_human", "claimed"):
                        # Defense-in-depth — the predicate already excludes these
                        # (F6a): never re-dispatch a run waiting on a human.
                        summary["skipped"] += 1
                        continue
                    job_key = f"run:{row.id}"
                    job_id = q.job_id(job_key)
                    try:
                        job = await q.job(job_key)
                    except asyncio.CancelledError:
                        raise
                    except Exception:
                        summary["redis_errors"] += 1
                        _log.exception("dispatcher_reconcile: Redis read failed for run %s", row.id)
                        # Fail-safe: NEVER act on an unreadable Redis.
                        await _ingest_saq_error(
                            session,
                            org_id,
                            function="dispatcher_reconcile",
                            message=f"dispatcher_reconcile: Redis read failed for run {row.id}",
                            context={"run_id": str(row.id)},
                        )
                        continue

                    if job is not None:
                        summary["skipped"] += 1
                        continue  # job still exists — nothing to repair

                    # Capacity check for capacity-deferred runs (pending + no
                    # dispatched_at). Re-dispatch only when the pipeline has free
                    # capacity (plan F3b/F3c).
                    if row.status == "pending" and row.dispatched_at is None:
                        from modulo.db.crud.run import count_active_runs_for_pipeline
                        from modulo.db.models.pipeline import Pipeline

                        pipeline = await session.get(Pipeline, row.pipeline_id)
                        max_concurrent = pipeline.max_concurrent_runs if pipeline is not None else 0
                        if max_concurrent > 0:
                            active = await count_active_runs_for_pipeline(
                                session, row.pipeline_id, include_pending=False, exclude_run_id=row.id
                            )
                            if active >= max_concurrent:
                                summary["skipped"] += 1
                                continue

                    # Partial-eviction repair — all keys derived from the queue name.
                    try:
                        await redis_client.delete(f"saq:abort:{job_key}")
                        await redis_client.zrem(f"saq:{queue_name}:incomplete", job_id)
                        await redis_client.lrem(f"saq:{queue_name}:queued", 0, job_id)
                        await redis_client.lrem(f"saq:{queue_name}:active", 0, job_id)
                    except asyncio.CancelledError:
                        raise
                    except Exception:
                        summary["redis_errors"] += 1
                        _log.exception("dispatcher_reconcile: partial-eviction failed for run %s", row.id)
                        await _ingest_saq_error(
                            session,
                            org_id,
                            function="dispatcher_reconcile",
                            message=f"dispatcher_reconcile: partial-eviction failed for run {row.id}",
                            context={"run_id": str(row.id)},
                        )
                        continue

                    # Only pending/running rows reach here (awaiting_human/claimed
                    # are excluded by the predicate) — both re-dispatch as
                    # execute_run (F6a).
                    job_type = "execute_run"
                    try:
                        outcome, new_job_id = await _re_enqueue_run(q.name, str(row.id), str(org_id), job_type)
                        if outcome == "enqueued":
                            summary["repaired"] += 1
                            _log.info(
                                "dispatcher_reconcile: re-dispatched run %s as %s (%s)",
                                row.id,
                                job_type,
                                new_job_id,
                            )
                        else:
                            summary["deduped"] += 1
                            _log.warning("dispatcher_reconcile: re-enqueue still deduped for run %s", row.id)
                            await _ingest_saq_error(
                                session,
                                org_id,
                                function="dispatcher_reconcile",
                                message=f"dispatcher_reconcile: re-enqueue still deduped for run {row.id}",
                                context={"run_id": str(row.id), "job_type": job_type},
                            )
                    except asyncio.CancelledError:
                        raise
                    except Exception:
                        summary["redis_errors"] += 1
                        _log.exception("dispatcher_reconcile: re-enqueue failed for run %s", row.id)
                        await _ingest_saq_error(
                            session,
                            org_id,
                            function="dispatcher_reconcile",
                            message=f"dispatcher_reconcile: re-enqueue failed for run {row.id}",
                            context={"run_id": str(row.id), "job_type": job_type},
                        )
    finally:
        with _suppress_aclose():
            await redis_client.aclose()

    return summary


async def _re_enqueue_run(
    queue_name: str,
    run_id_str: str,
    org_id_str: str,
    job_type: str,
) -> tuple[str, str | None]:
    """Normal dispatch after partial-eviction; gate on the return value.

    ``dispatch_run`` is the single gating point (F3e): it capacity-checks,
    writes ``dispatched_at``, enqueues with the deterministic ``run:{id}`` key,
    and records ``dispatcher='saq'`` + fresh claim token. A still-deduped result
    (Lua dedupe not cleared) logs + alerts — it does NOT loop.
    """
    from modulo.core.dispatch import dispatch_run

    return await dispatch_run(run_id_str, org_id_str, queue=queue_name, job_type=job_type)


def func_now_minus(seconds: int) -> Any:
    """SQLAlchemy expression ``now() - interval`` for staleness predicates."""
    from sqlalchemy import text as _text

    return _text(f"now() - interval '{seconds} seconds'")
