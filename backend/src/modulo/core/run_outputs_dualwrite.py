"""Run-outputs failure orchestration — core-side surface for the FAR-583
``run_node_outputs`` migration.

The DB layer owns the store chokepoints (``crud.run.update_run_status`` blobs
branch, ``recovery._apply_recovery_markers``, the node-runner marker
savepoint). When the new-table store-write fails fail-closed, the chokepoint
raises :class:`~modulo.db.crud.run_node_outputs.DualWriteError` and its
caller's transaction rolls back — the legacy run row is never half-written.
THIS module is what makes that abort survivable:

* :func:`guard_dual_write` — the context manager core callers wrap around the
  chokepoint invocations. On ``DualWriteError`` it (1) rolls back the caller's
  transaction FIRST (releasing the run-row lock so the separate-session
  terminalize below cannot deadlock on it), then (2) orchestrates, then (3)
  re-raises so the surrounding ``session.begin()`` sees a clean abort.
* :func:`orchestrate_dual_write_failure` — (a) terminalizes the run ``failed``
  with ``error_code='dual_write_failed'`` by REUSING the fenced
  ``saq_hooks._mark_run_failed`` primitive in a SEPARATE session bounded by
  ``lock_timeout`` (a lock collision degrades instead of hanging; rowcount 0 =
  superseded, no state change); (b) emits the ``dual_write_failed`` error
  event via ``ErrorIngestionService`` (separate session, the run's org,
  ``source='backend'`` — the ``saq_hooks._ingest_error_event`` pattern),
  aggregated per org through a Redis edge gate so a broken write path cannot
  storm the Error Dashboard.

Post-B2a there is no dual-write kill-switch: with the legacy ``runs`` blob
columns no longer written (B1) there is no legacy-only mode to fall back to,
so the store write is unconditional and the "dual-write" name is historical
— the only machinery left is this failure path. (The legacy REDIS counters
and the catch-up sweep were removed at B2a; the error event IS the failure
signal.)

The ONE bounded in-session retry's retryable-SQLSTATE set lives in
``db.crud.run`` (``_DUAL_WRITE_RETRYABLE_SQLSTATES``) — the authoritative
copy; this module deliberately does NOT mirror it.

Every Redis / DB step here is best-effort and failure-isolated: the
orchestration must never replace the original abort with a new failure.
"""

from __future__ import annotations

import asyncio
import logging
import os
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from modulo.version import get_version

_log = logging.getLogger(__name__)

# Redis edge-gate window. The failure-event gate aggregates per
# (org, fingerprint) window so a broken write path produces one event per
# minute per org, not one per run.
_DUAL_WRITE_FAILED_EVENT_WINDOW_SECONDS = 60

# Cap for the ``error_detail`` embedded in the dual_write_failed event's
# context_json (qa rider d): the detail can carry blob content (the failed
# write's payload shapes the message), so it is sanitized
# (``sanitize_error_text``) AND truncated before it lands in error_events.
_ERROR_DETAIL_EMBED_LIMIT = 2000

# Bounded lock_timeout for the separate-session terminalize: a lock collision
# with the still-open (poisoned) caller transaction degrades to a no-op
# instead of hanging the abort path. 2s is ample for a single-row UPDATE.
_MARK_RUN_FAILED_LOCK_TIMEOUT_MS = 2000

_DUAL_WRITE_FAILED_ERROR_CODE = "dual_write_failed"


def _open_redis() -> Any:
    """A short-lived bounded Redis client (best-effort channel only).

    Both socket timeouts are bounded (qa M13): a hung Redis (accept but never
    answer) must stall the abort/degraded paths for at most ~2s per call, not
    forever.
    """
    from redis.asyncio import Redis as AsyncRedis

    from modulo.settings import get_settings

    return AsyncRedis.from_url(get_settings().redis_url, socket_connect_timeout=2, socket_timeout=2)


