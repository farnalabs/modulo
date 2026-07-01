"""Rate limiting middleware — Redis-backed sliding window with in-memory fallback.

NOTE: This middleware accepts an optional `Settings` object and an optional
`RateLimiterRegistry` in the constructor. When running inside a FastAPI app
that overrides `get_settings` (e.g. in tests), tests should pass settings
explicitly rather than relying on the module-level `get_settings()` call.
"""

import logging
from typing import Any, ClassVar

from fastapi import FastAPI, Request, Response
from jose import jwt as jose_jwt
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.status import HTTP_429_TOO_MANY_REQUESTS

from modulo.core.rate_limiter import AuthRateLimiter as AuthRateLimiterCls
from modulo.core.rate_limiter import RateLimiterRegistry
from modulo.settings import Settings, get_settings

RATELIMIT_BYPASS_HEADER = "MODULO_RATELIMIT_BYPASS_TOKEN"

redis_available: bool = False

_log = logging.getLogger(__name__)

# Tracked Redis clients for graceful shutdown.
_redis_clients: set[Any] = set()


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
            _redis_clients.add(client)
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
        ("/api/v1/runs", 60, 60),         # PRD §7.18: POST /api/v1/runs — 60/min
        ("/api/v1/triggers", 100, 60),    # PRD §7.18: webhook POST — 100/min
        ("/api/v1/errors/ingest", 10, 60), # PRD §7.18: error ingest — 10/min per session
        ("/mcp", 200, 60),                # PRD §7.18: general MCP tools — 200/min
        # NOTE: MCP trigger_pipeline tool has a separate 60/min limit enforced
        # in mcp_server.py at the application level since all MCP tools share
        # the same HTTP path. HITL review endpoints (20/min per PRD §7.18)
        # live under /api/v1/runs/{id}/hitl/ and are capped by the runs rule.
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
        path = request.url.path

        # 1. Auth principal set by outer middleware (MCP sub-app)
        principal = request.scope.get("auth_principal")
        if principal:
            if principal["type"] == "api_key":
                return f"ak:{principal['org_id']}:{principal['prefix']}:{path}"
            if principal["type"] == "user":
                return f"user:{principal['org_id']}:{principal['user_id']}:{path}"

        # 2. Parse Authorization header directly
        auth_header = request.headers.get("Authorization", "")
        if auth_header.startswith("Bearer "):
            token = auth_header[len("Bearer ") :].strip()

            if token.startswith("mk_"):
                prefix = token[3:11]
                return f"ak:none:{prefix}:{path}"

            try:
                claims = jose_jwt.get_unverified_claims(token)
                org_id = claims.get("org_id", "")
                user_id = claims.get("user_id", "") or claims.get("account_id", "")
                if org_id and user_id:
                    return f"user:{org_id}:{user_id}:{path}"
            except Exception as exc:
                _log.debug("ratelimit.jwt_decode_failed", extra={"error": str(exc)})

        # 3. Fallback to IP-based keying
        forwarded = request.headers.get("X-Forwarded-For", "")
        ip = forwarded.split(",")[0].strip() if forwarded else request.client.host if request.client else "unknown"
        return f"ip:{ip}:{path}"


# ---------------------------------------------------------------------------
# Auth-specific rate limiter
# ---------------------------------------------------------------------------

_auth_rate_limiter: AuthRateLimiterCls | None = None


def get_auth_rate_limiter(settings: Settings | None = None) -> AuthRateLimiterCls:
    """Return the singleton auth rate limiter, creating it if necessary."""
    global _auth_rate_limiter
    if _auth_rate_limiter is not None:
        return _auth_rate_limiter

    resolved = settings or get_settings()
    max_attempts = resolved.modulo_auth_max_attempts
    window_s = resolved.modulo_auth_window_seconds

    if not resolved.modulo_auth_rate_limit_enabled:
        _auth_rate_limiter = AuthRateLimiterCls(
            redis_client=None,
            max_attempts=max_attempts,
            window_s=window_s,
        )
        return _auth_rate_limiter

    if resolved.redis_url:
        try:
            from redis.asyncio import Redis

            client: Any = Redis.from_url(resolved.redis_url, decode_responses=False)
            _redis_clients.add(client)
            _auth_rate_limiter = AuthRateLimiterCls(
                redis_client=client,
                max_attempts=max_attempts,
                window_s=window_s,
            )
            return _auth_rate_limiter
        except Exception as exc:
            _log.warning("auth_ratelimit.redis_fallback", extra={"error": str(exc)})

    _auth_rate_limiter = AuthRateLimiterCls(
        redis_client=None,
        max_attempts=max_attempts,
        window_s=window_s,
    )
    return _auth_rate_limiter


class AuthRateLimitMiddleware(BaseHTTPMiddleware):
    """Rate-limits auth endpoints by IP with exponential backoff.

    Returns 429 with ``Retry-After`` header when the IP has exceeded
    the allowed number of failed attempts within the sliding window.
    """

    def __init__(
        self,
        app: FastAPI,
        settings: Settings | None = None,
        rate_limiter: AuthRateLimiterCls | None = None,
    ) -> None:
        super().__init__(app)
        resolved = settings or get_settings()
        self._bypass_token = resolved.modulo_ratelimit_bypass_token
        self._rate_limiter = rate_limiter or get_auth_rate_limiter(resolved)

    async def dispatch(self, request: Request, call_next: Any) -> Response:
        if not self._should_rate_limit(request):
            return await call_next(request)

        ip = self._client_ip(request)
        allowed, retry_after = await self._rate_limiter.check_login(ip)
        if not allowed:
            _log.warning("auth_ratelimit.exceeded", extra={"ip": ip, "retry_after": retry_after})
            return Response(
                status_code=HTTP_429_TOO_MANY_REQUESTS,
                content='{"detail":"Too many login attempts. Try again later.","error_code":"rate_limit_exceeded"}',
                media_type="application/json",
                headers={"Retry-After": str(retry_after)},
            )

        return await call_next(request)

    def _should_rate_limit(self, request: Request) -> bool:
        if request.method not in ("POST", "PUT", "PATCH"):
            return False
        token = request.headers.get(RATELIMIT_BYPASS_HEADER, "")
        if token and self._bypass_token and token == self._bypass_token:
            return False
        return request.url.path.startswith("/api/v1/auth/")

    @staticmethod
    def _client_ip(request: Request) -> str:
        forwarded = request.headers.get("X-Forwarded-For", "")
        if forwarded:
            return forwarded.split(",")[0].strip()
        if request.client:
            return request.client.host
        return "unknown"


async def shutdown_rate_limiters() -> None:
    """Close all Redis clients created by the rate limiter middleware.

    Call during application shutdown to release Redis connections.
    Safe to call multiple times — subsequent calls are no-ops once
    the set is empty.
    """
    global _redis_clients
    for client in list(_redis_clients):
        try:
            await client.aclose()
        except Exception:
            _log.exception("Failed to close rate limiter Redis client")
    _redis_clients.clear()
    _log.info("Rate limiter Redis clients closed")
