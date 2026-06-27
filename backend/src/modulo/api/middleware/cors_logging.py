"""CORS middleware with logging support.

Extends Starlette's CORSMiddleware to add:
- Logging when a request is rejected due to CORS (origin not in allowed list)
- DEBUG-level logging of CORS preflight requests
"""

import logging
from typing import Any

from starlette.datastructures import Headers
from starlette.middleware.cors import CORSMiddleware as StarletteCORSMiddleware
from starlette.types import ASGIApp, Receive, Scope, Send

logger = logging.getLogger(__name__)


class CorsLoggingMiddleware(StarletteCORSMiddleware):
    def __init__(
        self,
        app: ASGIApp,
        **kwargs: Any,
    ) -> None:
        super().__init__(app, **kwargs)

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        headers = Headers(scope=scope)
        origin = headers.get("origin")
        method = scope["method"]

        if origin:
            is_preflight = method == "OPTIONS" and "access-control-request-method" in headers

            if is_preflight:
                logger.debug("CORS preflight from origin: %s", origin)

            if not self.is_allowed_origin(origin):
                logger.warning(
                    "CORS rejected origin=%s method=%s path=%s",
                    origin,
                    method,
                    scope.get("path", ""),
                )

        await super().__call__(scope, receive, send)
