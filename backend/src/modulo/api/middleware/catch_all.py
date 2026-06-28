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
        except Exception as exc:
            rid = getattr(request.state, "request_id", None)
            logger.exception(
                "middleware.unhandled_exception",
                extra={"method": request.method, "path": str(request.url.path), "request_id": rid},
            )
            import traceback
            tb_str = "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))
            body = ErrorResponse(
                error=ErrorDetail(
                    code="INTERNAL_ERROR",
                    message=f"{type(exc).__name__}: {exc}",
                    detail=tb_str[-2000:],
                    detail=None,
                    request_id=rid,
                )
            )
            content = body.model_dump(mode="json")
            return JSONResponse(
                status_code=500,
                content=content,
                headers={"X-Request-ID": rid or ""},
            )
