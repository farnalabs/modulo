"""Scheduled-report registry + fire + delivery (SAQ path).

Report generators are registered via ``register_report_type()``. The per-item
fire job (``fire_report_trigger`` in :mod:`modulo.core.cron_helpers`, enqueued
by ``fire_due_triggers``) generates, formats, and delivers each due report as a
bounded SAQ job. The Celery beat scheduler (``DatabaseReportScheduler`` /
``ReportFireTask``) was removed in PR C of the Celery->SAQ migration.

The admin/search fire logic itself lives here, in ``_fire_scheduled_report`` —
the SINGLE implementation shared by the SAQ fire job
(``cron_helpers.fire_report_trigger``) and every other caller. The
``ScheduledReport`` row lock is held only for the two short critical sections
(the read/snapshot before generation, then the terminal stamp after it); report
generation/delivery runs outside it and is fenced by a Redis lease so two fires
of the same report can never run concurrently (FAR-1822).

Idempotency (FAR-1822 fix): the fire is keyed by an ``epoch`` (the enqueue
tick), stamped onto the row's ``last_sent_at`` on success. Phase 1 SKIPS an
epoch whose delivery was already recorded, so a SAQ retry (or a re-queued dead
job) can never deliver the same epoch twice. A *terminal-stamp* failure after a
successful send is caught and reported as success — the send already happened,
so SAQ must not retry the whole fire. A fire that never reaches phase 3 leaves
``next_send_at`` untouched (NEVER nulled), so the due-scan can re-select it.
"""

from __future__ import annotations

import asyncio
import datetime
import hashlib
import hmac
import json
import logging
import threading
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit, urlunsplit

import httpx
from croniter import croniter
from redis.asyncio import Redis as AsyncRedis
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine

from modulo.db.models.scheduled_report import ScheduledReport
from modulo.db.url_utils import split_engine_sslmode
from modulo.settings import get_settings

_log = logging.getLogger(__name__)

_ENGINE: AsyncEngine | None = None
_ENGINE_LOCK: threading.Lock = threading.Lock()

_TEST_ENGINE: AsyncEngine | None = None

# ---------------------------------------------------------------------------
# Type aliases
# ---------------------------------------------------------------------------

ReportData = dict[str, Any]
"""Returned by a report generator."""

ReportGenerator = Callable[[AsyncSession, uuid.UUID, dict[str, Any]], Awaitable[ReportData]]
"""(session, org_id, config) -> report_data"""

ReportFormatter = Callable[[ReportData], Any]
"""Takes report_data, returns a deliverable payload."""

ReportDeliverer = Callable[[Any, dict[str, Any]], Awaitable[list[dict[str, Any]]]]
"""(payload, recipient_config) -> list[delivery_result]"""

# ---------------------------------------------------------------------------
# Report-type registry
# ---------------------------------------------------------------------------

_generators: dict[str, ReportGenerator] = {}
_formatters: dict[str, ReportFormatter] = {}
_deliverers: dict[str, ReportDeliverer] = {}


def register_report_type(
    report_type: str,
    generator: ReportGenerator,
    formatter: ReportFormatter | None = None,
    deliverer: ReportDeliverer | None = None,
) -> None:
    """Register a report generator (and optional formatter/deliverer) by type."""
    _generators[report_type] = generator
    if formatter is not None:
        _formatters[report_type] = formatter
    if deliverer is not None:
        _deliverers[report_type] = deliverer


def get_generator(report_type: str) -> ReportGenerator | None:
    return _generators.get(report_type)


def get_formatter(report_type: str) -> ReportFormatter | None:
    return _formatters.get(report_type)


def get_deliverer(report_type: str) -> ReportDeliverer | None:
    return _deliverers.get(report_type)


# ---------------------------------------------------------------------------
# Engine singleton
# ---------------------------------------------------------------------------


def _get_engine() -> AsyncEngine:
    if _TEST_ENGINE is not None:
        return _TEST_ENGINE
    global _ENGINE
    if _ENGINE is None:
        with _ENGINE_LOCK:
            if _ENGINE is None:
                settings = get_settings()
                # FAR-1440: guard on the URL's ACTUAL driver scheme, not the
                # independent ``modulo_db`` knob, and translate the preserved
                # ``sslmode`` into asyncpg's ``ssl`` connect arg — the raw URL
                # raises TypeError at first connect once Settings stops
                # stripping sslmode.
                engine_url, ssl_arg = split_engine_sslmode(settings.database_url)
                connect_args: dict[str, Any] = {"timeout": 10}
                if ssl_arg is not None:
                    connect_args["ssl"] = ssl_arg
                    connect_args["statement_cache_size"] = 0
                _ENGINE = create_async_engine(
                    engine_url,
                    pool_pre_ping=True,
                    connect_args=connect_args,
                    # FAR-1524: settings-driven recycle window, strictly below
                    # the Fly HAProxy 30m session timeout (the old hardcoded
                    # 3600 s exceeded it) — same contract as
                    # db.session._build_engine.
                    pool_recycle=settings.db_pool_recycle_seconds,
                    pool_timeout=30,
                )
    return _ENGINE


