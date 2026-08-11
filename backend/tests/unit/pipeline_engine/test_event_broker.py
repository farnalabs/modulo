"""Unit tests for RunEventBroker and BrokerRegistry."""

import asyncio
import logging
import time
import uuid

import pytest

from modulo.core.pipeline_engine.event_broker import (
    _RING_BUFFER_SIZE,
    BrokerRegistry,
    RunEvent,
    RunEventBroker,
    _log_redis_error,
    configure_registry,
    get_registry,
)

# ---------------------------------------------------------------------------
# RunEventBroker — basic publish / subscribe
# ---------------------------------------------------------------------------


def test_publish_increments_seq():
    broker = RunEventBroker(uuid.uuid4())
    e1 = broker.publish("node_started", {"node_id": "a"})
    e2 = broker.publish("node_completed", {"node_id": "a"})
    assert e1.seq == 1
    assert e2.seq == 2


def test_published_event_in_buffer():
    broker = RunEventBroker(uuid.uuid4())
    event = broker.publish("node_started", {"node_id": "a"})
    assert event in list(broker._buffer)


def test_publish_to_closed_broker_raises():
    broker = RunEventBroker(uuid.uuid4())
    broker.close()
    with pytest.raises(RuntimeError, match="closed"):
        broker.publish("node_started", {})


async def test_subscribe_receives_published_events():
    broker = RunEventBroker(uuid.uuid4())
    q = broker.subscribe()
    event = broker.publish("node_started", {"node_id": "a"})
    received = await asyncio.wait_for(q.get(), timeout=1.0)
    assert received is event


def test_subscribe_to_closed_broker_raises():
    broker = RunEventBroker(uuid.uuid4())
    broker.close()
    with pytest.raises(RuntimeError, match="closed"):
        broker.subscribe()


async def test_multiple_subscribers_all_receive_event():
    broker = RunEventBroker(uuid.uuid4())
    q1 = broker.subscribe()
    q2 = broker.subscribe()
    broker.publish("run_completed", {})
    r1 = await asyncio.wait_for(q1.get(), timeout=1.0)
    r2 = await asyncio.wait_for(q2.get(), timeout=1.0)
    assert r1.seq == r2.seq == 1


def test_unsubscribe_removes_queue():
    broker = RunEventBroker(uuid.uuid4())
    q = broker.subscribe()
    assert broker.subscriber_count == 1
    broker.unsubscribe(q)
    assert broker.subscriber_count == 0


async def test_close_sends_none_sentinel_to_subscribers():
    broker = RunEventBroker(uuid.uuid4())
    q = broker.subscribe()
    broker.close()
    sentinel = await asyncio.wait_for(q.get(), timeout=1.0)
    assert sentinel is None


def test_close_clears_subscribers():
    broker = RunEventBroker(uuid.uuid4())
    broker.subscribe()
    broker.subscribe()
    broker.close()
    assert broker.subscriber_count == 0
    assert broker.is_closed


# ---------------------------------------------------------------------------
# Ring buffer
# ---------------------------------------------------------------------------


def test_ring_buffer_capped_at_max_size():
    broker = RunEventBroker(uuid.uuid4())
    for i in range(_RING_BUFFER_SIZE + 10):
        broker.publish("node_started", {"i": i})
    assert broker.buffered_count == _RING_BUFFER_SIZE


def test_ring_buffer_drops_oldest_when_full():
    broker = RunEventBroker(uuid.uuid4())
    for i in range(_RING_BUFFER_SIZE + 1):
        broker.publish("x", {"i": i})
    # Oldest seq is 2 (seq=1 was evicted); newest is _RING_BUFFER_SIZE + 1
    seqs = [e.seq for e in broker._buffer]
    assert 1 not in seqs
    assert _RING_BUFFER_SIZE + 1 in seqs


# ---------------------------------------------------------------------------
# replay_since
# ---------------------------------------------------------------------------


def test_replay_since_returns_events_after_seq():
    broker = RunEventBroker(uuid.uuid4())
    broker.publish("node_started", {"node_id": "a"})  # seq=1
    broker.publish("node_completed", {"node_id": "a"})  # seq=2
    broker.publish("run_completed", {})  # seq=3
    replayed = broker.replay_since(1)
    seqs = [e.seq for e in replayed]
    assert seqs == [2, 3]


def test_replay_since_zero_returns_all():
    broker = RunEventBroker(uuid.uuid4())
    broker.publish("node_started", {})
    broker.publish("node_completed", {})
    assert len(broker.replay_since(0)) == 2


