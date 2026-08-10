"""Analytics facts maintenance — backfill, reconcile, retention (ADR 020).

ONE plain ``modulo_app`` cron (like ``retention_cleanup``): sessions WITHOUT
``set_rls_org`` — ``modulo_app`` is BYPASSRLS, so cross-org scans work,
matching every existing system cron. Non-Postgres backends no-op (the
INSERT...SELECT anti-join and ``ON CONFLICT`` are Postgres-idiomatic).

Roles:

- ``backfill_facts``  — per-day INSERT...SELECT anti-join (idempotent).
- ``backfill_batches`` — Python-driven day loop, ONE transaction per day,
  hard-capped batches per invocation (remainder re-enqueued next run).
- ``reconcile_facts`` — day-aggregate cross-check vs the org daily ledger
  (org-level rows only), direction-aware, auto-repair within source
  availability, cooldown-keyed alerts beyond it.
- ``retention_facts`` — chunked day-slice DELETE older than the retention
  window (config-driven, default 13 months).
"""

from __future__ import annotations

import asyncio
import calendar
import logging
import time
import uuid
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Any

import sqlalchemy as sa
from sqlalchemy import func
from sqlalchemy.dialects.postgresql import insert as pg_insert

from modulo.core.analytics.metrics import (
    record_facts_skip_non_pg,
    record_reconcile_alert,
    set_backfill_last_run_ts,
    set_backfill_rows,
    set_retention_lag,
)
from modulo.db.models.daily_run_count import OrgDailyRunCount
from modulo.db.models.pipeline import Pipeline
from modulo.db.models.pipeline_snapshot import PipelineSnapshot
from modulo.db.models.run import TERMINAL_STATUSES, Run
from modulo.db.models.run_daily_facts import RunDailyFact
from modulo.db.models.team import Team
from modulo.settings import get_settings

_log = logging.getLogger(__name__)

__all__ = [
    "backfill_facts",
    "reconcile_facts",
    "retention_facts",
    "run_maintenance",
]

# Default facts retention window (config-driven via
# ``analytics_facts_retention_months`` when set; 13 months keeps one full year
# of YoY-capable history plus a margin month).
_FACTS_RETENTION_MONTHS = 13
# Hard cap on backfill day-batches per invocation — the remainder re-enqueues
# (idempotent anti-join makes the loop naturally resumable).
_BACKFILL_MAX_BATCHES = 30
# Reconcile lookback — never compare deeper than the run-retention floor
# (beyond that, the source runs may be purged and a drift is irrecoverable).
_RECONCILE_LOOKBACK_DAYS = 90
# Runs retention (purge window) — matches ``batch_delete_old_terminal_runs``.
_RUN_RETENTION_DAYS = 90

# Cooldown for reconcile alerts keyed (org_id, drift_type) — a repeating
# irrecoverable drift must alert, but not on every daily invocation.
_RECONCILE_ALERT_COOLDOWN_SECONDS = 6 * 3600
_reconcile_cooldown: dict[tuple[str, str], float] = {}


def _subtract_months(day: date, months: int) -> date:
    """Subtract whole months, clamping the day-of-month (stdlib only)."""
    month_index = day.year * 12 + (day.month - 1) - months
    year, zero_based_month = divmod(month_index, 12)
    month = zero_based_month + 1
    last_day = int(calendar.monthrange(year, month)[1])
    return date(year, month, min(day.day, last_day))


async def _dialect_name(session: Any) -> str:
    conn = await session.connection()
    return str(conn.dialect.name)


def _reconcile_cooldown_allows(org_id: uuid.UUID, drift_type: str) -> bool:
    key = (str(org_id), drift_type)
    now = time.monotonic()
    _reconcile_cooldown_prune(now)
    last = _reconcile_cooldown.get(key)
    if last is not None and now - last < _RECONCILE_ALERT_COOLDOWN_SECONDS:
        return False
    _reconcile_cooldown[key] = now
    return True


def _reconcile_cooldown_prune(now: float) -> None:
    """Best-effort eviction of reconcile-alert cooldown entries.

    The dict is bounded by org count but unbounded over time — an entry whose
    timestamp has fully aged out of the cooldown window can never suppress a
    future alert, so drop it on every check/set to keep the map bounded.
    """
    for key, last in list(_reconcile_cooldown.items()):
        if now - last >= _RECONCILE_ALERT_COOLDOWN_SECONDS:
            del _reconcile_cooldown[key]


