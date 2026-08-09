"""In-process worker-liveness watchdog with Slack-compatible webhook alerting.

Postmortem (2026-08-08/09): a rolling deploy left both SAQ worker machines
``stopped`` for ~3 hours and nothing alerted a human. The web/app process
stayed up throughout the outage — only the worker machines died. A watchdog
running IN the web process (a plain asyncio task in the FastAPI lifespan —
NOT an SAQ cron job, NOT routed through the system-worker cron path) can
detect worker death and alert within minutes.

Why NOT an SAQ cron: if the workers are down, the cron path is down — the
alert must not depend on the very thing it watches.

Design:
- Every ``watchdog_tick_seconds`` (default 30s) reads SAQ worker liveness
  DIRECTLY from Redis: the ``saq:{queue}:stats`` worker_info zset (TTL 90s,
  expiry scores in ms) and the system-cron heartbeats
  (``saq:cron:heartbeat:fire_due_triggers:*``).
- "All workers dead" = no live worker on ANY configured queue (runs AND
  system), sustained for ``watchdog_worker_stale_seconds`` (default 180s =
  2x the 90s worker_info TTL).
- On alert: POST ``{"text": ...}`` to ``alert_webhook_url`` (default-off —
  nothing is sent until the operator sets it), deduped by a Redis cooldown
  key so it fires at most once per ``watchdog_alert_cooldown_seconds``.
- Fail-open on Redis read errors: cannot confirm death => never alert, just
  log and continue. The watchdog never crashes the web process.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import time
from datetime import UTC, datetime

import httpx
import redis.asyncio as aioredis

from modulo.settings import Settings, get_settings

_log = logging.getLogger("modulo.watchdog")

# Redis keys owned by this watchdog. The cooldown key doubles as the alert
# dedup fence (at most one alert per cooldown window) and the heartbeat key
# lets an operator verify the watchdog itself is alive (a dead watchdog is
# detectable by comparing the stored timestamp to now).
_ALERT_COOLDOWN_KEY = "watchdog:alert:worker_liveness"
_WATCHDOG_HEARTBEAT_KEY = "watchdog:heartbeat:worker_liveness"

# SAQ worker_info heartbeat TTL is 90s (saq_worker._TIMERS["worker_info"]=89
# +1); the watchdog stale threshold defaults to 2x this. fire_due_triggers
# (system cron) runs every 60s and writes a per-machine cron heartbeat; stale
# fleet-wide = no machine fired within 2x the cadence.
_CRON_CADENCE_SECONDS = 60
_CRON_STALE_SECONDS = 2 * _CRON_CADENCE_SECONDS

# Webhook POST timeout — a hung webhook must never stall the watchdog loop.
_WEBHOOK_TIMEOUT_SECONDS = 10.0


def _hostname() -> str:
    """Machine identity shared with the health gate (FLY_MACHINE_ID or hostname)."""
    return os.environ.get("FLY_MACHINE_ID") or os.environ.get("HOSTNAME") or "unknown"


def _configured_queues(settings: Settings) -> list[str]:
    """PREFIX-AWARE queue names for this environment (runs + system).

    Mirrors ``modulo.api.routes.health._configured_queues`` — reimplemented
    here so the watchdog (a ``core`` module) does not import from ``api``.
    """
    runs_queue = settings.saq_runs_queue
    system_queue = runs_queue.replace("runs", "system") if "runs" in runs_queue else "system"
    return [runs_queue, system_queue]


async def _live_worker_count(redis: aioredis.Redis, queue_name: str) -> int:
    """Live SAQ workers on *queue_name* from the worker_info stats zset.

    Live = a ``saq:{queue}:stats`` zset entry whose expiry score (ms) is in
    the future (worker_info timer 89s / TTL 90s). Scores are milliseconds
    (SAQ's ``now()`` is ``int(time.time() * 1000)``), so the lower bound must
    be milliseconds too — otherwise stale workers are never filtered.
    """
    stats_key = f"saq:{queue_name}:stats"
    now_ms = int(time.time() * 1000)
    members = await redis.zrangebyscore(stats_key, now_ms, "+inf")
    return len(members) if members else 0


async def _cron_heartbeat_fresh(redis: aioredis.Redis) -> bool:
    """True when ANY machine's ``fire_due_triggers`` cron heartbeat is fresh.

    Fleet-wide semantics (matches the ``app``-machine health gate): the
    system cron runs on worker machines only, so a stale fleet-wide reading
    means no worker's cron scheduler has fired within 2x its 60s cadence.
    """
    heartbeat_keys = await redis.keys("saq:cron:heartbeat:fire_due_triggers:*")
    now = time.time()
    for key in heartbeat_keys:
        raw = await redis.get(key)
        if raw is None:
            continue
        try:
            last_ts = float(raw)
        except (TypeError, ValueError):
            continue
        if now - last_ts <= _CRON_STALE_SECONDS:
            return True
    return False


async def _write_watchdog_heartbeat(redis: aioredis.Redis) -> None:
    """Stamp this watchdog's own liveness key so a dead watchdog is detectable."""
    await redis.set(_WATCHDOG_HEARTBEAT_KEY, str(int(time.time())))


async def _cooldown_active(redis: aioredis.Redis) -> bool:
    """True when an alert fired within the cooldown window (dedup)."""
    try:
        return bool(await redis.exists(_ALERT_COOLDOWN_KEY))
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        _log.warning("watchdog.cooldown_read_failed: %s", exc)
        return False


async def _set_cooldown(redis: aioredis.Redis, settings: Settings) -> None:
    """Set the alert dedup fence. Best-effort — a write failure never raises."""
    try:
        await redis.set(
            _ALERT_COOLDOWN_KEY,
            str(int(time.time())),
            ex=settings.watchdog_alert_cooldown_seconds,
        )
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        _log.warning("watchdog.cooldown_write_failed: %s", exc)


async def _post_webhook(settings: Settings, conditions: list[str]) -> None:
    """Best-effort Slack-compatible webhook POST. Never raises out of the task."""
    webhook_url = settings.alert_webhook_url
    if not webhook_url:
        _log.warning("watchdog.webhook_no_url")
        return

    text = (
        "\U0001f6a8 *Modulo watchdog: worker-liveness alert*\n"
        + "\n".join(f"\u2022 {condition}" for condition in conditions)
        + f"\nDetected at {datetime.now(UTC).isoformat()} on {_hostname()}"
    )
    payload = json.dumps({"text": text}).encode()
    try:
        async with httpx.AsyncClient(timeout=_WEBHOOK_TIMEOUT_SECONDS) as client:
            resp = await client.post(
                webhook_url,
                content=payload,
                headers={"Content-Type": "application/json", "User-Agent": "Modulo-Watchdog/1.0"},
            )
        if not resp.is_success:
            _log.warning("watchdog.webhook_http_error status=%s", resp.status_code)
    except asyncio.CancelledError:
        raise
    except httpx.RequestError as exc:
        _log.warning("watchdog.webhook_request_failed: %s", exc)
    except Exception as exc:
        _log.warning("watchdog.webhook_unknown_failure: %s", exc)


async def _maybe_alert(settings: Settings, redis: aioredis.Redis, conditions: list[str]) -> None:
    """Fire one deduped alert per cooldown window. Never raises."""
    message = "; ".join(conditions)
    if not settings.alert_webhook_url:
        # Default-off: the watchdog still ticks and logs, but never POSTs.
        _log.warning("watchdog.alert_suppressed_no_webhook conditions=%s", message)
        return
    if await _cooldown_active(redis):
        _log.warning("watchdog.alert_suppressed_cooldown conditions=%s", message)
        return
    await _set_cooldown(redis, settings)
    # JSON-formatter logs are not reliably rendered in `fly logs` — the alert
    # event needs stdout visibility (repo lesson).
    print(f"[watchdog] ALERT worker-liveness: {message}", flush=True)  # noqa: T201
    await _post_webhook(settings, conditions)


async def _evaluate_once(
    settings: Settings,
    redis: aioredis.Redis,
    all_dead_since: float | None,
) -> float | None:
    """One watchdog tick; returns the updated ``all_dead_since`` timestamp.

    ``all_dead_since`` is ``None`` while at least one worker is live, else the
    wall-clock time the fleet first looked fully dead. An alert fires only
    when the fleet has been continuously dead for the stale threshold.

    Fail-open: any Redis read error returns the state unchanged — death cannot
    be confirmed, so we neither alert nor lose progress on a transient blip.
    """
    now = time.time()

    # 1. SAQ worker liveness — is ANY worker live on ANY configured queue?
    any_live = False
    try:
        for queue_name in _configured_queues(settings):
            if await _live_worker_count(redis, queue_name) > 0:
                any_live = True
                break
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        _log.warning("watchdog.worker_read_failed: %s", exc)
        return all_dead_since

    conditions: list[str] = []
    if any_live:
        all_dead_since = None
    else:
        if all_dead_since is None:
            all_dead_since = now
        dead_for = now - all_dead_since
        if dead_for >= settings.watchdog_worker_stale_seconds:
            conditions.append(
                f"no live SAQ worker on any queue for {dead_for:.0f}s "
                f"(stale threshold {settings.watchdog_worker_stale_seconds}s)"
            )
        else:
            _log.info("watchdog.workers_dead_detected dead_for=%.0fs", dead_for)

    # 2. System-cron liveness — fire_due_triggers heartbeat fresh anywhere?
    try:
        if not await _cron_heartbeat_fresh(redis):
            conditions.append("system-cron (fire_due_triggers) heartbeat stale fleet-wide")
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        _log.warning("watchdog.cron_read_failed: %s", exc)

    if conditions:
        await _maybe_alert(settings, redis, conditions)

    return all_dead_since


async def run_worker_liveness_watchdog(settings: Settings | None = None) -> None:
    """In-process watchdog loop (started by the FastAPI lifespan).

    Plain asyncio background task — deliberately NOT an SAQ cron job and NOT
    routed through the system-worker cron path. If the workers are down, the
    cron path is down, so the alert must not depend on it.
    """
    settings = settings or get_settings()
    all_dead_since: float | None = None
    while True:
        redis: aioredis.Redis | None = None
        try:
            redis = aioredis.Redis.from_url(settings.redis_url, socket_connect_timeout=3)
            await _write_watchdog_heartbeat(redis)
            all_dead_since = await _evaluate_once(settings, redis, all_dead_since)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            # Fail-open: a Redis failure cannot confirm worker death — log and
            # continue rather than alert or crash the web process.
            _log.warning("watchdog.tick_failed: %s", exc)
        finally:
            if redis is not None:
                with contextlib.suppress(Exception):
                    await redis.aclose()
        await asyncio.sleep(settings.watchdog_tick_seconds)
