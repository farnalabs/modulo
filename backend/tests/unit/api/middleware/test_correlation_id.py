"""Unit tests for modulo.api.middleware.correlation_id.CorrelationIdMiddleware.

QA lens pass (correctness, bugs, maintainability, deps) on the correlation-id
middleware. The happy-path behaviour (UUID v4 generation, response header,
contextvar propagation, per-request uniqueness, backward-compat
``request_id``) was already covered. This pass adds the previously untested
contract around failures and inbound headers:

- the X-Request-ID response header is present even on 4xx/5xx responses;
- the logging contextvar is always cleared when the downstream handler
  raises, so it can never leak into the next request;
- an inbound X-Request-ID is intentionally ignored (the middleware owns the
  id) — locked as the documented behaviour.
"""

import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import pytest
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse
from httpx import ASGITransport, AsyncClient

from modulo.api.middleware.correlation_id import REQUEST_ID_HEADER


@pytest.fixture
def app() -> FastAPI:
    from modulo.api.middleware.correlation_id import CorrelationIdMiddleware

    app = FastAPI()

    @app.get("/test")
    async def correlation_endpoint(request: Request) -> JSONResponse:
        cid = request.state.correlation_id
        assert cid is not None
        parsed = uuid.UUID(cid)
        return JSONResponse(
            {"correlation_id": cid, "uuid_version": parsed.version},
            headers={"X-Custom-CID": cid},
        )

    @app.get("/no-state")
    async def no_state(request: Request) -> JSONResponse:
        return JSONResponse({"ok": True})

    @app.get("/boom")
    async def boom_endpoint() -> JSONResponse:
        raise HTTPException(status_code=500, detail="boom")

    app.add_middleware(CorrelationIdMiddleware)
    return app


@asynccontextmanager
async def _client(app: FastAPI) -> AsyncIterator[AsyncClient]:
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        yield client


@pytest.mark.anyio
async def test_correlation_id_set_on_request_state(app: FastAPI) -> None:
    async with _client(app) as client:
        resp = await client.get("/test")
    assert resp.status_code == 200
    body = resp.json()
    assert "correlation_id" in body
    assert body["uuid_version"] == 4


@pytest.mark.anyio
async def test_correlation_id_in_response_header(app: FastAPI) -> None:
    async with _client(app) as client:
        resp = await client.get("/test")
    assert resp.status_code == 200
    assert REQUEST_ID_HEADER in resp.headers
    cid = resp.headers[REQUEST_ID_HEADER]
    assert uuid.UUID(cid).version == 4


@pytest.mark.anyio
async def test_correlation_id_consistent(app: FastAPI) -> None:
    async with _client(app) as client:
        resp = await client.get("/test")
    body = resp.json()
    header_cid = resp.headers[REQUEST_ID_HEADER]
    assert body["correlation_id"] == header_cid


@pytest.mark.anyio
async def test_correlation_id_contextvar_propagated(app: FastAPI) -> None:
    from modulo.core.logging_config import correlation_id_var

    seen: dict[str, str] = {}

    @app.get("/capture-cid")
    async def capture_cid(request: Request) -> JSONResponse:
        seen["during_request"] = correlation_id_var.get()
        return JSONResponse({"correlation_id": request.state.correlation_id})

    async with _client(app) as client:
        resp = await client.get("/capture-cid")

    cid = resp.json()["correlation_id"]
    # contextvar must be set for the duration of the request...
    assert seen["during_request"] == cid
    # ...and cleared afterwards so it cannot leak across requests
    assert correlation_id_var.get() is None


@pytest.mark.anyio
async def test_unique_per_request(app: FastAPI) -> None:
    async with _client(app) as client:
        resp1 = await client.get("/test")
        resp2 = await client.get("/test")
    cid1 = resp1.json()["correlation_id"]
    cid2 = resp2.json()["correlation_id"]
    assert cid1 != cid2


@pytest.mark.anyio
async def test_correlation_id_on_no_state_route(app: FastAPI) -> None:
    async with _client(app) as client:
        resp = await client.get("/no-state")
    assert resp.status_code == 200
    assert REQUEST_ID_HEADER in resp.headers
    assert uuid.UUID(resp.headers[REQUEST_ID_HEADER]).version == 4


@pytest.mark.anyio
async def test_backward_compat_request_id(app: FastAPI) -> None:
    @app.get("/check-backward")
    async def check_backward(request: Request) -> JSONResponse:
        rid = request.state.request_id
        cid = request.state.correlation_id
        assert rid == cid
        return JSONResponse({"request_id": rid, "correlation_id": cid})

    async with _client(app) as client:
        resp = await client.get("/check-backward")
    body = resp.json()
    assert body["request_id"] == body["correlation_id"]


@pytest.mark.anyio
async def test_response_header_present_on_not_found(app: FastAPI) -> None:
    async with _client(app) as client:
        resp = await client.get("/definitely-not-a-route")
    assert resp.status_code == 404
    assert REQUEST_ID_HEADER in resp.headers
    assert uuid.UUID(resp.headers[REQUEST_ID_HEADER]).version == 4


@pytest.mark.anyio
async def test_response_header_present_on_error(app: FastAPI) -> None:
    async with _client(app) as client:
        resp = await client.get("/boom")
    assert resp.status_code == 500
    assert REQUEST_ID_HEADER in resp.headers
    assert uuid.UUID(resp.headers[REQUEST_ID_HEADER]).version == 4


@pytest.mark.anyio
async def test_contextvar_cleared_when_handler_raises(app: FastAPI) -> None:
    from modulo.core.logging_config import correlation_id_var

    seen: dict[str, str] = {}

    @app.get("/capture-boom")
    async def capture_boom(request: Request) -> JSONResponse:
        seen["during_request"] = correlation_id_var.get()
        raise HTTPException(status_code=400, detail="nope")

    async with _client(app) as client:
        resp = await client.get("/capture-boom")

    assert resp.status_code == 400
    assert seen["during_request"] is not None
    # The contextvar must be reset even when the handler raised.
    assert correlation_id_var.get() is None


@pytest.mark.anyio
async def test_inbound_request_id_header_is_overridden(app: FastAPI) -> None:
    async with _client(app) as client:
        resp = await client.get("/test", headers={REQUEST_ID_HEADER: "client-provided-id"})
    assert resp.status_code == 200
    cid = resp.headers[REQUEST_ID_HEADER]
    # The middleware owns the id: an inbound X-Request-ID is ignored and the
    # response always carries a freshly generated UUID v4.
    assert cid != "client-provided-id"
    assert uuid.UUID(cid).version == 4
    assert resp.json()["correlation_id"] == cid
