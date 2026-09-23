"""Web-process Redis -> EventBus relay pump (FAR-250).

The EventBus broadcasts every local event to Redis one-way; until now nothing
subscribed to ``modulo:events:resource:{org_id}``. This pump is that missing
consumer — **web process only**. SAQ workers hold a subscription too (see
:mod:`modulo.core.events.worker_subscription`) but never relay: only the web
process serves SSE, so only the web process re-injects.

Design:

* **Refcounted per-org subscriptions** — subscribe when the first SSE client
  for an org connects (``acquire``), unsubscribe when the last disconnects
  (``release``). An idle-TTL sweeper force-stops any subscription whose org
  has no tracked SSE clients (fallback for a missed ``release``).
* **Echo suppression** — every Redis payload carries the publisher's
  ``producer_id`` (stamped in ``EventBus._redis_broadcast``); payloads whose
  producer is THIS process are dropped. Without it, a web process would
  re-deliver its own local events to its own SSE clients (double delivery).
* **No re-broadcast** — relayed events go through ``EventBus.deliver_local``
  only (local fan-out, no Redis leg), so two web processes cannot ping-pong.
* **Reconnect backfill** — Redis pub/sub has no replay. When an established
  subscription blips, recent notifications are re-read from the DB and
  re-fanned with ``event_id`` dedupe so connected clients recover what the
  gap dropped without a duplicate on the events that did arrive.
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections import OrderedDict
from collections.abc import Callable
from typing import TYPE_CHECKING, Any

from modulo.core.events.event_bus import LOCAL_PRODUCER_ID, EventBus

if TYPE_CHECKING:
    from modulo.core.events.redis_broker import RedisEventBroker

_log = logging.getLogger(__name__)

_IDLE_SWEEP_DIVISOR = 2
_MAX_SEEN_DEFAULT = 512
_BACKOFF_BASE_SECONDS = 1.0
_BACKOFF_MAX_SECONDS = 30.0


class EventRelay:
    """Refcounted, echo-suppressing Redis -> local EventBus relay."""

    def __init__(
        self,
        *,
        broker: RedisEventBroker,
        bus: EventBus,
        has_clients: Callable[[str], bool],
        backfill_loader: Callable[[str], Any] | None = None,
        idle_ttl: float = 60.0,
        local_producer_id: str | None = None,
        max_seen: int = _MAX_SEEN_DEFAULT,
    ) -> None:
        self._broker = broker
        self._bus = bus
        self._has_clients = has_clients
        self._backfill_loader = backfill_loader
        self._idle_ttl = idle_ttl
        self._local_producer_id = local_producer_id if local_producer_id is not None else LOCAL_PRODUCER_ID
        self._max_seen = max_seen

        self._refcounts: dict[str, int] = {}
        self._org_tasks: dict[str, asyncio.Task[Any]] = {}
        self._ready: dict[str, asyncio.Event] = {}
        self._seen: dict[str, OrderedDict[str, None]] = {}
        self._sweeper: asyncio.Task[Any] | None = None
        self._org_tasks_held: set[asyncio.Task[Any]] = set()

    # -- lifecycle -----------------------------------------------------------

    def start(self) -> None:
        """Start the idle-TTL sweeper (idempotent)."""
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            # Sync entry point reached without a loop; fail open with a log —
            # acquire()/configure_relay() are async and always have one.
            # (loop.create_task via a guarded get_running_loop is the same
            # idiom as EventBus._redis_broadcast_if_configured.)
            _log.warning("event_relay.start_skipped_no_loop")
            return
        if self._sweeper is None or self._sweeper.done():
            self._sweeper = loop.create_task(self._sweep_loop())
            self._org_tasks_held.add(self._sweeper)
            self._sweeper.add_done_callback(self._org_tasks_held.discard)

    def is_broker(self, broker: RedisEventBroker) -> bool:
        """True when this relay is bound to *broker* (singleton reuse check)."""
        return self._broker is broker

    async def acquire(self, org_id: str) -> None:
        """Register one SSE client for *org_id*; subscribe on the first one."""
        count = self._refcounts.get(org_id, 0) + 1
        self._refcounts[org_id] = count
        if count == 1:
            ready = asyncio.Event()
            self._ready[org_id] = ready
            task = asyncio.create_task(self._org_loop(org_id, ready))
            self._org_tasks[org_id] = task
            self._org_tasks_held.add(task)
            task.add_done_callback(self._org_tasks_held.discard)
        self.start()

    async def release(self, org_id: str) -> None:
        """Unregister one SSE client; unsubscribe when the last one leaves."""
        count = self._refcounts.get(org_id, 0) - 1
        if count > 0:
            self._refcounts[org_id] = count
            return
        self._refcounts.pop(org_id, None)
        await self._stop_org(org_id)

    async def wait_ready(self, org_id: str, *, max_wait_s: float = 5.0) -> bool:
        """Wait until the org's Redis subscription is established (tests/ops)."""
        ready = self._ready.get(org_id)
        if ready is None:
            return False
        try:
            # shield: wait_for must not cancel a waiter that outlives the
            # timeout window (asyncio-waitfor-wait-unshielded); Event.wait()
            # creates a fresh future per call, so a timed-out waiter completes
            # harmlessly if the event is later set.
            await asyncio.wait_for(asyncio.shield(ready.wait()), timeout=max_wait_s)
        except TimeoutError:
            return False
        return True

    async def aclose(self) -> None:
        """Stop every org subscription and the sweeper (process teardown)."""
        for org_id in list(self._org_tasks):
            await self._stop_org(org_id)
        sweeper = self._sweeper
        self._sweeper = None
        if sweeper is not None and not sweeper.done():
            sweeper.cancel()
            try:
                await sweeper
            except asyncio.CancelledError:
                # Same guard as _stop_org: swallow only the sweeper's own
                # cancelled outcome, never our own pending cancellation.
                current = asyncio.current_task()
                if current is not None and current.cancelling():
                    raise

    async def _stop_org(self, org_id: str) -> None:
        """Cancel an org's subscription task and wait for it to FINISH.

        Membership (``_org_tasks``) is removed only AFTER the child task has
        actually completed — popping first would let observers see the org as
        "stopped" while its pubsub teardown was still in flight. The child's
        cancelled outcome is swallowed only when *we* are still healthy:
        ``Task.cancelling()`` distinguishes "child ended as requested" (0)
        from "our own cancellation was delivered at this await" (>0), which
        must propagate — a blanket ``suppress(CancelledError)`` here once
        swallowed the sweeper's own cancellation and hung ``aclose()`` on a
        bare future forever.
        """
        task = self._org_tasks.get(org_id)
        if task is None:
            self._ready.pop(org_id, None)
            return
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            current = asyncio.current_task()
            if current is not None and current.cancelling():
                raise
        finally:
            if self._org_tasks.get(org_id) is task:
                self._org_tasks.pop(org_id, None)
            if self._ready.get(org_id) is not None and org_id not in self._org_tasks:
                self._ready.pop(org_id, None)

    # -- idle-TTL fallback ---------------------------------------------------

    async def _sweep_loop(self) -> None:
        """Force-stop subscriptions whose org has no tracked SSE clients.

        ``release`` already unsubscribes on the last disconnect; this sweeper
        is the safety net for a release that never ran (e.g. a response
        discarded before its generator's ``finally`` executed — the refcount
        stays >= 1 forever while the route's connection table is empty).
        The route tracks the connection BEFORE acquire, so a live org always
        has clients while its refcount is > 0; absence of clients is therefore
        the authoritative idle signal, not the refcount.
        """
        interval = max(self._idle_ttl / _IDLE_SWEEP_DIVISOR, 0.05)
        while True:
            await asyncio.sleep(interval)
            for org_id in list(self._org_tasks):
                if self._has_clients(org_id):
                    continue
                _log.info("event_relay.idle_sweep_stop", extra={"org_id": org_id})
                self._refcounts.pop(org_id, None)
                await self._stop_org(org_id)

    # -- per-org subscription loop ------------------------------------------

    async def _org_loop(self, org_id: str, ready: asyncio.Event) -> None:
        backoff = _BACKOFF_BASE_SECONDS
        established = False
        while True:
            pubsub = None
            try:
                await self._broker.connect()
                pubsub = await self._broker.subscribe(f"resource:{org_id}")
                async for message in pubsub.listen():
                    msg_type = message.get("type")
                    if msg_type == "subscribe":
                        # Subscription confirmation drained — safe to publish
                        # against this subscription without a lost-message race.
                        established = True
                        backoff = _BACKOFF_BASE_SECONDS
                        ready.set()
                        continue
                    if msg_type != "message":
                        continue
                    await self._handle_payload(org_id, message.get("data"))
            except asyncio.CancelledError:
                raise
            except Exception:
                _log.warning("event_relay.subscription_error", extra={"org_id": org_id}, exc_info=True)
                if established:
                    # Pub/sub blip: messages were dropped with no replay —
                    # re-read recent notifications from the DB and re-fan
                    # (event_id dedupe drops anything that did arrive).
                    await self._backfill(org_id)
                await self._sleep_backoff(backoff)
                backoff = min(backoff * 2, _BACKOFF_MAX_SECONDS)
            finally:
                await _close_pubsub(pubsub)
            # listen() returning without error (server-side unsubscribe):
            # resubscribe immediately without backoff (no gap to recover).
            ready.clear()

    async def _sleep_backoff(self, delay: float) -> None:
        await asyncio.sleep(delay)

    async def _backfill(self, org_id: str) -> None:
        if self._backfill_loader is None:
            return
        try:
            events = self._backfill_loader(org_id)
            if asyncio.iscoroutine(events) or isinstance(events, asyncio.Future):
                events = await events
        except asyncio.CancelledError:
            raise
        except Exception:
            _log.warning("event_relay.backfill_failed", extra={"org_id": org_id}, exc_info=True)
            return
        for event_dict in events:
            if isinstance(event_dict, dict):
                await self._forward(org_id, event_dict, source="backfill")

    # -- message handling ----------------------------------------------------

    async def _handle_payload(self, org_id: str, data: Any) -> None:
        if not isinstance(data, str):
            _log.warning("event_relay.non_string_payload", extra={"org_id": org_id})
            return
        try:
            payload = json.loads(data)
        except (TypeError, ValueError):
            _log.warning("event_relay.bad_json", extra={"org_id": org_id})
            return
        if not isinstance(payload, dict):
            _log.warning("event_relay.non_object_payload", extra={"org_id": org_id})
            return
        await self._forward(org_id, payload, source="redis")

    async def _forward(self, org_id: str, event: dict[str, Any], *, source: str) -> None:
        producer = event.get("producer_id")
        if producer == self._local_producer_id:
            # Echo of an event this process already delivered locally.
            return
        payload_org = event.get("org_id")
        if isinstance(payload_org, str) and payload_org and payload_org != org_id:
            _log.warning(
                "event_relay.org_mismatch",
                extra={"org_id": org_id, "payload_org_id": payload_org},
            )
            return
        event_id = event.get("event_id")
        if isinstance(event_id, str) and event_id:
            seen = self._seen.setdefault(org_id, OrderedDict())
            if event_id in seen:
                return
            seen[event_id] = None
            while len(seen) > self._max_seen:
                seen.popitem(last=False)
        await self._bus.deliver_local(event)
        _log.debug("event_relay.forwarded", extra={"org_id": org_id, "source": source})


