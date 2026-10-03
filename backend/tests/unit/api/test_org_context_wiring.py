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
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest
from fastapi import Depends, FastAPI
from httpx import ASGITransport, AsyncClient

_ORG_ID = uuid.UUID("00000000-0000-0000-0000-000000000041")
_ACCOUNT_ID = uuid.UUID("00000000-0000-0000-0000-000000000042")
_ROUTE_LOGGER = "test.org_context_wiring.route"


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
