"""Structured reason reporting for fail-closed 503 responses.

2026-09-04 incident: ~8/10 GitHub pr-review webhook POSTs to app.modulo.run
failed with HTTP 503 and no persisted cause existed anywhere — the fail-closed
raise sites logged either a bare log code or nothing, so the reason was lost
the moment the response left the process. The sender sees only the status
code; the structured warning this helper emits is the persisted reason trail.
"""

import logging

logger = logging.getLogger(__name__)


def log_service_unavailable(
    reason: str,
    exc: Exception | None = None,
    *,
    route: str,
    detail: str | None = None,
) -> None:
    """Emit the structured warning that precedes every 503 raise.

    Call this IMMEDIATELY BEFORE each ``HTTPException(503)`` raise. The record
    carries the route/path, a short machine-readable ``reason`` code (e.g.
    ``db_transient``, ``system_bootstrap_degraded``,
    ``snapshot_lock_unavailable``), the exception class name, and a brief
    detail string.

    Details MUST be fixed strings authored at the call site — never request
    payloads, tokens, secrets, or raw exception text (which can embed SQL).
    Passing ``exc`` attaches its traceback via ``exc_info`` (parity with the
    ``_log.exception`` calls this helper replaces) and records the class name.
    The raise keeps its existing semantics: ``from None`` context suppression
    stays as-is — this log replaces the traceback in the persisted trail, not
    the other way round.
    """
    record = {
        "reason": reason,
        "route": route,
        "exception_class": type(exc).__name__ if exc is not None else None,
        "detail": detail,
    }
    logger.warning(
        "service_unavailable route=%s reason=%s exception=%s detail=%s",
        record["route"],
        record["reason"],
        record["exception_class"],
        record["detail"],
        extra={"service_unavailable": record},
        exc_info=exc,
    )
