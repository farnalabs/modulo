"""Prometheus-style metrics for error tracking — wired to the OTel meter provider.

Also owns the run-runtime liveness instruments (D1): ``runs_running_count`` /
``runs_oldest_running_age_seconds`` gauges, the ``runs_stall_reason_total``
counter (label ``stall_reason``), and the ``runs_claim_count_total`` histogram.
These are sampled from the runs table on the dispatcher_reconcile tick (every
60s) when telemetry is enabled — see ``sample_run_runtime_metrics``.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

_log = logging.getLogger(__name__)

# Module-level metric handles — initialised once by _init_metrics().
_errors_total: Any = None
_error_groups_active: Any = None
_error_alerts_total: Any = None

# Alert-health instruments (FAR-151, §15.14): cooldown suppressions and failed
# notifier forwards — registered lazily alongside the alert counter.
_alerts_suppressed_total: Any = None
_alert_delivery_failed_total: Any = None

# Run-runtime instruments (D1) — registered lazily by init_runtime_metrics().
_runs_running_gauge: Any = None
_runs_oldest_running_gauge: Any = None
_runs_stall_reason_total: Any = None
_runs_claim_count_histogram: Any = None


def _get_meter() -> Any:
    try:
        from opentelemetry import metrics

        provider = metrics.get_meter_provider()
        if provider is None:
            return None
        return provider.get_meter("modulo.error_tracking", version="0.1.0")
    except Exception:
        _log.debug("error_tracking.metrics.meter_unavailable")
        return None


def init_metrics() -> None:
    global _errors_total, _error_groups_active

    if _errors_total is not None and _error_groups_active is not None:
        return

    meter = _get_meter()
    if meter is None:
        _log.warning("metrics.no_meter_provider — OTel metrics disabled")
        return

    _errors_total = meter.create_counter(
        name="modulo_errors_total",
        description="Total number of error events ingested, by level and source",
        unit="1",
    )

    try:
        _error_groups_active = meter.create_gauge(
            name="modulo_error_groups_active",
            description="Number of currently unresolved error groups, by level",
            unit="1",
        )
    except AttributeError:
        _log.warning("metrics.gauge_not_supported — OTel SDK version does not support create_gauge")

    _log.info("metrics.registered")


def _runtime_instruments() -> list[tuple[str, str, str]]:
    """Instrument specs for the run-runtime liveness metrics (D1).

    Registered as a list so ``init_runtime_metrics`` can iterate and a missing
    instrument never blocks the others.
    """
    return [
        ("gauge", "runs_running_count", "Number of runs currently in the 'running' state"),
        ("gauge", "runs_oldest_running_age_seconds", "Age of the oldest 'running' run, in seconds"),
        ("counter", "runs_stall_reason_total", "Total terminalizations by stall reason, labelled stall_reason"),
        ("histogram", "runs_claim_count_total", "Distribution of claim_count across currently running runs"),
    ]


def init_runtime_metrics() -> None:
    """Register the run-runtime liveness instruments once (idempotent)."""
    global _runs_running_gauge, _runs_oldest_running_gauge, _runs_stall_reason_total, _runs_claim_count_histogram

    if (
        _runs_running_gauge is not None
        and _runs_oldest_running_gauge is not None
        and _runs_stall_reason_total is not None
        and _runs_claim_count_histogram is not None
    ):
        return

    meter = _get_meter()
    if meter is None:
        return

    for kind, name, description in _runtime_instruments():
        try:
            if kind == "gauge":
                handle = meter.create_gauge(name=name, description=description, unit="1")
            elif kind == "counter":
                handle = meter.create_counter(name=name, description=description, unit="1")
            else:
                handle = meter.create_histogram(name=name, description=description, unit="1")
        except AttributeError:
            _log.warning("metrics.runtime_instrument_unsupported — %s skipped", name)
            continue
        except Exception:
            _log.warning("metrics.runtime_instrument_failed — %s skipped", name)
            continue
        if name == "runs_running_count":
            _runs_running_gauge = handle
        elif name == "runs_oldest_running_age_seconds":
            _runs_oldest_running_gauge = handle
        elif name == "runs_stall_reason_total":
            _runs_stall_reason_total = handle
        else:
            _runs_claim_count_histogram = handle

    _log.info("metrics.runtime_registered")


def record_error_ingest(level: str, source: str, environment: str | None) -> None:
    if _errors_total is not None:
        attrs: dict[str, Any] = {
            "level": level,
            "source": source,
            "environment": environment or "unknown",
        }
        _errors_total.add(1, attributes=attrs)


def set_active_groups(count: int, level: str) -> None:
    if _error_groups_active is not None:
        _error_groups_active.set(count, attributes={"level": level})


async def sample_error_group_metrics(factory: Any) -> None:
    """Sample the error_groups table and update the ``modulo_error_groups_active``
    gauge.

    Called from the dispatcher_reconcile tick (every 60s) when telemetry is
    enabled. Previously the gauge existed but was never updated — it stayed at
    its initial value forever. "Active" means a group in a non-terminal state
    (``new`` or ``acknowledged``; ``resolved`` and ``archived`` are excluded),
    counted per ``level_peak``. Counts are cross-org by design (same as the
    run-runtime liveness sample). Levels with zero active groups are explicitly
    set to 0 so a drained level doesn't leave a stale reading. Every failure is
    swallowed: metrics must never break the tick.
    """
    if _error_groups_active is None:
        init_metrics()
    if _error_groups_active is None:
        return
    try:
        from sqlalchemy import func, select

        from modulo.db.models.error_group import ErrorGroup

        async with factory() as session:
            rows = (
                await session.execute(
                    select(ErrorGroup.level_peak, func.count(ErrorGroup.id))
                    .where(ErrorGroup.status.in_(("new", "acknowledged")))
                    .group_by(ErrorGroup.level_peak)
                )
            ).all()

        counts: dict[str, int] = {level: int(count) for level, count in rows}
        for level in ("warning", "error", "critical"):
            set_active_groups(counts.get(level, 0), level)
    except asyncio.CancelledError:
        raise
    except Exception:
        _log.warning("metrics.sample_error_groups_failed", exc_info=True)


def record_stall_reason(stall_reason: str, count: int = 1) -> None:
    """Record terminalizations of running runs by their stall reason (D1).

    The label matches the run's ``error_code`` (``executor_stalled``,
    ``executor_superseded``, ``claim_cap_exhausted``, ``dispatch_failed``, ...).
    """
    if _runs_stall_reason_total is None:
        init_runtime_metrics()
    if _runs_stall_reason_total is not None and count > 0:
        _runs_stall_reason_total.add(count, attributes={"stall_reason": stall_reason})


def update_runs_liveness(running_count: int, oldest_running_age_seconds: float | None) -> None:
    """Update the running-count / oldest-running-age gauges (D1)."""
    if _runs_running_gauge is None or _runs_oldest_running_gauge is None:
        init_runtime_metrics()
    if _runs_running_gauge is not None:
        _runs_running_gauge.set(running_count)
    if _runs_oldest_running_gauge is not None and oldest_running_age_seconds is not None:
        _runs_oldest_running_gauge.set(oldest_running_age_seconds)


async def sample_run_runtime_metrics(factory: Any) -> None:
    """Sample the runs table and update the run-runtime instruments (D1).

    Called from the dispatcher_reconcile tick (every 60s) when telemetry is
    enabled. Runs on a system-scoped session (no RLS org) — the count and the
    oldest age are cross-org by design. The claim-count histogram records the
    ``claim_count`` of each currently-running run (the distribution of claim
    attempts). Every failure is swallowed: metrics must never break the tick.
    """
    if _runs_running_gauge is None and _runs_stall_reason_total is None:
        init_runtime_metrics()
    try:
        from sqlalchemy import func, select

        from modulo.db.models.run import Run

        async with factory() as session:
            count_result = await session.execute(select(func.count()).select_from(Run).where(Run.status == "running"))
            running_count = int(count_result.scalar_one() or 0)

            # Oldest-running age is computed in Python so the query compiles on
            # both Postgres and SQLite. The former raw ``extract(epoch from
            # (now() - MAX(started_at)))`` text query is Postgres-only and threw
            # on SQLite every 60s dispatcher tick — only the broad except
            # swallowed it. ``func.current_timestamp()`` returns a tz-aware
            # value on Postgres and a naive value on SQLite, matching
            # ``started_at`` on each dialect, so the subtraction never mixes
            # aware/naive datetimes (see backend AGENTS.md lesson).
            now_and_max = (
                await session.execute(
                    select(func.current_timestamp(), func.max(Run.started_at)).where(
                        Run.status == "running", Run.started_at.isnot(None)
                    )
                )
            ).one()
            now_value, max_started = now_and_max
            oldest_age: float | None = None
            if now_value is not None and max_started is not None:
                oldest_age = (now_value - max_started).total_seconds()

            claim_rows = (await session.execute(select(Run.claim_count).where(Run.status == "running"))).scalars()
            claim_counts = list(claim_rows)

        update_runs_liveness(running_count, oldest_age)
        if _runs_claim_count_histogram is not None:
            for claim_count in claim_counts:
                _runs_claim_count_histogram.record(int(claim_count or 0))
    except asyncio.CancelledError:
        raise
    except Exception:
        _log.warning("metrics.sample_run_runtime_failed", exc_info=True)


def _init_alert_counter() -> None:
    global _error_alerts_total
    if _error_alerts_total is not None:
        return
    try:
        meter = _get_meter()
        if meter is None:
            return
        _error_alerts_total = meter.create_counter(
            name="modulo_error_alerts_total",
            description="Total number of error alerts dispatched",
            unit="1",
        )
    except Exception:
        _log.warning("metrics.alert_counter_failed")


def record_error_alert(level: str, action_type: str) -> None:
    if _error_alerts_total is None:
        _init_alert_counter()
    if _error_alerts_total is not None:
        _error_alerts_total.add(1, attributes={"level": level, "action_type": action_type})


def _init_suppressed_counter() -> None:
    global _alerts_suppressed_total
    if _alerts_suppressed_total is not None:
        return
    try:
        meter = _get_meter()
        if meter is None:
            return
        _alerts_suppressed_total = meter.create_counter(
            name="modulo_alerts_suppressed_total",
            description="Total number of alert evaluations suppressed by cooldown",
            unit="1",
        )
    except Exception:
        _log.warning("metrics.suppressed_counter_failed")


def record_alert_suppressed(rule_id: str) -> None:
    if _alerts_suppressed_total is None:
        _init_suppressed_counter()
    if _alerts_suppressed_total is not None:
        _alerts_suppressed_total.add(1, attributes={"rule_id": rule_id})


def _init_delivery_failed_counter() -> None:
    global _alert_delivery_failed_total
    if _alert_delivery_failed_total is not None:
        return
    try:
        meter = _get_meter()
        if meter is None:
            return
        _alert_delivery_failed_total = meter.create_counter(
            name="modulo_alert_delivery_failed_total",
            description="Total number of alert dispatches that failed to reach a notifier",
            unit="1",
        )
    except Exception:
        _log.warning("metrics.delivery_failed_counter_failed")


def record_alert_delivery_failed(rule_id: str, action_type: str) -> None:
    if _alert_delivery_failed_total is None:
        _init_delivery_failed_counter()
    if _alert_delivery_failed_total is not None:
        _alert_delivery_failed_total.add(1, attributes={"rule_id": rule_id, "action_type": action_type})
