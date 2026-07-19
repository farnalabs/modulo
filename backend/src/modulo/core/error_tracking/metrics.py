"""Prometheus-style metrics for error tracking — wired to the OTel meter provider."""

from __future__ import annotations

import logging
from typing import Any

_log = logging.getLogger(__name__)

# Module-level metric handles — initialised once by _init_metrics().
_errors_total: Any = None
_error_groups_active: Any = None
_error_alerts_total: Any = None


def _get_meter() -> Any:
    try:
        from opentelemetry import metrics

        provider = metrics.get_meter_provider()
        if provider is None:
            return None
        return provider.get_meter("modulo.error_tracking", version="0.1.0")
    except Exception:
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
