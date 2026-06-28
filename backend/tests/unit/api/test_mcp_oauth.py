"""Unit tests for /api/v1/mcp/oauth/* endpoints."""

import uuid
from collections.abc import AsyncGenerator, Generator
from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from modulo.api.dependencies import _get_engine, get_db_session
from modulo.api.main import app
from modulo.auth.dependencies import get_current_user
from modulo.auth.jwt import AuthenticatedPrincipal
from modulo.settings import Settings, get_settings

_VALID_32 = "a" * 32
_ORG_ID = uuid.UUID("00000000-0000-0000-0000-000000000001")
_USER_ID = uuid.UUID("00000000-0000-0000-0000-000000000002")
_NOW = datetime(2025, 6, 1, tzinfo=UTC)


def _make_settings() -> Settings:
    return Settings(
        database_url="postgresql+asyncpg://localhost/test",
        secret_key=_VALID_32,
        fernet_key=_VALID_32,
        modulo_admin_password="testpass",
        modulo_public_url="https://modulo.example.com",
    )


def _make_mock_session() -> AsyncMock:
    session = AsyncMock()
    begin_cm = AsyncMock()
    begin_cm.__aenter__ = AsyncMock(return_value=None)
    begin_cm.__aexit__ = AsyncMock(return_value=None)
    session.begin = MagicMock(return_value=begin_cm)
    return session


def _make_admin_principal() -> AuthenticatedPrincipal:
    return AuthenticatedPrincipal(
        username="admin",
        organisation_id=_ORG_ID,
        user_id=_USER_ID,
        org_role="admin",
    )


def _make_runner_principal() -> AuthenticatedPrincipal:
    return AuthenticatedPrincipal(
        username="runner",
        organisation_id=_ORG_ID,
        user_id=_USER_ID,
        org_role="runner",
    )


@pytest.fixture()
def admin_client() -> Generator[TestClient, None, None]:
    mock_session = _make_mock_session()

    async def override_session() -> AsyncGenerator[AsyncMock, None]:
        yield mock_session

    app.dependency_overrides[get_settings] = _make_settings
    app.dependency_overrides[get_db_session] = override_session
    app.dependency_overrides[_get_engine] = lambda: MagicMock()
    app.dependency_overrides[get_current_user] = _make_admin_principal
    yield TestClient(app)
    app.dependency_overrides.clear()


@pytest.fixture()
def runner_client() -> Generator[TestClient, None, None]:
    mock_session = _make_mock_session()

    async def override_session() -> AsyncGenerator[AsyncMock, None]:
        yield mock_session

    app.dependency_overrides[get_settings] = _make_settings
    app.dependency_overrides[get_db_session] = override_session
    app.dependency_overrides[_get_engine] = lambda: MagicMock()
    app.dependency_overrides[get_current_user] = _make_runner_principal
    yield TestClient(app)
    app.dependency_overrides.clear()


# ---------------------------------------------------------------------------
# POST /api/v1/mcp/oauth/clients
# ---------------------------------------------------------------------------


class TestRegisterOAuthClient:
    ENDPOINT = "/api/v1/mcp/oauth/clients"

    def test_create_returns_201_with_secret(self, admin_client: TestClient) -> None:
        with (
            patch("modulo.api.routes.mcp_oauth.create_oauth_client") as mock_create,
            patch("modulo.api.routes.mcp_oauth.set_rls_org"),
        ):
            mock_client = MagicMock()
            mock_client.id = uuid.uuid4()
            mock_client.client_id = "abc123def4567890"
            mock_client.name = "My App"
            mock_create.return_value = (mock_client, "raw_secret_40_chars_long_here")

            resp = admin_client.post(
                self.ENDPOINT,
                json={
                    "name": "My App",
                    "redirect_uris": ["https://app.example.com/callback"],
                    "scopes": ["trigger:run"],
                },
            )

        assert resp.status_code == 201
        body = resp.json()
        assert body["client_id"] == "abc123def4567890"
        assert body["client_secret"] == "raw_secret_40_chars_long_here"
        assert body["name"] == "My App"
        assert "id" in body

    def test_create_rejects_missing_name(self, admin_client: TestClient) -> None:
        resp = admin_client.post(
            self.ENDPOINT,
            json={"redirect_uris": ["http://localhost/cb"], "scopes": ["trigger:run"]},
        )
        assert resp.status_code == 422

    def test_create_rejects_empty_redirect_uris(self, admin_client: TestClient) -> None:
        resp = admin_client.post(
            self.ENDPOINT,
            json={"name": "App", "redirect_uris": [], "scopes": ["trigger:run"]},
        )
        assert resp.status_code == 422

    def test_create_rejects_empty_scopes(self, admin_client: TestClient) -> None:
        resp = admin_client.post(
            self.ENDPOINT,
            json={
                "name": "App",
                "redirect_uris": ["http://localhost/cb"],
                "scopes": [],
            },
        )
        assert resp.status_code == 422

    def test_create_runner_gets_403(self, runner_client: TestClient) -> None:
        resp = runner_client.post(
            self.ENDPOINT,
            json={
                "name": "App",
                "redirect_uris": ["http://localhost/cb"],
                "scopes": ["trigger:run"],
            },
        )
        assert resp.status_code == 403

    def test_create_disallows_invalid_scopes(self, admin_client: TestClient) -> None:
        with patch("modulo.api.routes.mcp_oauth.set_rls_org"):
            resp = admin_client.post(
                self.ENDPOINT,
                json={
                    "name": "App",
                    "redirect_uris": ["http://localhost/cb"],
                    "scopes": ["unknown:scope"],
                },
            )
        assert resp.status_code == 400

    def test_create_requires_public_url(self, admin_client: TestClient) -> None:
        def _settings_no_url() -> Settings:
            return Settings(
                database_url="postgresql+asyncpg://localhost/test",
                secret_key=_VALID_32,
                fernet_key=_VALID_32,
                modulo_admin_password="testpass",
                modulo_public_url="http://localhost:8000",
            )

        app.dependency_overrides[get_settings] = _settings_no_url
        try:
            with patch("modulo.api.routes.mcp_oauth.set_rls_org"):
                resp = admin_client.post(
                    self.ENDPOINT,
                    json={
                        "name": "App",
                        "redirect_uris": ["http://localhost/cb"],
                        "scopes": ["trigger:run"],
                    },
                )
            assert resp.status_code == 500
            assert "MODULO_PUBLIC_URL" in resp.json()["detail"]
        finally:
            app.dependency_overrides[get_settings] = _make_settings


