"""Rate limiting middleware — Redis-backed sliding window with in-memory fallback.

NOTE: This middleware accepts an optional `Settings` object and an optional
`RateLimiterRegistry` in the constructor. When running inside a FastAPI app
that overrides `get_settings` (e.g. in tests), tests should pass settings
explicitly rather than relying on the module-level `get_settings()` call.
"""

import asyncio
import hmac
import logging
import re
from collections.abc import Awaitable, Callable
from typing import Any, ClassVar

import jwt
from fastapi import FastAPI, Request, Response
from starlette.middleware.base import BaseHTTPMiddleware

from modulo.api.models.problem import ProblemDetail, ProblemType
from modulo.core.rate_limiter import AuthRateLimiter as AuthRateLimiterCls
from modulo.core.rate_limiter import RateLimiterRegistry, RateLimitRule, TokenBucket
from modulo.settings import Settings, get_settings

RATELIMIT_BYPASS_HEADER = "MODULO_RATELIMIT_BYPASS_TOKEN"

# FAR-535: process-local floor for the demo auto-login path. The demo
# endpoint mints real sessions with zero user input, so its 10/hour cap must
# survive EVERY degraded registry state:
#   * Redis configured and healthy — the shared registry enforces the rule.
#   * Redis unconfigured (or sqlite mode) — the registry is _NoopRateLimiter,
#     which allows everything; the floor enforces 10/hour per-process.
#   * Redis configured but failing at request time — the registry check
#     raises and the middleware would normally fail open; for the demo rule
#     the floor engages instead. Other routes keep failing open.
DEMO_RULE_PREFIX = "/api/v1/auth/demo"
_DEMO_FLOOR_RATE_PER_HOUR = 10
_DEMO_FLOOR_BURST = 10
_DEMO_FLOOR_MAX_KEYS = 1024  # trivial bound: drop-oldest on overflow, no background cleanup
_demo_floor_buckets: dict[str, TokenBucket] = {}


async def _consume_demo_floor(key: str) -> bool:
    """Consume one token from the per-key process-local demo bucket.

    Keys are RateLimitMiddleware client keys (per-IP for anonymous demo
    traffic, whose path segment is constant). Bounded: when the dict exceeds
    ``_DEMO_FLOOR_MAX_KEYS`` the oldest entry is dropped — a deliberate
    simplest-possible bound, no background sweeper.
    """
    bucket = _demo_floor_buckets.get(key)
    if bucket is None:
        if len(_demo_floor_buckets) >= _DEMO_FLOOR_MAX_KEYS:
            oldest = next(iter(_demo_floor_buckets))
            del _demo_floor_buckets[oldest]
        bucket = TokenBucket(rate=_DEMO_FLOOR_RATE_PER_HOUR / 3600, burst=_DEMO_FLOOR_BURST)
        _demo_floor_buckets[key] = bucket
    return await bucket.consume()


redis_available: bool = False


# Tracked Redis clients for graceful shutdown.
_log = logging.getLogger(__name__)

_redis_clients: set[Any] = set()

# Pattern to strip variable UUID segments from HITL paths to prevent
# per-segment bucket rotation (FAR-1304).
_RE_VARIABLE_SEGMENT = re.compile(
    r"/runs/[0-9a-f-]+/hitl/[0-9a-f-]+",
    re.IGNORECASE,
)


class _NoopRateLimiter:
    """No-op rate limiter used when Redis is unavailable — allows all requests."""

    async def check(self, _key: str, max_requests: int, window_s: int = 60) -> bool:
        return True


def _matches_bypass_token(token: str, bypass_token: str) -> bool:
    """Return whether ``token`` matches the bypass secret using a constant-time comparison.

    The bypass token is a shared secret that disables rate limiting (including the
    auth lockout), so it must not be compared with ``==`` which short-circuits on the
    first mismatching byte and leaks a timing oracle.
    """
    if not token or not bypass_token:
        return False
    return hmac.compare_digest(token, bypass_token)