async def backfill_facts(session: Any, day: date) -> int:
    """Per-day INSERT...SELECT anti-join backfill for terminal runs on *day*.

    Idempotent — ``ON CONFLICT (run_id) DO NOTHING`` (the unique
    ``uq_run_daily_facts_run_id`` index is the conflict target). The UTC
    ``run_date`` expression matches the live writer
    (``started_at/created_at AT TIME ZONE 'UTC'``).
    """
    run_date_expr = sa.cast(
        sa.func.timezone("UTC", sa.func.coalesce(Run.started_at, Run.created_at)),
        sa.Date,
    )
    duration_ms_expr = sa.case(
        (
            sa.and_(Run.completed_at.is_not(None), Run.started_at.is_not(None)),
            sa.cast(sa.func.extract("epoch", Run.completed_at - Run.started_at) * 1000, sa.BigInteger),
        ),
        else_=None,
    )
    queue_wait_ms_expr = sa.case(
        (
            sa.and_(Run.dispatched_at.is_not(None), Run.started_at.is_not(None)),
            sa.cast(sa.func.extract("epoch", Run.started_at - Run.dispatched_at) * 1000, sa.BigInteger),
        ),
        else_=None,
    )
    final_idle_ms_expr = sa.case(
        (
            sa.and_(Run.completed_at.is_not(None), Run.heartbeat_at.is_not(None)),
            sa.cast(sa.func.extract("epoch", Run.completed_at - Run.heartbeat_at) * 1000, sa.BigInteger),
        ),
        else_=None,
    )
    # Graph-derived fields from the snapshot's ``graph_json`` (the serialised
    # pipeline graph: a dict with a ``nodes`` list). ``graph_json`` is the
    # native Postgres ``json`` type, so the ``json_*`` functions apply; all
    # three degrade to defaults when the graph is malformed/absent — backfilled
    # rows must NEVER carry NULL here (NULL facts on backfilled rows are a bug).
    graph_nodes_json = PipelineSnapshot.graph_json.op("->")("nodes")
    node_count_expr = sa.case(
        (graph_nodes_json.is_not(None), sa.func.coalesce(sa.func.json_array_length(graph_nodes_json), 0)),
        else_=0,
    )
    _node_arr = sa.func.json_array_elements(graph_nodes_json).table_valued("value")
    _node_value = _node_arr.c.value
    sandbox_count_subq = (
        sa.select(sa.func.count())
        .select_from(_node_arr)
        .where(_node_value.op("->>")("node_type") == "sandbox_agent")
        .scalar_subquery()
    )
    sandbox_agent_node_count_expr = sa.func.coalesce(sandbox_count_subq, 0)
    timeout_subq = (
        sa.select(sa.func.max(sa.cast(_node_value.op("->>")("timeout_seconds"), sa.Integer)))
        .select_from(_node_arr)
        .where(_node_value.op("->>")("timeout_seconds").is_not(None))
        .scalar_subquery()
    )
    max_node_timeout_seconds_expr = timeout_subq

    select_stmt = (
        sa.select(
            # The surrogate PK must be unique PER ROW — the ORM's Python-side
            # uuid.uuid4 default would be rendered once for the whole
            # INSERT...SELECT (duplicate PKs), so generate per-row ids.
            sa.func.gen_random_uuid().label("id"),
            Run.id.label("run_id"),
            Run.organisation_id,
            run_date_expr.label("run_date"),
            Run.created_at.label("created_at"),
            Run.owner_team_id.label("team_id"),
            Team.name.label("team_name"),
            Run.pipeline_id,
            Pipeline.name.label("pipeline_name"),
            Pipeline.folder_id.label("folder_id"),
            Run.trigger_type,
            Run.status,
            Run.total_cost_usd,
            Run.total_tokens,
            duration_ms_expr.label("duration_ms"),
            Run.error_code.label("error_code"),
            Run.claim_count.label("claim_count"),
            queue_wait_ms_expr.label("queue_wait_ms"),
            final_idle_ms_expr.label("final_idle_ms"),
            Run.cancellation_requested.label("cancellation_requested"),
            Run.dispatcher.label("dispatcher"),
            node_count_expr.label("node_count"),
            sandbox_agent_node_count_expr.label("sandbox_agent_node_count"),
            max_node_timeout_seconds_expr.label("max_node_timeout_seconds"),
            Run.parent_run_id.label("parent_run_id"),
            Run.snapshot_id.label("snapshot_id"),
            Run.run_number.label("run_number"),
            sa.func.length(sa.cast(Run.outputs_json, sa.Text)).label("output_bytes"),
            sa.func.length(sa.cast(Run.node_telemetry_json, sa.Text)).label("telemetry_bytes"),
            Run.rate_limit_key.is_not(None).label("rate_limited"),
        )
        .select_from(Run)
        .outerjoin(Team, Team.id == Run.owner_team_id)
        .outerjoin(Pipeline, Pipeline.id == Run.pipeline_id)
        .outerjoin(PipelineSnapshot, PipelineSnapshot.id == Run.snapshot_id)
        .outerjoin(RunDailyFact, RunDailyFact.run_id == Run.id)
        .where(
            Run.status.in_(TERMINAL_STATUSES),
            RunDailyFact.run_id.is_(None),
            sa.func.date_trunc("day", sa.func.coalesce(Run.started_at, Run.created_at)) == day,
        )
    )
    stmt = (
        pg_insert(RunDailyFact)
        .from_select(
            [
                RunDailyFact.id,
                RunDailyFact.run_id,
                RunDailyFact.organisation_id,
                RunDailyFact.run_date,
                RunDailyFact.created_at,
                RunDailyFact.team_id,
                RunDailyFact.team_name,
                RunDailyFact.pipeline_id,
                RunDailyFact.pipeline_name,
                RunDailyFact.folder_id,
                RunDailyFact.trigger_type,
                RunDailyFact.status,
                RunDailyFact.total_cost_usd,
                RunDailyFact.total_tokens,
                RunDailyFact.duration_ms,
                RunDailyFact.error_code,
                RunDailyFact.claim_count,
                RunDailyFact.queue_wait_ms,
                RunDailyFact.final_idle_ms,
                RunDailyFact.cancellation_requested,
                RunDailyFact.dispatcher,
                RunDailyFact.node_count,
                RunDailyFact.sandbox_agent_node_count,
                RunDailyFact.max_node_timeout_seconds,
                RunDailyFact.parent_run_id,
                RunDailyFact.snapshot_id,
                RunDailyFact.run_number,
                RunDailyFact.output_bytes,
                RunDailyFact.telemetry_bytes,
                RunDailyFact.rate_limited,
            ],
            select_stmt,
        )
        .on_conflict_do_nothing(index_elements=[RunDailyFact.run_id])
    )
    result = await session.execute(stmt)
    return result.rowcount or 0


