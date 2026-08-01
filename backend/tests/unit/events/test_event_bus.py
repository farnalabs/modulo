"""Unit tests for the in-memory EventBus and module-level bus wiring.

Covers fan-out publish semantics, slow-consumer ejection, Redis broadcast
(configured / absent / failing), subscribe/unsubscribe lifecycle, the module
singleton, and configure_event_bus broker swapping — all without a DB or Redis.
"""

from __future__ import annotations

import asyncio
from collections.abc import Iterator
from unittest.mock import AsyncMock, MagicMock

import pytest

from modulo.core.events import event_bus as eb
from modulo.core.events.event_bus import EventBus


@pytest.fixture(autouse=True)
def _reset_module_state() -> Iterator[None]:
    """Reset module-level globals that tests mutate."""
    eb._event_bus = None
    eb._background_tasks.clear()
    yield
    eb._event_bus = None
    eb._background_tasks.clear()


@pytest.fixture
def bus() -> EventBus:
    """An EventBus with no Redis broker configured."""
    return EventBus()


def _drain(q: asyncio.Queue[dict[str, object]]) -> list[dict[str, object]]:
    """Drain and return all buffered events from a subscriber queue."""
    out: list[dict[str, object]] = []
    while not q.empty():
        out.append(q.get_nowait())
    return out


async def _drain_tasks(wait: float = 0.05) -> None:
    """Let fire-and-forget Redis broadcast tasks finish."""
    for _ in range(50):
        if not eb._background_tasks:
            await asyncio.sleep(0)
            return
        await asyncio.sleep(wait / 50)


def _event(*, rid: str, version: int) -> dict[str, str | int]:
    return {"type": "run", "id": rid, "action": "created", "version": version, "org_id": "org-1"}


# ---------------------------------------------------------------------------
# publish
# ---------------------------------------------------------------------------


async def test_publish_fans_out_event_to_all_org_subscribers(bus: EventBus) -> None:
    q1 = await bus.subscribe("org-1")
    q2 = await bus.subscribe("org-1")
    other = await bus.subscribe("org-2")

    await bus.publish("org-1", "run", "run-1", "created", 3)

    expected = _event(rid="run-1", version=3)
    assert _drain(q1) == [expected]
    assert _drain(q2) == [expected]
    assert other.empty()


async def test_publish_with_no_subscribers_is_noop(bus: EventBus) -> None:
    await bus.publish("org-1", "run", "run-1", "created", 1)  # must not raise
    assert "org-1" not in bus._subscribers


async def test_publish_delivers_independent_event_copies(bus: EventBus) -> None:
    q = await bus.subscribe("org-1")
    await bus.publish("org-1", "run", "run-1", "created", 1)

    first = q.get_nowait()
    first["id"] = "mutated"

    await bus.publish("org-1", "run", "run-2", "created", 2)
    assert q.get_nowait() == _event(rid="run-2", version=2)


async def test_publish_drops_slow_consumers_that_fill_up(bus: EventBus) -> None:
    slow = await bus.subscribe("org-1", maxsize=1)
    healthy = await bus.subscribe("org-1")

    await bus.publish("org-1", "run", "r1", "created", 1)  # fills the slow queue
    await bus.publish("org-1", "run", "r2", "created", 2)  # slow queue full -> ejected
    await bus.publish("org-1", "run", "r3", "created", 3)

    assert [e["id"] for e in _drain(slow)] == ["r1"]
    assert [e["id"] for e in _drain(healthy)] == ["r1", "r2", "r3"]


async def test_publish_removes_org_entry_when_last_subscriber_is_dropped(bus: EventBus) -> None:
    await bus.subscribe("org-1", maxsize=1)
    await bus.publish("org-1", "run", "r1", "created", 1)
    await bus.publish("org-1", "run", "r2", "created", 2)

    assert "org-1" not in bus._subscribers


async def test_publish_does_not_fan_out_to_other_org_after_slow_drop(bus: EventBus) -> None:
    dropped = await bus.subscribe("org-1", maxsize=1)
    await bus.publish("org-1", "run", "r1", "created", 1)
    await bus.publish("org-1", "run", "r2", "created", 2)
    await bus.publish("org-1", "run", "r3", "created", 3)

    assert dropped.qsize() == 1


