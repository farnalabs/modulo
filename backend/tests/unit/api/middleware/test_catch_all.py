"""Unit tests for modulo.api.middleware.catch_all.CatchAllMiddleware.

QA lens pass (correctness, maintainability, dead-code removal) on the catch-all
middleware. Previously untested, this suite locks the full contract:

- a successful request passes through the middleware untouched;
- HTTPExceptions are re-raised (handled by the normal exception handlers), and
  the unhandled-exception counter is left untouched;
- any other exception increments the shared global counter under lock and is
  converted into a structured RFC 9457 500 response, with the request_id (when
  present) propagated into both the body and the X-Request-ID header;
- the best-effort error ingest runs for every swallowed exception, feeding the
  request metadata (method/path/correlation_id/user_id) into the ingest event;
- the ingest helper is itself exception-safe: an ingest failure is logged as
  ``middleware.error_ingest_failed`` and never aborts the 500 response;
- the counter is observable via ``get_unhandled_exception_count``;
- the 500 response falls back to a plain JSON body if even ProblemDetail
  construction fails.
"""

import os
from contextlib import asynccontextmanager
from types import SimpleNamespace
from typing import Any
from unittest import mock

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.responses import JSONResponse
from httpx import ASGITransport, AsyncClient
from starlette.exceptions import HTTPException as StarletteHTTPException

from modulo.api.middleware.catch_all import (
    CatchAllMiddleware,
    _make_500_response,
    get_unhandled_exception_count,
)

_PATH = "modulo.api.middleware.catch_all"


def _request(method: str = "GET", path: str = "/boom", **state: Any) -> SimpleNamespace:
    return SimpleNamespace(
        method=method,
        url=SimpleNamespace(path=path),
        state=SimpleNamespace(**state),
    )


def _reset_counter() -> None:
    import modulo.api.middleware.catch_all as module

    with module._unhandled_count_lock:
        module._unhandled_exception_count = 0


@pytest.fixture(autouse=True)
def _cleanup_counter() -> None:
    _reset_counter()
    yield
    _reset_counter()


class TestDispatch:
    async def test_successful_request_passes_through(self) -> None:
        middleware = CatchAllMiddleware(app=object())

        async def ok(request: object) -> JSONResponse:
            return JSONResponse({"ok": True})

        response = await middleware.dispatch(_request(), ok)
        assert response.status_code == 200
        assert get_unhandled_exception_count() == 0

    async def test_http_exception_is_re_raised_untouched(self, caplog: pytest.LogCaptureFixture) -> None:
        middleware = CatchAllMiddleware(app=object())

        async def boom(request: object) -> JSONResponse:
            raise HTTPException(status_code=404, detail="missing")

        with pytest.raises(HTTPException):
            await middleware.dispatch(_request(), boom)
        assert get_unhandled_exception_count() == 0
        assert not any("middleware.unhandled_exception" in rec.message for rec in caplog.records)

    async def test_swallows_starlette_http_exception_too(self) -> None:
        # FastAPI's HTTPException is caught and re-raised, but the raw Starlette
        # base class is not a subclass of it — so it is treated like any other
        # unhandled exception and converted into a 500. Locked as documented
        # behaviour (in the real ASGI stack Starlette's ExceptionMiddleware
        # converts these to responses before this middleware ever sees them).
        middleware = CatchAllMiddleware(app=object())

        async def boom(request: object) -> JSONResponse:
            raise StarletteHTTPException(status_code=403, detail="forbidden")

        response = await middleware.dispatch(_request(), boom)
        assert response.status_code == 500
        assert get_unhandled_exception_count() == 1

    async def test_unhandled_exception_returns_500_problem_detail(self) -> None:
        import json

        middleware = CatchAllMiddleware(app=object())

        async def boom(request: object) -> JSONResponse:
            raise RuntimeError("kaboom")

        response = await middleware.dispatch(_request(request_id="req-abc"), boom)
        assert response.status_code == 500
        payload = json.loads(response.body)
        assert payload["type"] == "urn:problem:modulo:internal_error"
        assert payload["detail"] == "An unexpected error occurred"
        assert payload["request_id"] == "req-abc"
        assert response.headers["X-Request-ID"] == "req-abc"

    async def test_unhandled_exception_increments_counter(self) -> None:
        middleware = CatchAllMiddleware(app=object())

        async def boom(request: object) -> JSONResponse:
            raise RuntimeError("kaboom")

        await middleware.dispatch(_request(), boom)
        await middleware.dispatch(_request(), boom)
        assert get_unhandled_exception_count() == 2

    async def test_response_without_request_id_omits_header(self) -> None:
        middleware = CatchAllMiddleware(app=object())

        async def boom(request: object) -> JSONResponse:
            raise RuntimeError("kaboom")

        response = await middleware.dispatch(_request(), boom)
        assert response.status_code == 500
        assert "X-Request-ID" not in response.headers

    async def test_ingest_runs_for_swallowed_exception(self) -> None:
        middleware = CatchAllMiddleware(app=object())

        async def boom(request: object) -> JSONResponse:
            raise RuntimeError("kaboom")

        with mock.patch(f"{_PATH}._ingest_unhandled_error", new=mock.AsyncMock()) as ingest:
            response = await middleware.dispatch(
                _request(request_id="req-abc", organisation_id="org-1", user_id="user-1"),
                boom,
            )
        ingest.assert_awaited_once()
        assert response.status_code == 500


