"""Readiness-degradation email alerts for the operator (FAR-1446, in-app half).

Modulo already had a rich health surface (``/healthz/ready`` and its ~14
sub-checks) and a working SMTP path (``core.email_service``), but nothing
connected them: when the app degraded *while still up*, nobody was emailed.
This module closes that gap from the existing system-cron path
(``saq_worker.health_readiness_alert``, every 5 minutes) — no new scheduler.

Design decisions (each mirrors something the repo already does rather than
inventing a mechanism):

* **Reuse, not reimplement.** The health evaluation is
  ``modulo.api.routes.health.evaluate_readiness`` — the SAME code the
  ``/healthz/ready`` route runs — imported lazily inside the default observer
  (a ``core`` module must not import ``api`` at module-import time:
  ``api.routes.health`` imports ``core.cron_helpers``). The alert therefore
  describes exactly what readiness reports, sub-check by sub-check.
* **Durable dedup state in Redis**, one JSON document at ``STATE_KEY``: the
  last state we notified plus the pending-confirmation counters, so dedup
  survives process restarts and is read from the store on every tick (never
  held in memory). This follows the repo's existing alert-edge mechanism —
  ``core.watchdog.worker_liveness`` keeps its edge state in Redis the same way
  — and it is deliberately NOT the database: a degraded/down database is one
  of the states being alerted about, so the dedup store must not be the thing
  that is currently broken.
* **Edge-triggered, confirmed by hysteresis.** A new state must be observed on
  ``CONFIRM_TICKS`` consecutive ticks before any notification. A single-probe
  blip never emails, and a one-tick flap produces ZERO emails (neither the
  alert nor the recovery edge confirms), so a flapping check cannot ping-pong
  the inbox. Once confirmed, exactly one email per incident (alert) and one
  at the end (recovery) — never one per tick.
* **Interval: 5 minutes** (``_CRON_EVERY_5_MINUTES`` in ``saq_worker``),
  matching the other advisory system sweeps (stale_run_recovery,
  slot_reconciliation, hitl_park_sweep, ...). One readiness evaluation per
  5 minutes is noise next to the platform probes that already hit
  ``/healthz/ready`` every 15-30s, and ``CONFIRM_TICKS = 2`` bounds the
  worst-case notification latency at ~10 minutes — the right trade for an
  in-app degradation email. The fast/hard full-outage case is covered
  out-of-process by the compose deployment's Gatus sentinel (PR #1260), not
  here: this cron intentionally depends on the system worker being alive.
* **Quiet, not silent, when email is unconfigured.** The compose deployment
  ships no SMTP config, so that is the default state: the check still
  evaluates health and still advances its state machine, but never calls the
  sender, never raises, and logs that alerting is disabled (and what to set)
  at most once per ``DISABLED_LOG_INTERVAL_SECONDS`` at INFO.
* **Send-then-commit.** The notification state advances only after the sender
  reports success, so a failed SMTP send is retried on the next confirmed
  tick instead of being swallowed. ``unique=True`` on the SAQ cron entry means
  only one fleet-wide execution runs per scheduled slot, so there is no
  concurrent tick to race the read-modify-write; a save failure after a
  successful send can at worst re-send once, which is logged.
"""

from __future__ import annotations

import asyncio
import contextlib
import html
import json
import logging
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

import redis.asyncio as aioredis

from modulo.core.alert_context import alert_context_html, alert_context_text, alert_environment_line
from modulo.core.email_service import EmailSendingError, send_email
from modulo.settings import Settings, get_settings, resolve_instance_identity

_log = logging.getLogger(__name__)

