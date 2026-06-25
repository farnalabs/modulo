import logging

from fastapi import Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from modulo.api.models.error import ErrorDetail, ErrorResponse

_log = logging.getLogger(__name__)


def _request_id(request: Request) -> str | None:
    return getattr(request.state, "request_id", None)


async def http_exception_handler(request: Request, exc: StarletteHTTPException) -> JSONResponse:
    status = exc.status_code
    detail = exc.detail if isinstance(exc.detail, str) else str(exc.detail)

    code_map = {
        400: "BAD_REQUEST",
        401: "UNAUTHORIZED",
        403: "FORBIDDEN",
        404: "NOT_FOUND",
        405: "METHOD_NOT_ALLOWED",
        409: "CONFLICT",
        422: "VALIDATION_ERROR",
        429: "RATE_LIMITED",
    }
    code = code_map.get(status, f"HTTP_{status}")

    body = ErrorResponse(
        error=ErrorDetail(
            code=code,
            message=detail,
            detail=None,
            request_id=_request_id(request),
        )
    )
    content = body.model_dump(mode="json")
    return JSONResponse(
        status_code=status,
        content=content,
        headers={
            "X-Request-ID": _request_id(request) or "",
        },
    )


async def validation_exception_handler(
    request: Request, exc: RequestValidationError
) -> JSONResponse:
    errors = exc.errors()
    detail_str = "; ".join(
        f"{'.'.join(str(p) for p in e.get('loc', []))}: {e.get('msg', '')}"
        for e in errors
    )
    body = ErrorResponse(
        error=ErrorDetail(
            code="VALIDATION_ERROR",
            message="Request validation failed",
            detail=detail_str or str(exc),
            request_id=_request_id(request),
        )
    )
    content = body.model_dump(mode="json")
    return JSONResponse(
        status_code=422,
        content=content,
        headers={
            "X-Request-ID": _request_id(request) or "",
        },
    )
