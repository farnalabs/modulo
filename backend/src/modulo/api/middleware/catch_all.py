import logging
import os
from typing import Any

from fastapi import Request
from fastapi.responses import JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import Response

from modulo.api.models.error import ErrorDetail, ErrorResponse
from modulo.core.logging_config import correlation_id_var
from modulo.version import get_version

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
            try:
                await _ingest_unhandled_error(request)
            except Exception:
                logger.exception("middleware.error_ingest_dispatch_failed")
            return _make_500_response(rid)


async def _ingest_unhandled_error(request: Request) -> None:
    """Best-effort ingest of unhandled exceptions to error tracking."""
    try:
        from modulo.api.dependencies import (
            get_or_create_engine,
            get_or_create_session_factory,
        )
        from modulo.core.error_tracking import ErrorIngestionService
        from modulo.db.rls import set_rls_org
        from modulo.settings import get_settings

        settings = get_settings()
        engine = get_or_create_engine(settings)
        factory = get_or_create_session_factory(engine)

        org_id = getattr(request.state, "organisation_id", None)
        user_id = getattr(request.state, "user_id", None)
        correlation_id = correlation_id_var.get()

        event_data: dict[str, Any] = {
            "level": "error",
            "message": f"Unhandled exception: {request.method} {request.url.path}",
            "source": "backend",
            "context_json": {
                "method": request.method,
                "path": str(request.url.path),
                "correlation_id": correlation_id,
                "user_id": str(user_id) if user_id else None,
            },
            "environment": os.environ.get("MODULO_ENV", "development"),
            "version": get_version(),
        }

        service = ErrorIngestionService()
        async with factory() as session:
            await set_rls_org(session, org_id)
            async with session.begin():
                await service.ingest(session, org_id, event_data)
    except Exception:
        logger.exception("middleware.error_ingest_failed")


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
