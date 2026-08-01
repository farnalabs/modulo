"""Unit tests for EventBus — in-memory pub/sub with async queues."""

from __future__ import annotations

import asyncio
from collections.abc import Iterator
from unittest.mock import AsyncMock, MagicMock

import pytest

from modulo.core.events.event_bus import EventBus, configure_event_bus, get_event_bus


@pytest.fixture(autouse=True)
def _reset_singleton() -> Iterator[None]:
    """Reset the module-level singleton before each test."""
    import modulo.core.events.event_bus as eb

    eb._event_bus = None
    eb._background_tasks = set()
    yield
    eb._event_bus = None
    eb._background_tasks = set()


class TestEventBus:
    async def test_subscribe_and_receive(self) -> None:
        bus = EventBus()
        org_id = "org-123"
        queue = await bus.subscribe(org_id)

        await bus.publish(org_id, "run", "run-1", "created", version=0)

        event = await asyncio.wait_for(queue.get(), timeout=1.0)
        assert event["type"] == "run"
        assert event["id"] == "run-1"
        assert event["action"] == "created"
        assert event["org_id"] == org_id

    async def test_multiple_subscribers_all_receive(self) -> None:
        bus = EventBus()
        org_id = "org-123"
        q1 = await bus.subscribe(org_id)
        q2 = await bus.subscribe(org_id)

        await bus.publish(org_id, "pipeline", "pipe-1", "updated", version=1)

        event1 = await asyncio.wait_for(q1.get(), timeout=1.0)
        event2 = await asyncio.wait_for(q2.get(), timeout=1.0)
        assert event1["id"] == "pipe-1"
        assert event2["id"] == "pipe-1"

    async def test_unsubscribe_removes_from_fan_out(self) -> None:
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

    async def test_org_isolation(self) -> None:
        bus = EventBus()
        q_a = await bus.subscribe("org-a")
        q_b = await bus.subscribe("org-b")

        await bus.publish("org-a", "run", "run-a", "created", version=0)

        with pytest.raises(asyncio.TimeoutError):
            await asyncio.wait_for(q_b.get(), timeout=0.2)
        event_a = await asyncio.wait_for(q_a.get(), timeout=1.0)
        assert event_a["org_id"] == "org-a"

    async def test_slow_consumer_removed(self) -> None:
        bus = EventBus()
        org_id = "org-123"
        q = await bus.subscribe(org_id)

        # Fill the queue to its maxsize (default 0 = infinite).
        # Create a queue with maxsize=1 for slow-consumer test.
        bus._subscribers[org_id].remove(q)
        limited_q: asyncio.Queue[dict[str, object]] = asyncio.Queue(maxsize=1)
        bus._subscribers[org_id].append(limited_q)

        await bus.publish(org_id, "run", "r1", "created", version=0)
        await bus.publish(org_id, "run", "r2", "updated", version=1)

        assert limited_q not in bus._subscribers.get(org_id, [])

    async def test_singleton_get_event_bus(self) -> None:
        bus1 = get_event_bus()
        bus2 = get_event_bus()
        assert bus1 is bus2

    async def test_configure_event_bus_sets_redis(self) -> None:
        mock_redis = MagicMock(spec=["publish"])
        mock_redis.publish = AsyncMock()

        await configure_event_bus(redis_broker=mock_redis)
        bus = get_event_bus()
        assert bus._redis_broker is mock_redis

    async def test_configure_event_bus_lazy_init(self) -> None:
        import modulo.core.events.event_bus as eb

        eb._event_bus = None
        mock_redis = MagicMock(spec=["publish"])
        mock_redis.publish = AsyncMock()

        await configure_event_bus(redis_broker=mock_redis)
        bus = get_event_bus()
        assert bus._redis_broker is mock_redis

    async def test_publish_to_multiple_orgs(self) -> None:
        bus = EventBus()
        q_a = await bus.subscribe("org-a")
        q_b = await bus.subscribe("org-b")

        await bus.publish("org-a", "schema", "s1", "created", version=0)
        await bus.publish("org-b", "team", "t1", "updated", version=0)

        event_a = await asyncio.wait_for(q_a.get(), timeout=1.0)
        event_b = await asyncio.wait_for(q_b.get(), timeout=1.0)
        assert event_a["type"] == "schema"
        assert event_b["type"] == "team"

    async def test_no_subscribers_does_not_raise(self) -> None:
        bus = EventBus()
        await bus.publish("org-empty", "run", "r1", "created", version=0)
        # publishing to an org with no subscribers must not register a queue
        assert bus._subscribers.get("org-empty") is None

    async def test_late_subscriber_does_not_receive_past_events(self) -> None:
        bus = EventBus()
        await bus.publish("org-123", "run", "run-1", "created", version=0)
        q = await bus.subscribe("org-123")
        with pytest.raises(asyncio.TimeoutError):
            await asyncio.wait_for(q.get(), timeout=0.2)

    async def test_publish_with_all_event_fields(self) -> None:
        bus = EventBus()
        org_id = "org-123"
        q = await bus.subscribe(org_id)
        await bus.publish(org_id, "pipeline", "pipe-1", "updated", version=2)
        event = await asyncio.wait_for(q.get(), timeout=1.0)
        assert event["type"] == "pipeline"
        assert event["id"] == "pipe-1"
        assert event["action"] == "updated"
        assert event["version"] == 2

    async def test_multiple_events_in_order(self) -> None:
        bus = EventBus()
        org_id = "org-123"
        q = await bus.subscribe(org_id)
        await bus.publish(org_id, "run", "r1", "created", version=0)
        await bus.publish(org_id, "run", "r2", "updated", version=1)
        await bus.publish(org_id, "run", "r3", "deleted", version=2)
        for expected_id in ("r1", "r2", "r3"):
            event = await asyncio.wait_for(q.get(), timeout=1.0)
            assert event["id"] == expected_id

    async def test_redis_broker_publish_path(self) -> None:
        mock_redis = MagicMock(spec=["publish"])
        mock_redis.publish = AsyncMock()
        await configure_event_bus(redis_broker=mock_redis)
        bus = get_event_bus()

        await bus.publish("org-123", "run", "r1", "created", version=0)
        await asyncio.sleep(0.01)
        mock_redis.publish.assert_awaited_once_with(
            "resource:org-123",
            {"type": "run", "id": "r1", "action": "created", "version": 0, "org_id": "org-123"},
        )

    async def test_remove_dead_queues_with_missing_org_is_noop(self) -> None:
        bus = EventBus()
        await bus._remove_dead_queues("missing-org", [asyncio.Queue[dict[str, object]](maxsize=0)])
        assert bus._subscribers == {}

    async def test_slow_consumer_removed_but_org_kept_for_others(self) -> None:
        bus = EventBus()
        org_id = "org-123"
        healthy_q = await bus.subscribe(org_id)
        limited_q: asyncio.Queue[dict[str, object]] = asyncio.Queue(maxsize=1)
        bus._subscribers[org_id].append(limited_q)

        await bus.publish(org_id, "run", "r1", "created", version=0)
        await bus.publish(org_id, "run", "r2", "updated", version=1)

        assert limited_q not in bus._subscribers[org_id]
        assert healthy_q in bus._subscribers[org_id]
        event = await asyncio.wait_for(healthy_q.get(), timeout=1.0)
        assert event["id"] == "r1"

    async def test_redis_broadcast_failure_is_swallowed(self, caplog: pytest.LogCaptureFixture) -> None:
        mock_redis = MagicMock(spec=["publish"])

        async def failing_publish(*_args: object, **_kwargs: object) -> None:
            raise ConnectionError("redis down")

        mock_redis.publish = failing_publish
        await configure_event_bus(redis_broker=mock_redis)
        bus = get_event_bus()

        await bus.publish("org-123", "run", "r1", "created", version=0)
        await asyncio.sleep(0.01)
        assert "event_bus.redis_broadcast_failed" in caplog.text

    async def test_redis_broadcast_cancellation_propagates(self) -> None:
        import modulo.core.events.event_bus as eb

        mock_redis = MagicMock(spec=["publish"])

        async def blocking_publish(*_args: object, **_kwargs: object) -> None:
            await asyncio.Event().wait()

        mock_redis.publish = blocking_publish
        await configure_event_bus(redis_broker=mock_redis)
        bus = get_event_bus()

        await bus.publish("org-123", "run", "r1", "created", version=0)
        await asyncio.sleep(0.01)
        assert eb._background_tasks, "broadcast task should be pending"
        task = next(iter(eb._background_tasks))
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert not eb._background_tasks

    async def test_unsubscribe_unknown_org_is_noop(self) -> None:
        bus = EventBus()
        await bus.unsubscribe("missing-org", asyncio.Queue[dict[str, object]](maxsize=0))
        assert bus._subscribers == {}

    async def test_unsubscribe_unknown_queue_is_noop(self) -> None:
        bus = EventBus()
        q = await bus.subscribe("org-1")
        await bus.unsubscribe("org-1", asyncio.Queue[dict[str, object]](maxsize=0))
        assert q in bus._subscribers["org-1"]

    async def test_unsubscribe_last_queue_removes_org_key(self) -> None:
        bus = EventBus()
        q = await bus.subscribe("org-1")
        await bus.unsubscribe("org-1", q)
        assert "org-1" not in bus._subscribers

    async def test_configure_event_bus_closes_old_broker(self) -> None:
        old_broker = MagicMock(spec=["publish"])
        old_broker.publish = AsyncMock()
        old_broker.close = AsyncMock()
        new_broker = MagicMock(spec=["publish"])
        new_broker.publish = AsyncMock()
        new_broker.close = AsyncMock()

        await configure_event_bus(redis_broker=old_broker)
        await configure_event_bus(redis_broker=new_broker)

        bus = get_event_bus()
        assert bus._redis_broker is new_broker
        old_broker.close.assert_awaited_once()

    async def test_configure_event_bus_does_not_close_same_broker(self) -> None:
        broker = MagicMock(spec=["publish"])
        broker.publish = AsyncMock()
        broker.close = AsyncMock()

        await configure_event_bus(redis_broker=broker)
        await configure_event_bus(redis_broker=broker)

        assert get_event_bus()._redis_broker is broker
        broker.close.assert_not_called()

    async def test_configure_event_bus_logs_warning_when_old_broker_close_fails(
        self,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        old_broker = MagicMock(spec=["publish"])
        old_broker.publish = AsyncMock()
        old_broker.close = AsyncMock(side_effect=RuntimeError("close failed"))
        new_broker = MagicMock(spec=["publish"])
        new_broker.publish = AsyncMock()
        new_broker.close = AsyncMock()

        await configure_event_bus(redis_broker=old_broker)
        await configure_event_bus(redis_broker=new_broker)

        assert get_event_bus()._redis_broker is new_broker
        assert "event_bus.close_old_broker_failed" in caplog.text

    async def test_configure_event_bus_reraises_cancellation_when_closing_old_broker(self) -> None:
        old_broker = MagicMock(spec=["publish"])
        old_broker.publish = AsyncMock()
        started = asyncio.Event()

        async def blocking_close(*_args: object, **_kwargs: object) -> None:
            started.set()
            await asyncio.Event().wait()

        old_broker.close = blocking_close
        new_broker = MagicMock(spec=["publish"])
        new_broker.publish = AsyncMock()
        new_broker.close = AsyncMock()

        await configure_event_bus(redis_broker=old_broker)
        task = asyncio.create_task(configure_event_bus(redis_broker=new_broker))
        await started.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

        assert get_event_bus()._redis_broker is new_broker
