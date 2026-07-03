from collections.abc import Awaitable, Callable

from fastapi import FastAPI, Request, Response
from starlette.middleware.base import BaseHTTPMiddleware

from modulo.settings import get_settings


class SecurityHeadersMiddleware(BaseHTTPMiddleware):
    """Adds security-related HTTP response headers to every response.

    Headers set:
    - Content-Security-Policy
    - Strict-Transport-Security (only when not debug)
    - X-Frame-Options
    - X-Content-Type-Options
    - Referrer-Policy
    - Permissions-Policy
    """

    def __init__(self, app: FastAPI) -> None:
        super().__init__(app)
        settings = get_settings()
        self._debug = settings.debug
        csp_connect = "'self' *.ingest.sentry.io *.datadoghq.com *.dd.dg *.rum.browserevents.com"
        if self._debug:
            csp_connect += " ws: wss:"
        if settings.modulo_monitor_domains:
            csp_connect += " " + settings.modulo_monitor_domains
        self._csp = (
            f"default-src 'self'; connect-src {csp_connect}; "
            "script-src 'self' 'unsafe-inline'; "
            "style-src 'self' 'unsafe-inline'; "
            "frame-ancestors 'none'"
        )
        self._hsts = "max-age=31536000; includeSubDomains"
        self._xfo = "DENY"
        self._cto = "nosniff"
        self._referrer = "strict-origin-when-cross-origin"
        self._permissions = "camera=(), microphone=(), geolocation=()"

    async def dispatch(
        self,
        request: Request,
        call_next: Callable[[Request], Awaitable[Response]],
    ) -> Response:
        response: Response = await call_next(request)

        response.headers["Content-Security-Policy"] = self._csp
        if not self._debug:
            response.headers["Strict-Transport-Security"] = self._hsts
        response.headers["X-Frame-Options"] = self._xfo
        response.headers["X-Content-Type-Options"] = self._cto
        response.headers["Referrer-Policy"] = self._referrer
        response.headers["Permissions-Policy"] = self._permissions

        return response
