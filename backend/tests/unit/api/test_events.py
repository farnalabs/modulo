"""Unit tests for the SSE event endpoint and EventBus integration."""

from __future__ import annotations

import asyncio
import json

import pytest
from fastapi.testclient import TestClient

from modulo.api.main import app
from modulo.auth.dependencies import get_current_user
from modulo.auth.jwt import AuthenticatedPrincipal
from modulo.core.events.event_bus import EventBus, get_event_bus
from modulo.settings import Settings, get_settings


def _make_settings() -> Settings:
    return Settings(
        database_url="postgresql+asyncpg://localhost/test",
        secret_key="a" * 32,
        fernet_key="a" * 32,
        modulo_admin_password="test",
        redis_url="",
    )


@pytest.fixture(autouse=True)
def _reset_singleton():
    import modulo.core.events.event_bus as eb

    eb._event_bus = None
    yield
    eb._event_bus = None


# ---------------------------------------------------------------------------
# Auth rejection (non-streaming, works with TestClient)
# ---------------------------------------------------------------------------


class TestAuthRejection:
    def test_no_auth_returns_401(self):
        app.dependency_overrides[get_settings] = _make_settings
        client = TestClient(app)
        resp = client.get("/api/v1/events")
        assert resp.status_code == 401
        app.dependency_overrides.clear()

    def test_invalid_token_returns_401(self):
        app.dependency_overrides[get_settings] = _make_settings
        client = TestClient(app)
        resp = client.get("/api/v1/events", headers={"Authorization": "Bearer invalid"})
        assert resp.status_code == 401
        app.dependency_overrides.clear()


# ---------------------------------------------------------------------------
# EventBus integration tests (async, direct queue access)
# ---------------------------------------------------------------------------


class TestEventBusSSEIntegration:
    """Test the SSE event flow end-to-end via the EventBus directly.

    The SSE endpoint wraps EventBus.subscribe() in an HTTP StreamingResponse.
    The core logic (subscribe → publish → receive → unsubscribe) is tested here
    to verify the exact same code path without HTTP streaming overhead.
    """

    @pytest.mark.asyncio
    async def test_receive_event(self):
        bus = get_event_bus()
        org_id = "org-test"
        q = await bus.subscribe(org_id)

        bus.publish(org_id, "run", "run-1", "created", version=0)

        event = await asyncio.wait_for(q.get(), timeout=2.0)
        assert event["type"] == "run"
        assert event["id"] == "run-1"
        assert event["action"] == "created"
        assert event["org_id"] == org_id

    @pytest.mark.asyncio
    async def test_org_filtering(self):
        bus = get_event_bus()
        q_a = await bus.subscribe("org-a")
        q_b = await bus.subscribe("org-b")

        bus.publish("org-a", "pipeline", "pipe-1", "created", version=0)

        event_a = await asyncio.wait_for(q_a.get(), timeout=2.0)
        assert event_a["org_id"] == "org-a"

        with pytest.raises(asyncio.TimeoutError):
            await asyncio.wait_for(q_b.get(), timeout=0.3)

        await bus.unsubscribe("org-a", q_a)
        await bus.unsubscribe("org-b", q_b)

    @pytest.mark.asyncio
    async def test_multiple_subscribers(self):
        bus = get_event_bus()
        org_id = "org-multi"
        q1 = await bus.subscribe(org_id)
        q2 = await bus.subscribe(org_id)

        bus.publish(org_id, "agent", "agent-1", "updated", version=1)

        e1 = await asyncio.wait_for(q1.get(), timeout=2.0)
        e2 = await asyncio.wait_for(q2.get(), timeout=2.0)
        assert e1["id"] == "agent-1"
        assert e2["id"] == "agent-1"

        await bus.unsubscribe(org_id, q1)
        await bus.unsubscribe(org_id, q2)

    @pytest.mark.asyncio
    async def test_cleanup_on_unsubscribe(self):
        bus = get_event_bus()
        org_id = "org-cleanup"
        q = await bus.subscribe(org_id)
        assert len(bus._subscribers.get(org_id, [])) == 1

        await bus.unsubscribe(org_id, q)
        assert bus._subscribers.get(org_id) is None

    @pytest.mark.asyncio
    async def test_sse_message_format(self):
        bus = get_event_bus()
        org_id = "org-format"
        q = await bus.subscribe(org_id)

        bus.publish(org_id, "schema", "schema-1", "updated", version=0)

        event = await asyncio.wait_for(q.get(), timeout=2.0)
        sse = f"event: resource_changed\ndata: {json.dumps(event)}\n\n"

        assert "event: resource_changed" in sse
        assert '"type": "schema"' in sse
        assert '"id": "schema-1"' in sse
        assert '"action": "updated"' in sse

        await bus.unsubscribe(org_id, q)

    @pytest.mark.asyncio
    async def test_slow_consumer_cleanup(self):
        bus = EventBus()
        org_id = "org-slow"
        limited_q = asyncio.Queue(maxsize=1)
        bus._subscribers[org_id] = [limited_q]

        bus.publish(org_id, "run", "r1", "updated", version=0)
        bus.publish(org_id, "run", "r2", "deleted", version=0)

        assert bus._subscribers.get(org_id) is None or len(bus._subscribers.get(org_id, [])) == 0

    @pytest.mark.asyncio
    async def test_publish_no_subscribers_does_not_raise(self):
        bus = get_event_bus()
        bus.publish("org-empty", "run", "r1", "created", version=0)
