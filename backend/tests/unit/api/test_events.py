"""Unit tests for the SSE event endpoint and EventBus integration."""

from __future__ import annotations

import asyncio
import json
import uuid
from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient
from starlette import status

from modulo.api.main import app
from modulo.api.routes.events import (
    _active_connections,
    _test_reset_connections,
    _track_connection,
    _untrack_connection,
)
from modulo.auth.jwt import TenantPrincipal
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


def _make_principal(
    org_id: uuid.UUID | None = None,
    user_id: uuid.UUID | None = None,
) -> TenantPrincipal:
    return TenantPrincipal(
        username="testuser",
        organisation_id=org_id or uuid.uuid4(),
        account_id=user_id or uuid.uuid4(),
        org_role="admin",
    )


@pytest.fixture(autouse=True)
def _reset_singleton():
    import modulo.core.events.event_bus as eb

    eb._event_bus = None
    yield
    eb._event_bus = None


@pytest.fixture(autouse=True)
def _reset_connections():
    """Reset global connection tracking between tests."""
    _test_reset_connections()
    yield
    _test_reset_connections()


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

        await bus.publish(org_id, "run", "run-1", "created", version=0)

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

        await bus.publish("org-a", "pipeline", "pipe-1", "created", version=0)

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

        await bus.publish(org_id, "agent", "agent-1", "updated", version=1)

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

        await bus.publish(org_id, "schema", "schema-1", "updated", version=0)

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

        await bus.publish(org_id, "run", "r1", "updated", version=0)
        await bus.publish(org_id, "run", "r2", "deleted", version=0)

        assert bus._subscribers.get(org_id) is None or not bus._subscribers.get(org_id, [])

    @pytest.mark.asyncio
    async def test_publish_no_subscribers_does_not_raise(self):
        bus = get_event_bus()
        await bus.publish("org-empty", "run", "r1", "created", version=0)
        # publishing to an org with no subscribers must not register a queue
        assert bus._subscribers.get("org-empty") is None


# ---------------------------------------------------------------------------
# Connection tracking (_track_connection / _untrack_connection)
# ---------------------------------------------------------------------------


class TestTrackConnection:
    @pytest.mark.asyncio
    async def test_success(self):
        q: asyncio.Queue[dict] = asyncio.Queue()
        await _track_connection("org-1", "user-1", q, max_org=10, max_user=5)
        assert "org-1" in _active_connections
        assert q in _active_connections["org-1"]

    @pytest.mark.asyncio
    async def test_org_limit_reached(self):
        q = asyncio.Queue()
        for i in range(3):
            await _track_connection("org-lim", f"user-{i}", asyncio.Queue(), max_org=3, max_user=10)
        with pytest.raises(HTTPException) as exc_info:
            await _track_connection("org-lim", "user-new", q, max_org=3, max_user=10)
        assert exc_info.value.status_code == status.HTTP_429_TOO_MANY_REQUESTS
        assert "organisation" in exc_info.value.detail.lower()

    @pytest.mark.asyncio
    async def test_user_limit_reached(self):
        for i in range(2):
            await _track_connection("org-u", "user-a", asyncio.Queue(), max_org=10, max_user=2)
        with pytest.raises(HTTPException) as exc_info:
            await _track_connection("org-u", "user-a", asyncio.Queue(), max_org=10, max_user=2)
        assert exc_info.value.status_code == status.HTTP_429_TOO_MANY_REQUESTS
        assert "user" in exc_info.value.detail.lower()

    @pytest.mark.asyncio
    async def test_different_users_within_limit(self):
        q1 = asyncio.Queue()
        q2 = asyncio.Queue()
        await _track_connection("org-d", "user-1", q1, max_org=10, max_user=2)
        await _track_connection("org-d", "user-2", q2, max_org=10, max_user=2)
        assert q1 in _active_connections["org-d"]
        assert q2 in _active_connections["org-d"]


