"""Unit tests for EventBus — in-memory pub/sub with async queues."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

from modulo.core.events.event_bus import EventBus, configure_event_bus, get_event_bus


@pytest.fixture(autouse=True)
def _reset_singleton():
    """Reset the module-level singleton before each test."""
    import modulo.core.events.event_bus as eb

    eb._event_bus = None
    yield
    eb._event_bus = None


class TestEventBus:
    async def test_subscribe_and_receive(self):
        bus = EventBus()
        org_id = "org-123"
        queue = await bus.subscribe(org_id)

        await bus.publish(org_id, "run", "run-1", "created", version=0)

        event = await asyncio.wait_for(queue.get(), timeout=1.0)
        assert event["type"] == "run"
        assert event["id"] == "run-1"
        assert event["action"] == "created"
        assert event["org_id"] == org_id

    async def test_multiple_subscribers_all_receive(self):
        bus = EventBus()
        org_id = "org-123"
        q1 = await bus.subscribe(org_id)
        q2 = await bus.subscribe(org_id)

        await bus.publish(org_id, "pipeline", "pipe-1", "updated", version=1)

        event1 = await asyncio.wait_for(q1.get(), timeout=1.0)
        event2 = await asyncio.wait_for(q2.get(), timeout=1.0)
        assert event1["id"] == "pipe-1"
        assert event2["id"] == "pipe-1"

    async def test_unsubscribe_removes_from_fan_out(self):
        bus = EventBus()
        org_id = "org-123"
        q1 = await bus.subscribe(org_id)
        q2 = await bus.subscribe(org_id)

        await bus.unsubscribe(org_id, q1)
        await bus.publish(org_id, "agent", "agent-1", "deleted", version=0)

        with pytest.raises(asyncio.TimeoutError):
            await asyncio.wait_for(q1.get(), timeout=0.2)
        event2 = await asyncio.wait_for(q2.get(), timeout=1.0)
        assert event2["id"] == "agent-1"

    async def test_org_isolation(self):
        bus = EventBus()
        q_a = await bus.subscribe("org-a")
        q_b = await bus.subscribe("org-b")

        await bus.publish("org-a", "run", "run-a", "created", version=0)

        with pytest.raises(asyncio.TimeoutError):
            await asyncio.wait_for(q_b.get(), timeout=0.2)
        event_a = await asyncio.wait_for(q_a.get(), timeout=1.0)
        assert event_a["org_id"] == "org-a"

    async def test_slow_consumer_removed(self):
        bus = EventBus()
        org_id = "org-123"
        q = await bus.subscribe(org_id)

        # Fill the queue to its maxsize (default 0 = infinite).
        # Create a queue with maxsize=1 for slow-consumer test.
        bus._subscribers[org_id].remove(q)
        limited_q: asyncio.Queue = asyncio.Queue(maxsize=1)
        bus._subscribers[org_id].append(limited_q)

        await bus.publish(org_id, "run", "r1", "created", version=0)
        await bus.publish(org_id, "run", "r2", "updated", version=1)

        assert limited_q not in bus._subscribers.get(org_id, [])

    async def test_singleton_get_event_bus(self):
        bus1 = get_event_bus()
        bus2 = get_event_bus()
        assert bus1 is bus2

    async def test_configure_event_bus_sets_redis(self):
        mock_redis = MagicMock(spec=["publish"])
        mock_redis.publish = AsyncMock()

        configure_event_bus(redis_broker=mock_redis)
        bus = get_event_bus()
        assert bus._redis_broker is mock_redis

    async def test_configure_event_bus_lazy_init(self):
        import modulo.core.events.event_bus as eb

        eb._event_bus = None
        mock_redis = MagicMock(spec=["publish"])
        mock_redis.publish = AsyncMock()

        configure_event_bus(redis_broker=mock_redis)
        bus = get_event_bus()
        assert bus._redis_broker is mock_redis

    async def test_publish_to_multiple_orgs(self):
        bus = EventBus()
        q_a = await bus.subscribe("org-a")
        q_b = await bus.subscribe("org-b")

        await bus.publish("org-a", "schema", "s1", "created", version=0)
        await bus.publish("org-b", "team", "t1", "updated", version=0)

        event_a = await asyncio.wait_for(q_a.get(), timeout=1.0)
        event_b = await asyncio.wait_for(q_b.get(), timeout=1.0)
        assert event_a["type"] == "schema"
        assert event_b["type"] == "team"

    async def test_no_subscribers_does_not_raise(self):
        bus = EventBus()
        await bus.publish("org-empty", "run", "r1", "created", version=0)
