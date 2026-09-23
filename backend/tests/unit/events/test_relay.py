"""Unit tests for the web-process Redis -> EventBus relay pump (FAR-250).

Covers refcount acquire/release, own-producer echo suppression, cross-process
forwarding with event_id dedupe, subscription-blip backfill, and the idle-TTL
fallback sweeper — with a scripted fake broker (no Redis server; the
fakeredis integration lives in test_relay_fakeredis.py).
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

import pytest

from modulo.core.events import event_bus as eb
from modulo.core.events import relay as relay_mod
from modulo.core.events.event_bus import LOCAL_PRODUCER_ID, EventBus
from modulo.core.events.relay import EventRelay

_ORG = "org-relay"


class _FakePubSub:
    """Scriptable stand-in for redis.asyncio PubSub."""

    def __init__(self) -> None:
        self.inbox: asyncio.Queue[Any] = asyncio.Queue()
        self.unsubscribe_calls = 0
        self.closed = False

    async def listen(self):
        while True:
            item = await self.inbox.get()
            if isinstance(item, Exception):
                raise item
            if item is None:
                return
            yield item

    async def unsubscribe(self) -> None:
        self.unsubscribe_calls += 1

    async def aclose(self) -> None:
        self.closed = True


class _FakeBroker:
    def __init__(self) -> None:
        self.pubsubs: list[_FakePubSub] = []
        self.subscribed = asyncio.Event()
        self.channel: str | None = None

    async def connect(self) -> None:
        return None

    async def subscribe(self, channel: str) -> _FakePubSub:
        self.channel = channel
        ps = _FakePubSub()
        self.pubsubs.append(ps)
        self.subscribed.set()
        return ps

    async def publish(self, channel: str, data: dict[str, Any]) -> None:
        return None


async def _establish(relay: EventRelay, broker: _FakeBroker, org: str = _ORG) -> _FakePubSub:
    """Acquire, wait for subscribe, drain the subscription confirmation."""
    await relay.acquire(org)
    await asyncio.wait_for(broker.subscribed.wait(), timeout=5)
    ps = broker.pubsubs[0]
    await ps.inbox.put({"type": "subscribe", "channel": f"resource:{org}"})
    assert await relay.wait_ready(org, max_wait_s=5)
    return ps


async def _wait_pubsub_count(broker: _FakeBroker, count: int) -> None:
    """Poll until the org loop has (re)subscribed *count* times.

    The loop runs backfill BEFORE resubscribing, so once the Nth pubsub
    exists, any backfill for the preceding blip has completed.
    """
    for _ in range(500):
        if len(broker.pubsubs) >= count:
            return
        await asyncio.sleep(0.01)
    raise AssertionError(f"expected {count} subscriptions, got {len(broker.pubsubs)}")


def _message(org: str, *, producer: str, event_id: str, rid: str) -> dict[str, Any]:
    return {
        "type": "run",
        "id": rid,
        "action": "created",
        "version": 1,
        "org_id": org,
        "producer_id": producer,
        "event_id": event_id,
    }


async def _put_event(ps: _FakePubSub, event: dict[str, Any]) -> None:
    """Push *event* into the fake pubsub as a Redis message frame."""
    await ps.inbox.put(
        {
            "type": "message",
            "channel": f"resource:{event['org_id']}",
            "data": json.dumps(event),
        }
    )


@pytest.fixture(autouse=True)
def _reset_singleton() -> Any:
    relay_mod._relay = None
    yield
    relay_mod._relay = None


@pytest.fixture
def bus() -> EventBus:
    return EventBus()


@pytest.fixture
def broker() -> _FakeBroker:
    return _FakeBroker()


def _make_relay(
    broker: _FakeBroker,
    bus: EventBus,
    *,
    has_clients: Any = None,
    backfill_loader: Any = None,
    idle_ttl: float = 60.0,
) -> EventRelay:
    return EventRelay(
        broker=broker,  # type: ignore[arg-type]
        bus=bus,
        has_clients=has_clients or (lambda _org: True),
        backfill_loader=backfill_loader,
        idle_ttl=idle_ttl,
    )


async def test_acquire_subscribes_and_release_stops(bus: EventBus, broker: _FakeBroker) -> None:
    relay = _make_relay(broker, bus)
    try:
        ps = await _establish(relay, broker)
        assert broker.channel == f"resource:{_ORG}"
        await relay.release(_ORG)
        assert _ORG not in relay._org_tasks
        assert ps.closed
        assert ps.unsubscribe_calls >= 1
    finally:
        await relay.aclose()


async def test_refcount_keeps_subscription_until_last_release(bus: EventBus, broker: _FakeBroker) -> None:
    relay = _make_relay(broker, bus)
    try:
        await _establish(relay, broker)
        await relay.acquire(_ORG)  # second client
        await relay.release(_ORG)  # first leaves — still one client
        assert _ORG in relay._org_tasks
        assert len(broker.pubsubs) == 1  # no re-subscribe churn
        await relay.release(_ORG)  # last leaves
        assert _ORG not in relay._org_tasks
    finally:
        await relay.aclose()


async def test_own_producer_echo_is_dropped(bus: EventBus, broker: _FakeBroker) -> None:
    """producer_id == local id must never be re-injected (echo suppression)."""
    relay = _make_relay(broker, bus)
    q = await bus.subscribe(_ORG)
    try:
        ps = await _establish(relay, broker)
        await _put_event(ps, _message(_ORG, producer=LOCAL_PRODUCER_ID, event_id="own-1", rid="r-own"))
        # Ordering marker from a foreign producer: once it arrives, the own
        # echo has already been processed and must NOT be in the queue.
        await _put_event(ps, _message(_ORG, producer="other-producer", event_id="foreign-1", rid="r-foreign"))
        first = await asyncio.wait_for(q.get(), timeout=5)
        assert first["event_id"] == "foreign-1"
        assert q.empty()
    finally:
        await bus.unsubscribe(_ORG, q)
        await relay.aclose()


async def test_other_producer_forwarded_exactly_once_with_dedupe(bus: EventBus, broker: _FakeBroker) -> None:
    relay = _make_relay(broker, bus)
    q = await bus.subscribe(_ORG)
    try:
        ps = await _establish(relay, broker)
        foreign = _message(_ORG, producer="web-b", event_id="evt-1", rid="r-1")
        await _put_event(ps, foreign)
        got1 = await asyncio.wait_for(q.get(), timeout=5)
        assert got1["id"] == "r-1"

        # Duplicate event_id (e.g. replayed after a blip) is deduped...
        await _put_event(ps, foreign)
        # ...and a NEW event still flows (this marker proves the dup was processed).
        await _put_event(ps, _message(_ORG, producer="web-b", event_id="evt-2", rid="r-2"))
        got2 = await asyncio.wait_for(q.get(), timeout=5)
        assert got2["id"] == "r-2"
        assert q.empty()  # exactly once each — no duplicate of r-1
    finally:
        await bus.unsubscribe(_ORG, q)
        await relay.aclose()


async def test_org_mismatch_is_dropped(bus: EventBus, broker: _FakeBroker) -> None:
    relay = _make_relay(broker, bus)
    q = await bus.subscribe(_ORG)
    try:
        ps = await _establish(relay, broker)
        await _put_event(ps, _message("org-other", producer="web-b", event_id="wrong-org", rid="r-x"))
        await _put_event(ps, _message(_ORG, producer="web-b", event_id="ok", rid="r-ok"))
        got = await asyncio.wait_for(q.get(), timeout=5)
        assert got["id"] == "r-ok"
        assert q.empty()
    finally:
        await bus.unsubscribe(_ORG, q)
        await relay.aclose()


async def test_bad_json_payload_is_dropped(bus: EventBus, broker: _FakeBroker) -> None:
    relay = _make_relay(broker, bus)
    q = await bus.subscribe(_ORG)
    try:
        ps = await _establish(relay, broker)
        await ps.inbox.put({"type": "message", "channel": f"resource:{_ORG}", "data": "{not json"})
        await _put_event(ps, _message(_ORG, producer="web-b", event_id="ok", rid="r-ok"))
        got = await asyncio.wait_for(q.get(), timeout=5)
        assert got["id"] == "r-ok"
        assert q.empty()
    finally:
        await bus.unsubscribe(_ORG, q)
        await relay.aclose()


async def test_subscription_blip_backfills_from_db_with_dedupe(
    bus: EventBus,
    broker: _FakeBroker,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """On an established-subscription error: backfill runs, event_id dedupes."""
    monkeypatch.setattr(relay_mod, "_BACKOFF_BASE_SECONDS", 0.01)
    loader_calls: list[str] = []

    async def loader(org: str) -> list[dict[str, Any]]:
        loader_calls.append(org)
        return [
            _message(org, producer="web-b", event_id="backfill-seen", rid="r-seen"),
            _message(org, producer="web-b", event_id="backfill-new", rid="r-new"),
        ]

    relay = _make_relay(broker, bus, backfill_loader=loader)
    q = await bus.subscribe(_ORG)
    try:
        ps = await _establish(relay, broker)
        # Deliver "backfill-seen" live first so the dedupe has something to drop.
        await _put_event(ps, _message(_ORG, producer="web-b", event_id="backfill-seen", rid="r-seen"))
        live = await asyncio.wait_for(q.get(), timeout=5)
        assert live["event_id"] == "backfill-seen"

        # Blip: listen() raises after the subscription was established.
        await ps.inbox.put(RuntimeError("redis connection lost"))
        await _wait_pubsub_count(broker, 2)  # backfill ran before this resubscribe

        drained: list[str] = []
        while not q.empty():
            drained.append(q.get_nowait()["event_id"])
        assert loader_calls == [_ORG]
        assert "backfill-new" in drained
        # The live "backfill-seen" copy was consumed above; the backfill's
        # second copy was dropped by event_id dedupe — nothing re-arrived.
        assert "backfill-seen" not in drained
    finally:
        await bus.unsubscribe(_ORG, q)
        await relay.aclose()


async def test_backfill_loader_failure_is_fail_open(
    bus: EventBus,
    broker: _FakeBroker,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    monkeypatch.setattr(relay_mod, "_BACKOFF_BASE_SECONDS", 0.01)

    async def loader(_org: str) -> list[dict[str, Any]]:
        raise RuntimeError("db down")

    relay = _make_relay(broker, bus, backfill_loader=loader)
    try:
        ps = await _establish(relay, broker)
        await ps.inbox.put(RuntimeError("blip"))
        await _wait_pubsub_count(broker, 2)  # survived the loader failure
        assert "event_relay.backfill_failed" in caplog.text
    finally:
        await relay.aclose()


async def test_no_backfill_loader_skips_backfill(
    bus: EventBus,
    broker: _FakeBroker,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(relay_mod, "_BACKOFF_BASE_SECONDS", 0.01)
    relay = _make_relay(broker, bus, backfill_loader=None)
    try:
        ps = await _establish(relay, broker)
        await ps.inbox.put(RuntimeError("blip"))
        await _wait_pubsub_count(broker, 2)
        # Survived the blip by resubscribing without attempting a backfill.
        assert relay._backfill_loader is None
        assert len(broker.pubsubs) == 2
    finally:
        await relay.aclose()


async def test_idle_ttl_sweeper_stops_stranded_subscription(bus: EventBus, broker: _FakeBroker) -> None:
    """Idle-TTL fallback: a subscription whose org has no SSE clients is stopped.

    Simulates a missed ``release``: the SSE route's tracked connections are
    gone but the org task (and its Redis subscription) is still alive with
    its refcount still >= 1.
    """
    clients = {"present": True}
    relay = _make_relay(
        broker,
        bus,
        has_clients=lambda _org: clients["present"],
        idle_ttl=0.1,
    )
    try:
        ps = await _establish(relay, broker)
        assert _ORG in relay._org_tasks
        assert relay._refcounts.get(_ORG, 0) >= 1
        clients["present"] = False  # all SSE clients vanished without release
        # The refcount stays >= 1 (the missed release) — the sweeper must stop
        # the subscription on the clients signal alone.
        for _ in range(200):
            if _ORG not in relay._org_tasks:
                break
            await asyncio.sleep(0.02)
        assert _ORG not in relay._org_tasks
        assert relay._refcounts.get(_ORG) is None
        assert ps.closed
    finally:
        await relay.aclose()


async def test_release_without_acquire_is_noop(bus: EventBus, broker: _FakeBroker) -> None:
    relay = _make_relay(broker, bus)
    try:
        await relay.release(_ORG)  # must not raise
        assert _ORG not in relay._org_tasks
    finally:
        await relay.aclose()


async def test_configure_relay_reuses_same_broker(bus: EventBus) -> None:
    broker = _FakeBroker()
    first = await relay_mod.configure_relay(
        broker=broker,  # type: ignore[arg-type]
        bus=bus,
        has_clients=lambda _org: False,
    )
    second = await relay_mod.configure_relay(
        broker=broker,  # type: ignore[arg-type]
        bus=bus,
        has_clients=lambda _org: False,
    )
    assert first is second
    assert relay_mod.get_active_relay() is first
    await relay_mod.reset_relay_for_tests()
    assert relay_mod.get_active_relay() is None


async def test_relayed_event_does_not_rebroadcast_to_redis(bus: EventBus) -> None:
    """Relay forwards via deliver_local only — no Redis re-broadcast loop."""
    broker = _FakeBroker()
    bus_with_broker = EventBus(redis_broker=broker)  # type: ignore[arg-type]
    relay = _make_relay(broker, bus_with_broker)  # type: ignore[arg-type]
    q = await bus_with_broker.subscribe(_ORG)
    try:
        ps = await _establish(relay, broker)
        await _put_event(ps, _message(_ORG, producer="web-b", event_id="e1", rid="r-1"))
        await asyncio.wait_for(q.get(), timeout=5)
        # No fire-and-forget broadcast task was scheduled by the forward.
        assert not eb._background_tasks
    finally:
        await bus_with_broker.unsubscribe(_ORG, q)
        await relay.aclose()