def _create_registry(settings: Settings) -> RateLimiterRegistry | _NoopRateLimiter:
    """Create a rate limiter registry, connecting to Redis if configured."""
    global redis_available

    if settings.modulo_db.lower() == "sqlite":
        _log.info("ratelimit.sqlite_disabled")
        redis_available = False
        return _NoopRateLimiter()

    if settings.redis_url:
        try:
            from redis.asyncio import Redis

            client: Any = Redis.from_url(
                settings.redis_url, decode_responses=False, socket_connect_timeout=5, socket_timeout=10
            )
            _redis_clients.add(client)
            registry = RateLimiterRegistry(redis_client=client)
            redis_available = True
            _log.info("ratelimit.redis_enabled")
            return registry
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            _log.warning("ratelimit.redis_fallback", extra={"error": str(exc)})

    redis_available = False
    _log.warning("ratelimit.in_memory_mode")
    return _NoopRateLimiter()


class RateLimitMiddleware(BaseHTTPMiddleware):
    """Rate limits per-route based on pre-defined rules.

    Uses Redis sliding window (ZADD + ZREMRANGEBYSCORE) when Redis is
    configured and reachable. In the two degraded states — Redis unconfigured
    (the registry is _NoopRateLimiter) and Redis configured but failing at
    request time (the registry check raises) — requests normally fail open,
    except the demo auto-login rule, which falls back to the process-local
    token-bucket floor (see ``_consume_demo_floor``).

    Accepts optional ``settings`` and ``registry`` constructor params.
    When provided, these are used instead of calling ``get_settings()``
    and ``_create_registry()`` — this allows tests to inject overrides.
    """

    RULES: ClassVar[list[RateLimitRule]] = [
        # FAR-535: demo auto-login — 10/hour per IP. Strict: the endpoint mints
        # a real session with zero user input, so it must be expensive to abuse.
        # Enforced by the Redis registry when it can enforce (healthy Redis),
        # and by the process-local token-bucket floor (see _consume_demo_floor)
        # whenever the registry cannot — noop registry (Redis unconfigured /
        # sqlite mode) or a runtime check failure (Redis configured but
        # failing). Declared FIRST: _rule_for returns the first matching
        # prefix and no other rule overlaps /api/v1/auth/demo, but leading
        # specificity keeps future prefix additions from silently overriding
        # this limit.
        RateLimitRule(path_prefix="/api/v1/auth/demo", max_requests=10, window_s=3600),
        # PRD §7.18: POST /api/v1/runs — 60/min
        RateLimitRule(path_prefix="/api/v1/runs", max_requests=60, window_s=60),
        # PRD §7.18: webhook POST — 100/min
        RateLimitRule(path_prefix="/api/v1/triggers", max_requests=100, window_s=60),
        # PRD §7.18: error ingest — 10/min per session
        RateLimitRule(path_prefix="/api/v1/errors/ingest", max_requests=10, window_s=60),
        # PRD §7.18: general MCP tools — 200/min
        RateLimitRule(path_prefix="/mcp", max_requests=200, window_s=60),
        # NOTE: MCP trigger_pipeline tool has a separate 60/min limit enforced
        # in mcp_server.py at the application level since all MCP tools share
        # the same HTTP path.
    ]

    # PRD §7.18: HITL review actions — 20/min per user. The review endpoints
    # live under /api/v1/runs/{run_id}/hitl/{gate_id}/ where the run/gate ids
    # are variable, so a static prefix cannot match them; the "/hitl/" marker
    # is matched as a path segment by _rule_for / _should_rate_limit. This is
    # more restrictive than the /api/v1/runs rule (60/min) that would
    # otherwise apply to these paths.
    HITL_RULE: ClassVar[RateLimitRule] = RateLimitRule(path_prefix="/hitl/", max_requests=20, window_s=60)

    @classmethod
    def set_rules(cls, rules: list[RateLimitRule]) -> None:
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
        self._secret_key = resolved.secret_key
        self._registry = registry or _create_registry(resolved)

    async def dispatch(self, request: Request, call_next: Callable[[Request], Awaitable[Response]]) -> Response:
        if self._should_rate_limit(request):
            client_key = self._client_key(request)
            rule = self._rule_for(request)

            registry_failed = False
            try:
                allowed = await self._registry.check(
                    client_key,
                    max_requests=rule.max_requests,
                    window_s=rule.window_s,
                )
            except asyncio.CancelledError:
                raise
            except Exception:
                registry_failed = True
                _log.warning(
                    "ratelimit.check_failed",
                    extra={"client_key": client_key},
                    exc_info=True,
                )
                allowed = True
            if rule.path_prefix == DEMO_RULE_PREFIX and (
                registry_failed or isinstance(self._registry, _NoopRateLimiter)
            ):
                # FAR-535 demo floor: engaged whenever the shared registry
                # cannot enforce the demo cap — either it degrades to the noop
                # limiter (Redis unconfigured / sqlite mode) or its runtime
                # check raises (Redis configured but failing). The bucket's
                # verdict is honoured (429 with Retry-After on exhaustion);
                # a failing registry must NOT bypass the cap. Other routes
                # keep the existing fail-open behaviour (allowed stays True).
                allowed = await _consume_demo_floor(client_key)
            if not allowed:
                _log.warning("ratelimit.exceeded", extra={"client_key": client_key})
                return ProblemDetail.from_type(
                    ProblemType.RATE_LIMITED,
                    detail="Rate limit exceeded. Try again later.",
                ).to_response(headers={"Retry-After": str(rule.window_s)})
        response: Response = await call_next(request)
        return response

    def _should_rate_limit(self, request: Request) -> bool:
        if request.method not in ("POST", "PUT", "PATCH"):
            return False
        token = request.headers.get(RATELIMIT_BYPASS_HEADER, "")
        if _matches_bypass_token(token, self._bypass_token or ""):
            return False
        path = request.url.path
        if self.HITL_RULE.path_prefix in path:
            return True
        return any(path.startswith(rule.path_prefix) for rule in self.RULES)

    def _rule_for(self, request: Request) -> RateLimitRule:
        path = request.url.path
        if self.HITL_RULE.path_prefix in path:
            return self.HITL_RULE
        for rule in self.RULES:
            if path.startswith(rule.path_prefix):
                return rule
        return RateLimitRule(path_prefix="", max_requests=0, window_s=0)

    def _client_key(self, request: Request) -> str:
        path = request.url.path

        # Normalize HITL paths to strip variable run/gate UUIDs, preventing
        # per-segment bucket rotation on variable-path endpoints.
        if "/hitl/" in path:
            path = _RE_VARIABLE_SEGMENT.sub("/runs/<run_id>/hitl/<gate_id>", path)

        # 1. Auth principal set by outer middleware (MCP sub-app)
        principal = request.scope.get("auth_principal")
        if principal:
            if principal["type"] == "api_key":
                return f"ak:{principal['org_id']}:{principal['prefix']}:{path}"
            if principal["type"] == "user":
                return f"user:{principal['org_id']}:{principal['user_id']}:{path}"

        # 2. Parse Authorization header — verify JWT before trusting claims
        auth_header = request.headers.get("Authorization", "")
        if auth_header.startswith("Bearer "):
            token = auth_header[len("Bearer ") :].strip()

            # mk_ API keys: use prefix + path (no bucket rotation via prefix)
            if token.startswith("mk_"):
                prefix = token[3:11]
                return f"ak:none:{prefix}:{path}"

            # JWT: verify signature before trusting claims for rate-limit bucketing.
            # Unverified claims can be forged to rotate buckets.
            try:
                claims = jwt.decode(token, self._secret_key, algorithms=["HS256"])
                org_id = claims.get("org_id", "")
                user_id = claims.get("user_id", "") or claims.get("account_id", "")
                if org_id and user_id:
                    return f"user:{org_id}:{user_id}:{path}"
            except Exception as exc:
                _log.debug("ratelimit.jwt_decode_failed", extra={"error": str(exc)})

        # 3. Fallback to IP-based keying — use request.client.host (the
        #    actual peer IP) instead of the first X-Forwarded-For value,
        #    which is attacker-controlled when the proxy doesn't strip it.
        ip = "unknown"
        if request.client is not None and request.client.host:
            ip = request.client.host
        elif forwarded := request.headers.get("X-Forwarded-For", ""):
            ip = forwarded.split(",")[0].strip()
        return f"ip:{ip}:{path}"


