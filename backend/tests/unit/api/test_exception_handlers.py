"""Unit tests for modulo.api.exception_handlers — FastAPI exception handlers.

QA lens pass (correctness, bugs, maintainability, deps) on the handlers that
every API error response flows through. ``http_exception_handler``,
``validation_exception_handler`` and ``unhandled_exception_handler`` are
registered on the FastAPI app in ``modulo.api.main`` and are the last line of
defence for converting exceptions into RFC 9457 ProblemDetail responses. These
tests lock the ProblemException fast-path, the plain-HTTPException mapping, the
validation join, and the 500 fallback that carries ``request_id`` and
``instance``.
"""

import json
from typing import Any

from fastapi.exceptions import RequestValidationError
from starlette.exceptions import HTTPException as StarletteHTTPException

from modulo.api.exception_handlers import (
    http_exception_handler,
    unhandled_exception_handler,
    validation_exception_handler,
)
from modulo.api.models.problem import ProblemException, ProblemType


class _Request:
    """Minimal stand-in exposing the fields the handlers read."""

    def __init__(self, request_id: str | None = None, path: str = "/x") -> None:
        self.state = type("_State", (), {"request_id": request_id})()
        self.url = type("_URL", (), {"path": path})()
        self.method = "GET"


def _body(resp: Any) -> Any:
    """Decode a JSONResponse body regardless of bytes/memoryview type."""
    return json.loads(bytes(resp.body))


class TestHttpExceptionHandler:
    async def test_plain_http_exception_maps_to_problem_detail(self) -> None:
        resp = await http_exception_handler(_Request("rid-1"), StarletteHTTPException(status_code=404, detail="gone"))
        assert resp.status_code == 404
        body = _body(resp)
        assert body["type"] == "urn:problem:modulo:not_found"
        assert body["title"] == "Not Found"
        assert body["status"] == 404
        assert body["detail"] == "gone"
        assert body["request_id"] == "rid-1"

    async def test_plain_http_exception_carries_headers(self) -> None:
        exc = StarletteHTTPException(status_code=429, detail="slow down", headers={"Retry-After": "30"})
        resp = await http_exception_handler(_Request(), exc)
        assert resp.status_code == 429
        assert resp.headers.get("retry-after") == "30"

    async def test_problem_exception_takes_the_fast_path(self) -> None:
        exc = ProblemException(ProblemType.RATE_LIMITED, detail="slow down", headers={"Retry-After": "5"})
        resp = await http_exception_handler(_Request("rid-9"), exc)
        assert resp.status_code == 429
        body = _body(resp)
        assert body["type"] == "urn:problem:modulo:rate_limited"
        assert body["title"] == "Rate Limited"
        assert body["detail"] == "slow down"
        assert body["request_id"] == "rid-9"
        assert resp.headers.get("retry-after") == "5"

    async def test_problem_exception_request_id_fills_state_gap(self) -> None:
        exc = ProblemException(ProblemType.FORBIDDEN, detail="no")
        resp = await http_exception_handler(_Request(None), exc)
        body = _body(resp)
        assert "request_id" not in body

    async def test_unknown_status_falls_back_to_500(self) -> None:
        resp = await http_exception_handler(_Request(), StarletteHTTPException(status_code=418, detail="teapot"))
        body = _body(resp)
        assert resp.status_code == 500
        assert body["type"] == "urn:problem:modulo:internal_error"


class TestValidationExceptionHandler:
    async def test_returns_422_problem_with_joined_errors(self) -> None:
        exc = RequestValidationError(errors=[{"loc": ("body", "name"), "msg": "field required"}])
        resp = await validation_exception_handler(_Request("rid-v"), exc)
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
        resp = await validation_exception_handler(_Request(), exc)
        body = _body(resp)
        assert body["detail"] == "query.limit: must be <= 100; body.items.0.id: invalid"


class TestUnhandledExceptionHandler:
    async def test_returns_500_problem_with_instance_and_request_id(self) -> None:
        resp = await unhandled_exception_handler(_Request("rid-u", "/crash"), RuntimeError("boom"))
        assert resp.status_code == 500
        body = _body(resp)
        assert body["type"] == "urn:problem:modulo:internal_error"
        assert body["title"] == "Internal Error"
        assert body["status"] == 500
        assert body["detail"] == "An unexpected error occurred"
        assert body["instance"] == "/crash"
        assert body["request_id"] == "rid-u"

    async def test_request_id_omitted_when_state_missing(self) -> None:
        resp = await unhandled_exception_handler(_Request(None), ValueError("boom"))
        body = _body(resp)
        assert "request_id" not in body