def _set_test_engine(engine: AsyncEngine | None) -> None:
    """Override the engine for testing. Pass None to reset."""
    global _TEST_ENGINE
    _TEST_ENGINE = engine


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def compute_next_send(cron_expression: str, after: datetime.datetime | None = None) -> datetime.datetime:
    """Compute the next send time for a cron expression.

    If *after* is None, uses the current UTC time.
    """
    base = after or datetime.datetime.now(datetime.UTC)
    cron = croniter(cron_expression, base)
    next_dt = cron.get_next(datetime.datetime)
    if not isinstance(next_dt, datetime.datetime):
        msg = f"croniter returned unexpected type: {type(next_dt)}"
        raise TypeError(msg)
    return next_dt


async def _set_rls_org(session: AsyncSession, org_id: uuid.UUID) -> None:
    from modulo.db.rls import set_rls_execution_context
    from modulo.db.rls import set_rls_org as _set_rls

    await _set_rls(session, org_id)
    # Scheduled-report generation is background machinery (no user principal) —
    # the execution hatch lets it read team-scoped tables org-wide.
    await set_rls_execution_context(session)


# ---------------------------------------------------------------------------
# Fire logic — the SINGLE shared async core
# ---------------------------------------------------------------------------

#: Redis key prefix for the per-report fire lease (FAR-1822). The lease makes
#: "one firing in flight per report" explicit now that generation no longer
#: runs under the ``ScheduledReport`` row lock.
_REPORT_FIRE_LEASE_PREFIX = "saq:report:firing:"

#: Lease lifetime in seconds, a pure backstop. A lease cannot legitimately
#: outlive the SAQ fire job (``FIRE_JOB_TIMEOUT`` is 300s), and CRASH RECOVERY
#: does NOT wait for this TTL: a re-queued attempt carries the same ``epoch``
#: as its dead predecessor and STEALS the lease, then releases it on completion
#: or on the next acquisition. The TTL only cleans up a lease whose holder died
#: without ever being retried.
_REPORT_FIRE_LEASE_TTL_SECONDS = 900

#: Release the lease only when we still own it (compare-and-delete on the whole
#: stored value), so a fire whose lease was stolen/expired cannot delete a newer
#: fire's lease.
_RELEASE_REPORT_FIRE_LEASE_LUA = (
    "if redis.call('get', KEYS[1]) == ARGV[1] then return redis.call('del', KEYS[1]) else return 0 end"
)

#: Steal a lease left by a dead SAME-epoch predecessor (compare-and-set on the
#: whole observed value). The compare-and-set — not a plain ``SET`` after a
#: check-then-read — is what makes the steal atomic: two concurrent re-queues of
#: the same epoch both observe the dead holder's value, but only the first SET
#: wins; the loser's CAS misses and it treats the lease as a genuine live fire.
_STEAL_REPORT_FIRE_LEASE_LUA = (
    "if redis.call('get', KEYS[1]) == ARGV[1] then "
    "redis.call('set', KEYS[1], ARGV[2], 'EX', ARGV[3]) return 1 "
    "else return 0 end"
)


class ReportFireLeaseUnavailableError(RuntimeError):
    """The per-report fire lease could NOT be established (no Redis client, or a
    Redis error).

    Deliberately distinct from a genuine held lease: an unavailable lease is an
    INFRASTRUCTURE failure and must be recorded as such (backoff + consecutive-
    failure counter + alerting), never as the benign ``already_firing`` skip —
    otherwise the epoch's delivery is silently dropped (FAR-1822 fix, finding 3).
    """


def _lease_value(token: str, epoch: str | None) -> str:
    """Serialize a lease as ``{"token", "epoch"}`` — the epoch lets a re-queued
    attempt recognise (and steal) a lease its own dead predecessor left."""
    return json.dumps({"token": token, "epoch": epoch}, separators=(",", ":"))


def _lease_epoch(raw: object) -> str | None:
    """Extract the epoch carried by a stored lease value, or ``None``."""
    if isinstance(raw, bytes):
        try:
            raw = raw.decode()
        except UnicodeDecodeError:
            return None
    if not isinstance(raw, str):
        return None
    try:
        data = json.loads(raw)
    except (ValueError, TypeError):
        return None
    if not isinstance(data, dict):
        return None
    epoch = data.get("epoch")
    return epoch if isinstance(epoch, str) else None


