"""Celery beat scheduler for scheduled reports.

Architecture
------------
*DatabaseReportScheduler* is a custom Celery beat ``Scheduler`` that queries
the ``scheduled_reports`` table for rows where ``active = true`` and
``next_send_at <= now()``.

On each tick it creates a schedule entry per matching report, firing
a ``ReportFireTask`` that:
  1. Re-reads the report row (with ``FOR UPDATE`` to serialise)
  2. Looks up the registered report generator for ``report_type``
  3. Calls the generator to produce report data
  4. Formats and delivers the report
  5. Updates ``last_sent_at`` and ``next_send_at``

Report generators are registered via ``register_report_type()``.
"""

from __future__ import annotations

import datetime
import logging
import uuid
from collections.abc import Awaitable, Callable
from typing import Any

import httpx
from croniter import croniter  # type: ignore[import-untyped]
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine

from modulo.db.models.scheduled_report import ScheduledReport
from modulo.settings import get_settings

try:
    from celery import Celery, Task  # type: ignore[import-untyped]
    from celery.beat import ScheduleEntry, Scheduler  # type: ignore[import-untyped]
except ImportError:
    import typing

    if typing.TYPE_CHECKING:
        from celery import Celery, Task  # type: ignore[import-untyped]
        from celery.beat import ScheduleEntry, Scheduler  # type: ignore[import-untyped]
    Celery = None  # type: ignore[misc]
    Task = object  # type: ignore[misc]
    ScheduleEntry = object  # type: ignore[misc]
    Scheduler = object  # type: ignore[misc]

_log = logging.getLogger(__name__)

_ENGINE: AsyncEngine | None = None

# ---------------------------------------------------------------------------
# Type aliases
# ---------------------------------------------------------------------------

ReportData = dict[str, Any]
"""Returned by a report generator."""

ReportGenerator = Callable[[AsyncSession, uuid.UUID, dict[str, Any]], Awaitable[ReportData]]
"""(session, org_id, config) -> report_data"""

ReportFormatter = Callable[[ReportData], Any]
"""Takes report_data, returns a deliverable payload."""

ReportDeliverer = Callable[[Any, dict[str, Any]], Awaitable[list[dict[str, Any]]]]
"""(payload, recipient_config) -> list[delivery_result]"""

# ---------------------------------------------------------------------------
# Report-type registry
# ---------------------------------------------------------------------------

_generators: dict[str, ReportGenerator] = {}
_formatters: dict[str, ReportFormatter] = {}
_deliverers: dict[str, ReportDeliverer] = {}


def register_report_type(
    report_type: str,
    generator: ReportGenerator,
    formatter: ReportFormatter | None = None,
    deliverer: ReportDeliverer | None = None,
) -> None:
    """Register a report generator (and optional formatter/deliverer) by type."""
    _generators[report_type] = generator
    if formatter is not None:
        _formatters[report_type] = formatter
    if deliverer is not None:
        _deliverers[report_type] = deliverer


def get_generator(report_type: str) -> ReportGenerator | None:
    return _generators.get(report_type)


def get_formatter(report_type: str) -> ReportFormatter | None:
    return _formatters.get(report_type)


def get_deliverer(report_type: str) -> ReportDeliverer | None:
    return _deliverers.get(report_type)


# ---------------------------------------------------------------------------
# Engine singleton
# ---------------------------------------------------------------------------


def _get_engine() -> AsyncEngine:
    global _ENGINE
    if _ENGINE is None:
        _ENGINE = create_async_engine(get_settings().database_url)
    return _ENGINE


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def compute_next_send(cron_expression: str, after: datetime.datetime | None = None) -> datetime.datetime:
    """Compute the next send time for a cron expression.

    If *after* is None, uses the current UTC time.
    """
    base = after or datetime.datetime.now(datetime.UTC)
    cron = croniter(cron_expression, base)
    next_dt = cron.get_next(datetime.datetime)
    if not isinstance(next_dt, datetime.datetime):
        msg = f"croniter returned unexpected type: {type(next_dt)}"
        raise TypeError(msg)
    return next_dt


async def _set_rls_org(session: AsyncSession, org_id: uuid.UUID) -> None:
    from sqlalchemy import text

    await session.execute(
        text("SELECT set_config('app.organisation_id', :val, true)"),
        {"val": str(org_id)},
    )


# ---------------------------------------------------------------------------
# Celery task — fire one scheduled report
# ---------------------------------------------------------------------------

celery_app_global: Any = None