#: Durable dedup/confirmation state — one JSON document shared by every tick
#: (multi-machine safe: the SAQ ``unique=True`` cron slot serialises ticks).
STATE_KEY = "saq:cron:alert:state:health_readiness"
#: Self-expiring: mirrors ``watchdog_alert_state_ttl_seconds`` (default 7 days).
#: An incident that never recovers re-notifies at most once per TTL instead of
#: the key (and the incident record) persisting forever.
STATE_TTL_SECONDS = 7 * 24 * 3600
#: Consecutive ticks a state must hold before it is notified (hysteresis).
CONFIRM_TICKS = 2
#: At most one "alerting is disabled" log per this window (quiet, not silent).
DISABLED_LOG_INTERVAL_SECONDS = 3600
#: Bound the recovered-conditions list we persist (a pathological check set
#: must not bloat the state document).
_MAX_PERSISTED_CONDITIONS = 50


#: ``settings.alert_email_to`` split into recipients (mirrors the watchdog's
#: ``_parse_alert_email_to`` — comma-separated, trimmed, empties dropped).
def _recipients(settings: Settings) -> list[str]:
    if not settings.alert_email_to:
        return []
    return [address.strip() for address in settings.alert_email_to.split(",") if address.strip()]


def alerting_configured(settings: Settings) -> bool:
    """True when the email alert channel is configured.

    Both halves are required: ``ALERT_EMAIL_TO`` (who) and ``SMTP_HOST`` (how).
    The compose deployment ships neither, so this is ``False`` by default and
    the check runs in its quiet mode.
    """
    return bool(settings.smtp_host and _recipients(settings))


# Set when "alerting is disabled" was last logged; None = never (so the very
# first tick always logs). Module-level on purpose: the rate limit is a
# per-process log-hygiene concern, NOT alert dedup state (that lives in Redis).
_last_disabled_log_at: float | None = None


def _log_disabled_once(now: float) -> None:
    """Log that alerting is disabled — at most once per hour, per process."""
    global _last_disabled_log_at
    if _last_disabled_log_at is not None and now - _last_disabled_log_at < DISABLED_LOG_INTERVAL_SECONDS:
        return
    _last_disabled_log_at = now
    _log.info(
        "health_alerts.disabled: readiness alerting is off — set SMTP_HOST and ALERT_EMAIL_TO "
        "to email the operator when readiness degrades (health is still evaluated; no email is sent)"
    )


@dataclass
class SubCheck:
    """One readiness sub-check's outcome (status + optional detail)."""

    status: str
    detail: str | None = None


@dataclass
class HealthObservation:
    """A readiness evaluation result — the alert's view of app health.

    ``status`` is the aggregate readiness status (``ok`` / ``degraded`` /
    ``unavailable``); ``checks`` carries every sub-check, advisory ones
    included, so the email can name exactly what is wrong.
    """

    status: str
    checks: dict[str, SubCheck]

    @property
    def observed_state(self) -> str:
        """``healthy`` only at aggregate ``ok`` — degraded AND unavailable are
        unhealthy (both mean an operator should know)."""
        return "healthy" if self.status == "ok" else "unhealthy"

    def conditions(self) -> list[str]:
        """Human-readable bullets for every non-``ok`` check (sorted, stable)."""
        bullets: list[str] = []
        for name in sorted(self.checks):
            check = self.checks[name]
            if check.status == "ok":
                continue
            bullet = f"{name}: {check.status}"
            if check.detail:
                bullet += f" ({check.detail})"
            bullets.append(bullet)
        return bullets


async def _observe_readiness() -> HealthObservation:
    """Default observer: the SAME evaluation the /healthz/ready route runs.

    Imported lazily to keep the core -> api dependency inside a function
    (``api.routes.health`` imports ``core.cron_helpers`` at module level).
    """
    from modulo.api.routes.health import evaluate_readiness

    response = await evaluate_readiness()
    return HealthObservation(
        status=response.status,
        checks={name: SubCheck(status=check.status, detail=check.detail) for name, check in response.checks.items()},
    )


#: The check's injectable collaborators — tests drive transitions with a fake
#: observer and a stub sender (no real SMTP, no network, no real readiness
#: probe).
HealthObserver = Callable[[], Awaitable[HealthObservation]]
EmailSender = Callable[[Settings, list[str], str, str, str], Awaitable[bool]]


