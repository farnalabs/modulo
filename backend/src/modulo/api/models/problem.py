"""RFC 9457 Problem Details for HTTP APIs."""

from __future__ import annotations

import enum
from collections.abc import Sequence
from typing import Any

from fastapi import HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from starlette.exceptions import HTTPException as StarletteHTTPException


class ProblemType(enum.StrEnum):
    BAD_REQUEST = "bad_request"
    VALIDATION_ERROR = "validation_error"
    UNAUTHORIZED = "unauthorized"
    FORBIDDEN = "forbidden"
    NOT_FOUND = "not_found"
    CONFLICT = "conflict"
    GONE = "gone"
    METHOD_NOT_ALLOWED = "method_not_allowed"
    # FAR-645: machine-readable HITL claim-failure types. The frontend
    # discriminates the three claim-conflict modes by ``type`` (not by
    # substring-matching English prose), so a backend rewording can never
    # silently degrade the claim-failure UX.
    HITL_REVIEW_ALREADY_CLAIMED = "hitl_review_already_claimed"
    HITL_REVIEW_ALREADY_DECIDED = "hitl_review_already_decided"
    HITL_RUN_NOT_AWAITING = "hitl_run_not_awaiting"
    RATE_LIMITED = "rate_limited"
    FEATURE_REQUIRED = "feature_required"
    PIPELINE_ERROR = "pipeline_error"
    MIGRATION_REQUIRED = "migration_required"
    BAD_GATEWAY = "bad_gateway"
    SERVICE_UNAVAILABLE = "service_unavailable"
    STORAGE_EXHAUSTED = "storage_exhausted"
    GATEWAY_TIMEOUT = "gateway_timeout"
    INTERNAL_ERROR = "internal_error"
    # FAR-1545: route-specific problem types. RFC 9457 §3.1.1 designates
    # ``type`` as the problem type's PRIMARY identifier, so a route error code
    # that names a genuinely distinct problem (not a status-derived generic
    # one) gets its own member: the wire ``type`` becomes
    # ``urn:problem:modulo:<code>`` instead of collapsing to the status-derived
    # type. The ``code`` extension member (#1336) still carries the same value.
    INVALID_TOKEN = "invalid_token"
    TOKEN_MISMATCH = "token_mismatch"
    ALREADY_CONFIGURED = "already_configured"
    ENCRYPTION_CONFIG_ERROR = "encryption_config_error"
    ENCRYPTION_ERROR = "encryption_error"
    UPDATE_FAILED = "update_failed"


_PROBLEM_METADATA: dict[ProblemType, dict[str, Any]] = {
    ProblemType.BAD_REQUEST: {"status": 400, "title": "Bad Request"},
    ProblemType.VALIDATION_ERROR: {"status": 422, "title": "Validation Error"},
    ProblemType.UNAUTHORIZED: {"status": 401, "title": "Unauthorized"},
    ProblemType.FORBIDDEN: {"status": 403, "title": "Forbidden"},
    ProblemType.NOT_FOUND: {"status": 404, "title": "Not Found"},
    ProblemType.CONFLICT: {"status": 409, "title": "Conflict"},
    ProblemType.GONE: {"status": 410, "title": "Gone"},
    ProblemType.METHOD_NOT_ALLOWED: {"status": 405, "title": "Method Not Allowed"},
    ProblemType.HITL_REVIEW_ALREADY_CLAIMED: {"status": 409, "title": "Conflict"},
    ProblemType.HITL_REVIEW_ALREADY_DECIDED: {"status": 409, "title": "Conflict"},
    ProblemType.HITL_RUN_NOT_AWAITING: {"status": 409, "title": "Conflict"},
    ProblemType.RATE_LIMITED: {"status": 429, "title": "Rate Limited"},
    ProblemType.FEATURE_REQUIRED: {"status": 402, "title": "Feature Not Available"},
    ProblemType.PIPELINE_ERROR: {"status": 500, "title": "Pipeline Error"},
    ProblemType.MIGRATION_REQUIRED: {"status": 501, "title": "Migration Required"},
    ProblemType.BAD_GATEWAY: {"status": 502, "title": "Bad Gateway"},
    ProblemType.SERVICE_UNAVAILABLE: {"status": 503, "title": "Service Unavailable"},
    ProblemType.STORAGE_EXHAUSTED: {"status": 503, "title": "Storage Exhausted"},
    ProblemType.GATEWAY_TIMEOUT: {"status": 504, "title": "Gateway Timeout"},
    ProblemType.INTERNAL_ERROR: {"status": 500, "title": "Internal Error"},
    # FAR-1545: per-type status/title (RFC 9457 §3.1.3 — short, per-type
    # title). Each ``status`` equals the HTTP status the raising route
    # declares (§3.1.2), so the problem object's ``status`` never disagrees
    # with the actual response.
    ProblemType.INVALID_TOKEN: {"status": 404, "title": "Invalid Token"},
    ProblemType.TOKEN_MISMATCH: {"status": 400, "title": "Token Mismatch"},
    ProblemType.ALREADY_CONFIGURED: {"status": 400, "title": "Already Configured"},
    ProblemType.ENCRYPTION_CONFIG_ERROR: {"status": 500, "title": "Encryption Not Configured"},
    ProblemType.ENCRYPTION_ERROR: {"status": 500, "title": "Encryption Error"},
    ProblemType.UPDATE_FAILED: {"status": 500, "title": "Update Failed"},
}