class TestUntrackConnection:
    @pytest.mark.asyncio
    async def test_removes_queue(self):
        q = asyncio.Queue()
        await _track_connection("org-ut", "user-1", q, max_org=10, max_user=5)
        assert q in _active_connections["org-ut"]
        await _untrack_connection("org-ut", q)
        assert "org-ut" not in _active_connections

    @pytest.mark.asyncio
    async def test_preserves_other_queues(self):
        q1 = asyncio.Queue()
        q2 = asyncio.Queue()
        await _track_connection("org-mix", "user-1", q1, max_org=10, max_user=5)
        await _track_connection("org-mix", "user-2", q2, max_org=10, max_user=5)
        await _untrack_connection("org-mix", q1)
        assert q2 in _active_connections["org-mix"]
        assert q1 not in _active_connections["org-mix"]

    @pytest.mark.asyncio
    async def test_noop_for_missing_org(self):
        q = asyncio.Queue()
        await _untrack_connection("nonexistent", q)
        assert q.qsize() == 0

    @pytest.mark.asyncio
    async def test_noop_for_missing_queue(self):
        q1 = asyncio.Queue()
        await _track_connection("org-nq", "user-1", q1, max_org=10, max_user=5)
        q2 = asyncio.Queue()
        await _untrack_connection("org-nq", q2)  # different queue object
        assert "org-nq" in _active_connections
        assert q1 in _active_connections["org-nq"]

    @pytest.mark.asyncio
    async def test_empty_org_deleted(self):
        q = asyncio.Queue()
        await _track_connection("org-empty-del", "user-1", q, max_org=10, max_user=5)
        assert "org-empty-del" in _active_connections
        await _untrack_connection("org-empty-del", q)
        assert "org-empty-del" not in _active_connections


# ---------------------------------------------------------------------------
# SSE route — org-less principal (403)
# ---------------------------------------------------------------------------


