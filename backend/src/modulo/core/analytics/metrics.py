"""Metric gauges/counters for the analytics facts subsystem (ADR 020).

The ``modulo_facts_*`` inventory lives here (NOT in
``modulo.core.cost_controller.breakdown.metrics`` — that module owns the cost
engine's metrics only; the naming decision is recorded in ADR 020). All
handles are lazy-initialised so a missing meter provider never breaks the
facts path.

Facts-denominator reference (ADR 020 Decision 1 / PRD §8.32.4): ``run_daily_facts``
stores one row per TERMINAL run, so the facts count = every run whose status is
in ``TERMINAL_STATUSES`` (``{"complete", "failed", "cancelled", "eval_failed",
"stalled"}``, defined in ``modulo.db.models.run`` — the same set the 90-day
run purge and the facts backfill/reconcile use). This is distinct from the
cost ledger (terminal runs with ``total_cost_usd > 0``) and the dashboard
summary (all runs). For success-rate purposes a run counts as SUCCESS only when
``status == "complete"``; a ``stalled`` run counts as a failure (it never
completed) — see ``build_facts_query``'s ``complete_count``/``failure_status``.
"""

from __future__ import annotations

import logging
from typing import Any

_log = logging.getLogger(__name__)

_facts_write_failed_total: Any = None
_backfill_last_run_ts: Any = None
_backfill_rows: Any = None
_reconcile_alert_total: Any = None
_retention_lag: Any = None
_facts_skip_non_pg_total: Any = None


def _get_meter() -> Any:
    try:
        from opentelemetry import metrics

        provider = metrics.get_meter_provider()
        if provider is None:
            return None
        return provider.get_meter("modulo.analytics", version="0.1.0")
    except Exception:
        _log.debug("analytics.metrics.meter_unavailable")
        return None


def _ensure() -> None:
    global \
        _facts_write_failed_total, \
        _backfill_last_run_ts, \
        _backfill_rows, \
        _reconcile_alert_total, \
        _retention_lag, \
        _facts_skip_non_pg_total
    if _facts_write_failed_total is not None:
        return
    meter = _get_meter()
    if meter is None:
        return
    _facts_write_failed_total = meter.create_counter(
        name="modulo_facts_write_failed_total",
        description="Facts writes that failed and were swallowed (fail-open; never affects the cost result)",
        unit="1",
    )
    _backfill_last_run_ts = meter.create_gauge(
        name="modulo_facts_backfill_last_run_ts",
        description="Epoch seconds of the last successful facts backfill batch run",
        unit="1",
    )
    _backfill_rows = meter.create_gauge(
        name="modulo_facts_backfill_rows",
        description="Facts rows written by the last backfill invocation",
        unit="1",
    )
    _reconcile_alert_total = meter.create_counter(
        name="modulo_facts_reconcile_alert_total",
        description="Reconcile alerts by org and drift_type (ledger > facts beyond source availability)",
        unit="1",
    )
    _retention_lag = meter.create_gauge(
        name="modulo_facts_retention_lag",
        description="Days between the oldest kept fact and today (drifts with retention_facts health)",
        unit="days",
    )
    _facts_skip_non_pg_total = meter.create_counter(
        name="modulo_facts_skip_non_pg_total",
        description="Analytics maintenance invocations skipped on a non-Postgres backend",
        unit="1",
    )


def record_facts_write_failed() -> None:
    if _facts_write_failed_total is None:
        _ensure()
    if _facts_write_failed_total is not None:
        _facts_write_failed_total.add(1)


def set_backfill_last_run_ts(epoch: float) -> None:
    if _backfill_last_run_ts is None:
        _ensure()
    if _backfill_last_run_ts is not None:
        _backfill_last_run_ts.set(epoch)


def set_backfill_rows(rows: int) -> None:
    if _backfill_rows is None:
        _ensure()
    if _backfill_rows is not None:
        _backfill_rows.set(rows)


def record_reconcile_alert(org_id: str, drift_type: str) -> None:
    if _reconcile_alert_total is None:
        _ensure()
    if _reconcile_alert_total is not None:
        _reconcile_alert_total.add(1, attributes={"org_id": org_id, "drift_type": drift_type})


def set_retention_lag(days: float) -> None:
    if _retention_lag is None:
        _ensure()
    if _retention_lag is not None:
        _retention_lag.set(days)


def record_facts_skip_non_pg() -> None:
    if _facts_skip_non_pg_total is None:
        _ensure()
    if _facts_skip_non_pg_total is not None:
        _facts_skip_non_pg_total.add(1)
