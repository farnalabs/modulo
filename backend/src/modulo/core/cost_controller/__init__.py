"""Cost controller — spend checks, daily run count updates, and limit enforcement.

All functions assume an active transaction with RLS org context set by the caller.

The daily ledger (``org_daily_run_counts``) is a REPORT, not a source of truth
(spec §9.3). ``check_and_record_spend`` is the TERMINAL-ONLY spend recording
path: it checks BOTH the org and team daily limits against the CREATED-AT day
(the same window the enforcement readers use) BEFORE writing either ledger row
(which is keyed by the RUN-START day), and persists the refused amount to the
day rows' ``refused_spend_usd`` on a refusal (§4.6). The limit-check SUM reads
the daily ledger itself (``org_daily_run_counts.total_spend_usd`` keyed by
``run_date``), so already-recorded spend is enforced even when no ``runs``
rows survive.
"""

import asyncio
import logging
import uuid
from datetime import UTC, date, datetime, timedelta
from decimal import ROUND_HALF_UP, Decimal
from typing import Any

from sqlalchemy import case, func, select, text, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from modulo.core.cost_controller.breakdown.constants import COST_COLUMN_CAP
from modulo.core.cost_controller.breakdown.metrics import (
    record_ledger_clamped,
    record_ledger_refused_clamped,
)
from modulo.db.models.daily_run_count import OrgDailyRunCount
from modulo.db.models.organisation import Organisation
from modulo.db.models.pipeline import Pipeline
from modulo.db.models.run import Run
from modulo.db.models.team import Team
from modulo.db.models.trigger import Trigger
from modulo.db.rls import set_rls_execution_context

_log = logging.getLogger(__name__)

__all__ = [
    "build_cost_report_buckets",
    "check_and_record_spend",
    "check_pipeline_circuit_breaker",
    "created_at_day_start",
    "get_cost_report",
    "get_or_create_daily_count",
    "reset_pipeline_circuit_breaker",
    "sum_pipeline_monthly_spend",
]

_REPORT_COMPONENT_LIMIT = 500
_REPORT_QUANT = Decimal("0.000001")

# Numeric-string pattern mirroring the decimal strings the breakdown producer
# writes (``_entry_amount`` — ``format(Decimal, "f")`` 6dp). Kept here as the
# documentation + parity anchor; the SAME pattern is embedded TWICE in the
# buckets SQL below (amount CASE + parse-keeps-row HAVING) — kept in sync by
# ``test_numeric_string_regex_is_embedded_in_the_buckets_sql``.
_NUMERIC_STRING_RE = r"^\s*[+-]?([0-9]+(\.[0-9]*)?|\.[0-9]+)([eE][+-]?[0-9]+)?\s*$"

# FAR-657 — the costs-overview buckets are aggregated IN POSTGRES. The retired
# path selected (owner_team_id, total_cost_usd, cost_breakdown) for EVERY run
# in the period and aggregated per-component in Python: full JSON transfer +
# per-row parse, which dominated /admin/costs latency. Both statements below
# are self-contained: each names only ``runs`` in its own FROM clause and
# binds only ``:org_id`` / ``:since`` (no interpolation — the architecture
# f-string rule).