# ---------------------------------------------------------------------------
# Auth-specific rate limiter
# ---------------------------------------------------------------------------

_auth_rate_limiter: AuthRateLimiterCls | None = None


def get_auth_rate_limiter(settings: Settings | None = None) -> AuthRateLimiterCls | None:
    """Return the singleton auth rate limiter, creating it if necessary.

    Returns None when ``modulo_auth_rate_limit_enabled`` is False —
    callers should skip rate limiting entirely.
    """
    global _auth_rate_limiter
    if _auth_rate_limiter is not None:
        return _auth_rate_limiter

    resolved = settings or get_settings()
    max_attempts = resolved.modulo_auth_max_attempts
    window_s = resolved.modulo_auth_window_seconds

    if not resolved.modulo_auth_rate_limit_enabled:
        _auth_rate_limiter = None
        return None

    if not resolved.redis_url:
        _log.warning("auth_ratelimit.no_redis_url")
        return None

    try:
        from redis.asyncio import Redis

        client: Any = Redis.from_url(
            resolved.redis_url, decode_responses=False, socket_connect_timeout=5, socket_timeout=10
        )
        _redis_clients.add(client)
        _auth_rate_limiter = AuthRateLimiterCls(
            redis_client=client,
            max_attempts=max_attempts,
            window_s=window_s,
        )
        return _auth_rate_limiter
    except asyncio.CancelledError:
        raise
    except Exception:
        _log.warning("auth_ratelimit.redis_connect_failed", exc_info=True)
        return None