async def _close_pubsub(pubsub: Any) -> None:
    """Best-effort close of a Redis PubSub (loop teardown / resubscribe)."""
    if pubsub is None:
        return
    try:
        await pubsub.unsubscribe()
    except asyncio.CancelledError:
        raise
    except Exception:
        _log.debug("event_relay.pubsub_unsubscribe_failed", exc_info=True)
    try:
        aclose = getattr(pubsub, "aclose", None)
        if aclose is not None:
            await aclose()
        else:  # redis<5 compatibility
            close = getattr(pubsub, "close", None)
            if close is not None:
                await close()
    except asyncio.CancelledError:
        raise
    except Exception:
        _log.debug("event_relay.pubsub_close_failed", exc_info=True)


# ---------------------------------------------------------------------------
# Module-level singleton (configured lazily by the SSE route)
# ---------------------------------------------------------------------------

_relay: EventRelay | None = None
_configure_lock: asyncio.Lock | None = None


def _get_configure_lock() -> asyncio.Lock:
    global _configure_lock
    if _configure_lock is None:
        _configure_lock = asyncio.Lock()
    return _configure_lock


def get_active_relay() -> EventRelay | None:
    """Return the configured relay, if any."""
    return _relay


async def configure_relay(
    *,
    broker: RedisEventBroker,
    bus: EventBus,
    has_clients: Callable[[str], bool],
    backfill_loader: Callable[[str], Any] | None = None,
    idle_ttl: float = 60.0,
) -> EventRelay:
    """Configure (or reuse) the process-wide relay. Rebuilds on broker swap."""
    global _relay
    async with _get_configure_lock():
        if _relay is not None and _relay.is_broker(broker):
            return _relay
        if _relay is not None:
            await _relay.aclose()
        relay = EventRelay(
            broker=broker,
            bus=bus,
            has_clients=has_clients,
            backfill_loader=backfill_loader,
            idle_ttl=idle_ttl,
        )
        relay.start()
        _relay = relay
        return relay


async def reset_relay_for_tests() -> None:
    """Stop and clear the singleton (test teardown only)."""
    global _relay
    relay = _relay
    _relay = None
    if relay is not None:
        await relay.aclose()