# Per-(owner_team_id, component) SUM over the org's scoped runs. The derived
# table scopes + filters BEFORE the set-returning function so a NULL or
# non-array breakdown can never reach ``jsonb_array_elements`` (the retired
# Python path skipped such rows silently — a non-list breakdown is neither
# legacy nor components). Marker-bearing runs (any element carrying the
# JSON-boolean-true ``total_clamped``) are EXCLUDED wholesale, matching the
# retired per-run marker check. The amount CASE mirrors the retired per-entry
# parse: JSON numbers and numeric-looking strings (the producer's format) cast
# to numeric; everything else (bool / object / malformed string) contributes 0.
# The HAVING keeps only components with at least one parseable-or-absent
# amount — a malformed amount DROPPED the entry in the retired path (no bucket
# row), while a null/missing amount KEPT it as a 0 row; a plain GROUP BY would
# have materialised a 0.000000 row for the malformed case.
_SQL_COST_COMPONENT_BUCKETS = r"""
SELECT
    r.owner_team_id AS team_id,
    elem->>'component' AS component,
    SUM(
        CASE
            WHEN jsonb_typeof(elem->'amount_usd') = 'number'
                THEN (elem->>'amount_usd')::numeric
            WHEN jsonb_typeof(elem->'amount_usd') = 'string'
                 AND elem->>'amount_usd' ~ '^\s*[+-]?([0-9]+(\.[0-9]*)?|\.[0-9]+)([eE][+-]?[0-9]+)?\s*$'
                THEN (elem->>'amount_usd')::numeric
            ELSE 0
        END
    ) AS amount_usd
FROM (
    SELECT owner_team_id, cost_breakdown
    FROM runs
    WHERE organisation_id = :org_id
      AND started_at IS NOT NULL
      AND started_at >= :since
      AND cost_breakdown IS NOT NULL
      AND jsonb_typeof(cost_breakdown::jsonb) = 'array'
      AND NOT EXISTS (
          SELECT 1
          FROM jsonb_array_elements(cost_breakdown::jsonb) AS marker
          WHERE marker->'total_clamped' = 'true'::jsonb
      )
) AS r
CROSS JOIN LATERAL jsonb_array_elements(r.cost_breakdown::jsonb) AS elem
WHERE jsonb_typeof(elem->'component') = 'string'
  AND elem->>'component' <> ''
GROUP BY r.owner_team_id, elem->>'component'
HAVING SUM(
    CASE
        WHEN jsonb_typeof(elem->'amount_usd') = 'number' THEN 1
        WHEN jsonb_typeof(elem->'amount_usd') = 'string'
             AND elem->>'amount_usd' ~ '^\s*[+-]?([0-9]+(\.[0-9]*)?|\.[0-9]+)([eE][+-]?[0-9]+)?\s*$' THEN 1
        WHEN elem->'amount_usd' IS NULL THEN 1
        WHEN jsonb_typeof(elem->'amount_usd') = 'null' THEN 1
        ELSE 0
    END
) > 0
"""

# ``Σ total_cost_usd`` over scoped runs with NO component breakdown (the
# pre-0066 "legacy" rows). A JSON-null breakdown deserializes to Python None
# and was counted as legacy by the retired path, so ``jsonb_typeof`` 'null' is
# included alongside SQL NULL.
_SQL_COST_LEGACY_TOTAL = """
SELECT COALESCE(SUM(total_cost_usd), 0) AS legacy_total
FROM runs
WHERE organisation_id = :org_id
  AND started_at IS NOT NULL
  AND started_at >= :since
  AND total_cost_usd IS NOT NULL
  AND (cost_breakdown IS NULL OR jsonb_typeof(cost_breakdown::jsonb) = 'null')
"""


def _safe_float(value: Decimal | None) -> float:
    return float(value) if value is not None else 0.0


def _safe_int(value: Decimal | int | None) -> int:
    return int(value) if value is not None else 0


def _report_since(today: date, period: str) -> date:
    """Start-of-period date for the report windows (day/week/month/year)."""
    if period == "day":
        return today
    if period == "week":
        return today - timedelta(days=today.weekday())
    if period == "month":
        return date(today.year, today.month, 1)
    return date(today.year, 1, 1)


def _report_amount(value: Decimal) -> str:
    """Serialize a reporting bucket as a 6dp Decimal string (never float)."""
    try:
        return format(value.quantize(_REPORT_QUANT, rounding=ROUND_HALF_UP), "f")
    except (TypeError, ValueError, ArithmeticError):
        return "0.000000"


def created_at_day_start(now: datetime | None = None) -> datetime:
    """The CREATED-AT day start (UTC) — the shared refusal/enforcement window.

    The SINGLE shared helper used by BOTH the enforcement readers
    (``cron_helpers`` / ``polling``) AND the refusal-window SUM, so the two
    surfaces cannot drift apart (spec §4.6). ``created_at >= :day_start``.
    """
    return (now or datetime.now(UTC)).replace(hour=0, minute=0, second=0, microsecond=0)


async def get_or_create_daily_count(
    session: AsyncSession,
    *,
    org_id: uuid.UUID,
    run_date: date,
    team_id: uuid.UUID | None = None,
) -> OrgDailyRunCount:
    """Get today's run count row (org-level or team-level), creating it if missing.

    The caller is expected to SELECT FOR UPDATE on the returned row
    before mutating it (see check_and_record_spend).
    """
    q = (
        select(OrgDailyRunCount)
        .where(
            OrgDailyRunCount.organisation_id == org_id,
            OrgDailyRunCount.run_date == run_date,
            OrgDailyRunCount.team_id == team_id,
        )
        .with_for_update()
    )

    result = await session.execute(q)
    row = result.scalar_one_or_none()
    if row is not None:
        return row

    savepoint = await session.begin_nested()
    try:
        row = OrgDailyRunCount(
            organisation_id=org_id,
            run_date=run_date,
            team_id=team_id,
            run_count=0,
            total_spend_usd=Decimal(0),
        )
        session.add(row)
        await session.flush()
        return row
    except asyncio.CancelledError:
        raise
    except IntegrityError:
        await savepoint.rollback()
        result = await session.execute(q)
        row = result.scalar_one_or_none()
        if row is not None:
            return row
        raise


