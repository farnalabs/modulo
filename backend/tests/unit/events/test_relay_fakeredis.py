"""FAR-250 relay integration over fakeredis — real RedisEventBroker pub/sub.

Exercises the actual broker JSON encode/prefix path against an in-process
Redis double (no server). Per the ticket contract:

* the relay subscriber is registered (and its subscribe confirmation drained)
  BEFORE anything is published — no lost-message race;
* every positive drain uses ``asyncio.wait_for(..., 5)``;
* teardown is strict (pubsub closed, broker clients closed — redis.asyncio
  pools are loop-affine);
* own-id payloads are NOT forwarded, other-id payloads ARE, and a
  web-originated ``bus.publish`` reaches the local SSE queue exactly once.
"""

from __future__ import annotations

import asyncio
import uuid
from typing import Any

import pytest
from fakeredis import FakeAsyncRedis, FakeServer

from modulo.core.events import event_bus as eb
from modulo.core.events.event_bus import LOCAL_PRODUCER_ID, EventBus
from modulo.core.events.redis_broker import RedisEventBroker
from modulo.core.events.relay import EventRelay, reset_relay_for_tests

_ORG = "org-fakeredis"


@pytest.fixture(autouse=True)
def _reset_bus_state() -> Any:
    eb._event_bus = None
    eb._background_tasks.clear()
    yield
    eb._event_bus = None
    eb._background_tasks.clear()


@pytest.fixture(autouse=True)
async def _reset_relay_singleton() -> Any:
    await reset_relay_for_tests()
    yield
    await reset_relay_for_tests()


@pytest.fixture
def broker(monkeypatch: pytest.MonkeyPatch) -> Any:
    server = FakeServer()

    def _make_client(self: RedisEventBroker) -> FakeAsyncRedis:
        return FakeAsyncRedis(server=server, decode_responses=True)

    monkeypatch.setattr(RedisEventBroker, "_make_client", _make_client)
    return RedisEventBroker("redis://localhost:6379/15")


async def _drain_broadcast_tasks() -> None:
    """Wait for EventBus fire-and-forget Redis broadcast tasks to finish."""
    for _ in range(200):
        if not eb._background_tasks:
            await asyncio.sleep(0)
            return
        await asyncio.sleep(0.01)
    raise AssertionError("redis broadcast tasks did not drain")


async def _establish_relay(bus: EventBus, broker: RedisEventBroker) -> tuple[EventRelay, asyncio.Queue]:
    """Subscribe the SSE queue FIRST, then acquire the relay (subscribe before publish)."""
    q = await bus.subscribe(_ORG)
    relay = EventRelay(broker=broker, bus=bus, has_clients=lambda _org: True)
    await relay.acquire(_ORG)
    # Subscribe confirmation drained -> subscription established before any publish.
    assert await relay.wait_ready(_ORG, max_wait_s=5), "relay subscription was not established"
    return relay, q


def _foreign_event(event_id: str, rid: str) -> dict[str, Any]:
    return {
        "type": "run",
        "id": rid,
        "action": "created",
        "version": 1,
        "org_id": _ORG,
        "producer_id": "web-process-B",
        "event_id": event_id,
    }