class TestSSERoute:
    @pytest.mark.asyncio
    async def test_no_org_returns_403(self):
        """A principal with no organisation_id should get 403."""
        from modulo.api.routes.events import sse_event_stream

        principal = TenantPrincipal(
            username="noorg",
            organisation_id=None,  # type: ignore[assignment]
            account_id=uuid.uuid4(),
            org_role="admin",
        )
        mock_settings = _make_settings()
        mock_request = AsyncMock()
        mock_request.is_disconnected = AsyncMock(return_value=False)

        with pytest.raises(HTTPException) as exc_info:
            await sse_event_stream(
                request=mock_request,
                settings=mock_settings,
                principal=principal,
            )
        assert exc_info.value.status_code == status.HTTP_403_FORBIDDEN
        assert "organisation" in exc_info.value.detail.lower()

    @pytest.mark.asyncio
    async def test_org_limit_rejects_and_unsubscribes(self):
        """When _track_connection raises 429, the queue is unsubscribed from the bus."""
        from modulo.api.routes.events import sse_event_stream

        bus = get_event_bus()
        principal = _make_principal()
        org_id = str(principal.organisation_id)
        mock_settings = _make_settings()
        mock_settings.modulo_sse_max_connections_per_org = 0  # force 429
        mock_request = AsyncMock()
        mock_request.is_disconnected = AsyncMock(return_value=False)

        with pytest.raises(HTTPException) as exc_info:
            await sse_event_stream(
                request=mock_request,
                settings=mock_settings,
                principal=principal,
            )
        assert exc_info.value.status_code == status.HTTP_429_TOO_MANY_REQUESTS
        # Verify the queue was unsubscribed (no leak)
        assert bus._subscribers.get(org_id) is None

    @pytest.mark.asyncio
    async def test_successful_stream_returns_streaming_response(self):
        """A valid request returns a StreamingResponse with correct headers."""
        from fastapi.responses import StreamingResponse

        from modulo.api.routes.events import sse_event_stream

        principal = _make_principal()
        mock_settings = _make_settings()
        mock_settings.modulo_sse_max_connections_per_org = 10
        mock_settings.modulo_sse_max_connections_per_user = 10
        mock_request = AsyncMock()
        mock_request.is_disconnected = AsyncMock(return_value=True)  # disconnect immediately

        resp = await sse_event_stream(
            request=mock_request,
            settings=mock_settings,
            principal=principal,
        )
        assert isinstance(resp, StreamingResponse)
        assert resp.media_type == "text/event-stream"
        assert resp.headers["Cache-Control"] == "no-cache, no-store"
        assert resp.headers["X-Accel-Buffering"] == "no"

    @pytest.mark.asyncio
    async def test_pump_heartbeat_on_timeout(self):
        """The _pump generator sends heartbeat on timeout (zombie detection)."""
        from modulo.api.routes.events import sse_event_stream

        principal = _make_principal()
        mock_settings = _make_settings()
        mock_settings.modulo_sse_zombie_timeout_seconds = 0.1  # very short
        mock_request = AsyncMock()
        # Return True on first call (enter loop), then False (exit loop)
        disconnect_calls = [False, True]
        mock_request.is_disconnected = AsyncMock(side_effect=disconnect_calls)

        resp = await sse_event_stream(
            request=mock_request,
            settings=mock_settings,
            principal=principal,
        )

        # Consume the generator
        chunks = []
        async for chunk in resp.body_iterator:  # type: ignore[union-attr]
            chunks.append(chunk)
        full_output = "".join(chunks)
        assert ": connected\n\n" in full_output
        assert ": heartbeat\n\n" in full_output

    @pytest.mark.asyncio
    async def test_pump_event_delivery(self):
        """The _pump generator delivers events from the bus."""
        from modulo.api.routes.events import sse_event_stream

        bus = get_event_bus()
        principal = _make_principal()
        org_id = str(principal.organisation_id)
        mock_settings = _make_settings()
        mock_settings.modulo_sse_zombie_timeout_seconds = 5.0
        mock_request = AsyncMock()

        # Subscribe first so we can publish, then disconnect
        await bus.subscribe(org_id)

        call_count = 0

        async def _is_disconnected():
            nonlocal call_count
            call_count += 1
            return call_count != 1

        mock_request.is_disconnected = AsyncMock(side_effect=_is_disconnected)

        resp = await sse_event_stream(
            request=mock_request,
            settings=mock_settings,
            principal=principal,
        )

        # Publish an event to the bus
        await bus.publish(org_id, "pipeline", "pipe-1", "created", version=0)

        # Consume
        chunks = []
        async for chunk in resp.body_iterator:  # type: ignore[union-attr]
            chunks.append(chunk)
        full_output = "".join(chunks)
        assert "resource_changed" in full_output
        assert "pipe-1" in full_output

    @pytest.mark.asyncio
    async def test_pump_graceful_disconnect_cleanup(self):
        """On disconnect, the pump unsubscribes the endpoint's queue from the bus."""
        from modulo.api.routes.events import sse_event_stream

        bus = get_event_bus()
        principal = _make_principal()
        org_id = str(principal.organisation_id)
        mock_settings = _make_settings()
        mock_settings.modulo_sse_zombie_timeout_seconds = 5.0
        mock_request = AsyncMock()

        # is_disconnected returns True immediately to exit the loop
        mock_request.is_disconnected = AsyncMock(return_value=True)

        resp = await sse_event_stream(
            request=mock_request,
            settings=mock_settings,
            principal=principal,
        )

        # Consume (which runs the generator)
        async for _ in resp.body_iterator:  # type: ignore[union-attr]
            pass

        # After the pump completes, the endpoint's queue should be unsubscribed.
        sub_list = bus._subscribers.get(org_id)
        assert sub_list is None or len(sub_list) == 0

    @pytest.mark.asyncio
    async def test_pump_exception_logs_and_cleans_up(self):
        """An exception in the event loop logs and cleans up."""
        from modulo.api.routes.events import sse_event_stream

        bus = get_event_bus()
        principal = _make_principal()
        org_id = str(principal.organisation_id)
        mock_settings = _make_settings()
        mock_settings.modulo_sse_zombie_timeout_seconds = 5.0
        mock_request = AsyncMock()

        call_count = 0

        async def _is_disconnected():
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return False
            # Simulate an error on the second iteration
            raise RuntimeError("simulated error")

        mock_request.is_disconnected = AsyncMock(side_effect=_is_disconnected)

        resp = await sse_event_stream(
            request=mock_request,
            settings=mock_settings,
            principal=principal,
        )

        # Should not raise — the exception is caught, logged, and cleaned up
        chunks = []
        async for chunk in resp.body_iterator:  # type: ignore[union-attr]
            chunks.append(chunk)

        # After error, the endpoint's queue should be unsubscribed
        sub_list = bus._subscribers.get(org_id)
        assert sub_list is None or len(sub_list) == 0

    @pytest.mark.asyncio
    async def test_user_limit_rejects_and_unsubscribes(self):
        """When per-user limit is hit, the queue is unsubscribed from the bus."""
        from modulo.api.routes.events import sse_event_stream

        bus = get_event_bus()
        principal = _make_principal()
        org_id = str(principal.organisation_id)
        mock_settings = _make_settings()
        mock_settings.modulo_sse_max_connections_per_org = 100
        mock_settings.modulo_sse_max_connections_per_user = 0  # force 429
        mock_request = AsyncMock()
        mock_request.is_disconnected = AsyncMock(return_value=False)

        with pytest.raises(HTTPException) as exc_info:
            await sse_event_stream(
                request=mock_request,
                settings=mock_settings,
                principal=principal,
            )
        assert exc_info.value.status_code == status.HTTP_429_TOO_MANY_REQUESTS
        assert "user" in exc_info.value.detail.lower()
        assert bus._subscribers.get(org_id) is None


