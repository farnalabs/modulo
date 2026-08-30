"""The cost probe — the verification canary (spec §4.7, PR A2).

A scheduled job on the SAQ system-worker cadence (every 5 minutes,
``retries=0``, ``unique=True``). For each org it samples the N=50 most recent
terminal runs, compares ``total_cost_usd`` to the sum of the breakdown's
component amounts (the HARD ``total == sum`` invariant), skips marker-bearing
(total-clamped) runs with a counter, and asserts org-ledger-row existence /
sufficiency as a WATCH signal (single batched query; per-date grouping in
Python; clamped-day skip).

The canonical rollback trigger — the probe rule (≥5 sampled runs AND ≥2
DISTINCT mismatching runs in ≥2 CONSECUTIVE cadences, persisted across
deploys/restarts) and the duplicate-terminal flood (>5 DISTINCT runs in 10
minutes, with the post-deploy/worker-restart cooldown) — is the hard-gate
input surface. The heartbeat gauge (``modulo_cost_probe_last_success_ts``)
turns a silently dead probe into a stale alert.

The probe is a CANARY, not an audit: it samples rather than exhaustively
checks, and its mechanism count is BUDGETED and must not grow in v1.
"""

from __future__ import annotations

import asyncio
import logging
import time
import uuid
from collections.abc import Callable
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Any

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from modulo.core.cost_controller.breakdown.metrics import (
    record_probe_clamped_skip,
    record_probe_mismatch_runs,
    record_probe_missing_ledger_row,
    record_probe_total_eq_mismatch,
    set_probe_last_success_ts,
)
from modulo.core.cost_controller.system_config import (
    acquire_kv_lock,
    read_system_config,
    write_system_config,
)
from modulo.db.models.daily_run_count import OrgDailyRunCount
from modulo.db.models.organisation import Organisation
from modulo.db.models.run import Run
from modulo.db.rls import set_rls_org

_log = logging.getLogger(__name__)

__all__ = [
    "PROBE_CADENCE_SECONDS",
    "run_probe",
    "set_duplicate_terminal_cooldown",
]

# The probe cadence + windows (5-min cadence; stale = 3x; adjacency reset = 2x).
PROBE_CADENCE_SECONDS = 5 * 60
PROBE_STALE_WINDOW_SECONDS = 3 * PROBE_CADENCE_SECONDS  # 15 min
PROBE_ADJACENCY_GAP_SECONDS = 2 * PROBE_CADENCE_SECONDS  # >10 min resets the consecutive chain

# Sample size + the canonical-trigger thresholds (spec §4.7).
PROBE_SAMPLE_SIZE = 50
PROBE_MIN_SAMPLE = 5
PROBE_MIN_DISTINCT_MISMATCHES = 2

# Duplicate-terminal flood thresholds + the post-deploy/worker-restart cooldown.
FLOOD_WINDOW_SECONDS = 10 * 60
FLOOD_MIN_DISTINCT_RUNS = 5
FLOOD_COOLDOWN_SECONDS = 15 * 60

# The canonical terminal-status set (spec §4.2) — the probe sample predicate.
PROBE_TERMINAL_STATUSES = (
    "complete",
    "failed",
    "cancelled",
    "eval_failed",
    "stalled",
    "budget_exceeded",
    "router_no_match",
    "cost_ceiling_exceeded",
    "compensation_failed",
)

# Per-org statement/query timeout (one stalled org cannot block the cadence).
_ORG_STATEMENT_TIMEOUT_MS = 5000

# EXPLAIN self-check runs once per process.
_explain_checked = False

_SAMPLE_QUERY_EXPLAIN_TEMPLATE = (
    "SELECT id, total_cost_usd, cost_breakdown, started_at, ledger_written, ledger_refused_at "
    "FROM runs "
    "WHERE organisation_id = :org_id "
    "AND status IN ('complete', 'failed', 'cancelled', 'eval_failed', 'stalled', "
    "'budget_exceeded', 'cost_ceiling_exceeded', 'router_no_match') "
    "AND cost_breakdown IS NOT NULL "
    "ORDER BY started_at DESC "
    "LIMIT 50"
)


def _is_marker_run(run: Any) -> bool:
    """True when the run's breakdown carries the ``total_clamped`` marker."""
    breakdown = run.cost_breakdown
    if not isinstance(breakdown, list):
        return False
    return any(isinstance(entry, dict) and entry.get("total_clamped") for entry in breakdown)


