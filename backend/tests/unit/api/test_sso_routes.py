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

import httpx
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


class TestOidcLoginRoute:
    def test_redirects_for_env_provider(self, client: TestClient) -> None:
        with patch("modulo.api.routes.sso.oidc_get_authorize_url", new_callable=AsyncMock) as mock_auth:
            mock_auth.return_value = ("https://accounts.google.com/o/oauth2/v2/auth?state=x", "raw-state")
            resp = client.get("/api/v1/auth/oidc/google/login", follow_redirects=False)

        assert resp.status_code == 307
        assert resp.headers["location"].startswith("https://accounts.google.com/o/oauth2/v2/auth")

    def test_passes_db_session_to_authorize_url(self, client: TestClient) -> None:
        with patch("modulo.api.routes.sso.oidc_get_authorize_url", new_callable=AsyncMock) as mock_auth:
            mock_auth.return_value = ("https://idp.example.com/auth", "raw-state")
            client.get("/api/v1/auth/oidc/google/login", follow_redirects=False)

        mock_auth.assert_awaited_once()
        _, kwargs = mock_auth.await_args
        assert kwargs.get("session") is not None
        assert kwargs.get("org_id") is None

    def test_unknown_provider_returns_400(self, client: TestClient) -> None:
        with patch("modulo.api.routes.sso.oidc_get_authorize_url", new_callable=AsyncMock) as mock_auth:
            mock_auth.side_effect = ValueError("OIDC provider 'ghost' not configured")
            resp = client.get("/api/v1/auth/oidc/ghost/login", follow_redirects=False)

        assert resp.status_code == 400
        assert "not configured" in resp.json()["detail"]

    def test_uses_db_provider_when_resolvable(self, client: TestClient) -> None:
        db_provider = {
            "provider_id": "okta",
            "client_id": "okta-client-id",
            "client_secret": "okta-client-secret",
            "discovery_url": "https://okta.example.com/.well-known/openid-configuration",
        }

        with patch("modulo.api.routes.sso.list_oidc_providers", new_callable=AsyncMock) as mock_list:
            mock_list.return_value = [db_provider]
            with patch("modulo.api.routes.sso.oidc_get_authorize_url", new_callable=AsyncMock) as mock_auth:
                mock_auth.return_value = ("https://okta.example.com/oauth2/v1/authorize?state=s", "raw")
                resp = client.get("/api/v1/auth/oidc/okta/login", follow_redirects=False)

        assert resp.status_code == 307
        mock_auth.assert_awaited_once()
        _, kwargs = mock_auth.await_args
        assert kwargs.get("session") is not None
        assert kwargs.get("org_id") is None


def _run_callback_route_test(
    client: TestClient, provider_id: str, state: str, org_id: uuid.UUID | None
) -> tuple[httpx.Response, AsyncMock, AsyncMock]:
    with (
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
            f"/api/v1/auth/oidc/{provider_id}/callback?code=authcode&state={state}",
            follow_redirects=False,
        )
    return resp, mock_resolve, mock_cb


class TestOidcCallbackRoute:
    def test_resolves_org_and_passes_to_callback(self, client: TestClient) -> None:
        org_id = uuid.uuid4()
        resp, _mock_resolve, mock_cb = _run_callback_route_test(client, "okta", "okta:xyz", org_id)

        assert resp.status_code == 307
        location = resp.headers.get("location", "")
        assert "access_token=at-oidc" in location
        assert "refresh_token=rt-oidc" in location
        mock_cb.assert_awaited_once()
        args, kwargs = mock_cb.await_args
        assert args[0] == "authcode"
        assert kwargs.get("org_id") == org_id

    def test_env_fallback_when_provider_not_in_db(self, client: TestClient) -> None:
        resp, mock_resolve, mock_cb = _run_callback_route_test(client, "google", "google:xyz", None)

        assert resp.status_code == 307
        mock_resolve.assert_awaited_once()
        mock_cb.assert_awaited_once()
        _, kwargs = mock_cb.await_args
        assert kwargs.get("org_id") is None