async def _send_via_smtp(
    settings: Settings,
    to: list[str],
    subject: str,
    body_html: str,
    body_text: str,
) -> bool:
    """Default sender: ``send_email`` off the event loop, failures swallowed
    to ``False`` so the tick can retry on the next confirmed evaluation.

    ``send_email`` is synchronous (smtplib + retries) — it MUST run via
    ``asyncio.to_thread`` so it never blocks the worker's event loop.
    """
    try:
        return bool(await asyncio.to_thread(send_email, settings, to, subject, body_html, body_text))
    except asyncio.CancelledError:
        raise
    except EmailSendingError as exc:
        _log.warning("health_alerts.email_send_failed: %s", exc)
        return False
    except Exception as exc:
        # A sender failure must never crash the cron tick — it returns False
        # so the transition is retried on the next confirmed evaluation.
        _log.warning("health_alerts.email_send_failed: %s", exc)
        return False


@dataclass
class _AlertState:
    """The persisted notification state (Redis JSON document at STATE_KEY)."""

    #: Last state we actually notified: ``"healthy"``, ``"unhealthy"``, or
    #: None when nothing has ever been emailed (cold start — never triggers a
    #: recovery email).
    notified: str | None = None
    #: State under confirmation (hysteresis) and how many consecutive ticks
    #: it has held.
    pending: str | None = None
    pending_count: int = 0
    #: The conditions the alert email reported, so the recovery email can say
    #: exactly what cleared.
    conditions: list[str] = field(default_factory=list)
    #: Wall-clock time the incident was notified (for the recovery duration).
    since: float | None = None

    def to_json(self) -> str:
        return json.dumps(
            {
                "notified": self.notified,
                "pending": self.pending,
                "pending_count": self.pending_count,
                "conditions": self.conditions,
                "since": self.since,
            }
        )

    @classmethod
    def from_raw(cls, raw: Any) -> _AlertState:
        """Parse a stored document; anything malformed degrades to a fresh
        state (and is logged) — a corrupt key must never crash the tick or
        spam notifications."""
        if raw is None:
            return cls()
        try:
            payload = json.loads(raw)
        except (TypeError, ValueError):
            _log.warning("health_alerts.state_unparsable — starting fresh")
            return cls()
        if not isinstance(payload, dict):
            _log.warning("health_alerts.state_unexpected_shape — starting fresh")
            return cls()
        notified = payload.get("notified")
        pending = payload.get("pending")
        raw_count = payload.get("pending_count")
        raw_conditions = payload.get("conditions")
        raw_since = payload.get("since")
        conditions = [c for c in raw_conditions if isinstance(c, str)] if isinstance(raw_conditions, list) else []
        return cls(
            notified=notified if notified in ("healthy", "unhealthy") else None,
            pending=pending if pending in ("healthy", "unhealthy") else None,
            pending_count=raw_count if isinstance(raw_count, int) and raw_count >= 0 else 0,
            conditions=conditions[:_MAX_PERSISTED_CONDITIONS],
            since=float(raw_since) if isinstance(raw_since, int | float) else None,
        )


def _host() -> str:
    """Instance identity (platform-neutral, ADR 043) — names the sender host."""
    return resolve_instance_identity()


def _alert_html(settings: Settings, status: str, conditions: list[str], observed_at: str) -> str:
    items = "".join(f"<li>{html.escape(condition)}</li>" for condition in conditions)
    return (
        "<html><body>"
        "<h2>Modulo: readiness degraded</h2>"
        f"<p>Readiness is now <strong>{html.escape(status)}</strong>. Failing checks:</p>"
        f"<ul>{items}</ul>"
        f"<p>Detected at {html.escape(observed_at)} on {html.escape(_host())}</p>"
        "<p>This is the first notification for this incident — no further alert "
        "emails will be sent until readiness recovers.</p>"
        f"{alert_context_html(settings)}"
        "</body></html>"
    )


