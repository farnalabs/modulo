"""FAR-1417 — the org context behind ``ErrorTrackingLogHandler`` must be wired by the REAL request path.

The regression this guards: ``org_id_var`` was declared and read by the
handler but never set anywhere in production code, so every backend ERROR
record was dropped before it could reach ``error_events`` — while the existing
tests stayed green because they set the contextvar by hand.

These tests deliberately do NOT set ``org_id_var`` themselves. They drive a
real ASGI app (real ``CorrelationIdMiddleware`` + ``CatchAllMiddleware``, real
``get_current_user`` dependency, real signed JWT) and assert that:

1. a route-level ERROR is forwarded by a real ``ErrorTrackingLogHandler``;
2. an unhandled exception — which ``CatchAllMiddleware`` logs from OUTSIDE the
   route's task context — is forwarded too, and its direct-ingest helper is
   handed a real ``request.state.organisation_id``;
3. nothing leaks into the caller's context or into the next request.

The two QA-gated follow-up surfaces (same defect class) are covered by
``TestMcpAuthSurface`` (MCP ``McpAuthMiddleware``) and
``TestRunWebSocketSurface`` (the run-streaming WebSocket, whose scope never
traverses ``BaseHTTPMiddleware``).
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import Depends, FastAPI, HTTPException
from httpx import ASGITransport, AsyncClient
from starlette.requests import Request
from starlette.responses import PlainTextResponse
from starlette.websockets import WebSocketState

_ORG_ID = uuid.UUID("00000000-0000-0000-0000-000000000041")
_ACCOUNT_ID = uuid.UUID("00000000-0000-0000-0000-000000000042")
_ROUTE_LOGGER = "test.org_context_wiring.route"
_MCP_LOGGER = "test.org_context_wiring.mcp"


class _RecordingSink:
    """Stand-in for ``ErrorTrackingLogHandler._async_emit``.

    Records each forwarded record AND the ``org_id_var`` value visible at
    forward time — the value the real ingest would have used to attribute the
    row. Plain instance (not a function), so ``self._async_emit(record)``
    resolves to ``sink(record)`` without binding the handler as ``self``.
    """

    def __init__(self) -> None:
        self.records: list[logging.LogRecord] = []
        self.orgs: list[str | None] = []

    async def __call__(self, record: logging.LogRecord) -> None:
        from modulo.core.logging_config import org_id_var

        self.records.append(record)
        self.orgs.append(org_id_var.get())

    @property
    def messages(self) -> list[str]:
        return [record.getMessage() for record in self.records]


@pytest.fixture(autouse=True)
def _clean_rate_limit_state() -> Any:
    """The handler forwards at most one record per org per 5s window."""
    from modulo.core.logging_config import ErrorTrackingLogHandler

    ErrorTrackingLogHandler._last_write_time.clear()
    yield
    ErrorTrackingLogHandler._last_write_time.clear()


@pytest.fixture
def sink() -> Any:
    return _RecordingSink()


@pytest.fixture
def capture(sink: _RecordingSink) -> Any:
    """Attach a REAL ``ErrorTrackingLogHandler`` (with a recording sink) to the root logger."""
    from modulo.core.logging_config import ErrorTrackingLogHandler

    handler = ErrorTrackingLogHandler()
    root = logging.getLogger()
    root.addHandler(handler)
    try:
        with patch.object(ErrorTrackingLogHandler, "_async_emit", new=sink):
            yield handler
    finally:
        root.removeHandler(handler)


@pytest.fixture
def app() -> FastAPI:
    """A real app: production middleware order + the REAL auth dependency.

    Middleware registration mirrors ``api.main``: ``CorrelationIdMiddleware``
    first (inner), ``CatchAllMiddleware`` second (outermost), because
    Starlette's ``add_middleware`` puts the last-added middleware on the
    outside. That ordering is what makes the unhandled-exception test a real
    test of the cross-context carrier.
    """
    from modulo.api.middleware.catch_all import CatchAllMiddleware
    from modulo.api.middleware.correlation_id import CorrelationIdMiddleware
    from modulo.auth.dependencies import get_current_user
    from modulo.auth.jwt import AuthenticatedPrincipal
    from modulo.core.logging_config import org_id_var

    seen: dict[str, Any] = {}
    app = FastAPI()
    app.add_middleware(CorrelationIdMiddleware)
    app.add_middleware(CatchAllMiddleware)

    @app.get("/protected")
    async def protected(principal: AuthenticatedPrincipal = Depends(get_current_user)) -> dict[str, str]:
        seen["in_route"] = org_id_var.get()
        logging.getLogger(_ROUTE_LOGGER).error("orgwiring.route_error")
        return {"ok": "yes"}

    @app.get("/crash")
    async def crash(principal: AuthenticatedPrincipal = Depends(get_current_user)) -> None:
        seen["in_crash"] = org_id_var.get()
        raise RuntimeError("boom")

    @app.get("/public")
    async def public() -> dict[str, str]:
        seen["in_public"] = org_id_var.get()
        logging.getLogger(_ROUTE_LOGGER).error("orgwiring.public_error")
        return {"ok": "yes"}

    app.state.seen = seen
    return app


def _auth_headers() -> dict[str, str]:
    """A real signed access token for the org under test (same secret the app uses)."""
    from modulo.auth.jwt import CLIENT_KIND_BROWSER, create_access_token
    from modulo.settings import get_settings

    token = create_access_token(
        "wiring-tester",
        get_settings().secret_key,
        organisation_id=str(_ORG_ID),
        account_id=str(_ACCOUNT_ID),
        org_role="admin",
        client_kind=CLIENT_KIND_BROWSER,
    )
    return {"Authorization": f"Bearer {token}"}


@asynccontextmanager
async def _client(app: FastAPI) -> AsyncIterator[AsyncClient]:
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        yield client


async def _drain() -> None:
    for _ in range(3):
        await asyncio.sleep(0)


async def test_route_error_is_forwarded_with_the_org_the_request_resolved(
    app: FastAPI,
    capture: object,
    sink: _RecordingSink,
) -> None:
    """The wiring itself: the real dependency binds, the real handler forwards."""
    from modulo.core.logging_config import org_id_var

    async with _client(app) as client:
        resp = await client.get("/protected", headers=_auth_headers())
    await _drain()

    assert resp.status_code == 200
    # The request path set it — this test never touches the contextvar.
    assert app.state.seen["in_route"] == str(_ORG_ID)
    # ...and it did not leak into the caller's context (per-request scoping).
    assert org_id_var.get() is None
    # A real ERROR record from the route was forwarded by the real handler.
    assert "orgwiring.route_error" in sink.messages
    # with the organisation the request resolved, both at emit and forward time.
    assert sink.orgs == [str(_ORG_ID)]


async def test_unhandled_exception_is_forwarded_and_ingest_gets_a_real_org(
    app: FastAPI,
    capture: object,
    sink: _RecordingSink,
) -> None:
    """CatchAll logs from OUTSIDE the route's task, so it re-binds from request.state.

    This is the unhandled-500 path — the one case where persisted evidence was
    missing entirely during FAR-1408.
    """
    with patch("modulo.api.middleware.catch_all._ingest_unhandled_error", new_callable=AsyncMock) as ingest:
        async with _client(app) as client:
            resp = await client.get("/crash", headers=_auth_headers())
        await _drain()

    assert resp.status_code == 500
    assert app.state.seen["in_crash"] == str(_ORG_ID)
    assert "middleware.unhandled_exception" in sink.messages
    assert sink.orgs == [str(_ORG_ID)]

    # The direct-ingest helper is handed the org it reads with getattr(..., None).
    ingest.assert_awaited_once()
    ingested_request = ingest.await_args.args[0]
    assert ingested_request.state.organisation_id == str(_ORG_ID)


async def test_unauthenticated_request_inherits_nothing_and_the_drop_is_visible(
    app: FastAPI,
    capture: object,
    sink: _RecordingSink,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A prior authenticated request must not attribute this request's ERROR to it."""
    from modulo.core.logging_config import org_id_var

    async with _client(app) as client:
        await client.get("/protected", headers=_auth_headers())
        await _drain()
        resp = await client.get("/public")

    assert resp.status_code == 200
    assert app.state.seen["in_public"] is None
    assert org_id_var.get() is None
    # Not forwarded (no org) — and the drop is announced, never silent.
    assert "orgwiring.public_error" not in sink.messages
    assert any("no_org_context" in record.getMessage() for record in caplog.records)


