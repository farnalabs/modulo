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
   storm the Error Dashboard; (c) bumps the dedicated
   ``outputs_dual_write_failed`` counter (the dual-write counters — see
   below).
* :func:`is_dual_write_enabled` — the per-call kill-switch read, FLEET-VISIBLE
  (qa C3: the runtime-config store is per-process memory, so an admin-API flip
  is invisible to the SAQ worker machines — the emergency valve would be a
  no-op fleet-wide). Read order:

  1. the Redis key ``saq:run_outputs:dual_write_enabled`` (the fleet path —
      set via :func:`set_dual_write_enabled` or a raw Redis SET; a present
      value of ``"0"``/``"false"`` turns dual-write OFF, anything else
      present turns it ON);
   2. the ``modulo_run_outputs_dual_write`` runtime-config override — a
      TEST/LOCAL-ONLY injection leg (process-local memory). It is
      deliberately NOT registered in ``core.runtime_config.store``'s
      KNOWN_KEYS, so the admin runtime-config API can never set it: a
      web-process-only partial flip would be invisible to the SAQ workers
      and worse than useless during an incident (qa Major 2 — there is
      exactly ONE operator lever for this switch, the Redis key);
   3. default ON (fail-closed).

  Any Redis read FAILURE serves the last-seen latch (qa iteration 2, Major 3):
  an operator's emergency OFF survives a Redis blip — the switch never
  silently re-enables; default ON (fail-closed) applies only when no explicit
  value has ever been read. A successful read of an ABSENT key clears the
  latch (explicit removal honored). Resolved values are served through a
  small in-process TTL cache (~5 seconds, qa Minor 5 / iteration 2 rider 9,
  single-flight): a bounded-staleness window (above the 2s socket timeout so
  one refresh cannot be re-triggered per call) while removing the fresh Redis
  client + GET per chokepoint call — the marker write holds the run row
  FOR UPDATE, and a hung Redis must not stall it per call.
  :func:`set_dual_write_enabled` invalidates the cache (and latches the
  explicit value) so the flipping process sees its own flip immediately. The
  FIRST process-local read of OFF emits a loud boot-time degraded-combo
  warning: readers now serve the new table while writes are legacy-only, and
  the catch-up sweep heals the gap.
* :func:`note_dual_write_disabled` — the kill-switch-OFF degraded signal:
  legacy-only writes continue, and an EDGE-TRIGGERED (first occurrence per
  org per flag-off window) ``dual_write_degraded`` warning event + counter
  record that the new table is no longer being fed (the catch-up sweep heals
  it). The edge outcome is latched process-locally (no client churn per
  write in steady-state OFF); Redis down → the event falls back to a
  process-local token bucket (qa iteration 2, rider 10 — at most one event
  per 10s per process: the outage still surfaces, the per-write storm does
  not).
* the dual-write counters — DEDICATED cumulative Redis keys
  ``saq:run_outputs:counters:<name>`` (qa M10: the app-side bumps used to
  read-modify-write the SHARED ``saq:cron:stats:dispatcher_reconcile`` blob,
  which the 60s tick wholesale-replaces — counters were visible <60s and the
  RMW was non-atomic). :func:`bump_dual_write_counter` INCRs a dedicated key
  with a ROLLING 7-day TTL (qa iteration 2, Major 6: every bump re-stamps it
  — an actively-failing counter never silently resets); the
  dispatcher_reconcile tick READS them into its summary
  (``read_dual_write_counters``) so /healthz still shows the numbers without
  the tick owning or resetting them.

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
import time
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from modulo.version import get_version

_log = logging.getLogger(__name__)

# The runtime-config key backing the dual-write kill-switch's TEST/LOCAL-ONLY
# injection leg (qa Major 2). Deliberately ABSENT from
# ``core.runtime_config.store``'s KNOWN_KEYS — the admin runtime-config API
# rejects unknown keys, so no operator (and no web-process-only partial flip)
# can set it; only tests / local processes inject through the process-local
# store override. Read only AFTER the fleet-visible Redis key (see the module
# docstring); the ONE operator lever is the Redis key.
DUAL_WRITE_ENABLED_KEY = "modulo_run_outputs_dual_write"

