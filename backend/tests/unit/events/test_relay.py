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
from unittest.mock import MagicMock

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


# ---------------------------------------------------------------------------
# FAR-250 coverage: start/wait_ready/close/forward/backfill/sweeper edges
# ---------------------------------------------------------------------------


class _UnsubRaisesPubSub:
    def __init__(self, exc: BaseException) -> None:
        self._exc = exc

    async def unsubscribe(self) -> None:
        raise self._exc

    async def aclose(self) -> None:
        return None


class _CloseOnlyPubSub:
    def __init__(self, close_exc: BaseException | None = None) -> None:
        self.closed = False
        self._exc = close_exc

    async def unsubscribe(self) -> None:
        return None

    async def close(self) -> None:
        if self._exc is not None:
            raise self._exc
        self.closed = True


class _AcloseRaisesPubSub:
    async def unsubscribe(self) -> None:
        return None

    async def aclose(self) -> None:
        raise RuntimeError("aclose boom")


class _NoClosePubSub:
    async def unsubscribe(self) -> None:
        return None


def test_start_without_running_loop_is_fail_open(caplog: pytest.LogCaptureFixture) -> None:
    """start() reached without a loop logs and returns (fail-open)."""
    relay = EventRelay(broker=MagicMock(), bus=EventBus(), has_clients=lambda _o: True)
    with caplog.at_level("WARNING", logger="modulo.core.events.relay"):
        relay.start()
    assert relay._sweeper is None
    assert "event_relay.start_skipped_no_loop" in caplog.text


async def test_wait_ready_unknown_org_returns_false(bus: EventBus, broker: _FakeBroker) -> None:
    relay = _make_relay(broker, bus)
    assert await relay.wait_ready("no-such-org", max_wait_s=0.01) is False


async def test_wait_ready_times_out_before_subscription(bus: EventBus, broker: _FakeBroker) -> None:
    relay = _make_relay(broker, bus)
    try:
        await relay.acquire(_ORG)
        await asyncio.wait_for(broker.subscribed.wait(), timeout=5)
        # No subscribe confirmation frame is delivered -> wait_ready times out.
        assert await relay.wait_ready(_ORG, max_wait_s=0.05) is False
    finally:
        await relay.aclose()


async def test_close_pubsub_none_is_noop() -> None:
    await relay_mod._close_pubsub(None)  # must not raise