async def _redis_increment_windowed(key: str, ttl_seconds: int, *, log_event: str) -> int | None:
    """Shared SET-NX-EX + increment leg (qa M12).

    TTL properties, in order:

    1. CREATION stamp — ``SET key 0 EX ttl NX`` before the increment so a
       brand-new key is born with its TTL (with the old INCR-then-EXPIRE
       ordering a process death between the two left a no-TTL key).
    2. EXPIRY-IN-GAP heal — ``EXPIRE key ttl NX`` AFTER the increment (qa
       iteration 2, Major 6): the two round-trips are not atomic, and a key
       that expires in the gap is RECREATED by INCR/INCRBY with NO TTL —
       NX-EXPIRE restores one so the key can never suppress an event window
       forever (a rolling defer would do the opposite: an event-suppression
       window must fire again after its TTL, not be deferred).

    ``None`` = Redis unavailable (the caller decides the fallback); a
    failure is logged under *log_event* (cancellation excepted).
    """
    client: Any = None
    try:
        client = _open_redis()
        await client.set(key, 0, ex=ttl_seconds, nx=True)
        value = int(await client.incrby(key, 1))
        await client.expire(key, ttl_seconds, nx=True)
        return value
    except asyncio.CancelledError:
        raise
    except Exception:
        _log.warning("%s key=%s", log_event, key, exc_info=True)
        return None
    finally:
        if client is not None:
            await client.aclose()


async def _redis_incr_window(key: str, ttl_seconds: int) -> int | None:
    """INCR a FIXED-TTL event window; ``None`` = Redis down.

    Delegates to the shared :func:`_redis_increment_windowed` leg (creation
    stamp + the NX expiry-in-gap heal).
    """
    return await _redis_increment_windowed(
        key,
        ttl_seconds,
        log_event="run_outputs.redis_incr_window_failed",
    )


async def _emit_error_event(
    org_id: uuid.UUID,
    *,
    level: str,
    message: str,
    context_json: dict[str, Any],
) -> None:
    """Ingest one error event in a SEPARATE session (the saq_hooks._ingest_error_event pattern).

    Mirrors :func:`modulo.core.error_tracking.saq_hooks._ingest_error_event`:
    the standalone engine factory (own connection pool), ``set_rls_org`` for
    the run's org, ``source='backend'`` (the ``ck_error_events_source``
    vocabulary). Raises on failure — callers wrap best-effort.
    """
    from modulo.core.error_tracking import ErrorIngestionService
    from modulo.core.error_tracking.saq_hooks import _open_factory
    from modulo.db.rls import set_rls_org

    async with _open_factory()() as session, session.begin():
        await set_rls_org(session, org_id)
        await ErrorIngestionService().ingest(
            session,
            org_id,
            {
                "level": level,
                "message": message,
                "source": "backend",
                "stacktrace": None,
                "context_json": context_json,
                "environment": os.environ.get("MODULO_ENV", "development"),
                "version": get_version(),
            },
        )


async def _emit_dual_write_failed_event(
    exc: Any,
    *,
    rowcount: int,
    error_detail: str | None,
) -> None:
    """The aggregated ``dual_write_failed`` event (per-org edge gate).

    The per-(org, fingerprint) aggregation: a Redis INCR window counts the
    failures inside ``_DUAL_WRITE_FAILED_EVENT_WINDOW_SECONDS``; only the
    first occurrence in the window is ingested (with the running count in
    ``context_json``), so a persistently broken write path produces one event
    per window per org instead of one per run. Redis unavailable → the event
    is emitted unthrottled (the Error Dashboard is the page-worthy channel;
    the abort itself is rare).

    The ``error_detail`` is sanitized AND truncated before it lands in
    ``context_json`` (qa rider d): the detail is derived from the failed
    write's exception text, which can embed blob content — raw blob payloads
    must never be stored verbatim in ``error_events``.
    """
    count = await _redis_incr_window(
        f"saq:run_outputs:dual_write_failed_window:{exc.organisation_id}",
        _DUAL_WRITE_FAILED_EVENT_WINDOW_SECONDS,
    )
    if count is not None and count > 1:
        _log.warning(
            "run_outputs.dual_write_failed_suppressed run=%s org=%s window_count=%s",
            exc.run_id,
            exc.organisation_id,
            count,
        )
        return
    from modulo.core.pipeline_engine.error_codes import sanitize_error_text

    detail_text = sanitize_error_text(error_detail) if error_detail is not None else None
    if detail_text is not None and len(detail_text) > _ERROR_DETAIL_EMBED_LIMIT:
        detail_text = detail_text[:_ERROR_DETAIL_EMBED_LIMIT] + "…"
    await _emit_error_event(
        uuid.UUID(str(exc.organisation_id)),
        level="error",
        message=(
            f"run_node_outputs dual-write failed after retry; run terminalized "
            f"{_DUAL_WRITE_FAILED_ERROR_CODE} (origin={exc.origin}, sqlstate={exc.sqlstate})"
        ),
        context_json={
            "run_id": str(exc.run_id),
            "origin": exc.origin,
            "sqlstate": exc.sqlstate,
            "window_count": count,
            "terminalize_rowcount": rowcount,
            "error_detail": detail_text,
        },
    )


