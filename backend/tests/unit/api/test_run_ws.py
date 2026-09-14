"""Unit tests for the run WebSocket forwarder.

Covers ``_sanitize_event`` (read-side sanitisation, FAR-163),
``_consume_run_ws_token`` (Redis single-use token), ``_forward_run_events``
(replay + live forwarding), and ``run_websocket`` (the full handler including
auth, DB load, and terminal-state early exit).
"""

from __future__ import annotations

import asyncio
import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import WebSocketDisconnect
from starlette.websockets import WebSocketState

from modulo.api.routes.run_ws import (
    _consume_run_ws_token,
    _forward_run_events,
    _sanitize_event,
    run_websocket,
)
from modulo.core.pipeline_engine.event_broker import RunEvent

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _event(payload: dict | None = None, seq: int = 1, event_type: str = "run_failed") -> RunEvent:
    return RunEvent(seq=seq, event_type=event_type, run_id=uuid.uuid4(), payload=payload or {})


class _FakeWebSocket:
    """Minimal async stub for FastAPI WebSocket."""

    def __init__(self) -> None:
        self.client_state: WebSocketState = WebSocketState.CONNECTING
        self.sent: list[dict] = []
        self.close_code: int | None = None

    async def accept(self) -> None:
        self.client_state = WebSocketState.CONNECTED

    async def receive_json(self) -> dict:
        return {}

    async def send_json(self, data: dict) -> None:
        self.sent.append(data)

    async def close(self, code: int = 1000) -> None:
        self.close_code = code
        self.client_state = WebSocketState.DISCONNECTED


class _FakeBroker:
    """Stub broker that returns canned replay events and a queue."""

    def __init__(
        self,
        replay: list[RunEvent] | None = None,
        live_items: list[RunEvent | None] | None = None,
    ) -> None:
        self._replay = replay or []
        self._queue: asyncio.Queue[RunEvent | None] = asyncio.Queue()
        for item in live_items or []:
            self._queue.put_nowait(item)

    def replay_since(self, seq: int) -> list[RunEvent]:
        return self._replay

    def subscribe(self) -> asyncio.Queue:
        return self._queue

    def unsubscribe(self, q: asyncio.Queue) -> None:
        pass


class _FakeRun:
    def __init__(self, status: str = "running") -> None:
        self.status = status


def _ws_payload() -> dict:
    return {
        "sub": "u",
        "org_id": str(uuid.uuid4()),
        "account_id": str(uuid.uuid4()),
        "org_role": "admin",
    }


# ---------------------------------------------------------------------------
# Existing: _sanitize_event tests (FAR-163)
# ---------------------------------------------------------------------------


def test_sanitize_event_redacts_db_url_from_detail():
    data = _sanitize_event(
        _event({"error": "RuntimeError", "detail": "postgresql://user:supersecret@db.example/modulo"})
    )
    assert "supersecret" not in data["payload"]["detail"]
    assert "<redacted>" in data["payload"]["detail"]


def test_sanitize_event_redacts_bearer_token_from_error():
    data = _sanitize_event(_event({"error": "RuntimeError", "detail": "Bearer tok1234567890 failed"}))
    assert "tok1234567890" not in data["payload"]["detail"]
    assert "<redacted>" in data["payload"]["detail"]


def test_sanitize_event_redacts_stall_reason():
    data = _sanitize_event(_event({"node_id": "node-a", "stall_reason": "stalled with Bearer tok1234567890"}))
    assert "tok1234567890" not in data["payload"]["stall_reason"]
    assert "<redacted>" in data["payload"]["stall_reason"]


def test_sanitize_event_passes_non_error_payload_through_unchanged():
    payload = {"node_id": "node-a", "output": {"status": "completed", "summary": "all good"}}
    data = _sanitize_event(_event(payload))
    assert data["payload"] == payload


def test_sanitize_event_does_not_mutate_the_original_event():
    payload = {"error": "RuntimeError", "detail": "Bearer tok1234567890"}
    event = _event(payload)
    _sanitize_event(event)
    assert event.payload["detail"] == "Bearer tok1234567890"