def _evaluate_run(run: Any) -> str:
    """Compare ``total_cost_usd`` to the summed component amounts.

    Returns ``"ok"`` / ``"mismatch"`` / ``"clamped"`` (marker-bearing run) /
    ``"malformed"`` (a malformed ``amount_usd`` string or a NULL total — the
    run is dropped from the sample, never counted as a mismatch).
    """
    if _is_marker_run(run):
        return "clamped"
    total = run.total_cost_usd
    breakdown = run.cost_breakdown
    if total is None or not isinstance(breakdown, list):
        return "malformed"
    try:
        total_dec = Decimal(str(total))
    except (ValueError, TypeError, ArithmeticError):
        return "malformed"
    summed = Decimal(0)
    for entry in breakdown:
        if not isinstance(entry, dict):
            continue
        amount = entry.get("amount_usd")
        if amount is None:
            continue
        try:
            summed += Decimal(str(amount))
        except (ValueError, TypeError, ArithmeticError):
            return "malformed"
    return "mismatch" if total_dec != summed else "ok"


async def _sample_runs(session: AsyncSession, org_id: uuid.UUID) -> list[Any]:
    """The N=50 most recent terminal runs with a breakdown (per org)."""
    result = await session.execute(
        select(
            Run.id,
            Run.total_cost_usd,
            Run.cost_breakdown,
            Run.started_at,
            Run.ledger_written,
            Run.ledger_refused_at,
        )
        .where(
            Run.organisation_id == org_id,
            Run.status.in_(PROBE_TERMINAL_STATUSES),
            Run.cost_breakdown.isnot(None),
        )
        .order_by(Run.started_at.desc())
        .limit(PROBE_SAMPLE_SIZE)
    )
    return list(result.all())


async def _assert_sample_query_index(session: AsyncSession, org_id: uuid.UUID) -> None:
    """EXPLAIN the sample query under ``SET enable_seqscan=off`` (once per process).

    The gate asserts INDEX USE for ``ix_runs_probe``, NOT "no sequential scan"
    (at small org scale a seq scan legitimately wins). The ``RESET`` runs in a
    ``finally`` so a failed EXPLAIN never leaves the session with seqscan
    disabled.
    """
    global _explain_checked
    if _explain_checked:
        return
    try:
        await session.execute(text("SET enable_seqscan = off"))
        try:
            bound_query = text("EXPLAIN " + _SAMPLE_QUERY_EXPLAIN_TEMPLATE)
            plan_rows = await session.execute(bound_query, {"org_id": str(org_id)})
            plan = "\n".join(str(r[0]) for r in plan_rows.all())
            if "ix_runs_probe" not in plan:
                _log.warning("cost_probe.sample_index_not_used", extra={"plan": plan[:800]})
        finally:
            await session.execute(text("RESET enable_seqscan"))
        _explain_checked = True
    except asyncio.CancelledError:
        raise
    except Exception:
        _log.exception("cost_probe.explain_check_failed")


async def _org_row_watch(session: AsyncSession, org_id: uuid.UUID, runs: list[Any]) -> int:
    """The org-row existence WATCH (spec §4.7).

    For each sampled run with ``ledger_written = true AND ledger_refused_at IS
    NULL``, asserts the org ledger row EXISTS for the run's date and is
    SUFFICIENT (``total_spend_usd >= sum of the sampled runs for that date``).
    ONE batched query per org; per-date grouping in Python; clamped days are
    skipped (a clamped day is a known anomaly, not a missing-row signal).
    Returns the number of dates that are absent or insufficient.
    """
    watched = [r for r in runs if r.ledger_written and r.ledger_refused_at is None]
    if not watched:
        return 0
    by_date: dict[date, Decimal] = {}
    for r in watched:
        started = r.started_at
        if started is None:
            continue
        d = started.astimezone(UTC).date()
        try:
            by_date[d] = by_date.get(d, Decimal(0)) + Decimal(str(r.total_cost_usd or 0))
        except (ValueError, TypeError, ArithmeticError):
            continue
    if not by_date:
        return 0
    dates = sorted(by_date)
    result = await session.execute(
        select(
            OrgDailyRunCount.run_date,
            OrgDailyRunCount.total_spend_usd,
            OrgDailyRunCount.clamped,
        ).where(
            OrgDailyRunCount.organisation_id == org_id,
            OrgDailyRunCount.team_id.is_(None),
            OrgDailyRunCount.run_date.in_(dates),
        )
    )
    rows = {r.run_date: r for r in result.all()}
    missing = 0
    for d in dates:
        row = rows.get(d)
        if row is None:
            missing += 1
            continue
        if not row.clamped and row.total_spend_usd is not None and Decimal(str(row.total_spend_usd)) < by_date[d]:
            missing += 1
    return missing