# Dual-write kill-switch default: ON (fail-closed). Only the literal "false"
# (any case/whitespace) — and the fleet-visible Redis values "0"/"false" —
# turn dual-write OFF.
_DUAL_WRITE_OFF = "false"
_REDIS_SWITCH_OFF_VALUES = frozenset({"0", "false"})

# The FLEET-VISIBLE kill-switch key (qa C3): every process — web AND SAQ
# worker — reads this Redis key first, so an operator flip reaches the whole
# fleet immediately. Written by :func:`set_dual_write_enabled` (the ops
# runbook's flip procedure) or a raw Redis SET — the switch's ONLY operator
# levers; expires so an emergency OFF cannot silently outlive its incident.
_REDIS_SWITCH_KEY = "saq:run_outputs:dual_write_enabled"

# Redis edge-gate keys + windows. The degraded edge is PER-ORG per flag-off
# window (one event per hour per org while the switch stays off); the
# failure-event gate aggregates per (org, fingerprint) window so a broken
# write path produces one event per minute per org, not one per run.
_DUAL_WRITE_DEGRADED_EDGE_KEY_PREFIX = "saq:run_outputs:dual_write:degraded_edge"
_DUAL_WRITE_DEGRADED_EDGE_TTL_SECONDS = 3600
_DUAL_WRITE_FAILED_EVENT_WINDOW_SECONDS = 60

# Cap for the ``error_detail`` embedded in the dual_write_failed event's
# context_json (qa rider d): the detail can carry blob content (the failed
# write's payload shapes the message), so it is sanitized
# (``sanitize_error_text``) AND truncated before it lands in error_events.
_ERROR_DETAIL_EMBED_LIMIT = 2000

# The DEDICATED cumulative dual-write counters (qa M10). Keys are
# ``saq:run_outputs:counters:<name>``; INCR with a ROLLING 7-day TTL (qa
# iteration 2, Major 6 — every bump re-stamps the window) — the
# dispatcher_reconcile tick's wholesale stats-blob rewrite never touches them
# (the tick only READS them into its summary).
_DUAL_WRITE_COUNTER_PREFIX = "saq:run_outputs:counters:"
_DUAL_WRITE_COUNTER_TTL_SECONDS = 7 * 24 * 3600
DUAL_WRITE_COUNTERS: tuple[str, ...] = (
    "outputs_dual_write_failed",
    "outputs_dual_write_retries",
    "outputs_dual_write_degraded",
    "outputs_dual_write_sentinel_filtered",
    "outputs_dual_write_skipped_no_org",
)

# Bounded lock_timeout for the separate-session terminalize: a lock collision
# with the still-open (poisoned) caller transaction degrades to a no-op
# instead of hanging the abort path. 2s is ample for a single-row UPDATE.
_MARK_RUN_FAILED_LOCK_TIMEOUT_MS = 2000

_DUAL_WRITE_FAILED_ERROR_CODE = "dual_write_failed"

# Boot-time degraded-combo warning (qa C3): the FIRST process-local read of
# the switch as OFF logs a loud warning — with the switch off, readers serve
# the new table while writes are legacy-only and the catch-up sweep heals the
# gap. Once per process.
_BOOT_OFF_WARNING_EMITTED = False


def _warn_switch_off_degraded_once() -> None:
    """Boot-time degraded-combo warning (qa C3), once per process.

    With the switch OFF the deployment is in a deliberate degraded combo:
    READERS still serve the new table while WRITES are legacy-only — the
    catch-up sweep heals the rows the switch-off window skipped. Make that
    loud the first time any process observes it.
    """
    global _BOOT_OFF_WARNING_EMITTED
    if _BOOT_OFF_WARNING_EMITTED:
        return
    _BOOT_OFF_WARNING_EMITTED = True
    _log.warning(
        "run_outputs.dual_write_switch_off_degraded_combo readers serve run_node_outputs "
        "while writes are LEGACY-ONLY — the catch-up sweep heals the skipped rows. "
        "Flip the switch back on (Redis key %r) once the write path is healthy.",
        _REDIS_SWITCH_KEY,
    )