async def _acquire_report_fire_lease(
    redis_client: AsyncRedis | None,
    report_id: uuid.UUID,
    epoch: str | None = None,
) -> str | None:
    """Claim the exclusive per-report fire lease for *epoch*.

    Returns the lease value when acquired, or ``None`` when a genuine, LIVE fire
    already holds it. Raises :class:`ReportFireLeaseUnavailableError` when the lease
    could not be established at all (no Redis client, or a Redis error) — the
    caller records a FAILURE rather than a benign skip so an infrastructure
    outage never looks like a successful no-op (finding 3).

    A re-queued attempt (SAQ re-runs a dead job with the SAME ``epoch``) may
    STEAL a lease its own predecessor left behind: the stored epoch identifies
    the dead attempt, so a same-epoch claim is safe and recovery is immediate
    rather than waiting out ``_REPORT_FIRE_LEASE_TTL_SECONDS`` (finding 4). A
    DIFFERENT epoch never steals — that is a genuine concurrent fire.

    The steal is a Lua compare-and-set on the exact value the ``GET`` observed,
    never a bare ``SET`` after a check: two concurrent re-queues of the same
    epoch both see the dead holder's value, but only the first ``SET`` wins —
    the loser's CAS misses and it reports a genuine live fire. Without the CAS
    both would steal and both would generate (the concurrency invariant would
    have a hole exactly on the steal path).
    """
    if redis_client is None:
        raise ReportFireLeaseUnavailableError(f"no redis client for report {report_id}")
    key = f"{_REPORT_FIRE_LEASE_PREFIX}{report_id}"
    value = _lease_value(uuid.uuid4().hex, epoch)
    try:
        # SET NX stores *value* only when the key is absent — the atomic claim.
        if await redis_client.set(key, value, nx=True, ex=_REPORT_FIRE_LEASE_TTL_SECONDS):
            return value
        if epoch is None:
            return None
        current = await redis_client.get(key)
        if current is not None and _lease_epoch(current) == epoch:
            # Same epoch, held by the dead predecessor SAQ re-queued -> steal it.
            # Compare-and-set on the EXACT value we just read (not merely the
            # epoch): a concurrent same-epoch re-queue reads the same dead
            # holder's value, so only the first SET wins and the loser falls
            # through to ``None`` (a genuine live fire).
            stolen = await redis_client.eval(
                _STEAL_REPORT_FIRE_LEASE_LUA,
                1,
                key,
                current,
                value,
                _REPORT_FIRE_LEASE_TTL_SECONDS,
            )
            return value if stolen else None
        return None
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        _log.exception("report fire lease acquire failed for %s", report_id)
        raise ReportFireLeaseUnavailableError(str(exc)) from exc


async def _release_report_fire_lease(
    redis_client: AsyncRedis | None,
    report_id: uuid.UUID,
    lease_value: str | None,
) -> None:
    """Best-effort release of the fire lease (compare-and-delete on the value)."""
    if redis_client is None or lease_value is None:
        return
    key = f"{_REPORT_FIRE_LEASE_PREFIX}{report_id}"
    try:
        await redis_client.eval(_RELEASE_REPORT_FIRE_LEASE_LUA, 1, key, lease_value)
    except asyncio.CancelledError:
        raise
    except Exception:  # pragma: no cover - a stuck lease self-heals via its TTL
        _log.warning("report fire lease release failed for %s", report_id, exc_info=True)


def _parse_epoch(epoch: str | None) -> datetime.datetime | None:
    """Parse the enqueue-epoch ISO timestamp, or ``None`` when absent/unusable."""
    if epoch is None:
        return None
    try:
        parsed = datetime.datetime.fromisoformat(epoch)
    except (TypeError, ValueError):
        _log.warning("report fire: unparseable epoch %r; proceeding without the epoch guard", epoch)
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=datetime.UTC)
    return parsed