# ---------------------------------------------------------------------------
# FAR-250: relay wiring, backfill loader, connection helper edges
# ---------------------------------------------------------------------------


async def test_backfill_loader_reads_recent_notifications(monkeypatch: pytest.MonkeyPatch) -> None:
    """_make_backfill_loader() re-reads recent notifications and shapes events."""
    from modulo.api.routes import events as ev

    org = uuid.uuid4()
    row = MagicMock()
    row.id = uuid.uuid4()
    row.category = "run_failed"
    row.created_at = datetime(2026, 9, 23, 12, 0, tzinfo=UTC)

    result = MagicMock()
    result.scalars.return_value.all.return_value = [row]
    session = AsyncMock()
    session.execute = AsyncMock(return_value=result)
    begin_cm = AsyncMock()
    begin_cm.__aenter__ = AsyncMock(return_value=None)
    begin_cm.__aexit__ = AsyncMock(return_value=False)
    session.begin = MagicMock(return_value=begin_cm)
    factory = MagicMock()
    factory.return_value.__aenter__ = AsyncMock(return_value=session)
    factory.return_value.__aexit__ = AsyncMock(return_value=False)

    monkeypatch.setattr(ev, "get_or_create_engine", MagicMock())
    monkeypatch.setattr(ev, "get_or_create_session_factory", MagicMock(return_value=factory))
    monkeypatch.setattr(ev, "set_rls_org", AsyncMock())

    loader = ev._make_backfill_loader()
    events = await loader(str(org))

    assert len(events) == 1
    assert events[0]["type"] == "notification"
    assert events[0]["notification_id"] == str(row.id)
    assert events[0]["category"] == "run_failed"


async def test_org_has_clients_reflects_tracked_connections() -> None:
    from modulo.api.routes import events as ev

    assert ev._org_has_clients("org-none") is False
    ev._active_connections["org-has"] = {asyncio.Queue()}
    assert ev._org_has_clients("org-has") is True


async def test_test_reset_connections_schedules_relay_reset(monkeypatch: pytest.MonkeyPatch) -> None:
    """In a running loop, the test reset schedules relay teardown."""
    from modulo.api.routes import events as ev

    reset = AsyncMock()
    monkeypatch.setattr(ev.relay_mod, "reset_relay_for_tests", reset)
    ev._active_connections["org-r"] = {asyncio.Queue()}
    ev._queue_users[1] = "u"

    ev._test_reset_connections()
    for _ in range(100):
        if not ev._relay_reset_tasks:
            break
        await asyncio.sleep(0.01)

    assert ev._active_connections == {}
    assert ev._queue_users == {}
    reset.assert_awaited_once()


