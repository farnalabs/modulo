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
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.requests import Request

from modulo.api.exception_handlers import (
    http_exception_handler,
    unhandled_exception_handler,
    union_allow_header,
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


def _scope_request(
    paths: dict[str, Any],
    path: str = "/api/v1/libraries",
    route: Any = None,
    app: Any = None,
) -> Request:
    """A real ``Request`` whose scope carries a fake FastAPI app + the routed path."""
    if app is None:
        app = SimpleNamespace(openapi=lambda: {"paths": paths})
    scope: dict[str, Any] = {
        "type": "http",
        "asgi": {"version": "3.0", "spec_version": "2.3"},
        "http_version": "1.1",
        "method": "OPTIONS",
        "scheme": "http",
        "path": path,
        "raw_path": path.encode(),
        "query_string": b"",
        "headers": [],
        "server": ("testserver", 80),
        "client": ("testclient", 50000),
        "app": app,
        "route": route,
        "path_params": {},
    }
    return Request(scope)


class TestUnionAllowHeader:
    def test_stub_request_without_scope_yields_none(self) -> None:
        """The handler must stay crash-free for the bare stubs used above."""
        assert union_allow_header(_Request("rid-s")) is None  # type: ignore[arg-type]

    def test_path_absent_from_the_document_yields_none(self) -> None:
        request = _scope_request({"/other": {"get": {}}}, path="/missing")
        assert union_allow_header(request) is None

    def test_non_fastapi_app_yields_none(self) -> None:
        """A plain ASGI app (no ``openapi``) keeps whatever header it produced."""
        request = _scope_request({}, app=SimpleNamespace(routes=[]))
        assert union_allow_header(request) is None

    def test_advertises_every_documented_method_for_the_template(self) -> None:
        """GET-then-POST registration must advertise BOTH methods (RFC 9110 §15.5.6)."""
        paths = {
            "/api/v1/libraries": {"get": {}, "post": {}, "parameters": []},
            "/api/v1/libraries/{primitive_id}": {"get": {}, "delete": {}},
        }
        request = _scope_request(paths)
        assert union_allow_header(request) == "GET, POST"

    def test_undocumented_verbs_are_not_advertised(self) -> None:
        """Only what the document declares — hidden routes can never widen ``Allow``."""
        request = _scope_request({"/api/v1/libraries": {"get": {}}})
        assert union_allow_header(request) == "GET"

    def test_document_entry_without_http_methods_yields_none(self) -> None:
        """A path present in the document but declaring no verb advertises nothing.

        Path-level keys such as ``parameters`` are not HTTP methods, so the
        intersection is empty and the caller keeps the original header.
        """
        request = _scope_request({"/api/v1/libraries": {"parameters": []}})
        assert union_allow_header(request) is None

    def test_path_parameter_template_resolves_through_the_matched_route(self) -> None:
        """The template of the route that matched — not the literal request path."""
        paths = {"/api/v1/libraries/{primitive_id}": {"get": {}, "delete": {}}}
        templated_route = SimpleNamespace(path="/api/v1/libraries/{primitive_id}")
        request = _scope_request(paths, path="/api/v1/libraries/abc", route=templated_route)
        assert union_allow_header(request) == "DELETE, GET"

    def test_schema_generation_failure_yields_none(self) -> None:
        """A document that cannot be built must not mask the original response."""

        def _boom() -> dict[str, Any]:
            raise RuntimeError("schema unavailable")

        request = _scope_request({}, app=SimpleNamespace(openapi=_boom))
        assert union_allow_header(request) is None

    def test_405_response_advertises_the_union(self) -> None:
        """The handler rewrites ``Allow`` on 405; other statuses stay untouched."""
        request = _scope_request({"/api/v1/libraries": {"get": {}, "post": {}}})
        exc = StarletteHTTPException(status_code=405, detail="Method Not Allowed", headers={"Allow": "GET"})
        resp = _asyncio_run(http_exception_handler(request, exc))  # type: ignore[arg-type]
        assert resp.status_code == 405
        assert resp.headers["allow"] == "GET, POST"

    def test_405_without_a_resolvable_document_keeps_the_original_header(self) -> None:
        request = _Request()  # type: ignore[arg-type]
        exc = StarletteHTTPException(status_code=405, detail="Method Not Allowed", headers={"Allow": "HEAD"})
        resp = _asyncio_run(http_exception_handler(request, exc))  # type: ignore[arg-type]
        assert resp.headers["allow"] == "HEAD"

    def test_non_405_headers_are_untouched(self) -> None:
        request = _scope_request({"/test": {"get": {}}}, path="/test")
        exc = StarletteHTTPException(status_code=429, detail="slow", headers={"Retry-After": "5"})
        resp = _asyncio_run(http_exception_handler(request, exc))  # type: ignore[arg-type]
        assert resp.headers.get("retry-after") == "5"
        assert "allow" not in resp.headers


@pytest.fixture
def client() -> TestClient:
    from modulo.api.main import app

    return TestClient(app)


class TestAllowHeaderOnLiveApp:
    """End-to-end: an unsupported method on a multi-verb resource advertises all verbs.

    Locks the regression the nightly ``Schemathesis API Fuzz`` gate caught:
    ``OPTIONS /api/v1/libraries`` returned ``Allow: GET`` while the
    OpenAPI document declares ``GET`` and ``POST`` for that path, so the
    ``allow_header_conformance`` check failed on all five fuzz groups.
    """

    @pytest.mark.parametrize("path", ["/api/v1/libraries", "/api/v1/pipelines"])
    def test_options_405_advertises_every_documented_method(self, client: TestClient, path: str) -> None:
        from modulo.api.main import app

        response = client.request("OPTIONS", path)
        assert response.status_code == 405
        assert response.json()["type"] == "urn:problem:modulo:method_not_allowed"
        advertised = {method.strip().upper() for method in response.headers["allow"].split(",") if method.strip()}
        declared = {
            method.upper()
            for method in app.openapi()["paths"][path]
            if method.upper() in {"GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS", "TRACE"}
        }
        # HEAD/OPTIONS are framework-implied and absent from the document.
        assert declared - {"HEAD", "OPTIONS"} <= advertised
        assert advertised - declared <= {"HEAD", "OPTIONS"}