async def _fire_scheduled_report(
    *,
    report_id: uuid.UUID,
    org_id: uuid.UUID,
    epoch: str | None = None,
    session_factory: async_sessionmaker[AsyncSession] | None = None,
    redis_client: AsyncRedis | None = None,
    rls_setter: Callable[[AsyncSession, uuid.UUID], Awaitable[None]] | None = None,
) -> dict[str, Any]:
    """Generate, format and deliver ONE scheduled report — the single fire path.

    FAR-1822: the ``ScheduledReport`` row lock (``SELECT … FOR UPDATE``) is held
    ONLY for two short critical sections — the read/snapshot before generation
    and the terminal stamp after it — and is RELEASED before the (potentially
    heavy) generator/formatter/deliverer run. Concurrency is fenced by a Redis
    lease so two fires of the same report can never run at once.

    Phase 1 (short lock): read the active report, snapshot the fire inputs, and
    SKIP the epoch if its delivery was already recorded (idempotency guard).
    Phase 2 (no lock): generate/format/deliver. Phase 3 (guarded write): stamp
    ``last_sent_at`` and, for a one-time report, the terminal ``active=False`` /
    ``next_send_at=None`` — never restating state the phase did not observe.

    ``epoch`` is the enqueue tick (the SAQ job passes it through); SAQ re-runs a
    dead job with the SAME epoch, so a retry of an already-delivered epoch is a
    skip, while a genuinely new enqueue (a later epoch) proceeds.

    A failure of the terminal stamp AFTER a successful send is caught and the
    fire reports success — the send happened, so SAQ must not retry the whole
    fire and deliver AGAIN. This send-then-stamp ordering is at-least-once: if
    the stamp is lost, ``last_sent_at`` is never recorded, so the *next*
    due-scan (``next_send_at IS NOT NULL``) re-selects the report — a one-time
    report keeps ``active=True`` and its due ``next_send_at`` — and re-delivers
    it under a NEW epoch the idempotency guard cannot see. A pre-send durable
    record would be the only exact fix; the catch-as-success protects against
    SAQ retrying the SAME epoch, not against the due-scan's next one.

    Callers own the failure policy: on a ``failed`` result the SAQ fire job backs
    ``next_send_at`` off so ``fire_due_triggers`` cannot re-enqueue every 30s.
    """
    rls = rls_setter or _set_rls_org
    factory = session_factory or async_sessionmaker(_get_engine(), expire_on_commit=False, autobegin=False)
    epoch_dt = _parse_epoch(epoch)

    try:
        lease_value = await _acquire_report_fire_lease(redis_client, report_id, epoch)
    except ReportFireLeaseUnavailableError:
        # Infrastructure failure — surfaced as `failed` so the SAQ wrapper backs
        # off and counts it, NEVER swallowed as a benign `already_firing` skip.
        _log.error("Report %s fire lease unavailable; recording a fire failure", report_id)
        return {"status": "failed", "reason": "lease_unavailable"}
    if lease_value is None:
        return {"status": "skipped", "reason": "already_firing"}
    try:
        snapshot, early_result = await _snapshot_fire_state(
            factory,
            rls,
            report_id=report_id,
            org_id=org_id,
            epoch_dt=epoch_dt,
            epoch=epoch,
        )
        if early_result is not None:
            return early_result
        assert snapshot is not None
        return await _generate_deliver_and_stamp(
            factory,
            rls,
            report_id=report_id,
            org_id=org_id,
            snapshot=snapshot,
            epoch_dt=epoch_dt,
        )
    finally:
        await _release_report_fire_lease(redis_client, report_id, lease_value)


@dataclass(frozen=True)
class _FireSnapshot:
    """Plain values captured under the phase-1 row lock."""

    report_type: str
    config: dict[str, Any]
    recipient_config: dict[str, Any]
    schedule_type: Any
    next_send: datetime.datetime | None
    now: datetime.datetime


async def _snapshot_fire_state(
    factory: async_sessionmaker[AsyncSession],
    rls: Callable[[AsyncSession, uuid.UUID], Awaitable[None]],
    *,
    report_id: uuid.UUID,
    org_id: uuid.UUID,
    epoch_dt: datetime.datetime | None,
    epoch: str | None,
) -> tuple[_FireSnapshot | None, dict[str, Any] | None]:
    """Phase 1: read + snapshot the fire inputs under a SHORT row lock."""
    async with factory() as session, session.begin():
        await rls(session, org_id)

        now = datetime.datetime.now(datetime.UTC)
        report = (
            await session.execute(
                select(ScheduledReport)
                .where(
                    ScheduledReport.id == report_id,
                    ScheduledReport.organisation_id == org_id,
                    ScheduledReport.active.is_(True),
                )
                .with_for_update()
            )
        ).scalar_one_or_none()
        if report is None or not report.active:
            return None, {"status": "skipped", "reason": "report_inactive_or_missing"}

        if _already_delivered(report, epoch_dt, report_id=report_id, epoch=epoch):
            return None, {"status": "skipped", "reason": "already_delivered"}

        # Snapshot the plain values generation needs BEFORE the transaction
        # commits (the ORM instance is detached afterwards).
        config = report.config_json or {}
        recipient_config = report.recipient_config or {}
        schedule_type = config.get("schedule_type")
        try:
            next_send = _compute_next_send(schedule_type, report.cron_expression, now)
        except (ValueError, TypeError, KeyError) as exc:
            _log.exception(
                "Invalid cron expression '%s' for report %s: %s",
                report.cron_expression,
                report_id,
                exc,
                exc_info=True,
            )
            await session.execute(update(ScheduledReport).where(ScheduledReport.id == report_id).values(active=False))
            return None, {"status": "failed", "reason": f"invalid_cron: {exc}"}

        # FAR-1822 (finding 2): do NOT stamp ``next_send_at`` here. The
        # enqueue already advanced it atomically, and a never-completed
        # one-time fire MUST keep its non-NULL value so the due-scan
        # (``next_send_at IS NOT NULL``) can re-select it — nulling it here
        # strands the report forever if the process dies before phase 3.
        return (
            _FireSnapshot(
                report_type=report.report_type,
                config=config,
                recipient_config=recipient_config,
                schedule_type=schedule_type,
                next_send=next_send,
                now=now,
            ),
            None,
        )


