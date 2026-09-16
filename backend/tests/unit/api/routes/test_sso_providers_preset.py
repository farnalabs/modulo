"""Tests for public SSO provider discovery (FAR-853).

Covers:
- GET /api/v1/auth/sso/providers returns display_name and preset
- Response contains no secret fields
- Pre-auth route returns 200 without credentials
"""

from __future__ import annotations

from collections.abc import AsyncGenerator, Generator
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from modulo.api.dependencies import get_db_session, get_system_db_session
from modulo.api.main import app

_VALID_32 = "a" * 32


def _make_settings(**overrides: object) -> object:
    from modulo.settings import Settings

    kwargs: dict = {
        "database_url": "postgresql+asyncpg://localhost/test",
        "secret_key": _VALID_32,
        "fernet_key": _VALID_32,
        "modulo_admin_password": "testpass",
        "modulo_auth_rate_limit_enabled": False,
        "redis_url": "",
    }
    kwargs.update(overrides)
    return Settings(**kwargs)


def _make_provider(
    *,
    provider_id: str = "google",
    name: str = "Google OIDC",
    provider_type: str = "oidc",
    enabled: bool = True,
    preset: str = "custom",
    organisation_id: object = None,
) -> MagicMock:
    p = MagicMock()
    p.id = MagicMock()
    p.provider_id = provider_id
    p.name = name
    p.provider_type = provider_type
    p.enabled = enabled
    p.preset = preset
    p.organisation_id = organisation_id or MagicMock()
    p.client_secret = None
    p.client_id = None
    return p


def _make_mock_session(providers: list[MagicMock] | None = None) -> AsyncMock:
    session = AsyncMock()
    begin_cm = AsyncMock()
    begin_cm.__aenter__ = AsyncMock(return_value=None)
    begin_cm.__aexit__ = AsyncMock(return_value=False)
    session.begin = MagicMock(return_value=begin_cm)
    result = MagicMock()
    result.scalar_one_or_none.return_value = None
    result.first.return_value = None
    result.scalars.return_value.all.return_value = providers or []
    session.execute = AsyncMock(return_value=result)
    session.info = {}
    return session


@pytest.fixture
def client() -> Generator[tuple[TestClient, AsyncMock], None, None]:
    app_session = _make_mock_session()
    system_session = _make_mock_session()

    async def override_app_session() -> AsyncGenerator[AsyncMock, None]:
        yield app_session

    async def override_system_session() -> AsyncGenerator[AsyncMock, None]:
        yield system_session

    app.dependency_overrides[get_db_session] = override_app_session
    app.dependency_overrides[get_system_db_session] = override_system_session
    app.dependency_overrides["get_settings"] = lambda: _make_settings()
    yield TestClient(app), app_session
    app.dependency_overrides.clear()


class TestSsoProvidersDiscovery:
    def test_returns_display_name_and_preset(self, client: tuple[TestClient, AsyncMock]) -> None:
        """OIDC providers include display_name and preset in the response."""
        http, _session = client
        provider = _make_provider(provider_id="google", name="Google SSO", preset="google")

        mock_plan = MagicMock()
        mock_plan.feature_enabled.return_value = True

        with (
            patch("modulo.api.routes.sso._anonymous_plan_context", new=AsyncMock(return_value=mock_plan)),
            patch("modulo.api.routes.sso._list_enabled_oidc_global", new=AsyncMock(return_value=[provider])),
            patch("modulo.api.routes.sso._get_enabled_saml_global", new=AsyncMock(return_value=None)),
            patch("modulo.api.routes.sso.parse_oidc_providers", return_value=[]),
        ):
            resp = http.get("/api/v1/auth/sso/providers")

        assert resp.status_code == 200
        data = resp.json()
        assert len(data["oidc"]) == 1
        p = data["oidc"][0]
        assert p["provider_id"] == "google"
        assert p["display_name"] == "Google SSO"
        assert p["preset"] == "google"

    def test_no_secret_fields_in_response(self, client: tuple[TestClient, AsyncMock]) -> None:
        """Pre-auth response must not contain client_secret or client_id."""
        http, _session = client
        provider = _make_provider()

        mock_plan = MagicMock()
        mock_plan.feature_enabled.return_value = True

        with (
            patch("modulo.api.routes.sso._anonymous_plan_context", new=AsyncMock(return_value=mock_plan)),
            patch("modulo.api.routes.sso._list_enabled_oidc_global", new=AsyncMock(return_value=[provider])),
            patch("modulo.api.routes.sso._get_enabled_saml_global", new=AsyncMock(return_value=None)),
            patch("modulo.api.routes.sso.parse_oidc_providers", return_value=[]),
        ):
            resp = http.get("/api/v1/auth/sso/providers")

        assert resp.status_code == 200
        body = resp.text
        assert "client_secret" not in body.lower()
        assert "client_id" not in body.lower()

    def test_no_auth_returns_200(self, client: tuple[TestClient, AsyncMock]) -> None:
        """Pre-auth endpoint returns 200 without Authorization header."""
        http, _session = client

        mock_plan = MagicMock()
        mock_plan.feature_enabled.return_value = False

        with patch("modulo.api.routes.sso._anonymous_plan_context", new=AsyncMock(return_value=mock_plan)):
            resp = http.get("/api/v1/auth/sso/providers")

        assert resp.status_code == 200
        assert resp.status_code != 401
        data = resp.json()
        assert data["oidc"] == []
        assert data["saml"] is False

    def test_preset_field_set(self, client: tuple[TestClient, AsyncMock]) -> None:
        """Each OIDC entry has exactly provider_id, display_name, preset — no extras."""
        http, _session = client
        provider = _make_provider(provider_id="okta", name="Okta", preset="okta")

        mock_plan = MagicMock()
        mock_plan.feature_enabled.return_value = True

        with (
            patch("modulo.api.routes.sso._anonymous_plan_context", new=AsyncMock(return_value=mock_plan)),
            patch("modulo.api.routes.sso._list_enabled_oidc_global", new=AsyncMock(return_value=[provider])),
            patch("modulo.api.routes.sso._get_enabled_saml_global", new=AsyncMock(return_value=None)),
            patch("modulo.api.routes.sso.parse_oidc_providers", return_value=[]),
        ):
            resp = http.get("/api/v1/auth/sso/providers")

        assert resp.status_code == 200
        data = resp.json()
        p = data["oidc"][0]
        assert set(p.keys()) == {"provider_id", "display_name", "preset"}