# Module-level sentinel: Redis was UNREACHABLE on the switch read (distinct
# from "read succeeded, key absent" — qa iteration 2, Major 3: a Redis outage
# must not un-flip an operator's active OFF).
_REDIS_UNREACHABLE: Any = object()


async def _read_redis_switch_value() -> Any:
    """The fleet-visible Redis switch value (qa C3).

    Returns the decoded string when the key is PRESENT, ``None`` when the
    read SUCCEEDED but the key is absent (an explicit removal — the caller
    falls through to the test/local runtime-config injection, never an
    operator surface), or the :data:`_REDIS_UNREACHABLE` sentinel when Redis
    could not be read (qa iteration 2, Major 3 — the caller serves the
    last-seen latch instead of falling through, so an emergency OFF survives
    a Redis blip).
    """
    client: Any = None
    try:
        client = _open_redis()
        raw = await client.get(_REDIS_SWITCH_KEY)
        if raw is None:
            return None
        return raw.decode() if isinstance(raw, bytes) else str(raw)
    except asyncio.CancelledError:
        raise
    except Exception:
        _log.warning("run_outputs.dual_write_switch_redis_read_failed", exc_info=True)
        return _REDIS_UNREACHABLE
    finally:
        if client is not None:
            await client.aclose()


# The last-seen latch (qa iteration 2, Major 3): the last EXPLICIT Redis value
# successfully read (True/False), or None when no explicit value has ever been
# seen. On a Redis read EXCEPTION the resolution serves this latch (an
# operator's emergency OFF stays OFF through a Redis blip — previously a
# read failure resolved to the fail-closed ON and silently un-flipped the
# switch ~1s after the blip); default ON only when no explicit value has ever
# been read (fail-closed). A successful read of an ABSENT key clears the
# latch: the operator's explicit key removal is honored (the next outage
# resolves through the fall-through again, not the stale OFF).
_LAST_SEEN_REDIS_STATE: bool | None = None


def _clear_switch_runtime_state() -> None:
    """Drop the cached read + the last-seen latch + the degraded-note maps
    (test isolation; :func:`set_dual_write_enabled` keeps the latch)."""
    global _SWITCH_CACHE, _LAST_SEEN_REDIS_STATE, _SWITCH_REFRESH_LOCK
    global _DEGRADED_LAST_FALLBACK_EMISSION
    _SWITCH_CACHE = None
    _LAST_SEEN_REDIS_STATE = None
    _DEGRADED_NOTED_WINDOW.clear()
    _DEGRADED_LAST_FALLBACK_EMISSION = float("-inf")
    # Rebuild the single-flight lock: asyncio primitives bind to the running
    # loop on first acquire, and pytest-asyncio runs every test on a FRESH
    # loop — a reused module-level lock would raise "bound to a different
    # event loop" on the second test. Production keeps one loop, so the
    # rebuild is test-only in effect.
    _SWITCH_REFRESH_LOCK = asyncio.Lock()


# The switch-read TTL cache (qa Minor 5): :func:`is_dual_write_enabled` runs
# at every dual-write chokepoint AND per marker write — the marker persist
# holds the run row FOR UPDATE, so a fresh Redis client + GET per call lets a
# hung Redis (2s bounded timeout) stall the write path per call. A ~5s
# process-local cache (qa iteration 2 rider 9: comfortably ABOVE the 2s
# socket timeout, so one refresh cannot be re-triggered per call) bounds the
# flip's worst-case staleness while collapsing the per-call client churn.
# Single-flight (rider 9): a concurrent miss awaits the ONE refresh instead
# of each caller opening clients — the in-lock re-check serves the refreshed
# value. Tests reset the cache (and the latch + noted maps) via
# :func:`_reset_switch_read_cache`.
_SWITCH_CACHE_TTL_SECONDS = 5.0
_SWITCH_CACHE: tuple[float, bool] | None = None
_SWITCH_REFRESH_LOCK = asyncio.Lock()


def _reset_switch_read_cache() -> None:
    """Drop the cached kill-switch read (test isolation + post-flip immediacy)."""
    _clear_switch_runtime_state()