async def backfill_batches(session: Any, *, max_batches: int = _BACKFILL_MAX_BATCHES) -> dict[str, Any]:
    """Backfill day-by-day from the oldest needed day to today.

    ONE transaction per day (``SET LOCAL timezone 'UTC'`` inside each, so
    ``run_date`` matches the live writer). Bounded by ``max_batches`` — the
    remainder is picked up by the next daily invocation (idempotent).
    """
    today = datetime.now(UTC).date()
    oldest = _subtract_months(today, _FACTS_RETENTION_MONTHS)
    rows = 0
    batches = 0
    for offset in range((today - oldest).days + 1):
        if batches >= max_batches:
            break
        day = oldest + timedelta(days=offset)
        async with session.begin():
            await session.execute(sa.text("SELECT set_config('timezone', 'UTC', true)"))
            rows += await backfill_facts(session, day)
        batches += 1
    set_backfill_rows(rows)
    set_backfill_last_run_ts(datetime.now(UTC).timestamp())
    return {"backfill_rows": rows, "backfill_batches": batches}


async def reconcile_facts(session: Any, *, today: date | None = None) -> dict[str, Any]:
    """Day-aggregate cross-check of facts vs the org daily ledger.

    Compares ``SUM(facts.total_cost_usd)`` per (org, run_date) across ALL team
    rows against the LEDGER ORG-LEVEL row only (``team_id IS NULL`` — the
    per-team ledger rows would double-count). Direction-aware:

    - ledger > facts → a gap (the facts writer or backfill missed a terminal);
      within source availability (runs still present) → auto-repair = backfill
      that day; beyond the purge window → alert (structured log + counter +
      cooldown keyed (org, drift_type)).
    - facts > ledger → tolerated (fallback/clamped/refused spend is not in the
      ledger) but logged.

    Only days in ``[today - lookback, today - 1]`` are compared (the
    facts-epoch anchor).
    """
    today = today or datetime.now(UTC).date()
    start = today - timedelta(days=_RECONCILE_LOOKBACK_DAYS)
    alerts = 0
    repaired = 0
    tolerated = 0

    ledger_rows = (
        await session.execute(
            sa.select(
                OrgDailyRunCount.organisation_id,
                OrgDailyRunCount.run_date,
                OrgDailyRunCount.total_spend_usd,
            ).where(
                OrgDailyRunCount.team_id.is_(None),
                OrgDailyRunCount.run_date >= start,
                OrgDailyRunCount.run_date < today,
            )
        )
    ).all()

    for org_id, run_date, ledger_total in ledger_rows:
        ledger_total = ledger_total or Decimal(0)
        facts_total = (
            await session.execute(
                sa.select(func.coalesce(func.sum(RunDailyFact.total_cost_usd), 0)).where(
                    RunDailyFact.organisation_id == org_id,
                    RunDailyFact.run_date == run_date,
                )
            )
        ).scalar_one()
        facts_total = facts_total or Decimal(0)

        if ledger_total > facts_total:
            drift = ledger_total - facts_total
            if run_date >= today - timedelta(days=_RUN_RETENTION_DAYS):
                await backfill_facts(session, run_date)
                repaired += 1
                _log.info(
                    "analytics.facts.reconcile_repaired",
                    extra={"org_id": str(org_id), "run_date": str(run_date), "drift": str(drift)},
                )
            else:
                drift_type = "ledger_exceeds_facts"
                if _reconcile_cooldown_allows(org_id, drift_type):
                    record_reconcile_alert(str(org_id), drift_type)
                    alerts += 1
                    _log.warning(
                        "analytics.facts.reconcile_alert",
                        extra={
                            "org_id": str(org_id),
                            "run_date": str(run_date),
                            "drift": str(drift),
                            "drift_type": drift_type,
                        },
                    )
        elif facts_total > ledger_total:
            tolerated += 1
            _log.info(
                "analytics.facts.reconcile_tolerated",
                extra={
                    "org_id": str(org_id),
                    "run_date": str(run_date),
                    "excess": str(facts_total - ledger_total),
                },
            )

    return {"reconcile_alerts": alerts, "reconcile_repaired": repaired, "reconcile_tolerated": tolerated}