def test_replay_since_returns_empty_when_up_to_date():
    broker = RunEventBroker(uuid.uuid4())
    broker.publish("node_started", {})
    assert broker.replay_since(1) == []


def test_replay_since_empty_buffer_returns_empty():
    broker = RunEventBroker(uuid.uuid4())
    assert broker.replay_since(0) == []
    assert broker.replay_since(5) == []


def test_replay_since_older_than_oldest_buffered_returns_empty():
    broker = RunEventBroker(uuid.uuid4())
    # Fill the ring buffer so seq=1..2 are evicted; the oldest buffered seq
    # is now > 1. replay_since must treat the request as "older than we can
    # replay" and return [] rather than a partial/incorrect replay.
    for _ in range(_RING_BUFFER_SIZE + 2):
        broker.publish("x", {})
    assert broker._buffer[0].seq > 1
    assert broker.replay_since(1) == []


# ---------------------------------------------------------------------------
# RunEvent.to_json
# ---------------------------------------------------------------------------


def test_run_event_to_json_shape():
    run_id = uuid.uuid4()
    event = RunEvent(seq=5, event_type="node_started", run_id=run_id, payload={"node_id": "x"})
    j = event.to_json()
    assert j["seq"] == 5
    assert j["type"] == "node_started"
    assert j["run_id"] == str(run_id)
    assert j["payload"] == {"node_id": "x"}
    assert "timestamp" in j


# ---------------------------------------------------------------------------
# Redis broker attachment (fire-and-forget cross-worker broadcast)
# ---------------------------------------------------------------------------


class _FakeRedisBroker:
    """Stand-in for RedisEventBroker that records fire-and-forget calls."""

    def __init__(self) -> None:
        self.published: list[tuple[str, dict]] = []
        self.close_count = 0

    async def publish(self, channel: str, data: dict) -> None:
        self.published.append((channel, data))

    async def close(self) -> None:
        self.close_count += 1


async def test_publish_with_redis_broker_broadcasts_event():
    redis = _FakeRedisBroker()
    broker = RunEventBroker(uuid.uuid4(), redis_broker=redis)
    event = broker.publish("node_started", {"node_id": "a"})
    await asyncio.sleep(0)  # let the fire-and-forget task run
    assert redis.published == [(str(broker.run_id), event.to_json())]


async def test_close_with_redis_broker_closes_redis():
    redis = _FakeRedisBroker()
    broker = RunEventBroker(uuid.uuid4(), redis_broker=redis)
    broker.close()
    await asyncio.sleep(0)
    assert redis.close_count == 1


async def test_redis_publish_failure_is_logged(caplog: pytest.LogCaptureFixture) -> None:
    class _FailingRedisBroker:
        async def publish(self, channel: str, data: dict) -> None:
            raise RuntimeError("redis down")

        async def close(self) -> None:
            return None

    with caplog.at_level(logging.WARNING, logger="modulo.core.pipeline_engine.event_broker"):
        broker = RunEventBroker(uuid.uuid4(), redis_broker=_FailingRedisBroker())
        broker.publish("node_started", {"node_id": "a"})
        for _ in range(5):  # let the failing task complete and be observed
            await asyncio.sleep(0)
    assert any("redis.publish_failed" in record.message for record in caplog.records)