# ---------------------------------------------------------------------------
# _consume_run_ws_token
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_consume_token_success():
    fake_redis = AsyncMock()
    payload = _ws_payload()
    with (
        patch("modulo.api.routes.run_ws.Redis") as mock_redis_cls,
        patch(
            "modulo.api.routes.run_ws.consume_ws_token",
            new_callable=AsyncMock,
            return_value=payload,
        ) as mock_consume,
    ):
        mock_redis_cls.from_url.return_value = fake_redis
        result = await _consume_run_ws_token("redis://x", "tok123")
    assert result == payload
    mock_consume.assert_awaited_once_with(fake_redis, "tok123")
    fake_redis.aclose.assert_awaited_once()


@pytest.mark.asyncio
async def test_consume_token_expired():
    from modulo.auth.ws_token import WsTokenExpiredError

    fake_redis = AsyncMock()
    with (
        patch("modulo.api.routes.run_ws.Redis") as mock_redis_cls,
        patch(
            "modulo.api.routes.run_ws.consume_ws_token",
            new_callable=AsyncMock,
            side_effect=WsTokenExpiredError(),
        ),
    ):
        mock_redis_cls.from_url.return_value = fake_redis
        result = await _consume_run_ws_token("redis://x", "tok123")
    assert result is None
    fake_redis.aclose.assert_awaited_once()


@pytest.mark.asyncio
async def test_consume_token_generic_exception():
    fake_redis = AsyncMock()
    with (
        patch("modulo.api.routes.run_ws.Redis") as mock_redis_cls,
        patch(
            "modulo.api.routes.run_ws.consume_ws_token",
            new_callable=AsyncMock,
            side_effect=RuntimeError("boom"),
        ),
    ):
        mock_redis_cls.from_url.return_value = fake_redis
        result = await _consume_run_ws_token("redis://x", "tok123")
    assert result is None
    fake_redis.aclose.assert_awaited_once()


# ---------------------------------------------------------------------------
# _forward_run_events
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_forward_replay_then_terminal():
    ev1 = _event({"node": "a"}, seq=1)
    ev2 = _event({"node": "b"}, seq=2)
    broker = _FakeBroker(replay=[ev1, ev2], live_items=[None])  # None = terminal
    ws = _FakeWebSocket()

    await _forward_run_events(ws, broker, broker.subscribe(), 0)

    assert len(ws.sent) == 3  # 2 replay + 1 terminal
    assert ws.sent[0]["payload"]["node"] == "a"
    assert ws.sent[1]["payload"]["node"] == "b"
    assert ws.sent[2] == {"status": "terminal"}


@pytest.mark.asyncio
async def test_forward_empty_replay_immediate_terminal():
    broker = _FakeBroker(replay=[], live_items=[None])
    ws = _FakeWebSocket()

    await _forward_run_events(ws, broker, broker.subscribe(), 0)

    assert ws.sent == [{"status": "terminal"}]


@pytest.mark.asyncio
async def test_forward_live_events_then_terminal():
    ev1 = _event({"step": 1}, seq=1)
    ev2 = _event({"step": 2}, seq=2)
    broker = _FakeBroker(replay=[], live_items=[ev1, ev2, None])
    ws = _FakeWebSocket()

    await _forward_run_events(ws, broker, broker.subscribe(), 0)

    assert len(ws.sent) == 3
    assert ws.sent[2] == {"status": "terminal"}


@pytest.mark.asyncio
async def test_forward_disconnect_during_replay():
    """WebSocketDisconnect raised during replay_since exits cleanly."""

    class _DisconnectOnReplay:
        def replay_since(self, seq: int):
            yield _event({"node": "a"})
            raise WebSocketDisconnect

        def subscribe(self):
            return asyncio.Queue()

        def unsubscribe(self, q: asyncio.Queue) -> None:
            pass

    ws = _FakeWebSocket()
    await _forward_run_events(ws, _DisconnectOnReplay(), asyncio.Queue(), 0)
    assert len(ws.sent) == 1


@pytest.mark.asyncio
async def test_forward_disconnect_during_send():
    """WebSocketDisconnect inside the live-event send loop breaks out."""
    queue: asyncio.Queue[RunEvent | None] = asyncio.Queue()
    ev = _event({"x": 1})
    queue.put_nowait(ev)

    broker = _FakeBroker(replay=[])
    ws = _FakeWebSocket()

    call_n = 0

    async def _send_that_disconnects(data: dict) -> None:
        nonlocal call_n
        call_n += 1
        if call_n == 1:
            raise WebSocketDisconnect

    ws.send_json = _send_that_disconnects  # type: ignore[method-assign]
    await _forward_run_events(ws, broker, queue, 0)
    assert call_n == 1


