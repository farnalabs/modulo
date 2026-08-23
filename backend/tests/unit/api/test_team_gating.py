"""Tests for team_rbac feature gating on team endpoints."""

import uuid
from collections.abc import AsyncGenerator, Generator
from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from modulo.api.dependencies import _get_engine, get_db_session, get_plan_context
from modulo.api.main import app
from modulo.auth.dependencies import get_current_user
from modulo.auth.jwt import AuthenticatedPrincipal
from modulo.settings import Settings, get_settings
from tests.unit.api.mock_session import configure_mock_session

_VALID_32 = "a" * 32
_ORG_ID = uuid.UUID("00000000-0000-0000-0000-000000000001")
_USER_ID = uuid.UUID("00000000-0000-0000-0000-000000000002")
_TEAM_ID = uuid.UUID("00000000-0000-0000-0000-000000000003")
_KEY_ID = uuid.UUID("00000000-0000-0000-0000-000000000004")
_NOW = datetime(2025, 1, 1, tzinfo=UTC)


def _make_settings(*, has_license: bool = False) -> Settings:
    return Settings(
        database_url="postgresql+asyncpg://localhost/test",
        secret_key=_VALID_32,
        fernet_key=_VALID_32,
        modulo_admin_password="testpass",
        modulo_license_key="license-key" if has_license else "",
    )


def _make_team(**overrides: object) -> MagicMock:
    t = MagicMock()
    t.id = overrides.get("id", _TEAM_ID)
    t.organisation_id = overrides.get("organisation_id", _ORG_ID)
    t.name = overrides.get("name", "Test Team")
    t.description = overrides.get("description")
    t.created_by = overrides.get("created_by", _USER_ID)
    t.created_at = _NOW
    return t


def _make_mock_session() -> AsyncMock:
    session = configure_mock_session(AsyncMock())
    begin_cm = AsyncMock()
    begin_cm.__aenter__ = AsyncMock(return_value=None)
    begin_cm.__aexit__ = AsyncMock(return_value=False)
    session.begin = MagicMock(return_value=begin_cm)
    begin_nested_cm = AsyncMock()
    begin_nested_cm.__aenter__ = AsyncMock(return_value=None)
    begin_nested_cm.__aexit__ = AsyncMock(return_value=False)
    session.begin_nested = MagicMock(return_value=begin_nested_cm)
    scalar_result = MagicMock()
    scalar_result.scalar.return_value = 0
    session.execute.return_value = scalar_result
    return session


@pytest.fixture
def free_client() -> Generator[TestClient, None, None]:
    """Client with no license key — team_rbac is disabled."""
    mock_session = _make_mock_session()

    async def override_session() -> AsyncGenerator[AsyncMock, None]:
        yield mock_session

    app.dependency_overrides[get_settings] = lambda: _make_settings(has_license=False)
    app.dependency_overrides[get_db_session] = override_session
    app.dependency_overrides[_get_engine] = lambda: MagicMock()
    app.dependency_overrides[get_current_user] = lambda: AuthenticatedPrincipal(
        username="testuser",
        organisation_id=_ORG_ID,
        account_id=_USER_ID,
        org_role="admin",
    )
    mock_plan = MagicMock()
    mock_plan.feature_enabled.return_value = False
    app.dependency_overrides[get_plan_context] = lambda: mock_plan
    yield TestClient(app)
    app.dependency_overrides.clear()


@pytest.fixture
def licensed_client() -> Generator[TestClient, None, None]:
    """Client with a license key — team_rbac is enabled."""
    mock_session = _make_mock_session()

    async def override_session() -> AsyncGenerator[AsyncMock, None]:
        yield mock_session

    app.dependency_overrides[get_settings] = lambda: _make_settings(has_license=True)
    app.dependency_overrides[get_db_session] = override_session
    app.dependency_overrides[_get_engine] = lambda: MagicMock()
    app.dependency_overrides[get_current_user] = lambda: AuthenticatedPrincipal(
        username="testuser",
        organisation_id=_ORG_ID,
        account_id=_USER_ID,
        org_role="admin",
    )
    mock_plan = MagicMock()
    mock_plan.feature_enabled.return_value = True
    app.dependency_overrides[get_plan_context] = lambda: mock_plan
    yield TestClient(app)
    app.dependency_overrides.clear()