class AuthRateLimitMiddleware(BaseHTTPMiddleware):
    """Rate-limits auth endpoints by IP with exponential backoff.

    Returns 429 with ``Retry-After`` header when the IP has exceeded
    the allowed number of failed attempts within the sliding window.
    FAR-535: the demo auto-login path (/api/v1/auth/demo) is exempt —
    the demo path never touches the shared AuthRateLimiter at any layer.
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

    async def dispatch(self, request: Request, call_next: Callable[[Request], Awaitable[Response]]) -> Response:
        if not self._should_rate_limit(request):
            return await call_next(request)

        if self._rate_limiter is None:
            return await call_next(request)

        ip = self._client_ip(request)
        allowed, retry_after = await self._rate_limiter.check_login(ip)
        if not allowed:
            _log.warning("auth_ratelimit.exceeded", extra={"ip": ip, "retry_after": retry_after})
            return ProblemDetail.from_type(
                ProblemType.RATE_LIMITED,
                detail="Too many login attempts. Try again later.",
            ).to_response(headers={"Retry-After": str(retry_after)})

        return await call_next(request)

    def _should_rate_limit(self, request: Request) -> bool:
        if request.method not in ("POST", "PUT", "PATCH"):
            return False
        token = request.headers.get(RATELIMIT_BYPASS_HEADER, "")
        if _matches_bypass_token(token, self._bypass_token or ""):
            return False
        path = request.url.path
        # FAR-535: the demo auto-login path NEVER touches the shared
        # AuthRateLimiter — at ANY layer, handler AND middleware. Exempting it
        # here (mirroring the /hitl/ special-case in RateLimitMiddleware) means
        # demo requests can neither inherit /login lockouts (429) nor re-arm
        # lockout keys (setex). Its abuse cap is the RateLimitMiddleware demo
        # rule (10/hour, with the process-local floor when Redis is absent).
        if path.startswith(DEMO_RULE_PREFIX):
            return False
        return path.startswith("/api/v1/auth/")

    @staticmethod
    def _client_ip(request: Request) -> str:
        if request.client is not None and request.client.host:
            return request.client.host
        forwarded = request.headers.get("X-Forwarded-For", "")
        if forwarded:
            return forwarded.split(",")[0].strip()
        return "unknown"


async def shutdown_rate_limiters() -> None:
    """Close all Redis clients created by the rate limiter middleware.

    Call during application shutdown to release Redis connections.
    Safe to call multiple times — subsequent calls are no-ops once
    the set is empty.
    """
    for client in list(_redis_clients):
        try:
            await client.aclose()
        except asyncio.CancelledError:
            raise
        except Exception:
            _log.exception("Failed to close rate limiter Redis client")
    _redis_clients.clear()
    _log.info("Rate limiter Redis clients closed")