# ---------------------------------------------------------------------------
# MAJOR 1 — MCP auth surface (McpAuthMiddleware resolves _ctx_org_id)
# ---------------------------------------------------------------------------

_ORG_MCP = uuid.UUID("00000000-0000-0000-0000-000000000051")
_USER_MCP = uuid.UUID("00000000-0000-0000-0000-000000000052")
_MCP_API_KEY = "mk_testprefix_testsecretkey1234567890abc"


def _async_cm(result: Any) -> AsyncMock:
    """An ``async with`` stand-in yielding *result* (the session-seam pattern)."""
    cm = AsyncMock()
    cm.__aenter__ = AsyncMock(return_value=result)
    cm.__aexit__ = AsyncMock(return_value=False)
    return cm


class TestMcpAuthSurface:
    """MCP resolves the tenant into ``_ctx_org_id`` — ``org_id_var`` must follow (FAR-1417, QA MAJOR 1).

    The bind happens inside the real ``_authenticate_api_key``; the assertion
    reads it from a CHILD TASK, the same way FastMCP spawns each per-request
    tool task from the authenticated middleware frame.
    """

    async def test_middleware_bind_reaches_the_tool_task_and_forwards_its_error(
        self,
        capture: object,
        sink: _RecordingSink,
    ) -> None:
        from modulo.api.mcp_server import McpAuthMiddleware
        from modulo.core.logging_config import org_id_var

        # --- seams for the real _authenticate_api_key (API-key flavour) ---
        key = MagicMock(role="operator", id=_USER_MCP)
        key.organisation_id = _ORG_MCP
        key.account_id = _USER_MCP
        key.team_id = None
        key.run_id = None
        key.name = None

        lookup_session = MagicMock()
        lookup_session.begin = MagicMock()
        lookup_session.begin.return_value = _async_cm(None)
        lookup_result = MagicMock()
        lookup_result.scalar_one_or_none = MagicMock(return_value=_ORG_MCP)
        lookup_session.execute = AsyncMock(return_value=lookup_result)
        factory = MagicMock(return_value=_async_cm(lookup_session))

        auth_session = AsyncMock()
        auth_session.execute.return_value = MagicMock()

        scope = {
            "type": "http",
            "method": "POST",
            "path": "/mcp/tools/call",
            "headers": [
                (b"authorization", f"Bearer {_MCP_API_KEY}".encode()),
                (b"host", b"localhost"),
            ],
            "query_string": b"",
            "scheme": "http",
            "client": ("127.0.0.1", 8000),
            "server": ("localhost", 8000),
        }
        request = Request(scope)
        seen: dict[str, Any] = {}

        async def downstream(_req: Request) -> PlainTextResponse:
            async def tool_handler() -> str | None:
                # FastMCP starts the tool task FROM this authenticated frame,
                # so asyncio copies the context at task creation.
                return org_id_var.get()

            seen["org_in_tool_task"] = await asyncio.create_task(tool_handler())
            logging.getLogger(_MCP_LOGGER).error("mcp.tool_error")
            return PlainTextResponse("ok")

        previous = org_id_var.get()
        try:
            with (
                patch("modulo.api.mcp_server._get_session_factory", return_value=factory),
                patch("modulo.api.mcp_server._session", return_value=_async_cm(auth_session)),
                patch("modulo.api.mcp_server.validate_api_key", new=AsyncMock(return_value=key)),
                patch(
                    "modulo.api.mcp_server.resolve_role_from_membership",
                    new=AsyncMock(return_value="operator"),
                ),
                patch("modulo.db.rls._ensure_active_transaction", new=AsyncMock(return_value="postgresql")),
                # The plan/flag gate is unrelated to org resolution (and needs a
                # real plan store); the bind happens BEFORE it, so pass it
                # straight through rather than faking the auth path.
                patch("modulo.api.mcp_server._mcp_server_feature_gate", new=AsyncMock(return_value=None)),
            ):
                response = await McpAuthMiddleware(MagicMock()).dispatch(request, downstream)
                await _drain()
                seen["after_dispatch"] = org_id_var.get()
        finally:
            org_id_var.set(previous)

        assert response.status_code == 200
        # Carriers, both of them:
        assert request.state.organisation_id == str(_ORG_MCP)
        assert seen["after_dispatch"] == str(_ORG_MCP)
        # ...propagated into the child (tool) task, where the ERROR was logged.
        assert seen["org_in_tool_task"] == str(_ORG_MCP)
        assert "mcp.tool_error" in sink.messages
        assert sink.orgs == [str(_ORG_MCP)]


