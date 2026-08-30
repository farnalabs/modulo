"""Advisory guardrail scorecards over run telemetry (FAR-217 T3).

Read-only, org-scoped analytics surface that aggregates the per-run
``guardrail_summary_json`` telemetry (FAR-223 item 11) plus the single-node
self-correction trail (FAR-210 T2b) into ADVISORY guardrail health metrics:

* **Fire counts** — runs with a guardrail bound, runs with ≥1 violation,
  blocked runs, total violations, and the observed / redacted / errored /
  skipped breakdowns (including expected vs unexpected skips).
* **Pass/fail rates** — the raw-detection violation rate (``violated/bound``)
  plus a SEPARATED first-try-pass vs corrected-pass view. First-try-pass
  counts runs whose ingestion-edge guardrail pass found NO violation;
  corrected-pass counts single-node corrections that converged clean
  (``resolved``) vs escalated to HITL vs budget-exhausted. The two are NEVER
  merged into a single pass rate — retries would inflate a merged number
  (Goodhart: retries inflate pass rates).
* **Evasion-band drift** — advisory-only signal comparing the current errored
  rate (and unexpected-skip count) against a historical baseline window.

EVERYTHING in this module is advisory. The scorecard never gates autonomy,
never blocks a run, and never changes CI enforcement. A raw-pass-rate gate is
FAR-218 (deferred); this module deliberately does not build one — every rate
is labelled advisory and returned alongside a note stating it is not a gate.

Isolation invariant (same as the rest of ``modulo.core.analytics``):
``modulo_app`` is NOBYPASSRLS (tenant isolation relies on RLS policies) and the
ORM tenant filter is NOT registered on
Postgres, so every statement carries an explicit ``organisation_id`` predicate
— that predicate is the PRIMARY isolation control (RLS also enforces org
scoping). ``set_rls_org`` is
defense-in-depth, never the control.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any, NoReturn

import sqlalchemy as sa
from sqlalchemy import func, text
from sqlalchemy.exc import DBAPIError, ProgrammingError, SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from modulo.core.analytics.service import (
    AnalyticsDatabaseError,
    AnalyticsMigrationRequiredError,
    AnalyticsQueryTimeoutError,
    AnalyticsRateLimitedError,
    AnalyticsValidationError,
    _is_query_canceled,
    _normalise_bounds,
    _rate_limited,
)
from modulo.core.pipeline_engine.error_codes import expand_code_variants
from modulo.db.models.audit_event import AuditEvent
from modulo.db.models.feedback_record import FeedbackRecord
from modulo.db.models.run import Run
from modulo.db.rls import set_rls_org, set_rls_user_context
from modulo.settings import Settings

_log = logging.getLogger(__name__)

__all__ = ["run_guardrail_scorecard"]

# Repeated SQL fragments and typed error messages (S1192).
_SQL_SET_STATEMENT_TIMEOUT = "SELECT set_config('statement_timeout', :ms, true)"
_SQL_SET_TIMEZONE_UTC = "SELECT set_config('timezone', 'UTC', true)"
_ERR_DATABASE_UNAVAILABLE = "Database temporarily unavailable."
_ERR_RATE_LIMIT_EXCEEDED = "Rate limit exceeded"
_MSG_MIGRATION_REQUIRED = "Feature is not available. Run database migrations to enable it."

# Default statement timeout (ms) — settings-driven via
# ``analytics_query_statement_timeout_ms`` when configured (mirrors the shared
# analytics service default).
_DEFAULT_STATEMENT_TIMEOUT_MS = 5000

# Advisory drift margin — the current errored rate is flagged as drifting when
# it exceeds the historical baseline rate by more than this absolute margin.
# Advisory only: the flag is a soft-band signal, never a gate.
_DRIFT_ERR_RATE_MARGIN = 0.05

# Baseline lookback cap — the drift baseline is computed over the window of
# the same length as the queried range, capped at this many days so the
# baseline query stays bounded.
_BASELINE_MAX_DAYS = 30

# The feedback-handler types that represent single-node guardrail corrections
# (the T2b self-correction path). Mirrors feedback_manager._AI_HANDLER_TYPES —
# the scorecard only counts AI-correction records, never pure-human ones.
_AI_CORRECTION_HANDLER_TYPES = ("ai_correction", "ai_correction_with_human_review")

# The correction-escalation audit event whose payload carries a ``verdict``
# field. A verdict of ``budget_exhausted`` is the specific budget-exhausted
# signal (mirrors correction.EVENT_CORRECTION_ESCALATED).
_EVENT_CORRECTION_ESCALATED = "guardrail.correction_escalated"
_VERDICT_BUDGET_EXHAUSTED = "budget_exhausted"

# Fixed note strings — the advisory-only contract is surfaced on every rate so
# no consumer can mistake a scorecard metric for a gate (S1192).
_NOTE_RATES = (
    "Advisory only — the first-try-pass and corrected-pass rates are reported "
    "SEPARATELY and never merged into a single pass rate (retries inflate a "
    "merged number). No scorecard rate gates autonomy."
)
_NOTE_CORRECTIONS = (
    "Advisory only — corrected-pass counts single-node corrections that "
    "converged clean (resolved) vs escalated to HITL vs budget-exhausted; "
    "reported separately from first-try-pass, never merged."
)
_NOTE_DRIFT = (
    "Advisory only — a drift indicator never gates or blocks anything. It "
    "flags when unexpected guardrail skips occurred or the errored rate moved "
    "above the historical baseline + margin."
)


# ---------------------------------------------------------------------------
# Dialect-aware JSON field extraction (guardrail_summary_json is a generic
# JSON column; per-field access differs between Postgres and SQLite/MariaDB)
# ---------------------------------------------------------------------------


def _json_int(column: Any, key: str, dialect: str) -> Any:
    """A SQL expression for *column*->>'*key*' cast to Integer (portable)."""
    if dialect == "postgresql":
        return sa.cast(column.op("->>")(key), sa.Integer)
    return sa.cast(func.json_extract(column, f"$.{key}"), sa.Integer)


def _json_text(column: Any, key: str, dialect: str) -> Any:
    """A SQL expression for *column*->>'*key*' as text (portable)."""
    if dialect == "postgresql":
        return column.op("->>")(key)
    return func.json_extract(column, f"$.{key}")


# ---------------------------------------------------------------------------
# Statement builders (explicit org predicate = the PRIMARY isolation control,
# with RLS also enforcing org scoping)
# ---------------------------------------------------------------------------


def _build_runs_scorecard_stmt(
    dialect: str,
    org_id: uuid.UUID,
    date_from: datetime,
    date_to: datetime,
) -> sa.Select[Any]:
    """One aggregate row of guardrail fire counts over *runs* in range.

    The guardrail summary is a point-in-time snapshot persisted at run
    creation; only runs carrying one (``guardrail_summary_json IS NOT NULL``)
    participate. ``runs_blocked`` counts runs whose guardrail pass terminalised
    the run (``error_code`` in the ``eval.blocked`` spelling set) — the only
    run-level "blocked" signal the telemetry exposes. ``first_try_pass_runs``
    counts runs whose ingestion-edge pass found NO violation (``violated == 0``
    among runs with a bound guardrail).
    """
    g = Run.guardrail_summary_json
    bound = _json_int(g, "bound", dialect)
    violated = _json_int(g, "violated", dialect)
    blocked_codes = sorted(expand_code_variants("eval.blocked"))
    conditions = [
        Run.organisation_id == org_id,
        Run.guardrail_summary_json.is_not(None),
        Run.created_at >= date_from,
        Run.created_at <= date_to,
    ]
    return sa.select(
        sa.func.count().label("runs_total"),
        sa.func.sum(sa.case((bound > 0, 1), else_=0)).label("runs_with_guardrail"),
        sa.func.sum(sa.case((violated > 0, 1), else_=0)).label("runs_with_violations"),
        sa.func.sum(sa.case((sa.and_(bound > 0, violated == 0), 1), else_=0)).label("first_try_pass_runs"),
        sa.func.sum(sa.case((Run.error_code.in_(blocked_codes), 1), else_=0)).label("runs_blocked"),
        sa.func.coalesce(sa.func.sum(bound), 0).label("bound_total"),
        sa.func.coalesce(sa.func.sum(_json_int(g, "evaluated", dialect)), 0).label("evaluated_total"),
        sa.func.coalesce(sa.func.sum(_json_int(g, "passed", dialect)), 0).label("passed_total"),
        sa.func.coalesce(sa.func.sum(violated), 0).label("violated_total"),
        sa.func.coalesce(sa.func.sum(_json_int(g, "observed", dialect)), 0).label("observed_total"),
        sa.func.coalesce(sa.func.sum(_json_int(g, "errored", dialect)), 0).label("errored_total"),
        sa.func.coalesce(sa.func.sum(_json_int(g, "redacted", dialect)), 0).label("redacted_total"),
        sa.func.coalesce(sa.func.sum(_json_int(g, "skipped", dialect)), 0).label("skipped_total"),
        sa.func.coalesce(sa.func.sum(_json_int(g, "expected_skips", dialect)), 0).label("expected_skips_total"),
        sa.func.coalesce(sa.func.sum(_json_int(g, "unexpected_skips", dialect)), 0).label("unexpected_skips_total"),
    ).where(*conditions)


def _build_corrections_stmt(
    org_id: uuid.UUID,
    date_from: datetime,
    date_to: datetime,
) -> sa.Select[Any]:
    """One aggregate row of single-node correction outcomes in range.

    Counts feedback records whose handler is an AI-correction type (the T2b
    self-correction path). ``converged_clean`` = ``resolved`` (the RESOLVED
    verdict transitions the record to ``resolved``); ``escalated_hitl`` =
    ``escalated`` (every non-resolved verdict escalates to HITL);
    ``dismissed`` / ``in_flight`` complete the status distribution.
    """
    return sa.select(
        sa.func.count().label("corrections_total"),
        sa.func.sum(sa.case((FeedbackRecord.feedback_status == "resolved", 1), else_=0)).label("converged_clean"),
        sa.func.sum(sa.case((FeedbackRecord.feedback_status == "escalated", 1), else_=0)).label("escalated_hitl"),
        sa.func.sum(sa.case((FeedbackRecord.feedback_status == "dismissed", 1), else_=0)).label("dismissed"),
        sa.func.sum(
            sa.case((FeedbackRecord.feedback_status.in_(("pending", "routing", "correcting")), 1), else_=0)
        ).label("in_flight"),
    ).where(
        FeedbackRecord.organisation_id == org_id,
        FeedbackRecord.feedback_handler_type.in_(_AI_CORRECTION_HANDLER_TYPES),
        FeedbackRecord.created_at >= date_from,
        FeedbackRecord.created_at <= date_to,
    )


def _build_budget_exhausted_stmt(
    dialect: str,
    org_id: uuid.UUID,
    date_from: datetime,
    date_to: datetime,
) -> sa.Select[Any]:
    """Count of budget-exhausted corrections via the escalation audit verdict.

    The correction trail persists the specific escalation reason only in the
    ``guardrail.correction_escalated`` audit event's ``payload_json.verdict``
    (the FeedbackRecord status collapses every non-resolved verdict to
    ``escalated``). Counting that verdict is the exact budget-exhausted signal.
    """
    return sa.select(sa.func.count()).where(
        AuditEvent.organisation_id == org_id,
        AuditEvent.event_type == _EVENT_CORRECTION_ESCALATED,
        AuditEvent.created_at >= date_from,
        AuditEvent.created_at <= date_to,
        _json_text(AuditEvent.payload_json, "verdict", dialect) == _VERDICT_BUDGET_EXHAUSTED,
    )


# ---------------------------------------------------------------------------
# Pure aggregation + drift derivation (unit-testable without a DB)
# ---------------------------------------------------------------------------


def _rate(numerator: int | None, denominator: int | None) -> float | None:
    """A bounded 4dp ratio, or None when the denominator is absent/zero."""
    if not denominator:
        return None
    return round(float(numerator or 0) / float(denominator), 4)


def _drift_indicator(
    unexpected_skips: int,
    current_rate: float | None,
    baseline_rate: float | None,
) -> tuple[bool, str]:
    """Advisory drift flag + indicator label.

    Drift is flagged when unexpected skips occurred OR the current errored
    rate exceeds the baseline by the margin. Without a baseline the signal is
    ``no_baseline`` unless unexpected skips already flagged drift. Advisory
    only — the caller never gates on it.
    """
    if unexpected_skips > 0:
        return True, "drift"
    if current_rate is not None and baseline_rate is not None and current_rate > baseline_rate + _DRIFT_ERR_RATE_MARGIN:
        return True, "drift"
    return False, ("in_band" if baseline_rate is not None else "no_baseline")


def _assemble_scorecard(
    runs_row: Any,
    corrections_row: Any,
    budget_exhausted: int,
    baseline_row: Any,
    *,
    date_from: str,
    date_to: str,
    baseline_window_days: int,
) -> dict[str, Any]:
    """Compose the advisory scorecard response dict from the aggregate rows.

    The first-try-pass and corrected-pass numbers are deliberately kept in
    separate top-level objects and never combined into a single pass rate.
    """
    runs_with_guardrail = int(runs_row.runs_with_guardrail or 0)
    runs_with_violations = int(runs_row.runs_with_violations or 0)
    first_try_pass_runs = int(runs_row.first_try_pass_runs or 0)
    bound_total = int(runs_row.bound_total or 0)
    violated_total = int(runs_row.violated_total or 0)
    errored_total = int(runs_row.errored_total or 0)
    unexpected_skips = int(runs_row.unexpected_skips_total or 0)

    raw_violation_rate = _rate(violated_total, bound_total)
    first_try_pass_rate = _rate(first_try_pass_runs, runs_with_guardrail) if runs_with_guardrail else None

    corrections_total = int(corrections_row.corrections_total or 0)
    converged_clean = int(corrections_row.converged_clean or 0)
    corrected_pass_rate = _rate(converged_clean, corrections_total) if corrections_total else None

    current_errored_rate = _rate(errored_total, bound_total)
    baseline_errored_rate = _rate(
        int(baseline_row.errored_total or 0),
        int(baseline_row.bound_total or 0),
    )
    drift_detected, drift_indicator = _drift_indicator(
        unexpected_skips=unexpected_skips,
        current_rate=current_errored_rate,
        baseline_rate=baseline_errored_rate,
    )

    return {
        "advisory_only": True,
        "date_from": date_from,
        "date_to": date_to,
        "scope": {
            "runs_with_guardrail": runs_with_guardrail,
            "runs_with_violations": runs_with_violations,
            "runs_blocked": int(runs_row.runs_blocked or 0),
            "first_try_pass_runs": first_try_pass_runs,
        },
        "fire_counts": {
            "bound": bound_total,
            "evaluated": int(runs_row.evaluated_total or 0),
            "passed": int(runs_row.passed_total or 0),
            "violated": violated_total,
            "observed": int(runs_row.observed_total or 0),
            "errored": errored_total,
            "redacted": int(runs_row.redacted_total or 0),
            "skipped": int(runs_row.skipped_total or 0),
            "expected_skips": int(runs_row.expected_skips_total or 0),
            "unexpected_skips": unexpected_skips,
        },
        "rates": {
            "raw_violation_rate": raw_violation_rate,
            "first_try_pass_rate": first_try_pass_rate,
            "note": _NOTE_RATES,
        },
        "self_correction": {
            "corrections_total": corrections_total,
            "converged_clean": converged_clean,
            "escalated_hitl": int(corrections_row.escalated_hitl or 0),
            "budget_exhausted": budget_exhausted,
            "dismissed": int(corrections_row.dismissed or 0),
            "in_flight": int(corrections_row.in_flight or 0),
            "corrected_pass_rate": corrected_pass_rate,
            "note": _NOTE_CORRECTIONS,
        },
        "evasion_band_drift": {
            "current_errored_rate": current_errored_rate,
            "baseline_errored_rate": baseline_errored_rate,
            "baseline_window_days": baseline_window_days,
            "unexpected_skips_total": unexpected_skips,
            "drift_detected": drift_detected,
            "drift_indicator": drift_indicator,
            "advisory_only": True,
            "note": _NOTE_DRIFT,
        },
        "generated_at": datetime.now(UTC).isoformat(),
    }


# ---------------------------------------------------------------------------
# Orchestration (mirrors the shared analytics service's guard structure)
# ---------------------------------------------------------------------------


async def _maybe_apply_postgres_timeout(session: AsyncSession, settings: Settings) -> None:
    """Apply the analytics statement timeout + UTC timezone on Postgres only.

    SQLite/MariaDB ignore these (and have no equivalent knobs), so this is a
    no-op for non-Postgres dialects — callers gate on dialect before calling.
    """
    timeout_ms = getattr(settings, "analytics_query_statement_timeout_ms", _DEFAULT_STATEMENT_TIMEOUT_MS)
    await session.execute(text(_SQL_SET_TIMEZONE_UTC))
    await session.execute(text(_SQL_SET_STATEMENT_TIMEOUT), {"ms": str(int(timeout_ms))})


def _raise_mapped_error(exc: Exception, org_id: uuid.UUID) -> NoReturn:
    """Map a raw/exception into the route-facing typed ``AnalyticsError`` set.

    Typed ``AnalyticsError`` subclasses and ``asyncio.CancelledError`` are
    propagated unchanged (the route maps them to HTTP statuses / cancellation).
    ``ProgrammingError`` becomes a migration-required error; ``DBAPIError`` whose
    cancellation predicate fires becomes a timeout; any other SQLAlchemy error
    becomes a generic DB-unavailable error. Anything unexpected is collapsed to
    a generic DB-unavailable error so no raw exception escapes to the route.
    """
    if isinstance(exc, asyncio.CancelledError):
        raise exc
    if isinstance(
        exc,
        (
            AnalyticsValidationError,
            AnalyticsRateLimitedError,
            AnalyticsQueryTimeoutError,
            AnalyticsMigrationRequiredError,
            AnalyticsDatabaseError,
        ),
    ):
        raise exc
    if isinstance(exc, ProgrammingError):
        _log.exception("analytics.guardrails.programming_error", extra={"org_id": str(org_id)})
        raise AnalyticsMigrationRequiredError(_MSG_MIGRATION_REQUIRED) from None
    if isinstance(exc, DBAPIError):
        if _is_query_canceled(exc):
            _log.warning("analytics.guardrails.timeout", extra={"org_id": str(org_id)})
            raise AnalyticsQueryTimeoutError("query exceeded timeout — reduce the date range") from None
        _log.exception("analytics.guardrails.db_error", extra={"org_id": str(org_id)})
        raise AnalyticsDatabaseError(_ERR_DATABASE_UNAVAILABLE) from None
    if isinstance(exc, SQLAlchemyError):
        _log.exception("analytics.guardrails.db_error", extra={"org_id": str(org_id)})
        raise AnalyticsDatabaseError(_ERR_DATABASE_UNAVAILABLE) from None
    _log.exception("analytics.guardrails.unexpected_error", extra={"org_id": str(org_id)})
    raise AnalyticsDatabaseError(_ERR_DATABASE_UNAVAILABLE) from None


async def _run_scorecard_queries(
    factory: async_sessionmaker[AsyncSession],
    org_id: uuid.UUID,
    baseline_from: datetime,
    effective_from: datetime,
    effective_to: datetime,
    *,
    account_id: uuid.UUID | None,
    org_role: str | None,
    settings: Settings,
) -> tuple[Any, Any, int, Any]:
    """Open a session and run the four scorecard aggregate queries.

    Returns ``(runs_row, corrections_row, budget_exhausted, baseline_row)``.
    Any DB-level or unexpected failure is mapped by ``_raise_mapped_error`` into
    the typed ``AnalyticsError`` the REST route expects — no raw exception ever
    escapes this helper.
    """
    try:
        async with factory() as session, session.begin():
            await set_rls_org(session, org_id)
            if account_id is not None:
                await set_rls_user_context(session, account_id, org_role or "")
            dialect = (await session.connection()).dialect.name
            if dialect == "postgresql":
                await _maybe_apply_postgres_timeout(session, settings)

            runs_row = (
                await session.execute(_build_runs_scorecard_stmt(dialect, org_id, effective_from, effective_to))
            ).one()
            corrections_row = (
                await session.execute(_build_corrections_stmt(org_id, effective_from, effective_to))
            ).one()
            budget_exhausted = int(
                (
                    await session.execute(_build_budget_exhausted_stmt(dialect, org_id, effective_from, effective_to))
                ).scalar()
                or 0
            )
            baseline_row = (
                await session.execute(_build_runs_scorecard_stmt(dialect, org_id, baseline_from, effective_from))
            ).one()
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        _raise_mapped_error(exc, org_id)

    return runs_row, corrections_row, budget_exhausted, baseline_row


async def run_guardrail_scorecard(
    *,
    org_id: uuid.UUID,
    factory: async_sessionmaker[AsyncSession],
    settings: Settings,
    account_id: uuid.UUID | None = None,
    org_role: str | None = None,
    date_from: datetime | None = None,
    date_to: datetime | None = None,
) -> dict[str, Any]:
    """Compute the advisory guardrail scorecard for *org_id* over the range.

    Returns the scorecard dict in the ``GuardrailScorecardResponse`` shape.
    Raises the typed ``AnalyticsError`` subclasses on rate-limit / validation /
    DB failure, exactly like the shared analytics service — the REST route maps
    them to HTTP status codes.

    The drift baseline is computed over the window of the same length as the
    queried range, immediately preceding ``date_from`` (capped at
    ``_BASELINE_MAX_DAYS``). A range with no baseline data yields
    ``baseline_errored_rate = None`` and ``drift_indicator = "no_baseline"``.
    """
    if _rate_limited(str(org_id)):
        raise AnalyticsRateLimitedError(_ERR_RATE_LIMIT_EXCEEDED)

    effective_from, effective_to = _normalise_bounds(date_from, date_to)
    range_days = max((effective_to.date() - effective_from.date()).days, 1)
    baseline_window_days = min(range_days, _BASELINE_MAX_DAYS)
    baseline_from = effective_from - timedelta(days=baseline_window_days)

    runs_row, corrections_row, budget_exhausted, baseline_row = await _run_scorecard_queries(
        factory,
        org_id,
        baseline_from,
        effective_from,
        effective_to,
        account_id=account_id,
        org_role=org_role,
        settings=settings,
    )

    return _assemble_scorecard(
        runs_row,
        corrections_row,
        budget_exhausted,
        baseline_row,
        date_from=effective_from.isoformat(),
        date_to=effective_to.isoformat(),
        baseline_window_days=baseline_window_days,
    )
