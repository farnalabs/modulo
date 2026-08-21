"""Unit tests for modulo.api.middleware.security_headers.SecurityHeadersMiddleware.

QA lens pass (correctness, bugs, maintainability, deps) on the security
headers middleware. The happy-path headers were already covered; this pass
adds the configuration-dependent behaviour that was previously untested:

- CSP ``connect-src`` gains ``ws: wss:`` in debug mode and appends
  ``modulo_monitor_domains`` when configured;
- HSTS is suppressed in debug mode;
- headers are applied to error (4xx/5xx) responses too, so clients always
  receive the security surface regardless of status.
"""

from unittest.mock import patch

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.responses import JSONResponse
from httpx import ASGITransport, AsyncClient

from modulo.settings import Settings

EXPECTED_CSP = (
    "default-src 'self'; "
    "connect-src 'self' *.ingest.sentry.io *.datadoghq.com *.dd.dg *.rum.browserevents.com; "
    "script-src 'self' 'unsafe-inline'; "
    "style-src 'self' 'unsafe-inline'; "
    "img-src 'self' data:; "
    "frame-src 'self'; "
    "frame-ancestors 'none'"
)
EXPECTED_HSTS = "max-age=31536000; includeSubDomains"
EXPECTED_XFO = "DENY"
EXPECTED_CTO = "nosniff"
EXPECTED_REFERRER = "strict-origin-when-cross-origin"
EXPECTED_PERMISSIONS = "camera=(), microphone=(), geolocation=()"


def _make_settings(**overrides: object) -> Settings:
    return Settings(
        database_url="postgresql+asyncpg://localhost/test",
        secret_key="a" * 32,
        fernet_key="a" * 32,
        modulo_admin_password="testpass",
        **overrides,
    )


def _build_app(settings: Settings) -> FastAPI:
    from modulo.api.middleware.security_headers import SecurityHeadersMiddleware

    app = FastAPI()

    @app.get("/test")
    async def health_endpoint() -> JSONResponse:
        return JSONResponse({"ok": True})

    @app.get("/not-found")
    async def not_found_endpoint() -> JSONResponse:
        return JSONResponse({"error": "missing"}, status_code=404)

    @app.get("/boom")
    async def boom_endpoint() -> JSONResponse:
        raise HTTPException(status_code=500, detail="boom")

    app.add_middleware(SecurityHeadersMiddleware)
    return app


@pytest.fixture
def app() -> FastAPI:
    settings = _make_settings()
    with patch("modulo.api.middleware.security_headers.get_settings", return_value=settings):
        return _build_app(settings)


async def _get(app: FastAPI, path: str = "/test"):
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        return await client.get(path)


@pytest.mark.anyio
async def test_content_security_policy(app: FastAPI) -> None:
    resp = await _get(app)
    assert resp.status_code == 200
    assert resp.headers["Content-Security-Policy"] == EXPECTED_CSP


@pytest.mark.anyio
async def test_x_frame_options(app: FastAPI) -> None:
    resp = await _get(app)
    assert resp.status_code == 200
    assert resp.headers["X-Frame-Options"] == EXPECTED_XFO


@pytest.mark.anyio
async def test_x_content_type_options(app: FastAPI) -> None:
    resp = await _get(app)
    assert resp.status_code == 200
    assert resp.headers["X-Content-Type-Options"] == EXPECTED_CTO


@pytest.mark.anyio
async def test_referrer_policy(app: FastAPI) -> None:
    resp = await _get(app)
    assert resp.status_code == 200
    assert resp.headers["Referrer-Policy"] == EXPECTED_REFERRER


@pytest.mark.anyio
async def test_permissions_policy(app: FastAPI) -> None:
    resp = await _get(app)
    assert resp.status_code == 200
    assert resp.headers["Permissions-Policy"] == EXPECTED_PERMISSIONS


@pytest.mark.anyio
async def test_all_headers_present(app: FastAPI) -> None:
    resp = await _get(app)
    assert resp.status_code == 200
    assert resp.headers["Content-Security-Policy"] == EXPECTED_CSP
    assert resp.headers["X-Frame-Options"] == EXPECTED_XFO
    assert resp.headers["X-Content-Type-Options"] == EXPECTED_CTO
    assert resp.headers["Referrer-Policy"] == EXPECTED_REFERRER
    assert resp.headers["Permissions-Policy"] == EXPECTED_PERMISSIONS


@pytest.mark.anyio
async def test_hsts_sent_in_production(app: FastAPI) -> None:
    """HSTS is sent when debug=False (the default in tests)."""
    resp = await _get(app)
    assert resp.headers["Strict-Transport-Security"] == EXPECTED_HSTS


@pytest.mark.anyio
async def test_debug_mode_appends_ws_to_csp_connect_src() -> None:
    settings = _make_settings(debug=True)
    with patch("modulo.api.middleware.security_headers.get_settings", return_value=settings):
        app = _build_app(settings)
        resp = await _get(app)

    assert resp.status_code == 200
    csp = resp.headers["Content-Security-Policy"]
    assert "connect-src 'self' *.ingest.sentry.io *.datadoghq.com *.dd.dg *.rum.browserevents.com ws: wss:;" in csp
    assert "default-src 'self'" in csp
    assert "script-src 'self' 'unsafe-inline'" in csp
    assert "frame-ancestors 'none'" in csp


@pytest.mark.anyio
async def test_hsts_not_sent_in_debug_mode() -> None:
    settings = _make_settings(debug=True)
    with patch("modulo.api.middleware.security_headers.get_settings", return_value=settings):
        app = _build_app(settings)
        resp = await _get(app)

    assert resp.status_code == 200
    assert "Strict-Transport-Security" not in resp.headers


@pytest.mark.anyio
async def test_monitor_domains_appended_to_csp_connect_src() -> None:
    settings = _make_settings(modulo_monitor_domains="https://monitor.internal *.ingest.modulo.dev")
    with patch("modulo.api.middleware.security_headers.get_settings", return_value=settings):
        app = _build_app(settings)
        resp = await _get(app)

    assert resp.status_code == 200
    csp = resp.headers["Content-Security-Policy"]
    assert "https://monitor.internal *.ingest.modulo.dev" in csp
    assert "connect-src 'self' *.ingest.sentry.io *.datadoghq.com *.dd.dg *.rum.browserevents.com" in csp


@pytest.mark.anyio
async def test_headers_applied_to_not_found_response(app: FastAPI) -> None:
    resp = await _get(app, path="/not-found")
    assert resp.status_code == 404
    assert resp.headers["Content-Security-Policy"] == EXPECTED_CSP
    assert resp.headers["X-Frame-Options"] == EXPECTED_XFO
    assert resp.headers["X-Content-Type-Options"] == EXPECTED_CTO
    assert resp.headers["Referrer-Policy"] == EXPECTED_REFERRER
    assert resp.headers["Permissions-Policy"] == EXPECTED_PERMISSIONS


@pytest.mark.anyio
async def test_headers_applied_to_error_response(app: FastAPI) -> None:
    resp = await _get(app, path="/boom")
    assert resp.status_code == 500
    assert resp.headers["Content-Security-Policy"] == EXPECTED_CSP
    assert resp.headers["X-Frame-Options"] == EXPECTED_XFO
    assert resp.headers["X-Content-Type-Options"] == EXPECTED_CTO
    assert resp.headers["Referrer-Policy"] == EXPECTED_REFERRER
    assert resp.headers["Permissions-Policy"] == EXPECTED_PERMISSIONS
