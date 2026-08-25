"""RFC 9457 Problem Details for HTTP APIs."""

from __future__ import annotations

import enum
from collections.abc import Sequence
from typing import Any

from fastapi import Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from starlette.exceptions import HTTPException


class ProblemType(enum.StrEnum):
    BAD_REQUEST = "bad_request"
    VALIDATION_ERROR = "validation_error"
    UNAUTHORIZED = "unauthorized"
    FORBIDDEN = "forbidden"
    NOT_FOUND = "not_found"
    CONFLICT = "conflict"
    GONE = "gone"
    METHOD_NOT_ALLOWED = "method_not_allowed"
    RATE_LIMITED = "rate_limited"
    FEATURE_REQUIRED = "feature_required"
    PIPELINE_ERROR = "pipeline_error"
    MIGRATION_REQUIRED = "migration_required"
    BAD_GATEWAY = "bad_gateway"
    SERVICE_UNAVAILABLE = "service_unavailable"
    STORAGE_EXHAUSTED = "storage_exhausted"
    GATEWAY_TIMEOUT = "gateway_timeout"
    INTERNAL_ERROR = "internal_error"


_PROBLEM_METADATA: dict[ProblemType, dict[str, Any]] = {
    ProblemType.BAD_REQUEST: {"status": 400, "title": "Bad Request"},
    ProblemType.VALIDATION_ERROR: {"status": 422, "title": "Validation Error"},
    ProblemType.UNAUTHORIZED: {"status": 401, "title": "Unauthorized"},
    ProblemType.FORBIDDEN: {"status": 403, "title": "Forbidden"},
    ProblemType.NOT_FOUND: {"status": 404, "title": "Not Found"},
    ProblemType.CONFLICT: {"status": 409, "title": "Conflict"},
    ProblemType.GONE: {"status": 410, "title": "Gone"},
    ProblemType.METHOD_NOT_ALLOWED: {"status": 405, "title": "Method Not Allowed"},
    ProblemType.RATE_LIMITED: {"status": 429, "title": "Rate Limited"},
    ProblemType.FEATURE_REQUIRED: {"status": 402, "title": "Feature Not Available"},
    ProblemType.PIPELINE_ERROR: {"status": 500, "title": "Pipeline Error"},
    ProblemType.MIGRATION_REQUIRED: {"status": 501, "title": "Migration Required"},
    ProblemType.BAD_GATEWAY: {"status": 502, "title": "Bad Gateway"},
    ProblemType.SERVICE_UNAVAILABLE: {"status": 503, "title": "Service Unavailable"},
    ProblemType.STORAGE_EXHAUSTED: {"status": 503, "title": "Storage Exhausted"},
    ProblemType.GATEWAY_TIMEOUT: {"status": 504, "title": "Gateway Timeout"},
    ProblemType.INTERNAL_ERROR: {"status": 500, "title": "Internal Error"},
}


class ProblemDetail(BaseModel):
    type: str
    title: str
    status: int
    detail: str
    instance: str | None = None
    request_id: str | None = None

    @classmethod
    def from_type(
        cls,
        problem_type: ProblemType,
        detail: str,
        instance: str | None = None,
        request_id: str | None = None,
    ) -> ProblemDetail:
        meta = _PROBLEM_METADATA[problem_type]
        return cls(
            type=f"urn:problem:modulo:{problem_type.value}",
            title=meta["title"],
            status=meta["status"],
            detail=detail,
            instance=instance,
            request_id=request_id,
        )

    def to_response(self, headers: dict[str, str] | None = None) -> JSONResponse:
        merged = dict(headers or {})
        if self.request_id:
            merged.setdefault("X-Request-ID", self.request_id)
        return JSONResponse(
            status_code=self.status,
            content=self.model_dump(mode="json", exclude_none=True),
            headers=merged,
        )

    @staticmethod
    def fallback_internal_error(request_id: str | None = None) -> JSONResponse:
        """Build a 500 response when even ProblemDetail construction fails.

        This is a safety net so that an exception in the exception handler
        itself still produces a valid HTTP response.
        """
        return JSONResponse(
            status_code=500,
            content={
                "type": "urn:problem:modulo:internal_error",
                "title": "Internal Error",
                "detail": "An unexpected error occurred",
                "status": 500,
            },
            headers={"X-Request-ID": request_id or ""},
        )


class ProblemException(HTTPException):
    """Raise this anywhere to produce a structured ProblemDetail response."""

    def __init__(
        self,
        problem_type: ProblemType,
        detail: str,
        instance: str | None = None,
        headers: dict[str, str] | None = None,
    ) -> None:
        self.problem = ProblemDetail.from_type(
            problem_type=problem_type,
            detail=detail,
            instance=instance,
        )
        super().__init__(
            status_code=self.problem.status,
            detail=self.problem.detail,
            headers=headers,
        )


def problem_from_http_exception(
    request: Request,
    exc: HTTPException,
) -> ProblemDetail:
    """Map a plain HTTPException to a ProblemDetail (no ProblemException)."""
    status = exc.status_code
    # Handle dict detail (from FastAPI's raise HTTPException(detail={...}))
    raw = exc.detail
    detail = raw.get("detail", str(raw)) if isinstance(raw, dict) else str(raw)

    lookup = {
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
    }
    problem_type = lookup.get(status, ProblemType.INTERNAL_ERROR)
    return ProblemDetail.from_type(
        problem_type=problem_type,
        detail=detail,
        request_id=getattr(request.state, "request_id", None),
    )


def problem_from_validation_error(
    request: Request,
    errors: Sequence[dict[str, Any]],
) -> ProblemDetail:
    detail = "; ".join(f"{'.'.join(str(p) for p in e.get('loc', []))}: {e.get('msg', '')}" for e in errors)
    return ProblemDetail.from_type(
        problem_type=ProblemType.VALIDATION_ERROR,
        detail=detail or "Request validation failed",
        request_id=getattr(request.state, "request_id", None),
    )
