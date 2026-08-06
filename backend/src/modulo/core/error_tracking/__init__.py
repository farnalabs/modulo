"""Error ingestion service — fingerprinting, batch ingest, HMAC session key store."""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import logging
import os
import re
import secrets
import uuid
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from redis.asyncio import Redis

from redis.asyncio import Redis as AsyncRedis
from sqlalchemy import select
from sqlalchemy.exc import ProgrammingError

from modulo.core.error_tracking.alerting import AlertEngine
from modulo.core.error_tracking.forwarders import get_forwarder
from modulo.core.error_tracking.metrics import init_metrics, record_error_ingest
from modulo.db.crud.error_tracking import (
    create_error_event,
    get_error_group_by_fingerprint,
    upsert_error_group,
)
from modulo.db.models.error_forwarder_config import ErrorForwarderConfig
from modulo.settings import get_settings

_log = logging.getLogger(__name__)

_STACKTRACE_FILE_RE = re.compile(r'File "[^"]+", line \d+,')
_HMAC_KEY_TTL = 3600

# Module-level alert engine (lazy-initialised)
_alert_engine: AlertEngine | None = None
_alert_engine_lock = asyncio.Lock()

# SAQ retry-storm detection (plan F1 probe 6 / F3a): claims beyond the FIRST are
# re-claims; past this threshold a run is in a retry storm worth alerting on.
SAQ_RETRY_STORM_CLAIM_THRESHOLD = 3

# Missed-fire alert (plan F1 probe 6): only triggers with a cadence >= 1h are
# probed (sub-minute/sub-hour cadences are fire-and-forget in fire_due_triggers).
SAQ_MISSED_FIRE_MIN_PERIOD_SECONDS = 3600
SAQ_MISSED_FIRE_GRACE_SECONDS = 300  # grace above period before alerting
_MISSED_FIRE_COOLDOWN_SECONDS = 6 * 3600  # re-alert at most once per 6h window


def _normalize_stacktrace(stacktrace: str) -> str:
    lines = stacktrace.strip().split("\n")[:5]
    return "\n".join(_STACKTRACE_FILE_RE.sub("", line).strip() for line in lines)


class FingerprintError(Exception):
    pass