class TestIngestUnhandledError:
    async def _fixtures(self) -> tuple[Any, Any, Any, list[tuple[tuple[Any, ...], dict[str, Any]]]]:
        begin_cm = mock.AsyncMock()
        begin_cm.__aenter__ = mock.AsyncMock(return_value=None)
        begin_cm.__aexit__ = mock.AsyncMock(return_value=False)

        session = mock.Mock()
        session.begin.return_value = begin_cm

        factory_cm = mock.AsyncMock()
        factory_cm.__aenter__ = mock.AsyncMock(return_value=session)
        factory_cm.__aexit__ = mock.AsyncMock(return_value=False)
        factory = mock.Mock(return_value=factory_cm)

        ingest_calls: list[tuple[tuple[Any, ...], dict[str, Any]]] = []

        async def recording_ingest(*args: Any, **kwargs: Any) -> None:
            ingest_calls.append((args, kwargs))

        service = SimpleNamespace(ingest=recording_ingest)
        return session, factory, service, ingest_calls

    async def test_ingest_success_with_org(self) -> None:
        from modulo.api.middleware.catch_all import _ingest_unhandled_error
        from modulo.core.logging_config import correlation_id_var

        session, factory, service, ingest_calls = await self._fixtures()
        set_rls = mock.AsyncMock()
        token = correlation_id_var.set("corr-1")

        try:
            with (
                mock.patch("modulo.api.dependencies.get_or_create_engine"),
                mock.patch("modulo.api.dependencies.get_or_create_session_factory", return_value=factory),
                mock.patch("modulo.settings.get_settings", return_value=SimpleNamespace()),
                mock.patch("modulo.db.rls.set_rls_org", set_rls),
                mock.patch("modulo.core.error_tracking.ErrorIngestionService", return_value=service),
            ):
                await _ingest_unhandled_error(
                    _request(method="POST", path="/x", organisation_id="org-1", user_id="user-1")
                )
        finally:
            correlation_id_var.reset(token)

        set_rls.assert_awaited_once_with(session, "org-1")
        assert len(ingest_calls) == 1
        args, _ = ingest_calls[0]
        assert args[0] is session
        assert args[1] == "org-1"
        event = args[2]
        assert event["level"] == "error"
        assert event["message"] == "Unhandled exception: POST /x"
        assert event["source"] == "backend"
        assert event["context_json"]["method"] == "POST"
        assert event["context_json"]["path"] == "/x"
        assert event["context_json"]["correlation_id"] == "corr-1"
        assert event["context_json"]["user_id"] == "user-1"
        assert event["environment"] == os.environ.get("MODULO_ENV", "development")

    async def test_ingest_skips_ingestion_when_no_org(self) -> None:
        from modulo.api.middleware.catch_all import _ingest_unhandled_error

        session, factory, service, ingest_calls = await self._fixtures()
        set_rls = mock.AsyncMock()

        with (
            mock.patch("modulo.api.dependencies.get_or_create_engine"),
            mock.patch("modulo.api.dependencies.get_or_create_session_factory", return_value=factory),
            mock.patch("modulo.settings.get_settings", return_value=SimpleNamespace()),
            mock.patch("modulo.db.rls.set_rls_org", set_rls),
            mock.patch("modulo.core.error_tracking.ErrorIngestionService", return_value=service),
        ):
            await _ingest_unhandled_error(_request(user_id="user-1"))

        set_rls.assert_awaited_once_with(session, None)
        assert ingest_calls == []

    async def test_ingest_without_user_id(self) -> None:
        from modulo.api.middleware.catch_all import _ingest_unhandled_error

        _, factory, service, ingest_calls = await self._fixtures()

        with (
            mock.patch("modulo.api.dependencies.get_or_create_engine"),
            mock.patch("modulo.api.dependencies.get_or_create_session_factory", return_value=factory),
            mock.patch("modulo.settings.get_settings", return_value=SimpleNamespace()),
            mock.patch("modulo.db.rls.set_rls_org", mock.AsyncMock()),
            mock.patch("modulo.core.error_tracking.ErrorIngestionService", return_value=service),
        ):
            await _ingest_unhandled_error(_request(organisation_id="org-1"))

        event = ingest_calls[0][0][2]
        assert event["context_json"]["user_id"] is None

    async def test_ingest_failure_is_logged_and_swallowed(self, caplog: pytest.LogCaptureFixture) -> None:
        from modulo.api.middleware.catch_all import _ingest_unhandled_error

        with (
            mock.patch("modulo.settings.get_settings", side_effect=RuntimeError("settings boom")),
            caplog.at_level("ERROR", logger="modulo.api.middleware.catch_all"),
        ):
            await _ingest_unhandled_error(_request(organisation_id="org-1"))

        assert any("middleware.error_ingest_failed" in rec.message for rec in caplog.records)

    async def test_ingest_failure_within_body_is_swallowed(self, caplog: pytest.LogCaptureFixture) -> None:
        from modulo.api.middleware.catch_all import _ingest_unhandled_error

        _, factory, _, _ = await self._fixtures()

        with (
            mock.patch("modulo.api.dependencies.get_or_create_engine"),
            mock.patch("modulo.api.dependencies.get_or_create_session_factory", return_value=factory),
            mock.patch("modulo.settings.get_settings", return_value=SimpleNamespace()),
            mock.patch("modulo.db.rls.set_rls_org", mock.AsyncMock()),
            mock.patch("modulo.core.error_tracking.ErrorIngestionService", side_effect=RuntimeError("service boom")),
            caplog.at_level("ERROR", logger="modulo.api.middleware.catch_all"),
        ):
            await _ingest_unhandled_error(_request(organisation_id="org-1"))

        assert any("middleware.error_ingest_failed" in rec.message for rec in caplog.records)


