"""Approaching-deadline HITL review warning (FAR-1270).

An UNCLAIMED, UNDECIDED gate on an ``awaiting_human`` run is terminalised by
the ``dispatcher_reconcile`` FAR-648 terminaliser — ``cancelled`` /
``hitl_review_expired`` — with no warning to a human, so the review window
lapses silently (observed live: a pipeline cancelled ~78 min after its gate
fires because nobody reviewed it). This sweep is the WARNING half: while the
gate still has time left, it emails the same recipients the gate-fire alert
uses (the ``hitl_email_alerts`` substrate — org members holding ``hitl.claim``
whose per-user preference resolves TRUE for the pipeline), so someone can act
BEFORE the terminaliser cancels the run.

Design decisions (FAR-1270):

- **Deadline source — dual-arm, mirroring the terminaliser.** The effective
  deadline is ``hitl_claims.terminalize_at`` when stamped (FAR-1257, PR
  #1072: the absolute fire-time review-window stamp; that branch may not be
  merged into this tree yet, hence the defensive ``getattr``), else the
  legacy arithmetic the current terminaliser collects on:
  ``expires_at + settings.hitl_review_cancel_grace_seconds``. After FAR-1257
  lands, stamped gates switch over automatically — no change here.

- **Lead time — a fraction of the window, floored at the sweep cadence and
  capped at 1 hour:** ``lead = min(max(window / 2, 60s), window, 3600s)``
  where the window is ``deadline - claim.created_at``. For the 60s minimum
  review window the floor makes ``lead == window`` — i.e. the warning fires
  from gate-fire time, the only lead that is still meaningful inside a 60s
  window. The floor is NOT an arbitrary constant: it equals the every-minute
  SAQ cron cadence, so the approaching band (``[deadline - lead,
  deadline)``) is always at least one tick wide and a tick is guaranteed to
  land inside it. The 1h cap keeps long (multi-day) windows from warning
  hours/days early.

- **Mechanism — a sibling SAQ system cron** (``hitl_deadline_warning``,
  every 60s, ``unique=True``) alongside ``hitl_overdue``. The existing
  overdue sweep runs every 5 minutes — far too coarse for a band that can be
  60s wide — so the cadence, not a bigger lead, is what makes the 60s
  window work. Same per-org transaction + advisory-lock pattern as
  ``overdue_warning``; dispatch happens outside the selection transaction.

- **Idempotency — notify at most ONCE per gate, via the fire-once claim
  key pattern already used for exactly-once alert emails**
  (``watchdog.worker_liveness._claim_alert``,
  ``error_tracking._fire_once_allowed``): ``SET NX EX`` on
  ``hitl:deadline_warning:{claim_id}`` in Redis, with an in-process
  monotonic backstop when Redis errors or is unavailable (this sweep is
  periodic, so a bare fail-open would re-email every tick during an
  outage). ``hitl_claims.overdue_notified_at`` is deliberately NOT reused —
  it is the ``hitl_overdue`` path's marker and sharing it would suppress
  that working escalation (and vice versa). No schema change. The ONE claim
  guards BOTH delivery channels (FAR-1295): the claim is taken before the
  email send and before the ``Notifier`` dispatch, so a gate can never
  produce a second email NOR a second webhook / in-app notification on a
  later tick.

- **Channels (FAR-1295).** Two independent legs fire after that single
  claim: the email leg (unchanged — ``hitl_email_alerts`` recipients) and
  the ``Notifier`` leg (webhook endpoints + the in-app notification the
  ``NotificationEventMapper`` creates), dispatched as
  ``EVENT_HITL_DEADLINE_WARNING``. The marker is claimed when AT LEAST ONE
  channel can deliver: email recipients resolved non-empty, OR the org has
  a subscribed webhook endpoint (``Notifier.has_subscribers``). When
  neither channel can deliver the marker is left unset — a later opt-in or
  endpoint subscription can still warn while the band is open. A failure of
  one leg never suppresses the other (each is isolated per entry).

- **Skip conditions:** gates already CLAIMED or DECIDED (SQL predicate),
  runs not ``awaiting_human`` or cancellation-requested (the cancel path
  owns those), deadlines already past (the terminaliser owns those), gates
  not yet inside the lead band, runs with any CLAIMED open sibling gate
  (the terminaliser will not cancel a run that has live human work — a
  warning would be a false alarm), gates with NO delivery channel (email
  recipients empty AND no subscribed webhook endpoint; the once-only marker
  is left unset so a later opt-in or subscription can still warn), and
  gates already claimed by the fire-once key.
"""