async def check_and_record_spend(
    session: AsyncSession,
    *,
    org_id: uuid.UUID,
    cost_usd: Decimal | None,
    team_id: uuid.UUID | None = None,
    run_id: uuid.UUID | None = None,
    run_date: date | None = None,
) -> tuple[bool, str | None]:
    """Check spend limits atomically and record the spend (TERMINAL-ONLY, §4.6).

    Returns ``(approved: bool, reason: str | None)``. ``ok=false`` = a daily
    limit would be exceeded and NO spend row was written; ``reason`` is a
    stable machine code (``"daily_limit_exceeded"``) qualified by the refused
    scope (``"daily_limit_exceeded: organisation"`` / ``"daily_limit_exceeded:
    team"``). Consumers log on false (``cost_ledger.limit_reached``) but do
    NOT fail the run.

    Refusal-window semantics (normative in spec §4.6):

    - The limit check is keyed to the CREATED-AT day (the same window the
      enforcement readers use) via ``created_at_day_start``.
    - Each SUM reads the daily ledger (``org_daily_run_counts``) — the org SUM
      over the org-level row (``team_id IS NULL``), the team SUM over the
      team-scoped row — filtered to the created-at day (``run_date ==
      day_start.date()``). The check runs BEFORE the current run is written to
      the ledger, so the SUM is naturally current-run-exclusive;
      ``cost_usd`` is then added UNCONDITIONALLY — the run counts EXACTLY
      ONCE per predicate.
    - The SUMs SHORT-CIRCUIT when the limit is NULL (a no-limit org runs NO
      SUM). The limit fetch preserves NULL (no ``coalesce``).
    - BOTH limits are checked BEFORE either spend row is written; only if both
      pass are the org row then the team row written (org-first mutation
      order). A NULL-``team_id`` run (NULL-owner) writes ONLY the org row.
    - On a refusal the refused amount is written to BOTH day rows'
      ``refused_spend_usd`` (org + team) with the accumulation clamp — a
      refusal is not silent.
    - The daily-ledger clamp protects the STARTED-AT row (``run_date``) after
      the limit check; a clamped day sets the row's ``clamped`` boolean.
    """
    if cost_usd is None:
        return False, "cost_must_not_be_none"
    if cost_usd.is_nan() or cost_usd.is_infinite():
        return False, "cost_must_be_finite"
    if cost_usd < 0:
        return False, "cost_must_be_non_negative"
    if run_date is None:
        run_date = datetime.now(UTC).date()
    day_start = created_at_day_start()

    # --- check BOTH limits BEFORE writing either spend row (§4.6) ---
    refuse_org = False
    org_limit = None
    org_limit_result = await session.execute(select(Organisation.daily_spend_limit).where(Organisation.id == org_id))
    org_limit = org_limit_result.scalar_one_or_none()
    if org_limit is not None:
        org_sum = await _sum_created_at_day(session, org_id=org_id, day_start=day_start, _run_id=run_id)
        refuse_org = org_sum + cost_usd > org_limit

    refuse_team = False
    team_limit: Decimal | None = None
    if team_id is not None:
        team_limit_result = await session.execute(select(Team.daily_spend_limit).where(Team.id == team_id))
        team_limit = team_limit_result.scalar_one_or_none()
        if team_limit is not None:
            team_sum = await _sum_created_at_day(
                session, org_id=org_id, day_start=day_start, _run_id=run_id, team_id=team_id
            )
            refuse_team = team_sum + cost_usd > team_limit

    if refuse_org or refuse_team:
        # PERMANENT refusal — write the refused amount to the day rows'
        # refused_spend_usd (org + team). A first-of-day refusal creates a
        # refused-only row (total_spend_usd 0, run_count 0) — the limit checks
        # ran BEFORE get_or_create_daily_count, so no 0-spend row is created.
        org_refused = await get_or_create_daily_count(session, org_id=org_id, run_date=run_date, team_id=None)
        _accumulate_refused(org_refused, cost_usd)
        if team_id is not None:
            team_refused = await get_or_create_daily_count(session, org_id=org_id, run_date=run_date, team_id=team_id)
            _accumulate_refused(team_refused, cost_usd)
        await session.flush()
        scopes: list[str] = []
        if refuse_org:
            scopes.append("organisation")
        if refuse_team:
            scopes.append("team")
        return False, f"daily_limit_exceeded: {', '.join(scopes)}"

    # --- both limits pass — write the org row then the team row ---
    org_count = await get_or_create_daily_count(session, org_id=org_id, run_date=run_date, team_id=None)
    _add_spend(org_count, cost_usd)
    if team_id is not None:
        team_count = await get_or_create_daily_count(session, org_id=org_id, run_date=run_date, team_id=team_id)
        _add_spend(team_count, cost_usd)

    await session.flush()
    return True, None


