"""Run-outputs dual-write orchestration — the core-side failure + kill-switch
surface for the FAR-583 ``run_node_outputs`` migration.

The DB layer owns the dual-write chokepoints (``crud.run.update_run_status``
both branches, ``recovery._apply_recovery_markers``, the node-runner marker
savepoint). When the new-table leg fails fail-closed, the chokepoint raises
:class:`~modulo.db.crud.run_node_outputs.DualWriteError` and its caller's
transaction rolls back — the legacy run row is never half-written. THIS
module is what makes that abort survivable:

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
  storm the Error Dashboard; (c) bumps the ``outputs_dual_write_failed`` /
  ``outputs_dual_write_retries`` counters on the shared dispatcher_reconcile
  Redis stats key (the ``/healthz/ready`` channel).
* :func:`is_dual_write_enabled` — the per-call kill-switch read. The switch is
  the ``modulo_run_outputs_dual_write`` runtime-config key (default ``true``,
  flippable mid-incident via the admin runtime-config override API without a
  redeploy). Fail-closed: any read failure or unparseable value keeps
  dual-write ON; only the literal ``false`` disables it. NEVER cached at
  import — every dual-write call re-reads the store.
* :func:`note_dual_write_disabled` — the kill-switch-OFF degraded signal:
  legacy-only writes continue, and an EDGE-TRIGGERED (first occurrence per
  flag-off window) ``dual_write_degraded`` warning event + counter record that
  the new table is no longer being fed (the catch-up sweep heals it).

Every Redis / DB step here is best-effort and failure-isolated: the
orchestration must never replace the original abort with a new failure.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from modulo.version import get_version

_log = logging.getLogger(__name__)

# The runtime-config key backing the dual-write kill-switch. Registered in
# ``core.runtime_config.store``'s KNOWN_KEYS (default ``"true"``,
# hot_reloadable). Read PER CALL — never cached at import — so an operator can
# flip it mid-incident via the admin runtime-config override API.
DUAL_WRITE_ENABLED_KEY = "modulo_run_outputs_dual_write"

# Dual-write kill-switch default: ON (fail-closed). Only the literal "false"
# (any case/whitespace) turns dual-write OFF.
_DUAL_WRITE_OFF = "false"

# Redis edge-gate keys + windows. The degraded edge is per flag-off window
# (one event per hour while the switch stays off); the failure-event gate
# aggregates per (org, fingerprint) window so a persistently broken write path
# produces one event per minute per org, not one per run.
_DUAL_WRITE_DEGRADED_EDGE_KEY = "saq:run_outputs:dual_write:degraded_edge"
_DUAL_WRITE_DEGRADED_EDGE_TTL_SECONDS = 3600
_DUAL_WRITE_FAILED_EVENT_WINDOW_SECONDS = 60

# Bounded lock_timeout for the separate-session terminalize: a lock collision
# with the still-open (poisoned) caller transaction degrades to a no-op
# instead of hanging the abort path. 2s is ample for a single-row UPDATE.
_MARK_RUN_FAILED_LOCK_TIMEOUT_MS = 2000

_DUAL_WRITE_FAILED_ERROR_CODE = "dual_write_failed"

# Retryable SQLSTATEs for the ONE bounded in-session retry (mirror of the
# chokepoint's set — re-declared here only for the docstring cross-reference;
# the authoritative copy lives in db.crud.run).
RETRYABLE_SQLSTATES = frozenset({"40001", "40P01", "53300", "57014"})


def is_dual_write_enabled() -> bool:
    """Read the dual-write kill-switch PER CALL (fail-closed ON).

    ``override > env > default`` via the runtime-config store; ``false`` (any
    case) disables, everything else — including a store read failure — keeps
    dual-write ON.
    """
    from modulo.core.runtime_config.store import get_runtime_config_store

    try:
        value = get_runtime_config_store().get(DUAL_WRITE_ENABLED_KEY)
    except Exception:  # pragma: no cover — the store read cannot realistically raise
        _log.warning("run_outputs.dual_write_switch_read_failed", exc_info=True)
        return True
    if value is None:
        return True
    return value.strip().lower() != _DUAL_WRITE_OFF


def _open_redis() -> Any:
    """A short-lived bounded Redis client (best-effort channel only)."""
    from redis.asyncio import Redis as AsyncRedis

    from modulo.settings import get_settings

    return AsyncRedis.from_url(get_settings().redis_url, socket_connect_timeout=2)


async def _redis_set_nx(key: str, ttl_seconds: int) -> bool | None:
    """SET NX EX edge gate; ``True`` = first occurrence, ``False`` = seen
    recently, ``None`` = Redis unavailable (the caller decides the fallback)."""
    client: Any = None
    try:
        client = _open_redis()
        return bool(await client.set(key, "1", ex=ttl_seconds, nx=True))
    except asyncio.CancelledError:
        raise
    except Exception:
        return None
    finally:
        if client is not None:
            await client.aclose()


async def _redis_incr_window(key: str, ttl_seconds: int) -> int | None:
    """INCR with a TTL stamped on the first increment; ``None`` = Redis down."""
    client: Any = None
    try:
        client = _open_redis()
        count = int(await client.incr(key))
        if count == 1:
            await client.expire(key, ttl_seconds)
        return count
    except asyncio.CancelledError:
        raise
    except Exception:
        return None
    finally:
        if client is not None:
            await client.aclose()


async def bump_dispatcher_reconcile_counter(field: str, delta: int = 1) -> None:
    """Best-effort read-modify-write bump of one dispatcher_reconcile stat.

    Writes the SHARED ``saq:cron:stats:dispatcher_reconcile`` key (the
    ``/healthz/ready`` channel) preserving the existing payload — including
    its ``last_run_at`` — and the shared TTL, so a bump never makes a dead
    cron look fresh. The cron tick rewrites the blob wholesale each minute, so
    app-process bumps are visible until the next tick; the field defaults are
    declared in ``cron_helpers`` (both the stats dict and the setter) so the
    tick's write keeps the vocabulary. Raises on Redis failure — callers wrap
    best-effort.
    """
    from modulo.core.cron_helpers import (
        DISPATCHER_RECONCILE_STATS_KEY,
        DISPATCHER_RECONCILE_STATS_TTL_SECONDS,
    )

    client: Any = None
    try:
        client = _open_redis()
        raw = await client.get(DISPATCHER_RECONCILE_STATS_KEY)
        stats: dict[str, Any] = {}
        if raw:
            try:
                parsed = json.loads(raw)
                if isinstance(parsed, dict):
                    stats = parsed
            except (TypeError, ValueError):
                stats = {}
        stats[field] = int(stats.get(field, 0) or 0) + delta
        await client.set(
            DISPATCHER_RECONCILE_STATS_KEY,
            json.dumps(stats),
            ex=DISPATCHER_RECONCILE_STATS_TTL_SECONDS,
        )
    finally:
        if client is not None:
            await client.aclose()


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
            "error_detail": error_detail,
        },
    )


async def orchestrate_dual_write_failure(exc: Any, *, error_detail: str | None = None) -> int:
    """Handle a :class:`DualWriteError` — terminalize + event + counters.

    See the module docstring for the three channels. Returns the terminalize
    rowcount (0 = the guards rejected the write: superseded / already terminal
    / cancellation requested / lock-timeout degradation). Never raises
    (except cancellation) — a failure inside the orchestration is logged and
    swallowed; the run's terminalization then falls to the periodic
    dispatcher_reconcile sweeps.
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

    # (c) Redis counters — best-effort.
    try:
        await bump_dispatcher_reconcile_counter("outputs_dual_write_failed")
    except asyncio.CancelledError:
        raise
    except Exception:
        _log.exception("run_outputs.dual_write_counter_failed run=%s", run_id)

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
    """Catch a :class:`DualWriteError` from a dual-write chokepoint call.

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


async def note_dual_write_disabled(run_id: uuid.UUID | str, org_id: uuid.UUID | None) -> None:
    """Edge-triggered degraded signal for kill-switch-OFF legacy-only writes.

    First occurrence per flag-off window (Redis SET NX, 1h) emits one
    ``dual_write_degraded`` warning event so ops knows the new table stopped
    being fed (the catch-up sweep heals it); subsequent writes inside the
    window only bump the counter. Best-effort and never raises: the event
    fires BEFORE the counter bump so a Redis outage cannot silence the event
    channel.
    """
    try:
        edge = await _redis_set_nx(_DUAL_WRITE_DEGRADED_EDGE_KEY, _DUAL_WRITE_DEGRADED_EDGE_TTL_SECONDS)
        _log.warning(
            "run_outputs.dual_write_disabled run=%s org=%s edge_fired=%s",
            run_id,
            org_id,
            edge,
        )
        if edge:
            org_uuid = uuid.UUID(str(org_id)) if org_id else None
            message = "run_node_outputs dual-write disabled (kill-switch off) — legacy-only writes; sweep will heal"
            if org_uuid is not None:
                await _emit_error_event(
                    org_uuid,
                    level="warning",
                    message=message,
                    context_json={"run_id": str(run_id)},
                )
        if edge is not None:
            try:
                await bump_dispatcher_reconcile_counter("outputs_dual_write_degraded")
            except asyncio.CancelledError:
                raise
            except Exception:
                _log.exception("run_outputs.dual_write_degraded_counter_failed run=%s", run_id)
    except asyncio.CancelledError:
        raise
    except Exception:
        _log.exception("run_outputs.dual_write_disabled_note_failed run=%s", run_id)


async def note_dual_write_retry(run_id: uuid.UUID | str, sqlstate: str | None) -> None:
    """Best-effort counter bump for the ONE bounded in-session retry."""
    try:
        await bump_dispatcher_reconcile_counter("outputs_dual_write_retries")
    except asyncio.CancelledError:
        raise
    except Exception:
        _log.exception("run_outputs.dual_write_retry_note_failed run=%s", run_id)
    _log.warning("run_outputs.dual_write_retry run=%s sqlstate=%s", run_id, sqlstate)


async def note_dual_write_marker_failure(run_id: str, node_id: str, attempt_key: str | None) -> None:
    """Best-effort counter bump when the node-runner marker savepoint fails.

    The legacy marker write still commits (the savepoint scoped the loss to
    the new-table insert); the sweep + the 0177 repair heal the missing row.
    Counted on ``outputs_dual_write_failed`` — the hold criteria treat ANY
    dual-write failure as page-worthy.
    """
    try:
        await bump_dispatcher_reconcile_counter("outputs_dual_write_failed")
    except asyncio.CancelledError:
        raise
    except Exception:
        _log.exception("run_outputs.dual_write_marker_note_failed run=%s", run_id)
    _log.exception(
        "sandbox_agent.raw_output_marker_new_table_write_failed run=%s node_id=%s attempt_key=%s",
        run_id,
        node_id,
        attempt_key,
    )
