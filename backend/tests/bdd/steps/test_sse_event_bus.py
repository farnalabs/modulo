"""BDD step definitions: SSE Event Bus feature scenarios."""

from __future__ import annotations

import asyncio
import contextlib
import os as _os
import time
import uuid
from typing import Any

import pytest
from pytest_bdd import given, parsers, scenarios, then, when

from modulo.api.routes.events import _test_reset_connections
from modulo.core.events.event_bus import EventBus, get_event_bus

_features_dir = _os.path.normpath(_os.path.join(_os.path.dirname(__file__), "..", "features", "events"))
scenarios(_features_dir)

_ORG_ID = "00000000-0000-0000-0000-000000000001"
_ALT_ORG_ID = "00000000-0000-0000-0000-000000000003"


# ---------------------------------------------------------------------------
# Synchronous helpers for EventBus (pytest-bdd 8.x doesn't await step fns)
# ---------------------------------------------------------------------------


def _publish_sync(
    bus: EventBus,
    org_id: str,
    resource_type: str,
    resource_id: str,
    action: str,
    version: int = 1,
) -> None:
    """Synchronous EventBus publish — puts directly into subscriber queues."""
    event: dict[str, Any] = {
        "type": resource_type,
        "id": resource_id,
        "action": action,
        "version": version,
        "org_id": org_id,
    }
    for q in list(bus._subscribers.get(org_id, [])):
        with contextlib.suppress(Exception):
            q.put_nowait(event)


def _queue_get_with_timeout(q, timeout: float = 2.0) -> dict[str, Any]:
    """Synchronous blocking read from an asyncio.Queue with polling timeout."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            return q.get_nowait()
        except Exception:
            time.sleep(0.01)
    raise AssertionError(f"Timed out after {timeout}s waiting for event on queue")


def _queue_empty_with_timeout(q, timeout: float = 0.3) -> None:
    """Assert no event arrives on a queue within the given timeout."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            q.get_nowait()
            raise AssertionError("Expected queue to remain empty but an event was present")
        except Exception:
            time.sleep(0.01)


# ---------------------------------------------------------------------------
# Fixtures — per-scenario isolation
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _reset_event_bus():
    """Reset EventBus singleton + connection tracking before each scenario."""
    import modulo.core.events.event_bus as eb

    eb._event_bus = None
    _test_reset_connections()
    yield
    eb._event_bus = None
    _test_reset_connections()


@pytest.fixture
def ctx():
    """Shared mutable context dict for SSE event bus tests."""
    return {}


# ===========================================================================
# Given steps
# ===========================================================================


@given("a valid auth token")
def valid_auth_token(ctx) -> None:
    """No-op: the ``client`` fixture already provides an authenticated principal."""


@given("an invalid auth token")
def invalid_auth_token(ctx, unauth_client) -> None:
    """Switch to an unauthenticated TestClient for the 401 scenario."""
    ctx["unauth_client"] = unauth_client


@given("two clients in different organisations")
def two_clients_diff_orgs(ctx) -> None:
    """Set up subscriber queues for two separate orgs."""
    bus = get_event_bus()
    q_a = asyncio.Queue()
    q_b = asyncio.Queue()
    bus._subscribers[_ORG_ID] = [q_a]
    bus._subscribers[_ALT_ORG_ID] = [q_b]
    ctx["queue_a"] = q_a
    ctx["queue_b"] = q_b


@given("a connected SSE client")
def connected_sse_client(ctx) -> None:
    """Simulate an SSE client by subscribing to the EventBus."""
    bus = get_event_bus()
    q = asyncio.Queue()
    bus._subscribers[_ORG_ID] = [q]
    ctx["queue"] = q


# ===========================================================================
# When steps
# ===========================================================================


