import asyncio
import logging
from collections.abc import Awaitable, Callable
from typing import Any

from fastapi import Request
from fastapi.responses import JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import Response

_log = logging.getLogger(__name__)


class RequestTimeoutMiddleware(BaseHTTPMiddleware):
    """Enforce a maximum duration for each request.

    If the request handler does not complete within *timeout_seconds*,
    the middleware returns a 504 Gateway Timeout response.

    Per-path overrides can be passed via *overrides*:

    .. code-block:: python

        overrides = {"/healthz": 5, "/api/v1/runs": 300}

    All other paths use the default *timeout_seconds* (120 s).
    """

    def __init__(
        self,
        app: Any,
        timeout_seconds: int = 120,
        overrides: dict[str, int] | None = None,
    ) -> None:
        super().__init__(app)
        self._default = timeout_seconds
        self._overrides = overrides or {}

    async def dispatch(self, request: Request, call_next: Callable[[Request], Awaitable[Response]]) -> Response:
        timeout = self._timeout_for(request.url.path)
        if timeout <= 0:
            return await call_next(request)
        try:
            return await asyncio.wait_for(call_next(request), timeout=timeout)
        except TimeoutError:
            _log.warning(
                "middleware.request_timeout",
                extra={
                    "method": request.method,
                    "path": str(request.url.path),
                    "timeout_s": timeout,
                },
            )
            return JSONResponse(
                status_code=504,
                content={
                    "error": "gateway_timeout",
                    "detail": f"Request exceeded {timeout}s timeout",
                },
            )
        except asyncio.CancelledError:
            raise

    def _timeout_for(self, path: str) -> int:
        for prefix, to in self._overrides.items():
            if path.startswith(prefix):
                return to
        return self._default
