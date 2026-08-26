"""Unit tests for modulo.api.models.problem — RFC 9457 Problem Details.

QA lens pass (correctness, bugs, maintainability, deps) on the problem-detail
models that every API error response is built from. The module has 5+ consumers
(exception_handlers, catch_all, rate_limiter, dependencies, scim_auth) but no
dedicated unit test file — the contract was only exercised indirectly through
end-to-end endpoint assertions. These tests lock the RFC 9457 shape, the
status/title mapping table, the ProblemException HTTP surface, and the
fallback safety net.
"""

import json
from typing import Any

import pytest
from starlette.exceptions import HTTPException

from modulo.api.models.problem import (
    ProblemDetail,
    ProblemException,
    ProblemType,
    problem_from_http_exception,
    problem_from_validation_error,
)


class _Request:
    """Minimal stand-in exposing only ``request.state.request_id``.

    Handlers read ``request.state.request_id`` via getattr and never touch
    anything else on the request, so a plain stand-in is a faithful double.
    """

    def __init__(self, request_id: str | None = None) -> None:
        state = type("_State", (), {"request_id": request_id})
        self.state = state()


def _problem_from_http_exception(request_id: str | None, status: int, detail: str | None) -> ProblemDetail:
    """Call problem_from_http_exception with a stand-in request."""
    return problem_from_http_exception(_Request(request_id), HTTPException(status_code=status, detail=detail))  # type: ignore[arg-type]


def _problem_from_validation_error(request_id: str | None, errors: list[dict[str, Any]]) -> ProblemDetail:
    """Call problem_from_validation_error with a stand-in request."""
    return problem_from_validation_error(_Request(request_id), errors)  # type: ignore[arg-type]


def _body(resp: Any) -> Any:
    """Decode a JSONResponse body regardless of bytes/memoryview type."""
    return json.loads(bytes(resp.body))


@pytest.mark.parametrize(
    ("problem_type", "expected_status", "expected_title"),
    [
        (ProblemType.BAD_REQUEST, 400, "Bad Request"),
        (ProblemType.VALIDATION_ERROR, 422, "Validation Error"),
        (ProblemType.UNAUTHORIZED, 401, "Unauthorized"),
        (ProblemType.FORBIDDEN, 403, "Forbidden"),
        (ProblemType.NOT_FOUND, 404, "Not Found"),
        (ProblemType.CONFLICT, 409, "Conflict"),
        (ProblemType.GONE, 410, "Gone"),
        (ProblemType.METHOD_NOT_ALLOWED, 405, "Method Not Allowed"),
        (ProblemType.RATE_LIMITED, 429, "Rate Limited"),
        (ProblemType.FEATURE_REQUIRED, 402, "Feature Not Available"),
        (ProblemType.PIPELINE_ERROR, 500, "Pipeline Error"),
        (ProblemType.MIGRATION_REQUIRED, 501, "Migration Required"),
        (ProblemType.BAD_GATEWAY, 502, "Bad Gateway"),
        (ProblemType.SERVICE_UNAVAILABLE, 503, "Service Unavailable"),
        (ProblemType.GATEWAY_TIMEOUT, 504, "Gateway Timeout"),
        (ProblemType.INTERNAL_ERROR, 500, "Internal Error"),
    ],
    ids=(
        "bad_request",
        "validation_error",
        "unauthorized",
        "forbidden",
        "not_found",
        "conflict",
        "gone",
        "method_not_allowed",
        "rate_limited",
        "feature_required",
        "pipeline_error",
        "migration_required",
        "bad_gateway",
        "service_unavailable",
        "gateway_timeout",
        "internal_error",
    ),
)
def test_problem_type_metadata(problem_type: ProblemType, expected_status: int, expected_title: str) -> None:
    problem = ProblemDetail.from_type(problem_type, detail="d")
    assert problem.type == f"urn:problem:modulo:{problem_type.value}"
    assert problem.status == expected_status
    assert problem.title == expected_title


