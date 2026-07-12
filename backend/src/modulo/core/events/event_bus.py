"""In-memory event bus for real-time frontend sync via SSE."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import threading
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from modulo.core.events.redis_broker import RedisEventBroker

_log = logging.getLogger(__name__)

type _SubscriberMap = dict[str, list[asyncio.Queue[dict[str, Any]]]]

_background_tasks: set[asyncio.Task[Any]] = set()
_bus_init_lock: threading.Lock = threading.Lock()


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
        """Initialize with an optional Redis broker for cross-worker broadcast."""
        self._subscribers: _SubscriberMap = {}
        self._redis_broker = redis_broker
        self._lock: asyncio.Lock = asyncio.Lock()

    async def publish(
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
        async with self._lock:
            queues = list(self._subscribers.get(org_id, []))
        for q in queues:
            try:
                q.put_nowait(event)
            except asyncio.QueueFull:
                dead.append(q)
        await self._remove_dead_queues(org_id, dead)
        self._redis_broadcast_if_configured(org_id, event)

    async def _remove_dead_queues(
        self,
        org_id: str,
        dead: list[asyncio.Queue[dict[str, Any]]],
    ) -> None:
        if not dead:
            return
        async with self._lock:
            sub_list = self._subscribers.get(org_id)
            if sub_list is None:
                return
            for q in dead:
                with contextlib.suppress(ValueError):
                    sub_list.remove(q)
            if not sub_list:
                del self._subscribers[org_id]

    def _redis_broadcast_if_configured(self, org_id: str, event: dict[str, Any]) -> None:
        broker = self._redis_broker
        if broker is None:
            return
        task = asyncio.create_task(self._redis_broadcast(broker, org_id, event))
        _background_tasks.add(task)
        task.add_done_callback(_background_tasks.discard)

    async def _redis_broadcast(self, broker: RedisEventBroker, org_id: str, event: dict[str, Any]) -> None:
        """Fire-and-forget: publish event to Redis channel (best-effort)."""
        try:
            await broker.publish(f"resource:{org_id}", event)
        except asyncio.CancelledError:
            raise
        except Exception:
            _log.exception("event_bus.redis_broadcast_failed", extra={"org_id": org_id})

    async def subscribe(self, org_id: str, maxsize: int = 256) -> asyncio.Queue[dict[str, Any]]:
        """Return a queue that receives resource-change events for the org.

        The queue has a finite *maxsize* so that slow consumers are detected
        and ejected by the publisher (see ``QueueFull`` handling in
        :meth:`publish`).
        """
        async with self._lock:
            q: asyncio.Queue[dict[str, Any]] = asyncio.Queue(maxsize=maxsize)
            if org_id not in self._subscribers:
                self._subscribers[org_id] = []
            self._subscribers[org_id].append(q)
            return q

    async def unsubscribe(self, org_id: str, queue: asyncio.Queue[dict[str, Any]]) -> None:
        """Remove a subscriber queue from the org's fan-out set."""
        async with self._lock:
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


def _set_event_bus(bus: EventBus | None) -> None:
    global _event_bus
    _event_bus = bus


def get_event_bus() -> EventBus:
    """Return the module-level EventBus singleton (lazy init)."""
    if _event_bus is None:
        with _bus_init_lock:
            if _event_bus is None:
                _set_event_bus(EventBus())
    assert _event_bus is not None
    return _event_bus


async def configure_event_bus(redis_broker: RedisEventBroker | None = None) -> None:
    """Configure the module-level EventBus with an optional Redis broker.

    Call during application startup (before any events are published) to
    enable cross-worker event broadcasting via Redis.
    """
    old: RedisEventBroker | None = None
    with _bus_init_lock:
        if _event_bus is None:
            _set_event_bus(EventBus(redis_broker=redis_broker))
            return
        old = _event_bus._redis_broker
        _event_bus._redis_broker = redis_broker
    if old is not None and old is not redis_broker:
        try:
            await old.close()
        except asyncio.CancelledError:
            raise
        except Exception:
            _log.warning("event_bus.close_old_broker_failed", exc_info=True)