async def _sum_created_at_day(
    session: AsyncSession,
    *,
    org_id: uuid.UUID,
    day_start: datetime,
    _run_id: uuid.UUID | None,
    team_id: uuid.UUID | None = None,
) -> Decimal:
    """SUM the day's ledger spend (``org_daily_run_counts.total_spend_usd``).

    NOTE: the refusal SUM reads the ``org_daily_run_counts`` LEDGER (keyed by
    ``run_date``), NOT ``runs`` — so it does NOT use ``ix_runs_refusal``
    (organisation_id, created_at). That index is KEPT for the per-trigger
    daily-spend-limit enforcement readers (``cron_helpers`` / ``polling``) and
    the billing overview, which still query ``Run.created_at``.

    The ledger is keyed by ``(organisation_id, team_id, run_date)`` via
    ``get_or_create_daily_count``, so the created-at-day window filters
    ``run_date == day_start.date()``. The org SUM reads the org-level row
    (``team_id IS NULL``); the team SUM reads the team-scoped row
    (``team_id == :team_id``). The limit check runs BEFORE the current run is
    written to the ledger, so the SUM is naturally current-run-exclusive — the
    run counts EXACTLY ONCE per predicate. ``run_id`` is accepted for signature
    compatibility but is NOT applied (the ledger is keyed per day, not per run).
    """
    stmt = select(func.coalesce(func.sum(OrgDailyRunCount.total_spend_usd), 0)).where(
        OrgDailyRunCount.organisation_id == org_id,
        OrgDailyRunCount.run_date == day_start.date(),
    )
    if team_id is not None:
        stmt = stmt.where(OrgDailyRunCount.team_id == team_id)
    else:
        stmt = stmt.where(OrgDailyRunCount.team_id.is_(None))
    result = await session.execute(stmt)
    value = result.scalar_one()
    return Decimal(value or 0)


def _add_spend(row: OrgDailyRunCount, cost_usd: Decimal) -> None:
    """Apply the daily-ledger clamp and increment a ledger row (§4.5/§4.6)."""
    new_total = row.total_spend_usd + cost_usd
    if new_total > COST_COLUMN_CAP:
        row.total_spend_usd = COST_COLUMN_CAP
        row.clamped = True
        record_ledger_clamped()
        _log.warning(
            "cost_ledger.day_clamped",
            extra={"run_date": str(row.run_date), "team_id": str(row.team_id or "none")},
        )
    else:
        row.total_spend_usd = new_total
    row.run_count += 1


def _accumulate_refused(row: OrgDailyRunCount, amount: Decimal) -> None:
    """Accumulate a refused amount onto a day row with the accumulation clamp."""
    new_refused = row.refused_spend_usd + amount
    if new_refused > COST_COLUMN_CAP:
        row.refused_spend_usd = COST_COLUMN_CAP
        record_ledger_refused_clamped()
        _log.warning(
            "cost_ledger.refused_clamped",
            extra={"run_date": str(row.run_date), "team_id": str(row.team_id or "none")},
        )
    else:
        row.refused_spend_usd = new_refused


# ===========================================================================
# Pipeline cost-control circuit breaker (FAR-105, spec §8.10)
# ===========================================================================
#
# Per-pipeline monthly spend threshold. When the pipeline's accumulated run
# spend for the current month plus a new run's cost would exceed
# ``Pipeline.circuit_breaker_threshold``, the breaker trips: the pipeline is
# marked ``circuit_breaker_tripped``, ALL of its triggers are permanently
# paused (``Trigger.active = False`` — the same predicate the trigger engine /
# cron / polling paths gate on, so no new trigger-initiated runs can start),
# and an admin notification is dispatched (``EVENT_CIRCUIT_BREAKER_TRIPPED``).
# An admin re-enabling the pipeline clears the flag and re-activates triggers.


