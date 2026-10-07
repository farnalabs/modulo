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
* **Advisory sweeps alert too — but only the REAL breakage ones (FAR-1571).**
  The readiness aggregate deliberately excludes the advisory checks, so
  keying alert-worthiness on the aggregate alone made a dead or erroring
  system sweep (``stale_run_recovery``, ``slot_reconciliation``,
  ``runner_marker_sweep``, ...) invisible in-app once the external uptime
  monitor went away. ``REAL_FAILURE_ADVISORY_CHECKS`` names the advisory
  checks whose failure IS real breakage: a non-``ok`` result on any of them
  marks the observation unhealthy even when the aggregate stays ``ok``, and
  the alert reports ``degraded`` (never the misleading aggregate ``ok``).
  The benign advisories are deliberately NOT in that set and must not page:
  ``event_loop_lag`` (a transient stall diagnostic), ``break_glass`` (an
  expected config posture), and ``db_hygiene`` (a GRADED failure already
  gates the aggregate — so it already alerts — while its NOT-MEASURED probe
  is advisory per FAR-1510 and is not a hygiene failure at all). The
  hysteresis below is unchanged, so a single transient advisory blip still
  never emails. On recovery the email reports ONLY the alert-time conditions
  that actually cleared — the alert-time list can contain a benign advisory
  that is STILL degraded when the real failure recovers, and claiming that
  one "cleared" would be false.
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

from modulo.core.alert_context import (
    alert_context_html,
    alert_context_text,
    alert_environment_line,
    stamp_stdout,
)
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

#: Advisory checks whose non-``ok`` result is REAL breakage and must alert
#: (FAR-1571). Single source of truth for the classification — the readiness
#: aggregate in ``api.routes.health`` excludes all advisory checks, so these
#: dead/erroring sweeps would otherwise emit nothing in-app.
#:
#: These are exactly the advisory sweep/probe checks from the readiness
#: ``checks`` dict (``api.routes.health`` is the taxonomy). Deliberately
#: EXCLUDED as benign/other-channel:
#:   * ``event_loop_lag`` — transient stall diagnostic (visible in the body;
#:     must not page on its own),
#:   * ``break_glass`` — expected config posture,
#:   * ``db_hygiene`` — a GRADED failure already gates the aggregate (so it
#:     already alerts); its NOT-MEASURED probe is advisory (FAR-1510) and is
#:     not a hygiene failure.
#:
#: ``dispatcher_reconcile`` is the one member that is ALSO a gating check: it
#: gates the aggregate at its ``unavailable`` tier (a silently dead reconcile
#: cron); only its ``degraded`` tier (a single missed 60s tick) is advisory —
#: see the aggregation in ``api.routes.health``. Both routes merge into the
#: same binary ``observed_state``, so an incident there still yields exactly
#: ONE alert email and ONE recovery email (never double-counted, no second
#: edge — the state machine has one unhealthy edge regardless of which route
#: made it unhealthy).
REAL_FAILURE_ADVISORY_CHECKS: frozenset[str] = frozenset(
    {
        "dispatcher_reconcile",
        "stale_run_recovery",
        "slot_reconciliation",
        "hitl_park_sweep",
        "runner_workspace_reconcile",
        "runner_marker_sweep",
        "runner_health_probe",
    }
)


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
        """``healthy`` only when the aggregate is ``ok`` AND no real-failure
        advisory check is failing.

        Degraded AND unavailable are unhealthy (both mean an operator should
        know). Because the readiness aggregate EXCLUDES the advisory checks
        (FAR-1571), a dead/erroring sweep in
        ``REAL_FAILURE_ADVISORY_CHECKS`` is unhealthy too even while the
        aggregate still reads ``ok`` — the FAR-1156 blind spot this closes.
        Benign advisories (``event_loop_lag``, ``break_glass``,
        ``db_hygiene``'s not-measured probe) are not in that set, so they
        never flip the state and never page.
        """
        if self.status != "ok":
            return "unhealthy"
        if any(self.checks[name].status != "ok" for name in REAL_FAILURE_ADVISORY_CHECKS if name in self.checks):
            return "unhealthy"
        return "healthy"

    @property
    def reported_status(self) -> str:
        """The status an alert email must report — never misleading (FAR-1571).

        ``unavailable`` when the aggregate is unavailable; otherwise
        ``degraded`` when the observation is alerting (the aggregate is ``ok``
        but a real-failure advisory check is broken — the subject must NOT
        read ``[Modulo] Readiness ok``); otherwise ``ok`` (no alert fires).
        """
        if self.status == "unavailable":
            return "unavailable"
        return "degraded" if self.observed_state == "unhealthy" else "ok"

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

    def non_ok_names(self) -> set[str]:
        """Names of the checks that are non-``ok`` RIGHT NOW.

        The recovery split keys on these: an alert-time condition has cleared
        exactly when its check name is absent here (FAR-1571) — a benign
        advisory still degraded at recovery time is NOT reported as cleared.
        """
        return {name for name, check in self.checks.items() if check.status != "ok"}


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


def _split_cleared_conditions(
    alert_conditions: list[str],
    current_non_ok_names: set[str],
) -> tuple[list[str], list[str]]:
    """Partition the alert-time conditions into ``(cleared, still_failing)``.

    A condition bullet is ``"<name>: <status> (<detail>)"`` — its check NAME
    is everything before the first ``":"``. An alert-time condition has
    CLEARED exactly when its check is no longer non-``ok`` in the current
    observation. This matters under FAR-1571: the alert-time list can contain
    benign advisories (``event_loop_lag``, ``break_glass``, a not-measured
    ``db_hygiene``) that are STILL degraded when the real failure recovers —
    the recovery email must not claim those cleared.
    """
    cleared: list[str] = []
    still_failing: list[str] = []
    for condition in alert_conditions:
        name = condition.split(":", 1)[0].strip()
        if name in current_non_ok_names:
            still_failing.append(condition)
        else:
            cleared.append(condition)
    return cleared, still_failing


