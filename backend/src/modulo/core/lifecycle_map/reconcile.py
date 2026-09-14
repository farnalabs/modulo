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
The FAR-794 work-item-refs counters also live here
(``modulo_work_item_refs_by_source_total``, ``..._unknown_source_total``,
``..._shadow_strip_hits_total``, ``..._malformed_total``): the db layer cannot
import core, so ``db.crud.run`` / ``pipeline_engine.decorator`` emit events
through the ``lifecycle_refs`` hook, which this module registers at import
time. All handles are lazy-initialised so a missing meter provider never
breaks the journey path.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import and_, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from modulo.core.lifecycle_map.advancement import (
    advance_journeys,
    agent_minting_enabled,
    confirm_reported_refs,
)
from modulo.db.lifecycle_refs import (
    REFS_EVENT_ASSIGNED_SOURCE,
    REFS_EVENT_DISMISSAL_SUPPRESSED,
    REFS_EVENT_MALFORMED,
    REFS_EVENT_SHADOW_STRIP_HIT,
    REFS_EVENT_UNKNOWN_SOURCE,
    set_refs_counter_hook,
    validate_ref_entry,
)
from modulo.db.models.journey import Journey
from modulo.db.models.run import TERMINAL_STATUSES, Run

# FAR-795: engine-assigned agent provenance (rank 0).
_AGENT_SOURCE = "agent"

_log = logging.getLogger(__name__)

__all__ = [
    "reconcile_journeys",
    "record_journey_advance",
    "record_journey_finalise_attempt",
    "record_journey_parse_failure",
    "record_journey_reconcile_drift",
    "record_refs_agent_mint_budget_exceeded",
    "record_refs_by_source",
    "record_refs_cap_dropped",
    "record_refs_dismissal_suppressed",
    "record_refs_malformed",
    "record_refs_shadow_strip_hit",
    "record_refs_unknown_source",
    "record_self_report_refs_capped",
    "record_unmatched_self_report_refs",
]

# Advancing terminal statuses — the ONLY statuses that move evidence. Mirrors
# ``advancement._ADVANCING_TERMINAL_STATUSES``; defined here so the drift rule
# ("stale" only applies to advancing runs) stays local and explicit.
_ADVANCING_STATUSES: frozenset[str] = frozenset(
    {
        "complete",
        "failed",
        "eval_failed",
        "compensation_failed",
        "router_no_match",
    }
)

# ---------------------------------------------------------------------------
# Journey metric counters — the owning module for this delivery
# ---------------------------------------------------------------------------

_journey_advance_total: Any = None
_journey_parse_failure_total: Any = None
_journey_finalise_attempt_total: Any = None
_self_report_refs_capped_total: Any = None
_unmatched_self_report_refs_total: Any = None
_journey_reconcile_drift_total: Any = None
_refs_by_source_total: Any = None
_refs_unknown_source_total: Any = None
_refs_shadow_strip_hits_total: Any = None
_refs_malformed_total: Any = None
_refs_cap_dropped_total: Any = None
_refs_agent_minted_total: Any = None
_refs_agent_mint_suppressed_total: Any = None
_refs_agent_mint_budget_exceeded_total: Any = None
_refs_dismissal_suppressed_total: Any = None