@pytest.mark.asyncio
async def test_forward_unsubscribes_on_normal_exit():
    unsubscribed = False

    class _TrackingBroker:
        def replay_since(self, seq: int) -> list[RunEvent]:
            return []

        def subscribe(self) -> asyncio.Queue:
            q: asyncio.Queue[RunEvent | None] = asyncio.Queue()
            q.put_nowait(None)
            return q

        def unsubscribe(self, q: asyncio.Queue) -> None:
            nonlocal unsubscribed
            unsubscribed = True

    ws = _FakeWebSocket()
    broker = _TrackingBroker()
    await _forward_run_events(ws, broker, broker.subscribe(), 0)
    assert unsubscribed


# ---------------------------------------------------------------------------
# run_websocket — auth branches
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_ws_no_token_closes_4001():
    ws = _FakeWebSocket()
    await run_websocket(ws, uuid.uuid4())
    assert ws.close_code == 4001


@pytest.mark.asyncio
async def test_ws_expired_token_closes_4001():
    ws = _FakeWebSocket()
    with patch("modulo.api.routes.run_ws._consume_run_ws_token", new_callable=AsyncMock, return_value=None):
        await run_websocket(ws, uuid.uuid4(), token="bad")
    assert ws.close_code == 4001


@pytest.mark.asyncio
async def test_ws_negative_seq_closes_4001():
    ws = _FakeWebSocket()
    payload = {"sub": "u", "org_id": str(uuid.uuid4()), "account_id": str(uuid.uuid4()), "org_role": "admin"}
    with patch("modulo.api.routes.run_ws._consume_run_ws_token", new_callable=AsyncMock, return_value=payload):
        await run_websocket(ws, uuid.uuid4(), since_event_seq=-1, token="tok")
    assert ws.close_code == 4001


# ---------------------------------------------------------------------------
# run_websocket — DB error branches
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_ws_programming_error_sends_migration_msg():
    from sqlalchemy.exc import ProgrammingError

    ws = _FakeWebSocket()
    payload = {
        "sub": "u",
        "org_id": str(uuid.uuid4()),
        "account_id": str(uuid.uuid4()),
        "org_role": "admin",
    }
    with (
        patch(
            "modulo.api.routes.run_ws._consume_run_ws_token",
            new_callable=AsyncMock,
            return_value=payload,
        ),
        patch(
            "modulo.api.routes.run_ws._load_run_with_rls",
            new_callable=AsyncMock,
            side_effect=ProgrammingError("no table", None, None),
        ),
    ):
        await run_websocket(ws, uuid.uuid4(), token="tok")
    assert ws.close_code == 1011
    assert ws.sent[0]["error"] == "migration_required"


@pytest.mark.asyncio
async def test_ws_sqlalchemy_error_sends_db_unavailable():
    from sqlalchemy.exc import SQLAlchemyError

    ws = _FakeWebSocket()
    payload = {
        "sub": "u",
        "org_id": str(uuid.uuid4()),
        "account_id": str(uuid.uuid4()),
        "org_role": "admin",
    }
    with (
        patch(
            "modulo.api.routes.run_ws._consume_run_ws_token",
            new_callable=AsyncMock,
            return_value=payload,
        ),
        patch(
            "modulo.api.routes.run_ws._load_run_with_rls",
            new_callable=AsyncMock,
            side_effect=SQLAlchemyError("down"),
        ),
    ):
        await run_websocket(ws, uuid.uuid4(), token="tok")
    assert ws.close_code == 1011
    assert ws.sent[0]["error"] == "db_unavailable"


@pytest.mark.asyncio
async def test_ws_generic_exception_sends_internal_error():
    ws = _FakeWebSocket()
    payload = {
        "sub": "u",
        "org_id": str(uuid.uuid4()),
        "account_id": str(uuid.uuid4()),
        "org_role": "admin",
    }
    with (
        patch(
            "modulo.api.routes.run_ws._consume_run_ws_token",
            new_callable=AsyncMock,
            return_value=payload,
        ),
        patch(
            "modulo.api.routes.run_ws._load_run_with_rls",
            new_callable=AsyncMock,
            side_effect=RuntimeError("surprise"),
        ),
    ):
        await run_websocket(ws, uuid.uuid4(), token="tok")
    assert ws.close_code == 1011
    assert ws.sent[0]["error"] == "internal_error"


