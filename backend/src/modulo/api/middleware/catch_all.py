import logging
from typing import Any

from fastapi import Request
from fastapi.responses import JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import Response

from modulo.api.models.error import ErrorDetail, ErrorResponse

logger = logging.getLogger(__name__)


class CatchAllMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next: Any) -> Response:
        try:
            response = await call_next(request)
            return response  # type: ignore[no-any-return]
        except Exception:
            rid = getattr(request.state, "request_id", None)
            logger.exception(
                "middleware.unhandled_exception",
                extra={"method": request.method, "path": str(request.url.path), "request_id": rid},
            )
            return _make_500_response(rid)


def _make_500_response(request_id: str | None) -> JSONResponse:
    """Build a 500 response, defensively handling serialisation failures."""
    try:
        body = ErrorResponse(
            error=ErrorDetail(
                code="INTERNAL_ERROR",
                message="An unexpected error occurred",
                detail=None,
                request_id=request_id,
            )
        )
        content = body.model_dump(mode="json")
        return JSONResponse(
            status_code=500,
            content=content,
            headers={"X-Request-ID": request_id or ""},
        )
    except Exception:
        logger.exception("middleware.error_response_failed")
        return JSONResponse(
            status_code=500,
            content={"error": {"code": "INTERNAL_ERROR", "message": "An unexpected error occurred"}},
            headers={"X-Request-ID": request_id or ""},
        )