from __future__ import annotations

import asyncio
import logging
import time
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from modulo.core.cron_helpers import _bound_org
from modulo.core.hitl_email_alerts import resolve_hitl_email_recipients, send_hitl_deadline_alerts
from modulo.core.notifier import EVENT_HITL_DEADLINE_WARNING
from modulo.db.models.hitl_claim import HitlClaim
from modulo.db.models.organisation import Organisation
from modulo.db.models.pipeline import Pipeline
from modulo.db.models.run import AWAITING_HUMAN_STATUS, Run
from modulo.db.rls import set_rls_execution_context, set_rls_org

_log = logging.getLogger(__name__)

#: The SAQ cron cadence this sweep runs at, in seconds. The lead-time floor
#: is tied to it: the approaching band must be at least one tick wide, so a
#: cadence change MUST change this constant too.
_SWEEP_CADENCE_SECONDS = 60

#: Never warn more than this far before the deadline (long windows warn at
#: the last hour, not days early).
_LEAD_CAP_SECONDS = 3600

#: TTL of the Redis fire-once marker. Generously longer than the largest
#: possible remaining band (the band ends at the deadline, after which the
#: gate is excluded), so a gate is never re-warned — including after a
#: claim-lapse resets ``expires_at`` and re-opens a band on the same row.
_DEADLINE_NOTIFY_TTL_SECONDS = 8 * 24 * 3600

# Advisory lock id for the deadline sweep — distinct from the claim-expiry
# sweep (721_336_517) and the overdue sweep (721_336_518) so the three
# system crons never contend.
_DEADLINE_LOCK_KEY = 721_336_519

#: Redis key prefix for the once-per-gate fire-once marker.
_FIRE_ONCE_KEY_PREFIX = "hitl:deadline_warning"

#: In-process backstop cap: evict markers older than the TTL once the map
#: outgrows it (a worker process runs for weeks; one entry per warned gate
#: must not grow without bound).
_MEMORY_CLAIM_MAX = 4096
_MEMORY_CLAIMS: dict[uuid.UUID, float] = {}


def lead_time_seconds(window_seconds: float) -> float:
    """Lead time before the deadline at which a gate starts being notified.

    ``min(max(window / 2, cadence), window, cap)`` — half the review window,
    floored at the sweep cadence (so the approaching band is always at least
    one tick wide — this is what makes the 60s minimum window work: a 60s
    window yields a 60s lead, i.e. warn from gate-fire time), never more
    than the window itself, never more than the 1h cap.

    Raises ``ValueError`` for a non-positive window (a gate whose stamps are
    inconsistent is skipped by the caller rather than warned about).
    """
    if window_seconds <= 0:
        raise ValueError(f"window_seconds must be positive, got {window_seconds}")
    return min(
        max(window_seconds / 2, float(_SWEEP_CADENCE_SECONDS)),
        window_seconds,
        float(_LEAD_CAP_SECONDS),
    )


def effective_deadline(claim: HitlClaim, grace_seconds: int) -> datetime | None:
    """The absolute instant the terminaliser will collect this gate at.

    Two-armed, mirroring the FAR-648/FAR-1257 terminaliser predicate:

    1. ``hitl_claims.terminalize_at`` (FAR-1257) when stamped — read via
       ``getattr`` because this tree may predate the migration that adds the
       column; once FAR-1257 merges and the model maps it, stamped gates
       switch over with no change here. Stamped rows IGNORE the grace knob
       (the absolute stamp IS the deadline).
    2. Legacy fallback for unstamped rows: ``expires_at + grace_seconds``
       — exactly the arithmetic the current terminaliser collects on.

    Returns ``None`` when neither arm yields a usable instant (the caller
    skips the gate).
    """
    stamped: Any = getattr(claim, "terminalize_at", None)
    if isinstance(stamped, datetime):
        return stamped
    expires_at = claim.expires_at
    if not isinstance(expires_at, datetime):
        return None
    return expires_at + timedelta(seconds=grace_seconds)