def _already_delivered(
    report: Any,
    epoch_dt: datetime.datetime | None,
    *,
    report_id: uuid.UUID,
    epoch: str | None,
) -> bool:
    """Idempotency guard: has this epoch's delivery already been recorded?"""
    # ``last_sent_at`` carries the epoch of the last DELIVERED fire, so a
    # retry/re-queue of an already-delivered epoch is a skip. A cancelled/failed
    # attempt never reaches phase 3, so its epoch is NOT recorded as delivered
    # and the retry still makes progress (at-least-once for an unconfirmed send).
    if epoch_dt is None:
        return False
    last_sent_at = getattr(report, "last_sent_at", None)
    if last_sent_at is not None and last_sent_at >= epoch_dt:
        _log.info("Report %s already delivered for epoch %s; skipping", report_id, epoch)
        return True
    return False


def _compute_next_send(
    schedule_type: Any,
    cron_expression: str,
    now: datetime.datetime,
) -> datetime.datetime | None:
    """Next send time: None for one-time reports, computed otherwise."""
    if schedule_type == "one_time":
        return None
    return compute_next_send(cron_expression, after=now)


async def _generate_deliver_and_stamp(
    factory: async_sessionmaker[AsyncSession],
    rls: Callable[[AsyncSession, uuid.UUID], Awaitable[None]],
    *,
    report_id: uuid.UUID,
    org_id: uuid.UUID,
    snapshot: _FireSnapshot,
    epoch_dt: datetime.datetime | None,
) -> dict[str, Any]:
    """Phases 2 and 3: generate + deliver outside the lock, then stamp."""
    delivery_results, failure = await _generate_and_deliver(
        factory,
        rls,
        report_id=report_id,
        org_id=org_id,
        snapshot=snapshot,
    )
    if failure is not None:
        return failure

    delivered_at = epoch_dt if epoch_dt is not None else snapshot.now
    await _stamp_terminal(
        factory,
        rls,
        report_id=report_id,
        org_id=org_id,
        schedule_type=snapshot.schedule_type,
        delivered_at=delivered_at,
    )

    _log.info(
        "Report %s (%s) sent. Next send: %s",
        report_id,
        snapshot.report_type,
        snapshot.next_send.isoformat() if snapshot.next_send is not None else "none (one-time report completed)",
    )

    return {
        "status": "sent",
        "report_id": str(report_id),
        "report_type": snapshot.report_type,
        "next_send_at": snapshot.next_send.isoformat() if snapshot.next_send is not None else None,
        "delivery_results": delivery_results,
    }


async def _generate_and_deliver(
    factory: async_sessionmaker[AsyncSession],
    rls: Callable[[AsyncSession, uuid.UUID], Awaitable[None]],
    *,
    report_id: uuid.UUID,
    org_id: uuid.UUID,
    snapshot: _FireSnapshot,
) -> tuple[list[dict[str, Any]] | None, dict[str, Any] | None]:
    """Phase 2: run the generator/formatter/deliverer without the row lock."""
    generator = get_generator(snapshot.report_type)
    if generator is None:
        _log.warning("No generator registered for report type %s", snapshot.report_type)
        return None, {"status": "failed", "reason": f"no_generator_for_{snapshot.report_type}"}

    try:
        async with factory() as gen_session, gen_session.begin():
            # The generator runs in its OWN transaction (the row lock is gone)
            # — re-apply the org RLS context so it can read the team-scoped
            # tables org-wide exactly as before.
            await rls(gen_session, org_id)
            report_data = await generator(gen_session, org_id, snapshot.config)

        formatter = get_formatter(snapshot.report_type)
        payload: Any = formatter(report_data) if formatter is not None else report_data

        deliverer = get_deliverer(snapshot.report_type)
        if deliverer is not None:
            return await deliverer(payload, snapshot.recipient_config), None
        return await _deliver_via_config(payload, snapshot.recipient_config), None
    except asyncio.CancelledError:
        raise
    except Exception:
        _log.exception(
            "Report %s (%s) generation or delivery failed",
            report_id,
            snapshot.report_type,
        )
        return None, {"status": "failed", "reason": "generation_or_delivery_failed"}