def get_celery_app() -> Any:
    global celery_app_global
    if celery_app_global is None:
        from modulo.celery_app import get_celery_app as _get_celery_app

        celery_app_global = _get_celery_app()
    return celery_app_global


class ReportFireTask(Task):
    """Task that fires a single scheduled report — generates and delivers."""

    name = "modulo.reports.fire_report"
    autoretry_for = (Exception,)
    max_retries = 3
    default_retry_delay = 60

    def run(self, report_id: str, org_id: str) -> dict[str, Any]:
        import asyncio

        return asyncio.run(
            _fire_scheduled_report(
                report_id=uuid.UUID(report_id),
                org_id=uuid.UUID(org_id),
            )
        )


async def _fire_scheduled_report(
    *,
    report_id: uuid.UUID,
    org_id: uuid.UUID,
) -> dict[str, Any]:
    """Core fire logic — runs inside asyncio.run() inside the Celery task."""
    engine = _get_engine()
    factory = async_sessionmaker(engine, expire_on_commit=False)

    async with factory() as session:
        async with session.begin():
            await _set_rls_org(session, org_id)

            result = await session.execute(
                select(ScheduledReport)
                .where(
                    ScheduledReport.id == report_id,
                    ScheduledReport.organisation_id == org_id,
                )
                .with_for_update()
            )
            report = result.scalar_one_or_none()
            if report is None or not report.active:
                return {"status": "skipped", "reason": "report_inactive_or_missing"}

            generator = get_generator(report.report_type)
            if generator is None:
                _log.warning("No generator registered for report type %s", report.report_type)
                return {"status": "failed", "reason": f"no_generator_for_{report.report_type}"}

            config = report.config_json or {}
            report_data = await generator(session, org_id, config)

            formatter = get_formatter(report.report_type)
            payload: Any = report_data
            if formatter is not None:
                payload = formatter(report_data)

            deliverer = get_deliverer(report.report_type)
            recipient_config = report.recipient_config or {}
            delivery_results: list[dict[str, Any]] = []
            if deliverer is not None:
                delivery_results = await deliverer(payload, recipient_config)
            else:
                delivery_results = await _deliver_via_config(payload, recipient_config, org_id)

            now = datetime.datetime.now(datetime.UTC)
            next_send = compute_next_send(report.cron_expression, after=now)
            await session.execute(
                update(ScheduledReport)
                .where(ScheduledReport.id == report_id)
                .values(last_sent_at=now, next_send_at=next_send)
            )

            _log.info(
                "Report %s (%s) sent. Next send: %s",
                report_id,
                report.report_type,
                next_send.isoformat(),
            )

            return {
                "status": "sent",
                "report_id": str(report_id),
                "report_type": report.report_type,
                "next_send_at": next_send.isoformat(),
                "delivery_results": delivery_results,
            }


# ---------------------------------------------------------------------------
# Generic delivery
# ---------------------------------------------------------------------------


async def _deliver_via_config(
    payload: Any,
    recipient_config: dict[str, Any],
    org_id: uuid.UUID,
) -> list[dict[str, Any]]:
    """Deliver a report payload based on recipient_config type."""
    config_type = recipient_config.get("type", "webhook")

    if config_type == "slack_webhook":
        urls = recipient_config.get("webhook_urls", [])
        return await _deliver_slack_webhook(payload, urls)

    return await _deliver_webhook(payload, recipient_config)


async def _deliver_slack_webhook(payload: Any, webhook_urls: list[str]) -> list[dict[str, Any]]:
    """POST payload as Slack blocks to each webhook URL."""
    results: list[dict[str, Any]] = []
    async with httpx.AsyncClient(timeout=30.0) as client:
        for url in webhook_urls:
            try:
                body = payload if isinstance(payload, dict) else {"text": str(payload)}
                resp = await client.post(
                    url,
                    json=body,
                    headers={"Content-Type": "application/json"},
                )
                results.append(
                    {
                        "url": url,
                        "status": "delivered" if resp.is_success else "failed",
                        "status_code": resp.status_code,
                        "error": None if resp.is_success else resp.text[:200],
                    }
                )
            except httpx.RequestError as exc:
                results.append(
                    {
                        "url": url,
                        "status": "failed",
                        "status_code": None,
                        "error": str(exc),
                    }
                )
    return results