def review_window_seconds(claim: HitlClaim, deadline: datetime | None) -> float | None:
    """Total review window for this gate: ``deadline - created_at``.

    Both stamps are on the claim row, so the window needs no settings read
    and works for both deadline arms (stamped: the resolved window verbatim;
    legacy: claim-TTL + grace). ``None`` when either stamp is missing.
    """
    if deadline is None:
        return None
    created_at = claim.created_at
    if not isinstance(created_at, datetime):
        return None
    return (deadline - created_at).total_seconds()


def _within_lead(deadline: datetime | None, window_seconds: float | None, now: datetime) -> bool:
    """Whether *now* falls inside ``[deadline - lead, deadline)``.

    Past the deadline the terminaliser owns the gate (skip — warning about a
    run that is about to be cancelled anyway, or already cancelled, helps
    nobody); not yet inside the band, it is too early to warn.
    """
    if deadline is None or window_seconds is None or window_seconds <= 0:
        return False
    if deadline <= now:
        return False
    return deadline <= now + timedelta(seconds=lead_time_seconds(window_seconds))


async def dispatch_deadline_notifications(
    factory: async_sessionmaker[AsyncSession],
    *,
    grace_seconds: int,
    redis_client: Any | None = None,
    notifier: Any | None = None,
    now: datetime | None = None,
) -> list[dict[str, Any]]:
    """Warn opt-in reviewers about gates approaching their terminalisation deadline.

    Shared by the SAQ ``hitl_deadline_warning`` system cron (the sole
    caller). Each org's selection runs in its own transaction guarded by
    ``pg_try_advisory_xact_lock`` so concurrent ticks on multiple workers
    never double-select; the per-gate ``SET NX EX`` fire-once key is the
    second line of defence and the actual once-only guarantee — ONE claim
    guards BOTH channels (email and ``Notifier`` webhook / in-app), so the
    two can never double-fire for the same gate (FAR-1295). Notification
    dispatch happens OUTSIDE the selection transaction (SMTP and webhook
    I/O must never pin a pooled connection), after recipients are resolved
    in their own short transaction. ``notifier=None`` (init failure in the
    cron) simply disables the webhook / in-app leg — the email leg still
    runs.

    Returns the entries notified on at least one channel (claim_id, run_id,
    review_id, pipeline_name, gate_label, deadline, minutes_remaining).
    """
    if grace_seconds < 0:
        raise ValueError(f"grace_seconds must be non-negative, got {grace_seconds}")

    resolved_now = now if now is not None else datetime.now(UTC)
    all_notified: list[dict[str, Any]] = []
    for org_id in await _fetch_org_ids(factory):
        # FAR-1501: bind THIS org for the whole per-org tick so every ERROR
        # inside it — recipient resolution, email send, webhook dispatch,
        # subscriber check, any selection-txn failure — is attributed by
        # ErrorTrackingLogHandler instead of dropped as no_org_context.
        # ``_bound_org`` resets in finally, so the pre-loop phases (org
        # collection) and the next org's tick never inherit the binding; a
        # ``return`` out of ``_process_org_deadline`` exits this ``async
        # with`` normally.
        async with _bound_org(org_id):
            all_notified.extend(
                await _process_org_deadline(org_id, factory, grace_seconds, redis_client, notifier, resolved_now)
            )
    return all_notified


async def _fetch_org_ids(factory: async_sessionmaker[AsyncSession]) -> list[uuid.UUID]:
    """Return every organisation id; runs in its own short read transaction."""
    async with factory() as session, session.begin():
        result = await session.execute(select(Organisation.id))
        return list(result.scalars())


async def _process_org_deadline(
    org_id: uuid.UUID,
    factory: async_sessionmaker[AsyncSession],
    grace_seconds: int,
    redis_client: Any | None,
    notifier: Any | None,
    now: datetime,
) -> list[dict[str, Any]]:
    """Sweep one org: lock, select approaching gates, then notify outside the txn."""
    async with factory() as session, session.begin():
        await set_rls_org(session, org_id)
        await set_rls_execution_context(session)

        if not await _try_acquire_deadline_lock(session, org_id):
            return []

        entries = await _fetch_approaching_entries(session, org_id, grace_seconds, now)

    if not entries:
        return []

    return await _notify_entries(factory, org_id, entries, redis_client, notifier)


