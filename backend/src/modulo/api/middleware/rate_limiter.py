"""Rate limiting middleware — Redis-backed sliding window with in-memory fallback.

NOTE: This middleware accepts an optional `Settings` object and an optional
`RateLimiterRegistry` in the constructor. When running inside a FastAPI app
that overrides `get_settings` (e.g. in tests), tests should pass settings
explicitly rather than relying on the module-level `get_settings()` call.
"""

import logging
from typing import Any, ClassVar

from fastapi import FastAPI, Request, Response
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.status import HTTP_429_TOO_MANY_REQUESTS

from modulo.core.rate_limiter import RateLimiterRegistry
from modulo.settings import Settings, get_settings

RATELIMIT_BYPASS_HEADER = "MODULO_RATELIMIT_BYPASS_TOKEN"

redis_available: bool = False

_log = logging.getLogger(__name__)


def _create_registry(settings: Settings) -> RateLimiterRegistry:
    """Create a rate limiter registry, connecting to Redis if configured."""
    global redis_available

    if settings.modulo_db.lower() == "sqlite":
        _log.info("ratelimit.sqlite_disabled")
        redis_available = False
        return RateLimiterRegistry(redis_client=None)

    if settings.redis_url:
        try:
            from redis.asyncio import Redis

            client: Any = Redis.from_url(settings.redis_url, decode_responses=False)
            registry = RateLimiterRegistry(redis_client=client)
            redis_available = True
            _log.info("ratelimit.redis_enabled")
            return registry
        except Exception as exc:
            _log.warning("ratelimit.redis_fallback", extra={"error": str(exc)})

    redis_available = False
    _log.warning("ratelimit.in_memory_mode")
    return RateLimiterRegistry(redis_client=None)


class RateLimitMiddleware(BaseHTTPMiddleware):
    """Rate limits per-route based on pre-defined rules.

    Uses Redis sliding window (ZADD + ZREMRANGEBYSCORE) when Redis is
    configured; falls back to in-memory token bucket otherwise.

    Accepts optional ``settings`` and ``registry`` constructor params.
    When provided, these are used instead of calling ``get_settings()``
    and ``_create_registry()`` — this allows tests to inject overrides.
    """

    RULES: ClassVar[list[tuple[str, int, int]]] = [
        ("/api/v1/runs", 100, 60),
        ("/api/v1/triggers", 50, 60),
        ("/mcp", 200, 60),
        ("/api/v1/hitl", 30, 60),
    ]

    @classmethod
    def set_rules(cls, rules: list[tuple[str, int, int]]) -> None:
        cls.RULES = rules

    def __init__(
        self,
        app: FastAPI,
        settings: Settings | None = None,
        registry: RateLimiterRegistry | None = None,
    ) -> None:
        super().__init__(app)
        resolved = settings or get_settings()
        self._bypass_token = resolved.modulo_ratelimit_bypass_token
        self._registry = registry or _create_registry(resolved)

    async def dispatch(self, request: Request, call_next: Any) -> Response:
        if self._should_rate_limit(request):
            client_key = self._client_key(request)
            rule = self._rule_for(request)

            allowed = await self._registry.check(
                client_key,
                max_requests=rule[1],
                window_s=rule[2],
            )
            if not allowed:
                _log.warning("ratelimit.exceeded", extra={"client_key": client_key})
                return Response(
                    status_code=HTTP_429_TOO_MANY_REQUESTS,
                    content=('{"detail":"Rate limit exceeded. Try again later.","error_code":"rate_limit_exceeded"}'),
                    media_type="application/json",
                    headers={"Retry-After": str(rule[2])},
                )
        response: Response = await call_next(request)
        return response

    def _should_rate_limit(self, request: Request) -> bool:
        if request.method not in ("POST", "PUT", "PATCH"):
            return False
        token = request.headers.get(RATELIMIT_BYPASS_HEADER, "")
        if token and self._bypass_token and token == self._bypass_token:
            return False
        path = request.url.path
        return any(path.startswith(p) for p, _, _ in self.RULES)

    def _rule_for(self, request: Request) -> tuple[str, int, int]:
        path = request.url.path
        for prefix, max_req, window in self.RULES:
            if path.startswith(prefix):
                return (prefix, max_req, window)
        return ("", 0, 0)

    @staticmethod
    def _client_key(request: Request) -> str:
        forwarded = request.headers.get("X-Forwarded-For", "")
        ip = forwarded.split(",")[0].strip() if forwarded else request.client.host if request.client else "unknown"
        return f"{ip}:{request.url.path}"
