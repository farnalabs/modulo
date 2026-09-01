"""Middleware that adds Sunset and Deprecation headers to deprecated endpoints,
and returns 410 Gone after the sunset date has passed.

Usage:
    from modulo.api.middleware.deprecation_headers import DeprecationHeaderMiddleware

    app.add_middleware(DeprecationHeaderMiddleware)
    DeprecationHeaderMiddleware.deprecate(
        "/api/v1/old-endpoint",
        sunset="2026-09-01",
        migration_url="/docs/migrations/v2",
    )
"""

from collections.abc import Awaitable, Callable
from datetime import UTC, date, datetime
from typing import ClassVar

from fastapi import FastAPI, Request, Response
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import JSONResponse

_DeprecationRule = dict[str, str | None]
_DeprecationRegistry = dict[str, _DeprecationRule]


class DeprecationHeaderMiddleware(BaseHTTPMiddleware):
    """Adds Deprecation, Sunset, and Link headers to deprecated routes.

    Configure via the classmethod ``deprecate()`` which registers a path
    prefix along with an optional sunset date and migration URL.

    After the sunset date has passed, the endpoint returns ``410 Gone``
    for the 30-day grace period described in the deprecation policy.
    """

    _registry: ClassVar[_DeprecationRegistry] = {}

    @classmethod
    def deprecate(
        cls,
        path_prefix: str,
        sunset: str | None = None,
        migration_url: str | None = None,
    ) -> None:
        """Register *path_prefix* as deprecated.

        Args:
            path_prefix: URL path prefix (e.g. ``"/api/v1/old"``).
            sunset: ISO 8601 date string after which the endpoint will be removed.
            migration_url: Link to the migration guide.

        """
        cls._registry[path_prefix] = {
            "sunset": sunset,
            "migration_url": migration_url,
        }

    @classmethod
    def clear(cls) -> None:
        """Clear all deprecation rules (useful in tests)."""
        cls._registry = {}

    def __init__(self, app: FastAPI) -> None:
        super().__init__(app)

    async def dispatch(
        self,
        request: Request,
        call_next: Callable[[Request], Awaitable[Response]],
    ) -> Response:
        path = request.url.path
        rule = self._matching_rule(path)
        if rule is not None:
            sunset = rule.get("sunset")
            if sunset and self._is_past_sunset(sunset):
                return JSONResponse(
                    status_code=410,
                    content={
                        "detail": "This endpoint is no longer available. "
                        "It was deprecated and the sunset date has passed. "
                        f"See {rule.get('migration_url', 'the migration guide')} for details.",
                    },
                    headers={
                        "Deprecation": "true",
                        "Sunset": sunset,
                    },
                )

            response: Response = await call_next(request)
            response.headers["Deprecation"] = "true"
            if sunset:
                response.headers["Sunset"] = sunset
            migration_url = rule.get("migration_url")
            if migration_url:
                response.headers["Link"] = f'{migration_url}; rel="deprecation"'

            return response

        return await call_next(request)

    def _matching_rule(self, path: str) -> _DeprecationRule | None:
        for prefix, rule in self._registry.items():
            if path.startswith(prefix):
                return rule
        return None

    @staticmethod
    def _is_past_sunset(sunset: str) -> bool:
        """Return True if the current date is past the sunset date."""
        try:
            sunset_date = date.fromisoformat(sunset)
            return datetime.now(UTC).date() > sunset_date
        except (ValueError, TypeError):
            return False
