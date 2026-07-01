"""In-memory event bus for real-time frontend sync via SSE."""

from __future__ import annotations

import asyncio
import logging
import threading
from typing import Any

from modulo.core.events.redis_broker import RedisEventBroker

_log = logging.getLogger(__name__)

_background_tasks: set[asyncio.Task[Any]] = set()


class EventBus:
    """In-memory pub/sub event bus for org-scoped resource change events.

    Each org has a set of subscriber queues. Publishers fan out to all
    subscribers of the target org. If a *redis_broker* is configured,
    events are also broadcast to Redis for cross-worker delivery.

    Slow consumers (queues that fill up) are automatically removed to
    prevent back-pressure on publishers.

    Coroutine-safe: all subscriber list mutations are guarded by an asyncio
    lock so concurrent publish/subscribe/unsubscribe calls from different
    coroutines do not race on shared state.
    """

    def __init__(self, redis_broker: RedisEventBroker | None = None) -> None:
        self._subscribers: dict[str, list[asyncio.Queue[dict[str, Any]]]] = {}
        self._redis_broker = redis_broker
        self._lock: threading.Lock = threading.Lock()

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
        dead: list[asyncio.Queue[dict[str, Any]]] = []
        queues = list(self._subscribers.get(org_id, []))
        for q in queues:
            try:
                q.put_nowait(event)
            except (asyncio.QueueFull, ValueError):
                dead.append(q)
        if dead:
            with self._lock:
                sub_list = self._subscribers.get(org_id)
                if sub_list is not None:
                    for q in dead:
                        try:
                            sub_list.remove(q)
                        except ValueError:
                            pass
                    if not sub_list:
                        del self._subscribers[org_id]
        broker = self._redis_broker
        if broker is not None:
            try:
                asyncio.get_running_loop()
            except RuntimeError:
                _log.warning("event_bus.no_running_loop", extra={"org_id": org_id})
            else:
                _task = asyncio.create_task(self._redis_broadcast(broker, org_id, event))
                _background_tasks.add(_task)
                _task.add_done_callback(_background_tasks.discard)

    async def _redis_broadcast(self, broker: RedisEventBroker, org_id: str, event: dict[str, Any]) -> None:
        """Fire-and-forget: publish event to Redis channel (best-effort)."""
        try:
            await broker.publish(f"resource:{org_id}", event)
        except Exception:
            _log.exception("event_bus.redis_broadcast_failed", extra={"org_id": org_id})

    async def subscribe(self, org_id: str, maxsize: int = 256) -> asyncio.Queue[dict[str, Any]]:
        """Return a queue that receives resource-change events for the org.

        The queue has a finite *maxsize* so that slow consumers are detected
        and ejected by the publisher (see ``QueueFull`` handling in
        :meth:`publish`).
        """
        with self._lock:
            q: asyncio.Queue[dict[str, Any]] = asyncio.Queue(maxsize=maxsize)
            if org_id not in self._subscribers:
                self._subscribers[org_id] = []
            self._subscribers[org_id].append(q)
            return q

    async def unsubscribe(self, org_id: str, queue: asyncio.Queue[dict[str, Any]]) -> None:
        """Remove a subscriber queue from the org's fan-out set."""
        with self._lock:
            sub_list = self._subscribers.get(org_id)
            if sub_list is None:
                return
            try:
                sub_list.remove(queue)
            except ValueError:
                return
            if not sub_list:
                del self._subscribers[org_id]


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