async def _stamp_terminal(
    factory: async_sessionmaker[AsyncSession],
    rls: Callable[[AsyncSession, uuid.UUID], Awaitable[None]],
    *,
    report_id: uuid.UUID,
    org_id: uuid.UUID,
    schedule_type: Any,
    delivered_at: datetime.datetime,
) -> None:
    """Phase 3: guarded terminal stamp (best-effort after a successful send).

    FAR-1822 (finding 5): write ONLY what this phase observed. A recurring
    report's next_send_at was advanced by the enqueue and a concurrent
    advance/deactivation must not be rewound, so we never restate
    next_send_at/active for it. A one-time report is terminally retired here,
    on SUCCESS only, guarded on active so we never resurrect a row a concurrent
    operator deactivated.
    """
    terminal = update(ScheduledReport).where(ScheduledReport.id == report_id)
    if schedule_type == "one_time":
        terminal = terminal.where(ScheduledReport.active.is_(True)).values(
            last_sent_at=delivered_at,
            next_send_at=None,
            active=False,
        )
    else:
        terminal = terminal.values(last_sent_at=delivered_at)
    try:
        async with factory() as session, session.begin():
            await rls(session, org_id)
            await session.execute(terminal)
    except asyncio.CancelledError:
        raise
    except Exception:
        # The send already happened. Retrying the whole fire would deliver
        # AGAIN — so log the terminal-stamp failure and report success.
        _log.exception(
            "Report %s sent but its terminal stamp failed; not retrying the fire",
            report_id,
        )


# ---------------------------------------------------------------------------
# Registry + delivery (shared — imported by cron_helpers fire jobs)
# ---------------------------------------------------------------------------


async def _deliver_via_config(
    payload: Any,
    recipient_config: dict[str, Any],
) -> list[dict[str, Any]]:
    """Deliver a report payload based on recipient_config type."""
    config_type = recipient_config.get("type", "webhook")

    if config_type == "slack_webhook":
        urls = recipient_config.get("webhook_urls", [])
        return await _deliver_slack_webhook(
            payload,
            urls,
            signing_secret=recipient_config.get("signing_secret"),
            request_timeout=recipient_config.get("timeout"),
        )

    _log.warning("Unknown recipient config type '%s', falling back to generic webhook", config_type)
    return await _deliver_webhook(payload, recipient_config)


_REPORT_HTTP_TIMEOUT = 30.0
_REPORT_MAX_RETRIES = 3
_REPORT_BACKOFF_BASE = 2.0
_REPORT_MAX_BACKOFF = 30.0

_SIGNATURE_HEADER = "X-Modulo-Signature"


def _webhook_url_error(url: object) -> str | None:
    """Pre-flight validate a webhook recipient URL.

    Returns a short machine-readable reason string when *url* can never be
    delivered (permanent config error, not a transient failure), or ``None``
    when it is safe to attempt delivery.

    Validation contract — the URL must:

    * be a non-empty string (whitespace-only rejected)
    * carry no whitespace anywhere in the value
    * parse via ``urlsplit`` (broken IPv6 like ``http://[::1`` rejected)
    * use the ``http`` or ``https`` scheme (``ftp://``, ``file://``, etc. rejected)
    * carry a non-empty hostname (bare ``https://`` rejected)
    * carry a valid port when one is present (``https://host:abc`` and
      ``https://host:99999`` are malformed — ``urlsplit`` accepts them but
      httpx raises ``InvalidURL`` on ``post``, which would otherwise fall into
      the retry/backoff loop)
    * not embed userinfo credentials (``https://user:pass@host`` — httpx would
      send them as a Basic-Auth header and they would leak into audit/delivery
      logs)

    Reasons are stable tokens (``url_not_a_string``, ``url_empty``,
    ``url_contains_whitespace``, ``url_malformed``, ``url_scheme_not_http``,
    ``url_missing_host``, ``url_contains_credentials``) so callers and tests can
    assert on them.
    """
    if not isinstance(url, str):
        return "url_not_a_string"
    candidate = url.strip()
    if not candidate:
        return "url_empty"
    if any(ch.isspace() for ch in candidate):
        return "url_contains_whitespace"
    try:
        parts = urlsplit(candidate)
    except ValueError:
        return "url_malformed"
    if parts.scheme not in ("http", "https"):
        return "url_scheme_not_http"
    if not parts.hostname:
        return "url_missing_host"
    if parts.username is not None or parts.password is not None:
        return "url_contains_credentials"
    try:
        _ = parts.port
    except ValueError:
        return "url_malformed"
    return None


def _redact_url_credentials(url: str) -> str:
    """Strip embedded ``user:pass@`` credentials from *url* for storage/logging.

    ``https://user:pass@host/x`` becomes ``https://host/x`` so the credentialed
    URL rejected by ``_webhook_url_error`` does not leak its credentials into
    delivery logs, per-URL results, or the SAQ job result. URLs without
    userinfo are returned unchanged.
    """
    try:
        parts = urlsplit(url)
    except ValueError:
        return url
    if parts.username is None and parts.password is None:
        return url
    host = parts.hostname or ""
    try:
        port = parts.port
    except ValueError:
        port = None
    if port is not None:
        host = f"{host}:{port}"
    return urlunsplit((parts.scheme, host, parts.path, parts.query, parts.fragment))


def _serialize_json_body(body: dict[str, Any] | list[Any]) -> bytes:
    """Serialize a payload to the exact bytes that will be sent on the wire.

    Compact separators are used so the signed bytes match what the recipient
    receives byte-for-byte.
    """
    return json.dumps(body, separators=(",", ":"), sort_keys=True).encode("utf-8")


