"""OIDC login/callback route tests — DB-first provider resolution.

The login and callback routes are pre-auth and resolve OIDC providers from the
DB ``sso_providers`` table first (falling back to the env var). These tests
verify the routes still honour their public contract (307 redirect on success,
400 on unknown provider) and that they hand the DB session / resolved org to
the auth-layer functions.
"""

import json
import uuid
from collections.abc import Generator
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import AsyncSession

from modulo.api.dependencies import _get_engine, get_db_session, get_plan_context
from modulo.api.routes.sso import router as sso_router
from modulo.core.feature_flags import DbPlanContext, FeatureFlagRegistry
from modulo.settings import Settings, get_settings

_VALID_32 = "a" * 32


def _override(**kwargs: str | bool) -> Settings:
    base: dict[str, str | bool] = {
        "database_url": "postgresql+asyncpg://localhost/test",
        "secret_key": _VALID_32,
        "fernet_key": _VALID_32,
        "modulo_license_key": "test-license",
        "modulo_oidc_providers": json.dumps(
            [
                {
                    "provider_id": "google",
                    "client_id": "google-client-id",
                    "client_secret": "google-client-secret",
                    "discovery_url": "https://accounts.google.com/.well-known/openid-configuration",
                }
            ]
        ),
    }
    base.update(kwargs)
    return Settings(**base)  # type: ignore[arg-type]


@pytest.fixture(autouse=True)
def _set_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DATABASE_URL", "postgresql+asyncpg://localhost/test")
    monkeypatch.setenv("SECRET_KEY", _VALID_32)
    monkeypatch.setenv("FERNET_KEY", _VALID_32)
    get_settings.cache_clear()


_app = FastAPI()
_app.include_router(sso_router)


@pytest.fixture
def client() -> Generator[TestClient, None, None]:
    mock_session = AsyncMock(spec=AsyncSession)

    async def _override_session() -> AsyncMock:
        yield mock_session

    _app.dependency_overrides[get_settings] = lambda: _override()
    _app.dependency_overrides[get_db_session] = _override_session
    _app.dependency_overrides[_get_engine] = lambda: MagicMock()
    _app.dependency_overrides[get_plan_context] = lambda: DbPlanContext(FeatureFlagRegistry(current_tier="team"))
    try:
        yield TestClient(_app)
    finally:
        _app.dependency_overrides.clear()


def _fake_system_factory(session: AsyncMock) -> MagicMock:
    """Return a fake ``async_sessionmaker`` whose ``()`` yields ``session``.

    Mirrors the route's ``async with _new_system_session_factory()() as session``
    usage: calling the factory produces an async context manager that resolves
    to ``session``, and ``session.begin()`` (an ``AsyncMock``) supports ``async with``.
    """
    factory = MagicMock()
    cm = AsyncMock()
    cm.__aenter__.return_value = session
    cm.__aexit__.return_value = False
    factory.return_value = cm
    return factory


class TestOidcLoginRoute:
    def test_redirects_for_env_provider(self, client: TestClient) -> None:
        system_session = AsyncMock(spec=AsyncSession)
        with (
            patch(
                "modulo.api.routes.sso._new_system_session_factory",
                return_value=_fake_system_factory(system_session),
            ),
            patch("modulo.api.routes.sso.oidc_get_authorize_url", new_callable=AsyncMock) as mock_auth,
        ):
            mock_auth.return_value = ("https://accounts.google.com/o/oauth2/v2/auth?state=x", "raw-state")
            resp = client.get("/api/v1/auth/oidc/google/login", follow_redirects=False)

        assert resp.status_code == 307
        assert resp.headers["location"].startswith("https://accounts.google.com/o/oauth2/v2/auth")

    def test_passes_system_session_to_authorize_url(self, client: TestClient) -> None:
        """The pre-auth provider lookup must use the system session (BYPASSRLS)."""
        system_session = AsyncMock(spec=AsyncSession)
        with (
            patch(
                "modulo.api.routes.sso._new_system_session_factory",
                return_value=_fake_system_factory(system_session),
            ),
            patch("modulo.api.routes.sso.oidc_get_authorize_url", new_callable=AsyncMock) as mock_auth,
        ):
            mock_auth.return_value = ("https://idp.example.com/auth", "raw-state")
            client.get("/api/v1/auth/oidc/google/login", follow_redirects=False)

        mock_auth.assert_awaited_once()
        _, kwargs = mock_auth.await_args
        assert kwargs.get("session") is system_session
        assert kwargs.get("org_id") is None

    def test_unknown_provider_returns_400(self, client: TestClient) -> None:
        system_session = AsyncMock(spec=AsyncSession)
        with (
            patch(
                "modulo.api.routes.sso._new_system_session_factory",
                return_value=_fake_system_factory(system_session),
            ),
            patch("modulo.api.routes.sso.oidc_get_authorize_url", new_callable=AsyncMock) as mock_auth,
        ):
            mock_auth.side_effect = ValueError("OIDC provider 'ghost' not configured")
            resp = client.get("/api/v1/auth/oidc/ghost/login", follow_redirects=False)

        assert resp.status_code == 400
        assert "not configured" in resp.json()["detail"]

    def test_uses_db_provider_when_resolvable(self, client: TestClient) -> None:
        system_session = AsyncMock(spec=AsyncSession)

        with (
            patch(
                "modulo.api.routes.sso._new_system_session_factory",
                return_value=_fake_system_factory(system_session),
            ),
            patch("modulo.api.routes.sso.oidc_get_authorize_url", new_callable=AsyncMock) as mock_auth,
        ):
            mock_auth.return_value = ("https://okta.example.com/oauth2/v1/authorize?state=s", "raw")
            resp = client.get("/api/v1/auth/oidc/okta/login", follow_redirects=False)

        assert resp.status_code == 307
        mock_auth.assert_awaited_once()
        _, kwargs = mock_auth.await_args
        assert kwargs.get("session") is system_session
        assert kwargs.get("org_id") is None


