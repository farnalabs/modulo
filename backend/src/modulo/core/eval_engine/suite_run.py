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

from modulo.core.notifier import EVENT_EVAL_REGRESSION
from modulo.db.models.eval_definition import EvalDefinition
from modulo.db.models.eval_result import EvalResult
from modulo.db.models.eval_suite import EvalSuite
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


# --------------------------------------------------------------------------- #
# Eval-definition versioning (FAR-382)                                        #
# --------------------------------------------------------------------------- #
async def resolve_eval_definition_version(
    session: AsyncSession,
    org_id: uuid.UUID,
    eval_id: uuid.UUID,
    pinned_version: int | None = None,
) -> int:
    """Resolve which eval-definition version a lookup should target.

    FAR-382: a version can be pinned on an ``EvalResult`` (its
    ``eval_definition_version`` snapshot) or passed by a caller. When a version
    IS pinned, it is returned unchanged. When NO version is pinned (the
    NULL-version lookup for legacy rows), the definition's CURRENT (latest)
    ``version`` is resolved from the DB — a legacy result is treated as having
    been scored under the definition's latest version, never as a mystery.

    Raises ``SuiteRunError`` when the definition does not exist (or belongs to a
    different org), so versioning never silently resolves against a foreign row.
    """
    if pinned_version is not None:
        return pinned_version
    result = await session.execute(
        select(EvalDefinition).where(
            EvalDefinition.id == eval_id,
            EvalDefinition.organisation_id == org_id,
        )
    )
    row = result.scalar_one_or_none()
    if row is None:
        raise SuiteRunError(f"eval definition {eval_id} not found while resolving version for org {org_id}")
    return row.version


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