async def orchestrate_dual_write_failure(exc: Any, *, error_detail: str | None = None) -> int:
    """Handle a :class:`DualWriteError` — terminalize + event.

    See the module docstring for the channels. Returns the terminalize
    rowcount (0 = the guards rejected the write: superseded / already
    terminal / cancellation requested / lock-timeout degradation). Never
    raises (except cancellation) — a failure inside the orchestration is
    logged and swallowed; the run's terminalization then falls to the
    periodic dispatcher_reconcile sweeps.
    """
    try:
        return await _orchestrate_dual_write_failure(exc, error_detail=error_detail)
    except asyncio.CancelledError:
        raise
    except Exception:
        _log.exception(
            "run_outputs.dual_write_orchestration_failed run=%s org=%s",
            getattr(exc, "run_id", None),
            getattr(exc, "organisation_id", None),
        )
        return 0


async def _orchestrate_dual_write_failure(exc: Any, *, error_detail: str | None) -> int:
    run_id = exc.run_id
    org_id = exc.organisation_id
    detail = error_detail or str(exc)

    # (a) Terminalize — separate session, claim-token fenced, lock_timeout
    # bounded. rowcount 0 = superseded / already terminal / 'unknown' run
    # (never transitioned) / lock-collision degradation.
    rowcount = 0
    try:
        from modulo.core.error_tracking.saq_hooks import _mark_run_failed

        rowcount = await _mark_run_failed(
            str(run_id),
            str(org_id),
            claim_token=exc.claim_token,
            error_code=_DUAL_WRITE_FAILED_ERROR_CODE,
            error_detail=detail,
            lock_timeout_ms=_MARK_RUN_FAILED_LOCK_TIMEOUT_MS,
        )
    except asyncio.CancelledError:
        raise
    except Exception:
        _log.exception("run_outputs.dual_write_terminalize_failed run=%s", run_id)

    # (b) Aggregated failure event — best-effort.
    try:
        await _emit_dual_write_failed_event(exc, rowcount=rowcount, error_detail=detail)
    except asyncio.CancelledError:
        raise
    except Exception:
        _log.exception("run_outputs.dual_write_event_failed run=%s", run_id)

    _log.error(
        "run_outputs.dual_write_failed run=%s org=%s origin=%s sqlstate=%s terminalize_rowcount=%s",
        run_id,
        org_id,
        exc.origin,
        exc.sqlstate,
        rowcount,
    )
    return rowcount


@asynccontextmanager
async def guard_dual_write(session: AsyncSession) -> AsyncIterator[None]:
    """Catch a :class:`DualWriteError` from a store chokepoint call.

    Ordering is load-bearing: the caller's transaction is rolled back FIRST
    (releasing the run-row lock the terminalize's separate session needs),
    THEN the failure is orchestrated, THEN the error re-raises so the
    surrounding ``session.begin()`` completes its (now no-op) rollback and the
    caller's outer error handling sees a clean fail-closed abort.
    """
    from modulo.db.crud.run_node_outputs import DualWriteError

    try:
        yield
    except DualWriteError as exc:
        try:
            # Full-transaction rollback is deliberate (fail-closed): the caller's
            # entire txn must abort and release the run-row lock so the
            # terminalize path's separate session can proceed. A savepoint
            # (begin_nested) would keep the outer txn alive, contradicting the
            # fail-closed design.
            await session.rollback()  # nosemgrep: session-rollback-abuse
        except asyncio.CancelledError:
            raise
        except Exception:
            _log.exception("run_outputs.dual_write_rollback_failed run=%s", exc.run_id)
        await orchestrate_dual_write_failure(exc)
        raise
