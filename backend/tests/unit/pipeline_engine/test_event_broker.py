"""Unit tests for RunEventBroker and BrokerRegistry."""

import asyncio
import uuid

import pytest

from modulo.core.pipeline_engine.event_broker import (
    _RING_BUFFER_SIZE,
    BrokerRegistry,
    RunEvent,
    RunEventBroker,
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
# BrokerRegistry
# ---------------------------------------------------------------------------


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
    registry.close(uuid.uuid4())  # must not raise


# ---------------------------------------------------------------------------
# Executor event mapping (_map_lg_event)
# ---------------------------------------------------------------------------


def test_map_lg_event_node_started():
    from modulo.core.pipeline_engine.executor import _map_lg_event

    run_id = uuid.uuid4()
    result = _map_lg_event(
        {"event": "on_chain_start", "name": "node-a"},
        run_id,
        {"node-a", "node-b"},
    )
    assert result == ("node_started", {"node_id": "node-a"})


def test_map_lg_event_node_completed():
    from modulo.core.pipeline_engine.executor import _map_lg_event

    run_id = uuid.uuid4()
    result = _map_lg_event(
        {"event": "on_chain_end", "name": "node-a"},
        run_id,
        {"node-a"},
    )
    assert result == ("node_completed", {"node_id": "node-a"})


def test_map_lg_event_node_failed():
    from modulo.core.pipeline_engine.executor import _map_lg_event

    run_id = uuid.uuid4()
    result = _map_lg_event(
        {"event": "on_chain_error", "name": "node-a", "data": {"error": "timeout"}},
        run_id,
        {"node-a"},
    )
    assert result == ("node_failed", {"node_id": "node-a", "error": "timeout"})


def test_map_lg_event_non_node_name_returns_none():
    from modulo.core.pipeline_engine.executor import _map_lg_event

    run_id = uuid.uuid4()
    result = _map_lg_event(
        {"event": "on_chain_start", "name": "LangGraph"},  # graph-level event
        run_id,
        {"node-a"},
    )
    assert result is None


def test_map_lg_event_unknown_event_kind_returns_none():
    from modulo.core.pipeline_engine.executor import _map_lg_event

    run_id = uuid.uuid4()
    result = _map_lg_event(
        {"event": "on_llm_start", "name": "node-a"},
        run_id,
        {"node-a"},
    )
    assert result is None