class TestTeamGating:
    """Team /api/v1/teams endpoints return 402 when team_rbac is disabled."""

    def test_list_teams_returns_402_when_disabled(self, free_client: TestClient) -> None:
        resp = free_client.get("/api/v1/teams")
        assert resp.status_code == 402
        assert "team_rbac" in resp.json()["detail"].lower()

    def test_create_team_returns_402_when_disabled(self, free_client: TestClient) -> None:
        resp = free_client.post("/api/v1/teams", json={"name": "New Team"})
        assert resp.status_code == 402
        assert "team_rbac" in resp.json()["detail"].lower()

    def test_get_team_returns_402_when_disabled(self, free_client: TestClient) -> None:
        resp = free_client.get(f"/api/v1/teams/{_TEAM_ID}")
        assert resp.status_code == 402
        assert "team_rbac" in resp.json()["detail"].lower()

    def test_update_team_returns_402_when_disabled(self, free_client: TestClient) -> None:
        resp = free_client.patch(f"/api/v1/teams/{_TEAM_ID}", json={"name": "Updated"})
        assert resp.status_code == 402
        assert "team_rbac" in resp.json()["detail"].lower()

    def test_delete_team_returns_402_when_disabled(self, free_client: TestClient) -> None:
        resp = free_client.delete(f"/api/v1/teams/{_TEAM_ID}")
        assert resp.status_code == 402
        assert "team_rbac" in resp.json()["detail"].lower()

    def test_list_members_returns_402_when_disabled(self, free_client: TestClient) -> None:
        resp = free_client.get(f"/api/v1/teams/{_TEAM_ID}/members")
        assert resp.status_code == 402
        assert "team_rbac" in resp.json()["detail"].lower()

    def test_add_member_returns_402_when_disabled(self, free_client: TestClient) -> None:
        resp = free_client.post(
            f"/api/v1/teams/{_TEAM_ID}/members",
            json={"user_id": str(_USER_ID), "role": "viewer"},
        )
        assert resp.status_code == 402
        assert "team_rbac" in resp.json()["detail"].lower()

    def test_remove_member_returns_402_when_disabled(self, free_client: TestClient) -> None:
        resp = free_client.delete(f"/api/v1/teams/{_TEAM_ID}/members/{uuid.uuid4()}")
        assert resp.status_code == 402
        assert "team_rbac" in resp.json()["detail"].lower()


class TestAdminTeamGating:
    """Admin /api/v1/admin/teams endpoints return 402 when team_rbac is disabled."""

    def test_admin_create_team_returns_402_when_disabled(self, free_client: TestClient) -> None:
        resp = free_client.post("/api/v1/admin/teams", json={"name": "Admin Team"})
        assert resp.status_code == 402
        assert "team_rbac" in resp.json()["detail"].lower()

    def test_admin_list_teams_returns_402_when_disabled(self, free_client: TestClient) -> None:
        resp = free_client.get("/api/v1/admin/teams")
        assert resp.status_code == 402
        assert "team_rbac" in resp.json()["detail"].lower()

    def test_admin_update_team_returns_402_when_disabled(self, free_client: TestClient) -> None:
        resp = free_client.put(f"/api/v1/admin/teams/{_TEAM_ID}", json={"name": "Updated"})
        assert resp.status_code == 402
        assert "team_rbac" in resp.json()["detail"].lower()

    def test_admin_delete_team_returns_402_when_disabled(self, free_client: TestClient) -> None:
        resp = free_client.delete(f"/api/v1/admin/teams/{_TEAM_ID}")
        assert resp.status_code == 402
        assert "team_rbac" in resp.json()["detail"].lower()


