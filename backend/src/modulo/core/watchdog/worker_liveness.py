"""In-process worker-liveness watchdog with multi-channel alerting.

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
- Edge-triggered alerting: ONE alert email when worker-liveness
  conditions first appear, ONE recovery ("all clear") email when conditions
  FULLY clear, and nothing in between — no repeated alerts during a
  sustained incident. Multi-machine safe: the alert edge is claimed
  atomically with ``SET key NX`` and the recovery edge with ``GETDEL``, so
  whichever app machine ticks first wins and the others stay silent. The
  alert state lives in Redis (``_ALERT_STATE_KEY``) so it survives app
  restarts.
- On alert: fan out to EVERY configured channel — generic webhook
  (``alert_webhook_url``, Slack-compatible ``{"text": ...}``), Microsoft
  Teams webhook (``alert_teams_webhook_url``, MessageCard), and/or email
  (``alert_email_to`` + SMTP settings). Each channel is isolated: one
  channel's failure never blocks the others. Default-off — nothing is sent
  until at least one channel is configured.
- Fail-open on Redis read errors: cannot confirm death => never alert, just
  log and continue. The watchdog never crashes the web process.
"""

from __future__ import annotations

import asyncio
import contextlib
import html
import json
import logging
import os
import time
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from typing import Any

import httpx
import redis.asyncio as aioredis

from modulo.core.email_service import EmailSendingError, send_email
from modulo.settings import Settings, get_settings

_log = logging.getLogger("modulo.watchdog")

# Redis keys owned by this watchdog. The alert-state key stores the active
# incident (JSON: conditions + started_at) so the watchdog is edge-triggered:
# one alert when it first appears, one recovery when it clears. The heartbeat
# key lets an operator verify the watchdog itself is alive (a dead watchdog is
# detectable by comparing the stored timestamp to now).
_ALERT_STATE_KEY = "watchdog:alert:state:worker_liveness"
_WATCHDOG_HEARTBEAT_KEY = "watchdog:heartbeat:worker_liveness"