async def sum_pipeline_monthly_spend(
    session: AsyncSession,
    *,
    org_id: uuid.UUID,
    pipeline_id: uuid.UUID,
    exclude_run_id: uuid.UUID | None = None,
) -> Decimal:
    """SUM the pipeline's run spend (``runs.total_cost_usd``) for the current month.

    Reads the ``runs`` table (the terminal-cost source, spec §8.10) for runs of
    the pipeline whose ``started_at`` falls in the current UTC month. The daily
    ledger (``org_daily_run_counts``) is NOT used — it is keyed per org/team
    day, not per pipeline, so a per-pipeline monthly total must come from
    ``runs``.

    ``exclude_run_id`` excludes the in-flight run from the sum. The ledger
    wiring (``finalize``) writes the run's cost BEFORE the breaker check, so
    the current run must be excluded to avoid double-counting it as both a
    stored run and the added ``cost_usd``.
    """
    now = datetime.now(UTC)
    month_start = datetime(now.year, now.month, 1, tzinfo=UTC)
    stmt = select(func.coalesce(func.sum(Run.total_cost_usd), 0)).where(
        Run.organisation_id == org_id,
        Run.pipeline_id == pipeline_id,
        Run.started_at.isnot(None),
        Run.started_at >= month_start,
    )
    if exclude_run_id is not None:
        stmt = stmt.where(Run.id != exclude_run_id)
    result = await session.execute(stmt)
    return Decimal(result.scalar_one() or 0)


async def check_pipeline_circuit_breaker(
    session: AsyncSession,
    *,
    org_id: uuid.UUID,
    pipeline_id: uuid.UUID,
    cost_usd: Decimal,
    run_id: uuid.UUID | None = None,
) -> tuple[bool, str | None]:
    """Check the pipeline's monthly circuit-breaker threshold, tripping if exceeded.

    Returns ``(approved: bool, reason: str | None)``. ``ok=false`` with
    ``reason="circuit_breaker_tripped"`` means the pipeline's monthly
    accumulated spend plus *cost_usd* would exceed its threshold (the breaker
    tripped, pausing the pipeline's triggers and notifying admins), or the
    breaker was already tripped — a new run must not proceed. ``ok=true`` with
    ``reason=None`` when the pipeline has no threshold configured or the spend
    stays within it.

    Fail-closed on a tripped pipeline: an already-tripped breaker rejects
    immediately (no spend re-read). The trip is idempotent — a concurrent
    second trip is a no-op.
    """
    if cost_usd is None:
        return False, "cost_must_not_be_none"
    if cost_usd.is_nan() or cost_usd.is_infinite():
        return False, "cost_must_be_finite"
    if cost_usd < 0:
        return False, "cost_must_be_non_negative"

    # Cost control runs inside the executor's org-only context (no user
    # principal) — the execution hatch lets it read a team-private pipeline's
    # breaker state.
    await set_rls_execution_context(session)
    pipeline = (
        await session.execute(select(Pipeline).where(Pipeline.id == pipeline_id, Pipeline.organisation_id == org_id))
    ).scalar_one_or_none()
    if pipeline is None:
        return True, None
    if pipeline.circuit_breaker_tripped:
        return False, "circuit_breaker_tripped"
    threshold = pipeline.circuit_breaker_threshold
    if threshold is None:
        return True, None

    monthly = await sum_pipeline_monthly_spend(
        session,
        org_id=org_id,
        pipeline_id=pipeline_id,
        exclude_run_id=run_id,
    )
    if monthly + cost_usd > threshold:
        await trip_pipeline_circuit_breaker(
            session,
            org_id=org_id,
            pipeline_id=pipeline_id,
            pipeline_name=pipeline.name,
            pipeline=pipeline,
            run_id=run_id,
        )
        return False, "circuit_breaker_tripped"
    return True, None


async def trip_pipeline_circuit_breaker(
    session: AsyncSession,
    *,
    org_id: uuid.UUID,
    pipeline_id: uuid.UUID,
    pipeline_name: str,
    pipeline: Pipeline | None = None,
    run_id: uuid.UUID | None = None,
) -> bool:
    """Trip the pipeline's circuit breaker: mark tripped, pause triggers, notify.

    Sets ``circuit_breaker_tripped`` (idempotent), sets ``active = False`` on
    every non-deleted trigger of the pipeline (the enforcement predicate for
    trigger-initiated runs), and dispatches the ``circuit_breaker_tripped``
    admin notification (fail-open — a notifier failure never blocks the trip).

    ``pipeline`` may be passed in (already loaded by the caller) to avoid a
    re-read; when omitted the row is loaded here. Returns ``True`` when the
    pipeline existed and was (re)marked tripped.
    """
    if pipeline is None:
        await set_rls_execution_context(session)
        pipeline = (
            await session.execute(
                select(Pipeline).where(Pipeline.id == pipeline_id, Pipeline.organisation_id == org_id)
            )
        ).scalar_one_or_none()
        if pipeline is None:
            return False
    if pipeline.circuit_breaker_tripped:
        return False  # idempotent — a concurrent second trip is a no-op
    pipeline.circuit_breaker_tripped = True
    pipeline.circuit_breaker_tripped_at = datetime.now(UTC)
    await session.execute(
        update(Trigger)
        .where(
            Trigger.organisation_id == org_id,
            Trigger.pipeline_id == pipeline_id,
            Trigger.deleted_at.is_(None),
        )
        .values(active=False)
    )
    await session.flush()
    await _dispatch_circuit_breaker_tripped(org_id, pipeline_id, pipeline_name, run_id=run_id)
    return True