def _alert_text(settings: Settings, status: str, conditions: list[str], observed_at: str) -> str:
    return (
        "Modulo: readiness degraded\n"
        f"Readiness is now {status}. Failing checks:\n"
        + "\n".join(f"- {condition}" for condition in conditions)
        + f"\nDetected at {observed_at} on {_host()}\n"
        + alert_context_text(settings)
    )


def _recovery_html(
    settings: Settings,
    prior_conditions: list[str],
    duration_seconds: float | None,
    resolved_at: str,
) -> str:
    items = "".join(f"<li>{html.escape(condition)}</li>" for condition in prior_conditions)
    duration = f" after {duration_seconds:.0f}s" if duration_seconds is not None else ""
    return (
        "<html><body>"
        "<h2>Modulo: readiness recovered</h2>"
        f"<p>The following conditions have cleared{html.escape(duration)}:</p>"
        f"<ul>{items}</ul>"
        f"<p>Resolved at {html.escape(resolved_at)} on {html.escape(_host())}</p>"
        f"{alert_context_html(settings)}"
        "</body></html>"
    )


def _recovery_text(
    settings: Settings,
    prior_conditions: list[str],
    duration_seconds: float | None,
    resolved_at: str,
) -> str:
    duration = f" after {duration_seconds:.0f}s" if duration_seconds is not None else ""
    return (
        "Modulo: readiness recovered\n"
        f"The following conditions have cleared{duration}:\n"
        + "\n".join(f"- {condition}" for condition in prior_conditions)
        + f"\nResolved at {resolved_at} on {_host()}\n"
        + alert_context_text(settings)
    )


def _stamp(message: str) -> None:
    """Best-effort stdout stamp for an alert event (``fly logs`` renders JSON
    logs unreliably — same lesson as the watchdog).

    STRICTLY best-effort: this runs after the alert email was delivered but
    before the dedup state is committed, so a ``print`` failure
    (``UnicodeEncodeError`` on a non-UTF-8 stdout, ``BrokenPipeError``, ...)
    must never propagate — a raise here would skip ``redis.set`` and the next
    confirmed tick would re-send a DUPLICATE alert. Best-effort fails open,
    with a log.
    """
    try:
        print(message, flush=True)  # noqa: T201
    except Exception:
        _log.warning("health_alerts.stamp_print_failed", exc_info=True)


async def _notify_alert(
    settings: Settings,
    sender: EmailSender,
    *,
    configured: bool,
    status: str,
    conditions: list[str],
    state: _AlertState,
    now: float,
) -> str:
    """Alert edge: email once, then record the incident. Returns the action."""
    if not configured:
        # Quiet path: state untouched, so a later SMTP configuration still
        # alerts on the next confirmed tick. The disabled notice was already
        # logged (rate-limited) at the top of the tick.
        return "disabled"
    recipients = _recipients(settings)
    observed_at = datetime.now(UTC).isoformat()
    delivered = await sender(
        settings,
        recipients,
        f"[Modulo] Readiness {status}",
        _alert_html(settings, status, conditions, observed_at),
        _alert_text(settings, status, conditions, observed_at),
    )
    if not delivered:
        return "send_failed"
    state.notified = "unhealthy"
    state.conditions = list(conditions)
    state.since = now
    # Stamp carries only the conditions + the environment line: ALERT_CONTEXT
    # is repr=False precisely to keep it out of logs, so it never goes to
    # stdout. The stamp is best-effort (see _stamp) — it cannot skip the
    # state commit below.
    _stamp(f"[health-alert] ALERT readiness={status}: {'; '.join(conditions)} | {alert_environment_line(settings)}")
    return "alert"


