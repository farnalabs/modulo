"""Unit tests for modulo.api.exception_handlers.

QA lens pass (correctness, bugs, maintainability, deps) on the three handlers
registered in ``api/main.py`` that shape every error response in the API:
``http_exception_handler``, ``validation_exception_handler``, and
``unhandled_exception_handler``. The handlers are the bridge between the RFC
9457 problem models and Starlette/FastAPI — the last line of defence for
converting exceptions into ProblemDetail responses. These tests lock the bridge
contract (ProblemException fast-path, plain-HTTPException mapping, header
merging, request-id propagation, the validation join, and the 500 fallback that
carries ``request_id`` and ``instance``) so a regression is caught at the unit
layer rather than by a production regression.
"""

import asyncio
import json
import logging
from collections.abc import Awaitable
from typing import Any

import pytest
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from modulo.api.exception_handlers import (
    http_exception_handler,
    unhandled_exception_handler,
    validation_exception_handler,
)
from modulo.api.models.problem import ProblemDetail, ProblemException, ProblemType


class _Request:
    """Minimal stand-in exposing the surface the handlers touch on request."""

    def __init__(self, request_id: str | None = None, path: str = "/test") -> None:
        self.state = type("_State", (), {"request_id": request_id})()
        self.method = "GET"
        self.url = type("_Url", (), {"path": path})()


def _asyncio_run(coro: Awaitable[JSONResponse]) -> JSONResponse:
    return asyncio.run(coro)  # type: ignore[arg-type]


def _run_http(request: _Request, exc: StarletteHTTPException) -> JSONResponse:
    return _asyncio_run(http_exception_handler(request, exc))  # type: ignore[arg-type]


def _run_validation(request: _Request, exc: RequestValidationError) -> JSONResponse:
    return _asyncio_run(validation_exception_handler(request, exc))  # type: ignore[arg-type]


def _run_unhandled(request: _Request, exc: Exception) -> JSONResponse:
    return _asyncio_run(unhandled_exception_handler(request, exc))  # type: ignore[arg-type]


def _body(resp: JSONResponse) -> dict[str, Any]:
    return json.loads(bytes(resp.body))  # type: ignore[no-any-return]


class TestHttpExceptionHandler:
    def test_plain_http_exception_maps_to_problem(self) -> None:
        exc = StarletteHTTPException(status_code=404, detail="gone")
        resp = _run_http(_Request("rid-1"), exc)
        assert resp.status_code == 404
        body = _body(resp)
        assert body["type"] == "urn:problem:modulo:not_found"
        assert body["detail"] == "gone"
        assert body["request_id"] == "rid-1"

    async def test_plain_http_exception_maps_to_problem_detail(self) -> None:
        resp = await http_exception_handler(_Request("rid-1"), StarletteHTTPException(status_code=404, detail="gone"))  # type: ignore[arg-type]
        assert resp.status_code == 404
        body = _body(resp)
        assert body["type"] == "urn:problem:modulo:not_found"
        assert body["title"] == "Not Found"
        assert body["status"] == 404
        assert body["detail"] == "gone"
        assert body["request_id"] == "rid-1"

    def test_http_exception_without_request_id(self) -> None:
        resp = _run_http(_Request(), StarletteHTTPException(status_code=400, detail="bad"))
        body = _body(resp)
        assert "request_id" not in body

    def test_plain_http_exception_merges_headers(self) -> None:
        exc = StarletteHTTPException(status_code=429, detail="slow", headers={"Retry-After": "5"})
        resp = _run_http(_Request("rid-5"), exc)
        assert resp.headers.get("retry-after") == "5"

    async def test_plain_http_exception_carries_headers(self) -> None:
        exc = StarletteHTTPException(status_code=429, detail="slow down", headers={"Retry-After": "30"})
        resp = await http_exception_handler(_Request(), exc)  # type: ignore[arg-type]
        assert resp.status_code == 429
        assert resp.headers.get("retry-after") == "30"

    def test_problem_exception_returns_its_own_problem(self) -> None:
        exc = ProblemException(ProblemType.RATE_LIMITED, detail="slow down", instance="/x")
        resp = _run_http(_Request("rid-2"), exc)
        assert resp.status_code == 429
        body = _body(resp)
        assert body["type"] == "urn:problem:modulo:rate_limited"
        assert body["instance"] == "/x"
        assert body["request_id"] == "rid-2"

    async def test_problem_exception_takes_the_fast_path(self) -> None:
        exc = ProblemException(ProblemType.RATE_LIMITED, detail="slow down", headers={"Retry-After": "5"})
        resp = await http_exception_handler(_Request("rid-9"), exc)  # type: ignore[arg-type]
        assert resp.status_code == 429
        body = _body(resp)
        assert body["type"] == "urn:problem:modulo:rate_limited"
        assert body["title"] == "Rate Limited"
        assert body["detail"] == "slow down"
        assert body["request_id"] == "rid-9"
        assert resp.headers.get("retry-after") == "5"

    async def test_problem_exception_request_id_fills_state_gap(self) -> None:
        exc = ProblemException(ProblemType.FORBIDDEN, detail="no")
        resp = await http_exception_handler(_Request(None), exc)  # type: ignore[arg-type]
        body = _body(resp)
        assert "request_id" not in body

    def test_problem_exception_headers_are_propagated(self) -> None:
        exc = ProblemException(ProblemType.RATE_LIMITED, detail="slow", headers={"Retry-After": "30"})
        resp = _run_http(_Request("rid-3"), exc)
        assert resp.headers.get("retry-after") == "30"

    def test_problem_exception_sets_x_request_id_header(self) -> None:
        exc = ProblemException(ProblemType.BAD_REQUEST, detail="bad")
        resp = _run_http(_Request("rid-4"), exc)
        assert resp.headers.get("x-request-id") == "rid-4"

    async def test_unknown_status_falls_back_to_500(self) -> None:
        resp = await http_exception_handler(_Request(), StarletteHTTPException(status_code=418, detail="teapot"))  # type: ignore[arg-type]
        body = _body(resp)
        assert resp.status_code == 500
        assert body["type"] == "urn:problem:modulo:internal_error"


