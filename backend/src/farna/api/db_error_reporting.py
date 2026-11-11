"""Structured reason reporting for fail-closed 503 responses.

2026-09-04 incident: ~8/10 GitHub pr-review webhook POSTs to app.modulo.run
failed with HTTP 503 and no persisted cause existed anywhere — the fail-closed
raise sites logged either a bare log code or nothing, so the reason was lost
the moment the response left the process. The sender sees only the status
code; the structured record this helper emits is the persisted reason trail.
"""

import logging

logger = logging.getLogger(__name__)


def log_service_unavailable(
    reason: str,
    exc: Exception | None = None,
    *,
    route: str,
    detail: str | None = None,
    level: int | None = None,
) -> None:
    """Emit the structured record that precedes every 503 raise.

    Call this IMMEDIATELY BEFORE each ``HTTPException(503)`` raise. The record
    carries the route/path, a short machine-readable ``reason`` code (e.g.
    ``db_transient``, ``system_bootstrap_degraded``,
    ``snapshot_lock_unavailable``), the exception class name, and a brief
    detail string.

    Details MUST be fixed strings authored at the call site — never request
    payloads, tokens, secrets, or raw exception text (which can embed SQL).
    The raise keeps its existing semantics: ``from None`` context suppression
    stays as-is — this log replaces the traceback in the persisted trail, not
    the other way round.

    Level rule: ``level=None`` (the default) derives the severity inside this
    helper — ERROR when ``exc`` is not None, WARNING otherwise. The ERROR
    default matters because ``ErrorTrackingLogHandler`` (core/logging_config)
    ingests only records with ``levelno >= ERROR`` into the error_events table
    and the Error Dashboard: exception-backed 503s must emit at ERROR to reach
    the only persisted/queryable sink, while guard-style 503s raised without an
    exception stay at WARNING. Pass an explicit ``level`` only to override.

    ``exc`` hygiene: passing ``exc`` persists its formatted traceback via
    ``exc_info``, and a traceback may embed SQL statements and bind params —
    so ``exc`` must be a DB/engine exception (SQLAlchemyError and friends),
    never a payload-processing exception that could carry request data.

    Deferred mitigation (2026-09-04 transient 503 incident): the suspected
    root cause is database endpoint slowness / connection-pool budget
    exhaustion. Infra mitigation (pool sizing) is intentionally deferred
    pending evidence from the persisted ``service_unavailable`` records; if
    ``db_transient`` dominates with pool-timeout exception classes, tune the
    SQLAlchemy pool BEFORE considering caller-side retries.
    """
    if level is None:
        level = logging.ERROR if exc is not None else logging.WARNING
    record = {
        "reason": reason,
        "route": route,
        "exception_class": type(exc).__name__ if exc is not None else None,
        "detail": detail,
    }
    logger.log(
        level,
        "service_unavailable route=%s reason=%s exception=%s detail=%s",
        record["route"],
        record["reason"],
        record["exception_class"],
        record["detail"],
        extra={"service_unavailable": record},
        exc_info=exc,
    )