async def _try_acquire_deadline_lock(session: AsyncSession, org_id: uuid.UUID) -> bool:
    """Attempt the per-org advisory lock; return True if the org may proceed.

    Returns False only when the lock is already held elsewhere. On a lock
    query error we log and proceed (mirroring the overdue sweep) so a
    transient advisory-lock failure never silently skips an org's warning.
    """
    try:
        lock_result = await session.execute(
            text("SELECT pg_try_advisory_xact_lock(:key)"),
            {"key": _DEADLINE_LOCK_KEY},
        )
        return bool(lock_result.scalar_one())
    except asyncio.CancelledError:
        raise
    except Exception:
        _log.warning("hitl.deadline_warning.lock_unavailable org=%s", org_id)
        return True


async def _fetch_approaching_entries(
    session: AsyncSession,
    org_id: uuid.UUID,
    grace_seconds: int,
    now: datetime,
) -> list[dict[str, Any]]:
    """Select unclaimed, undecided gates on live ``awaiting_human`` runs inside the lead band.

    The SQL predicate carries only the structural skips (org, undecided,
    unclaimed, run still awaiting the review, cancel-wins intact) — the
    deadline is computed per-row in Python because it is dual-arm (stamped
    ``terminalize_at`` vs legacy ``expires_at + grace``) and the stamp column
    may not exist in this tree yet. The run-level false-alarm guard (any
    CLAIMED open sibling gate spares the run from the terminaliser, so no
    warning should fire) is a second, batched query over the band survivors.
    """
    result = await session.execute(
        select(HitlClaim, Pipeline.name)
        .join(Run, Run.id == HitlClaim.run_id)
        .join(Pipeline, Pipeline.id == HitlClaim.pipeline_id)
        .where(
            HitlClaim.organisation_id == org_id,
            HitlClaim.decision.is_(None),
            HitlClaim.account_id.is_(None),
            Run.organisation_id == org_id,
            Run.status == AWAITING_HUMAN_STATUS,
            Run.cancellation_requested.is_(False),
        )
    )

    entries: list[dict[str, Any]] = []
    for claim, pipeline_name in result.all():
        deadline = effective_deadline(claim, grace_seconds)
        window_seconds = review_window_seconds(claim, deadline)
        # The explicit None checks (redundant with _within_lead's own guards)
        # exist for mypy: they narrow the deadline before it is stamped on
        # the entry and used in the minutes-remaining arithmetic.
        if deadline is None or window_seconds is None:
            continue
        if not _within_lead(deadline, window_seconds, now):
            continue
        gate_label = (claim.gate_config_json or {}).get("label") or claim.review_id
        entries.append(
            {
                "claim_id": claim.id,
                "run_id": claim.run_id,
                "review_id": claim.review_id,
                "pipeline_id": claim.pipeline_id,
                "pipeline_name": pipeline_name,
                "gate_label": gate_label,
                "deadline": deadline,
                # Floor with a 1-minute floor: understating the remaining time
                # is the safe direction for a warning.
                "minutes_remaining": max(1, int((deadline - now).total_seconds() // 60)),
            }
        )

    if not entries:
        return []

    blocked_runs = await _runs_with_claimed_open_gate(session, org_id, [entry["run_id"] for entry in entries])
    if blocked_runs:
        entries = [entry for entry in entries if entry["run_id"] not in blocked_runs]
    return entries


async def _runs_with_claimed_open_gate(
    session: AsyncSession,
    org_id: uuid.UUID,
    run_ids: list[uuid.UUID],
) -> set[uuid.UUID]:
    """Runs among *run_ids* that still have a CLAIMED, undecided gate.

    The terminaliser's NOT-EXISTS arm leaves such runs alone (live human work
    attached), so an approaching-deadline warning for them would be a false
    alarm — the run is not going to be cancelled.
    """
    if not run_ids:
        return set()
    result = await session.execute(
        select(HitlClaim.run_id).where(
            HitlClaim.organisation_id == org_id,
            HitlClaim.run_id.in_(run_ids),
            HitlClaim.decision.is_(None),
            HitlClaim.account_id.is_not(None),
        )
    )
    return {run_id for (run_id,) in result.all()}


async def _notify_entries(
    factory: async_sessionmaker[AsyncSession],
    org_id: uuid.UUID,
    entries: list[dict[str, Any]],
    redis_client: Any | None,
    notifier: Any | None,
) -> list[dict[str, Any]]:
    """Resolve both channels, claim the once-only marker, then fire both legs.

    Order matters: the email recipients AND the webhook-subscription check
    resolve BEFORE the marker is claimed, so an org where NO channel can
    deliver never burns the once-only marker (a later opt-in or endpoint
    subscription can still warn while the band is open). The marker is
    claimed BEFORE either send: this is an at-most-once warning (a send
    failure after the claim is logged and not retried — losing a warning
    degrades to today's silent-cancel behaviour, while a retry loop would
    double-deliver). Because ONE claim gates both legs, the email and the
    webhook / in-app legs cannot double-fire for the same gate (FAR-1295):
    whichever tick wins the ``SET NX EX`` claim fires each leg exactly once,
    and every later tick is blocked. Each leg is isolated — an email failure
    never suppresses the webhook leg, and vice versa.
    """
    webhook_subscribed = await _org_has_webhook_subscriber(notifier, org_id)
    notified: list[dict[str, Any]] = []
    recipient_cache: dict[uuid.UUID, list[str]] = {}
    for entry in entries:
        pipeline_id: uuid.UUID = entry["pipeline_id"]
        recipients = recipient_cache.get(pipeline_id)
        if recipients is None:
            try:
                recipients = await _resolve_recipients(factory, org_id, pipeline_id)
            except asyncio.CancelledError:
                raise
            except Exception:
                # Skip the whole entry: without resolved recipients we cannot
                # know whether the email channel is live, and claiming the
                # marker now could burn it with the email leg never sent.
                # The next tick inside the band retries both channels.
                _log.exception(
                    "hitl.deadline_warning.recipient_resolution_failed",
                    extra={"org_id": str(org_id), "run_id": str(entry["run_id"])},
                )
                continue
            recipient_cache[pipeline_id] = recipients

        if not recipients and not webhook_subscribed:
            _log.info(
                "hitl.deadline_warning.no_recipients",
                extra={"org_id": str(org_id), "run_id": str(entry["run_id"]), "pipeline_id": str(pipeline_id)},
            )
            continue

        if not await _claim_deadline_warning(redis_client, entry["claim_id"]):
            _log.debug(
                "hitl.deadline_warning.already_notified",
                extra={"org_id": str(org_id), "claim_id": str(entry["claim_id"])},
            )
            continue

        email_sent = await _send_deadline_email(recipients, entry, org_id)
        webhook_sent = await _dispatch_deadline_event(notifier, org_id, entry)
        if email_sent or webhook_sent:
            notified.append(entry)
    return notified


async def _send_deadline_email(recipients: list[str], entry: dict[str, Any], org_id: uuid.UUID) -> bool:
    """Email leg: send to the resolved recipients; True when it fired.

    Empty recipients short-circuit without calling the sender (the marker is
    already claimed by then, so the webhook leg decides whether the entry is
    notified).
    """
    if not recipients:
        return False
    try:
        await send_hitl_deadline_alerts(
            recipients,
            entry["run_id"],
            entry["gate_label"],
            entry["pipeline_name"],
            entry["minutes_remaining"],
        )
    except asyncio.CancelledError:
        raise
    except Exception:
        _log.exception(
            "hitl.deadline_warning.send_failed",
            extra={"org_id": str(org_id), "run_id": str(entry["run_id"])},
        )
        return False
    return True


async def _dispatch_deadline_event(notifier: Any | None, org_id: uuid.UUID, entry: dict[str, Any]) -> bool:
    """Webhook / in-app leg: dispatch ``EVENT_HITL_DEADLINE_WARNING``; True when it fired.

    The ``Notifier`` posts to every subscribed endpoint AND creates the
    in-app notification via the ``NotificationEventMapper`` — one dispatch
    covers both surfaces. Failures are logged, never raised into the sweep
    (the email leg has already been given its chance by the shared claim).
    """
    if notifier is None:
        return False
    try:
        await notifier.dispatch_event(
            org_id=org_id,
            event_type=EVENT_HITL_DEADLINE_WARNING,
            payload={
                "run_id": str(entry["run_id"]),
                "review_id": entry["review_id"],
                "pipeline_id": str(entry["pipeline_id"]),
                "pipeline_name": entry["pipeline_name"],
                "gate_label": entry["gate_label"],
                "minutes_remaining": entry["minutes_remaining"],
                "deadline": entry["deadline"].isoformat(),
            },
            run_id=str(entry["run_id"]),
        )
    except asyncio.CancelledError:
        raise
    except Exception:
        _log.exception(
            "hitl.deadline_warning.webhook_dispatch_failed",
            extra={"org_id": str(org_id), "run_id": str(entry["run_id"])},
        )
        return False
    return True


async def _org_has_webhook_subscriber(notifier: Any | None, org_id: uuid.UUID) -> bool:
    """Whether the org has an active endpoint subscribed to the deadline event.

    Resolved ONCE per org before the entry loop (every entry shares the
    event type). ``notifier=None`` (cron init failure) means no webhook leg
    at all; a check failure returns ``False`` so the once-only marker is NOT
    burned on an unverifiable channel — the next tick inside the band
    retries.
    """
    if notifier is None:
        return False
    try:
        return bool(await notifier.has_subscribers(org_id, EVENT_HITL_DEADLINE_WARNING))
    except asyncio.CancelledError:
        raise
    except Exception:
        _log.exception(
            "hitl.deadline_warning.webhook_subscriber_check_failed",
            extra={"org_id": str(org_id)},
        )
        return False


async def _resolve_recipients(
    factory: async_sessionmaker[AsyncSession],
    org_id: uuid.UUID,
    pipeline_id: uuid.UUID,
) -> list[str]:
    """Resolve gate-alert recipients in their own short RLS transaction.

    Same resolver the gate-fire email uses (``hitl.claim`` holders with a
    TRUE per-user ``hitl_email`` preference for this pipeline) — the opt-in
    contract is shared, not re-implemented.
    """
    async with factory() as session, session.begin():
        await set_rls_org(session, org_id)
        return await resolve_hitl_email_recipients(session, org_id, pipeline_id)


async def _claim_deadline_warning(redis_client: Any | None, claim_id: uuid.UUID) -> bool:
    """Atomically claim the once-per-gate warning edge; True means SEND.

    Redis ``SET NX EX`` is both the check and the mark (the fire-once
    pattern used by the worker-liveness alert email and the error-tracking
    alert). When Redis errors — this sweep runs every 60s, so a bare
    fail-open would re-email every recipient for the rest of the band — we
    fall back to an in-process monotonic map: the warning still fires once
    per process, which bounds duplicates to Redis-outage windows instead of
    one email per tick.
    """
    if redis_client is not None:
        key = f"{_FIRE_ONCE_KEY_PREFIX}:{claim_id}"
        try:
            return bool(await redis_client.set(key, "1", nx=True, ex=_DEADLINE_NOTIFY_TTL_SECONDS))
        except asyncio.CancelledError:
            raise
        except Exception:
            _log.warning(
                "hitl.deadline_warning.claim_redis_failed — falling back to in-process marker",
                extra={"claim_id": str(claim_id)},
                exc_info=True,
            )
    return _claim_in_memory(claim_id)


def _claim_in_memory(claim_id: uuid.UUID) -> bool:
    """In-process fire-once backstop (monotonic clock, TTL-bounded, capped)."""
    now = time.monotonic()
    last = _MEMORY_CLAIMS.get(claim_id)
    if last is not None and now - last < _DEADLINE_NOTIFY_TTL_SECONDS:
        return False
    if len(_MEMORY_CLAIMS) >= _MEMORY_CLAIM_MAX:
        cutoff = now - _DEADLINE_NOTIFY_TTL_SECONDS
        for stale_id, stamped in list(_MEMORY_CLAIMS.items()):
            if stamped < cutoff:
                del _MEMORY_CLAIMS[stale_id]
    _MEMORY_CLAIMS[claim_id] = now
    return True