class TestProblemDetail:
    def test_rfc9457_field_surface(self) -> None:
        problem = ProblemDetail.from_type(ProblemType.NOT_FOUND, detail="gone", instance="/x/1", request_id="rid")
        assert problem.detail == "gone"
        assert problem.instance == "/x/1"
        assert problem.request_id == "rid"

    def test_instance_and_request_id_default_to_none(self) -> None:
        problem = ProblemDetail.from_type(ProblemType.NOT_FOUND, detail="gone")
        assert problem.instance is None
        assert problem.request_id is None

    def test_to_response_returns_problem_status_and_rfc9457_body(self) -> None:
        problem = ProblemDetail.from_type(ProblemType.CONFLICT, detail="boom", instance="/x")
        resp = problem.to_response()
        assert resp.status_code == 409
        body = _body(resp)
        assert body["type"] == "urn:problem:modulo:conflict"
        assert body["title"] == "Conflict"
        assert body["status"] == 409
        assert body["detail"] == "boom"
        assert body["instance"] == "/x"

    def test_to_response_excludes_none_fields_from_body(self) -> None:
        problem = ProblemDetail.from_type(ProblemType.CONFLICT, detail="boom")
        body = _body(problem.to_response())
        assert "instance" not in body
        assert "request_id" not in body

    def test_to_response_sets_x_request_id_header_only_when_request_id_present(self) -> None:
        with_rid = ProblemDetail.from_type(ProblemType.BAD_REQUEST, detail="d", request_id="rid-1")
        assert with_rid.to_response().headers.get("x-request-id") == "rid-1"

        without_rid = ProblemDetail.from_type(ProblemType.BAD_REQUEST, detail="d")
        assert without_rid.to_response().headers.get("x-request-id") is None

    def test_to_response_merges_headers_but_does_not_override_caller(self) -> None:
        problem = ProblemDetail.from_type(ProblemType.BAD_REQUEST, detail="d", request_id="rid-1")
        resp = problem.to_response(headers={"X-Request-ID": "caller-rid", "Retry-After": "30"})
        assert resp.headers.get("x-request-id") == "caller-rid"
        assert resp.headers.get("retry-after") == "30"

    def test_serializes_plain_json(self) -> None:
        problem = ProblemDetail.from_type(ProblemType.FORBIDDEN, detail="no", request_id="r")
        resp = problem.to_response()
        assert resp.headers.get("content-type") == "application/json"

    def test_fallback_internal_error_never_raises(self) -> None:
        resp = ProblemDetail.fallback_internal_error("rid-fallback")
        assert resp.status_code == 500
        body = _body(resp)
        assert body["type"] == "urn:problem:modulo:internal_error"
        assert body["title"] == "Internal Error"
        assert body["status"] == 500
        assert body["detail"] == "An unexpected error occurred"
        assert resp.headers.get("x-request-id") == "rid-fallback"

    def test_fallback_internal_error_without_request_id(self) -> None:
        resp = ProblemDetail.fallback_internal_error()
        assert resp.status_code == 500
        assert resp.headers.get("x-request-id") == ""


class TestProblemException:
    def test_exposes_problem_and_http_status(self) -> None:
        exc = ProblemException(ProblemType.RATE_LIMITED, detail="slow down")
        assert isinstance(exc, HTTPException)
        assert exc.status_code == 429
        assert exc.detail == "slow down"
        assert exc.problem.type == "urn:problem:modulo:rate_limited"
        assert exc.problem.status == 429

    def test_sets_instance_and_headers(self) -> None:
        exc = ProblemException(ProblemType.FORBIDDEN, detail="no", instance="/y", headers={"Retry-After": "5"})
        assert exc.problem.instance == "/y"
        assert exc.headers == {"Retry-After": "5"}

    def test_raise_and_catch(self) -> None:
        with pytest.raises(ProblemException) as excinfo:
            raise ProblemException(ProblemType.BAD_REQUEST, detail="bad")
        assert excinfo.value.problem.status == 400