async def test_close_pubsub_unsubscribe_exception_swallowed(caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level("DEBUG", logger="modulo.core.events.relay"):
        await relay_mod._close_pubsub(_UnsubRaisesPubSub(RuntimeError("boom")))
    assert "event_relay.pubsub_unsubscribe_failed" in caplog.text


async def test_close_pubsub_unsubscribe_cancellation_propagates() -> None:
    with pytest.raises(asyncio.CancelledError):
        await relay_mod._close_pubsub(_UnsubRaisesPubSub(asyncio.CancelledError()))


async def test_close_pubsub_falls_back_to_close_without_aclose() -> None:
    ps = _CloseOnlyPubSub()
    await relay_mod._close_pubsub(ps)
    assert ps.closed is True


async def test_close_pubsub_close_exception_swallowed(caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level("DEBUG", logger="modulo.core.events.relay"):
        await relay_mod._close_pubsub(_CloseOnlyPubSub(RuntimeError("close boom")))
    assert "event_relay.pubsub_close_failed" in caplog.text


async def test_close_pubsub_aclose_exception_swallowed(caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level("DEBUG", logger="modulo.core.events.relay"):
        await relay_mod._close_pubsub(_AcloseRaisesPubSub())
    assert "event_relay.pubsub_close_failed" in caplog.text


async def test_close_pubsub_without_aclose_or_close_is_noop() -> None:
    await relay_mod._close_pubsub(_NoClosePubSub())  # must not raise


async def test_handle_payload_drops_non_string_and_non_object(
    bus: EventBus,
    broker: _FakeBroker,
    caplog: pytest.LogCaptureFixture,
) -> None:
    relay = _make_relay(broker, bus)
    q = await bus.subscribe(_ORG)
    try:
        with caplog.at_level("WARNING", logger="modulo.core.events.relay"):
            await relay._handle_payload(_ORG, 12345)  # non-string data
            await relay._handle_payload(_ORG, json.dumps([1, 2, 3]))  # non-object JSON
            await relay._handle_payload(_ORG, json.dumps(_message(_ORG, producer="web-b", event_id="ok", rid="r-ok")))
        assert "event_relay.non_string_payload" in caplog.text
        assert "event_relay.non_object_payload" in caplog.text
        assert q.get_nowait()["id"] == "r-ok"
    finally:
        await bus.unsubscribe(_ORG, q)


async def test_forward_without_event_id_is_not_deduped(bus: EventBus, broker: _FakeBroker) -> None:
    relay = _make_relay(broker, bus)
    q = await bus.subscribe(_ORG)
    try:
        await relay._forward(_ORG, {"org_id": _ORG, "id": "r-noeid"}, source="redis")
        await relay._forward(_ORG, {"org_id": _ORG, "id": "r-noeid"}, source="redis")
        assert q.get_nowait()["id"] == "r-noeid"
        assert q.get_nowait()["id"] == "r-noeid"
    finally:
        await bus.unsubscribe(_ORG, q)


async def test_forward_dedupe_cap_evicts_oldest_event(bus: EventBus, broker: _FakeBroker) -> None:
    relay = EventRelay(broker=broker, bus=bus, has_clients=lambda _o: True, max_seen=1)  # type: ignore[arg-type]
    q = await bus.subscribe(_ORG)
    try:
        await relay._forward(_ORG, {"org_id": _ORG, "event_id": "e1", "id": "r1"}, source="redis")
        await relay._forward(_ORG, {"org_id": _ORG, "event_id": "e2", "id": "r2"}, source="redis")
        assert len(relay._seen[_ORG]) == 1  # e1 evicted by the cap
        # e1 is no longer "seen" and can flow again.
        await relay._forward(_ORG, {"org_id": _ORG, "event_id": "e1", "id": "r1-again"}, source="redis")
        assert [q.get_nowait()["id"] for _ in range(3)] == ["r1", "r2", "r1-again"]
    finally:
        await bus.unsubscribe(_ORG, q)


async def test_backfill_sync_loader_skips_non_dict_entries(bus: EventBus, broker: _FakeBroker) -> None:
    relay = _make_relay(
        broker,
        bus,
        backfill_loader=lambda org: [
            _message(org, producer="web-b", event_id="a", rid="r-a"),
            "not-a-dict",
        ],
    )
    q = await bus.subscribe(_ORG)
    try:
        await relay._backfill(_ORG)
        assert q.get_nowait()["id"] == "r-a"
        assert q.empty()
    finally:
        await bus.unsubscribe(_ORG, q)


async def test_backfill_loader_cancellation_propagates(bus: EventBus, broker: _FakeBroker) -> None:
    def loader(_org: str) -> list[dict[str, Any]]:
        raise asyncio.CancelledError

    relay = _make_relay(broker, bus, backfill_loader=loader)
    with pytest.raises(asyncio.CancelledError):
        await relay._backfill(_ORG)


async def test_sweeper_keeps_subscription_while_clients_present(bus: EventBus, broker: _FakeBroker) -> None:
    relay = _make_relay(broker, bus, has_clients=lambda _o: True, idle_ttl=0.05)
    try:
        ps = await _establish(relay, broker)
        await asyncio.sleep(0.2)  # several sweep intervals
        assert _ORG in relay._org_tasks
        assert not ps.closed
    finally:
        await relay.aclose()


async def test_non_message_frame_types_are_ignored(bus: EventBus, broker: _FakeBroker) -> None:
    relay = _make_relay(broker, bus)
    q = await bus.subscribe(_ORG)
    try:
        ps = await _establish(relay, broker)
        await ps.inbox.put({"type": "pong"})  # neither subscribe nor message
        await _put_event(ps, _message(_ORG, producer="web-b", event_id="ok", rid="r-ok"))
        got = await asyncio.wait_for(q.get(), timeout=5)
        assert got["id"] == "r-ok"
        assert q.empty()
    finally:
        await bus.unsubscribe(_ORG, q)
        await relay.aclose()


async def test_listen_return_resubscribes_without_backoff(bus: EventBus, broker: _FakeBroker) -> None:
    relay = _make_relay(broker, bus)
    try:
        ps = await _establish(relay, broker)
        await ps.inbox.put(None)  # listen() returns cleanly (server unsubscribe)
        await _wait_pubsub_count(broker, 2)
        assert len(broker.pubsubs) == 2
    finally:
        await relay.aclose()


async def test_subscribe_error_before_established_skips_backfill(
    bus: EventBus,
    caplog: pytest.LogCaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(relay_mod, "_BACKOFF_BASE_SECONDS", 0.01)
    broker = _FakeBroker()
    calls = {"n": 0}
    orig = broker.subscribe

    async def flaky(channel: str) -> _FakePubSub:
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("subscribe boom")
        return await orig(channel)

    broker.subscribe = flaky  # type: ignore[method-assign]
    loader_calls: list[str] = []

    def loader(org: str) -> list[dict[str, Any]]:
        loader_calls.append(org)
        return []

    relay = _make_relay(broker, bus, backfill_loader=loader)
    try:
        with caplog.at_level("WARNING", logger="modulo.core.events.relay"):
            await relay.acquire(_ORG)
            await asyncio.wait_for(broker.subscribed.wait(), timeout=5)  # retry succeeded
        assert loader_calls == []  # never established -> no backfill
        assert "event_relay.subscription_error" in caplog.text
    finally:
        await relay.aclose()


async def test_configure_relay_rebuilds_on_broker_swap(bus: EventBus) -> None:
    b1 = _FakeBroker()
    b2 = _FakeBroker()
    first = await relay_mod.configure_relay(broker=b1, bus=bus, has_clients=lambda _o: False)  # type: ignore[arg-type]
    second = await relay_mod.configure_relay(broker=b2, bus=bus, has_clients=lambda _o: False)  # type: ignore[arg-type]
    assert second is not first
    assert relay_mod.get_active_relay() is second
    await relay_mod.reset_relay_for_tests()


async def test_stop_org_propagates_own_cancellation(bus: EventBus, broker: _FakeBroker) -> None:
    """A cancellation delivered while awaiting the child must propagate."""
    relay = _make_relay(broker, bus)
    try:
        await _establish(relay, broker)
        stopper = asyncio.create_task(relay._stop_org(_ORG))
        await asyncio.sleep(0)  # stopper cancels the child and awaits it
        stopper.cancel()
        with pytest.raises(asyncio.CancelledError):
            await stopper
    finally:
        await relay.aclose()


async def test_aclose_propagates_own_cancellation(bus: EventBus, broker: _FakeBroker) -> None:
    """aclose() must not swallow its own cancellation while awaiting the sweeper."""
    relay = _make_relay(broker, bus)
    relay.start()
    await asyncio.sleep(0)  # let the sweeper task start sleeping
    closer = asyncio.create_task(relay.aclose())
    await asyncio.sleep(0)
    closer.cancel()
    with pytest.raises(asyncio.CancelledError):
        await closer


async def test_stop_org_without_ready_entry_still_stops(bus: EventBus, broker: _FakeBroker) -> None:
    """_stop_org handles an org task with no matching ready event."""
    relay = _make_relay(broker, bus)
    try:
        await relay.acquire(_ORG)
        await asyncio.wait_for(broker.subscribed.wait(), timeout=5)
        relay._ready.pop(_ORG, None)
        await relay._stop_org(_ORG)
        assert _ORG not in relay._org_tasks
    finally:
        await relay.aclose()