async def resolve_baseline_run(
    session: AsyncSession,
    run: SuiteRun,
    baseline_window: int | None = None,
) -> tuple[list[SuiteRun], str | None]:
    """Resolve the same-tuple COMPLETED run(s) prior to *run* (FAR-379 window).

    Rules (deterministic):
    * Only same-organisation runs are candidate baselines — a cross-org run is
      never selected (org isolation).
    * Only ``completed`` runs qualify (``partial``/``failed``/``cancelled`` and
      the current run itself are excluded).
    * Each baseline must share the exact immutable ``baseline_tuple``.
    * Each baseline must be strictly prior to *run* by ``(created_at, id)``.
    * ``baseline_window`` (a suite's ``EvalSuite.baseline_window``) controls how
      many of the most-recent same-tuple prior runs form the baseline. ``None``
      (or ``< 1``) keeps the single-latest behaviour; ``N`` returns the ``N``
      most-recent prior runs — the rolling "N-run baseline". A window NEVER
      disables comparison or suppresses alerting; it only widens the baseline,
      so a NULL window still resolves a single-latest baseline and alerting is
      governed by the regression signal, not by the window.
    * Tiebreak ``(created_at, id)``: the lexically-``id``-latest prior run wins
      deterministically so two concurrent workers cannot disagree.

    Returns ``(baseline_runs, warning)``. A warning is emitted (and an empty
    list returned) when no completed prior run exists — the caller must SKIP the
    comparison rather than flag a regression on a first run. The list is
    ordered most-recent-first (``(created_at, id)`` desc), so ``baseline_runs[0]``
    is the single "latest" baseline used when an alerting payload needs one.

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
        return [], "no completed prior run with the same baseline tuple — comparison skipped"
    # Deterministic tiebreak (created_at, id) — the lexically-``id``-latest
    # prior run wins. Sorting in code (not only in SQL) keeps the decision
    # robust regardless of backend index/ordering guarantees.
    matching.sort(key=lambda r: (r.created_at, r.id), reverse=True)
    if baseline_window is None or baseline_window < 1:
        return matching[:1], None
    return matching[:baseline_window], None


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
# Eval read-model — leaderboard / timeseries aggregation (FAR-378)            #
# --------------------------------------------------------------------------- #
#
# A pure read-model over the now-structured ``SuiteRun``/``eval_results`` data.
# It is deliberately LIVE aggregation over the existing tables — no parallel
# materialised view, no new "scores over time" store. The isolation invariant
# mirrors ``core/analytics``: ``modulo_app`` is NOBYPASSRLS (tenant isolation
# relies on RLS policies), so the explicit ``organisation_id = :org`` predicate
# injected into EVERY statement is the PRIMARY isolation control (RLS also
# enforces org scoping; ``set_rls_org`` stays defense-in-depth).
#
# Pass-rate discipline (the non-circular rule): pass-rates are computed from
# the ``passed`` BOOLEAN only, NEVER from the raw ``score`` column. Scores are
# not comparable across differing ``eval_type`` (an ``llm_judge`` 0.8 and a
# ``regex`` 0.6 measure different things), so every aggregation partitions by
# ``eval_type`` and rolls up by counting passes, never by averaging scores. A
# mixed-``eval_type`` axis never ranks by raw score.
EVAL_LEADERBOARD_AXES: tuple[str, ...] = ("pipeline", "node", "agent")

EVAL_LEADERBOARD_DEFAULT_DAYS = 30
EVAL_LEADERBOARD_MAX_DAYS = 365

# Suite-run outcomes count toward a leaderboard only when the run is terminal
# (``completed``/``partial``) — a ``running`` suite's per-case results are still
# in flight, so including them would make the read-model non-deterministic across
# two calls. Legacy pipeline-path rows (``suite_run_id`` NULL) carry no run
# state and are always included.
_SUITE_RUN_TERMINAL_STATES = frozenset({SuiteRunState.COMPLETED.value, SuiteRunState.PARTIAL.value})


def validate_leaderboard_axis(group_by: str) -> str:
    """Validate a leaderboard ``group_by`` against the fixed axis allowlist.

    The axis label/column expression is derived from this allowlist (see
    ``_leaderboard_axis_columns``) and never from user input, so there is no
    SQL-injection surface on the ``GROUP BY``/``SELECT`` axis.
    """
    if group_by not in EVAL_LEADERBOARD_AXES:
        raise ValueError(f"group_by must be one of {EVAL_LEADERBOARD_AXES}, got {group_by!r}")
    return group_by


def _leaderboard_axis_columns(group_by: str) -> tuple[str, str, str]:
    """Return ``(key_sql, label_sql, joins_sql)`` for a validated *group_by*.

    ``key_sql`` is the axis column; ``label_sql`` is a display expression (an
    aggregate ``MAX`` so the raw group key stays the only non-aggregate in
    ``GROUP BY``); ``joins_sql`` is the extra join needed for the label. Node
    leaderboards label by the node id because eval definitions carry no node
    name snapshot of their own.
    """
    if group_by == "pipeline":
        return "ed.pipeline_id", "MAX(p.name)", "LEFT JOIN pipelines p ON p.id = ed.pipeline_id"
    if group_by == "node":
        return "ed.node_id", "ed.node_id", ""
    if group_by == "agent":
        return "sr.model_backend_id", "MAX(mb.name)", "LEFT JOIN model_backends mb ON mb.id = sr.model_backend_id"
    raise ValueError(f"unknown leaderboard axis {group_by!r}")  # pragma: no cover - validated upstream


def _common_eval_read_conditions(
    *,
    org_id: uuid.UUID,
    since: datetime,
    eval_id: uuid.UUID | None,
    pipeline_id: uuid.UUID | None,
    node_id: uuid.UUID | None,
    model_backend_id: uuid.UUID | None,
) -> tuple[list[str], dict[str, Any]]:
    """The org-scoped WHERE fragments + params shared by leaderboard/timeseries.

    The org predicate is unconditional — the PRIMARY isolation control (RLS also
    enforces org scoping). Guardrail
    rows are excluded (``eval_type != 'guardrail'``, matching the suite-run
    consumer contract) and suite-run outcomes are restricted to terminal runs so
    the read-model is deterministic across calls.
    """
    terminal_state_list = ", ".join(f"'{s}'" for s in sorted(_SUITE_RUN_TERMINAL_STATES))
    conditions: list[str] = [
        "er.organisation_id = :org_id",
        "ed.organisation_id = :org_id",
        "ed.eval_type != 'guardrail'",
        "er.evaluated_at >= :since",
        f"(er.suite_run_id IS NULL OR sr.state IN ({terminal_state_list}))",
    ]
    params: dict[str, Any] = {"org_id": org_id, "since": since}
    if eval_id is not None:
        conditions.append("er.eval_id = :eval_id")
        params["eval_id"] = eval_id
    if pipeline_id is not None:
        conditions.append("ed.pipeline_id = :pipeline_id")
        params["pipeline_id"] = pipeline_id
    if node_id is not None:
        conditions.append("ed.node_id = :node_id")
        params["node_id"] = node_id
    if model_backend_id is not None:
        conditions.append("sr.model_backend_id = :model_backend_id")
        params["model_backend_id"] = model_backend_id
    return conditions, params


def build_eval_leaderboard_query(
    *,
    org_id: uuid.UUID,
    group_by: str,
    days: int = EVAL_LEADERBOARD_DEFAULT_DAYS,
    eval_id: uuid.UUID | None = None,
    pipeline_id: uuid.UUID | None = None,
    node_id: uuid.UUID | None = None,
    model_backend_id: uuid.UUID | None = None,
) -> tuple[str, dict[str, Any]]:
    """Build the org-scoped leaderboard aggregation statement + bind params.

    Aggregates per ``(axis, eval_type)``: the axis is one of ``pipeline`` /
    ``node`` / ``agent``. Pass-rates are computed from the ``passed`` boolean;
    ``run_count`` counts distinct runs (both suite-run and legacy pipeline-path
    outcomes). Returns ``(statement, params)`` — the statement is fully
    parameterised (no string interpolation of user input; the axis expression is
    the only interpolated fragment and comes from the fixed allowlist).
    """
    validate_leaderboard_axis(group_by)
    key_sql, label_sql, axis_joins = _leaderboard_axis_columns(group_by)
    since = datetime.now(UTC) - timedelta(days=days)
    conditions, params = _common_eval_read_conditions(
        org_id=org_id,
        since=since,
        eval_id=eval_id,
        pipeline_id=pipeline_id,
        node_id=node_id,
        model_backend_id=model_backend_id,
    )
    # Node/agent axes carry no meaningful entry for a NULL axis key — drop them.
    if group_by == "node":
        conditions.append("ed.node_id IS NOT NULL")
    if group_by == "agent":
        conditions.append("sr.model_backend_id IS NOT NULL")

    # S608/B608: the ONLY fragments interpolated here (``__AXIS_KEY__`` /
    # ``__AXIS_LABEL__`` / ``__AXIS_JOINS__``) come from the fixed
    # ``_leaderboard_axis_columns`` allowlist and the ``conditions`` predicates are
    # pre-composed bound-param strings. User inputs are always named binds. The
    # template is a static literal (implicit concatenation) + ``.replace()`` — no
    # f-string/format operator reaches the SQL.
    statement = (
        (
            "SELECT "  # nosec B608 - allowlist-controlled fragments; user inputs are bound
            "__AXIS_KEY__ AS axis_key, "
            "__AXIS_LABEL__ AS axis_label, "
            "ed.eval_type AS eval_type, "
            "COUNT(*) FILTER (WHERE er.passed) AS passed_count, "
            "COUNT(*) AS total_count, "
            "COUNT(DISTINCT COALESCE(er.suite_run_id, er.run_id)) AS run_count "
            "FROM eval_results er "
            "JOIN eval_definitions ed ON ed.id = er.eval_id "
            "__AXIS_JOINS__ "
            "LEFT JOIN suite_runs sr ON sr.id = er.suite_run_id "
            "WHERE __CONDITIONS__ "
            "GROUP BY __AXIS_KEY__, ed.eval_type "
            "ORDER BY __AXIS_KEY__, ed.eval_type"
        )
        .replace("__AXIS_KEY__", key_sql)
        .replace("__AXIS_LABEL__", label_sql)
        .replace("__AXIS_JOINS__", axis_joins)
        .replace("__CONDITIONS__", " AND ".join(conditions))
    )
    return statement, params


def build_eval_timeseries_query(
    *,
    org_id: uuid.UUID,
    eval_id: uuid.UUID,
    days: int = EVAL_LEADERBOARD_DEFAULT_DAYS,
    pipeline_id: uuid.UUID | None = None,
    node_id: uuid.UUID | None = None,
    model_backend_id: uuid.UUID | None = None,
) -> tuple[str, dict[str, Any]]:
    """Build the day-bucketed pass-rate time-series statement for one eval.

    Buckets ``eval_results`` by UTC day (``DATE_TRUNC``), partitioning by
    ``eval_type`` so a mixed partition is never rolled into a single raw
    average. Pass-rate is derived from ``passed`` only.
    """
    since = datetime.now(UTC) - timedelta(days=days)
    conditions, params = _common_eval_read_conditions(
        org_id=org_id,
        since=since,
        eval_id=eval_id,
        pipeline_id=pipeline_id,
        node_id=node_id,
        model_backend_id=model_backend_id,
    )
    statement = (
        "SELECT "  # nosec B608 - allowlist-controlled fragments; user inputs are bound
        "DATE_TRUNC('day', er.evaluated_at) AS bucket, "
        "ed.eval_type AS eval_type, "
        "COUNT(*) FILTER (WHERE er.passed) AS passed_count, "
        "COUNT(*) AS total_count, "
        "COUNT(DISTINCT COALESCE(er.suite_run_id, er.run_id)) AS run_count "
        "FROM eval_results er "
        "JOIN eval_definitions ed ON ed.id = er.eval_id "
        "LEFT JOIN suite_runs sr ON sr.id = er.suite_run_id "
        "WHERE __CONDITIONS__ "
        "GROUP BY bucket, ed.eval_type "
        "ORDER BY bucket"
    ).replace("__CONDITIONS__", " AND ".join(conditions))
    return statement, params


def build_eval_pipelines_query(
    *,
    org_id: uuid.UUID,
    eval_id: uuid.UUID,
    days: int = EVAL_LEADERBOARD_DEFAULT_DAYS,
    node_id: uuid.UUID | None = None,
    model_backend_id: uuid.UUID | None = None,
) -> tuple[str, dict[str, Any]]:
    """Build the cross-pipeline rollup statement for one eval.

    Lists every pipeline whose outcomes contributed to the eval within the
    window — the "eval reused across pipelines" rollup. Rows carry
    ``pipeline_id`` + ``pipeline_name`` (snapshot label via ``MAX``).
    """
    since = datetime.now(UTC) - timedelta(days=days)
    conditions, params = _common_eval_read_conditions(
        org_id=org_id,
        since=since,
        eval_id=eval_id,
        pipeline_id=None,
        node_id=node_id,
        model_backend_id=model_backend_id,
    )
    statement = (
        "SELECT "  # nosec B608 - allowlist-controlled fragments; user inputs are bound
        "ed.pipeline_id AS pipeline_id, MAX(p.name) AS pipeline_name "
        "FROM eval_results er "
        "JOIN eval_definitions ed ON ed.id = er.eval_id "
        "LEFT JOIN pipelines p ON p.id = ed.pipeline_id "
        "LEFT JOIN suite_runs sr ON sr.id = er.suite_run_id "
        "WHERE __CONDITIONS__ "
        "GROUP BY ed.pipeline_id "
        "ORDER BY ed.pipeline_id"
    ).replace("__CONDITIONS__", " AND ".join(conditions))
    return statement, params


def aggregate_eval_leaderboard(rows: Sequence[Any], *, group_by: str) -> list[dict[str, Any]]:
    """Roll the per-``(axis, eval_type)`` rows into per-axis leaderboard entries.

    Each entry carries the axis-key rollup (``pass_rate``/``passed``/``total``)
    computed by COUNTING passes across ``eval_type`` partitions — never by
    averaging raw scores — plus a per-``eval_type`` ``by_type`` breakdown and a
    ``stability`` score (``1 - cross-type pass-rate dispersion``). Entries are
    sorted by aggregate pass-rate descending; entries with no data sort last.
    """
    validate_leaderboard_axis(group_by)
    grouped: dict[str, dict[str, Any]] = {}
    for row in rows:
        key = getattr(row, "axis_key", None)
        if key is None:
            continue
        entry = grouped.setdefault(
            str(key),
            {
                "key": str(key),
                "label": getattr(row, "axis_label", None) or str(key),
                "by_type": {},
                "passed": 0,
                "total": 0,
            },
        )
        eval_type = getattr(row, "eval_type", None) or "unknown"
        passed = int(getattr(row, "passed_count", 0) or 0)
        total = int(getattr(row, "total_count", 0) or 0)
        run_count = int(getattr(row, "run_count", 0) or 0)
        by_type = entry["by_type"].setdefault(eval_type, {"passed": 0, "total": 0, "run_count": 0})
        by_type["passed"] += passed
        by_type["total"] += total
        by_type["run_count"] += run_count
        entry["passed"] += passed
        entry["total"] += total

    entries: list[dict[str, Any]] = []
    for entry in grouped.values():
        total = entry["total"]
        entry["pass_rate"] = round(entry["passed"] / total, 4) if total else None
        entry["by_type"] = {
            eval_type: {
                **by_type,
                "pass_rate": round(by_type["passed"] / by_type["total"], 4) if by_type["total"] else None,
            }
            for eval_type, by_type in entry["by_type"].items()
        }
        entry["stability"] = _leaderboard_stability(entry["by_type"])
        entries.append(entry)

    # Sort by aggregate pass-rate descending; rate-less entries sink to bottom.
    entries.sort(
        key=lambda e: (e["pass_rate"] is not None, e["pass_rate"] if e["pass_rate"] is not None else 0.0), reverse=True
    )
    return entries


def _leaderboard_stability(by_type: dict[str, dict[str, Any]]) -> float:
    """Cross-type pass-rate consistency: ``1 - stddev(per-type pass_rate)``.

    ``1.0`` when a single eval type drives the axis (no dispersion to measure)
    or when every type agrees; lower when the axis's types disagree. This is a
    *cross-type dispersion* measure — the suite-run path already guarantees a
    per-``eval_type`` partition on the raw data, so it is computed over those
    partitions only.
    """
    rates = [bt["pass_rate"] for bt in by_type.values() if bt["pass_rate"] is not None]
    if len(rates) <= 1:
        return 1.0
    mean_rate = sum(rates) / len(rates)
    variance = sum((r - mean_rate) ** 2 for r in rates) / len(rates)
    return float(round(max(0.0, 1.0 - variance**0.5), 4))


def _normalise_day(value: Any) -> Any:
    """Collapse a SQL ``DATE_TRUNC`` timestamp/date result to a ``datetime.date``."""
    if isinstance(value, datetime):
        return value.date()
    return value


def bucket_eval_timeseries(rows: Sequence[Any], *, since: datetime) -> list[dict[str, Any]]:
    """Bucket day-level rows into a zero-filled pass-rate series.

    Zero-fills the day grid from ``since`` through today so the series is
    continuous. A day with no outcomes is emitted with ``total=0`` and
    ``pass_rate=None`` (never ``0.0`` — an absent day is not a failed day).
    """
    per_day: dict[Any, dict[str, int]] = {}
    for row in rows:
        day = _normalise_day(getattr(row, "bucket", None))
        if day is None:
            continue
        bucket = per_day.setdefault(day, {"passed": 0, "total": 0, "run_count": 0})
        bucket["passed"] += int(getattr(row, "passed_count", 0) or 0)
        bucket["total"] += int(getattr(row, "total_count", 0) or 0)
        bucket["run_count"] += int(getattr(row, "run_count", 0) or 0)

    today = datetime.now(UTC).date()
    start = since.date() if hasattr(since, "date") else since
    out: list[dict[str, Any]] = []
    day = start
    while day <= today:
        bucket = per_day.get(day, {"passed": 0, "total": 0, "run_count": 0})
        total = bucket["total"]
        out.append(
            {
                "date": day.isoformat(),
                "passed": bucket["passed"],
                "total": total,
                "pass_rate": round(bucket["passed"] / total, 4) if total else None,
                "run_count": bucket["run_count"],
            }
        )
        day += timedelta(days=1)
    return out


def summarise_eval_timeseries(buckets: Sequence[dict[str, Any]]) -> dict[str, Any]:
    """Aggregate a time-series bucket list into a window summary."""
    passed = sum(b["passed"] for b in buckets)
    total = sum(b["total"] for b in buckets)
    return {
        "passed": passed,
        "total": total,
        "pass_rate": round(passed / total, 4) if total else None,
        "run_count": sum(b["run_count"] for b in buckets),
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


async def resolve_suite_last_alert_at(session: AsyncSession, run: SuiteRun) -> datetime | None:
    """Return the most recent ``notified_at`` of any prior run of the same suite.

    This is the per-suite rate-limit marker (FAR-379): a persistent regression
    must not alert on every run within the suite's ``cooldown`` window. Only
    the same organisation is queried, and the current run is excluded so its own
    ``notified_at`` (idempotency marker) cannot rate-limit itself. Returns
    ``None`` when no prior run of the suite ever alerted.
    """
    stmt = (
        select(SuiteRun.notified_at)
        .where(
            SuiteRun.organisation_id == run.organisation_id,
            SuiteRun.suite_id == run.suite_id,
            SuiteRun.notified_at.is_not(None),
            SuiteRun.id != run.id,
        )
        .order_by(SuiteRun.notified_at.desc())
        .limit(1)
    )
    return (await session.execute(stmt)).scalar_one_or_none()


def suite_alert_metrics(comparison_json: dict[str, Any] | None) -> dict[str, float | int] | None:
    """Derive the alert payload metrics from a ``comparison_json`` snapshot.

    Phase 3 already produced ``comparison_json`` (the persisted output of the
    grouped regression detection). This is a pure read — it never re-runs the
    detection, it only summarises the ``alerts`` already recorded. Returns
    ``None`` when there are no alert records; otherwise the metrics of the worst
    (largest ``drop_pct``) alert, which is the suite-level regression signal.
    """
    if not comparison_json:
        return None
    alerts = comparison_json.get("alerts") or []
    if not alerts:
        return None
    worst = max(alerts, key=lambda a: float(a.get("drop_pct") or 0.0))
    return {
        "alert_count": len(alerts),
        "prev_pass_rate": float(worst.get("prev_pass_rate") or 0.0),
        "current_pass_rate": float(worst.get("current_pass_rate") or 0.0),
        "drop_pct": float(worst.get("drop_pct") or 0.0),
    }


async def maybe_alert_eval_regression(
    session: AsyncSession,
    run: SuiteRun,
    suite: EvalSuite,
    baseline_run: SuiteRun | None,
    notifier: Any,
) -> str:
    """Alert on a detected regression — the ALERTING layer (FAR-379).

    Detection is Phase 3's job (``run.regressed`` / ``comparison_json``); this
    function decides WHEN and HOW OFTEN to alert, never whether a regression
    happened. Every guard returns a distinct outcome string for observability
    (logged by the caller):

    * ``skipped_no_baseline`` — no baseline resolved (first run) or the run has
      no regression flag. An explicit baseline is REQUIRED before any alert.
    * ``skipped_not_regressed`` — the comparison ran but found no regression.
    * ``skipped_partial_run`` — a ``partial`` run never alerts (its outcome is
      incomplete); only ``completed`` runs page.
    * ``skipped_already_notified`` — idempotent on ``suite_run_id``: an alert
      for this run was already sent.
    * ``skipped_below_minimum_delta`` — the observed drop is below the suite's
      configured ``minimum_delta``.
    * ``skipped_rate_limited`` — the suite alerted within its ``cooldown``
      window (a sustained regression does not spam every run).
    * ``dispatched`` — the alert was dispatched through ``notifier`` and
      ``run.notified_at`` stamped.

    Guards, in order: baseline -> regressed -> partial -> idempotency ->
    minimum_delta -> rate limit -> isolation -> dispatch. The isolation guard
    (``assert_eval_notification_isolated``) FAILS LOUDLY — it raises
    ``SuiteRunError`` when the eval channel has no eval-scoped subscribers or
    leaks to a production error forwarder, never silently dropping the alert.
    """
    if baseline_run is None or run.regressed is None:
        return "skipped_no_baseline"
    if run.regressed is False:
        return "skipped_not_regressed"
    if run.state != SuiteRunState.COMPLETED.value:
        return "skipped_partial_run"
    if run.notified_at is not None:
        return "skipped_already_notified"

    metrics = suite_alert_metrics(run.comparison_json)
    suite_min_delta = suite.minimum_delta
    if suite_min_delta is not None and (metrics is None or metrics["drop_pct"] < float(suite_min_delta)):
        return "skipped_below_minimum_delta"

    suite_cooldown = suite.cooldown
    if suite_cooldown is not None:
        last_alert_at = await resolve_suite_last_alert_at(session, run)
        if is_suite_rate_limited(last_alert_at, timedelta(minutes=suite_cooldown)):
            return "skipped_rate_limited"

    # Fail loudly before attempting to send: the eval channel must actually have
    # a reachable, non-error-forwarding subscriber — never a silent drop.
    subscribers = await load_eval_subscriber_events(session, run.organisation_id)
    assert_eval_notification_isolated(subscribers)

    metrics = metrics or {}
    payload = {
        "suite_id": str(suite.id),
        "suite_name": suite.name,
        "run_id": str(run.id),
        "baseline_run_id": str(baseline_run.id),
        "alert_count": metrics.get("alert_count", 0),
        "prev_pass_rate": metrics.get("prev_pass_rate", 0.0),
        "current_pass_rate": metrics.get("current_pass_rate", 0.0),
        "drop_pct": metrics.get("drop_pct", 0.0),
        # The eval notification templates are keyed on the actor being evaluated
        # (``agent_name``); for a suite we surface the suite as that actor so the
        # existing title/body render without a mapper change.
        "agent_name": suite.name,
    }
    await notifier.dispatch_event(run.organisation_id, EVENT_EVAL_REGRESSION, payload, run_id=run.id)
    run.notified_at = datetime.now(UTC)
    await session.flush()
    return "dispatched"


# --------------------------------------------------------------------------- #
# High-level orchestration                                                    #
# --------------------------------------------------------------------------- #
async def run_suite_comparison(
    session: AsyncSession,
    run: SuiteRun,
    entity_thresholds: dict[str, Any],
    baseline_window: int | None = None,
) -> dict[str, Any]:
    """Persist the run completion record and run the comparison against baseline.

    Resolves the same-tuple baseline (honouring the suite's ``baseline_window``,
    which widens the baseline to the ``N`` most-recent same-tuple prior runs or
    keeps the single-latest when ``None``), delegates the per-eval pass-rate
    comparison to ``detect_regressions`` (``group_by="suite_id"``), records the
    regression decision on the run, and (when a regression was flagged) emits the
    eval notification through the existing ``Notifier`` path. Never builds a
    parallel comparison store.

    ``entity_thresholds`` carries the configurable absolute/relative drop
    thresholds plus the per-suite spend ceiling.

    Returns the comparison result dict (also persisted as ``comparison_json``).
    """
    from modulo.core.eval_engine.regression import detect_regressions

    baseline_runs, warning = await resolve_baseline_run(session, run, baseline_window=baseline_window)
    baseline_run = baseline_runs[0] if baseline_runs else None
    comparison: dict[str, Any] = {"baseline_run_id": str(baseline_run.id) if baseline_run else None, "warning": warning}
    if not baseline_runs:
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
        baseline_run_ids=[r.id for r in baseline_runs],
        relative_threshold=rel_threshold,
    )
    regressed = bool(alerts)
    run.regressed = regressed
    run.baseline_run_id = baseline_runs[0].id
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


async def record_completion(
    session: AsyncSession,
    run: SuiteRun,
    entity_thresholds: dict[str, Any],
    baseline_window: int | None = None,
) -> None:
    """Terminalize a SuiteRun as ``completed`` and persist its comparison."""
    await session.refresh(run)
    entity_thresholds = entity_thresholds or {}
    comparison = await run_suite_comparison(session, run, entity_thresholds, baseline_window=baseline_window)
    run.comparison_json = comparison
    await session.flush()


__all__ = [
    "EVAL_LEADERBOARD_AXES",
    "EVAL_LEADERBOARD_DEFAULT_DAYS",
    "EVAL_LEADERBOARD_MAX_DAYS",
    "BaselineAmbiguityError",
    "SpendLimitExceededError",
    "SuiteRunError",
    "acquire_org_advisory_lock",
    "aggregate_eval_leaderboard",
    "assert_eval_notification_isolated",
    "bucket_eval_timeseries",
    "build_baseline_tuple",
    "build_eval_leaderboard_query",
    "build_eval_pipelines_query",
    "build_eval_timeseries_query",
    "claim_suite_run_cost",
    "daily_spend_exceeded",
    "is_suite_rate_limited",
    "load_eval_subscriber_events",
    "maybe_alert_eval_regression",
    "pass_rate_by_eval_type",
    "record_completion",
    "resolve_baseline_run",
    "resolve_eval_definition_version",
    "resolve_suite_last_alert_at",
    "run_suite_comparison",
    "should_notify_regression",
    "suite_alert_metrics",
    "suite_cumulative_exceeded",
    "suite_pass_rate",
    "summarise_eval_timeseries",
    "tuple_is_matching",
    "validate_leaderboard_axis",
]
