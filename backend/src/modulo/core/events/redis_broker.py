"""Redis-backed event broker for pub/sub across multiple workers."""

from __future__ import annotations

import json
import logging
from typing import Any

import redis.asyncio as aioredis
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

    def __init__(self, redis_url: str = "redis://localhost:6379/0") -> None:
        self._redis_url = redis_url
        self._pub: aioredis.Redis | None = None
        self._sub: aioredis.Redis | None = None

    async def connect(self) -> None:
        """Open dedicated connections for publishing and subscribing."""
        self._pub = aioredis.from_url(self._redis_url, decode_responses=True)
        self._sub = aioredis.from_url(self._redis_url, decode_responses=True)
        _log.info("RedisEventBroker connected to %s", self._redis_url)

    async def publish(self, channel: str, data: dict[str, Any]) -> None:
        """Serialize *data* as JSON and publish to the given *channel*."""
        if self._pub is None:
            await self.connect()
        await self._pub.publish(f"{CHANNEL_PREFIX}{channel}", json.dumps(data))

    async def subscribe(self, channel: str) -> PubSub:
        """Return a PubSub object subscribed to the given *channel*.

        The caller is responsible for iterating ``pubsub.listen()`` and
        calling ``await pubsub.unsubscribe()`` / ``await pubsub.close()``
        when done.
        """
        if self._sub is None:
            await self.connect()
        pubsub = self._sub.pubsub()
        await pubsub.subscribe(f"{CHANNEL_PREFIX}{channel}")
        return pubsub

    async def close(self) -> None:
        """Close both Redis connections."""
        if self._pub is not None:
            await self._pub.close()
            self._pub = None
        if self._sub is not None:
            await self._sub.close()
            self._sub = None
        _log.info("RedisEventBroker closed")