async def test_web_originated_publish_reaches_local_sse_exactly_once(broker: RedisEventBroker) -> None:
    """Local fan-out delivers once; the Redis echo of the SAME event is dropped."""
    bus = EventBus(redis_broker=broker)
    relay, q = await _establish_relay(bus, broker)
    try:
        await bus.publish(_ORG, "run", "run-1", "created", 1)
        local = await asyncio.wait_for(q.get(), timeout=5)
        assert local["id"] == "run-1"

        # Let the fire-and-forget Redis broadcast land BEFORE the ordering
        # marker, so the marker arriving proves the echo was processed first.
        await _drain_broadcast_tasks()
        await broker.connect()
        await broker.publish(f"resource:{_ORG}", _foreign_event("marker-1", "r-marker"))
        marker = await asyncio.wait_for(q.get(), timeout=5)
        assert marker["event_id"] == "marker-1"

        # Exactly one copy of the web-originated event across everything
        # received: the local fan-out (first get). The Redis echo was
        # processed (ordering: it landed before the marker) and dropped —
        # a relayed echo would have produced a second "run-1" in the drain.
        drained_ids = [local["id"], marker["id"]]
        while not q.empty():
            drained_ids.append(q.get_nowait()["id"])
        assert drained_ids.count("run-1") == 1
        assert q.empty()
    finally:
        await bus.unsubscribe(_ORG, q)
        await relay.release(_ORG)
        await relay.aclose()
        await broker.close()


async def test_own_id_not_forwarded_other_id_forwarded(broker: RedisEventBroker) -> None:
    """Echo suppression through the real broker: own producer dropped, other forwarded."""
    bus = EventBus(redis_broker=broker)
    relay, q = await _establish_relay(bus, broker)
    try:
        await broker.connect()
        own = {
            "type": "run",
            "id": "r-own",
            "action": "created",
            "version": 1,
            "org_id": _ORG,
            "producer_id": LOCAL_PRODUCER_ID,
            "event_id": "own-1",
        }
        await broker.publish(f"resource:{_ORG}", own)
        # Ordering: the foreign event is published after the own-id one; once
        # it arrives, the own-id payload has already been processed (and dropped).
        await broker.publish(f"resource:{_ORG}", _foreign_event("foreign-1", "r-foreign"))

        got = await asyncio.wait_for(q.get(), timeout=5)
        assert got["event_id"] == "foreign-1"
        assert got["id"] == "r-foreign"
        assert q.empty()  # own-id never forwarded
    finally:
        await bus.unsubscribe(_ORG, q)
        await relay.release(_ORG)
        await relay.aclose()
        await broker.close()


async def test_other_id_forwarded_exactly_once_with_event_id_dedupe(broker: RedisEventBroker) -> None:
    bus = EventBus(redis_broker=broker)
    relay, q = await _establish_relay(bus, broker)
    try:
        await broker.connect()
        foreign = _foreign_event("evt-1", "r-1")
        await broker.publish(f"resource:{_ORG}", foreign)
        got1 = await asyncio.wait_for(q.get(), timeout=5)
        assert got1["id"] == "r-1"

        # Replay of the same event_id is deduped; a NEW one still flows.
        await broker.publish(f"resource:{_ORG}", foreign)
        await broker.publish(f"resource:{_ORG}", _foreign_event("evt-2", "r-2"))
        got2 = await asyncio.wait_for(q.get(), timeout=5)
        assert got2["id"] == "r-2"
        assert q.empty()  # exactly once each
    finally:
        await bus.unsubscribe(_ORG, q)
        await relay.release(_ORG)
        await relay.aclose()
        await broker.close()


async def test_notification_event_payload_through_relay_has_no_content(broker: RedisEventBroker) -> None:
    """A notification event relayed cross-process carries no title/body."""
    bus = EventBus(redis_broker=broker)
    relay, q = await _establish_relay(bus, broker)
    try:
        from modulo.core.events.notification_events import build_notification_event

        event = build_notification_event(
            org_id=_ORG,
            notification_id=uuid.uuid4(),
            category="run_failed",
            created_at=None,
            version=1,
        )
        event["producer_id"] = "web-process-B"
        await broker.connect()
        await broker.publish(f"resource:{_ORG}", event)

        got = await asyncio.wait_for(q.get(), timeout=5)
        assert got["category"] == "run_failed"
        assert got["notification_id"] == got["id"]
        assert "title" not in got
        assert "body" not in got
        assert "content" not in got
    finally:
        await bus.unsubscribe(_ORG, q)
        await relay.release(_ORG)
        await relay.aclose()
        await broker.close()
