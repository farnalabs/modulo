import logging
import os
import threading
from collections.abc import Awaitable, Callable
from typing import Any

from fastapi import HTTPException, Request
from fastapi.responses import JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import Response

from modulo.api.models.problem import ProblemDetail, ProblemType
from modulo.core.logging_config import correlation_id_var
from modulo.version import get_version

logger = logging.getLogger(__name__)

_unhandled_exception_count: int = 0
_unhandled_count_lock = threading.Lock()


class CatchAllMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next: Callable[[Request], Awaitable[Response]]) -> Response:
        try:
            return await call_next(request)
        except HTTPException:
            raise
        except Exception:
            global _unhandled_exception_count
            with _unhandled_count_lock:
                _unhandled_exception_count += 1

            rid = getattr(request.state, "request_id", None)
            logger.exception(
                "middleware.unhandled_exception",
                extra={
                    "method": request.method,
                    "path": str(request.url.path),
                    "request_id": rid,
                    "total_unhandled": _unhandled_exception_count,
                },
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
        async with factory() as session, session.begin():
            await set_rls_org(session, org_id)
            if org_id is not None:
                await service.ingest(session, org_id, event_data)
    except Exception:
        logger.exception("middleware.error_ingest_failed")


def get_unhandled_exception_count() -> int:
    """Expose the counter for observability / monitoring."""
    global _unhandled_exception_count
    with _unhandled_count_lock:
        return _unhandled_exception_count


def _make_500_response(request_id: str | None) -> JSONResponse:
    """Build a 500 response, defensively handling serialisation failures."""
    try:
        return ProblemDetail.from_type(
            problem_type=ProblemType.INTERNAL_ERROR,
            detail="An unexpected error occurred",
            request_id=request_id,
        ).to_response()
    except Exception:
        logger.exception("middleware.error_response_failed")
        return ProblemDetail.fallback_internal_error(request_id)
