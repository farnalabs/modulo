"""Per-run WebSocket event broker.

Architecture:
- One RunEventBroker per active run (keyed by run_id in BrokerRegistry)
- The PipelineExecutor consumes astream_events() and calls broker.publish()
- WebSocket connections subscribe via broker.subscribe(), receiving a Queue
- On reconnect, broker.replay_since(seq) replays buffered events
- 100-event ring buffer per run (in-memory for alpha)

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
    """Fan-out broker for a single run. Thread-safe via asyncio primitives."""

    def __init__(self, run_id: uuid.UUID) -> None:
        self._run_id = run_id
        self._seq = 0
        self._buffer: deque[RunEvent] = deque(maxlen=_RING_BUFFER_SIZE)
        self._subscribers: weakref.WeakSet[asyncio.Queue[RunEvent | None]] = weakref.WeakSet()
        self._closed = False

    @property
    def run_id(self) -> uuid.UUID:
        return self._run_id

    def publish(self, event_type: str, payload: dict[str, Any]) -> RunEvent:
        """Emit an event, appending to buffer and notifying all subscribers."""
        if self._closed:
            raise RuntimeError(f"Broker for run {self._run_id} is closed")
        self._seq += 1
        event = RunEvent(seq=self._seq, event_type=event_type, run_id=self._run_id, payload=payload)
        self._buffer.append(event)
        for q in self._subscribers:
            q.put_nowait(event)
        return event

    def subscribe(self) -> asyncio.Queue[RunEvent | None]:
        """Return a Queue that receives future events. Caller must unsubscribe."""
        q: asyncio.Queue[RunEvent | None] = asyncio.Queue()
        self._subscribers.add(q)
        return q

    def unsubscribe(self, q: asyncio.Queue[RunEvent | None]) -> None:
        self._subscribers.discard(q)

    def replay_since(self, seq: int) -> list[RunEvent]:
        """Return all buffered events with seq > the given value (oldest first)."""
        return [e for e in self._buffer if e.seq > seq]

    def close(self) -> None:
        """Signal all subscribers that the run is done (sends None sentinel)."""
        self._closed = True
        for q in self._subscribers:
            q.put_nowait(None)
        self._subscribers.clear()

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
    """Global registry of per-run brokers. One instance per process (alpha)."""

    def __init__(self) -> None:
        self._brokers: dict[uuid.UUID, RunEventBroker] = {}

    def get_or_create(self, run_id: uuid.UUID) -> RunEventBroker:
        if run_id not in self._brokers:
            self._brokers[run_id] = RunEventBroker(run_id)
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