class TestProblemFromHttpException:
    def test_maps_known_status_codes(self) -> None:
        for status, expected in {
            400: ProblemType.BAD_REQUEST,
            401: ProblemType.UNAUTHORIZED,
            402: ProblemType.FEATURE_REQUIRED,
            403: ProblemType.FORBIDDEN,
            404: ProblemType.NOT_FOUND,
            405: ProblemType.METHOD_NOT_ALLOWED,
            409: ProblemType.CONFLICT,
            410: ProblemType.GONE,
            422: ProblemType.VALIDATION_ERROR,
            429: ProblemType.RATE_LIMITED,
            501: ProblemType.MIGRATION_REQUIRED,
            502: ProblemType.BAD_GATEWAY,
            503: ProblemType.SERVICE_UNAVAILABLE,
            504: ProblemType.GATEWAY_TIMEOUT,
        }.items():
            problem = _problem_from_http_exception("r", status, "d")
            assert problem.type == f"urn:problem:modulo:{expected.value}", f"status {status}"
            assert problem.status == status

    def test_504_maps_to_gateway_timeout_not_internal_error(self) -> None:
        problem = _problem_from_http_exception("r", 504, "Connector sampling timed out after 30s")
        assert problem.type == "urn:problem:modulo:gateway_timeout"
        assert problem.title == "Gateway Timeout"
        assert problem.status == 504
        assert problem.detail == "Connector sampling timed out after 30s"

    def test_unknown_status_falls_back_to_internal_error_500(self) -> None:
        problem = _problem_from_http_exception(None, 418, "teapot")
        assert problem.type == "urn:problem:modulo:internal_error"
        assert problem.status == 500

    def test_uses_request_id_from_request_state(self) -> None:
        problem = _problem_from_http_exception("rid-42", 404, "nope")
        assert problem.request_id == "rid-42"

    def test_request_id_none_when_state_missing(self) -> None:
        problem = _problem_from_http_exception(None, 404, "nope")
        assert problem.request_id is None

    def test_extracts_nested_detail_from_dict(self) -> None:
        exc = HTTPException(status_code=422, detail={"detail": "bad json body", "extra": 1})  # type: ignore[arg-type]
        problem = problem_from_http_exception(_Request(), exc)  # type: ignore[arg-type]
        assert problem.detail == "bad json body"

    def test_string_detail_passthrough(self) -> None:
        problem = _problem_from_http_exception(None, 400, "plain message")
        assert problem.detail == "plain message"

    def test_dict_detail_without_detail_key_uses_repr_of_dict(self) -> None:
        exc = HTTPException(status_code=422, detail={"field": "name"})  # type: ignore[arg-type]
        problem = problem_from_http_exception(_Request(), exc)  # type: ignore[arg-type]
        assert problem.detail == str({"field": "name"})


class TestProblemFromValidationError:
    def test_joins_loc_and_msg(self) -> None:
        errors: list[dict[str, Any]] = [
            {"loc": ("body", "name"), "msg": "field required"},
            {"loc": ("query", "limit"), "msg": "must be <= 100"},
        ]
        problem = _problem_from_validation_error(None, errors)
        assert problem.type == "urn:problem:modulo:validation_error"
        assert problem.status == 422
        assert problem.detail == "body.name: field required; query.limit: must be <= 100"

    def test_handles_nested_and_integer_loc_segments(self) -> None:
        errors: list[dict[str, Any]] = [{"loc": ("items", 0, "id"), "msg": "invalid"}]
        problem = _problem_from_validation_error(None, errors)
        assert problem.detail == "items.0.id: invalid"

    def test_empty_errors_use_default_detail(self) -> None:
        problem = _problem_from_validation_error(None, [])
        assert problem.detail == "Request validation failed"

    def test_errors_without_msg_skip_empty_segment(self) -> None:
        errors: list[dict[str, Any]] = [{"loc": ("body", "x")}]
        problem = _problem_from_validation_error(None, errors)
        assert problem.detail == "body.x: "