def _sign_payload(secret: str, body_bytes: bytes) -> str:
    """Compute the HMAC-SHA256 signature of a serialized payload.

    Returns ``sha256=<hex digest>`` so the recipient can verify authenticity
    against a shared secret. This is a plain HMAC-SHA256 over the raw body
    bytes (not Slack's ``v0:timestamp:body`` signing scheme).
    """
    digest = hmac.new(str(secret).encode("utf-8"), body_bytes, hashlib.sha256).hexdigest()
    return f"sha256={digest}"


def _coerce_timeout(value: object) -> float | None:
    """Validate a caller-supplied timeout, falling back to ``None`` (default)."""
    if isinstance(value, bool):
        return None
    try:
        parsed = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    if parsed <= 0:
        return None
    return parsed


def _mark_failed(result: dict[str, Any], *, status_code: int | None = None, error: str | None = None) -> None:
    """Record a failed per-URL delivery outcome on ``result`` (display value only)."""
    result.update({"status": "failed", "status_code": status_code, "error": error})


def _signed_body_and_headers(
    body: dict[str, Any] | list[Any],
    headers: dict[str, str] | None,
    signing_secret: str | None,
) -> tuple[bytes | None, dict[str, str]]:
    """Serialize *body* once and attach the HMAC signature header when signed.

    Returns the raw body bytes (``None`` when unsigned — the caller then sends
    ``json=body`` instead of ``content=body_bytes``) and the caller's headers
    merged with the signature plus a JSON content type. The signature is
    computed over the exact bytes that go on the wire so the recipient can
    verify byte-for-byte authenticity.
    """
    if not signing_secret:
        return None, headers or {}
    body_bytes = _serialize_json_body(body)
    merged_headers = {**(headers or {}), _SIGNATURE_HEADER: _sign_payload(signing_secret, body_bytes)}
    merged_headers.setdefault("Content-Type", "application/json")
    return body_bytes, merged_headers


def _backoff_seconds(attempt: int) -> float:
    """Capped exponential backoff for *attempt* (0-based)."""
    return min(_REPORT_BACKOFF_BASE**attempt, _REPORT_MAX_BACKOFF)


def _mark_retry_exhausted(result: dict[str, Any], last_resp_or_exc: httpx.Response | Exception | None) -> None:
    """Record the outcome of a URL whose retry budget ran out without a break.

    Prefers the last exception's message, then the last response's status and
    (truncated) body text, and finally the ``max_retries_exceeded`` sentinel
    when no attempt ever produced a response or an exception.
    """
    if isinstance(last_resp_or_exc, Exception):
        _mark_failed(result, error=str(last_resp_or_exc))
    elif last_resp_or_exc is not None:
        err_text = getattr(last_resp_or_exc, "text", None) or "max_retries_exceeded"
        _mark_failed(result, status_code=last_resp_or_exc.status_code, error=err_text[:200])
    else:
        _mark_failed(result, error="max_retries_exceeded")


def _handle_delivery_response(
    resp: httpx.Response,
    result: dict[str, Any],
    url: str,
    attempt: int,
) -> bool:
    """Classify *resp* for the retry loop, returning True when the loop must stop.

    A success or a non-transient response (neither 429 nor 5xx) is terminal: the
    outcome is recorded on ``result`` and the caller breaks out of the loop. A
    429 or 5xx is transient — the warning is logged and the caller retries.
    """
    if resp.is_success:
        result.update({"status": "delivered", "status_code": resp.status_code, "error": None})
        return True
    if resp.status_code == 429:
        _log.warning(
            "Delivery to %s rate-limited (429), retrying after %.0fs (attempt %d/%d)",
            url,
            _parse_retry_after(resp),
            attempt + 1,
            _REPORT_MAX_RETRIES,
        )
        return False
    if resp.status_code >= 500:
        _log.warning(
            "Delivery to %s returned %d, retrying (attempt %d/%d)",
            url,
            resp.status_code,
            attempt + 1,
            _REPORT_MAX_RETRIES,
        )
        return False
    _mark_failed(result, status_code=resp.status_code, error=resp.text[:200])
    return True


def _retry_delay(resp: httpx.Response, attempt: int) -> float:
    """Delay before the next attempt: the 429 ``Retry-After``, else capped backoff."""
    if resp.status_code == 429:
        return _parse_retry_after(resp)
    return _backoff_seconds(attempt)