async def reset_pipeline_circuit_breaker(
    session: AsyncSession,
    *,
    org_id: uuid.UUID,
    pipeline_id: uuid.UUID,
) -> bool:
    """Admin re-enable: clear the tripped flag and re-activate the pipeline's triggers.

    Returns ``True`` when the pipeline exists and the breaker was reset
    (triggers re-activated); ``False`` when no such pipeline exists in the org.
    """
    pipeline = (
        await session.execute(select(Pipeline).where(Pipeline.id == pipeline_id, Pipeline.organisation_id == org_id))
    ).scalar_one_or_none()
    if pipeline is None:
        return False
    pipeline.circuit_breaker_tripped = False
    pipeline.circuit_breaker_tripped_at = None
    # FAR-190: every re-activated trigger's no-delivery streak epoch is
    # re-anchored IN THE SAME atomic statement as the active=True flip (the
    # shared anchor semantics — no un-epoch'd active=True transition). Inlined
    # here (rather than a second UPDATE) so the whole re-enable is one statement.
    # RETURNING carries the re-enabled ids so the FAR-158 counter clear below
    # needs no extra query.
    result = await session.execute(
        update(Trigger)
        .where(
            Trigger.organisation_id == org_id,
            Trigger.pipeline_id == pipeline_id,
            Trigger.deleted_at.is_(None),
        )
        .values(active=True, streak_epoch=datetime.now(UTC))
        .returning(Trigger.id)
    )
    re_enabled_ids = list(result.scalars().all())
    await session.flush()
    # FAR-190 (qa FIX 12): a trigger re-activated via the circuit-breaker reset
    # must not keep its stale FAR-158 config-failure counter — a leftover
    # counter would re-deactivate the freshly re-enabled trigger on its next
    # top-up. Best-effort (never raises): over-clearing is safe, under-clearing
    # is not, so calling the shared clear helper post-flush is correct even if
    # the outer transaction later rolls back.
    try:
        from modulo.core.trigger_streak import clear_trigger_streak_after_reenable

        for re_enabled_id in re_enabled_ids:
            await clear_trigger_streak_after_reenable(re_enabled_id)
    except asyncio.CancelledError:
        raise
    except Exception:
        _log.warning("circuit_breaker.reset_streak_clear_failed pipeline=%s", pipeline_id)
    return True


async def _dispatch_circuit_breaker_tripped(
    org_id: uuid.UUID,
    pipeline_id: uuid.UUID,
    pipeline_name: str,
    run_id: uuid.UUID | None = None,
) -> None:
    """Dispatch the ``circuit_breaker_tripped`` admin notification (fail-open).

    Lazily builds the notifier from the shared engine + settings so the cost
    controller stays importable without an app engine. A notifier failure is
    logged and swallowed — the trip (flag + trigger pause) is the enforcement,
    the notification is best-effort (ADR: best-effort ops fail open WITH a log).
    """
    try:
        from modulo.core.notifier import EVENT_CIRCUIT_BREAKER_TRIPPED, Notifier
        from modulo.db.session import get_shared_engine
        from modulo.settings import get_settings

        settings = get_settings()
        notifier = Notifier(get_shared_engine(), settings.fernet_key)
        await notifier.dispatch_event(
            org_id,
            EVENT_CIRCUIT_BREAKER_TRIPPED,
            {
                "pipeline_id": str(pipeline_id),
                "pipeline_name": pipeline_name,
                "run_id": str(run_id) if run_id else None,
            },
            run_id=run_id,
        )
    except asyncio.CancelledError:
        raise
    except Exception:
        _log.exception(
            "circuit_breaker.notify_failed",
            extra={"org_id": str(org_id), "pipeline_id": str(pipeline_id)},
        )


