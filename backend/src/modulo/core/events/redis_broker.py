"""Redis-backed event broker for pub/sub across multiple workers."""

from __future__ import annotations

import asyncio
import json
import logging
from typing import TYPE_CHECKING, Any, ClassVar, cast
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
            return url.replace(f":{parsed.password}@", ":****@")
        return url

    def _make_client(self) -> aioredis.Redis:
        return cast(
            "aioredis.Redis",
            aioredis.from_url(  # type: ignore[no-untyped-call]  # redis-py omits this annotation
                self._redis_url,
                decode_responses=True,
                **self._REDIS_TIMEOUTS,
            ),
        )

    async def _close_clients(self, pub: aioredis.Redis | None, sub: aioredis.Redis | None) -> None:
        """Best-effort close of one or both freshly created clients."""
        if pub is not None:
            await pub.close()
        if sub is not None:
            await sub.close()

    async def _adopt_new_clients(self, pub: aioredis.Redis | None, sub: aioredis.Redis | None) -> bool:
        """Store the freshly created clients, unless a concurrent connect beat us.

        Returns ``True`` if the new clients were adopted (first connect to win the
        lock), or ``False`` if both connections were already established by another
        task (in which case the new clients are closed).
        """
        async with self._lock:
            if self._pub is not None and self._sub is not None:
                await self._close_clients(pub, sub)
                return False
            if pub is not None:
                self._pub = pub
            if sub is not None:
                self._sub = sub
            return True

    async def _close_client_after_error(self, client: aioredis.Redis | None, log_key: str) -> None:
        """Best-effort close of a client after an error, logging failures.

        Re-raises ``asyncio.CancelledError`` so cancellation always propagates.
        """
        if client is None:
            return
        try:
            await client.close(close_connection_pool=True)
        except asyncio.CancelledError:
            raise
        except Exception:
            _log.warning(log_key, exc_info=True)

    async def connect(self) -> None:
        """Open dedicated connections for publishing and subscribing."""
        async with self._lock:
            if self._pub is not None and self._sub is not None:
                return
            need_pub = self._pub is None
            need_sub = self._sub is None
        pub = None
        sub = None
        try:
            if need_pub:
                pub = self._make_client()
            if need_sub:
                sub = self._make_client()
        except asyncio.CancelledError:
            raise
        except Exception:
            _log.exception("redis_broker.connect_failed", extra={"url": self._redact_url(self._redis_url)})
            await self._close_clients(pub, sub)
            raise
        if await self._adopt_new_clients(pub, sub):
            _log.info("RedisEventBroker connected to %s", self._redact_url(self._redis_url))

    async def publish(self, channel: str, data: dict[str, Any]) -> None:
        """Serialize *data* as JSON and publish to the given *channel*."""
        if self._pub is None:
            await self.connect()
        async with self._lock:
            pub = self._pub
        if pub is None:
            _log.error("redis_broker.publish_no_connection", extra={"channel": channel})
            return
        try:
            payload = json.dumps(data)
        except TypeError:
            _log.exception("redis_broker.serialize_failed", extra={"channel": channel})
            return
        try:
            await pub.publish(f"{CHANNEL_PREFIX}{channel}", payload)
        except asyncio.CancelledError:
            raise
        except Exception:
            _log.exception("redis_broker.publish_failed", extra={"channel": channel})
            async with self._lock:
                old = self._pub
                self._pub = None
            await self._close_client_after_error(old, "redis_broker.pub_close_failed_after_error")

    async def subscribe(self, channel: str) -> PubSub:
        """Return a PubSub object subscribed to the given *channel*.

        The caller is responsible for iterating ``pubsub.listen()`` and
        calling ``await pubsub.unsubscribe()`` / ``await pubsub.close()``
        when done.
        """
        if self._sub is None:
            await self.connect()
        async with self._lock:
            sub = self._sub
        if sub is None:
            raise RuntimeError(
                f"Redis subscriber connection not established for channel {channel}. Call connect() first."
            )
        pubsub = sub.pubsub()
        try:
            await pubsub.subscribe(f"{CHANNEL_PREFIX}{channel}")
        except asyncio.CancelledError:
            raise
        except Exception:
            _log.exception("redis_broker.subscribe_failed", extra={"channel": channel})
            async with self._lock:
                old = self._sub
                self._sub = None
            await self._close_client_after_error(old, "redis_broker.sub_close_failed_after_error")
            raise
        return pubsub

    async def close(self) -> None:
        """Close both Redis connections."""
        async with self._lock:
            pub = self._pub
            sub = self._sub
            self._pub = None
            self._sub = None
        await self._close_client_after_error(pub, "redis_broker.pub_close_failed")
        await self._close_client_after_error(sub, "redis_broker.sub_close_failed")
        # After releasing the lock, another publish()/subscribe() call may have
        # created new connections via connect(). That's fine - they're independent.
        _log.info("RedisEventBroker closed for %s", self._redact_url(self._redis_url))