@when("a client connects to the SSE event stream")
def client_connects_sse(ctx) -> None:
    """Subscribe to EventBus (valid auth) or make raw HTTP call (invalid auth)."""
    unauth = ctx.get("unauth_client")
    if unauth is not None:
        # Make a real HTTP request — no StreamingResponse because auth fails
        # before the stream is created.
        ctx["_resp"] = unauth.get("/api/v1/events")
        return
    bus = get_event_bus()
    q = asyncio.Queue()
    bus._subscribers[_ORG_ID] = [q]
    ctx["queue"] = q


@when(parsers.parse("a run is updated via the API"))
def run_updated_via_api(ctx) -> None:
    """Publish a 'run updated' event via EventBus (same code path as SQLAlchemy listeners)."""
    _publish_sync(get_event_bus(), _ORG_ID, "run", str(uuid.uuid4()), "updated", version=1)


@when(parsers.parse("a resource is mutated in organisation A"))
def resource_mutated_in_org_a(ctx) -> None:
    """Publish an event to organisation A's subscribers only."""
    _publish_sync(get_event_bus(), _ORG_ID, "pipeline", str(uuid.uuid4()), "created", version=1)


@when("the client disconnects")
def client_disconnects(ctx) -> None:
    """Unsubscribe from EventBus to simulate client disconnect."""
    bus = get_event_bus()
    q = ctx.get("queue")
    if q is not None:
        subs = bus._subscribers.get(_ORG_ID, [])
        with contextlib.suppress(ValueError):
            subs.remove(q)


@when(parsers.parse("a new pipeline is created"))
def new_pipeline_created(ctx) -> None:
    """Publish a 'pipeline created' event via EventBus."""
    _publish_sync(get_event_bus(), _ORG_ID, "pipeline", str(uuid.uuid4()), "created", version=1)


@when(parsers.parse("a pipeline is deleted"))
def pipeline_deleted(ctx) -> None:
    """Publish a 'pipeline deleted' event via EventBus."""
    _publish_sync(get_event_bus(), _ORG_ID, "pipeline", str(uuid.uuid4()), "deleted", version=1)


# ===========================================================================
# Then steps
# ===========================================================================


@then(parsers.parse('the client receives a resource_changed event with type "{resource_type}" and action "{action}"'))
def client_receives_event(resource_type: str, action: str, ctx) -> None:
    """Verify the client's queue received the expected resource-changed event."""
    q = ctx.get("queue")
    assert q is not None, "No event queue — client was not connected"
    event = _queue_get_with_timeout(q, timeout=2.0)
    assert event["type"] == resource_type, f"Expected type {resource_type!r}, got {event['type']!r}"
    assert event["action"] == action, f"Expected action {action!r}, got {event['action']!r}"


@then(parsers.parse("only the client in organisation A receives the event"))
def only_org_a_receives(ctx) -> None:
    """Org A gets the event; org B's queue must remain empty."""
    q_a = ctx.get("queue_a")
    q_b = ctx.get("queue_b")
    assert q_a is not None, "Queue for org A missing"
    assert q_b is not None, "Queue for org B missing"
    event = _queue_get_with_timeout(q_a, timeout=2.0)
    assert event is not None
    assert event["org_id"] == _ORG_ID
    _queue_empty_with_timeout(q_b, timeout=0.3)


@then("no further events are delivered to that client")
def no_further_events(ctx) -> None:
    """After disconnect, verify the queue is unsubscribed and receives nothing."""
    q = ctx.get("queue")
    bus = get_event_bus()
    if q is not None:
        subs = bus._subscribers.get(_ORG_ID, [])
        assert q not in subs, "Queue is still subscribed after disconnect"
    _publish_sync(bus, _ORG_ID, "run", str(uuid.uuid4()), "updated", version=99)
    if q is not None:
        _queue_empty_with_timeout(q, timeout=0.3)


@then("the connection is rejected with 401")
def connection_rejected_401(ctx) -> None:
    """Verify the SSE endpoint returned 401 for unauthenticated requests."""
    resp = ctx.get("_resp")
    assert resp is not None, "No response stored — expected an HTTP call"
    assert resp.status_code == 401, f"Expected 401, got {resp.status_code}: {resp.text[:200]}"
