"""OpenTelemetry metrics for the generic REST connector (FAR-413).

Mirrors the ``modulo.core.error_tracking.metrics`` pattern: module-level
handles initialised lazily from the OTel ``get_meter_provider()``, every
operation no-ops when no meter provider is configured or the SDK does not
support an instrument, and every call is exception-swallowed so metrics can
never surface as a connector failure.

UNITS OF BEHAVIOUR
------------------
The cause-code taxonomy is a *stable, closed* set so that a metric consumer
can join it to a released topology without drift. Adding a code is a
semver-visible change; renaming one is forbidden once it ships.

    SUCCESS_OUTCOME = "success"
    CAUSE_3XX       = "http_3xx"       (redirects we refuse to follow)
    CAUSE_4XX       = "http_4xx"
    CAUSE_429       = "http_429"
    CAUSE_5XX       = "http_5xx"
    CAUSE_CONNECT   = "connect_error"
    CAUSE_TIMEOUT   = "timeout"
    CAUSE_TOO_LARGE = "response_too_large"
    CAUSE_AUTH      = "auth"
    CAUSE_SSRF      = "ssrf_blocked"
    CAUSE_UNKNOWN   = "unknown"
"""

from __future__ import annotations

import logging
from typing import Any

_log = logging.getLogger(__name__)

# ── Stable cause-code taxonomy ──────────────────────────────────────────────
# Closed set — see module docstring. Never rename a shipped value.

SUCCESS_OUTCOME = "success"
CAUSE_3XX = "http_3xx"
CAUSE_4XX = "http_4xx"
CAUSE_429 = "http_429"
CAUSE_5XX = "http_5xx"
CAUSE_CONNECT = "connect_error"
CAUSE_TIMEOUT = "timeout"
CAUSE_TOO_LARGE = "response_too_large"
CAUSE_AUTH = "auth"
CAUSE_SSRF = "ssrf_blocked"
CAUSE_UNKNOWN = "unknown"

# Counted as an "error" by the rollback error-rate. A 3xx we refuse to follow
# is not an error (no redirect is followed, the operation surfaces it); a 4xx
# client mistake is not a downstream anomaly — only 429/5xx/transport/timeout
# indicate an unhealthy remote we might need to roll back from.
_ERROR_CAUSE_CODES = frozenset({CAUSE_429, CAUSE_5XX, CAUSE_CONNECT, CAUSE_TIMEOUT})

# Module-level handles — initialised once by _init().
_requests_histogram: Any = None
_outcome_total: Any = None
_retry_total: Any = None
_ssrf_blocked_total: Any = None
_redaction_total: Any = None


def _get_meter() -> Any:
    try:
        from opentelemetry import metrics

        provider = metrics.get_meter_provider()
        if provider is None:
            return None
        return provider.get_meter("modulo.connectors.rest", version="0.1.0")
    except Exception:
        return None


def _init() -> None:
    global _requests_histogram, _outcome_total, _retry_total, _ssrf_blocked_total, _redaction_total

    if _requests_histogram is not None and _outcome_total is not None:
        return

    meter = _get_meter()
    if meter is None:
        return

    try:
        _requests_histogram = meter.create_histogram(
            name="modulo_rest_request_duration_seconds",
            description="End-to-end REST connector operation latency (one sample per logical operation)",
            unit="s",
        )
    except Exception:
        _log.warning("rest_metrics.request_histogram_failed")

    try:
        _outcome_total = meter.create_counter(
            name="modulo_rest_outcome_total",
            description="Total REST connector outcomes, labelled by cause code",
            unit="1",
        )
    except Exception:
        _log.warning("rest_metrics.outcome_counter_failed")

    try:
        _retry_total = meter.create_counter(
            name="modulo_rest_retry_total",
            description="Total retry attempts made by the REST connector, labelled by reason",
            unit="1",
        )
    except Exception:
        _log.warning("rest_metrics.retry_counter_failed")

    try:
        _ssrf_blocked_total = meter.create_counter(
            name="modulo_rest_ssrf_blocked_total",
            description="Total outbound requests blocked by the SSRF guard, labelled by destination host",
            unit="1",
        )
    except Exception:
        _log.warning("rest_metrics.ssrf_counter_failed")

    try:
        _redaction_total = meter.create_counter(
            name="modulo_rest_redaction_events_total",
            description="Total credential redaction events applied to connector output",
            unit="1",
        )
    except Exception:
        _log.warning("rest_metrics.redaction_counter_failed")

    _log.info("rest_metrics.registered")


def classify_status(status_code: int) -> str:
    """Map an HTTP status onto the stable cause-code taxonomy."""
    if status_code == 429:
        return CAUSE_429
    if status_code >= 500:
        return CAUSE_5XX
    if status_code >= 400:
        return CAUSE_4XX
    if status_code >= 300:
        return CAUSE_3XX
    return SUCCESS_OUTCOME


def record_request_duration(seconds: float, *, host: str, method: str, outcome: str) -> None:
    """Record an end-to-end operation latency + outcome (labelled host/method/outcome).

    The duration spans a whole logical operation — all retry attempts up to the
    terminal success or failure — so exactly one sample is emitted per operation
    (never one per attempt), keeping success-rate and p95 undiluted by failed
    intermediate attempts.
    """
    if _requests_histogram is None:
        _init()
    if _requests_histogram is not None and seconds >= 0:
        _requests_histogram.record(seconds, attributes={"host": host, "method": method, "outcome": outcome})
    record_outcome(outcome, host=host)


def record_outcome(outcome: str, *, host: str = "", cause_code: str | None = None) -> None:
    """Record a connector outcome. ``cause_code`` overrides ``outcome`` for the
    ``modulo_rest_outcome_total`` counter label (the stable closed taxonomy)."""
    if _outcome_total is None:
        _init()
    if _outcome_total is not None:
        attrs: dict[str, Any] = {"cause_code": cause_code or outcome}
        if host:
            attrs["host"] = host
        _outcome_total.add(1, attributes=attrs)


def record_retry(reason: str) -> None:
    """Record a retry attempt. ``reason`` is one of ``http_429``/``http_5xx``/``transport``."""
    if _retry_total is None:
        _init()
    if _retry_total is not None:
        _retry_total.add(1, attributes={"reason": reason})


def record_ssrf_blocked(host: str) -> None:
    """Record a request blocked by the SSRF guard (labelled by host)."""
    if _ssrf_blocked_total is None:
        _init()
    if _ssrf_blocked_total is not None:
        _ssrf_blocked_total.add(1, attributes={"host": host})


def record_redaction_event() -> None:
    """Record a single credential redaction applied to connector output."""
    if _redaction_total is None:
        _init()
    if _redaction_total is not None:
        _redaction_total.add(1)
