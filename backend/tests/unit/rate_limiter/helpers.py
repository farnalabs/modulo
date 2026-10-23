"""Shared builders for the rate_limiter test package.

Each test module previously re-implemented the Settings builder with slightly
different shapes. Keeping it here means a change to the Settings contract only
has to be made once. Fixtures (``auth_redis``, ``_isolate_module_state``) live
in ``conftest.py`` — pytest only auto-applies fixtures from conftest files and
test modules, so defining them here was dead code that silently duplicated the
conftest versions.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock

from modulo.core.rate_limiter import AuthRateLimiter
from modulo.settings import Settings

# FAR-856: pre-auth anonymous GET endpoints and the GET_RULES prefix that must
# resolve them. Shared by test_middleware_internals (``_rule_for``) and
# test_rate_limit_middleware (``_should_rate_limit``) so adding an endpoint
# updates one list instead of two drifting copies. Keyed by pytest id.
ANONYMOUS_GET_RULES: dict[str, tuple[str, str]] = {
    "login-context": ("/api/v1/auth/login-context", "/api/v1/auth/login-context"),
    "org-login": ("/api/v1/auth/org-login/acme", "/api/v1/auth/org-login/"),
    "sso-providers": ("/api/v1/auth/sso/providers", "/api/v1/auth/sso/providers"),
    # FAR-2059: the SSO login legs now do tenant + licence resolution
    # before the handler, so they must be inside the GET budget.
    "oidc-login": ("/api/v1/auth/oidc/google/login", "/api/v1/auth/oidc/"),
    "oidc-callback": ("/api/v1/auth/oidc/google/callback", "/api/v1/auth/oidc/"),
    "saml-login": ("/api/v1/auth/saml/login", "/api/v1/auth/saml/"),
    "saml-metadata": ("/api/v1/auth/saml/acme/metadata", "/api/v1/auth/saml/"),
    "saml-provider-login": ("/api/v1/auth/saml/acme/login", "/api/v1/auth/saml/"),
}


def make_auth_limiter(
    redis_client: MagicMock,
    *,
    max_attempts: int = 10,
    window_s: int = 60,
) -> AuthRateLimiter:
    """Build the shared-prefix ``AuthRateLimiter`` used across middleware tests."""
    return AuthRateLimiter(redis_client=redis_client, max_attempts=max_attempts, window_s=window_s)


BASE_SETTINGS: dict[str, object] = {
    "database_url": "postgresql+asyncpg://localhost/test",
    "secret_key": "a" * 32,
    "fernet_key": "a" * 32,
    "modulo_admin_password": "testpass",
    "redis_url": "redis://localhost:6379/0",
}


def make_settings(**overrides: object) -> Settings:
    """Build a Settings instance, overriding any field via keyword args."""
    base = dict(BASE_SETTINGS)
    base.update(overrides)
    return Settings(**base)


def make_redis_client(*, auth: bool = False) -> MagicMock:
    """Build a MagicMock Redis client with a chainable transactional pipeline.

    ``auth=False`` configures the pipeline for the sliding-window limiter
    (execute returns a 4-tuple: zrem/zadd/zcard/expire); ``auth=True``
    configures it for AuthRateLimiter.check_login (execute returns a 2-tuple).
    """
    client = MagicMock()
    client.ttl = AsyncMock(return_value=0)
    pipe = MagicMock()
    pipe.zremrangebyscore = MagicMock(return_value=pipe)
    pipe.zadd = MagicMock(return_value=pipe)
    pipe.zcard = MagicMock(return_value=pipe)
    pipe.expire = MagicMock(return_value=pipe)
    pipe.delete = MagicMock(return_value=pipe)
    pipe.execute = AsyncMock(return_value=(None, None, 1, True) if not auth else (None, 0))
    client.pipeline = MagicMock(return_value=pipe)
    client.zadd = AsyncMock()
    client.expire = AsyncMock()
    client.delete = AsyncMock()
    client.setex = AsyncMock()
    return client


def make_mock_request(
    method: str = "POST",
    path: str = "/api/v1/runs",
    headers: dict[str, str] | None = None,
    scope: dict | None = None,
    client: Any = None,
) -> MagicMock:
    """Build a MagicMock stand-in for a starlette.Request.

    ``headers.get`` honours the same default-return contract as the real
    implementation so callers can simulate X-Forwarded-For / Authorization /
    bypass headers; ``client`` is attached verbatim to drive the
    ``client.host`` IP fallback.
    """
    req = MagicMock()
    req.method = method
    req.url.path = path
    req.scope = {} if scope is None else scope
    header_map = headers or {}
    req.headers.get = MagicMock(side_effect=lambda name, default="": header_map.get(name, default))
    req.client = client
    return req
