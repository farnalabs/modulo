import logging
import os
import traceback as _traceback
from datetime import datetime, timezone
from typing import Any

from fastapi import Request
from fastapi.responses import JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import Response

from modulo.api.models.problem import ProblemDetail, ProblemType
from modulo.core.logging_config import correlation_id_var
from modulo.version import get_version

logger = logging.getLogger(__name__)


class CatchAllMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next: Any) -> Response:
        try:
            response = await call_next(request)
            return response  # type: ignore[no-any-return]
        except BaseException as exc:
            rid = getattr(request.state, "request_id", None)
            try:
                with open("/tmp/exception_log.txt", "a") as f:
                    f.write(f"[{datetime.now(timezone.utc).isoformat()}] {type(exc).__name__}: {exc}\n")
                    _traceback.print_exc(file=f)
                    f.write("---\n")
            except Exception:
                pass
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
            async with session.begin():
                await set_rls_org(session, org_id)
                if org_id is not None:
                    await service.ingest(session, org_id, event_data)
    except Exception:
        logger.exception("middleware.error_ingest_failed")


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
