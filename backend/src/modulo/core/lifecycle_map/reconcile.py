"""Journey reconciliation sweep + journey metric counters (FAR-143 part 4).

Two roles live here.

Reconciliation (``reconcile_journeys``)
----------------------------------------
The bounded, terminal-only safety net that re-derives ``journeys`` evidence
from runs whose journeys never advanced (or advanced for a different ref
set). ``advance_journeys`` (FAR-143 part 2) is the single finalise-path
writer; runs can still end up with a MISSING or STALE journey row when:

* they terminalised before the finalise hook shipped (a post-deploy backlog);
* a raw terminal writer (``mark_complete`` / ``fail_run_terminal``) skipped
  the hook for some refs; or
* a self-report confirm dropped a ref the create-time mint never saw.

The sweep is deliberately NOT a full-table sweep: at most ``batch_size``
terminal-run candidates are examined per invocation, oldest-completed first,
so a backlog drains oldest-first across ticks with a hard per-tick bound.

Drift definition (must match the finalise-path semantics exactly):

* MISSING — no ``journeys`` row for the run's canonical ``(org, kind, ref)``;
* STALE — the row's ``updated_at`` (the compare-and-set evidence anchor, see
  ``advancement.py``) is OLDER than the run's evidence timestamp
  (``completed_at``, falling back to ``created_at``). Only ADVANCING statuses
  (``complete`` / ``failed`` / ``eval_failed``) can be stale: ``cancelled`` /
  ``stalled`` runs are mint-only, so a mint-only run must never re-fire drift
  forever against an evidence timestamp it cannot move.

Only the DRIFT REFS are re-advanced, never the run's full ref set: a ref
whose journey is already current would otherwise have its ``run_count``
incremented a second time (the CAS protects evidence, not the counter). The
run's canonicalisation, dedup and advance all share ``advance_journeys``'s
canonicaliser (``validate_ref_entry``), so a second sweep over an already
reconciled run is a no-op — the sweep is idempotent.

FAIL-OPEN per run: a per-run advance failure is logged and counted in the
returned error tally — one bad run never aborts the sweep. The caller owns
the session and its transaction; the system cron uses the modulo_system role
(LOGIN, BYPASSRLS) for cross-org access, and the stage lookup inside
``advance_journeys`` is explicitly org-filtered.

Metrics
-------
The journey metric counters for this delivery live here (the single owning
module, mirroring ``cost_controller.breakdown.metrics`` and
``analytics.metrics``): ``parse_failure`` / ``finalise_attempt`` (per writer
path), ``self_report_refs_capped``, ``unmatched_self_report_refs``,
``journey_advance_total`` and ``journey_reconcile_drift``. The finalise hook
(``cost_controller.finalize``) imports the ``record_*`` functions from here.
All handles are lazy-initialised so a missing meter provider never breaks the
journey path.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import and_, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from modulo.core.lifecycle_map.advancement import advance_journeys
from modulo.db.lifecycle_refs import validate_ref_entry
from modulo.db.models.journey import Journey
from modulo.db.models.run import TERMINAL_STATUSES, Run

_log = logging.getLogger(__name__)

__all__ = [
    "reconcile_journeys",
    "record_journey_advance",
    "record_journey_finalise_attempt",
    "record_journey_parse_failure",
    "record_journey_reconcile_drift",
    "record_self_report_refs_capped",
    "record_unmatched_self_report_refs",
]

# Advancing terminal statuses — the ONLY statuses that move evidence. Mirrors
# ``advancement._ADVANCING_TERMINAL_STATUSES``; defined here so the drift rule
# ("stale" only applies to advancing runs) stays local and explicit.
_ADVANCING_STATUSES: frozenset[str] = frozenset({"complete", "failed", "eval_failed"})

# ---------------------------------------------------------------------------
# Journey metric counters — the owning module for this delivery
# ---------------------------------------------------------------------------

_journey_advance_total: Any = None
_journey_parse_failure_total: Any = None
_journey_finalise_attempt_total: Any = None
_self_report_refs_capped_total: Any = None
_unmatched_self_report_refs_total: Any = None
_journey_reconcile_drift_total: Any = None


def _get_meter() -> Any:
    try:
        from opentelemetry import metrics

        provider = metrics.get_meter_provider()
        if provider is None:
            return None
        return provider.get_meter("modulo.lifecycle_map", version="0.1.0")
    except Exception:
        _log.debug("lifecycle_map.meter_unavailable")
        return None


def _ensure() -> None:
    global \
        _journey_advance_total, \
        _journey_parse_failure_total, \
        _journey_finalise_attempt_total, \
        _self_report_refs_capped_total, \
        _unmatched_self_report_refs_total, \
        _journey_reconcile_drift_total
    if _journey_advance_total is not None:
        return
    meter = _get_meter()
    if meter is None:
        return
    _journey_advance_total = meter.create_counter(
        name="modulo_journey_advance_total",
        description="Journeys advanced (evidence + run_count) across the finalise and reconcile paths",
        unit="1",
    )
    _journey_parse_failure_total = meter.create_counter(
        name="modulo_journey_parse_failure_total",
        description="Self-report entries rejected as malformed during journey finalise, by finalise writer path",
        unit="1",
    )
    _journey_finalise_attempt_total = meter.create_counter(
        name="modulo_journey_finalise_attempt_total",
        description="Self-report entries the journey finalise hook attempted to validate, by finalise writer path",
        unit="1",
    )
    _self_report_refs_capped_total = meter.create_counter(
        name="modulo_journey_self_report_refs_capped_total",
        description="Self-report entries dropped by the max_refs cap (hostile/large outputs)",
        unit="1",
    )
    _unmatched_self_report_refs_total = meter.create_counter(
        name="modulo_journey_unmatched_self_report_refs_total",
        description="Confirmed-advisory self-report refs that matched no existing journey row and were dropped",
        unit="1",
    )
    _journey_reconcile_drift_total = meter.create_counter(
        name="modulo_journey_reconcile_drift_total",
        description="Missing/stale journey rows found by the reconciliation sweep, by drift kind",
        unit="1",
    )


def record_journey_advance(count: int = 1) -> None:
    """Record *count* journeys advanced (evidence + possibly run_count)."""
    if _journey_advance_total is None:
        _ensure()
    if _journey_advance_total is not None:
        _journey_advance_total.add(count)


def record_journey_parse_failure(writer: str, count: int = 1) -> None:
    """Record malformed self-report entries for a finalise writer path."""
    if _journey_parse_failure_total is None:
        _ensure()
    if _journey_parse_failure_total is not None:
        _journey_parse_failure_total.add(count, attributes={"writer": writer})


def record_journey_finalise_attempt(writer: str, count: int = 1) -> None:
    """Record self-report entries the journey finalise hook attempted, per writer."""
    if _journey_finalise_attempt_total is None:
        _ensure()
    if _journey_finalise_attempt_total is not None:
        _journey_finalise_attempt_total.add(count, attributes={"writer": writer})


def record_self_report_refs_capped(count: int = 1) -> None:
    """Record self-report entries dropped by the max_refs cap."""
    if _self_report_refs_capped_total is None:
        _ensure()
    if _self_report_refs_capped_total is not None:
        _self_report_refs_capped_total.add(count)


def record_unmatched_self_report_refs(count: int) -> None:
    """Record reported refs that matched no existing journey row (advisory drop)."""
    if _unmatched_self_report_refs_total is None:
        _ensure()
    if _unmatched_self_report_refs_total is not None:
        _unmatched_self_report_refs_total.add(count)


def record_journey_reconcile_drift(count: int = 1, kind: str = "missing") -> None:
    """Record missing/stale journey rows found by the reconciliation sweep."""
    if _journey_reconcile_drift_total is None:
        _ensure()
    if _journey_reconcile_drift_total is not None:
        _journey_reconcile_drift_total.add(count, attributes={"kind": kind})


# ---------------------------------------------------------------------------
# Reconciliation sweep
# ---------------------------------------------------------------------------


def _canonical_refs(refs: Any) -> list[dict[str, Any]]:
    """Canonicalise + dedupe stored work-item ref entries (fail-open).

    Mirrors ``advance_journeys._canonicalise_entry`` (``validate_ref_entry``
    + dedupe on the canonical ``(kind, ref)`` pair) so the drift detection and
    the re-advance share one canonicaliser. A malformed entry is dropped with
    a warning.
    """
    canonical: list[dict[str, Any]] = []
    if not isinstance(refs, list):
        return canonical
    seen: set[tuple[str, str]] = set()
    for entry in refs:
        try:
            canonicalised = validate_ref_entry(entry)
        except (ValueError, TypeError):
            _log.warning("journey_reconcile.dropping_invalid_ref", extra={"entry": entry})
            continue
        key = (canonicalised["kind"], canonicalised["ref"])
        if key in seen:
            continue
        seen.add(key)
        canonical.append(canonicalised)
    return canonical


async def _drift_refs(
    session: AsyncSession,
    organisation_id: uuid.UUID,
    canonical: list[dict[str, Any]],
    anchor: datetime | None,
    advancing: bool,
) -> list[dict[str, Any]]:
    """The subset of *canonical* refs whose journey row is MISSING or STALE.

    * ``populate_existing`` forces a re-read even when the ORM identity map
      holds a pre-advance instance — the sweep advances via raw SQL, so a
      second run in the same batch touching the same journey must see the
      post-advance ``updated_at`` or it would double-count.
    * STALE applies only to *advancing* runs (their evidence can move);
      ``cancelled`` / ``stalled`` runs mint-only, so only MISSING refs are
      drift for them (a stale row can never be moved and must not re-fire).
    """
    if not canonical:
        return []
    clauses = [and_(Journey.kind == entry["kind"], Journey.ref == entry["ref"]) for entry in canonical]
    rows = (
        await session.execute(
            select(Journey.kind, Journey.ref, Journey.updated_at)
            .where(
                Journey.organisation_id == organisation_id,
                or_(*clauses),
            )
            .execution_options(populate_existing=True)
        )
    ).all()
    found: dict[tuple[str, str], datetime | None] = {(row[0], row[1]): row[2] for row in rows}
    drift: list[dict[str, Any]] = []
    for entry in canonical:
        updated_at = found.get((entry["kind"], entry["ref"]))
        if updated_at is None or (advancing and anchor is not None and updated_at < anchor):
            drift.append(entry)
    return drift


def _drift_predicate(dialect: str) -> Any:
    """SQL predicate: the run carries at least one MISSING or STALE ref.

    Selecting ONLY drifting runs keeps the ``LIMIT`` batch window moving — a
    reconciled run is never re-selected, so the sweep drains oldest-first
    across ticks instead of re-scanning the same already-reconciled window
    forever. Per-ref JSON element access is dialect-specific: ``jsonb_array_elements``
    + ``->>`` on Postgres (``work_item_refs`` is JSONB there), ``json_each`` +
    ``json_extract`` on the generic JSON column used by SQLite/MariaDB. Each
    ref element is LEFT JOINed to its journey; an element is drift when the
    join misses OR (for advancing runs only) the journey's ``updated_at`` is
    older than the run's ``completed_at``. ``Run`` is correlated to the outer
    query — a ``NULL`` ``completed_at`` never satisfies ``updated_at <
    completed_at``, so such runs are selected only when a ref is MISSING.
    """
    if dialect == "postgresql":
        refs: Any = func.jsonb_array_elements(Run.work_item_refs).table_valued("value")
        kind_expr: Any = refs.c.value.op("->>")("kind")
        ref_expr: Any = refs.c.value.op("->>")("ref")
        # Native timestamptz comparison — exactly what advancement._evidence_timestamp
        # mirrors when it writes evidence into journeys.updated_at.
        stale_evidence: Any = Journey.updated_at < Run.completed_at
    else:
        refs = func.json_each(Run.work_item_refs).table_valued("value")
        kind_expr = func.json_extract(refs.c.value, "$.kind")
        ref_expr = func.json_extract(refs.c.value, "$.ref")
        # SQLite stores datetimes as text. `isoformat(sep=" ")` (advancement's
        # evidence writer) drops a zero microsecond suffix, so an equal-instant
        # updated_at would string-compare LESS than the stored completed_at and
        # re-select the run forever. datetime() normalises both sides to the
        # same "YYYY-MM-DD HH:MM:SS" form before comparing.
        stale_evidence = func.datetime(Journey.updated_at) < func.datetime(Run.completed_at)

    return (
        select(1)
        .select_from(
            refs.outerjoin(
                Journey,
                and_(
                    Journey.organisation_id == Run.organisation_id,
                    Journey.kind == kind_expr,
                    Journey.ref == ref_expr,
                ),
            )
        )
        .where(
            or_(
                Journey.id.is_(None),
                and_(
                    stale_evidence,
                    Run.status.in_(_ADVANCING_STATUSES),
                ),
            )
        )
        .exists()
        .correlate(Run)
    )


async def reconcile_journeys(session: AsyncSession, batch_size: int = 500) -> int:
    """Reconcile terminal-run journey drift (bounded, idempotent, fail-open).

    Examines at most ``batch_size`` terminal runs carrying at least one
    MISSING or STALE work-item ref, oldest-completed first (the drift
    predicate keeps reconciled runs out of the window, so the sweep drains the
    backlog across ticks). For each such run the DRIFT REFS ONLY are
    re-advanced through ``advance_journeys`` (compare-and-set on evidence,
    ``run_count`` never double-counted). Per-run failures are logged and
    counted, never fatal.

    Returns the number of journeys advanced (the count of refs re-advanced).
    """
    dialect = str(session.get_bind().dialect.name)
    result = await session.execute(
        select(Run)
        .where(
            Run.status.in_(TERMINAL_STATUSES),
            Run.work_item_refs.is_not(None),
            _drift_predicate(dialect),
        )
        .order_by(Run.completed_at.asc(), Run.created_at.asc())
        .limit(batch_size)
    )
    candidates = list(result.scalars().all())

    advanced = 0
    errors = 0
    drift_total = 0
    for run in candidates:
        canonical = _canonical_refs(run.work_item_refs)
        if not canonical:
            continue
        anchor = run.completed_at or run.created_at
        advancing = run.status in _ADVANCING_STATUSES
        drift = await _drift_refs(session, run.organisation_id, canonical, anchor, advancing=advancing)
        if not drift:
            continue
        drift_total += len(drift)
        record_journey_reconcile_drift(len(drift), kind="stale" if advancing else "missing")
        try:
            count = await advance_journeys(
                session,
                run.organisation_id,
                run_id=run.id,
                pipeline_id=run.pipeline_id,
                refs=drift,
                status=run.status,
                completed_at=run.completed_at,
                run_created_at=run.created_at,
                is_replay=bool(run.is_replay),
                variant_group_id=run.variant_group_id,
            )
            advanced += count
            record_journey_advance(count)
        except asyncio.CancelledError:
            raise
        except Exception:
            errors += 1
            _log.exception("journey_reconcile.advance_failed", extra={"run_id": str(run.id)})

    _log.info(
        "journey_reconcile.pass",
        extra={"candidates": len(candidates), "advanced": advanced, "drift": drift_total, "errors": errors},
    )
    return advanced