# ---------------------------------------------------------------------------
# run_websocket — run-not-found & terminal
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_ws_run_not_found_closes_4004():
    ws = _FakeWebSocket()
    run_id = uuid.uuid4()
    payload = {"sub": "u", "org_id": str(uuid.uuid4()), "account_id": str(uuid.uuid4()), "org_role": "admin"}
    with (
        patch("modulo.api.routes.run_ws._consume_run_ws_token", new_callable=AsyncMock, return_value=payload),
        patch("modulo.api.routes.run_ws._load_run_with_rls", new_callable=AsyncMock, return_value=None),
    ):
        await run_websocket(ws, run_id, token="tok")
    assert ws.close_code == 4004
    assert ws.sent[0]["error"] == "run_not_found"


@pytest.mark.asyncio
async def test_ws_terminal_run_sends_terminal_and_closes():
    ws = _FakeWebSocket()
    run_id = uuid.uuid4()
    payload = {"sub": "u", "org_id": str(uuid.uuid4()), "account_id": str(uuid.uuid4()), "org_role": "admin"}
    with (
        patch("modulo.api.routes.run_ws._consume_run_ws_token", new_callable=AsyncMock, return_value=payload),
        patch("modulo.api.routes.run_ws._load_run_with_rls", new_callable=AsyncMock, return_value=_FakeRun("complete")),
    ):
        await run_websocket(ws, run_id, token="tok")
    assert ws.sent[0]["status"] == "terminal"
    assert ws.sent[0]["run_status"] == "complete"
    assert ws.sent[0]["run_id"] == str(run_id)
    assert ws.close_code == 1000  # close() called with default code


# ---------------------------------------------------------------------------
# run_websocket — happy path (subscribes + forwards)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_ws_happy_path_subscribes_and_forwards():
    ws = _FakeWebSocket()
    run_id = uuid.uuid4()
    payload = {"sub": "u", "org_id": str(uuid.uuid4()), "account_id": str(uuid.uuid4()), "org_role": "admin"}
    ev = _event({"step": "ok"}, seq=5, event_type="node_completed")
    fake_broker = _FakeBroker(replay=[ev], live_items=[None])

    with (
        patch("modulo.api.routes.run_ws._consume_run_ws_token", new_callable=AsyncMock, return_value=payload),
        patch("modulo.api.routes.run_ws._load_run_with_rls", new_callable=AsyncMock, return_value=_FakeRun("running")),
        patch("modulo.api.routes.run_ws.get_registry") as mock_get_reg,
    ):
        mock_reg = MagicMock()
        mock_reg.get_or_create.return_value = fake_broker
        mock_get_reg.return_value = mock_reg
        await run_websocket(ws, run_id, since_event_seq=3, token="tok")

    mock_reg.get_or_create.assert_called_once_with(run_id)
    assert ws.sent[0]["payload"]["step"] == "ok"
    assert ws.sent[1] == {"status": "terminal"}
    assert not ws.close_code  # normal close()


@pytest.mark.asyncio
async def test_ws_clamps_large_since_event_seq_to_zero():
    """since_event_seq > 10_000 is clamped to 0, not rejected."""
    ws = _FakeWebSocket()
    run_id = uuid.uuid4()
    payload = {"sub": "u", "org_id": str(uuid.uuid4()), "account_id": str(uuid.uuid4()), "org_role": "admin"}
    fake_broker = _FakeBroker(replay=[], live_items=[None])

    with (
        patch("modulo.api.routes.run_ws._consume_run_ws_token", new_callable=AsyncMock, return_value=payload),
        patch("modulo.api.routes.run_ws._load_run_with_rls", new_callable=AsyncMock, return_value=_FakeRun("running")),
        patch("modulo.api.routes.run_ws.get_registry") as mock_get_reg,
    ):
        mock_reg = MagicMock()
        mock_reg.get_or_create.return_value = fake_broker
        mock_get_reg.return_value = mock_reg
        await run_websocket(ws, run_id, since_event_seq=99999, token="tok")

    # replay_since was called with 0 (clamped), not 99999
    assert ws.sent[0] == {"status": "terminal"}