# ---------------------------------------------------------------------------
# GET /api/v1/mcp/oauth/clients
# ---------------------------------------------------------------------------


class TestListOAuthClients:
    ENDPOINT = "/api/v1/mcp/oauth/clients"

    def test_list_returns_200(self, admin_client: TestClient) -> None:
        with (
            patch("modulo.api.routes.mcp_oauth.list_oauth_clients") as mock_list,
            patch("modulo.api.routes.mcp_oauth.set_rls_org"),
        ):
            mock_list.return_value = [
                {
                    "id": str(uuid.uuid4()),
                    "client_id": "cid1",
                    "name": "App 1",
                    "scopes": ["trigger:run"],
                    "redirect_uris": ["http://localhost/cb"],
                    "created_at": _NOW.isoformat(),
                }
            ]
            resp = admin_client.get(self.ENDPOINT)

        assert resp.status_code == 200
        body = resp.json()
        assert len(body) == 1
        assert body[0]["name"] == "App 1"

    def test_list_empty(self, admin_client: TestClient) -> None:
        with (
            patch("modulo.api.routes.mcp_oauth.list_oauth_clients") as mock_list,
            patch("modulo.api.routes.mcp_oauth.set_rls_org"),
        ):
            mock_list.return_value = []
            resp = admin_client.get(self.ENDPOINT)

        assert resp.status_code == 200
        assert resp.json() == []


# ---------------------------------------------------------------------------
# DELETE /api/v1/mcp/oauth/clients/{client_id}
# ---------------------------------------------------------------------------


class TestDeleteOAuthClient:
    ENDPOINT = "/api/v1/mcp/oauth/clients"

    def test_delete_returns_200(self, admin_client: TestClient) -> None:
        with (
            patch("modulo.api.routes.mcp_oauth.delete_oauth_client") as mock_delete,
            patch("modulo.api.routes.mcp_oauth.set_rls_org"),
        ):
            mock_delete.return_value = True
            resp = admin_client.delete(f"{self.ENDPOINT}/myclient123")

        assert resp.status_code == 200
        assert resp.json()["deleted"] is True

    def test_delete_not_found_returns_404(self, admin_client: TestClient) -> None:
        with (
            patch("modulo.api.routes.mcp_oauth.delete_oauth_client") as mock_delete,
            patch("modulo.api.routes.mcp_oauth.set_rls_org"),
        ):
            mock_delete.return_value = False
            resp = admin_client.delete(f"{self.ENDPOINT}/nonexistent")

        assert resp.status_code == 404

    def test_delete_runner_gets_403(self, runner_client: TestClient) -> None:
        resp = runner_client.delete(f"{self.ENDPOINT}/someclient")
        assert resp.status_code == 403


# ---------------------------------------------------------------------------
# Unauthenticated access
# ---------------------------------------------------------------------------


def test_list_returns_401_without_auth() -> None:
    app.dependency_overrides[get_settings] = _make_settings
    resp = TestClient(app).get("/api/v1/mcp/oauth/clients")
    assert resp.status_code in (401, 403)
    app.dependency_overrides.clear()
