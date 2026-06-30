"""Tests for RequestTimeoutMiddleware."""

import asyncio

import pytest
from fastapi import FastAPI
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient

from modulo.api.middleware.request_timeout import RequestTimeoutMiddleware


def _build_app(timeout: int = 2, overrides: dict[str, int] | None = None) -> FastAPI:
    app = FastAPI()

    @app.get("/fast")
    async def fast() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/slow")
    async def slow() -> dict[str, str]:
        await asyncio.sleep(10)
        return {"status": "ok"}

    @app.get("/moderate")
    async def moderate() -> dict[str, str]:
        await asyncio.sleep(0.5)
        return {"status": "ok"}

    app.add_middleware(RequestTimeoutMiddleware, timeout_seconds=timeout, overrides=overrides or {})
    return app


class TestRequestTimeoutMiddleware:
    def test_fast_request_completes(self) -> None:
        app = _build_app(timeout=2)
        client = TestClient(app)
        resp = client.get("/fast")
        assert resp.status_code == 200
        assert resp.json() == {"status": "ok"}

    def test_slow_request_times_out(self) -> None:
        app = _build_app(timeout=1)
        client = TestClient(app)
        resp = client.get("/slow")
        assert resp.status_code == 504
        body = resp.json()
        assert body["error"] == "gateway_timeout"
        assert "timeout" in body["detail"]

    def test_request_completes_within_default_timeout(self) -> None:
        app = _build_app(timeout=5)
        client = TestClient(app)
        resp = client.get("/moderate")
        assert resp.status_code == 200

    def test_path_override_shorter(self) -> None:
        app = _build_app(timeout=5, overrides={"/slow": 1})
        client = TestClient(app)
        resp = client.get("/slow")
        assert resp.status_code == 504

    def test_path_override_longer(self) -> None:
        app = _build_app(timeout=1, overrides={"/slow": 10})
        client = TestClient(app)
        resp = client.get("/fast")
        assert resp.status_code == 200

    def test_response_has_json_content_type(self) -> None:
        app = _build_app(timeout=1)
        client = TestClient(app)
        resp = client.get("/slow")
        assert resp.headers["content-type"] == "application/json"

    def test_default_timeout_does_not_affect_other_routes(self) -> None:
        app = _build_app(timeout=1, overrides={"/moderate": 10})
        client = TestClient(app)
        resp = client.get("/moderate")
        assert resp.status_code == 200


class TestRequestTimeoutMiddlewareEdgeCases:
    def test_timeout_zero_disables_timeout(self) -> None:
        app = FastAPI()

        @app.get("/zero")
        async def zero() -> dict[str, str]:
            return {"status": "ok"}

        app.add_middleware(RequestTimeoutMiddleware, timeout_seconds=0)
        client = TestClient(app)
        resp = client.get("/zero")
        assert resp.status_code == 200

    def test_timeout_response_structure(self) -> None:
        app = _build_app(timeout=1)
        client = TestClient(app)
        resp = client.get("/slow")
        body = resp.json()
        assert "error" in body
        assert "detail" in body