async def _deliver_one_url(
    client: httpx.AsyncClient,
    url: str,
    body: dict[str, Any] | list[Any],
    *,
    body_bytes: bytes | None = None,
    headers: dict[str, str],
) -> dict[str, Any]:
    """POST to one URL with the retry/backoff loop, returning its result entry.

    Retries transient failures (5xx, connection errors, 429) up to
    ``_REPORT_MAX_RETRIES`` times; a non-transient 4xx breaks out immediately.
    ``body_bytes`` carries the pre-serialized, pre-signed payload when a
    signing secret is in use; otherwise ``json=body`` is sent. ``headers``
    already carries any signature header computed by the caller.
    """
    result: dict[str, Any] = {
        "url": _redact_url_credentials(url),
        "status": "failed",
        "status_code": None,
        "error": None,
    }
    last_resp_or_exc: httpx.Response | Exception | None = None
    for attempt in range(_REPORT_MAX_RETRIES):
        try:
            if body_bytes is not None:
                resp = await client.post(url, content=body_bytes, headers=headers)
            else:
                resp = await client.post(url, json=body, headers=headers)
            last_resp_or_exc = resp
            if _handle_delivery_response(resp, result, url, attempt):
                break
        except (httpx.RequestError, TypeError) as exc:
            _log.warning(
                "Delivery to %s failed: %s (attempt %d/%d)",
                url,
                exc,
                attempt + 1,
                _REPORT_MAX_RETRIES,
                exc_info=True,
            )
            last_resp_or_exc = exc
            # A request error sleeps only while further attempts remain — the
            # final failure is reported immediately, with no trailing delay.
            if attempt < _REPORT_MAX_RETRIES - 1:
                await asyncio.sleep(_backoff_seconds(attempt))
            continue
        # Transient response (429 / 5xx): sleep before the next attempt. The
        # 429 honours Retry-After; both paths still sleep on the FINAL attempt
        # so the observed request cadence is unchanged (the loop then ends).
        await asyncio.sleep(_retry_delay(resp, attempt))
    else:
        _mark_retry_exhausted(result, last_resp_or_exc)
    return result


async def _deliver_to_urls(
    urls: list[str],
    body: dict[str, Any] | list[Any],
    headers: dict[str, str] | None = None,
    *,
    request_timeout: float | None = None,
    signing_secret: str | None = None,
) -> list[dict[str, Any]]:
    """POST JSON body to each URL, returning per-URL results.

    Retries transient failures (5xx, connection errors, 429) per URL
    with exponential backoff. Non-transient 4xx errors are not retried.

    When ``signing_secret`` is provided, the body is serialized once, signed
    with HMAC-SHA256, and sent byte-for-byte with an ``X-Modulo-Signature``
    header so the recipient can verify authenticity. ``request_timeout``
    overrides the per-request timeout (default ``_REPORT_HTTP_TIMEOUT``).
    """
    body_bytes, merged_headers = _signed_body_and_headers(body, headers, signing_secret)
    effective_timeout = _coerce_timeout(request_timeout) if request_timeout is not None else None
    request_timeout_seconds = effective_timeout if effective_timeout is not None else _REPORT_HTTP_TIMEOUT

    results: list[dict[str, Any]] = []
    async with httpx.AsyncClient(timeout=request_timeout_seconds) as client:
        for url in urls:
            invalid_reason = _webhook_url_error(url)
            if invalid_reason is not None:
                # Permanent config error — skip the retry/backoff loop entirely;
                # a malformed URL cannot become deliverable on a later attempt.
                display_url = _redact_url_credentials(url)
                _log.warning(
                    "Delivery to %s skipped: invalid webhook URL (%s)",
                    display_url,
                    invalid_reason,
                )
                result: dict[str, Any] = {
                    "url": display_url,
                    "status": "failed",
                    "status_code": None,
                    "error": f"invalid_webhook_url: {invalid_reason}",
                }
                results.append(result)
                continue
            results.append(await _deliver_one_url(client, url, body, body_bytes=body_bytes, headers=merged_headers))
    return results


def _parse_retry_after(resp: httpx.Response) -> float:
    """Extract Retry-After header value, defaulting to 5 seconds."""
    raw = resp.headers.get("Retry-After", "5")
    try:
        return float(raw)
    except (ValueError, TypeError):
        return 5.0


async def _deliver_slack_webhook(
    payload: Any,
    webhook_urls: list[str],
    *,
    signing_secret: str | None = None,
    request_timeout: float | None = None,
) -> list[dict[str, Any]]:
    body = payload if isinstance(payload, (dict, list)) else {"text": str(payload)}
    return await _deliver_to_urls(webhook_urls, body, signing_secret=signing_secret, request_timeout=request_timeout)


async def _deliver_webhook(
    payload: Any,
    recipient_config: dict[str, Any],
) -> list[dict[str, Any]]:
    urls = recipient_config.get("urls", [])
    headers = recipient_config.get("headers", {})
    signing_secret = recipient_config.get("signing_secret")
    request_timeout = recipient_config.get("timeout")
    body = payload if isinstance(payload, (dict, list)) else {"data": str(payload)}
    return await _deliver_to_urls(
        urls,
        body,
        headers=headers,
        signing_secret=signing_secret,
        request_timeout=request_timeout,
    )