async def is_dual_write_enabled() -> bool:
    """Read the dual-write kill-switch (fail-closed ON, fleet-visible, ~5s
    TTL-cached with single-flight refresh — qa Minor 5 / iteration 2 rider 9).

    Resolution order on a cache miss (see the module docstring):
    (1) the Redis key ``saq:run_outputs:dual_write_enabled`` — the fleet path
    every process honours and the ONLY operator lever; (2) the process-local
    runtime-config override — TEST/LOCAL-ONLY injection, deliberately not an
    admin-API surface; (3) default ON. An explicit OFF value on either leg
    (plus the once-per-process boot warning) is the ONLY way dual-write turns
    off. A resolved value (whichever leg produced it) is cached for
    ``_SWITCH_CACHE_TTL_SECONDS``; :func:`set_dual_write_enabled` invalidates
    the cache.

    qa iteration 2 (Major 3): a Redis read EXCEPTION serves the last-seen
    latch (:data:`_LAST_SEEN_REDIS_STATE`) instead of falling through — an
    operator's emergency OFF cannot be silently un-flipped by a Redis blip;
    the latch survives cache expiry. A successful read of an ABSENT key
    clears the latch (explicit removal honored).
    """
    global _SWITCH_CACHE
    cached = _SWITCH_CACHE
    if cached is not None and time.monotonic() < cached[0]:
        return cached[1]
    async with _SWITCH_REFRESH_LOCK:
        # Re-check under the lock: another caller may have refreshed while we
        # awaited the lock (single-flight — one Redis read per window).
        cached = _SWITCH_CACHE
        if cached is not None and time.monotonic() < cached[0]:
            return cached[1]
        enabled = await _resolve_dual_write_enabled()
        _SWITCH_CACHE = (time.monotonic() + _SWITCH_CACHE_TTL_SECONDS, enabled)
        return enabled


async def _resolve_dual_write_enabled() -> bool:
    """The UNCACHED switch resolution (Redis key → test/local override → ON)."""
    value = await _read_redis_switch_value()
    global _LAST_SEEN_REDIS_STATE
    if value is _REDIS_UNREACHABLE:
        # qa iteration 2 (Major 3): serve the last-seen latch on a read
        # EXCEPTION — an active OFF must not silently re-enable; default ON
        # (fail-closed) only when no explicit value has ever been read (then
        # the fall-through to the test/local leg below still applies).
        latched = _LAST_SEEN_REDIS_STATE
        if latched is not None:
            if not latched:
                _warn_switch_off_degraded_once()
            return latched
    elif value is not None:
        enabled = value.strip().lower() not in _REDIS_SWITCH_OFF_VALUES
        _LAST_SEEN_REDIS_STATE = enabled
        if not enabled:
            _warn_switch_off_degraded_once()
        return enabled
    else:
        # Key ABSENT (read succeeded): the operator's explicit removal clears
        # the latch — a later outage falls through instead of serving the
        # stale OFF.
        _LAST_SEEN_REDIS_STATE = None

    try:
        from modulo.core.runtime_config.store import get_runtime_config_store

        value = get_runtime_config_store().get(DUAL_WRITE_ENABLED_KEY)
    except Exception:
        _log.warning("run_outputs.dual_write_switch_read_failed", exc_info=True)
        return True
    if value is None:
        return True
    enabled = value.strip().lower() != _DUAL_WRITE_OFF
    if not enabled:
        _warn_switch_off_degraded_once()
    return enabled


async def set_dual_write_enabled(enabled: bool, ttl_seconds: int) -> None:
    """The ops runbook's fleet-visible kill-switch flip (qa C3) — the switch's
    ONE operator lever (qa Major 2; a raw Redis SET of the same key is the
    equivalent manual procedure).

    Writes the Redis key every process reads per dual-write call (through the
    ~1s TTL cache), so the flip reaches web AND SAQ worker machines
    immediately. *ttl_seconds* bounds the emergency state: an OFF must not
    silently outlive its incident (re-issue the flip when a state must outlive
    the TTL). Raises on a Redis failure — the operator must KNOW the flip did
    not land. The local cache is invalidated so the flipping process observes
    its own flip without waiting out the TTL.
    """
    client: Any = None
    try:
        client = _open_redis()
        await client.set(_REDIS_SWITCH_KEY, "1" if enabled else "0", ex=ttl_seconds)
    finally:
        if client is not None:
            await client.aclose()
    # The flip is the new fleet truth: invalidate the cache AND latch the
    # explicit value (qa iteration 2, Major 3 — an immediate Redis blip after
    # the flip must not un-flip it before the next successful read).
    global _SWITCH_CACHE, _LAST_SEEN_REDIS_STATE
    _SWITCH_CACHE = None
    _LAST_SEEN_REDIS_STATE = enabled
    _log.warning(
        "run_outputs.dual_write_switch_flipped enabled=%s ttl_seconds=%s (fleet-visible Redis key %s)",
        enabled,
        ttl_seconds,
        _REDIS_SWITCH_KEY,
    )


