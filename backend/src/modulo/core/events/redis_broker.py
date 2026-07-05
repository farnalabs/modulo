"""Redis-backed event broker for pub/sub across multiple workers."""

from __future__ import annotations

import asyncio
import json
import logging
from typing import TYPE_CHECKING, Any, ClassVar
from urllib.parse import urlparse

import redis.asyncio as aioredis

if TYPE_CHECKING:
    from redis.asyncio.client import PubSub

_log = logging.getLogger(__name__)

CHANNEL_PREFIX = "modulo:events:"


class RedisEventBroker:
    """Pub/sub event broker using Redis.

    Designed for cross-worker event distribution in multi-process deployments.
    Each event is JSON-serialized and published to a Redis channel prefixed with
    *CHANNEL_PREFIX*. Workers subscribe to the same channel prefix to receive
    events from any publisher.

    Usage:

        broker = RedisEventBroker("redis://localhost:6379/0")
        await broker.connect()

        # Publisher
        await broker.publish("run:abc-123", {"event": "node_started", ...})

        # Subscriber
        pubsub = await broker.subscribe("run:abc-123")
        async for message in pubsub.listen():
            ...
    """

    _REDIS_TIMEOUTS: ClassVar[dict[str, float]] = {"socket_connect_timeout": 2.0, "socket_timeout": 5.0}

    def __init__(self, redis_url: str = "redis://localhost:6379/0") -> None:
        """Initialize with a Redis URL; connections are lazily opened."""
        self._redis_url = redis_url
        self._pub: aioredis.Redis | None = None
        self._sub: aioredis.Redis | None = None
        self._lock: asyncio.Lock = asyncio.Lock()

    @staticmethod
    def _redact_url(url: str) -> str:
        """Return a log-safe URL with the password portion masked."""
        parsed = urlparse(url)
        if parsed.password:
            return url.replace(parsed.password, "****")
        return url

    def _make_client(self) -> aioredis.Redis:
        client = aioredis.from_url(  # type: ignore[no-untyped-call]
            self._redis_url,
            decode_responses=True,
            **self._REDIS_TIMEOUTS,
        )
        return client  # type: ignore[no-any-return]

    async def connect(self) -> None:
        """Open dedicated connections for publishing and subscribing."""
        async with self._lock:
            if self._pub is not None and self._sub is not None:
                return
            pub: aioredis.Redis | None = None
            sub: aioredis.Redis | None = None
            try:
                if self._pub is None:
                    pub = self._make_client()
                if self._sub is None:
                    sub = self._make_client()
                if pub is not None:
                    self._pub = pub
                if sub is not None:
                    self._sub = sub
            except Exception:
                _log.exception("redis_broker.connect_failed", extra={"url": self._redact_url(self._redis_url)})
                if pub is not None:
                    await pub.close()
                if sub is not None:
                    await sub.close()
                raise
            _log.info("RedisEventBroker connected to %s", self._redact_url(self._redis_url))

    async def _ensure_connected(self) -> None:
        """Ensure both connections are established."""
        if self._pub is None or self._sub is None:
            await self.connect()

    async def publish(self, channel: str, data: dict[str, Any]) -> None:
        """Serialize *data* as JSON and publish to the given *channel*."""
        async with self._lock:
            if self._pub is None:
                await self.connect()
            if self._pub is None:
                _log.error("redis_broker.publish_no_connection", extra={"channel": channel})
                return
            try:
                await self._pub.publish(f"{CHANNEL_PREFIX}{channel}", json.dumps(data))
            except (ConnectionError, TimeoutError, OSError) as exc:
                _log.exception("redis_broker.publish_failed", extra={"channel": channel, "error": str(exc)})
                self._pub = None

    async def subscribe(self, channel: str) -> PubSub:
        """Return a PubSub object subscribed to the given *channel*.

        The caller is responsible for iterating ``pubsub.listen()`` and
        calling ``await pubsub.unsubscribe()`` / ``await pubsub.close()``
        when done.
        """
        async with self._lock:
            if self._sub is None:
                await self.connect()
            if self._sub is None:
                raise RuntimeError("Redis subscriber connection not established. Call connect() first.")
            pubsub = self._sub.pubsub()
            await pubsub.subscribe(f"{CHANNEL_PREFIX}{channel}")
            return pubsub

    async def close(self) -> None:
        """Close both Redis connections."""
        async with self._lock:
            pub = self._pub
            sub = self._sub
            self._pub = None
            self._sub = None
        if pub is not None:
            try:
                await pub.close(close_connection_pool=True)
            except Exception:
                _log.warning("redis_broker.pub_close_failed", exc_info=True)
        if sub is not None:
            try:
                await sub.close(close_connection_pool=True)
            except Exception:
                _log.warning("redis_broker.sub_close_failed", exc_info=True)
        _log.info("RedisEventBroker closed for %s", self._redact_url(self._redis_url))