def _recovery_summary(
    cleared: list[str],
    still_failing: list[str],
    duration_seconds: float | None,
) -> tuple[str, list[str]]:
    """``(intro sentence, conditions to list)`` — honest about what cleared.

    * Some cleared -> list ONLY those (a still-failing benign advisory is
      deliberately not re-listed: the email must never claim it cleared, and
      listing only true statements keeps that impossible).
    * None cleared -> say so, and list what remains (nothing was confirmed
      cleared, so there is nothing else to report as such).
    * Nothing recorded at alert time -> say that instead of an empty list.
    """
    duration = f" after {duration_seconds:.0f}s" if duration_seconds is not None else ""
    if cleared:
        return f"The following conditions have cleared{duration}:", cleared
    if still_failing:
        return (
            f"The incident recovered{duration}, but no alert-time condition has cleared — still failing:",
            still_failing,
        )
    return f"The incident recovered{duration}; no alert-time condition is still failing.", []


def _recovery_html(
    settings: Settings,
    cleared: list[str],
    still_failing: list[str],
    duration_seconds: float | None,
    resolved_at: str,
) -> str:
    intro, items = _recovery_summary(cleared, still_failing, duration_seconds)
    list_html = "<ul>" + "".join(f"<li>{html.escape(item)}</li>" for item in items) + "</ul>" if items else ""
    return (
        "<html><body>"
        "<h2>Modulo: readiness recovered</h2>"
        f"<p>{html.escape(intro)}</p>"
        f"{list_html}"
        f"<p>Resolved at {html.escape(resolved_at)} on {html.escape(_host())}</p>"
        f"{alert_context_html(settings)}"
        "</body></html>"
    )


def _recovery_text(
    settings: Settings,
    cleared: list[str],
    still_failing: list[str],
    duration_seconds: float | None,
    resolved_at: str,
) -> str:
    intro, items = _recovery_summary(cleared, still_failing, duration_seconds)
    listing = "\n".join(f"- {item}" for item in items)
    if listing:
        listing = f"\n{listing}"
    return (
        "Modulo: readiness recovered\n"
        f"{intro}{listing}\n"
        f"Resolved at {resolved_at} on {_host()}\n" + alert_context_text(settings)
    )


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
    # stdout. The stamp is best-effort (see alert_context.stamp_stdout) — it
    # cannot skip the state commit below.
    stamp_stdout(
        f"[health-alert] ALERT readiness={status}: {'; '.join(conditions)} | {alert_environment_line(settings)}",
        logger=_log,
        log_event="health_alerts.stamp_print_failed",
    )
    return "alert"


async def _notify_recovery(
    settings: Settings,
    sender: EmailSender,
    *,
    configured: bool,
    cleared: list[str],
    still_failing: list[str],
    state: _AlertState,
    now: float,
) -> str:
    """Recovery edge: email that the incident ended, then close it out.

    ``cleared`` / ``still_failing`` are the caller's split of the alert-time
    conditions against the CURRENT observation (FAR-1571): only ``cleared``
    is ever reported as cleared — an alert-time benign advisory that is still
    degraded must not be claimed as resolved.
    """
    if not configured:
        return "disabled"
    recipients = _recipients(settings)
    resolved_at = datetime.now(UTC).isoformat()
    duration_seconds = (now - state.since) if state.since is not None else None
    delivered = await sender(
        settings,
        recipients,
        "[Modulo] Readiness recovered",
        _recovery_html(settings, cleared, still_failing, duration_seconds, resolved_at),
        _recovery_text(settings, cleared, still_failing, duration_seconds, resolved_at),
    )
    if not delivered:
        return "send_failed"
    state.notified = "healthy"
    state.conditions = []
    state.since = None
    # Best-effort stamp (see alert_context.stamp_stdout): the state above is
    # already mutated in memory and committed by the caller after this returns
    # — a print failure must not abort the commit. The stamp mirrors the
    # email's honesty: it names only what actually cleared.
    if cleared:
        stamp_summary = "; ".join(cleared)
    elif still_failing:
        stamp_summary = "no alert-time condition cleared; still failing: " + "; ".join(still_failing)
    else:
        stamp_summary = "no alert-time condition is still failing"
    stamp_stdout(
        f"[health-alert] RECOVERY readiness=ok: {stamp_summary} | {alert_environment_line(settings)}",
        logger=_log,
        log_event="health_alerts.stamp_print_failed",
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
                # Never the raw aggregate: when a real-failure advisory check
                # fired the alert while the aggregate is ``ok``, the subject
                # must read ``degraded``, not ``ok`` (FAR-1571).
                status=observation.reported_status,
                conditions=conditions,
                state=state,
                now=current_time,
            )
        elif confirmed and observed == "healthy" and state.notified == "unhealthy":
            # Split the alert-time conditions against the CURRENT observation:
            # only what actually cleared is reported as cleared (a benign
            # advisory still degraded here must not be claimed resolved).
            cleared, still_failing = _split_cleared_conditions(state.conditions, observation.non_ok_names())
            action = await _notify_recovery(
                settings,
                send_fn,
                configured=configured,
                cleared=cleared,
                still_failing=still_failing,
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