async def test_concurrent_publish_delivers_every_event(bus: EventBus) -> None:
    q = await bus.subscribe("org-1", maxsize=1000)

    await asyncio.gather(*(bus.publish("org-1", "run", f"r{i}", "created", i) for i in range(50)))

    assert q.qsize() == 50
    assert {e["id"] for e in _drain(q)} == {f"r{i}" for i in range(50)}


# ---------------------------------------------------------------------------
# _remove_dead_queues
# ---------------------------------------------------------------------------


async def test_remove_dead_queues_noop_when_no_dead(bus: EventBus) -> None:
    q = await bus.subscribe("org-1")
    await bus._remove_dead_queues("org-1", [])
    assert bus._subscribers["org-1"] == [q]


async def test_remove_dead_queues_removes_only_dead(bus: EventBus) -> None:
    keep = await bus.subscribe("org-1")
    dead = await bus.subscribe("org-1")
    await bus._remove_dead_queues("org-1", [dead])
    assert bus._subscribers["org-1"] == [keep]


async def test_remove_dead_queues_deletes_empty_org(bus: EventBus) -> None:
    q = await bus.subscribe("org-1")
    await bus._remove_dead_queues("org-1", [q])
    assert "org-1" not in bus._subscribers


async def test_remove_dead_queues_unknown_org_is_noop(bus: EventBus) -> None:
    await bus._remove_dead_queues("nope", [asyncio.Queue()])


async def test_remove_dead_queues_ignores_queue_not_present(bus: EventBus) -> None:
    await bus.subscribe("org-1")
    await bus._remove_dead_queues("org-1", [asyncio.Queue()])
    assert len(bus._subscribers["org-1"]) == 1


# ---------------------------------------------------------------------------
# Redis broadcast
# ---------------------------------------------------------------------------


async def test_redis_broadcast_publishes_to_resource_channel() -> None:
    broker = AsyncMock()
    await EventBus(redis_broker=broker)._redis_broadcast(broker, "org-1", {"type": "run"})
    broker.publish.assert_awaited_once_with("resource:org-1", {"type": "run"})


async def test_redis_broadcast_logs_failure(caplog: pytest.LogCaptureFixture) -> None:
    broker = AsyncMock()
    broker.publish.side_effect = ConnectionError("redis down")
    await EventBus(redis_broker=broker)._redis_broadcast(broker, "org-1", {"type": "run"})
    assert "event_bus.redis_broadcast_failed" in caplog.text


async def test_redis_broadcast_propagates_cancellation() -> None:
    broker = AsyncMock()
    broker.publish.side_effect = asyncio.CancelledError()
    with pytest.raises(asyncio.CancelledError):
        await EventBus(redis_broker=broker)._redis_broadcast(broker, "org-1", {"type": "run"})


async def test_redis_broadcast_if_configured_noop_without_broker(bus: EventBus) -> None:
    bus._redis_broadcast_if_configured("org-1", {"type": "run"})
    assert not eb._background_tasks


async def test_publish_broadcasts_to_redis_when_configured() -> None:
    broker = AsyncMock()
    bus = EventBus(redis_broker=broker)
    await bus.subscribe("org-1")

    await bus.publish("org-1", "run", "run-1", "created", 3)
    await _drain_tasks()

    broker.publish.assert_awaited_once()
    assert broker.publish.await_args.args[0] == "resource:org-1"
    assert broker.publish.await_args.args[1] == _event(rid="run-1", version=3)
    assert not eb._background_tasks


async def test_publish_background_broadcast_is_discarded_after_completion() -> None:
    broker = AsyncMock()
    bus = EventBus(redis_broker=broker)

    await bus.publish("org-1", "run", "run-1", "created", 1)
    await _drain_tasks()

    assert not eb._background_tasks


# ---------------------------------------------------------------------------
# subscribe
# ---------------------------------------------------------------------------


async def test_subscribe_creates_queue_with_default_maxsize(bus: EventBus) -> None:
    q = await bus.subscribe("org-1")
    assert q.maxsize == 256


async def test_subscribe_honors_custom_maxsize(bus: EventBus) -> None:
    q = await bus.subscribe("org-1", maxsize=8)
    assert q.maxsize == 8


async def test_subscribe_appends_to_existing_org(bus: EventBus) -> None:
    q1 = await bus.subscribe("org-1")
    q2 = await bus.subscribe("org-1")
    assert bus._subscribers["org-1"] == [q1, q2]


