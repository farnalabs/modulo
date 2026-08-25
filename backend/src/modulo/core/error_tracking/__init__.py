"""Error ingestion service — fingerprinting, batch ingest, HMAC session key store.

Also owns the FAR-151 per-signal ingestion writers, seeded default alert rules
(§15.6), the fire-once retry-alert guard and ``alert_resolved`` lifecycle events
(§15.5), all feeding the single AlertEngine evaluator (§15.8).
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import logging
import os
import re
import secrets
import time
import uuid
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from redis.asyncio import Redis

from redis.asyncio import Redis as AsyncRedis
from sqlalchemy import select
from sqlalchemy.exc import ProgrammingError
from sqlalchemy.ext.asyncio import AsyncSession

from modulo.core.error_tracking.alerting import AlertEngine
from modulo.core.error_tracking.forwarders import get_forwarder
from modulo.core.error_tracking.metrics import init_metrics, record_error_ingest
from modulo.db.crud.error_tracking import (
    create_error_event,
    get_error_group_by_fingerprint,
    upsert_error_group,
)
from modulo.db.models.error_event import ErrorEvent
from modulo.db.models.error_forwarder_config import ErrorForwarderConfig
from modulo.db.models.error_notification_rule import DeletedDefault, ErrorNotificationRule
from modulo.db.models.organisation import Organisation
from modulo.db.models.system_config import SystemConfig
from modulo.db.rls import set_rls_org
from modulo.otel_bridge import trace_id_for_run
from modulo.settings import get_settings

_log = logging.getLogger(__name__)

_STACKTRACE_FILE_RE = re.compile(r'File "[^"]+", line \d+,')
_HMAC_KEY_TTL = 3600

# Module-level alert engine (lazy-initialised)
_alert_engine: AlertEngine | None = None
_alert_engine_lock = asyncio.Lock()


async def _get_alert_engine(redis_client: Any = None) -> AlertEngine:
    """Return the module-level singleton AlertEngine (lazy-init, lock-guarded).

    The first caller's redis client wins — later callers share the same engine.
    """
    global _alert_engine
    if _alert_engine is not None:
        return _alert_engine
    async with _alert_engine_lock:
        if _alert_engine is None:
            _alert_engine = AlertEngine(redis_client=redis_client)
    return _alert_engine


# ---------------------------------------------------------------------------
# Per-signal ingestion (FAR-151 §15.8) + seeded default rules (§15.6)
# ---------------------------------------------------------------------------

SIGNAL_AGENT_FAILED = "agent.failed"
SIGNAL_AGENT_NOOP = "agent.no_op"
SIGNAL_AGENT_STALL = "agent.stall"
SIGNAL_CONTRACT_SCHEMA = "contract.schema"

# Harness/sandbox/connector transient error classes are ingested with
# ``signal = error_code`` (fingerprint ``{error_code}:{pipeline_id}``), so their
# signal set is dynamic — seeded rules cover the fixed signals below.
DEFAULT_SIGNALS: tuple[str, ...] = (
    SIGNAL_AGENT_FAILED,
    SIGNAL_AGENT_NOOP,
    SIGNAL_AGENT_STALL,
    SIGNAL_CONTRACT_SCHEMA,
)

SEEDED_DEFAULTS_VERSION_KEY = "seeded_defaults_version"
SEEDED_DEFAULTS_VERSION = 1

# (signal, name, level, action_type) — seeded rows carry ``is_default=true`` and
# are user-editable; a version-bump re-seed force-updates only never-edited rows.
DEFAULT_ALERT_RULES: list[dict[str, str]] = [
    {"signal": SIGNAL_AGENT_FAILED, "name": "Agent failed", "level": "critical", "action_type": "in_app"},
    {
        "signal": SIGNAL_AGENT_NOOP,
        "name": "Agent produced no output",
        "level": "warning",
        "action_type": "in_app",
    },
    {"signal": SIGNAL_AGENT_STALL, "name": "Agent stalled", "level": "warning", "action_type": "in_app"},
    {
        "signal": SIGNAL_CONTRACT_SCHEMA,
        "name": "Contract schema violation",
        "level": "warning",
        "action_type": "in_app",
    },
]

_FIRE_ONCE_TTL_SECONDS = 7 * 24 * 3600  # superseder chain time-box is 24h; 7d is generous
_FIRE_ONCE_KEY_PREFIX = "alert_fire_once"
_fire_once_memory: dict[str, float] = {}


def signal_fingerprint(signal: str, pipeline_id: uuid.UUID | None) -> str:
    """STABLE per-signal fingerprint: SHA-256 of ``{signal}:{pipeline_id}``.

    ``run_id`` never enters the fingerprint — it lives in the event context — so
    windowed rules (``min_count > 1``) keep counting across retries of the same
    signal+pipeline (FAR-151 §15.8).
    """
    raw = f"{signal}:{pipeline_id}" if pipeline_id is not None else signal
    return hashlib.sha256(raw.encode()).hexdigest()


def _run_trace_id(org_id: Any, run_id: Any) -> str | None:
    """Deterministic OTel trace id for an error event's run (FAR-198).

    Mirrors ``RunResponse.trace_id`` (uuid5 of the run's LangGraph thread id
    ``{org_id}:{run_id}``) so error events deep-link to the same trace.
    Fail-open: an unusable org/run id must never break error ingestion.
    """
    if run_id is None:
        return None
    try:
        return trace_id_for_run(org_id, run_id)
    except (TypeError, ValueError, AttributeError):
        return None


async def _create_signal_event(
    session: Any,
    *,
    org_id: Any,
    fingerprint: str,
    level: str,
    message: str,
    signal: str,
    context_json: dict[str, Any] | None,
    environment: str | None,
) -> ErrorEvent:
    """Persist one per-signal ErrorEvent (the new signal writer)."""
    event = ErrorEvent(
        organisation_id=org_id,
        fingerprint=fingerprint,
        level=level,
        message=message,
        source="saq",
        signal=signal,
        context_json=context_json,
        environment=environment,
    )
    session.add(event)
    await session.flush()
    return event


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


def _sanitize_context_json(value: Any, sanitize: Any) -> Any:
    """Recursively sanitize every string leaf in a context_json payload."""
    if isinstance(value, str):
        return sanitize(value)
    if isinstance(value, dict):
        return {k: _sanitize_context_json(v, sanitize) for k, v in value.items()}
    if isinstance(value, list):
        return [_sanitize_context_json(v, sanitize) for v in value]
    return value


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
        return await _get_alert_engine(self._redis)

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

        # Sanitize at the ingest choke point (P8) — BEFORE the fingerprint — so
        # two events differing only by an embedded secret hash to the SAME
        # group. Idempotent + a no-op for clean inputs, so existing fingerprint
        # expectations are unchanged. Covers all 5 ingestion paths (logging
        # handler, webhooks, cron_helpers, saq_hooks, public ingest).
        from modulo.core.pipeline_engine.error_codes import sanitize_error_text

        message = sanitize_error_text(message)
        raw_stacktrace = event_data.get("stacktrace")
        stacktrace = None if raw_stacktrace is None else sanitize_error_text(raw_stacktrace)
        context_json = _sanitize_context_json(event_data.get("context_json"), sanitize_error_text)

        fp = self.fingerprint(
            message=message,
            stacktrace=stacktrace,
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
            stacktrace=stacktrace,
            context_json=context_json,
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
# Per-signal ingestion writers + retry-alert compensation (FAR-151 §15.5/15.8)
#
# * :func:`emit_signal_event` — the new per-signal writer. Creates an
#   ErrorEvent with a STABLE fingerprint per (signal, pipeline_id) and feeds the
#   single AlertEngine evaluator.
# * :func:`emit_retry_deferred_alert` — fire-once-per-(run_group, signal)
#   deferred critical for a retry cancelled by supersession.
# * :func:`emit_alert_resolved` — ``alert_resolved`` lifecycle event when a
#   superseding run terminalizes success (a moot critical never stays open).
# ---------------------------------------------------------------------------


async def emit_signal_event(
    session: Any,
    org_id: Any,
    *,
    signal: str,
    pipeline_id: uuid.UUID | None,
    message: str,
    level: str,
    environment: str | None = None,
    run_id: str | None = None,
    run_group_id: uuid.UUID | None = None,
    attempt_n: int | None = None,
    elevation_signal: str | None = None,
    redis_client: Any = None,
) -> dict[str, Any]:
    """Ingest a per-signal run event (new writer) and evaluate alert rules.

    The ErrorEvent carries ``signal`` and a STABLE fingerprint per
    (signal, pipeline_id); ``run_id`` lives in the event context, never the
    fingerprint, so windowed rules (``min_count > 1``) keep firing across
    retries. Signal-keyed rules (``rule.signal`` set) fire only when their
    signal matches; legacy level-based rules keep matching by level.
    """
    fp = signal_fingerprint(signal, pipeline_id)
    event = await _create_signal_event(
        session,
        org_id=org_id,
        fingerprint=fp,
        level=level,
        message=message,
        signal=signal,
        context_json={
            "signal": signal,
            "run_id": run_id,
            "run_group_id": str(run_group_id) if run_group_id is not None else None,
            "attempt_n": attempt_n,
            "pipeline_id": str(pipeline_id) if pipeline_id is not None else None,
            "trace_id": _run_trace_id(org_id, run_id),
        },
        environment=environment,
    )
    existing = await get_error_group_by_fingerprint(session=session, org_id=org_id, fingerprint=fp)
    group = await upsert_error_group(
        session=session,
        org_id=org_id,
        fingerprint=fp,
        level=level,
        sample_event_id=event.id,
    )
    record_error_ingest(level, "saq", environment)

    try:
        engine = await _get_alert_engine(redis_client)
        alerts = await engine.evaluate(
            org_id=org_id,
            session=session,
            error_group_id=group.id,
            fingerprint=fp,
            level=level,
            count=group.count,
            environment=environment,
            signal=signal,
            run_id=run_id,
            elevation_signal=elevation_signal,
            attempt_n=attempt_n,
            run_group_id=run_group_id,
        )
        if alerts:
            await engine.dispatch_all(org_id=org_id, alerts=alerts, session=session, error_group=group)
    except Exception:
        _log.exception("error_tracking.signal_alert_evaluation_failed signal=%s", signal)

    return {"group_id": str(group.id), "is_new": existing is None}


async def _fire_once_allowed(redis_client: Any, org_id: Any, run_group_id: Any, signal: str) -> bool:
    """Atomic check-and-mark: the deferred retry critical fires ONCE per (run_group, signal).

    ``SET NX EX`` is both the check and the mark, so concurrent emitters in a
    superseded chain cannot double-fire. On a Redis failure the guard FAILS OPEN
    (returns True so the alert fires) — a fire-once guard must never suppress a
    real alert because Redis is down. Without Redis, falls back to an in-memory
    dict (debug mode).
    """
    key = f"{_FIRE_ONCE_KEY_PREFIX}:{org_id}:{run_group_id}:{signal}"
    if redis_client is not None:
        try:
            return bool(await redis_client.set(key, "1", nx=True, ex=_FIRE_ONCE_TTL_SECONDS))
        except asyncio.CancelledError:
            raise
        except Exception:
            _log.warning("error_tracking.fire_once_redis_failed signal=%s", signal)
            return True
    now = time.monotonic()
    if key in _fire_once_memory and now - _fire_once_memory[key] < _FIRE_ONCE_TTL_SECONDS:
        return False
    _fire_once_memory[key] = now
    return True


async def emit_retry_deferred_alert(
    session: Any,
    org_id: Any,
    *,
    run_id: str,
    run_group_id: Any,
    signal: str,
    pipeline_id: uuid.UUID | None,
    message: str,
    attempt_n: int,
    reason: str,
    environment: str | None = None,
    redis_client: Any = None,
) -> bool:
    """Fire the deferred critical for a retry cancelled by supersession.

    Fires ONCE per (run_group, signal) with ``attempt_n`` + ``reason`` — the
    fire-once guard prevents re-fire across a superseded chain. Returns True
    when the alert was emitted, False when a previous emission in the same
    run_group+signal window already fired it.
    """
    if not await _fire_once_allowed(redis_client, org_id, run_group_id, signal):
        _log.debug(
            "error_tracking.retry_deferred_alert_suppressed",
            extra={"signal": signal, "run_group_id": str(run_group_id)},
        )
        return False
    fp = signal_fingerprint(signal, pipeline_id)
    event = await _create_signal_event(
        session,
        org_id=org_id,
        fingerprint=fp,
        level="critical",
        message=message,
        signal=signal,
        context_json={
            "run_id": run_id,
            "run_group_id": str(run_group_id),
            "attempt_n": attempt_n,
            "reason": reason,
            "elevation_signal": signal,
            "pipeline_id": str(pipeline_id) if pipeline_id is not None else None,
            "trace_id": _run_trace_id(org_id, run_id),
        },
        environment=environment,
    )
    group = await upsert_error_group(
        session=session,
        org_id=org_id,
        fingerprint=fp,
        level="critical",
        sample_event_id=event.id,
    )
    record_error_ingest("critical", "saq", environment)
    try:
        engine = await _get_alert_engine(redis_client)
        alerts = await engine.evaluate(
            org_id=org_id,
            session=session,
            error_group_id=group.id,
            fingerprint=fp,
            level="critical",
            count=group.count,
            environment=environment,
            signal=signal,
            run_id=run_id,
            elevation_signal=signal,
            attempt_n=attempt_n,
            run_group_id=run_group_id,
        )
        if alerts:
            await engine.dispatch_all(org_id=org_id, alerts=alerts, session=session, error_group=group)
    except Exception:
        _log.exception("error_tracking.retry_deferred_alert_evaluation_failed signal=%s", signal)
    return True


async def emit_alert_resolved(
    session: Any,
    org_id: Any,
    *,
    signal: str,
    group_id: Any,
    reason: str,
) -> None:
    """Emit an ``alert_resolved`` lifecycle event for an earlier critical now moot.

    Looks up the org's rule for *signal* to find its delivery webhook (if any)
    and records the resolution in-app. Best-effort: never propagates (the
    superseding-run terminalization path must not fail on a notification).
    """
    from modulo.core.error_tracking.alert_dispatcher import dispatch_alert_resolved

    webhook_url: str | None = None
    try:
        rule_result = await session.execute(
            select(ErrorNotificationRule).where(
                ErrorNotificationRule.organisation_id == org_id,
                ErrorNotificationRule.signal == signal,
            )
        )
        rule = rule_result.scalar_one_or_none()
        if rule is not None:
            webhook_url = rule.webhook_url
    except Exception:
        _log.exception("error_tracking.alert_resolved_rule_lookup_failed signal=%s", signal)
    try:
        await dispatch_alert_resolved(
            org_id=org_id,
            group_id=group_id,
            signal=signal,
            reason=reason,
            session=session,
            webhook_url=webhook_url,
        )
    except Exception:
        _log.exception("error_tracking.alert_resolved_dispatch_failed signal=%s", signal)


# ---------------------------------------------------------------------------
# Seeded default alert rules + tombstones (FAR-151 §15.6)
#
# Settings row ``seeded_defaults_version`` (a SystemConfig key) triggers the
# re-seed. The upsert is keyed by (org_id, signal): it adds missing signals and
# never touches edited (``is_default=false``) or tombstoned rows. A version bump
# force-updates only rows still ``is_default=true``. ``deleted_defaults`` is the
# tombstone: restore-defaults skips tombstoned signals; a per-rule restore
# clears the tombstone so a re-seed re-adds the rule.
# ---------------------------------------------------------------------------


async def seed_default_alert_rules_for_org(session: AsyncSession, org_id: Any) -> int:
    """Upsert the default alert rules for *org_id* (idempotent).

    Caller must be inside ``session.begin()`` — RLS context is set here.
    Returns the number of rows seeded/force-updated.
    """
    await set_rls_org(session, org_id)
    tomb_result = await session.execute(select(DeletedDefault.signal).where(DeletedDefault.organisation_id == org_id))
    tombstoned = {row[0] for row in tomb_result.all()}

    seeded = 0
    for spec in DEFAULT_ALERT_RULES:
        signal = spec["signal"]
        if signal in tombstoned:
            continue
        rule_result = await session.execute(
            select(ErrorNotificationRule).where(
                ErrorNotificationRule.organisation_id == org_id,
                ErrorNotificationRule.signal == signal,
            )
        )
        rule = rule_result.scalar_one_or_none()
        if rule is None:
            session.add(
                ErrorNotificationRule(
                    organisation_id=org_id,
                    name=spec["name"],
                    enabled=True,
                    condition_level=spec["level"],
                    condition_min_count=1,
                    condition_window_seconds=0,
                    action_type=spec["action_type"],
                    webhook_url=None,
                    cooldown_seconds=300,
                    signal=signal,
                    is_default=True,
                )
            )
            seeded += 1
        elif rule.is_default:
            # Version bump: force-update only never-edited default rows.
            rule.name = spec["name"]
            rule.condition_level = spec["level"]
            rule.action_type = spec["action_type"]
            seeded += 1
    return seeded


async def seed_default_alert_rules(factory: Any) -> int:
    """Seed default alert rules for every org, gated on ``seeded_defaults_version``.

    Org enumeration runs in SYSTEM CONTEXT (no RLS); ``set_rls_org`` applies
    only inside each per-org transaction. The marker is bumped to the current
    version only after every org has been visited, so a partial failure retries
    on the next boot.
    """
    async with factory() as session, session.begin():
        marker_result = await session.execute(
            select(SystemConfig).where(SystemConfig.key == SEEDED_DEFAULTS_VERSION_KEY)
        )
        marker = marker_result.scalar_one_or_none()
        if marker is not None:
            try:
                current_version = int(marker.value)
            except (TypeError, ValueError):
                current_version = 0
            if current_version >= SEEDED_DEFAULTS_VERSION:
                return 0
        org_result = await session.execute(select(Organisation.id).order_by(Organisation.created_at))
        org_ids = [row[0] for row in org_result.all()]

    seeded = 0
    for org_id in org_ids:
        try:
            async with factory() as session, session.begin():
                seeded += await seed_default_alert_rules_for_org(session, org_id)
        except asyncio.CancelledError:
            raise
        except Exception:
            _log.exception("error_tracking.seed_defaults_org_failed org=%s", org_id)

    async with factory() as session, session.begin():
        marker_result = await session.execute(
            select(SystemConfig).where(SystemConfig.key == SEEDED_DEFAULTS_VERSION_KEY)
        )
        marker = marker_result.scalar_one_or_none()
        if marker is None:
            session.add(SystemConfig(key=SEEDED_DEFAULTS_VERSION_KEY, value=SEEDED_DEFAULTS_VERSION))
        else:
            marker.value = SEEDED_DEFAULTS_VERSION

    return seeded


async def tombstone_default_rule(session: AsyncSession, org_id: Any, signal: str) -> None:
    """Mark *signal*'s default rule as deleted so restore-defaults skips it.

    Caller must be inside ``session.begin()``. Idempotent.
    """
    await set_rls_org(session, org_id)
    existing = await session.execute(
        select(DeletedDefault).where(
            DeletedDefault.organisation_id == org_id,
            DeletedDefault.signal == signal,
        )
    )
    if existing.scalar_one_or_none() is None:
        session.add(DeletedDefault(organisation_id=org_id, signal=signal))


async def clear_default_rule_tombstone(session: AsyncSession, org_id: Any, signal: str) -> bool:
    """Per-rule restore: clear *signal*'s tombstone so a re-seed re-adds the rule.

    Caller must be inside ``session.begin()``. Returns True when a tombstone
    was removed.
    """
    await set_rls_org(session, org_id)
    existing = await session.execute(
        select(DeletedDefault).where(
            DeletedDefault.organisation_id == org_id,
            DeletedDefault.signal == signal,
        )
    )
    row = existing.scalar_one_or_none()
    if row is None:
        return False
    await session.delete(row)
    return True


async def restore_default_alert_rules_for_org(session: AsyncSession, org_id: Any) -> int:
    """Restore deleted default rules: clear ALL tombstones, then re-seed.

    Caller must be inside ``session.begin()``. Returns the number of rules
    (re)seeded.
    """
    await set_rls_org(session, org_id)
    tombstones = await session.execute(select(DeletedDefault).where(DeletedDefault.organisation_id == org_id))
    for row in tombstones.scalars().all():
        await session.delete(row)
    return await seed_default_alert_rules_for_org(session, org_id)


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
        _log.debug("error_tracking.cadence_uncomputable", extra={"trigger_type": trigger_type})
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
                        # ``ongoing`` is INTENTIONALLY excluded here (FAR-158):
                        # the type self-heals — the top-up recomputes from
                        # current state every scan, so a missed tick needs no
                        # alert, and an at-target no-op is NOT a missed fire.
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