def _open_redis() -> Any:
    """A short-lived bounded Redis client (best-effort channel only).

    Both socket timeouts are bounded (qa M13): a hung Redis (accept but never
    answer) must stall the abort/degraded paths for at most ~2s per call, not
    forever.
    """
    from redis.asyncio import Redis as AsyncRedis

    from modulo.settings import get_settings

    return AsyncRedis.from_url(get_settings().redis_url, socket_connect_timeout=2, socket_timeout=2)


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
        _log.warning("run_outputs.redis_set_nx_failed key=%s", key, exc_info=True)
        return None
    finally:
        if client is not None:
            await client.aclose()


async def _redis_increment_windowed(
    key: str,
    ttl_seconds: int,
    *,
    delta: int = 1,
    rolling: bool,
    log_event: str,
) -> int | None:
    """Shared SET-NX-EX + increment leg (qa M12 + iteration 2 Major 6).

    THREE TTL properties, in order:

    1. CREATION stamp — ``SET key 0 EX ttl NX`` before the increment so a
       brand-new key is born with its TTL (with the old INCR-then-EXPIRE
       ordering a process death between the two left a no-TTL key).
    2. EXPIRY-IN-GAP heal — ``EXPIRE key ttl NX`` AFTER the increment (qa
       iteration 2, Major 6): the two round-trips are not atomic, and a key
       that expires in the gap is RECREATED by INCR/INCRBY with NO TTL —
       NX-EXPIRE restores one so the key can never suppress an event window
       (or persist a counter) forever.
    3. ROLLING vs FIXED — *rolling* (the cumulative counters): a NON-NX
       EXPIRE after every INCRBY makes the window ``last-bump + ttl``, so an
       actively-failing counter never silently resets mid-incident; *fixed*
       (the event-suppression windows): the NX form heals the no-TTL state
       WITHOUT deferring the window (a rolling suppression window would defer
       the second event indefinitely under constant failures).

    ``None`` = Redis unavailable (the caller decides the fallback); a
    failure is logged under *log_event* (cancellation excepted).
    """
    client: Any = None
    try:
        client = _open_redis()
        await client.set(key, 0, ex=ttl_seconds, nx=True)
        value = int(await client.incrby(key, delta))
        await client.expire(key, ttl_seconds, nx=not rolling)
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

    Delegates to the shared :func:`_redis_increment_windowed` leg (fixed
    window: creation stamp + the NX expiry-in-gap heal, never rolling — a
    rolling suppression window would defer the second event indefinitely).
    """
    return await _redis_increment_windowed(
        key,
        ttl_seconds,
        rolling=False,
        log_event="run_outputs.redis_incr_window_failed",
    )


async def bump_dual_write_counter(name: str, delta: int = 1) -> None:
    """INCR one DEDICATED cumulative dual-write counter (qa M10).

    Counters live on ``saq:run_outputs:counters:<name>`` — never on the shared
    ``saq:cron:stats:dispatcher_reconcile`` blob the 60s tick wholesale-
    replaces (the old read-modify-write here made the counters visible <60s
    and was non-atomic). The TTL is a ROLLING 7-day window (qa iteration 2,
    Major 6): every bump re-stamps it (``last-bump + 7d``), so an
    actively-failing counter never silently resets mid-incident, and the
    shared leg's NX-EXPIRE heal closes the expiry-in-gap race. The
    dispatcher_reconcile tick READS these keys into its summary
    (:func:`read_dual_write_counters`) — /healthz keeps showing the numbers
    without the tick owning or resetting them. Best-effort: a Redis failure is
    logged and swallowed (cancellation excepted) — a counter bump must never
    turn a healthy write into a failure.
    """
    if name not in DUAL_WRITE_COUNTERS:
        raise ValueError(f"unknown dual-write counter: {name!r}")
    await _redis_increment_windowed(
        f"{_DUAL_WRITE_COUNTER_PREFIX}{name}",
        _DUAL_WRITE_COUNTER_TTL_SECONDS,
        delta=delta,
        rolling=True,
        log_event="run_outputs.dual_write_counter_bump_failed",
    )


async def read_dual_write_counters(client: Any) -> dict[str, int]:
    """READ the cumulative dual-write counters for the reconcile summary.

    Takes the caller's Redis client (the reconcile tick already holds one);
    missing keys read as 0. Raises on Redis errors — the caller (the tick)
    decides the fail-open behaviour.
    """
    keys = [f"{_DUAL_WRITE_COUNTER_PREFIX}{name}" for name in DUAL_WRITE_COUNTERS]
    values = await client.mget(keys)
    return {name: int(value) if value else 0 for name, value in zip(DUAL_WRITE_COUNTERS, values, strict=True)}


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
        await bump_dual_write_counter("outputs_dual_write_failed")
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


# qa iteration 2 (rider 10): process-local degraded-note state.
#
# *_DEGRADED_NOTED_WINDOW* — "this org's flag-off window was already noted":
# once an edge gate result is recorded (edge fired, or seen-recently), later
# note calls within ~the edge window skip EVERYTHING (no client, no event,
# no counter) — kill-switch-OFF steady state must not open a fresh Redis
# client per legacy-only write. TTL ≈ the Redis edge window, so a Redis-outage
# recovery re-arms it at worst one window late.
#
# *_DEGRADED_LAST_FALLBACK_EMISSION* — the token bucket for the Redis-down
# unthrottled fallback (edge=None): previously EVERY legacy-only write during
# a Redis outage opened a client (which failed) and emitted an event; now at
# most one fallback emission per *_DEGRADED_FALLBACK_MIN_INTERVAL_SECONDS*
# per process — a Redis outage must still surface (the bucket), just not per
# write.
_DEGRADED_NOTED_WINDOW: dict[str, float] = {}
_DEGRADED_FALLBACK_MIN_INTERVAL_SECONDS = 10.0
_DEGRADED_LAST_FALLBACK_EMISSION = float("-inf")


async def _emit_dual_write_degraded_event(run_id: uuid.UUID | str, org_id: uuid.UUID | None, message: str) -> None:
    """Emit one ``dual_write_degraded`` warning event, when the org is resolvable.

    The org-less degraded path has no org to attribute the event to, so it
    stays silent (the log + counter still carry the signal).
    """
    org_uuid = uuid.UUID(str(org_id)) if org_id else None
    if org_uuid is not None:
        await _emit_error_event(
            org_uuid,
            level="warning",
            message=message,
            context_json={"run_id": str(run_id)},
        )


async def _note_dual_write_edge_fired(
    run_id: uuid.UUID | str, org_id: uuid.UUID | None, org_key: str, now: float
) -> None:
    """First occurrence in the flag-off window (edge fired): latch + emit + count."""
    _DEGRADED_NOTED_WINDOW[org_key] = now
    await _emit_dual_write_degraded_event(
        run_id,
        org_id,
        "run_node_outputs dual-write disabled (kill-switch off) — legacy-only writes; sweep will heal",
    )
    await bump_dual_write_counter("outputs_dual_write_degraded")


async def _note_dual_write_redis_down(run_id: uuid.UUID | str, org_id: uuid.UUID | None, now: float) -> None:
    """Redis down (edge is None): the token-bucket fallback emission.

    At most one event per *_DEGRADED_FALLBACK_MIN_INTERVAL_SECONDS* per
    process (a Redis outage must not silence the channel, nor open a failed
    client per write). The counter needs Redis; skip it (as before). The
    window is NOT latched: the next write retries the edge gate so a
    recovered Redis re-arms the real edge immediately.
    """
    global _DEGRADED_LAST_FALLBACK_EMISSION
    _log.warning(
        "run_outputs.dual_write_disabled run=%s org=%s edge_fired=None (redis down, token-bucketed)",
        run_id,
        org_id,
    )
    if now - _DEGRADED_LAST_FALLBACK_EMISSION >= _DEGRADED_FALLBACK_MIN_INTERVAL_SECONDS:
        _DEGRADED_LAST_FALLBACK_EMISSION = now
        await _emit_dual_write_degraded_event(
            run_id,
            org_id,
            "run_node_outputs dual-write disabled (kill-switch off, redis unavailable) "
            "— legacy-only writes; sweep will heal",
        )


async def note_dual_write_disabled(run_id: uuid.UUID | str, org_id: uuid.UUID | None) -> None:
    """Edge-triggered degraded signal for kill-switch-OFF legacy-only writes.

    First occurrence per ORG per flag-off window (Redis SET NX, 1h; qa rider
    c — the edge key is per-org so one org's window cannot mask another's)
    emits one ``dual_write_degraded`` warning event so ops knows the new
    table stopped being fed (the catch-up sweep heals it). qa iteration 2
    (rider 10): the edge outcome is latched process-locally
    (:data:`_DEGRADED_NOTED_WINDOW`, TTL ≈ the edge window) so steady-state
    OFF mode does not open a fresh Redis client per write; the Redis-down
    fallback (edge=None) is throttled to one event per
    *_DEGRADED_FALLBACK_MIN_INTERVAL_SECONDS* per process (the token bucket —
    the outage still surfaces, the per-write storm does not). Best-effort and
    never raises.
    """
    try:
        org_key = str(org_id) if org_id is not None else ""
        now = time.monotonic()
        if now - _DEGRADED_NOTED_WINDOW.get(org_key, float("-inf")) < _DUAL_WRITE_DEGRADED_EDGE_TTL_SECONDS:
            return  # already noted this flag-off window — zero client churn
        edge_key = (
            f"{_DUAL_WRITE_DEGRADED_EDGE_KEY_PREFIX}:{org_id}"
            if org_id is not None
            else _DUAL_WRITE_DEGRADED_EDGE_KEY_PREFIX
        )
        edge = await _redis_set_nx(edge_key, _DUAL_WRITE_DEGRADED_EDGE_TTL_SECONDS)
        if edge is True:
            # First occurrence in the window: emit the degraded event.
            await _note_dual_write_edge_fired(run_id, org_id, org_key, now)
        elif edge is False:
            # Seen recently (another write in this window noted it): count it,
            # latch the window locally, no event, no further clients.
            _DEGRADED_NOTED_WINDOW[org_key] = now
            await bump_dual_write_counter("outputs_dual_write_degraded")
        else:
            await _note_dual_write_redis_down(run_id, org_id, now)
    except asyncio.CancelledError:
        raise
    except Exception:
        _log.exception("run_outputs.dual_write_disabled_note_failed run=%s", run_id)


async def note_dual_write_retry(run_id: uuid.UUID | str, sqlstate: str | None) -> None:
    """Best-effort counter bump for the ONE bounded in-session retry."""
    await bump_dual_write_counter("outputs_dual_write_retries")
    _log.warning("run_outputs.dual_write_retry run=%s sqlstate=%s", run_id, sqlstate)


async def note_dual_write_marker_failure(run_id: str, node_id: str, attempt_key: str | None) -> None:
    """Best-effort counter bump when the node-runner marker savepoint fails.

    The legacy marker write still commits when the savepoint failure is
    savepoint-scoped (the savepoint rolled back ONLY the new-table insert);
    the sweep + the 0177 repair heal the missing row. When the failure
    aborts the WHOLE transaction (deadlock / shutdown / connection loss) the
    caller logs ``legacy_marker_also_lost`` — the legacy write is rolled
    back too and the claim must be loud. Counted on
    ``outputs_dual_write_failed`` — the hold criteria treat ANY dual-write
    failure as page-worthy.
    """
    await bump_dual_write_counter("outputs_dual_write_failed")
    _log.exception(
        "sandbox_agent.raw_output_marker_new_table_write_failed run=%s node_id=%s attempt_key=%s",
        run_id,
        node_id,
        attempt_key,
    )