async def test_ensure_relay_returns_configured_relay(monkeypatch: pytest.MonkeyPatch) -> None:
    """_ensure_relay() configures the relay when the bus has a Redis broker."""
    from modulo.api.routes import events as ev

    bus = MagicMock()
    bus.redis_broker = MagicMock()
    fake_relay = MagicMock()
    configure = AsyncMock(return_value=fake_relay)
    monkeypatch.setattr(ev, "get_event_bus", lambda: bus)
    monkeypatch.setattr(ev.relay_mod, "configure_relay", configure)

    got = await ev._ensure_relay()

    assert got is fake_relay
    configure.assert_awaited_once()


async def _run_sse_with_relay(monkeypatch: pytest.MonkeyPatch, fake_relay: MagicMock) -> object:
    """Invoke the SSE route with _ensure_relay stubbed to *fake_relay*."""
    from modulo.api.routes import events as ev

    monkeypatch.setattr(ev, "_ensure_relay", AsyncMock(return_value=fake_relay))
    principal = _make_principal()
    settings = _make_settings()
    settings.modulo_sse_zombie_timeout_seconds = 5.0
    settings.modulo_sse_max_connections_per_org = 10
    settings.modulo_sse_max_connections_per_user = 10
    request = AsyncMock()
    request.is_disconnected = AsyncMock(return_value=True)
    return await ev.sse_event_stream(request=request, settings=settings, principal=principal)


async def test_sse_route_acquires_and_releases_relay(monkeypatch: pytest.MonkeyPatch) -> None:
    fake_relay = MagicMock()
    fake_relay.acquire = AsyncMock()
    fake_relay.release = AsyncMock()

    resp = await _run_sse_with_relay(monkeypatch, fake_relay)
    async for _ in resp.body_iterator:  # type: ignore[attr-defined]
        pass

    fake_relay.acquire.assert_awaited_once()
    fake_relay.release.assert_awaited_once()


async def test_sse_route_relay_acquire_cancellation_cleans_up(monkeypatch: pytest.MonkeyPatch) -> None:
    fake_relay = MagicMock()
    fake_relay.acquire = AsyncMock(side_effect=asyncio.CancelledError())

    with pytest.raises(asyncio.CancelledError):
        await _run_sse_with_relay(monkeypatch, fake_relay)


async def test_sse_route_relay_acquire_failure_is_fail_open(monkeypatch: pytest.MonkeyPatch) -> None:
    fake_relay = MagicMock()
    fake_relay.acquire = AsyncMock(side_effect=RuntimeError("relay boom"))
    fake_relay.release = AsyncMock()

    resp = await _run_sse_with_relay(monkeypatch, fake_relay)
    async for _ in resp.body_iterator:  # type: ignore[attr-defined]
        pass

    # The relay was dropped after acquire failed -> no release attempted.
    fake_relay.release.assert_not_awaited()


async def test_sse_route_relay_release_failure_is_fail_open(monkeypatch: pytest.MonkeyPatch) -> None:
    fake_relay = MagicMock()
    fake_relay.acquire = AsyncMock()
    fake_relay.release = AsyncMock(side_effect=RuntimeError("release boom"))

    resp = await _run_sse_with_relay(monkeypatch, fake_relay)
    async for _ in resp.body_iterator:  # type: ignore[attr-defined]
        pass  # must not raise

    fake_relay.release.assert_awaited_once()


async def test_sse_route_relay_release_cancellation_propagates(monkeypatch: pytest.MonkeyPatch) -> None:
    fake_relay = MagicMock()
    fake_relay.acquire = AsyncMock()
    fake_relay.release = AsyncMock(side_effect=asyncio.CancelledError())

    resp = await _run_sse_with_relay(monkeypatch, fake_relay)
    with pytest.raises(asyncio.CancelledError):
        async for _ in resp.body_iterator:  # type: ignore[attr-defined]
            pass