# ---------------------------------------------------------------------------
# MAJOR 2 — run-streaming WebSocket (never traverses BaseHTTPMiddleware)
# ---------------------------------------------------------------------------

_ORG_WS = uuid.UUID("00000000-0000-0000-0000-000000000061")
_USER_WS = uuid.UUID("00000000-0000-0000-0000-000000000062")


def _ws_payload() -> dict[str, str]:
    return {"sub": "u", "org_id": str(_ORG_WS), "account_id": str(_USER_WS), "org_role": "admin"}


class _FakeWebSocket:
    """Minimal async stub for FastAPI's WebSocket (accept/send/close only)."""

    def __init__(self) -> None:
        self.client_state: WebSocketState = WebSocketState.CONNECTING
        self.sent: list[dict[str, Any]] = []
        self.close_code: int | None = None

    async def accept(self) -> None:
        self.client_state = WebSocketState.CONNECTED

    async def send_json(self, data: dict[str, Any]) -> None:
        self.sent.append(data)

    async def close(self, code: int = 1000) -> None:
        self.close_code = code
        self.client_state = WebSocketState.DISCONNECTED


class _FakeRun:
    def __init__(self, status: str = "running") -> None:
        self.status = status


class TestRunWebSocketSurface:
    """``run_websocket`` resolves the principal from the ws-token (FAR-1417, QA MAJOR 2).

    Every call runs inside ``asyncio.create_task`` — the exact shape uvicorn
    gives a websocket connection (``create_task(run_asgi())``), which is what
    scopes the bind to the socket: the caller's context must stay clean.
    """

    async def test_db_error_inside_the_handler_is_captured_with_the_socket_org(
        self,
        capture: object,
        sink: _RecordingSink,
    ) -> None:
        from sqlalchemy.exc import SQLAlchemyError

        from modulo.api.routes.run_ws import run_websocket
        from modulo.core.logging_config import org_id_var

        ws = _FakeWebSocket()
        seen: dict[str, Any] = {}

        async def _loader(_factory: Any, _principal: Any, _run_id: Any) -> Any:
            seen["org_in_handler"] = org_id_var.get()
            raise SQLAlchemyError("db down")

        with (
            patch(
                "modulo.api.routes.run_ws._consume_run_ws_token",
                new=AsyncMock(return_value=_ws_payload()),
            ),
            patch("modulo.api.routes.run_ws._load_run_with_rls", new=_loader),
        ):
            await asyncio.create_task(run_websocket(ws, uuid.uuid4(), token="tok"))
            await _drain()

        # The bind happened at principal resolution — before the failing DB call.
        assert seen["org_in_handler"] == str(_ORG_WS)
        assert "run_ws.run_websocket" in sink.messages
        assert sink.orgs == [str(_ORG_WS)]
        # ...and it was scoped to the connection task, not the caller's context.
        assert org_id_var.get() is None
        assert ws.close_code == 1011

    async def test_escaping_error_logged_by_handle_db_errors_keeps_the_org(
        self,
        capture: object,
        sink: _RecordingSink,
    ) -> None:
        """The decorator logs OUTSIDE the handler frame — the bind must survive to it.

        This pins why there is deliberately no in-body ``finally`` reset: it
        would run before ``handle_db_errors`` and drop exactly the escaping-
        error records (streaming-phase failures) the bind exists to capture.
        """
        from sqlalchemy.exc import SQLAlchemyError

        from modulo.api.routes.run_ws import run_websocket
        from modulo.core.logging_config import org_id_var

        ws = _FakeWebSocket()
        broker = MagicMock()
        broker.subscribe.return_value = asyncio.Queue()

        async def _forward_boom(*_args: Any, **_kwargs: Any) -> None:
            raise SQLAlchemyError("stream down")

        with (
            patch(
                "modulo.api.routes.run_ws._consume_run_ws_token",
                new=AsyncMock(return_value=_ws_payload()),
            ),
            patch(
                "modulo.api.routes.run_ws._load_run_with_rls",
                new=AsyncMock(return_value=_FakeRun("running")),
            ),
            patch(
                "modulo.api.routes.run_ws.get_registry",
                return_value=MagicMock(get_or_create=MagicMock(return_value=broker)),
            ),
            patch("modulo.api.routes.run_ws._forward_run_events", new=_forward_boom),
        ):
            task = asyncio.create_task(run_websocket(ws, uuid.uuid4(), token="tok"))
            with pytest.raises(HTTPException) as excinfo:
                await task
            await _drain()

        assert excinfo.value.status_code == 503
        # The wrapper's own record — logged after the handler frame unwound.
        assert "run_ws.run_websocket.db_error" in sink.messages
        assert sink.orgs == [str(_ORG_WS)]
        # Still scoped to the connection task: the caller's context is clean.
        assert org_id_var.get() is None