class TestMake500Response:
    def test_builds_problem_detail_with_request_id(self) -> None:
        response = _make_500_response("req-xyz")
        assert response.status_code == 500
        assert response.headers["X-Request-ID"] == "req-xyz"

    def test_builds_problem_detail_without_request_id(self) -> None:
        response = _make_500_response(None)
        assert response.status_code == 500
        assert "X-Request-ID" not in response.headers

    def test_falls_back_when_problem_detail_construction_fails(self, caplog: pytest.LogCaptureFixture) -> None:
        with (
            mock.patch(f"{_PATH}.ProblemDetail.from_type", side_effect=RuntimeError("model boom")),
            caplog.at_level("ERROR", logger="modulo.api.middleware.catch_all"),
        ):
            response = _make_500_response("req-xyz")
        assert response.status_code == 500
        assert any("middleware.error_response_failed" in rec.message for rec in caplog.records)


class TestCounterAccessor:
    def test_counter_starts_at_zero(self) -> None:
        assert get_unhandled_exception_count() == 0

    def test_counter_exposed_after_increment(self) -> None:
        import modulo.api.middleware.catch_all as module

        with module._unhandled_count_lock:
            module._unhandled_exception_count = 7
        assert get_unhandled_exception_count() == 7


class TestAsgiIntegration:
    @asynccontextmanager
    async def _client(self, app: FastAPI):
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            yield client

    async def test_pass_through_and_swallow_via_asgi(self) -> None:
        app = FastAPI()

        @app.get("/ok")
        async def ok() -> dict[str, bool]:
            return {"ok": True}

        @app.get("/boom")
        async def boom() -> JSONResponse:
            raise RuntimeError("kaboom")

        app.add_middleware(CatchAllMiddleware)

        async with self._client(app) as client:
            ok_resp = await client.get("/ok")
            boom_resp = await client.get("/boom")

        assert ok_resp.status_code == 200
        assert boom_resp.status_code == 500
        payload = boom_resp.json()
        assert payload["type"] == "urn:problem:modulo:internal_error"
        assert get_unhandled_exception_count() == 1
