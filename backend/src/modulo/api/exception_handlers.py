import logging
import traceback

from fastapi import Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from modulo.api.models.problem import (
    ProblemDetail,
    ProblemException,
    ProblemType,
    problem_from_http_exception,
    problem_from_validation_error,
)
from modulo.db.capacity import StorageExhaustedError

_log = logging.getLogger(__name__)


async def http_exception_handler(request: Request, exc: StarletteHTTPException) -> JSONResponse:
    headers = dict(exc.headers or {})
    if isinstance(exc, ProblemException):
        problem = exc.problem
        problem.request_id = getattr(request.state, "request_id", None)
        return problem.to_response(headers=headers)
    problem = problem_from_http_exception(request, exc)
    return problem.to_response(headers=headers)


async def validation_exception_handler(request: Request, exc: RequestValidationError) -> JSONResponse:
    problem = problem_from_validation_error(request, exc.errors())
    return problem.to_response()


async def storage_exhausted_exception_handler(request: Request, exc: StorageExhaustedError) -> JSONResponse:
    """Map the DB-capacity ``StorageExhaustedError`` to HTTP 503 (FAR-426).

    A ``fixed``-mode DB at/over the 98% hard-stop refuses NEW run creation.
    Rendered as ``urn:problem:modulo:storage_exhausted`` so the frontend can
    distinguish "storage is full — clear work" from a generic outage.
    """
    rid = getattr(request.state, "request_id", None)
    return ProblemDetail.from_type(
        problem_type=ProblemType.STORAGE_EXHAUSTED,
        detail=str(exc),
        instance=str(request.url.path),
        request_id=rid,
    ).to_response()


async def unhandled_exception_handler(request: Request, exc: Exception) -> JSONResponse:
    """Catch-all for any exception not handled by specific handlers.

    Logs the full exception with stack trace and returns a structured
    ProblemDetail 500 response with correlation_id.
    """
    rid = getattr(request.state, "request_id", None)
    _log.exception(
        "exception_handlers.unhandled_exception",
        extra={
            "method": request.method,
            "path": str(request.url.path),
            "request_id": rid,
            "exc_type": type(exc).__name__,
            "traceback": "".join(traceback.format_exception(type(exc), exc, exc.__traceback__)),
        },
    )
    try:
        return ProblemDetail.from_type(
            problem_type=ProblemType.INTERNAL_ERROR,
            detail="An unexpected error occurred",
            instance=str(request.url.path),
            request_id=rid,
        ).to_response()
    except Exception:
        return ProblemDetail.fallback_internal_error(rid)
