"""Per-run WebSocket event broker.

Architecture:
- One RunEventBroker per active run (keyed by run_id in BrokerRegistry)
- The PipelineExecutor consumes astream_events() and calls broker.publish()
- WebSocket connections subscribe via broker.subscribe(), receiving a Queue
- On reconnect, broker.replay_since(seq) replays buffered events
- 100-event ring buffer per run (in-memory for alpha)

Optionally, a RedisEventBroker can be attached for cross-worker event
broadcasting. When set, publish() also fires the event to Redis (fire-and-forget).

Subscribers are held as weak references so a disconnected WebSocket handler
that drops its queue reference is automatically cleaned up — no memory leak
from stale subscriptions.
"""

import asyncio
import uuid
import weakref
from collections import deque
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from modulo.core.events.redis_broker import RedisEventBroker


@dataclass
class RunEvent:
    seq: int
    event_type: str
    run_id: uuid.UUID
    payload: dict[str, Any]
    timestamp: datetime = field(default_factory=lambda: datetime.now(UTC))

    def to_json(self) -> dict[str, Any]:
        return {
            "seq": self.seq,
            "type": self.event_type,
            "run_id": str(self.run_id),
            "payload": self.payload,
            "timestamp": self.timestamp.isoformat(),
        }


_RING_BUFFER_SIZE = 100


class RunEventBroker:
    """Fan-out broker for a single run. Thread-safe via asyncio primitives.

    If *redis_broker* is provided, events are also broadcast to Redis so
    workers on other processes (or machines) can receive them.
    """

    def __init__(self, run_id: uuid.UUID, redis_broker: RedisEventBroker | None = None) -> None:
        self._run_id = run_id
        self._seq = 0
        self._buffer: deque[RunEvent] = deque(maxlen=_RING_BUFFER_SIZE)
        self._subscribers: weakref.WeakSet[asyncio.Queue[RunEvent | None]] = weakref.WeakSet()
        self._closed = False
        self._redis_broker = redis_broker

    @property
    def run_id(self) -> uuid.UUID:
        return self._run_id

    def publish(self, event_type: str, payload: dict[str, Any]) -> RunEvent:
        """Emit an event, appending to buffer and notifying all subscribers.

        If a *redis_broker* is attached, the event is also broadcast to Redis
        as a fire-and-forget task so other workers can receive it.
        """
        if self._closed:
            raise RuntimeError(f"Broker for run {self._run_id} is closed")
        self._seq += 1
        event = RunEvent(seq=self._seq, event_type=event_type, run_id=self._run_id, payload=payload)
        self._buffer.append(event)
        for q in self._subscribers:
            q.put_nowait(event)
        if self._redis_broker is not None:
            asyncio.ensure_future(self._redis_broker.publish(str(self._run_id), event.to_json()))  # noqa: RUF006
        return event

    def subscribe(self) -> asyncio.Queue[RunEvent | None]:
        """Return a Queue that receives future events. Caller must unsubscribe."""
        q: asyncio.Queue[RunEvent | None] = asyncio.Queue()
        self._subscribers.add(q)
        return q

    def unsubscribe(self, q: asyncio.Queue[RunEvent | None]) -> None:
        self._subscribers.discard(q)

    def replay_since(self, seq: int) -> list[RunEvent]:
        """Return all buffered events with seq > the given value (oldest first).

        Returns empty list if the requested seq is older than the oldest
        buffered event (i.e. has been evicted from the ring buffer).
        """
        if not self._buffer or (seq > 0 and seq < self._buffer[0].seq):
            return []
        return [e for e in self._buffer if e.seq > seq]

    def close(self) -> None:
        """Signal all subscribers that the run is done (sends None sentinel).

        If a *redis_broker* is attached, it is closed as well.
        """
        self._closed = True
        for q in self._subscribers:
            q.put_nowait(None)
        self._subscribers.clear()
        if self._redis_broker is not None:
            asyncio.ensure_future(self._redis_broker.close())  # noqa: RUF006

    @property
    def is_closed(self) -> bool:
        return self._closed

    @property
    def subscriber_count(self) -> int:
        return len(self._subscribers)

    @property
    def buffered_count(self) -> int:
        return len(self._buffer)


class BrokerRegistry:
    """Global registry of per-run brokers. One instance per process (alpha).

    If *redis_broker* is provided, every new RunEventBroker created by
    :meth:`get_or_create` will receive it for cross-worker broadcasting.
    """

    def __init__(self, redis_broker: RedisEventBroker | None = None) -> None:
        self._brokers: dict[uuid.UUID, RunEventBroker] = {}
        self._redis_broker = redis_broker

    def get_or_create(self, run_id: uuid.UUID) -> RunEventBroker:
        if run_id not in self._brokers:
            self._brokers[run_id] = RunEventBroker(run_id, redis_broker=self._redis_broker)
        return self._brokers[run_id]

    def get(self, run_id: uuid.UUID) -> RunEventBroker | None:
        return self._brokers.get(run_id)

    def close(self, run_id: uuid.UUID) -> None:
        broker = self._brokers.pop(run_id, None)
        if broker is not None:
            broker.close()

    @property
    def active_run_count(self) -> int:
        return len(self._brokers)


# Module-level singleton — shared by executor and WebSocket handler
_registry = BrokerRegistry()


def get_registry() -> BrokerRegistry:
    return _registry


def configure_registry(redis_broker: RedisEventBroker | None = None) -> None:
    """Set the Redis broker on the module-level registry.

    Call during application startup (before any runs) to enable cross-worker
    event broadcasting.
    """
    _registry._redis_broker = redis_broker