async def get_cost_report(
    session: AsyncSession,
    *,
    org_id: uuid.UUID,
    group_by: str = "team",
    period: str = "month",
) -> list[dict[str, Any]]:
    """Build a cost report grouped by team or organisation for a given period.

    Args:
        group_by: "team" or "org"
        period: "day", "week", "month", "year"

    Returns a list of dicts with keys: entity_id, entity_name, total_spend_usd, total_runs.

    """
    valid_periods = frozenset({"day", "week", "month", "year"})
    if period not in valid_periods:
        raise ValueError(f"Unknown period '{period}'. Expected one of: {', '.join(sorted(valid_periods))}")

    if group_by not in ("team", "org"):
        raise ValueError(f"Unknown group_by '{group_by}'. Expected 'team' or 'org'.")

    since = _report_since(datetime.now(UTC).date(), period)

    if group_by == "team":
        q = (
            select(
                OrgDailyRunCount.team_id,
                func.sum(OrgDailyRunCount.total_spend_usd).label("total_spend_usd"),
                func.sum(OrgDailyRunCount.run_count).label("total_runs"),
            )
            .where(
                OrgDailyRunCount.organisation_id == org_id,
                OrgDailyRunCount.run_date >= since,
                OrgDailyRunCount.team_id.isnot(None),
            )
            .group_by(OrgDailyRunCount.team_id)
        )

        result = await session.execute(q)
        rows = result.all()

        team_ids = [row.team_id for row in rows if row.team_id is not None]
        teams_map: dict[uuid.UUID, Team] = {}
        if team_ids:
            teams_result = await session.execute(select(Team).where(Team.id.in_(team_ids)))
            teams_map = {t.id: t for t in teams_result.scalars().all()}

        return [
            {
                "entity_id": str(row.team_id),
                "entity_name": team.name if (team := teams_map.get(row.team_id)) else "Unknown",
                "total_spend_usd": _safe_float(row.total_spend_usd),
                "total_runs": _safe_int(row.total_runs),
            }
            for row in rows
            if row.team_id is not None
        ]

    # group_by == "org"
    org_q = select(
        func.sum(OrgDailyRunCount.total_spend_usd).label("total_spend_usd"),
        func.sum(OrgDailyRunCount.run_count).label("total_runs"),
    ).where(
        OrgDailyRunCount.organisation_id == org_id,
        OrgDailyRunCount.run_date >= since,
        OrgDailyRunCount.team_id.is_(None),
    )
    result = await session.execute(org_q)
    org_row = result.one_or_none()
    org_spend = _safe_float(org_row.total_spend_usd if org_row else None)
    org_runs = _safe_int(org_row.total_runs if org_row else None)

    org_result = await session.execute(select(Organisation.name).where(Organisation.id == org_id))
    org_name = org_result.scalar_one_or_none() or "Unknown"

    return [
        {
            "entity_id": str(org_id),
            "entity_name": org_name,
            "total_spend_usd": org_spend,
            "total_runs": org_runs,
        }
    ]


