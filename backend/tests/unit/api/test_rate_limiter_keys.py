"""Unit tests for rate limit key derivation.

Verifies that RateLimitMiddleware._client_key() produces the correct
prefix based on the request's auth context.
"""

import uuid
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from modulo.api.middleware.rate_limiter import RateLimitMiddleware
from modulo.auth.jwt import create_access_token
from modulo.core.rate_limiter import RateLimiterRegistry
from modulo.settings import Settings

API_KEY_HEADER = "mk_abcdefgh_test1234567890123456789012"
API_KEY_ALT_HEADER = "mk_zyxwvuts_test1234567890123456789012"

JWT_SECRET = "a" * 32


def _make_settings() -> Settings:
    return Settings(
        database_url="postgresql+asyncpg://localhost/test",
        secret_key=JWT_SECRET,
        fernet_key="a" * 32,
        modulo_admin_password="testpass",
        modulo_ratelimit_bypass_token="test-bypass",
    )


def _make_app(registry: RateLimiterRegistry | None = None) -> FastAPI:
    app = FastAPI()

    @app.post("/api/v1/runs")
    async def create_run():
        return {"id": "run-1"}

    app.add_middleware(
        RateLimitMiddleware,
        settings=_make_settings(),
        registry=registry,
    )
    return app


def _valid_jwt(org_id: str | None = None, user_id: str | None = None) -> str:
    return create_access_token(
        subject="testuser",
        secret_key=JWT_SECRET,
        organisation_id=org_id or str(uuid.uuid4()),
        account_id=user_id or str(uuid.uuid4()),
        org_role="admin",
    )


