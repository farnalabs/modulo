"""Unit tests for modulo.api.middleware.cors_logging.CorsLoggingMiddleware.

QA lens pass (correctness, bugs, maintainability, deps) on the CORS
middleware subclass. The module has a single consumer (``modulo.api.main``)
and previously had no dedicated unit test file — the CORS allow/reject
behaviour and the added logging (preflight DEBUG, rejected-origin WARNING)
were only exercised indirectly. These tests lock the Starlette CORS contract
(preflight 200/400, allow-origin header only for allowed origins) and the
extra logging this subclass adds on top of it.
"""

import logging

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

_LOGGER = "modulo.api.middleware.cors_logging"

_ALLOWED = "https://allowed.example"
_DENIED = "https://evil.example"


@pytest.fixture
def app() -> FastAPI:
    from modulo.api.middleware.cors_logging import CorsLoggingMiddleware

    app = FastAPI()

    @app.get("/hello")
    async def hello() -> dict[str, bool]:
        return {"ok": True}

    app.add_middleware(
        CorsLoggingMiddleware,
        allow_origins=[_ALLOWED],
        allow_methods=["*"],
        allow_headers=["*"],
        allow_credentials=True,
    )
    return app


async def _get(app: FastAPI, path: str, headers: dict[str, str]) -> object:
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        return await client.request("GET", path, headers=headers)


async def _options(app: FastAPI, path: str, headers: dict[str, str]) -> object:
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        return await client.request("OPTIONS", path, headers=headers)


class TestCorsLoggingMiddleware:
    async def test_preflight_allowed_origin_returns_cors_headers(self, app: FastAPI) -> None:
        resp = await _options(
            app,
            "/hello",
            {"Origin": _ALLOWED, "Access-Control-Request-Method": "GET"},
        )
        assert resp.status_code == 200
        assert resp.headers["access-control-allow-origin"] == _ALLOWED
        assert resp.headers["access-control-allow-credentials"] == "true"

    async def test_preflight_allowed_origin_logs_debug(self, app: FastAPI, caplog: pytest.LogCaptureFixture) -> None:
        with caplog.at_level(logging.DEBUG, logger=_LOGGER):
            resp = await _options(
                app,
                "/hello",
                {"Origin": _ALLOWED, "Access-Control-Request-Method": "GET"},
            )
        assert resp.status_code == 200
        assert any("CORS preflight" in record.message for record in caplog.records)

    async def test_preflight_denied_origin_rejected_and_warns(
        self, app: FastAPI, caplog: pytest.LogCaptureFixture
    ) -> None:
        with caplog.at_level(logging.WARNING, logger=_LOGGER):
            resp = await _options(
                app,
                "/hello",
                {"Origin": _DENIED, "Access-Control-Request-Method": "GET"},
            )
        assert resp.status_code == 400
        assert "access-control-allow-origin" not in resp.headers
        assert any("CORS rejected" in record.message for record in caplog.records)
        assert any(_DENIED in record.message for record in caplog.records)

    async def test_denied_origin_warning_includes_method_and_path(
        self, app: FastAPI, caplog: pytest.LogCaptureFixture
    ) -> None:
        with caplog.at_level(logging.WARNING, logger=_LOGGER):
            await _options(
                app,
                "/hello",
                {"Origin": _DENIED, "Access-Control-Request-Method": "GET"},
            )
        warnings = [r for r in caplog.records if "CORS rejected" in r.message]
        assert warnings
        assert "method=OPTIONS" in warnings[0].message
        assert "path=/hello" in warnings[0].message

    async def test_simple_request_allowed_origin_has_cors_headers(self, app: FastAPI) -> None:
        resp = await _get(app, "/hello", {"Origin": _ALLOWED})
        assert resp.status_code == 200
        assert resp.headers["access-control-allow-origin"] == _ALLOWED

    async def test_simple_request_denied_origin_warns_but_passes(
        self, app: FastAPI, caplog: pytest.LogCaptureFixture
    ) -> None:
        with caplog.at_level(logging.WARNING, logger=_LOGGER):
            resp = await _get(app, "/hello", {"Origin": _DENIED})
        assert resp.status_code == 200
        assert resp.json() == {"ok": True}
        assert "access-control-allow-origin" not in resp.headers
        assert any("CORS rejected" in record.message for record in caplog.records)

    async def test_request_without_origin_passes_through_without_logs(
        self, app: FastAPI, caplog: pytest.LogCaptureFixture
    ) -> None:
        with caplog.at_level(logging.DEBUG, logger=_LOGGER):
            resp = await _get(app, "/hello", {})
        assert resp.status_code == 200
        assert "access-control-allow-origin" not in resp.headers
        assert not any(record.name == _LOGGER for record in caplog.records)

    async def test_preflight_without_origin_is_not_logged_as_preflight(
        self, app: FastAPI, caplog: pytest.LogCaptureFixture
    ) -> None:
        with caplog.at_level(logging.DEBUG, logger=_LOGGER):
            resp = await _options(app, "/hello", {})
        assert resp.status_code == 405
        assert not any("CORS preflight" in record.message for record in caplog.records)


class TestCorsLoggingMiddlewareNonHttp:
    async def test_non_http_scope_is_forwarded_untouched(self) -> None:
        from modulo.api.middleware.cors_logging import CorsLoggingMiddleware

        scopes_seen: list[str] = []

        async def inner_app(scope: object, receive: object, send: object) -> None:
            scopes_seen.append(scope["type"])  # type: ignore[index]

        middleware = CorsLoggingMiddleware(  # type: ignore[arg-type]
            inner_app,  # type: ignore[arg-type]
            allow_origins=[_ALLOWED],
            allow_methods=["*"],
            allow_headers=["*"],
        )
        scope = {
            "type": "websocket",
            "path": "/ws",
            "headers": [(b"origin", _DENIED.encode())],
            "query_string": b"",
            "client": ("127.0.0.1", 12345),
            "server": ("testserver", 80),
            "scheme": "ws",
            "subprotocols": [],
            "state": {},
        }
        await middleware(scope, lambda: {"type": "websocket.connect"}, lambda _: None)  # type: ignore[arg-type]

        assert scopes_seen == ["websocket"]