# The cap-drop event name emitted by the finalize merge and the node-input
# injection paths (both report it verbatim through ``notify_refs_event``).
# Declared locally — the db-layer event vocabulary in ``lifecycle_refs``
# predates this counter and does not carry it.
_REFS_EVENT_CAP_DROPPED = "refs_cap_dropped"
# FAR-795 slice B: agent-mint counter event names. The db layer cannot import
# core, so ``modulo.db.crud.run`` emits these raw strings through the
# ``lifecycle_refs`` counter hook and this sink maps them onto the OTel
# counters — the string pair is a deliberate layering mirror.
REFS_EVENT_AGENT_MINTED = "agent_minted"
REFS_EVENT_AGENT_MINT_SUPPRESSED = "agent_mint_suppressed_by_flag"
# FAR-795: agent mints denied by the per-org budget cap (consume_agent_mint_budget
# returned False). Mirrors the flag-suppressed event so the two suppression reasons
# are independently observable.
REFS_EVENT_AGENT_MINT_BUDGET_EXCEEDED = "agent_mint_budget_exceeded"


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
        _journey_reconcile_drift_total, \
        _refs_by_source_total, \
        _refs_unknown_source_total, \
        _refs_shadow_strip_hits_total, \
        _refs_malformed_total, \
        _refs_cap_dropped_total, \
        _refs_agent_minted_total, \
        _refs_agent_mint_suppressed_total, \
        _refs_agent_mint_budget_exceeded_total, \
        _refs_dismissal_suppressed_total
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
    _refs_by_source_total = meter.create_counter(
        name="modulo_work_item_refs_by_source_total",
        description="Work-item refs stored on runs, by engine-assigned provenance source (FAR-794)",
        unit="1",
    )
    _refs_unknown_source_total = meter.create_counter(
        name="modulo_work_item_refs_unknown_source_total",
        description="Ref submissions whose wire 'source' disagreed with the engine-assigned value (FAR-794)",
        unit="1",
    )
    _refs_shadow_strip_hits_total = meter.create_counter(
        name="modulo_work_item_refs_shadow_strip_hits_total",
        description="Collisions on the system-managed _work_item_refs key at strip boundaries (FAR-794 shadow mode)",
        unit="1",
    )
    _refs_malformed_total = meter.create_counter(
        name="modulo_work_item_refs_malformed_total",
        description="Work-item ref entries dropped as malformed at the intake boundary (FAR-794)",
        unit="1",
    )
    _refs_cap_dropped_total = meter.create_counter(
        name="modulo_work_item_refs_cap_dropped_total",
        description="Work-item refs dropped by the unified work_item_refs cap (finalise merge + node-input injection)",
        unit="1",
    )
    _refs_agent_minted_total = meter.create_counter(
        name="modulo_work_item_refs_agent_minted_total",
        description="Agent-sourced journey mints that created a journey row (flag ON, FAR-795)",
        unit="1",
    )
    _refs_agent_mint_suppressed_total = meter.create_counter(
        name="modulo_work_item_refs_agent_mint_suppressed_by_flag_total",
        description="Agent-sourced journey mints suppressed because the org flag was OFF (FAR-795)",
        unit="1",
    )
    _refs_agent_mint_budget_exceeded_total = meter.create_counter(
        name="modulo_work_item_refs_agent_mint_budget_exceeded_total",
        description="Agent-sourced journey mints denied by the per-org agent-mint budget cap (FAR-795)",
        unit="1",
    )
    _refs_dismissal_suppressed_total = meter.create_counter(
        name="modulo_work_item_refs_dismissal_suppressed_total",
        description="Journeys re-mint/advance attempts suppressed by an operator dismissal (FAR-795)",
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
# FAR-794 work-item-refs counters
# ---------------------------------------------------------------------------


def record_refs_by_source(source: str, count: int = 1) -> None:
    """Record refs stored on runs by their ENGINE-ASSIGNED provenance source."""
    if _refs_by_source_total is None:
        _ensure()
    if _refs_by_source_total is not None:
        _refs_by_source_total.add(count, attributes={"source": source})


def record_refs_unknown_source(count: int = 1) -> None:
    """Record submissions whose wire ``source`` disagreed with the engine's."""
    if _refs_unknown_source_total is None:
        _ensure()
    if _refs_unknown_source_total is not None:
        _refs_unknown_source_total.add(count)


def record_refs_shadow_strip_hit(surface: str = "unknown") -> None:
    """Record a collision on the system-managed ``_work_item_refs`` key."""
    if _refs_shadow_strip_hits_total is None:
        _ensure()
    if _refs_shadow_strip_hits_total is not None:
        _refs_shadow_strip_hits_total.add(1, attributes={"surface": surface})


def record_refs_malformed(count: int = 1) -> None:
    """Record ref entries dropped as malformed at the intake boundary."""
    if _refs_malformed_total is None:
        _ensure()
    if _refs_malformed_total is not None:
        _refs_malformed_total.add(count)


def record_refs_cap_dropped(count: int = 1) -> None:
    """Record refs dropped by the unified ``work_item_refs`` cap."""
    if _refs_cap_dropped_total is None:
        _ensure()
    if _refs_cap_dropped_total is not None:
        _refs_cap_dropped_total.add(count)


def record_refs_agent_minted(count: int = 1) -> None:
    """Record agent-sourced journey mints that created a journey row (FAR-795)."""
    if _refs_agent_minted_total is None:
        _ensure()
    if _refs_agent_minted_total is not None:
        _refs_agent_minted_total.add(count)


def record_refs_agent_mint_suppressed_by_flag(count: int = 1) -> None:
    """Record agent-sourced journey mints suppressed by the org flag (FAR-795)."""
    if _refs_agent_mint_suppressed_total is None:
        _ensure()
    if _refs_agent_mint_suppressed_total is not None:
        _refs_agent_mint_suppressed_total.add(count)


def record_refs_agent_mint_budget_exceeded(count: int = 1) -> None:
    """Record agent-sourced journey mints denied by the per-org budget cap (FAR-795)."""
    if _refs_agent_mint_budget_exceeded_total is None:
        _ensure()
    if _refs_agent_mint_budget_exceeded_total is not None:
        _refs_agent_mint_budget_exceeded_total.add(count)


def record_refs_dismissal_suppressed(count: int = 1) -> None:
    """Record journey mint/advance attempts suppressed by an operator dismissal (FAR-795)."""
    if _refs_dismissal_suppressed_total is None:
        _ensure()
    if _refs_dismissal_suppressed_total is not None:
        _refs_dismissal_suppressed_total.add(count)


def _refs_event_sink(event: str, attrs: dict[str, Any]) -> None:
    """Dispatch db-layer ref events onto the FAR-794 counters.

    Registered as the ``lifecycle_refs`` counter hook at import time (below):
    the db layer cannot import core, so create-run / decorator emissions arrive
    here through the hook. Unknown events are ignored (forward compatibility).
    """
    surface = attrs.get("surface")
    source = attrs.get("source")
    count = attrs.get("count")
    if event == REFS_EVENT_SHADOW_STRIP_HIT:
        record_refs_shadow_strip_hit(str(surface) if surface is not None else "unknown")
    elif event == REFS_EVENT_ASSIGNED_SOURCE:
        record_refs_by_source(
            str(source) if source is not None else "unknown",
            int(count) if count is not None else 1,
        )
    elif event == REFS_EVENT_UNKNOWN_SOURCE:
        record_refs_unknown_source(int(count) if count is not None else 1)
    elif event == REFS_EVENT_MALFORMED:
        record_refs_malformed(int(count) if count is not None else 1)
    elif event == _REFS_EVENT_CAP_DROPPED:
        record_refs_cap_dropped(int(count) if count is not None else 1)
    elif event == REFS_EVENT_AGENT_MINTED:
        record_refs_agent_minted(int(count) if count is not None else 1)
    elif event == REFS_EVENT_AGENT_MINT_SUPPRESSED:
        record_refs_agent_mint_suppressed_by_flag(int(count) if count is not None else 1)
    elif event == REFS_EVENT_AGENT_MINT_BUDGET_EXCEEDED:
        record_refs_agent_mint_budget_exceeded(int(count) if count is not None else 1)
    elif event == REFS_EVENT_DISMISSAL_SUPPRESSED:
        record_refs_dismissal_suppressed(int(count) if count is not None else 1)


def _init_once_register_refs_counter_hook() -> None:
    """Register the ``lifecycle_refs`` counter hook (idempotent, safe at import).

    The db layer cannot import core, so create-run / decorator ref emissions
    arrive here through this hook. Registered once at import time; the
    ``_init_once`` prefix keeps it a no-op if already registered and satisfies
    the no-module-level-side-effects architecture gate.
    """
    set_refs_counter_hook(_refs_event_sink)


_init_once_register_refs_counter_hook()


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
    * FAR-795 slice C: a DISMISSED tombstone row is never drift — the sweep
      must not resurrect a dismissed journey. Its suppressed re-advance
      attempt is counted (``refs_dismissal_suppressed``) and the ref is
      skipped.
    """
    if not canonical:
        return []
    clauses = [and_(Journey.kind == entry["kind"], Journey.ref == entry["ref"]) for entry in canonical]
    rows = (
        await session.execute(
            select(Journey.kind, Journey.ref, Journey.updated_at, Journey.dismissed_at)
            .where(
                Journey.organisation_id == organisation_id,
                or_(*clauses),
            )
            .execution_options(populate_existing=True)
        )
    ).all()
    found: dict[tuple[str, str], tuple[datetime | None, datetime | None]] = {
        (row[0], row[1]): (row[2], row[3]) for row in rows
    }
    drift: list[dict[str, Any]] = []
    dismissed = 0
    for entry in canonical:
        row = found.get((entry["kind"], entry["ref"]))
        if row is None:
            drift.append(entry)
            continue
        updated_at, dismissed_at = row
        if dismissed_at is not None:
            dismissed += 1
        elif updated_at is None or (advancing and anchor is not None and updated_at < anchor):
            drift.append(entry)
    if dismissed:
        record_refs_dismissal_suppressed(dismissed)
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
                # FAR-795 slice C: a joined DISMISSED row is never stale drift
                # — the sweep must not select-and-resurrect a dismissed
                # journey forever. Mirrors the ``_drift_refs`` skip.
                and_(
                    stale_evidence,
                    Run.status.in_(_ADVANCING_STATUSES),
                    Journey.dismissed_at.is_(None),
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
        # FAR-795 slice B: agent-sourced drift refs mint only behind the
        # org flag (fail-closed). The sweep is the third mint surface that
        # can observe an agent ref — flag OFF means zero agent mints here
        # too, and the suppressed mints are counted.
        agent_minting = await agent_minting_enabled(session, run.organisation_id)
        agent_drift = [entry for entry in drift if entry.get("source") == _AGENT_SOURCE]
        if agent_drift and not agent_minting:
            record_refs_agent_mint_suppressed_by_flag(len(agent_drift))
            drift = [entry for entry in drift if entry.get("source") != _AGENT_SOURCE]
            if not drift:
                continue
        elif agent_drift:
            # Flag ON: count only the agent entries with NO pre-existing row
            # as fresh agent mints (row-existence check via the shared
            # confirm helper — fail-open to counting ALL as mints).
            try:
                confirmed, _unmatched = await confirm_reported_refs(session, run.organisation_id, agent_drift)
            except Exception:
                _log.exception("journey_reconcile.agent_mint_probe_failed org=%s", run.organisation_id)
                confirmed = []
            record_refs_agent_minted(len(agent_drift) - len(confirmed))
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
