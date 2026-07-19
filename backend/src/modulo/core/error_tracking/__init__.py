"""Error ingestion service — fingerprinting, batch ingest, HMAC session key store."""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import logging
import re
import secrets
import time
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from redis.asyncio import Redis

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

logger = logging.getLogger(__name__)
_log = logging.getLogger(__name__)

_STACKTRACE_FILE_RE = re.compile(r'File "[^"]+", line \d+,')
_HMAC_KEY_TTL = 3600

# Module-level alert engine (lazy-initialised)
_alert_engine: AlertEngine | None = None
_alert_engine_lock = asyncio.Lock()


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


class _SessionKeyEntry:
    __slots__ = ("expires_at", "key")

    def __init__(self, key: str, ttl: int = _HMAC_KEY_TTL) -> None:
        if ttl <= 0:
            raise ValueError(f"ttl must be positive, got {ttl}")
        self.key = key
        self.expires_at = time.time() + ttl

    @property
    def expired(self) -> bool:
        return time.time() > self.expires_at


class SessionKeyStore:
    """In-memory HMAC key store (Redis-backed when ``redis_client`` provided).

    Keys are identified by ``account_id`` (str). Each key has a 1-hour TTL.
    """

    def __init__(self, redis_client: Redis | None = None) -> None:
        self._redis = redis_client
        self._memory: dict[str, _SessionKeyEntry] = {}
        if redis_client is None:
            _log.warning("No Redis client for HMAC session keys — using in-memory store (non-persistent)")

    async def _cleanup_expired(self) -> None:
        now = time.time()
        expired = [k for k, v in self._memory.items() if v.expires_at <= now]
        for k in expired[:10]:
            self._memory.pop(k, None)

    async def generate_key(self, account_id: str) -> str:
        await self._cleanup_expired()
        key = secrets.token_hex(32)
        if self._redis is not None:
            try:
                await self._redis.setex(f"error_hmac_key:{account_id}", _HMAC_KEY_TTL, key)
            except Exception:
                _log.exception("session_key_store.redis_set_failed", extra={"account_id": account_id})
                self._memory[account_id] = _SessionKeyEntry(key)
        else:
            self._memory[account_id] = _SessionKeyEntry(key)
        return key

    async def get_key(self, account_id: str) -> str | None:
        await self._cleanup_expired()
        if self._redis is not None:
            try:
                val = await self._redis.get(f"error_hmac_key:{account_id}")
                return val.decode() if isinstance(val, bytes) else val
            except Exception:
                _log.exception("session_key_store.redis_get_failed", extra={"account_id": account_id})
        entry = self._memory.get(account_id)
        if entry is None or entry.expired:
            self._memory.pop(account_id, None)
            return None
        return entry.key

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
            logger.exception("core.error_tracking")

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
