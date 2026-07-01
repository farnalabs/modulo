"""Error ingestion service — fingerprinting, batch ingest, HMAC session key store."""

from __future__ import annotations

import hashlib
import hmac
import logging
import re
import secrets
import time
from typing import Any

from modulo.db.crud.error_tracking import (
    create_error_event,
    get_error_group_by_fingerprint,
    upsert_error_group,
)

_log = logging.getLogger(__name__)

_STACKTRACE_FILE_RE = re.compile(r'File "[^"]+", line \d+,')
_HMAC_KEY_TTL = 3600


def _normalize_stacktrace(stacktrace: str) -> str:
    lines = stacktrace.strip().split("\n")[:5]
    return "\n".join(
        _STACKTRACE_FILE_RE.sub("", line).strip()
        for line in lines
    )


class FingerprintError(Exception):
    pass


class ErrorIngestionService:
    """Creates error events, upserts groups, batches.

    All methods accept a SQLAlchemy ``AsyncSession`` (or any compatible
    async session) and an ``org_id`` (typically ``uuid.UUID``).
    """

    @staticmethod
    def fingerprint(message: str, stacktrace: str | None = None, source: str = "") -> str:
        """SHA-256 of (message + normalised stacktrace top 5 frames + source)."""
        normalised = _normalize_stacktrace(stacktrace or "")
        raw = f"{message}|{normalised}|{source}"
        return hashlib.sha256(raw.encode()).hexdigest()

    async def ingest(
        self,
        session: Any,
        org_id: Any,
        event_data: dict[str, Any],
    ) -> dict[str, Any]:
        fp = self.fingerprint(
            message=event_data["message"],
            stacktrace=event_data.get("stacktrace"),
            source=event_data["source"],
        )
        event = await create_error_event(
            session=session,
            org_id=org_id,
            fingerprint=fp,
            level=event_data["level"],
            message=event_data["message"],
            source=event_data["source"],
            stacktrace=event_data.get("stacktrace"),
            context_json=event_data.get("context_json"),
            environment=event_data.get("environment"),
            version=event_data.get("version"),
        )
        existing = await get_error_group_by_fingerprint(session=session, org_id=org_id, fingerprint=fp)
        group = await upsert_error_group(
            session=session,
            org_id=org_id,
            fingerprint=fp,
            level=event_data["level"],
            sample_event_id=event.id,
        )
        return {"group_id": str(group.id), "is_new": existing is None}

    async def ingest_batch(
        self,
        session: Any,
        org_id: Any,
        events: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        results: list[dict[str, Any]] = []
        for event_data in events:
            results.append(await self.ingest(session, org_id, event_data))
        return results


# ---------------------------------------------------------------------------
# HMAC session-key store
# ---------------------------------------------------------------------------


class _SessionKeyEntry:
    __slots__ = ("expires_at", "key")

    def __init__(self, key: str, ttl: int = _HMAC_KEY_TTL) -> None:
        self.key = key
        self.expires_at = time.time() + ttl

    @property
    def expired(self) -> bool:
        return time.time() > self.expires_at


class SessionKeyStore:
    """In-memory HMAC key store (Redis-backed when ``redis_client`` provided).

    Keys are identified by ``account_id`` (str). Each key has a 1-hour TTL.
    """

    def __init__(self, redis_client: Any | None = None) -> None:
        self._redis = redis_client
        self._memory: dict[str, _SessionKeyEntry] = {}
        if redis_client is None:
            _log.warning("No Redis client for HMAC session keys — using in-memory store (non-persistent)")

    async def generate_key(self, account_id: str) -> str:
        key = secrets.token_hex(32)
        if self._redis is not None:
            await self._redis.setex(f"error_hmac_key:{account_id}", _HMAC_KEY_TTL, key)
        else:
            self._memory[account_id] = _SessionKeyEntry(key)
        return key

    async def get_key(self, account_id: str) -> str | None:
        if self._redis is not None:
            val = await self._redis.get(f"error_hmac_key:{account_id}")
            return val.decode() if isinstance(val, bytes) else val
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
