"""In-memory event bus for real-time frontend sync via SSE."""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from modulo.core.events.redis_broker import RedisEventBroker

_log = logging.getLogger(__name__)

_CHANNEL_PREFIX = "modulo:events:resource:"


class EventBus:
    """In-memory pub/sub event bus for org-scoped resource change events.

    Each org has a set of subscriber queues. Publishers fan out to all
    subscribers of the target org. If a *redis_broker* is configured,
    events are also broadcast to Redis for cross-worker delivery.

    Slow consumers (queues that fill up) are automatically removed to
    prevent back-pressure on publishers.

    Thread-safe: all subscriber list mutations are guarded by an asyncio
    lock so concurrent publish/subscribe/unsubscribe calls from different
    coroutines do not race on shared state.
    """

    def __init__(self, redis_broker: RedisEventBroker | None = None) -> None:
        self._subscribers: dict[str, list[asyncio.Queue]] = {}
        self._redis_broker = redis_broker
        self._lock: asyncio.Lock = asyncio.Lock()

    def publish(
        self,
        org_id: str,
        resource_type: str,
        resource_id: str,
        action: str,
        version: int,
    ) -> None:
        """Fan-out a resource-change event to all subscribers of the org."""
        event: dict[str, Any] = {
            "type": resource_type,
            "id": resource_id,
            "action": action,
            "version": version,
            "org_id": org_id,
        }
        queues = list(self._subscribers.get(org_id, []))
        dead: list[asyncio.Queue] = []
        for q in queues:
            try:
                q.put_nowait(event)
            except (asyncio.QueueFull, ValueError):
                dead.append(q)
        for q in dead:
            try:
                self._subscribers[org_id].remove(q)
            except (ValueError, KeyError):
                pass
        if self._redis_broker is not None:
            _task = asyncio.ensure_future(self._redis_broadcast(org_id, event))  # noqa: RUF006  # fire-and-forget

    async def _redis_broadcast(self, org_id: str, event: dict[str, Any]) -> None:
        """Fire-and-forget: publish event to Redis channel (best-effort)."""
        try:
            await self._redis_broker.publish(f"resource:{org_id}", event)
        except Exception:
            _log.warning("event_bus.redis_broadcast_failed", extra={"org_id": org_id})

    async def subscribe(self, org_id: str) -> asyncio.Queue:
        """Return a queue that receives resource-change events for the org."""
        async with self._lock:
            q: asyncio.Queue = asyncio.Queue()
            if org_id not in self._subscribers:
                self._subscribers[org_id] = []
            self._subscribers[org_id].append(q)
            return q

    async def unsubscribe(self, org_id: str, queue: asyncio.Queue) -> None:
        """Remove a subscriber queue from the org's fan-out set."""
        async with self._lock:
            try:
                self._subscribers[org_id].remove(queue)
            except (ValueError, KeyError):
                pass


# Module-level singleton
_event_bus: EventBus | None = None


def get_event_bus() -> EventBus:
    """Return the module-level EventBus singleton (lazy init)."""
    global _event_bus
    if _event_bus is None:
        _event_bus = EventBus()
    return _event_bus


def configure_event_bus(redis_broker: RedisEventBroker | None = None) -> None:
    """Configure the module-level EventBus with an optional Redis broker.

    Call during application startup (before any events are published) to
    enable cross-worker event broadcasting via Redis.
    """
    global _event_bus
    if _event_bus is None:
        _event_bus = EventBus(redis_broker=redis_broker)
    else:
        _event_bus._redis_broker = redis_broker
