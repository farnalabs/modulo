"""CSRF protection middleware — double-submit cookie pattern.

For cookie-authenticated state-changing requests (POST, PUT, PATCH, DELETE):
  - Reads X-CSRF-Token header and compares against XSRF-TOKEN cookie
  - Returns 403 on mismatch/missing

For Bearer-authenticated requests (API keys, OAuth tokens): no CSRF check.
Safe methods (GET, HEAD, OPTIONS, TRACE): no CSRF check.
Exempt paths (configurable): no CSRF check.
"""

import fnmatch
import logging
import secrets
from typing import Any

from fastapi import FastAPI, Request, Response
from fastapi.responses import JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware

from modulo.settings import Settings, get_settings

_log = logging.getLogger(__name__)

SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS", "TRACE"})


class CsrfMiddleware(BaseHTTPMiddleware):
    """Double-submit cookie CSRF protection for cookie-authenticated requests."""

    SESSION_COOKIE = "modulo_session"
    CSRF_COOKIE = "XSRF-TOKEN"
    CSRF_HEADER = "X-CSRF-Token"

    def __init__(
        self,
        app: FastAPI,
        settings: Settings | None = None,
    ) -> None:
        super().__init__(app)
        resolved = settings or get_settings()
        self._enabled = resolved.modulo_csrf_enabled
        raw_exempt = resolved.modulo_csrf_exempt_paths or ""
        self._exempt_paths = [p.strip() for p in raw_exempt.split(",") if p.strip()]

    async def dispatch(self, request: Request, call_next: Any) -> Response:
        if not self._enabled:
            return await call_next(request)  # type: ignore[no-any-return]

        if request.method in SAFE_METHODS:
            return await call_next(request)  # type: ignore[no-any-return]

        path = request.url.path
        if self._is_exempt(path):
            return await call_next(request)  # type: ignore[no-any-return]

        auth_header = request.headers.get("Authorization", "")
        if auth_header.startswith("Bearer "):
            return await call_next(request)  # type: ignore[no-any-return]

        csrf_cookie = request.cookies.get(self.CSRF_COOKIE)
        csrf_header = request.headers.get(self.CSRF_HEADER)

        if not csrf_cookie or not csrf_header:
            _log.warning(
                "csrf.missing_token",
                extra={"method": request.method, "path": path},
            )
            return JSONResponse(
                status_code=403,
                content={"error": "csrf_token_mismatch", "detail": "Missing CSRF token"},
            )

        if not secrets.compare_digest(csrf_cookie.encode(), csrf_header.encode()):
            _log.warning(
                "csrf.token_mismatch",
                extra={"method": request.method, "path": path},
            )
            return JSONResponse(
                status_code=403,
                content={"error": "csrf_token_mismatch", "detail": "CSRF token mismatch"},
            )

        return await call_next(request)  # type: ignore[no-any-return]

    def _is_exempt(self, path: str) -> bool:
        for pattern in self._exempt_paths:
            if fnmatch.fnmatch(path, pattern):
                return True
            if path.startswith(pattern):
                return True
        return False
