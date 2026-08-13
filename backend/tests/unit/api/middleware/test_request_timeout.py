"""Unit tests for modulo.api.middleware.request_timeout.RequestTimeoutMiddleware.

QA lens pass (correctness, maintainability) on the request-timeout middleware.
Previously untested, this suite locks the full contract:

- requests completing within the window pass through unchanged;
- a per-path override shortens (or extends) the deadline;
- the first matching override prefix wins, otherwise the default applies;
- a timeout_seconds <= 0 disables the deadline entirely (the request runs to
  completion regardless of duration);
- a timed-out request returns the RFC-style 504 gateway_timeout body and logs
  a structured warning carrying method/path/deadline;
- an asyncio.CancelledError is re-raised, never swallowed into a 504.
"""

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import pytest
from fastapi import FastAPI
from fastapi.responses import JSONResponse
from httpx import ASGITransport, AsyncClient

from modulo.api.middleware.request_timeout import RequestTimeoutMiddleware


@pytest.fixture
def app() -> FastAPI:
    app = FastAPI()

    @app.get("/ok")
    async def ok() -> dict[str, bool]:
        return {"ok": True}

    @app.get("/fast-override")
    async def fast_override() -> dict[str, str]:
        return {"route": "fast-override"}

    @app.get("/slow-override")
    async def slow_override() -> dict[str, str]:
        await asyncio.sleep(5)
        return {"route": "slow-override"}

    @app.get("/no-timeout")
    async def no_timeout() -> dict[str, str]:
        await asyncio.sleep(0.2)
        return {"route": "no-timeout"}

    app.add_middleware(
        RequestTimeoutMiddleware,
        timeout_seconds=1,
        overrides={"/fast-override": 5, "/slow-override": 0.05, "/no-timeout": 0},
    )
    return app


@asynccontextmanager
async def _client(app: FastAPI) -> AsyncIterator[AsyncClient]:
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        yield client


class TestDispatch:
    @pytest.mark.anyio
    async def test_completing_request_passes_through(self, app: FastAPI) -> None:
        async with _client(app) as client:
            resp = await client.get("/ok")
        assert resp.status_code == 200
        assert resp.json() == {"ok": True}

    @pytest.mark.anyio
    async def test_override_extends_deadline(self, app: FastAPI) -> None:
        async with _client(app) as client:
            resp = await client.get("/fast-override")
        assert resp.status_code == 200
        assert resp.json() == {"route": "fast-override"}

    @pytest.mark.anyio
    async def test_timeout_returns_504_gateway_timeout(self, app: FastAPI) -> None:
        async with _client(app) as client:
            resp = await client.get("/slow-override")
        assert resp.status_code == 504
        assert resp.json() == {
            "error": "gateway_timeout",
            "detail": "Request exceeded 0.05s timeout",
        }

    @pytest.mark.anyio
    async def test_timeout_logs_structured_warning(self, app: FastAPI, caplog: pytest.LogCaptureFixture) -> None:
        with caplog.at_level("WARNING", logger="modulo.api.middleware.request_timeout"):
            async with _client(app) as client:
                await client.get("/slow-override")
        assert any(rec.message == "middleware.request_timeout" for rec in caplog.records)
        records = [rec for rec in caplog.records if rec.message == "middleware.request_timeout"]
        assert len(records) == 1
        extra = records[0].__dict__
        assert extra.get("method") == "GET"
        assert extra.get("path") == "/slow-override"
        assert extra.get("timeout_s") == 0.05

    @pytest.mark.anyio
    async def test_zero_timeout_disables_deadline(self, app: FastAPI) -> None:
        async with _client(app) as client:
            resp = await client.get("/no-timeout")
        assert resp.status_code == 200
        assert resp.json() == {"route": "no-timeout"}


class TestTimeoutResolution:
    def test_default_timeout_when_no_override_matches(self) -> None:
        middleware = RequestTimeoutMiddleware(app=object(), timeout_seconds=30)
        assert middleware._timeout_for("/some/path") == 30

    def test_override_wins_for_exact_path(self) -> None:
        middleware = RequestTimeoutMiddleware(app=object(), timeout_seconds=30, overrides={"/healthz": 5})
        assert middleware._timeout_for("/healthz") == 5

    def test_override_wins_for_prefixed_path(self) -> None:
        middleware = RequestTimeoutMiddleware(app=object(), timeout_seconds=30, overrides={"/api/v1": 10})
        assert middleware._timeout_for("/api/v1/runs/42") == 10

    def test_first_override_match_wins(self) -> None:
        middleware = RequestTimeoutMiddleware(
            app=object(),
            timeout_seconds=30,
            overrides={"/api": 10, "/api/v1": 20},
        )
        assert middleware._timeout_for("/api/v1/runs") == 10

    def test_empty_overrides_uses_default(self) -> None:
        middleware = RequestTimeoutMiddleware(app=object(), timeout_seconds=120)
        assert middleware._timeout_for("/") == 120

    def test_overrides_default_when_none_passed(self) -> None:
        middleware = RequestTimeoutMiddleware(app=object(), timeout_seconds=120)
        assert middleware._overrides == {}

    def test_default_timeout_is_120(self) -> None:
        middleware = RequestTimeoutMiddleware(app=object())
        assert middleware._default == 120


class TestCancelledError:
    @pytest.mark.anyio
    async def test_cancelled_error_is_re_raised(self) -> None:
        middleware = RequestTimeoutMiddleware(app=object(), timeout_seconds=5)

        async def cancel(request: object) -> JSONResponse:
            raise asyncio.CancelledError

        request = type("FakeRequest", (), {"url": type("U", (), {"path": "/x"})()})()

        with pytest.raises(asyncio.CancelledError):
            await middleware.dispatch(request, cancel)