async def _evaluate_trigger(
    session: AsyncSession,
    org_id: uuid.UUID,
    runs: list[Any],
    mismatches: list[str],
) -> bool:
    """The CANONICAL probe rule — the ≥5 + 2-distinct x 2-consecutive rule.

    Persists ``probe_state:<org_id>`` on the GLOBAL ``system_config`` (NO RLS)
    under the advisory-lock read-modify-write (single-instance). Temporal
    adjacency: a persisted mismatch list whose ``last_cadence_at`` is older
    than 2x the cadence does NOT count toward the next cadence's "consecutive"
    — an outage gap resets the chain. The stored run-ids are DIAGNOSTIC ONLY;
    only the per-cadence COUNT matters.
    """
    sampled_count = len(runs)
    distinct = len(set(mismatches))
    key = f"probe_state:{org_id}"
    blob = await read_system_config(session, key)
    now = datetime.now(UTC)
    prior_count = 0
    last_cadence_at: datetime | None = None
    if isinstance(blob, dict):
        raw_ts = blob.get("last_cadence_at")
        if raw_ts:
            try:
                last_cadence_at = datetime.fromisoformat(str(raw_ts))
            except (ValueError, TypeError):
                last_cadence_at = None
        prior_list = blob.get("last_cadence_mismatch_runs")
        if isinstance(prior_list, list):
            prior_count = len({str(x) for x in prior_list})

    fired = False
    if sampled_count >= PROBE_MIN_SAMPLE and distinct >= PROBE_MIN_DISTINCT_MISMATCHES:
        adjacent = (
            last_cadence_at is not None and (now - last_cadence_at).total_seconds() <= PROBE_ADJACENCY_GAP_SECONDS
        )
        if adjacent and prior_count >= PROBE_MIN_DISTINCT_MISMATCHES:
            fired = True

    await acquire_kv_lock(session, key)
    await write_system_config(
        session,
        key,
        {"last_cadence_mismatch_runs": mismatches, "last_cadence_at": now.isoformat()},
    )
    return fired


async def _duplicate_flood_trigger(session: AsyncSession) -> bool:
    """The duplicate-terminal flood hard-gate input (spec §4.7).

    >5 DISTINCT runs logged ``duplicate_terminal`` within 10 minutes, SKIPPED
    while ``duplicate_terminal_suppressed_until`` (system_config, NO RLS) is in
    the future (the post-deploy/worker-restart cooldown). The cooldown
    suppresses the TRIGGER only — never the log or the counter; a stale
    persisted value from a crashed worker EXPIRES by wall-clock comparison.
    """
    now = datetime.now(UTC)
    suppressed_raw = await read_system_config(session, "duplicate_terminal_suppressed_until")
    if suppressed_raw:
        try:
            suppressed = datetime.fromisoformat(str(suppressed_raw))
            if now < suppressed:
                return False
        except (ValueError, TypeError):
            pass
    events = await read_system_config(session, "duplicate_terminal_events")
    cutoff = now - timedelta(seconds=FLOOD_WINDOW_SECONDS)
    distinct: set[str] = set()
    if isinstance(events, list):
        for event in events:
            if not isinstance(event, dict):
                continue
            ts = event.get("ts")
            try:
                parsed = datetime.fromisoformat(str(ts)) if ts else None
            except (ValueError, TypeError):
                parsed = None
            if parsed is not None and parsed >= cutoff and event.get("run_id"):
                distinct.add(str(event["run_id"]))
    return len(distinct) > FLOOD_MIN_DISTINCT_RUNS