class TestOidcCallbackRoute:
    def test_resolves_org_and_passes_to_callback(self, client: TestClient) -> None:
        org_id = uuid.uuid4()
        system_session = AsyncMock(spec=AsyncSession)

        with (
            patch(
                "modulo.api.routes.sso._new_system_session_factory",
                return_value=_fake_system_factory(system_session),
            ),
            patch("modulo.api.routes.sso.resolve_oidc_provider_org", new_callable=AsyncMock) as mock_resolve,
            patch("modulo.api.routes.sso.oidc_process_callback", new_callable=AsyncMock) as mock_cb,
        ):
            mock_resolve.return_value = org_id
            mock_cb.return_value = {
                "access_token": "at-oidc",
                "refresh_token": "rt-oidc",
                "token_type": "bearer",
            }

            resp = client.get(
                "/api/v1/auth/oidc/okta/callback?code=authcode&state=okta:xyz",
                follow_redirects=False,
            )

        assert resp.status_code == 307
        location = resp.headers.get("location", "")
        assert "access_token=at-oidc" in location
        assert "refresh_token=rt-oidc" in location
        # The pre-auth org resolution runs on the system session (BYPASSRLS).
        mock_resolve.assert_awaited_once()
        resolve_args, _ = mock_resolve.await_args
        assert resolve_args[0] is system_session
        # The callback keeps the DI session for JIT provisioning/token issuance
        # and passes the system session for the internal provider lookup.
        mock_cb.assert_awaited_once()
        args, kwargs = mock_cb.await_args
        assert args[0] == "authcode"
        assert kwargs.get("org_id") == org_id
        assert kwargs.get("session") is not system_session
        assert kwargs.get("provider_session") is system_session

    def test_env_fallback_when_provider_not_in_db(self, client: TestClient) -> None:
        system_session = AsyncMock(spec=AsyncSession)
        with (
            patch(
                "modulo.api.routes.sso._new_system_session_factory",
                return_value=_fake_system_factory(system_session),
            ),
            patch("modulo.api.routes.sso.resolve_oidc_provider_org", new_callable=AsyncMock) as mock_resolve,
            patch("modulo.api.routes.sso.oidc_process_callback", new_callable=AsyncMock) as mock_cb,
        ):
            mock_resolve.return_value = None
            mock_cb.return_value = {
                "access_token": "at-oidc",
                "refresh_token": "rt-oidc",
                "token_type": "bearer",
            }

            resp = client.get(
                "/api/v1/auth/oidc/google/callback?code=authcode&state=google:xyz",
                follow_redirects=False,
            )

        assert resp.status_code == 307
        mock_resolve.assert_awaited_once()
        resolve_args, _ = mock_resolve.await_args
        assert resolve_args[0] is system_session
        mock_cb.assert_awaited_once()
        _, kwargs = mock_cb.await_args
        assert kwargs.get("org_id") is None
        assert kwargs.get("provider_session") is system_session