class TestValidationExceptionHandler:
    def test_returns_422_with_joined_errors(self) -> None:
        errors: list[dict[str, Any]] = [
            {"loc": ("body", "name"), "msg": "field required"},
            {"loc": ("query", "limit"), "msg": "must be <= 100"},
        ]
        exc = RequestValidationError(errors)
        resp = _run_validation(_Request("rid-6"), exc)
        assert resp.status_code == 422
        body = _body(resp)
        assert body["type"] == "urn:problem:modulo:validation_error"
        assert body["detail"] == "body.name: field required; query.limit: must be <= 100"
        assert body["request_id"] == "rid-6"

    async def test_returns_422_problem_with_joined_errors(self) -> None:
        exc = RequestValidationError(errors=[{"loc": ("body", "name"), "msg": "field required"}])
        resp = await validation_exception_handler(_Request("rid-v"), exc)  # type: ignore[arg-type]
        assert resp.status_code == 422
        body = _body(resp)
        assert body["type"] == "urn:problem:modulo:validation_error"
        assert body["title"] == "Validation Error"
        assert body["detail"] == "body.name: field required"
        assert body["request_id"] == "rid-v"

    async def test_joins_multiple_errors(self) -> None:
        exc = RequestValidationError(
            errors=[
                {"loc": ("query", "limit"), "msg": "must be <= 100"},
                {"loc": ("body", "items", 0, "id"), "msg": "invalid"},
            ]
        )
        resp = await validation_exception_handler(_Request(), exc)  # type: ignore[arg-type]
        body = _body(resp)
        assert body["detail"] == "query.limit: must be <= 100; body.items.0.id: invalid"

    def test_empty_errors_use_default_detail(self) -> None:
        exc = RequestValidationError([])
        resp = _run_validation(_Request("rid-7"), exc)
        body = _body(resp)
        assert body["detail"] == "Request validation failed"


class TestUnhandledExceptionHandler:
    def test_returns_500_problem_with_request_id(self) -> None:
        resp = _run_unhandled(_Request("rid-8", path="/boom"), RuntimeError("kaboom"))
        assert resp.status_code == 500
        body = _body(resp)
        assert body["type"] == "urn:problem:modulo:internal_error"
        assert body["instance"] == "/boom"
        assert body["request_id"] == "rid-8"

    async def test_returns_500_problem_with_instance_and_request_id(self) -> None:
        resp = await unhandled_exception_handler(_Request("rid-u", "/crash"), RuntimeError("boom"))  # type: ignore[arg-type]
        assert resp.status_code == 500
        body = _body(resp)
        assert body["type"] == "urn:problem:modulo:internal_error"
        assert body["title"] == "Internal Error"
        assert body["status"] == 500
        assert body["detail"] == "An unexpected error occurred"
        assert body["instance"] == "/crash"
        assert body["request_id"] == "rid-u"

    def test_returns_500_without_request_id(self) -> None:
        resp = _run_unhandled(_Request(path="/boom"), RuntimeError("kaboom"))
        body = _body(resp)
        assert "request_id" not in body

    async def test_request_id_omitted_when_state_missing(self) -> None:
        resp = await unhandled_exception_handler(_Request(None), ValueError("boom"))  # type: ignore[arg-type]
        body = _body(resp)
        assert "request_id" not in body

    def test_logs_exception_with_structured_context(self, caplog: pytest.LogCaptureFixture) -> None:
        with caplog.at_level(logging.ERROR, logger="modulo.api.exception_handlers"):
            _run_unhandled(_Request("rid-9", path="/x"), ValueError("bad value"))
        assert any("exception_handlers.unhandled_exception" in r.getMessage() for r in caplog.records)
        assert any(r.exc_info is not None for r in caplog.records)

    def test_falls_back_when_problem_construction_raises(self, monkeypatch: pytest.MonkeyPatch) -> None:
        def _boom(*args: object, **kwargs: object) -> ProblemDetail:
            raise RuntimeError("cannot even build problem")

        monkeypatch.setattr(
            "modulo.api.exception_handlers.ProblemDetail.from_type",
            classmethod(_boom),
        )
        resp = _run_unhandled(_Request("rid-fallback"), RuntimeError("kaboom"))
        assert resp.status_code == 500
        body = _body(resp)
        assert body["type"] == "urn:problem:modulo:internal_error"
        assert resp.headers.get("x-request-id") == "rid-fallback"