async def _deliver_webhook(payload: Any, recipient_config: dict[str, Any]) -> list[dict[str, Any]]:
    """POST payload as JSON to configured webhook URLs."""
    urls = recipient_config.get("urls", [])
    headers = recipient_config.get("headers", {})
    results: list[dict[str, Any]] = []
    async with httpx.AsyncClient(timeout=30.0) as client:
        for url in urls:
            try:
                resp = await client.post(
                    url,
                    json=payload if isinstance(payload, (dict, list)) else {"data": str(payload)},
                    headers={"Content-Type": "application/json", **headers},
                )
                results.append(
                    {
                        "url": url,
                        "status": "delivered" if resp.is_success else "failed",
                        "status_code": resp.status_code,
                        "error": None if resp.is_success else resp.text[:200],
                    }
                )
            except httpx.RequestError as exc:
                results.append(
                    {
                        "url": url,
                        "status": "failed",
                        "status_code": None,
                        "error": str(exc),
                    }
                )
    return results


# ---------------------------------------------------------------------------
# Database-backed beat scheduler
# ---------------------------------------------------------------------------


class DatabaseReportEntry(ScheduleEntry):
    """A single schedule entry representing one scheduled report row."""

    def __init__(
        self,
        report_id: uuid.UUID,
        org_id: uuid.UUID,
        cron_expression: str,
        next_send_at: datetime.datetime,
    ) -> None:
        self._report_id = report_id
        self._org_id = org_id
        self._cron_expression = cron_expression
        self._next_send_at = next_send_at

    @property
    def name(self) -> str:
        return f"report-{self._report_id}"

    @property
    def task(self) -> str:
        return ReportFireTask.name

    @property
    def schedule(self) -> Any:
        return self

    @property
    def args(self) -> list[str]:
        return [str(self._report_id), str(self._org_id)]

    @property
    def kwargs(self) -> dict[str, Any]:
        return {}

    @property
    def options(self) -> dict[str, Any]:
        return {"task_id": f"report-{self._report_id}-{self._next_send_at.timestamp():.0f}"}

    def is_due(self) -> tuple[bool, datetime.timedelta]:
        now = datetime.datetime.now(datetime.UTC)
        if self._next_send_at <= now:
            return (True, datetime.timedelta(seconds=0))
        delay = (self._next_send_at - now).total_seconds()
        return (False, datetime.timedelta(seconds=max(delay, 0)))

    def __repr__(self) -> str:
        return f"<DatabaseReportEntry report={self._report_id} next={self._next_send_at.isoformat()}>"


class DatabaseReportScheduler(Scheduler):
    """Celery beat scheduler that reads scheduled reports from the database.

    On each tick (default every 60 s via ``max_interval``), the scheduler
    queries the ``scheduled_reports`` table for active rows whose
    ``next_send_at <= now()`` and creates one ``DatabaseReportEntry`` per match.
    """

    Entry = DatabaseReportEntry

    def __init__(self, app: Celery, **kwargs: Any) -> None:
        super().__init__(app, **kwargs)
        self._schedule: dict[str, DatabaseReportEntry] = {}

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

        rows = asyncio.run(self._fetch_due_reports())

        current_ids = set(self._schedule.keys())
        db_ids: set[str] = set()

        for row in rows:
            entry_id = f"report-{row['report_id']}"
            db_ids.add(entry_id)

            if entry_id in self._schedule:
                existing = self._schedule[entry_id]
                if existing._next_send_at == row["next_send_at"]:
                    continue

            entry = DatabaseReportEntry(
                report_id=row["report_id"],
                org_id=row["org_id"],
                cron_expression=row["cron_expression"],
                next_send_at=row["next_send_at"],
            )
            self._schedule[entry_id] = entry

        stale = current_ids - db_ids
        for sid in stale:
            self._schedule.pop(sid, None)

    async def _fetch_due_reports(self) -> list[dict[str, Any]]:
        """Async query for scheduled reports due to fire."""
        try:
            factory = async_sessionmaker(_get_engine(), expire_on_commit=False)

            async with factory() as session:
                now = datetime.datetime.now(datetime.UTC)
                result = await session.execute(
                    select(
                        ScheduledReport.id,
                        ScheduledReport.organisation_id,
                        ScheduledReport.cron_expression,
                        ScheduledReport.next_send_at,
                    ).where(
                        ScheduledReport.active == True,  # noqa: E712
                        ScheduledReport.next_send_at <= now,
                    )
                )
                rows = result.all()

                reports: list[dict[str, Any]] = []
                for row in rows:
                    reports.append(
                        {
                            "report_id": row.id,
                            "org_id": row.organisation_id,
                            "cron_expression": row.cron_expression,
                            "next_send_at": row.next_send_at,
                        }
                    )
                return reports
        except Exception:
            _log.exception("Failed to fetch scheduled reports from database")
            return []

    @property
    def max_interval(self) -> int:
        """Maximum sleep between ticks — 60 seconds."""
        return 60