async def build_cost_report_buckets(
    session: AsyncSession,
    *,
    org_id: uuid.UUID,
    period: str = "month",
) -> dict[str, Any]:
    """PR B: per-component + org reporting buckets over the RUNS table.

    REPORTING fields only — the ledger (``OrgDailyRunCount``) stays the
    period-total source; these buckets read the runs table, which IS purged at
    ~90 days by run retention, so they cover only un-purged runs (for a
    year-to-date report, windows > 90 days show empty buckets — accepted and
    stated in the operator guide).

    FAR-657: the per-component buckets are aggregated IN POSTGRES — a lateral
    ``jsonb_array_elements`` expansion groups per (owner_team_id, component)
    and SUMs the amounts in SQL (``_SQL_COST_COMPONENT_BUCKETS``). The
    endpoint never hydrates unbounded per-run rows: the retired path selected
    every run's full ``cost_breakdown`` JSON and parsed it row-by-row in
    Python, which dominated the /admin/costs latency. The marker exclusion,
    entry validation, grouping, and 6dp serialization are byte-identical to
    the retired algorithm (pinned by the parity tests in
    ``tests/unit/core/cost_controller/test_cost_buckets_sql_aggregation.py``).

    Returns:
        components_by_team: ``{str(team_id) | "__org__": [{name, amount_usd}]}``
            where ``component`` is the stable aggregation key (pre-delete and
            post-recreate amounts combine under one slug).
        annotations_by_team: ``{str(team_id) | "__org__": {"refused_total_usd",
            "clamped_total_usd"}}`` — refused reads the ``refused_spend_usd``
            ledger column (survives the run purge); clamped is the sum of spend
            over days whose ledger row has ``clamped = true`` and is NOT
            additive with ``total_spend_usd``.
        legacy_total: ``Σ run.total_cost_usd`` over scoped runs with
            ``cost_breakdown IS NULL`` (Decimal string).
        org_unassigned_components: component-attributed spend from runs with no
            team (Decimal string).
        org_total: ``Σ(team components) + org_unassigned_components +
            legacy_total`` over NON-marker-bearing runs (Decimal string) — the
            REPORTING invariant, never a health gate.
        has_more: True when a component bucket was truncated at
            ``_REPORT_COMPONENT_LIMIT`` (bounded by design).

    """
    valid_periods = frozenset({"day", "week", "month", "year"})
    if period not in valid_periods:
        raise ValueError(f"Unknown period '{period}'. Expected one of: {', '.join(sorted(valid_periods))}")

    since = _report_since(datetime.now(UTC).date(), period)

    component_result = await session.execute(text(_SQL_COST_COMPONENT_BUCKETS), {"org_id": org_id, "since": since})
    # GROUP BY (owner_team_id, component) guarantees row uniqueness, so each
    # fetched row lands in exactly one bucket slot — no accumulation needed.
    team_components: dict[uuid.UUID | None, dict[str, Decimal]] = {}
    for row in component_result.all():
        team_id: uuid.UUID | None = row.team_id
        bucket = team_components.setdefault(team_id, {})
        bucket[row.component] = Decimal(str(row.amount_usd))

    legacy_result = await session.execute(text(_SQL_COST_LEGACY_TOTAL), {"org_id": org_id, "since": since})
    legacy_total = Decimal(str(legacy_result.scalar_one()))

    def _serialized(
        bucket: dict[str, Decimal],
        limit: int = _REPORT_COMPONENT_LIMIT,
    ) -> tuple[list[dict[str, str]], bool]:
        entries = sorted(bucket.items(), key=lambda kv: (-kv[1], kv[0]))
        truncated = len(entries) > limit
        return [{"name": name, "amount_usd": _report_amount(amount)} for name, amount in entries[:limit]], truncated

    components_by_team: dict[str, list[dict[str, str]]] = {}
    has_more = False
    for team_id, bucket in team_components.items():
        key = str(team_id) if team_id is not None else "__org__"
        comps, truncated = _serialized(bucket)
        components_by_team[key] = comps
        if truncated:
            has_more = True

    annotation_result = await session.execute(
        select(
            OrgDailyRunCount.team_id,
            func.sum(OrgDailyRunCount.refused_spend_usd).label("refused_total"),
            func.sum(
                case(
                    (OrgDailyRunCount.clamped.is_(True), OrgDailyRunCount.total_spend_usd),
                    else_=Decimal(0),
                )
            ).label("clamped_total"),
        )
        .where(
            OrgDailyRunCount.organisation_id == org_id,
            OrgDailyRunCount.run_date >= since,
        )
        .group_by(OrgDailyRunCount.team_id)
    )
    annotations_by_team: dict[str, dict[str, float | None]] = {}
    for row in annotation_result.all():
        key = str(row.team_id) if row.team_id is not None else "__org__"
        refused = _safe_float(row.refused_total)
        clamped = _safe_float(row.clamped_total)
        annotations_by_team[key] = {
            "refused_total_usd": refused if refused > 0 else None,
            "clamped_total_usd": clamped if clamped > 0 else None,
        }

    org_run_count_result = await session.execute(
        select(func.sum(OrgDailyRunCount.run_count)).where(
            OrgDailyRunCount.organisation_id == org_id,
            OrgDailyRunCount.run_date >= since,
            OrgDailyRunCount.team_id.is_(None),
        )
    )
    org_run_count_value = org_run_count_result.scalar_one_or_none()
    org_run_count = int(org_run_count_value) if org_run_count_value is not None else 0

    org_unassigned = Decimal(0)
    team_sum = Decimal(0)
    for team_id, bucket in team_components.items():
        for amount in bucket.values():
            if team_id is None:
                org_unassigned += amount
            else:
                team_sum += amount

    return {
        "components_by_team": components_by_team,
        "annotations_by_team": annotations_by_team,
        "legacy_total": _report_amount(legacy_total),
        "org_unassigned_components": _report_amount(org_unassigned),
        "org_total": _report_amount(team_sum + org_unassigned + legacy_total),
        "org_run_count": org_run_count,
        "has_more": has_more,
    }