class ErrorIngestionService:
    """Creates error events, upserts groups, batches, and evaluates alert rules.

    All methods accept a SQLAlchemy ``AsyncSession`` (or any compatible
    async session) and an ``org_id`` (typically ``uuid.UUID``).
    """

    def __init__(self, redis_client: Redis | None = None) -> None:
        self._redis = redis_client
        init_metrics()

    @staticmethod
    def fingerprint(message: str, stacktrace: str | None = None, source: str = "") -> str:
        """SHA-256 of (message + normalised stacktrace top 5 frames + source)."""
        normalised = _normalize_stacktrace(stacktrace or "")
        raw = f"{message}|{normalised}|{source}"
        return hashlib.sha256(raw.encode()).hexdigest()

    async def _ensure_alert_engine(self) -> AlertEngine:
        global _alert_engine
        if _alert_engine is not None:
            return _alert_engine
        async with _alert_engine_lock:
            if _alert_engine is None:
                _alert_engine = AlertEngine(redis_client=self._redis)
        return _alert_engine

    async def ingest(
        self,
        session: Any,
        org_id: Any,
        event_data: dict[str, Any],
    ) -> dict[str, Any]:
        message = event_data.get("message")
        level = event_data.get("level")
        source = event_data.get("source")
        if not message or not level or not source:
            raise ValueError("ingest requires 'message', 'level', and 'source' in event_data")

        fp = self.fingerprint(
            message=message,
            stacktrace=event_data.get("stacktrace"),
            source=source,
        )
        environment = event_data.get("environment")

        event = await create_error_event(
            session=session,
            org_id=org_id,
            fingerprint=fp,
            level=level,
            message=message,
            source=source,
            stacktrace=event_data.get("stacktrace"),
            context_json=event_data.get("context_json"),
            environment=environment,
            version=event_data.get("version"),
        )
        existing = await get_error_group_by_fingerprint(session=session, org_id=org_id, fingerprint=fp)
        group = await upsert_error_group(
            session=session,
            org_id=org_id,
            fingerprint=fp,
            level=level,
            sample_event_id=event.id,
        )

        # Record Prometheus metrics
        record_error_ingest(level, source, environment)

        # Fire-and-forget alert evaluation
        try:
            engine = await self._ensure_alert_engine()
            alerts = await engine.evaluate(
                org_id=org_id,
                session=session,
                error_group_id=group.id,
                fingerprint=fp,
                level=level,
                count=group.count,
                environment=environment,
            )
            if alerts:
                await engine.dispatch_all(
                    org_id=org_id,
                    alerts=alerts,
                    session=session,
                    error_group=group,
                )
        except Exception:
            _log.exception("error_tracking.alert_evaluation_failed")

        try:
            await _dispatch_forwarders(org_id, group, event, event_data, session=session)
        except Exception:
            _log.exception("error_tracking.forwarder_dispatch_failed")

        return {"group_id": str(group.id), "is_new": existing is None}

    async def ingest_batch(
        self,
        session: Any,
        org_id: Any,
        events: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        results: list[dict[str, Any]] = []
        for event_data in events:
            try:
                results.append(await self.ingest(session, org_id, event_data))
            except Exception:
                _log.exception("error_tracking.batch_item_failed", extra={"org_id": str(org_id)})
        return results


# ---------------------------------------------------------------------------
# HMAC session-key store
# ---------------------------------------------------------------------------


class SessionKeyStore:
    """Redis-backed HMAC key store.

    Keys are identified by ``account_id`` (str). Each key has a 1-hour TTL.
    """

    def __init__(self, redis_client: Redis | None = None) -> None:
        self._redis = redis_client
        self._in_memory: dict[str, str] = {}

    async def generate_key(self, account_id: str) -> str:
        key = secrets.token_hex(32)
        if self._redis is not None:
            try:
                await self._redis.setex(f"error_hmac_key:{account_id}", _HMAC_KEY_TTL, key)
            except Exception:
                _log.exception("session_key_store.redis_set_failed", extra={"account_id": account_id})
                raise
        else:
            self._in_memory[account_id] = key
        return key

    async def get_key(self, account_id: str) -> str | None:
        if self._redis is not None:
            try:
                val = await self._redis.get(f"error_hmac_key:{account_id}")
                return val.decode() if isinstance(val, bytes) else val
            except Exception:
                _log.exception("session_key_store.redis_get_failed", extra={"account_id": account_id})
                raise
        return self._in_memory.get(account_id)

    async def verify_hmac(self, account_id: str, body: bytes, signature: str) -> bool:
        key = await self.get_key(account_id)
        if key is None:
            return False
        expected = hmac.new(key.encode(), body, hashlib.sha256).hexdigest()
        return hmac.compare_digest(expected, signature)


# ---------------------------------------------------------------------------
# Forwarder dispatch — called after alert evaluation
# ---------------------------------------------------------------------------

_DEFAULT_FORWARDER_CONFIGS: dict[str, dict[str, Any]] = {}


def configure_forwarders(configs: dict[str, dict[str, Any]]) -> None:
    """Set org-level forwarder configs at startup.

    Expected shape::

        {
            "sentry": {"dsn": "...", "org_slug": "...", "project_slug": "..."},
            "datadog": {"api_key": "...", "site": "datadoghq.com"},
        }
    """
    global _DEFAULT_FORWARDER_CONFIGS
    _DEFAULT_FORWARDER_CONFIGS = configs


async def _dispatch_forwarders(
    org_id: Any,
    error_group: Any,
    error_event: Any,
    event_data: dict[str, Any],
    session: Any | None = None,
) -> None:
    """Call all configured forwarders for the org.

    Forwarder configs are looked up by org_id from the DB (or fall back to
    a global default).  Each forwarder runs independently; a single
    forwarder failure does not affect others.
    """
    per_org_configs: dict[str, dict[str, Any]] = {}
    if session is not None:
        try:
            result = await session.execute(
                select(ErrorForwarderConfig).where(
                    ErrorForwarderConfig.organisation_id == org_id,
                    ErrorForwarderConfig.enabled.is_(True),
                ),
            )
            for row in result.scalars().all():
                if row.config_json:
                    per_org_configs[row.forwarder_type] = row.config_json

        except ProgrammingError:
            _log.exception("core.error_tracking")

            raise

    configs = per_org_configs or _DEFAULT_FORWARDER_CONFIGS
    if not configs:
        return

    for type_name, fwd_config in configs.items():
        forwarder = get_forwarder(type_name)
        if forwarder is None:
            _log.warning("dispatch_forwarders.unknown_type", extra={"type": type_name})
            continue

        try:
            await forwarder.forward(
                org_id=org_id,
                error_group=error_group,
                error_event=error_event,
                config=fwd_config,
            )
        except asyncio.CancelledError:
            raise
        except Exception:
            _log.exception(
                "dispatch_forwarders.failed",
                extra={"type": type_name, "org_id": str(org_id)},
            )


# ---------------------------------------------------------------------------
# SAQ alerting layer (plan F1 probe 6 / F3a)
#
# Two standalone alert emitters, both firing error_events with source='saq':
#
#   * :func:`emit_saq_retry_storm_alert` — claim_count retry-storm detection.
#     Called from the claim path (pipeline_execution.claim_run_async) so a run
#     that is being re-claimed in a loop surfaces an error_event.
#   * :func:`check_missed_fire_alerts` — missed-fire probe for low-cadence
#     triggers (period >= 1h). Runnable from the system cron.
# ---------------------------------------------------------------------------


async def emit_saq_retry_storm_alert(
    aengine: Any,
    org_id: Any,
    run_id: str,
    claim_count: int,
) -> None:
    """Fire an error_event (source='saq') for a SAQ retry storm.

    A run whose ``claim_count`` crossed :data:`SAQ_RETRY_STORM_CLAIM_THRESHOLD`
    is being repeatedly re-claimed (each re-claim rotates the claim token, so
    the original executor is superseded each time). Best-effort: a failure never
    propagates to the claim path.
    """
    if claim_count < SAQ_RETRY_STORM_CLAIM_THRESHOLD:
        return
    message = f"SAQ retry storm: run {run_id} re-claimed {claim_count} times"
    fingerprint = ErrorIngestionService.fingerprint(message=message, source="saq")
    try:
        from sqlalchemy.ext.asyncio import async_sessionmaker

        from modulo.db.rls import set_rls_org

        org_uuid = uuid.UUID(str(org_id))
        factory = async_sessionmaker(aengine, expire_on_commit=False, autobegin=False)
        async with factory() as session, session.begin():
            await set_rls_org(session, org_uuid)
            await create_error_event(
                session,
                org_id=org_uuid,
                fingerprint=fingerprint,
                level="error",
                message=message,
                source="saq",
                context_json={"run_id": str(run_id), "claim_count": claim_count},
                environment=os.environ.get("MODULO_ENV", "development"),
            )
    except asyncio.CancelledError:
        raise
    except Exception:
        _log.exception("error_tracking.saq_retry_storm_alert_failed run=%s", run_id)


def _trigger_period_seconds(
    trigger_type: str,
    cron_expression: str | None,
    cron_timezone: str | None,
    config_json: dict[str, Any] | None,
    now: datetime,
) -> int | None:
    """Best-effort fixed schedule cadence (seconds) for a cron/polling trigger.

    Cron cadence is the gap between two consecutive scheduled fires (the next
    fire after the previous one); polling cadence is ``poll_interval_seconds``.
    An uncomputable cadence returns None (the trigger is skipped by the
    missed-fire probe).
    """
    try:
        if trigger_type == "polling":
            interval = (config_json or {}).get("poll_interval_seconds")
            if not interval:
                return None
            return max(int(interval), 1)
        if trigger_type == "cron" and cron_expression:
            from zoneinfo import ZoneInfo

            from croniter import croniter

            tz = ZoneInfo(cron_timezone or "UTC")
            local_now = now.astimezone(tz)
            iterator = croniter(cron_expression, local_now - timedelta(seconds=1))
            prev = iterator.get_prev(datetime).astimezone(UTC)
            nxt = iterator.get_next(datetime).astimezone(UTC)
            return max(int((nxt - prev).total_seconds()), 1)
        return None
    except Exception:
        return None


# Missed-fire alert cooldown is Redis-backed (SAQ follow-up, retro item 5): the
# pre-cutover in-memory dict reset on every worker restart and duplicated
# alerts across the multiple system-cron workers. Key scheme:
# ``saq:alert:cooldown:missed_fire:{org_id}:{trigger_id}`` with a TTL equal to
# the cooldown window; the atomic ``SET NX EX`` is both the check and the mark,
# so concurrent cron workers can never double-alert. On a Redis failure the
# probe FAILS OPEN (the alert fires) and logs — an alerting cooldown must never
# suppress a real alert because Redis is down.
#
# ``_missed_fire_cooldowns`` is retained ONLY so legacy callers/tests that
# ``clear()`` the old in-memory dict keep working; the operative cooldown lives
# in Redis (see :func:`_missed_fire_cooldown_ok`).
_MISSED_FIRE_COOLDOWN_KEY_PREFIX = "saq:alert:cooldown:missed_fire"
_missed_fire_cooldowns: dict[str, float] = {}


async def _missed_fire_cooldown_ok(redis_client: Any, org_id: str, trigger_id: Any) -> bool:
    """Atomically check-and-mark the missed-fire alert cooldown.

    Returns True when the trigger is NOT within the cooldown window (the alert
    may fire now); False when a recent alert already marked the window. The
    ``SET key 1 NX EX <window>`` round-trip is atomic, so concurrent cron
    workers cannot race past the cooldown. A Redis failure FAILS OPEN (returns
    True so the alert fires) and is logged — the cooldown must never suppress
    a real alert because Redis is unavailable.
    """
    key = f"{_MISSED_FIRE_COOLDOWN_KEY_PREFIX}:{org_id}:{trigger_id}"
    try:
        return bool(await redis_client.set(key, "1", nx=True, ex=_MISSED_FIRE_COOLDOWN_SECONDS))
    except asyncio.CancelledError:
        raise
    except Exception:
        _log.warning("error_tracking.missed_fire_cooldown_redis_failed trigger=%s", trigger_id)
        return True


async def check_missed_fire_alerts(
    aengine: Any,
    *,
    grace_seconds: int = SAQ_MISSED_FIRE_GRACE_SECONDS,
    org_id: uuid.UUID | None = None,
) -> int:
    """Missed-fire probe (plan F1 probe 6) — alert for silent low-cadence triggers.

    For every active cron/polling trigger whose cadence is >= 1h, alert when
    ``last_fired_at`` is NULL or older than ``cadence + grace_seconds``. Emits
    one error_event (source='saq') per affected trigger, throttled by a
    Redis-backed cooldown (``saq:alert:cooldown:missed_fire:*``, one per 6h
    window) so a dead trigger alerts once per window instead of every cron
    tick — across ALL system-cron workers. Runs per-org under RLS; pass
    ``org_id`` to probe a single org (system context) or None to scan all orgs.
    A Redis failure fails open: the alert still fires (never suppressed) but is
    logged.

    Returns the number of alerts emitted.
    """
    from sqlalchemy import select

    from modulo.db.models.organisation import Organisation
    from modulo.db.models.trigger import Trigger

    emitted = 0
    now = datetime.now(UTC)
    redis_client = AsyncRedis.from_url(
        get_settings().redis_url,
        socket_connect_timeout=5,
        socket_keepalive=True,
    )
    try:
        async with aengine.connect() as c:
            if org_id is not None:
                org_ids: list[uuid.UUID] = [org_id]
            else:
                result = await c.execute(select(Organisation.id))
                org_ids = [row[0] for row in result.all()]
        if not org_ids:
            return 0

        from sqlalchemy.ext.asyncio import async_sessionmaker

        from modulo.db.rls import set_rls_org

        factory = async_sessionmaker(aengine, expire_on_commit=False, autobegin=False)
        for oid in org_ids:
            oid_uuid = uuid.UUID(str(oid))
            async with factory() as session, session.begin():
                await set_rls_org(session, oid_uuid)
                result = await session.execute(
                    select(
                        Trigger.id,
                        Trigger.trigger_type,
                        Trigger.cron_expression,
                        Trigger.cron_timezone,
                        Trigger.config_json,
                        Trigger.last_fired_at,
                        Trigger.created_at,
                    ).where(
                        Trigger.organisation_id == oid_uuid,
                        Trigger.active.is_(True),
                        Trigger.deleted_at.is_(None),
                        Trigger.trigger_type.in_(("cron", "polling")),
                    )
                )
                rows = result.all()
            for row in rows:
                period = _trigger_period_seconds(
                    row.trigger_type,
                    row.cron_expression,
                    row.cron_timezone,
                    row.config_json,
                    now,
                )
                if period is None or period < SAQ_MISSED_FIRE_MIN_PERIOD_SECONDS:
                    continue
                if row.last_fired_at is not None and row.last_fired_at >= now - timedelta(
                    seconds=period + grace_seconds
                ):
                    continue
                if row.last_fired_at is None and (
                    row.created_at is None or row.created_at >= now - timedelta(seconds=period + grace_seconds)
                ):
                    # A brand-new trigger that has not yet had its first scheduled
                    # fire is not "missed" — only probe it once it is old enough.
                    continue
                if not await _missed_fire_cooldown_ok(redis_client, str(oid_uuid), row.id):
                    continue
                message = f"Trigger {row.id} ({row.trigger_type}) has not fired for >= {period}s"
                fingerprint = ErrorIngestionService.fingerprint(message=message, source="saq")
                async with factory() as session, session.begin():
                    await set_rls_org(session, oid_uuid)
                    await create_error_event(
                        session,
                        org_id=oid_uuid,
                        fingerprint=fingerprint,
                        level="error",
                        message=message,
                        source="saq",
                        context_json={
                            "trigger_id": str(row.id),
                            "trigger_type": row.trigger_type,
                            "period_seconds": period,
                        },
                        environment=os.environ.get("MODULO_ENV", "development"),
                    )
                emitted += 1
        return emitted
    except asyncio.CancelledError:
        raise
    except Exception:
        _log.exception("error_tracking.missed_fire_check_failed")
        return 0
    finally:
        try:
            await redis_client.aclose()
        except asyncio.CancelledError:
            raise
        except Exception:
            _log.warning("error_tracking.missed_fire_redis_close_failed")