def test_publish_with_redis_broker_without_running_loop_skips_broadcast(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Sync publish with a Redis broker must not raise RuntimeError when no
    event loop is running — the fire-and-forget broadcast fails open with a log."""
    redis = _FakeRedisBroker()
    broker = RunEventBroker(uuid.uuid4(), redis_broker=redis)
    with caplog.at_level(logging.WARNING, logger="modulo.core.pipeline_engine.event_broker"):
        event = broker.publish("node_started", {"node_id": "a"})
    assert event.seq == 1
    assert redis.published == []
    assert any("redis_broadcast_skipped" in record.message for record in caplog.records)


def test_close_with_redis_broker_without_running_loop_skips_close(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Sync close with a Redis broker must not raise RuntimeError when no
    event loop is running."""
    redis = _FakeRedisBroker()
    broker = RunEventBroker(uuid.uuid4(), redis_broker=redis)
    with caplog.at_level(logging.WARNING, logger="modulo.core.pipeline_engine.event_broker"):
        broker.close()
    assert redis.close_count == 0
    assert any("redis_close_skipped" in record.message for record in caplog.records)


async def test_log_redis_error_noop_on_successful_task(caplog: pytest.LogCaptureFixture) -> None:
    async def _ok() -> int:
        return 1

    task = asyncio.create_task(_ok())
    await task
    _log_redis_error(task)
    assert not any("redis.publish_failed" in record.message for record in caplog.records)


async def test_log_redis_error_tolerates_cancelled_task() -> None:
    task = asyncio.create_task(asyncio.sleep(10))
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    # task.exception() raises CancelledError for a cancelled task — the
    # done-callback helper must swallow it instead of propagating.
    _log_redis_error(task)


# ---------------------------------------------------------------------------
# BrokerRegistry
# ---------------------------------------------------------------------------


def test_cleanup_stale_removes_old_open_brokers():
    registry = BrokerRegistry()
    rid = uuid.uuid4()
    registry.get_or_create(rid)
    stale = registry.get(rid)
    assert stale is not None
    stale._created_at = time.monotonic() - 10_000
    registry.cleanup_stale(max_age_seconds=60)
    assert registry.get(rid) is None
    assert registry.active_run_count == 0


def test_cleanup_stale_keeps_fresh_brokers():
    registry = BrokerRegistry()
    rid = uuid.uuid4()
    registry.get_or_create(rid)
    registry.cleanup_stale(max_age_seconds=60)
    assert registry.get(rid) is not None
    assert registry.active_run_count == 1


def test_cleanup_stale_does_not_sweep_closed_brokers():
    # Contract test: cleanup_stale only sweeps open-and-stale brokers. A closed
    # broker that was never registry.close()'d is left in place (docstring says
    # "closed OR older than max_age" but the sweep excludes closed brokers).
    registry = BrokerRegistry()
    rid = uuid.uuid4()
    broker = registry.get_or_create(rid)
    broker._created_at = time.monotonic() - 10_000
    broker.close()
    registry.cleanup_stale(max_age_seconds=60)
    assert registry.get(rid) is not None


def test_configure_registry_sets_redis_broker():
    redis = _FakeRedisBroker()
    try:
        configure_registry(redis)
        assert get_registry()._redis_broker is redis
    finally:
        configure_registry(None)


def test_registry_get_or_create_returns_same_instance():
    registry = BrokerRegistry()
    rid = uuid.uuid4()
    b1 = registry.get_or_create(rid)
    b2 = registry.get_or_create(rid)
    assert b1 is b2


def test_registry_get_returns_none_for_unknown():
    registry = BrokerRegistry()
    assert registry.get(uuid.uuid4()) is None


def test_registry_close_closes_and_removes_broker():
    registry = BrokerRegistry()
    rid = uuid.uuid4()
    broker = registry.get_or_create(rid)
    registry.close(rid)
    assert broker.is_closed
    assert registry.get(rid) is None
    assert registry.active_run_count == 0


def test_registry_active_run_count():
    registry = BrokerRegistry()
    registry.get_or_create(uuid.uuid4())
    registry.get_or_create(uuid.uuid4())
    assert registry.active_run_count == 2


def test_registry_close_unknown_run_is_noop():
    registry = BrokerRegistry()
    run_id = uuid.uuid4()
    registry.get_or_create(run_id)
    registry.close(uuid.uuid4())  # must not raise
    assert registry.active_run_count == 1
    assert registry.get(run_id) is not None


# ---------------------------------------------------------------------------
# Executor event mapping (_map_lg_event)
# ---------------------------------------------------------------------------


def test_map_lg_event_node_started():
    from modulo.core.pipeline_engine.executor import _map_lg_event

    result = _map_lg_event(
        {"event": "on_chain_start", "name": "node-a"},
        {"node-a", "node-b"},
    )
    assert result == ("node_started", {"node_id": "node-a"})


def test_map_lg_event_node_completed():
    from modulo.core.pipeline_engine.executor import _map_lg_event

    result = _map_lg_event(
        {"event": "on_chain_end", "name": "node-a"},
        {"node-a"},
    )
    assert result == ("node_completed", {"node_id": "node-a"})


def test_map_lg_event_node_failed():
    from modulo.core.pipeline_engine.executor import _map_lg_event

    result = _map_lg_event(
        {"event": "on_chain_error", "name": "node-a", "data": {"error": "timeout"}},
        {"node-a"},
    )
    assert result == ("node_failed", {"node_id": "node-a", "error": "timeout"})


def test_map_lg_event_non_node_name_returns_none():
    from modulo.core.pipeline_engine.executor import _map_lg_event

    result = _map_lg_event(
        {"event": "on_chain_start", "name": "LangGraph"},  # graph-level event
        {"node-a"},
    )
    assert result is None


def test_map_lg_event_unknown_event_kind_returns_none():
    from modulo.core.pipeline_engine.executor import _map_lg_event

    result = _map_lg_event(
        {"event": "on_llm_start", "name": "node-a"},
        {"node-a"},
    )
    assert result is None