class TestTeamSuccess:
    """Team endpoints succeed when team_rbac is enabled."""

    def test_list_teams_succeeds_when_enabled(self, licensed_client: TestClient) -> None:
        page_result = MagicMock(items=[_make_team()], total=1, page=1, page_size=20)
        with (
            patch("modulo.api.routes.teams.list_teams", return_value=page_result),
            patch("modulo.api.routes.teams.set_rls_org"),
            patch("modulo.api.routes.teams.set_rls_user_context"),
        ):
            resp = licensed_client.get("/api/v1/teams")
        assert resp.status_code == 200

    def test_create_team_succeeds_when_enabled(self, licensed_client: TestClient) -> None:
        with (
            patch("modulo.api.routes.teams.create_team", return_value=_make_team(name="New Team")),
            patch("modulo.api.routes.teams.get_team_by_name", return_value=None),
            patch("modulo.api.routes.teams.set_rls_org"),
            patch("modulo.api.routes.teams.set_rls_user_context"),
        ):
            resp = licensed_client.post("/api/v1/teams", json={"name": "New Team"})
        assert resp.status_code == 201

    def test_get_team_succeeds_when_enabled(self, licensed_client: TestClient) -> None:
        with (
            patch("modulo.api.routes.teams.get_team", return_value=_make_team()),
            patch("modulo.api.routes.teams.set_rls_org"),
            patch("modulo.api.routes.teams.set_rls_user_context"),
        ):
            resp = licensed_client.get(f"/api/v1/teams/{_TEAM_ID}")
        assert resp.status_code == 200

    def test_add_member_succeeds_when_enabled(self, licensed_client: TestClient) -> None:
        target_account = MagicMock()
        target_account.id = _USER_ID
        target_membership = MagicMock()
        target_membership.role = "admin"
        membership = MagicMock()
        membership.id = uuid.uuid4()
        membership.team_id = _TEAM_ID
        membership.account_id = _USER_ID
        membership.role = "viewer"
        membership.created_at = _NOW
        with (
            patch("modulo.api.routes.teams.add_team_member", new=AsyncMock(return_value=membership)),
            patch("modulo.db.crud.account.get_account_by_id", new=AsyncMock(return_value=target_account)),
            patch(
                "modulo.api.routes.teams.get_membership_by_account_and_org",
                new=AsyncMock(return_value=target_membership),
            ),
            patch("modulo.api.routes.teams.get_team", new=AsyncMock(return_value=_make_team())),
            patch("modulo.api.routes.teams.set_rls_org", new=AsyncMock()),
            patch("modulo.api.routes.teams.set_rls_user_context", new=AsyncMock()),
        ):
            resp = licensed_client.post(
                f"/api/v1/teams/{_TEAM_ID}/members",
                json={"user_id": str(_USER_ID), "role": "viewer"},
            )
        assert resp.status_code == 201


class TestNonTeamEndpoints:
    """Non-team endpoints are unaffected by team_rbac gating."""

    def test_list_api_keys_returns_200_on_free(self, free_client: TestClient) -> None:
        with (
            patch("modulo.api.routes.api_keys.set_rls_org"),
            patch("modulo.api.routes.api_keys.set_rls_user_context"),
            patch("modulo.api.routes.api_keys.list_api_keys", return_value=[]),
        ):
            resp = free_client.get("/api/v1/api-keys")
        assert resp.status_code == 200

    def test_mcp_config_returns_200_on_free(self, free_client: TestClient) -> None:
        resp = free_client.get("/api/v1/api-keys/mcp-config")
        assert resp.status_code == 200

    def test_revoke_api_key_returns_404_on_free(self, free_client: TestClient) -> None:
        with (
            patch("modulo.api.routes.api_keys.set_rls_org"),
            patch("modulo.api.routes.api_keys.set_rls_user_context"),
            patch("modulo.api.routes.api_keys.revoke_api_key", return_value=False),
        ):
            resp = free_client.delete(f"/api/v1/api-keys/{uuid.uuid4()}")
        assert resp.status_code == 404


class TestApiKeyTeamGating:
    """Team-scoped API keys are gated behind team_rbac."""

    def _make_key_mock(self) -> MagicMock:
        k = MagicMock()
        k.id = _KEY_ID
        k.name = "test-key"
        k.role = "operator"
        k.lookup_prefix = "test"
        k.created_at = _NOW
        k.team_id = None
        return k

    def test_create_key_with_team_id_returns_402(self, free_client: TestClient) -> None:
        resp = free_client.post(
            "/api/v1/api-keys",
            json={"name": "team-key", "team_id": str(_TEAM_ID)},
        )
        assert resp.status_code == 402
        assert "team" in resp.json()["detail"].lower()

    def test_create_key_without_team_id_succeeds(self, free_client: TestClient) -> None:
        key = self._make_key_mock()
        with (
            patch("modulo.api.routes.api_keys.create_api_key", return_value=(key, "mk_test")),
            patch("modulo.api.routes.api_keys.set_rls_org"),
            patch("modulo.api.routes.api_keys.set_rls_user_context"),
            patch(
                "modulo.api.routes.api_keys.resolve_role_from_membership",
                new=AsyncMock(return_value="admin"),
            ),
        ):
            resp = free_client.post("/api/v1/api-keys", json={"name": "basic-key"})
        assert resp.status_code == 201

    def test_update_key_with_team_id_returns_402(self, free_client: TestClient) -> None:
        resp = free_client.put(
            f"/api/v1/api-keys/{uuid.uuid4()}",
            json={"team_id": str(_TEAM_ID)},
        )
        assert resp.status_code == 402
        assert "team" in resp.json()["detail"].lower()