async def _probe_org(session_factory: Callable[[], Any], org_id: uuid.UUID) -> None:
    """Sample ONE org (per-org exception isolation; per-org query timeout)."""
    async with session_factory() as session, session.begin():
        await set_rls_org(session, org_id)
        await session.execute(
            text("SELECT set_config('statement_timeout', :ms, true)"),
            {"ms": str(int(_ORG_STATEMENT_TIMEOUT_MS))},
        )
        await _assert_sample_query_index(session, org_id)
        runs = await _sample_runs(session, org_id)

        mismatches: list[str] = []
        clamped_skips = 0
        for run in runs:
            try:
                outcome = _evaluate_run(run)
            except Exception:
                _log.warning("cost_probe.run_eval_failed", extra={"run_id": str(getattr(run, "id", "?"))})
                continue
            if outcome == "clamped":
                clamped_skips += 1
            elif outcome == "mismatch":
                mismatches.append(str(run.id))

        missing_rows = await _org_row_watch(session, org_id, runs)

        # The FIVE signals (four counters + the heartbeat gauge) + the sample log.
        record_probe_mismatch_runs(len(mismatches))
        if mismatches:
            record_probe_total_eq_mismatch()
        if clamped_skips:
            record_probe_clamped_skip(clamped_skips)
        if missing_rows:
            record_probe_missing_ledger_row(missing_rows)

        probe_rule_fired = await _evaluate_trigger(session, org_id, runs, mismatches)
        flood_fired = await _duplicate_flood_trigger(session)
        if probe_rule_fired or flood_fired:
            _log.error(
                "cost_probe.rollback_trigger",
                extra={
                    "org_id": str(org_id),
                    "probe_rule": probe_rule_fired,
                    "duplicate_flood": flood_fired,
                    "mismatch_run_ids": mismatches,
                },
            )

        _log.info(
            "cost_probe_sample",
            extra={
                "org_id": str(org_id),
                "sample_size": len(runs),
                "mismatches": len(mismatches),
                "clamped_skips": clamped_skips,
                "missing_ledger_rows": missing_rows,
                "heartbeat_ts": time.time(),
            },
        )


async def run_probe(session_factory: Callable[[], Any]) -> dict[str, Any]:
    """The probe — one cadence across all orgs.

    Org enumeration + the ≥1-org gate run OUTSIDE RLS (system context; the app
    role owns ``organisations``). Each org runs in its own ``try/except`` so one
    org's RLS/DB failure cannot abort the whole sample. The heartbeat advances
    on ANY cadence where ≥1 org succeeded (a 0-eligible-run org counts as
    success); it does NOT advance on an ALL-ORGS-FAILED cadence or a ZERO-ORG
    install.
    """
    summary: dict[str, Any] = {
        "orgs_enumerated": 0,
        "orgs_succeeded": 0,
        "orgs_failed": 0,
        "advanced_heartbeat": False,
    }
    try:
        async with session_factory() as session, session.begin():
            result = await session.execute(select(Organisation.id))
            org_ids: list[uuid.UUID] = list(result.scalars().all())
    except asyncio.CancelledError:
        raise
    except Exception:
        _log.exception("cost_probe.org_enumeration_failed")
        summary["error"] = "enumeration_failed"
        return summary

    summary["orgs_enumerated"] = len(org_ids)
    if not org_ids:
        return summary

    for org_id in org_ids:
        try:
            await _probe_org(session_factory, org_id)
            summary["orgs_succeeded"] += 1
        except asyncio.CancelledError:
            raise
        except Exception:
            summary["orgs_failed"] += 1
            _log.exception("cost_probe.org_failed", extra={"org_id": str(org_id)})

    if summary["orgs_succeeded"] > 0:
        set_probe_last_success_ts(time.time())
        summary["advanced_heartbeat"] = True
    return summary


async def set_duplicate_terminal_cooldown(session_factory: Callable[[], Any]) -> None:
    """Set the 15-min flood cooldown on system_config (worker start / version change).

    Written at system-worker start so a rollout cannot auto-roll itself back
    (the first 15 minutes of duplicate-terminal logs from a restart/re-dispatch
    burst are suppressed as a trigger input). A stale persisted value from a
    crashed worker EXPIRES by wall-clock comparison in ``_duplicate_flood_trigger``.
    """
    until = datetime.now(UTC) + timedelta(seconds=FLOOD_COOLDOWN_SECONDS)
    try:
        async with session_factory() as session, session.begin():
            await write_system_config(session, "duplicate_terminal_suppressed_until", until.isoformat())
    except asyncio.CancelledError:
        raise
    except Exception:
        _log.exception("cost_probe.cooldown_set_failed")
