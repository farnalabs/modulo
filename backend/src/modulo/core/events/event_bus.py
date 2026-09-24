"""In-memory event bus for real-time frontend sync via SSE."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import threading
import uuid
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from modulo.core.events.redis_broker import RedisEventBroker

_log = logging.getLogger(__name__)

type _SubscriberMap = dict[str, list[asyncio.Queue[dict[str, Any]]]]

_background_tasks: set[asyncio.Task[Any]] = set()
_bus_init_lock: threading.Lock = threading.Lock()

# Per-process identity stamped onto every Redis payload (FAR-250) so the
# web-process relay can drop its own echoes instead of double-delivering
# events this process already fanned out locally.
LOCAL_PRODUCER_ID: str = uuid.uuid4().hex


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
        *,
        broadcast_redis: bool = True,
        extra: dict[str, Any] | None = None,
    ) -> None:
        """Fan-out a resource-change event to all subscribers of the org.

        *broadcast_redis* controls the cross-process Redis leg only — the
        local fan-out always happens (FAR-250: the local ``after_insert``
        delivery is the lifeline when Redis is down). Notification creates
        pass ``broadcast_redis=False``: the notifier's post-commit publish
        owns the single Redis message per create (no double-publish).
        *extra* carries notification-only payload fields (never content).
        """
        event: dict[str, Any] = {
            "type": resource_type,
            "id": resource_id,
            "action": action,
            "version": version,
            "org_id": org_id,
        }
        if extra:
            event.update(extra)
        await self.deliver_local(event)
        if broadcast_redis:
            self._redis_broadcast_if_configured(org_id, event)

    async def deliver_local(self, event: dict[str, Any]) -> None:
        """Fan an already-formed event out to local subscribers only.

        No Redis re-broadcast — used by the web-process relay to inject
        remote events without creating a cross-process echo loop, and by
        :meth:`publish` for the shared local path.
        """
        org_id = event.get("org_id")
        if not isinstance(org_id, str) or not org_id:
            _log.warning("event_bus.deliver_local_missing_org_id")
            return
        dead: list[asyncio.Queue[dict[str, Any]]] = []
        async with self._lock:
            queues = list(self._subscribers.get(org_id, []))
        for q in queues:
            try:
                q.put_nowait(event)
            except asyncio.QueueFull:
                dead.append(q)
        await self._remove_dead_queues(org_id, dead)

    async def broadcast_redis_only(self, org_id: str, event: dict[str, Any]) -> None:
        """Publish *event* to Redis without any local fan-out (FAR-250).

        The notifier's post-commit path uses this: the originating process
        already delivered locally via the ``after_insert`` listener, so only
        the cross-process Redis message is emitted here — exactly one Redis
        message per created notification.
        """
        broker = self._redis_broker
        if broker is None:
            _log.warning("event_bus.redis_broadcast_skipped", extra={"org_id": org_id})
            return
        await self._redis_broadcast(broker, org_id, event)

    @property
    def redis_broker(self) -> RedisEventBroker | None:
        """The configured Redis broker, if any (used by the SSE relay)."""
        return self._redis_broker

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
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            # No running loop (sync/threaded call site) — the fire-and-forget
            # broadcast cannot be scheduled. Fail open: the in-memory fan-out
            # already happened, Redis is best-effort (cross-worker only).
            _log.warning("event_bus.redis_broadcast_skipped", extra={"org_id": org_id})
            return
        task = loop.create_task(self._redis_broadcast(broker, org_id, event))
        _background_tasks.add(task)
        task.add_done_callback(_background_tasks.discard)

    async def _redis_broadcast(self, broker: RedisEventBroker, org_id: str, event: dict[str, Any]) -> None:
        """Fire-and-forget: publish event to Redis channel (best-effort).

        Stamps ``producer_id`` onto the Redis payload (never onto the local
        event) so relays in other processes can drop their own echoes.
        """
        payload = {**event, "producer_id": LOCAL_PRODUCER_ID}
        try:
            await broker.publish(f"resource:{org_id}", payload)
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
    if _event_bus is None:
        raise RuntimeError("event bus singleton was not initialised")
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
