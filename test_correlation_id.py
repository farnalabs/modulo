import uuid

import pytest
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse


@pytest.fixture
def app() -> FastAPI:
    from modulo.api.middleware.correlation_id import CorrelationIdMiddleware

    app = FastAPI()

    @app.get("/test")
    async def test_endpoint(request: Request) -> JSONResponse:
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

    app.add_middleware(CorrelationIdMiddleware)
    return app


@pytest.mark.anyio
async def test_correlation_id_set_on_request_state(app, async_client) -> None:
    resp = await async_client.get("/test")
    body = resp.json()
    assert "correlation_id" in body
    assert body["uuid_version"] == 4


@pytest.mark.anyio
async def test_correlation_id_in_response_header(app, async_client) -> None:
    from modulo.api.middleware.correlation_id import REQUEST_ID_HEADER

    resp = await async_client.get("/test")
    assert REQUEST_ID_HEADER in resp.headers
    assert uuid.UUID(resp.headers[REQUEST_ID_HEADER]).version == 4


@pytest.mark.anyio
async def test_correlation_id_consistent(app, async_client) -> None:
    from modulo.api.middleware.correlation_id import REQUEST_ID_HEADER

    resp = await async_client.get("/test")
    body = resp.json()
    assert body["correlation_id"] == resp.headers[REQUEST_ID_HEADER]


@pytest.mark.anyio
async def test_correlation_id_contextvar_propagated(app, async_client) -> None:
    from modulo.core.logging_config import correlation_id_var

    resp = await async_client.get("/test")
    assert resp.json()["correlation_id"] is not None
    assert correlation_id_var.get() is None


@pytest.mark.anyio
async def test_unique_per_request(app, async_client) -> None:
    resp1 = await async_client.get("/test")
    resp2 = await async_client.get("/test")
    assert resp1.json()["correlation_id"] != resp2.json()["correlation_id"]


@pytest.mark.anyio
async def test_correlation_id_on_no_state_route(app, async_client) -> None:
    from modulo.api.middleware.correlation_id import REQUEST_ID_HEADER

    resp = await async_client.get("/no-state")
    assert REQUEST_ID_HEADER in resp.headers
    assert uuid.UUID(resp.headers[REQUEST_ID_HEADER]).version == 4


@pytest.mark.anyio
async def test_backward_compat_request_id(app, async_client) -> None:
    @app.get("/check-backward")
    async def check_backward(request: Request) -> JSONResponse:
        rid = request.state.request_id
        cid = request.state.correlation_id
        assert rid == cid
        return JSONResponse({"request_id": rid, "correlation_id": cid})

    resp = await async_client.get("/check-backward")
    body = resp.json()
    assert body["request_id"] == body["correlation_id"]
