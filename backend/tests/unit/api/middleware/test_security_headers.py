import pytest
from fastapi import FastAPI
from fastapi.responses import JSONResponse
from httpx import ASGITransport, AsyncClient

from modulo.api.middleware.security_headers import SecurityHeadersMiddleware

EXPECTED_CSP = (
    "default-src 'self'; "
    "connect-src 'self' *.ingest.sentry.io *.datadoghq.com *.dd.dg *.rum.browserevents.com; "
    "script-src 'self' 'unsafe-inline'; "
    "style-src 'self' 'unsafe-inline'; "
    "frame-ancestors 'none'"
)
EXPECTED_HSTS = "max-age=31536000; includeSubDomains"
EXPECTED_XFO = "DENY"
EXPECTED_CTO = "nosniff"
EXPECTED_REFERRER = "strict-origin-when-cross-origin"
EXPECTED_PERMISSIONS = "camera=(), microphone=(), geolocation=()"


@pytest.fixture
def app() -> FastAPI:
    app = FastAPI()

    @app.get("/test")
    async def test_endpoint() -> JSONResponse:
        return JSONResponse({"ok": True})

    app.add_middleware(SecurityHeadersMiddleware)
    return app


@pytest.mark.anyio
async def test_content_security_policy(app: FastAPI) -> None:
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get("/test")
    assert resp.status_code == 200
    assert resp.headers["Content-Security-Policy"] == EXPECTED_CSP


@pytest.mark.anyio
async def test_x_frame_options(app: FastAPI) -> None:
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get("/test")
    assert resp.status_code == 200
    assert resp.headers["X-Frame-Options"] == EXPECTED_XFO


@pytest.mark.anyio
async def test_x_content_type_options(app: FastAPI) -> None:
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get("/test")
    assert resp.status_code == 200
    assert resp.headers["X-Content-Type-Options"] == EXPECTED_CTO


@pytest.mark.anyio
async def test_referrer_policy(app: FastAPI) -> None:
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get("/test")
    assert resp.status_code == 200
    assert resp.headers["Referrer-Policy"] == EXPECTED_REFERRER


@pytest.mark.anyio
async def test_permissions_policy(app: FastAPI) -> None:
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get("/test")
    assert resp.status_code == 200
    assert resp.headers["Permissions-Policy"] == EXPECTED_PERMISSIONS


@pytest.mark.anyio
async def test_all_headers_present(app: FastAPI) -> None:
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get("/test")
    assert resp.status_code == 200
    assert resp.headers["Content-Security-Policy"] == EXPECTED_CSP
    assert resp.headers["X-Frame-Options"] == EXPECTED_XFO
    assert resp.headers["X-Content-Type-Options"] == EXPECTED_CTO
    assert resp.headers["Referrer-Policy"] == EXPECTED_REFERRER
    assert resp.headers["Permissions-Policy"] == EXPECTED_PERMISSIONS


@pytest.mark.anyio
async def test_hsts_sent_in_production(app: FastAPI) -> None:
    """HSTS is sent when debug=False (the default in tests)."""
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get("/test")
    assert resp.headers["Strict-Transport-Security"] == EXPECTED_HSTS