async def retention_facts(session: Any, *, cutoff: date | None = None, chunk_days: int = 7) -> dict[str, Any]:
    """Chunked day-slice DELETE of facts older than the retention window.

    The window is config-driven (``analytics_facts_retention_months``,
    default 13 months). Deletes per-day slices in a loop so each DELETE is a
    bounded statement. Updates the ``modulo_facts_retention_lag`` gauge.
    """
    if cutoff is None:
        months = getattr(get_settings(), "analytics_facts_retention_months", _FACTS_RETENTION_MONTHS)
        try:
            months = int(months)
        except (TypeError, ValueError):
            months = _FACTS_RETENTION_MONTHS
        cutoff = _subtract_months(datetime.now(UTC).date(), months)

    deleted = 0
    while True:
        oldest = (await session.execute(sa.select(func.min(RunDailyFact.run_date)))).scalar_one_or_none()
        if oldest is None or oldest >= cutoff:
            break
        day_end = min(oldest + timedelta(days=chunk_days - 1), cutoff - timedelta(days=1))
        result = await session.execute(
            sa.delete(RunDailyFact).where(
                RunDailyFact.run_date >= oldest,
                RunDailyFact.run_date <= day_end,
            )
        )
        deleted += result.rowcount or 0

    oldest = (await session.execute(sa.select(func.min(RunDailyFact.run_date)))).scalar_one_or_none()
    if oldest is not None:
        set_retention_lag(float((datetime.now(UTC).date() - oldest).days))
    return {"retention_deleted": deleted}


async def run_maintenance(factory: Any) -> dict[str, Any]:
    """Run the daily facts maintenance pass — backfill, reconcile, retention.

    Non-Postgres backends no-op (counter + log) — the anti-join and
    ``ON CONFLICT`` constructs are Postgres-idiomatic and SQLite/MariaDB get
    nothing.
    """
    # ``factory`` is built the way saq_worker does (``autobegin=False``), so the
    # dialect probe via ``session.connection()`` MUST run inside an explicit
    # transaction — a bare probe raises InvalidRequestError before any SQL runs.
    async with factory() as session, session.begin():
        dialect = await _dialect_name(session)
        if dialect != "postgresql":
            record_facts_skip_non_pg()
            _log.warning("analytics.facts.maintenance_skipped_non_pg", extra={"dialect": dialect})
            return {"skipped": True, "reason": "non_postgres"}

    stats: dict[str, Any] = {}
    try:
        stats.update(await backfill_batches(session))
        async with session.begin():
            await session.execute(sa.text("SELECT set_config('timezone', 'UTC', true)"))
            stats.update(await reconcile_facts(session))
        async with session.begin():
            await session.execute(sa.text("SELECT set_config('timezone', 'UTC', true)"))
            stats.update(await retention_facts(session))
    except asyncio.CancelledError:
        raise
    except Exception:
        _log.exception("analytics.facts.maintenance_failed")
        stats["maintenance_failed"] = True
    stats["skipped"] = False
    return stats
