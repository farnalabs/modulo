import uuid

import pytest
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from httpx import ASGITransport, AsyncClient


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
async def test_correlation_id_set_on_request_state(app: FastAPI) -> None:
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get("/test")
    assert resp.status_code == 200
    body = resp.json()
    assert "correlation_id" in body
    assert body["uuid_version"] == 4


@pytest.mark.anyio
async def test_correlation_id_in_response_header(app: FastAPI) -> None:
    from modulo.api.middleware.correlation_id import REQUEST_ID_HEADER

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get("/test")
    assert resp.status_code == 200
    assert REQUEST_ID_HEADER in resp.headers
    cid = resp.headers[REQUEST_ID_HEADER]
    assert uuid.UUID(cid).version == 4


@pytest.mark.anyio
async def test_correlation_id_consistent(app: FastAPI) -> None:
    from modulo.api.middleware.correlation_id import REQUEST_ID_HEADER

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get("/test")
    body = resp.json()
    header_cid = resp.headers[REQUEST_ID_HEADER]
    assert body["correlation_id"] == header_cid


@pytest.mark.anyio
async def test_correlation_id_contextvar_propagated(app: FastAPI) -> None:
    from modulo.core.logging_config import correlation_id_var

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get("/test")
    resp.json()["correlation_id"]
    assert correlation_id_var.get() is None


@pytest.mark.anyio
async def test_unique_per_request(app: FastAPI) -> None:
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp1 = await client.get("/test")
        resp2 = await client.get("/test")
    cid1 = resp1.json()["correlation_id"]
    cid2 = resp2.json()["correlation_id"]
    assert cid1 != cid2


@pytest.mark.anyio
async def test_correlation_id_on_no_state_route(app: FastAPI) -> None:
    from modulo.api.middleware.correlation_id import REQUEST_ID_HEADER

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
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

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get("/check-backward")
    body = resp.json()
    assert body["request_id"] == body["correlation_id"]