async def _notify_recovery(
    settings: Settings,
    sender: EmailSender,
    *,
    configured: bool,
    state: _AlertState,
    now: float,
) -> str:
    """Recovery edge: email that the incident ended, then close it out."""
    if not configured:
        return "disabled"
    recipients = _recipients(settings)
    resolved_at = datetime.now(UTC).isoformat()
    duration_seconds = (now - state.since) if state.since is not None else None
    prior_conditions = list(state.conditions)
    delivered = await sender(
        settings,
        recipients,
        "[Modulo] Readiness recovered",
        _recovery_html(settings, prior_conditions, duration_seconds, resolved_at),
        _recovery_text(settings, prior_conditions, duration_seconds, resolved_at),
    )
    if not delivered:
        return "send_failed"
    state.notified = "healthy"
    state.conditions = []
    state.since = None
    # Best-effort stamp (see _stamp): the state above is already mutated in
    # memory and committed by the caller after this returns — a print failure
    # must not abort the commit.
    _stamp(
        f"[health-alert] RECOVERY readiness=ok: {'; '.join(prior_conditions) or 'conditions cleared'}"
        f" | {alert_environment_line(settings)}"
    )
    return "recovery"


async def run_health_alert_check(
    *,
    observe: HealthObserver | None = None,
    sender: EmailSender | None = None,
    redis_client: aioredis.Redis | None = None,
    settings: Settings | None = None,
    now: Callable[[], float] | None = None,
) -> dict[str, Any]:
    """One health-alert tick — evaluate readiness, notify on a confirmed
    transition, persist the dedup state. Never raises (a bad tick is logged;
    the next 5-minute tick re-evaluates everything).

    Returns a small SAQ-friendly result dict for the cron's job output.
    """
    settings = settings or get_settings()
    observe_fn: HealthObserver = observe or _observe_readiness
    send_fn: EmailSender = sender or _send_via_smtp
    now_fn: Callable[[], float] = now or time.time

    try:
        observation = await observe_fn()
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        # A failed evaluation is logged, never fatal — the next tick retries.
        _log.warning("health_alerts.observe_failed: %s", exc)
        return {"status": "observe_failed", "action": "none", "error": str(exc)[:200]}

    observed = observation.observed_state
    conditions = observation.conditions()
    current_time = now_fn()

    configured = alerting_configured(settings)
    if not configured:
        _log_disabled_once(current_time)

    owns_client = redis_client is None
    redis = (
        aioredis.Redis.from_url(settings.redis_url, socket_connect_timeout=3) if redis_client is None else redis_client
    )
    action = "none"
    try:
        state = _AlertState.from_raw(await redis.get(STATE_KEY))
        # Hysteresis: track consecutive ticks in the observed state.
        if state.pending == observed:
            state.pending_count += 1
        else:
            state.pending = observed
            state.pending_count = 1
        confirmed = state.pending_count >= CONFIRM_TICKS

        if confirmed and observed == "unhealthy" and state.notified != "unhealthy":
            action = await _notify_alert(
                settings,
                send_fn,
                configured=configured,
                status=observation.status,
                conditions=conditions,
                state=state,
                now=current_time,
            )
        elif confirmed and observed == "healthy" and state.notified == "unhealthy":
            action = await _notify_recovery(
                settings,
                send_fn,
                configured=configured,
                state=state,
                now=current_time,
            )

        await redis.set(STATE_KEY, state.to_json(), ex=STATE_TTL_SECONDS)
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        # Fail quiet: an unreadable/unwritable store means we cannot dedup, so
        # NO notification decision is taken this tick (never spam, never a
        # silent no-op — the warning IS the signal, and the next tick retries).
        # A bad tick must never crash the worker.
        _log.warning("health_alerts.tick_failed: %s", exc)
        return {"status": "failed", "action": "none", "error": str(exc)[:200]}
    finally:
        if owns_client:
            with contextlib.suppress(Exception):
                await redis.aclose()

    return {
        "status": observed,
        "action": action,
        "pending_count": state.pending_count,
        "notified": state.notified or "none",
    }