async def test_late_subscriber_does_not_receive_past_events(bus: EventBus) -> None:
    await bus.publish("org-1", "run", "run-1", "created", 1)
    q = await bus.subscribe("org-1")
    assert q.empty()


async def test_multiple_events_are_delivered_in_order(bus: EventBus) -> None:
    q = await bus.subscribe("org-1")
    await bus.publish("org-1", "run", "r1", "created", 1)
    await bus.publish("org-1", "run", "r2", "updated", 2)
    await bus.publish("org-1", "run", "r3", "deleted", 3)

    assert [e["id"] for e in _drain(q)] == ["r1", "r2", "r3"]


# ---------------------------------------------------------------------------
# unsubscribe
# ---------------------------------------------------------------------------


async def test_unsubscribe_removes_queue_and_org(bus: EventBus) -> None:
    q = await bus.subscribe("org-1")
    await bus.unsubscribe("org-1", q)
    assert "org-1" not in bus._subscribers


async def test_unsubscribe_keeps_remaining_queues(bus: EventBus) -> None:
    q1 = await bus.subscribe("org-1")
    q2 = await bus.subscribe("org-1")
    await bus.unsubscribe("org-1", q1)
    assert bus._subscribers["org-1"] == [q2]


async def test_unsubscribe_unknown_org_is_noop(bus: EventBus) -> None:
    await bus.unsubscribe("nope", asyncio.Queue())


async def test_unsubscribe_unknown_queue_is_noop(bus: EventBus) -> None:
    q = await bus.subscribe("org-1")
    await bus.unsubscribe("org-1", asyncio.Queue())
    assert bus._subscribers["org-1"] == [q]


# ---------------------------------------------------------------------------
# Module singleton
# ---------------------------------------------------------------------------


def test_get_event_bus_returns_singleton() -> None:
    first = eb.get_event_bus()
    second = eb.get_event_bus()
    assert first is second
    assert isinstance(first, EventBus)


def test_get_event_bus_lazily_creates_bus_without_broker() -> None:
    assert eb._event_bus is None
    bus = eb.get_event_bus()
    assert bus._redis_broker is None


def test_set_event_bus_overrides_singleton() -> None:
    bus = EventBus()
    eb._set_event_bus(bus)
    assert eb.get_event_bus() is bus


# ---------------------------------------------------------------------------
# configure_event_bus
# ---------------------------------------------------------------------------


async def test_configure_event_bus_sets_broker_on_new_bus() -> None:
    broker = MagicMock()
    await eb.configure_event_bus(redis_broker=broker)
    assert eb._event_bus is not None
    assert eb._event_bus._redis_broker is broker


async def test_configure_event_bus_swaps_broker_and_closes_old() -> None:
    old = MagicMock()
    old.close = AsyncMock()
    await eb.configure_event_bus(redis_broker=old)
    new = MagicMock()
    new.close = AsyncMock()

    await eb.configure_event_bus(redis_broker=new)

    old.close.assert_awaited_once()
    new.close.assert_not_awaited()
    assert eb._event_bus is not None
    assert eb._event_bus._redis_broker is new


async def test_configure_event_bus_reuses_same_broker_without_close() -> None:
    broker = MagicMock()
    broker.close = AsyncMock()
    await eb.configure_event_bus(redis_broker=broker)
    await eb.configure_event_bus(redis_broker=broker)
    broker.close.assert_not_awaited()


async def test_configure_event_bus_clears_broker_and_closes_old() -> None:
    old = MagicMock()
    old.close = AsyncMock()
    await eb.configure_event_bus(redis_broker=old)

    await eb.configure_event_bus(redis_broker=None)

    old.close.assert_awaited_once()
    assert eb._event_bus is not None
    assert eb._event_bus._redis_broker is None


async def test_configure_event_bus_logs_close_failure(caplog: pytest.LogCaptureFixture) -> None:
    old = MagicMock()
    old.close = AsyncMock(side_effect=RuntimeError("close failed"))
    await eb.configure_event_bus(redis_broker=old)

    await eb.configure_event_bus(redis_broker=MagicMock())

    assert "event_bus.close_old_broker_failed" in caplog.text


async def test_configure_event_bus_propagates_cancellation_from_close() -> None:
    old = MagicMock()
    old.close = AsyncMock(side_effect=asyncio.CancelledError())
    await eb.configure_event_bus(redis_broker=old)

    with pytest.raises(asyncio.CancelledError):
        await eb.configure_event_bus(redis_broker=MagicMock())
