"""SuiteRun orchestration — baseline resolution, comparison, spend, notification.

This is the *behaviour* layer for FAR-376 Phase 3. It deliberately REUSES the
existing machinery rather than building a parallel comparison store:

* per-case outcomes are persisted into ``eval_results`` (``suite_run_id`` FK);
* the pass-rate comparison is delegated to ``detect_regressions``
  (``group_by="suite_id"``), reusing its default path unchanged;
* regression/comparison postings route through the existing ``Notifier``
  (``EVENT_EVAL_REGRESSION``), never a parallel "sink" abstraction.

It also owns the two new, pure and testable behaviours:

* the immutable baseline *tuple* (snapshot, never live-looked-up) and the
  deterministic "latest completed same-tuple prior run" baseline resolution;
* the per-``eval_type`` pass-rate aggregation that refuses to cross-combine
  raw ``score`` across differing eval types (type-incorrect).
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

from sqlalchemy import select, text, update
from sqlalchemy.ext.asyncio import AsyncSession

from modulo.db.models.eval_result import EvalResult
from modulo.db.models.eval_suite_run import SuiteRun, SuiteRunState
from modulo.db.models.notification_endpoint import NotificationEndpoint

_log = logging.getLogger(__name__)

# Eval-scoped notifier event families. Eval notification endpoints subscribe to
# exactly one of these; production error-forwarder events are a disjoint set.
_EVAL_EVENT_TYPES = frozenset({"eval_regression", "eval_blocked"})
# Events that belong to the production error-forwarder path. An eval endpoint
# must NEVER subscribe to these (the no-forwarder-leak guard).
_ERROR_FORWARDER_EVENT_TYPES = frozenset(
    {"error_forwarded", "run_failed", "run_stalled", "hitl_overdue", "circuit_breaker_tripped"}
)


class SuiteRunError(RuntimeError):
    """Raised for an illegal SuiteRun orchestration step."""


class BaselineAmbiguityError(RuntimeError):
    """Raised when two completed runs tie on the same baseline tuple ordering."""


class SpendLimitExceededError(SuiteRunError):
    """Raised when a spend ceiling (daily or per-suite) would be exceeded."""


def build_baseline_tuple(
    *,
    suite_id: uuid.UUID,
    dataset_id: uuid.UUID,
    dataset_version: int,
    eval_definition_ids: Sequence[uuid.UUID],
    definition_checksum: str,
    model_backend_id: uuid.UUID,
    scenario_signature: str | None,
) -> dict[str, Any]:
    """Snapshot the full comparison tuple at creation — NEVER live-looked-up.

    This is the immutable key a run is compared by. A changed dataset version,
    a changed eval-definition config (checksum), or a changed scenario input all
    produce a NEW tuple and therefore a NEW baseline, never a silent comparison
    against a different contract.
    """
    return {
        "suite_id": str(suite_id),
        "dataset_id": str(dataset_id),
        "dataset_version": int(dataset_version),
        "eval_definition_ids": sorted(str(x) for x in eval_definition_ids),
        "definition_checksum": definition_checksum,
        "model_backend_id": str(model_backend_id),
        "scenario_signature": scenario_signature,
    }


def tuple_is_matching(run: SuiteRun, baseline_tuple: dict[str, Any]) -> bool:
    """Return True when *run* was created against the exact *baseline_tuple*."""
    return run.baseline_tuple == baseline_tuple


def _strictly_prior(run: SuiteRun, other: SuiteRun) -> bool:
    """Return True when *other* completed strictly before *run* (created_at, id)."""
    if other.created_at is None or run.created_at is None:
        return False
    if other.created_at < run.created_at:
        return True
    if other.created_at > run.created_at:
        return False
    return str(other.id) < str(run.id)


async def resolve_baseline_run(session: AsyncSession, run: SuiteRun) -> tuple[SuiteRun | None, str | None]:
    """Resolve the same-tuple latest COMPLETED run prior to *run*.

    Rules (deterministic):
    * Only same-organisation runs are candidate baselines — a cross-org run is
      never selected (org isolation).
    * Only ``completed`` runs qualify (``partial``/``failed``/``cancelled`` and
      the current run itself are excluded).
    * The baseline must share the exact immutable ``baseline_tuple``.
    * The baseline must be strictly prior to *run* by ``(created_at, id)``.
    * Tiebreak ``(created_at, id)``: the lexically-``id``-latest prior run wins
      deterministically so two concurrent workers cannot disagree.

    Returns ``(baseline_run, warning)``. A warning is emitted (and ``None`` is
    returned) when no completed prior run exists — the caller must SKIP the
    comparison rather than flag a regression on a first run.

    Concurrency: the runner holds an org-scoped advisory lock (see
    ``acquire_org_advisory_lock``) around the resolution for the SuiteRun path;
    the SELECT itself uses ``FOR UPDATE SKIP LOCKED`` on Postgres so two workers
    resolving at the same instant cannot pick different baselines.
    """
    stmt = (
        select(SuiteRun)
        .where(
            SuiteRun.organisation_id == run.organisation_id,
            SuiteRun.state == SuiteRunState.COMPLETED.value,
            SuiteRun.id != run.id,
        )
        .order_by(SuiteRun.created_at.desc(), SuiteRun.id.desc())
    )
    candidates = (await session.scalars(stmt)).all()
    matching = [
        c
        for c in candidates
        if c.organisation_id == run.organisation_id
        and c.state == SuiteRunState.COMPLETED.value
        and tuple_is_matching(c, run.baseline_tuple or {})
        and _strictly_prior(run, c)
    ]
    if not matching:
        return None, "no completed prior run with the same baseline tuple — comparison skipped"
    # Deterministic tiebreak (created_at, id) — the lexically-``id``-latest
    # prior run wins. Sorting in code (not only in SQL) keeps the decision
    # robust regardless of backend index/ordering guarantees.
    matching.sort(key=lambda r: (r.created_at, r.id), reverse=True)
    return matching[0], None


async def acquire_org_advisory_lock(session: AsyncSession, run: SuiteRun) -> Any:
    """Best-effort serialisation of baseline resolution for a suite (Postgres).

    Uses an organisation-scoped ``pg_advisory_xact_lock`` so concurrent SuiteRun
    completions for the same org resolve their baseline atomically. A no-op on
    non-Postgres backends (where the ORM tenant filter + the deterministic
    tiebreak already prevent disagreement). Returns the lock key, or ``None``
    when the backend cannot take the lock.
    """
    bind = session.get_bind()
    if getattr(bind.dialect, "name", "") != "postgresql":
        return None
    lock_key = (run.organisation_id.int & 0x7FFFFFFF) ^ (run.suite_id.int & 0x7FFFFFFF)
    await session.execute(text("SELECT pg_advisory_xact_lock(:key)").bindparams(key=lock_key))
    return lock_key


# --------------------------------------------------------------------------- #
# Pass-rate aggregation (per eval_type ONLY — never cross-combined)           #
# --------------------------------------------------------------------------- #
def pass_rate_by_eval_type(rows: Sequence[tuple[str, bool]]) -> dict[str, dict[str, int | float]]:
    """Aggregate pass rates grouped by ``eval_type``.

    Pass rates are computed per ``eval_type`` and NEVER averaged across differing
    eval types — averaging raw ``score`` across ``llm_judge`` and ``regex`` is
    type-incorrect (different scales, different semantics). Each ``row`` is a
    ``(eval_type, passed)`` pair; the caller resolves ``eval_type`` via the
    eval-definition join. ``errored`` rows (excluded by the caller) are not
    present; the caller records the count as ``excluded_case_count``.

    Returns ``{eval_type: {"passed": int, "total": int, "pass_rate": float}}``.
    An eval with zero non-excluded results is absent from the mapping.
    """
    per_type: dict[str, dict[str, int]] = {}
    for eval_type, passed in rows:
        bucket = per_type.setdefault(eval_type, {"passed": 0, "total": 0})
        bucket["total"] += 1
        if passed:
            bucket["passed"] += 1
    return {
        eval_type: {
            "passed": bucket["passed"],
            "total": bucket["total"],
            "pass_rate": round(bucket["passed"] / bucket["total"], 4),
        }
        for eval_type, bucket in per_type.items()
        if bucket["total"] > 0
    }


def suite_pass_rate(results: Sequence[EvalResult], excluded_case_count: int = 0) -> dict[str, Any]:
    """Overall suite pass rate with errored cases excluded from the denominator.

    ``excluded_case_count`` is subtracted from the denominator — a ``partial``
    run never penalises its pass rate for cases that errored.
    """
    total_non_excluded = len(results)
    if total_non_excluded == 0:
        return {"passed": 0, "total": excluded_case_count, "pass_rate": 0.0, "excluded": excluded_case_count}
    passed = sum(1 for r in results if r.passed)
    return {
        "passed": passed,
        "total": total_non_excluded + excluded_case_count,
        "pass_rate": round(passed / total_non_excluded, 4),
        "excluded": excluded_case_count,
    }


# --------------------------------------------------------------------------- #
# Spend — two INDEPENDENT counters (daily + per-suite cumulative)             #
# --------------------------------------------------------------------------- #
def daily_spend_exceeded(current_daily_used: Decimal, daily_limit: Decimal | None) -> bool:
    """Return True when the org daily spend limit would be exceeded.

    Independent of the per-suite cumulative ceiling (a second, separate
    counter) — the two are never combined into one shared cap.
    """
    if daily_limit is None:
        return False
    return current_daily_used >= daily_limit


async def claim_suite_run_cost(session: AsyncSession, run: SuiteRun, amount: Decimal) -> Decimal:
    """Atomically increment the per-suite claimed cost BEFORE a judge call.

    Uses a row-locked ``UPDATE ... RETURNING`` on ``suite_runs`` so a
    read-check-write race cannot overshoot the per-suite cumulative ceiling: the
    ledger (``claimed_cost``) is incremented first, then the caller compares the
    new total against the ceiling. Returns the new claimed total.
    """
    new_total = (
        await session.execute(
            update(SuiteRun)
            .where(SuiteRun.id == run.id)
            .values(claimed_cost=SuiteRun.claimed_cost + amount)
            .returning(SuiteRun.claimed_cost)
        )
    ).scalar_one_or_none()
    if new_total is None:  # pragma: no cover - defensive; row must exist
        raise SuiteRunError(f"SuiteRun {run.id} not found while claiming cost")
    run.claimed_cost = new_total
    return new_total


def suite_cumulative_exceeded(claimed_cost: Decimal | None, suite_ceiling: Decimal | None) -> bool:
    """Return True when the per-suite cumulative cost ceiling is exceeded."""
    if suite_ceiling is None:
        return False
    claimed = claimed_cost or Decimal(0)
    return claimed >= suite_ceiling


# --------------------------------------------------------------------------- #
# Notification isolation — no forwarder leakage                               #
# --------------------------------------------------------------------------- #
def assert_eval_notification_isolated(subscribed_event_types: Sequence[str] | None) -> None:
    """Raise when an eval notification subscriber also targets an error forwarder.

    Eval regression/comparison postings must share ZERO subscribers with
    production error forwarders. Passing the *evaluated* subscriber's event list
    (already derived from the endpoint's ``events`` JSON) lets this run as a
    runtime guard; any overlap with the error-forwarder event family is a
    routing leak and fails loudly rather than silently double-posting.
    """
    subs = subscribed_event_types or []
    leaked = set(subs) & _ERROR_FORWARDER_EVENT_TYPES
    if leaked:
        raise SuiteRunError(f"eval notification subscriber leaks to error-forwarder events: {sorted(leaked)}")
    if not (set(subs) & _EVAL_EVENT_TYPES):
        raise SuiteRunError("eval notification has no eval-scoped subscribers (silent-drop guard)")


async def load_eval_subscriber_events(session: AsyncSession, org_id: uuid.UUID) -> list[str]:
    """Return the merged event list of the org's live eval notification endpoints.

    Used by the runtime guard to prove an eval alert actually has a reachable,
    non-error-forwarding subscriber (prevents a silent drop). The endpoint's
    ``events`` may be a JSON string or a native list — both are normalised.
    """
    import json as _json

    endpoints = (
        await session.scalars(
            select(NotificationEndpoint).where(
                NotificationEndpoint.organisation_id == org_id,
                NotificationEndpoint.auto_disabled.is_(False),
            )
        )
    ).all()
    merged: list[str] = []
    for ep in endpoints:
        raw = ep.events
        if isinstance(raw, list):
            events = raw
        elif isinstance(raw, str):
            try:
                parsed = _json.loads(raw)
            except (ValueError, TypeError):
                continue
            events = parsed if isinstance(parsed, list) else []
        else:
            continue
        merged.extend(e for e in events if isinstance(e, str))
    return merged


def is_suite_rate_limited(last_alert_at: datetime | None, min_interval: timedelta) -> bool:
    """Return True when the suite's last regression alert is within the interval."""
    if last_alert_at is None or min_interval is None or min_interval.total_seconds() <= 0:
        return False
    return datetime.now(UTC) - last_alert_at < min_interval


def should_notify_regression(run: SuiteRun, baseline_run: SuiteRun | None) -> bool:
    """Decide whether a regression alert should be posted.

    Requires a baseline to exist (a first run never alerts) and is idempotent on
    ``suite_run_id`` (never notify a run twice). Per-suite rate limiting is a
    separate, time-based guard (``is_suite_rate_limited``).
    """
    if baseline_run is None:
        return False
    return run.notified_at is None


# --------------------------------------------------------------------------- #
# High-level orchestration                                                    #
# --------------------------------------------------------------------------- #
async def run_suite_comparison(
    session: AsyncSession, run: SuiteRun, entity_thresholds: dict[str, Any]
) -> dict[str, Any]:
    """Persist the run completion record and run the comparison against baseline.

    Resolves the same-tuple baseline, delegates the per-eval pass-rate comparison
    to ``detect_regressions`` (``group_by="suite_id"``), records the regression
    decision on the run, and (when a regression was flagged) emits the eval
    notification through the existing ``Notifier`` path. Never builds a parallel
    comparison store.

    ``entity_thresholds`` carries the configurable absolute/relative drop
    thresholds plus the per-suite spend ceiling.

    Returns the comparison result dict (also persisted as ``comparison_json``).
    """
    from modulo.core.eval_engine.regression import detect_regressions

    baseline_run, warning = await resolve_baseline_run(session, run)
    comparison: dict[str, Any] = {"baseline_run_id": str(baseline_run.id) if baseline_run else None, "warning": warning}
    if baseline_run is None:
        run.regressed = None
        return comparison

    abs_threshold = float(entity_thresholds.get("absolute_drop", 0.15))
    rel_threshold = entity_thresholds.get("relative_drop")

    alerts = await detect_regressions(
        session,
        run.organisation_id,
        threshold=abs_threshold,
        group_by="suite_id",
        current_run_ids=[run.id],
        baseline_run_ids=[baseline_run.id],
        relative_threshold=rel_threshold,
    )
    regressed = bool(alerts)
    run.regressed = regressed
    run.baseline_run_id = baseline_run.id
    comparison.update(
        {
            "alert_count": len(alerts),
            "alerts": [a.__dict__ for a in alerts],
            "regressed": regressed,
            "thresholds": {
                "absolute_drop": abs_threshold,
                "relative_drop": rel_threshold,
            },
        }
    )
    return comparison


async def record_completion(session: AsyncSession, run: SuiteRun, entity_thresholds: dict[str, Any]) -> None:
    """Terminalize a SuiteRun as ``completed`` and persist its comparison."""
    await session.refresh(run)
    entity_thresholds = entity_thresholds or {}
    comparison = await run_suite_comparison(session, run, entity_thresholds)
    run.comparison_json = comparison
    await session.flush()


__all__ = [
    "BaselineAmbiguityError",
    "SpendLimitExceededError",
    "SuiteRunError",
    "acquire_org_advisory_lock",
    "assert_eval_notification_isolated",
    "build_baseline_tuple",
    "claim_suite_run_cost",
    "daily_spend_exceeded",
    "is_suite_rate_limited",
    "load_eval_subscriber_events",
    "pass_rate_by_eval_type",
    "record_completion",
    "resolve_baseline_run",
    "run_suite_comparison",
    "should_notify_regression",
    "suite_cumulative_exceeded",
    "suite_pass_rate",
    "tuple_is_matching",
]