# FAR-1545: ``code -> ProblemType`` for route error codes that name a DISTINCT
# problem type (RFC 9457 §4). ``problem_from_http_exception`` prefers this map
# over the coarse status lookup, so the wire ``type`` is specific instead of
# collapsing to the status-derived generic (500s to ``internal_error``).
#
# Deliberately ABSENT — truly generic conditions that stay status-derived
# (RFC §4: do not mint a type for a generic problem):
#
# * ``backend_not_found`` — a plain resource 404 (the RFC's own generic
#   example); the ``code`` extension member still carries the specificity.
# * ``database_error`` — a generic 503 outage; the status lookup already
#   resolves it to ``service_unavailable``.
# * ``conflict`` / ``internal_error`` / ``migration_required`` — their status
#   lookup resolves to exactly the same type the code names (409/500/501), so
#   a map entry would change nothing.
_CODE_TYPE_MAP: dict[str, ProblemType] = {
    "invalid_token": ProblemType.INVALID_TOKEN,
    "token_mismatch": ProblemType.TOKEN_MISMATCH,
    "already_configured": ProblemType.ALREADY_CONFIGURED,
    "encryption_config_error": ProblemType.ENCRYPTION_CONFIG_ERROR,
    "encryption_error": ProblemType.ENCRYPTION_ERROR,
    "update_failed": ProblemType.UPDATE_FAILED,
}


class ProblemDetail(BaseModel):
    type: str
    title: str
    status: int
    detail: str
    instance: str | None = None
    request_id: str | None = None
    # RFC 9457 extension member: the route's machine-readable error code (from
    # ``HTTPException(detail={"error": ...})``). Clients discriminate on this
    # rather than substring-matching the human ``detail`` prose.
    code: str | None = None

    @classmethod
    def from_type(
        cls,
        problem_type: ProblemType,
        detail: str,
        instance: str | None = None,
        request_id: str | None = None,
        code: str | None = None,
    ) -> ProblemDetail:
        meta = _PROBLEM_METADATA[problem_type]
        return cls(
            type=f"urn:problem:modulo:{problem_type.value}",
            title=meta["title"],
            status=meta["status"],
            detail=detail,
            instance=instance,
            request_id=request_id,
            code=code,
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
    """Raise this anywhere to produce a structured ProblemDetail response.

    Bases on FastAPI's ``HTTPException`` (a subclass of starlette's), NOT
    starlette's directly: the codebase's blanket ``except HTTPException:
    raise`` pass-through clauses in routes and ``handle_db_errors`` bind to
    FastAPI's class, so a starlette-based ProblemException raised inside a
    wrapped route body would fall through to the generic Exception backstop
    and be masked as a 500 (FAR-645).
    """

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
    exc: StarletteHTTPException,
) -> ProblemDetail:
    """Map a plain HTTPException to a ProblemDetail (no ProblemException)."""
    status = exc.status_code
    # Handle dict detail (from FastAPI's raise HTTPException(detail={...})).
    # The route's machine-readable ``error`` code is preserved as an RFC 9457
    # extension member so the frontend can branch on it; the human ``detail``
    # string (or, absent one, the code itself) becomes the problem detail.
    raw = exc.detail
    code: str | None = None
    if isinstance(raw, dict):
        raw_error = raw.get("error")
        code = raw_error if isinstance(raw_error, str) else None
        raw_detail = raw.get("detail")
        detail = raw_detail if isinstance(raw_detail, str) else (code or str(raw))
    else:
        detail = str(raw)

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
    # FAR-1545: prefer the route's ``code`` over the coarse status lookup
    # (RFC 9457 §3.1.1 — ``type`` is the problem type's primary identifier).
    # The map only holds codes naming a distinct problem type; an unknown (or
    # deliberately generic) code falls back to the status-derived type above.
    if code is not None:
        problem_type = _CODE_TYPE_MAP.get(code, problem_type)
    return ProblemDetail.from_type(
        problem_type=problem_type,
        detail=detail,
        code=code,
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