class TestRateLimitKeyDerivation:
    """Tests for _client_key prefix selection."""

    @pytest.fixture
    def spy_registry(self) -> MagicMock:
        registry = MagicMock(spec=RateLimiterRegistry)
        registry.check = AsyncMock(return_value=True)
        return registry

    def _get_key(self, app: FastAPI, **kwargs: object) -> str:
        """Send a POST and return the rate-limit key passed to registry.check()."""
        with TestClient(app) as client:
            resp = client.post("/api/v1/runs", **kwargs)
        assert resp.status_code == 200
        call_args = self._spy_registry.check.call_args  # type: ignore[attr-defined]
        assert call_args is not None
        return call_args[0][0]

    # ---- ak: prefix tests ----

    def test_api_key_uses_ak_prefix(self, spy_registry: MagicMock) -> None:
        self._spy_registry = spy_registry
        app = _make_app(registry=spy_registry)
        key = self._get_key(app, headers={"Authorization": f"Bearer {API_KEY_HEADER}"})
        assert key.startswith("ak:")

    def test_api_key_includes_prefix(self, spy_registry: MagicMock) -> None:
        self._spy_registry = spy_registry
        app = _make_app(registry=spy_registry)
        key = self._get_key(app, headers={"Authorization": f"Bearer {API_KEY_HEADER}"})
        assert "abcdefgh" in key

    def test_api_key_includes_path(self, spy_registry: MagicMock) -> None:
        self._spy_registry = spy_registry
        app = _make_app(registry=spy_registry)
        key = self._get_key(app, headers={"Authorization": f"Bearer {API_KEY_HEADER}"})
        assert "/api/v1/runs" in key

    def test_different_api_keys_have_different_keys(self, spy_registry: MagicMock) -> None:
        self._spy_registry = spy_registry
        app = _make_app(registry=spy_registry)
        with TestClient(app) as client:
            client.post(
                "/api/v1/runs",
                headers={"Authorization": f"Bearer {API_KEY_HEADER}"},
            )
            key1 = spy_registry.check.call_args[0][0]
            client.post(
                "/api/v1/runs",
                headers={"Authorization": f"Bearer {API_KEY_ALT_HEADER}"},
            )
            key2 = spy_registry.check.call_args[0][0]
        assert key1 != key2

    # ---- user: prefix tests ----

    def test_jwt_uses_user_prefix(self, spy_registry: MagicMock) -> None:
        self._spy_registry = spy_registry
        app = _make_app(registry=spy_registry)
        jwt = _valid_jwt()
        key = self._get_key(app, headers={"Authorization": f"Bearer {jwt}"})
        assert key.startswith("user:")

    def test_jwt_includes_org_id(self, spy_registry: MagicMock) -> None:
        self._spy_registry = spy_registry
        org_id = str(uuid.uuid4())
        app = _make_app(registry=spy_registry)
        jwt = _valid_jwt(org_id=org_id)
        key = self._get_key(app, headers={"Authorization": f"Bearer {jwt}"})
        assert org_id in key

    def test_jwt_includes_user_id(self, spy_registry: MagicMock) -> None:
        self._spy_registry = spy_registry
        user_id = str(uuid.uuid4())
        app = _make_app(registry=spy_registry)
        jwt = _valid_jwt(user_id=user_id)
        key = self._get_key(app, headers={"Authorization": f"Bearer {jwt}"})
        assert user_id in key

    def test_different_users_get_different_keys(self, spy_registry: MagicMock) -> None:
        self._spy_registry = spy_registry
        app = _make_app(registry=spy_registry)
        jwt1 = _valid_jwt(org_id=str(uuid.uuid4()), user_id=str(uuid.uuid4()))
        jwt2 = _valid_jwt(org_id=str(uuid.uuid4()), user_id=str(uuid.uuid4()))
        with TestClient(app) as client:
            client.post(
                "/api/v1/runs",
                headers={"Authorization": f"Bearer {jwt1}"},
            )
            key1 = spy_registry.check.call_args[0][0]
            client.post(
                "/api/v1/runs",
                headers={"Authorization": f"Bearer {jwt2}"},
            )
            key2 = spy_registry.check.call_args[0][0]
        assert key1 != key2

    # ---- ip: prefix tests ----

    def test_unauthenticated_uses_ip_prefix(self, spy_registry: MagicMock) -> None:
        self._spy_registry = spy_registry
        app = _make_app(registry=spy_registry)
        key = self._get_key(app)
        assert key.startswith("ip:")

    def test_unauthenticated_includes_ip(self, spy_registry: MagicMock) -> None:
        self._spy_registry = spy_registry
        app = _make_app(registry=spy_registry)
        key = self._get_key(app)
        assert "unknown" in key or "testclient" in key

    def test_unauthenticated_includes_path(self, spy_registry: MagicMock) -> None:
        self._spy_registry = spy_registry
        app = _make_app(registry=spy_registry)
        key = self._get_key(app)
        assert "/api/v1/runs" in key

    def test_unauthenticated_with_xff_uses_first_ip(self, spy_registry: MagicMock) -> None:
        self._spy_registry = spy_registry
        app = _make_app(registry=spy_registry)
        key = self._get_key(app, headers={"X-Forwarded-For": "203.0.113.42, 10.0.0.1"})
        assert "203.0.113.42" in key
        assert key.startswith("ip:203.0.113.42:")

    # ---- backward compatibility ----

    def test_no_auth_falls_back_to_ip(self, spy_registry: MagicMock) -> None:
        """Unauthenticated requests still get rate limited via IP."""
        self._spy_registry = spy_registry
        app = _make_app(registry=spy_registry)
        key = self._get_key(app)
        assert key.startswith("ip:")

    def test_malformed_jwt_falls_back_to_ip(self, spy_registry: MagicMock) -> None:
        """If the JWT can't be decoded, fall back to IP."""
        self._spy_registry = spy_registry
        app = _make_app(registry=spy_registry)
        key = self._get_key(app, headers={"Authorization": "Bearer Not.A.Token.AtAll"})
        assert key.startswith("ip:")

    def test_empty_bearer_falls_back_to_ip(self, spy_registry: MagicMock) -> None:
        """Bearer token with empty value falls back to IP."""
        self._spy_registry = spy_registry
        app = _make_app(registry=spy_registry)
        key = self._get_key(app, headers={"Authorization": "Bearer "})
        assert key.startswith("ip:")