# Self-expiring TTL for the watchdog's own liveness stamp (2026-09 Redis audit,
# FAR-538). The watchdog loop restamps every tick (settings.watchdog_tick_seconds,
# default 30s — settings.py). Unlike the cron heartbeat above, NO code reads
# this key: it is an operator diagnostic ("compare the stored timestamp to
# now"), so a MISSING key yields exactly the conclusion a stale timestamp
# already gives — the watchdog is not ticking — and nothing gates on it.
# TTL = 6 ticks (180s): far beyond one missed tick (a live watchdog's 30s
# refresh never lets it lapse) and equal to the watchdog's own default
# stale-detection window (settings.watchdog_worker_stale_seconds, 180s), so a
# dead watchdog's stamp self-expires in about the horizon in which watchdog
# death would be noticed, instead of persisting a dead timestamp forever.
# The derivation assumes the DEFAULT tick; the test suite pins
# _WATCHDOG_TICK_SECONDS to the settings default so drift fails loudly (and
# even if an operator raises the tick, this key is diagnostic-only — nothing
# gates on it).
_WATCHDOG_TICK_SECONDS = 30  # settings.watchdog_tick_seconds default
WATCHDOG_HEARTBEAT_TTL_SECONDS = _WATCHDOG_TICK_SECONDS * 6

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

    The heartbeat keys are discovered with ``SCAN`` — never ``KEYS`` — so a
    Redis instance already under load cannot be blocked by an O(N) full-key
    scan exactly when a fleet-wide death needs to be detected.
    """
    now = time.time()
    async for key in redis.scan_iter(match="saq:cron:heartbeat:fire_due_triggers:*"):
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
    """Stamp this watchdog's own liveness key so a dead watchdog is detectable.

    Self-expiring (FAR-538): see WATCHDOG_HEARTBEAT_TTL_SECONDS — a dead
    watchdog's stamp must not persist a dead timestamp forever.
    """
    await redis.set(_WATCHDOG_HEARTBEAT_KEY, str(int(time.time())), ex=WATCHDOG_HEARTBEAT_TTL_SECONDS)


async def _claim_alert(redis: aioredis.Redis, settings: Settings, conditions: list[str]) -> bool:
    """Atomically claim the ALERT edge for this incident (SET key NX).

    Multiple app machines run the watchdog; without an atomic claim two of
    them could both send the alert email. ``SET key <json> NX EX <ttl>``
    returns True only for the machine that wins — every other machine sees
    the state already present and stays silent. The stored JSON carries the
    conditions and start time so the recovery email can report duration.
    A Redis failure fails OPEN (True) — if the state cannot be written, an
    alert is sent rather than lost.
    """
    payload = json.dumps({"conditions": conditions, "started_at": time.time()})
    try:
        return bool(
            await redis.set(
                _ALERT_STATE_KEY,
                payload,
                nx=True,
                ex=settings.watchdog_alert_state_ttl_seconds,
            )
        )
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        _log.warning("watchdog.alert_claim_failed: %s", exc)
        return True


async def _claim_recovery(redis: aioredis.Redis) -> dict[str, Any] | None:
    """Atomically claim the RECOVERY edge (GETDEL).

    Returns the stored incident state ONLY to the machine that cleared the
    key — every other machine gets None and stays silent, so exactly one
    recovery email is sent per incident. A Redis failure returns None (no
    recovery claimed this tick; the next tick retries — no recovery is lost,
    only delayed).
    """
    try:
        raw = await redis.getdel(_ALERT_STATE_KEY)
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        _log.warning("watchdog.recovery_claim_failed: %s", exc)
        return None
    if not raw:
        return None
    try:
        parsed = json.loads(raw)
        return parsed if isinstance(parsed, dict) else None
    except (TypeError, ValueError):
        return None


def _channel_configured(settings: Settings) -> bool:
    """True when at least one alert channel is configured.

    Channels: generic webhook (``alert_webhook_url``), Teams webhook
    (``alert_teams_webhook_url``), or email (``alert_email_to`` AND
    ``smtp_host``). Default-off: with none configured the watchdog ticks and
    logs but never sends anything.
    """
    return bool(
        settings.alert_webhook_url
        or settings.alert_teams_webhook_url
        or (settings.alert_email_to and settings.smtp_host)
    )


def _alert_text(conditions: list[str]) -> str:
    """Shared human-readable alert text (title + condition bullets + detection stamp)."""
    return (
        "\U0001f6a8 *Modulo watchdog: worker-liveness alert*\n"
        + "\n".join(f"\u2022 {condition}" for condition in conditions)
        + f"\nDetected at {datetime.now(UTC).isoformat()} on {_hostname()}"
    )


def _recovery_text(state: dict[str, Any]) -> str:
    """Human-readable recovery text from the cleared incident state."""
    prior_conditions = state.get("conditions") or []
    started_at = state.get("started_at")
    duration = f" for {(time.time() - float(started_at)):.0f}s" if started_at else ""
    return (
        "\u2705 *Modulo watchdog: worker-liveness recovered*\n"
        "The following conditions have cleared"
        + duration
        + ":\n"
        + "\n".join(f"\u2022 {condition}" for condition in prior_conditions)
        + f"\nResolved at {datetime.now(UTC).isoformat()} on {_hostname()}"
    )


async def _post_webhook_payload(url: str, payload: bytes, channel: str) -> None:
    """Best-effort JSON webhook POST. Never raises out of the task.

    ``channel`` demultiplexes the failure log keys (``webhook`` for the generic
    Slack-compatible webhook, ``teams_webhook`` for the Microsoft Teams
    MessageCard webhook) so a failure stays attributable to its channel. The
    two webhook channels are otherwise byte-identical in their HTTP posting —
    one implementation avoids drift between the copies.
    """
    try:
        async with httpx.AsyncClient(timeout=_WEBHOOK_TIMEOUT_SECONDS) as client:
            resp = await client.post(
                url,
                content=payload,
                headers={"Content-Type": "application/json", "User-Agent": "Modulo-Watchdog/1.0"},
            )
        if not resp.is_success:
            _log.warning("watchdog.%s_http_error status=%s", channel, resp.status_code)
    except asyncio.CancelledError:
        raise
    except httpx.RequestError as exc:
        _log.warning("watchdog.%s_request_failed: %s", channel, exc)
    except Exception as exc:
        _log.warning("watchdog.%s_unknown_failure: %s", channel, exc)


async def _post_generic_webhook(settings: Settings, text: str) -> None:
    """Best-effort Slack-compatible webhook POST. Never raises out of the task."""
    webhook_url = settings.alert_webhook_url
    if not webhook_url:
        _log.warning("watchdog.webhook_no_url")
        return
    await _post_webhook_payload(webhook_url, json.dumps({"text": text}).encode(), "webhook")


async def _post_teams_webhook(settings: Settings, text: str) -> None:
    """Best-effort Microsoft Teams MessageCard POST. Never raises out of the task."""
    webhook_url = settings.alert_teams_webhook_url
    if not webhook_url:
        _log.warning("watchdog.teams_webhook_no_url")
        return
    payload = json.dumps(
        {
            "@type": "MessageCard",
            "@context": "http://schema.org/extensions",
            "summary": "Modulo watchdog: worker-liveness alert",
            "title": "Modulo watchdog: worker-liveness alert",
            "text": text,
        }
    ).encode()
    await _post_webhook_payload(webhook_url, payload, "teams_webhook")


def _parse_alert_email_to(alert_email_to: str | None) -> list[str]:
    """Split a comma-separated recipient list — trim whitespace, drop empties."""
    if not alert_email_to:
        return []
    return [address.strip() for address in alert_email_to.split(",") if address.strip()]


async def _send_email_alert(
    settings: Settings,
    conditions: list[str],
    *,
    recovery_state: dict[str, Any] | None = None,
) -> None:
    """Best-effort SMTP email alert (or recovery when ``recovery_state`` is given).

    ``send_email`` is synchronous (smtplib with retries) — it MUST run via
    ``asyncio.to_thread`` so it never blocks the watchdog's event loop.
    """
    to_emails = _parse_alert_email_to(settings.alert_email_to)
    if not to_emails:
        _log.warning("watchdog.email_no_recipients")
        return
    if not settings.smtp_host:
        _log.warning("watchdog.email_no_smtp_host")
        return

    if recovery_state is not None:
        subject = "[Modulo Watchdog] Worker-liveness recovered"
        prior = recovery_state.get("conditions") or []
        body_html = (
            "<html><body>"
            "<h2>Modulo watchdog: worker-liveness recovered</h2>"
            "<p>The following worker-liveness conditions have cleared:</p>"
            "<ul>" + "".join(f"<li>{html.escape(condition)}</li>" for condition in prior) + "</ul>"
            f"<p>Resolved at {html.escape(datetime.now(UTC).isoformat())} "
            f"on {html.escape(_hostname())}</p>"
            "</body></html>"
        )
        body_text = _recovery_text(recovery_state)
    else:
        subject = "[Modulo Watchdog] Worker-liveness alert"
        body_html = (
            "<html><body>"
            "<h2>Modulo watchdog: worker-liveness alert</h2>"
            "<p>The in-process watchdog detected one or more worker-liveness conditions:</p>"
            "<ul>" + "".join(f"<li>{html.escape(condition)}</li>" for condition in conditions) + "</ul>"
            f"<p>Detected at {html.escape(datetime.now(UTC).isoformat())} "
            f"on {html.escape(_hostname())}</p>"
            "</body></html>"
        )
        body_text = _alert_text(conditions)
    try:
        await asyncio.to_thread(
            send_email,
            settings,
            to_emails,
            subject,
            body_html,
            body_text,
        )
    except asyncio.CancelledError:
        raise
    except EmailSendingError as exc:
        _log.warning("watchdog.email_send_failed: %s", exc)
    except Exception as exc:
        _log.warning("watchdog.email_unknown_failure: %s", exc)


async def _dispatch_channel(coro_factory: Callable[[], Awaitable[None]], log_key: str) -> None:
    """Run one alert channel, isolating its failure from the others.

    Each channel is wrapped in its own try/except so one channel's failure
    never prevents the others from delivering (mirrors the error-forwarder
    isolation lesson). ``asyncio.CancelledError`` is re-raised (the watchdog
    task is being torn down) while any other exception is logged with the
    channel-specific *log_key* and swallowed so the caller never raises.
    """
    try:
        await coro_factory()
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        _log.warning("watchdog.%s: %s", log_key, exc)


async def _send_alerts(
    settings: Settings,
    conditions: list[str],
    *,
    recovery_state: dict[str, Any] | None = None,
) -> None:
    """Fan the alert (or recovery) out to every configured channel.

    Each channel is wrapped in its own try/except so one channel's failure
    never prevents the others from delivering (mirrors the error-forwarder
    isolation lesson). Never raises out of the watchdog task.
    """
    text = _recovery_text(recovery_state) if recovery_state is not None else _alert_text(conditions)
    if settings.alert_webhook_url:
        await _dispatch_channel(lambda: _post_generic_webhook(settings, text), "channel_generic_failed")
    if settings.alert_teams_webhook_url:
        await _dispatch_channel(lambda: _post_teams_webhook(settings, text), "channel_teams_failed")
    if settings.alert_email_to and settings.smtp_host:
        await _dispatch_channel(
            lambda: _send_email_alert(settings, conditions, recovery_state=recovery_state),
            "channel_email_failed",
        )


async def _maybe_alert(settings: Settings, redis: aioredis.Redis, conditions: list[str]) -> None:
    """Edge-triggered alert/recovery state machine. Never raises.

    - ``conditions`` non-empty: this is the ALERT edge. The incident state is
      claimed atomically (SET NX) so exactly ONE machine sends the alert email
      and later ticks during the same incident stay silent (state present).
    - ``conditions`` empty: this is the RECOVERY edge. The incident state is
      cleared atomically (GETDEL) so exactly ONE machine sends the recovery
      ("all clear") email, and later healthy ticks stay silent (no state).
    """
    if not _channel_configured(settings):
        # Default-off: the watchdog still ticks and logs, but never sends.
        _log.warning(
            "watchdog.alert_suppressed_no_channel conditions=%s "
            "(no generic webhook / Teams webhook / email configured)",
            "; ".join(conditions),
        )
        return
    if conditions:
        if not await _claim_alert(redis, settings, conditions):
            # Another machine already claimed this incident — stay silent.
            _log.info("watchdog.alert_already_active conditions=%s", "; ".join(conditions))
            return
        # JSON-formatter logs are not reliably rendered in `fly logs` — the alert
        # event needs stdout visibility (repo lesson).
        print(f"[watchdog] ALERT worker-liveness: {'; '.join(conditions)}", flush=True)  # noqa: T201
        await _send_alerts(settings, conditions)
    else:
        state = await _claim_recovery(redis)
        if state is None:
            return  # nothing was alerted — healthy state, stay silent
        print("[watchdog] RECOVERY worker-liveness: conditions cleared", flush=True)  # noqa: T201
        await _send_alerts(settings, [], recovery_state=state)


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
