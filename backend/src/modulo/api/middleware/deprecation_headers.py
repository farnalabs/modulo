"""Middleware that adds Sunset and Deprecation headers to deprecated endpoints.

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

from fastapi import FastAPI, Request, Response
from starlette.middleware.base import BaseHTTPMiddleware

_DeprecationRule = dict[str, str | None]
_DeprecationRegistry = dict[str, _DeprecationRule]


class DeprecationHeaderMiddleware(BaseHTTPMiddleware):
    """Adds Deprecation, Sunset, and Link headers to deprecated routes.

    Configure via the classmethod ``deprecate()`` which registers a path
    prefix along with an optional sunset date and migration URL.
    """

    _registry: _DeprecationRegistry

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
        cls._registry.clear()

    def __init__(self, app: FastAPI) -> None:
        super().__init__(app)
        type(self)._registry = {}

    async def dispatch(
        self,
        request: Request,
        call_next: Callable[[Request], Awaitable[Response]],
    ) -> Response:
        response: Response = await call_next(request)

        path = request.url.path
        rule = self._matching_rule(path)
        if rule is not None:
            response.headers["Deprecation"] = "true"
            sunset = rule.get("sunset")
            if sunset:
                response.headers["Sunset"] = sunset
            migration_url = rule.get("migration_url")
            if migration_url:
                response.headers["Link"] = f'{migration_url}; rel="deprecation"'

        return response

    def _matching_rule(self, path: str) -> _DeprecationRule | None:
        for prefix, rule in self._registry.items():
            if path.startswith(prefix):
                return rule
        return None
