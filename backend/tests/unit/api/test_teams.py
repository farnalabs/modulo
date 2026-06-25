"""Unit tests for /api/v1/teams endpoints."""

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
_TEAM_ID = uuid.UUID("00000000-0000-0000-0000-000000000003")
_MEMBERSHIP_ID = uuid.UUID("00000000-0000-0000-0000-000000000004")
_NOW = datetime(2025, 1, 1, tzinfo=UTC)


def _make_settings() -> Settings:
    return Settings(
        database_url="postgresql+asyncpg://localhost/test",
        secret_key=_VALID_32,
        fernet_key=_VALID_32,
        modulo_admin_password="testpass",
    )


def _make_team(**overrides: object) -> MagicMock:
    t = MagicMock()
    t.id = overrides.get("id", _TEAM_ID)
    t.organisation_id = overrides.get("organisation_id", _ORG_ID)
    t.name = overrides.get("name", "Test Team")
    t.description = overrides.get("description", None)
    t.created_by = overrides.get("created_by", _USER_ID)
    t.notification_endpoints = overrides.get("notification_endpoints", [])
    t.created_at = _NOW
    t.updated_at = _NOW
    return t


def _make_membership(**overrides: object) -> MagicMock:
    m = MagicMock()
    m.id = overrides.get("id", _MEMBERSHIP_ID)
    m.organisation_id = overrides.get("organisation_id", _ORG_ID)
    m.team_id = overrides.get("team_id", _TEAM_ID)
    m.user_id = overrides.get("user_id", _USER_ID)
    m.role = overrides.get("role", "viewer")
    m.created_at = _NOW
    return m


def _make_mock_session() -> AsyncMock:
    session = AsyncMock()
    begin_cm = AsyncMock()
    begin_cm.__aenter__ = AsyncMock(return_value=None)
    begin_cm.__aexit__ = AsyncMock(return_value=False)
    session.begin = MagicMock(return_value=begin_cm)
    return session


_TEAM_BODY = {"name": "New Team"}


@pytest.fixture()
def client() -> Generator[TestClient, None, None]:
    mock_session = _make_mock_session()

    async def override_session() -> AsyncGenerator[AsyncMock, None]:
        yield mock_session

    app.dependency_overrides[get_settings] = _make_settings
    app.dependency_overrides[get_db_session] = override_session
    app.dependency_overrides[_get_engine] = lambda: MagicMock()
    app.dependency_overrides[get_current_user] = lambda: AuthenticatedPrincipal(
        username="testuser",
        organisation_id=_ORG_ID,
        user_id=_USER_ID,
        org_role="admin",
    )
    yield TestClient(app)
    app.dependency_overrides.clear()


@pytest.fixture()
def unauth_client() -> Generator[TestClient, None, None]:
    app.dependency_overrides[get_settings] = _make_settings
    yield TestClient(app)
    app.dependency_overrides.clear()


@pytest.fixture()
def operator_client() -> Generator[TestClient, None, None]:
    mock_session = _make_mock_session()

    async def override_session() -> AsyncGenerator[AsyncMock, None]:
        yield mock_session

    app.dependency_overrides[get_settings] = _make_settings
    app.dependency_overrides[get_db_session] = override_session
    app.dependency_overrides[_get_engine] = lambda: MagicMock()
    app.dependency_overrides[get_current_user] = lambda: AuthenticatedPrincipal(
        username="operator",
        organisation_id=_ORG_ID,
        user_id=_USER_ID,
        org_role="operator",
    )
    yield TestClient(app)
    app.dependency_overrides.clear()


class TestListTeams:
    def test_returns_200(self, client: TestClient) -> None:
        page_result = MagicMock(
            items=[_make_team()], total=1, page=1, page_size=20
        )
        with (
            patch("modulo.api.routes.teams.list_teams", return_value=page_result),
            patch("modulo.api.routes.teams.set_rls_org"),
            patch("modulo.api.routes.teams.set_rls_user_context"),
        ):
            resp = client.get("/api/v1/teams")
        assert resp.status_code == 200
        data = resp.json()
        assert data["total"] == 1
        assert len(data["items"]) == 1

    def test_returns_empty_when_no_teams(self, client: TestClient) -> None:
        page_result = MagicMock(items=[], total=0, page=1, page_size=20)
        with (
            patch("modulo.api.routes.teams.list_teams", return_value=page_result),
            patch("modulo.api.routes.teams.set_rls_org"),
            patch("modulo.api.routes.teams.set_rls_user_context"),
        ):
            resp = client.get("/api/v1/teams")
        assert resp.status_code == 200
        assert resp.json()["total"] == 0

    def test_unauthorized_returns_4xx(self, unauth_client: TestClient) -> None:
        resp = unauth_client.get("/api/v1/teams")
        assert resp.status_code in (401, 403)


class TestCreateTeam:
    def test_returns_201(self, client: TestClient) -> None:
        with (
            patch(
                "modulo.api.routes.teams.create_team",
                return_value=_make_team(name="New Team"),
            ),
            patch(
                "modulo.api.routes.teams.get_team_by_name",
                return_value=None,
            ),
            patch("modulo.api.routes.teams.set_rls_org"),
            patch("modulo.api.routes.teams.set_rls_user_context"),
        ):
            resp = client.post("/api/v1/teams", json=_TEAM_BODY)
        assert resp.status_code == 201
        assert resp.json()["name"] == "New Team"

    def test_operator_returns_403(self, operator_client: TestClient) -> None:
        resp = operator_client.post("/api/v1/teams", json=_TEAM_BODY)
        assert resp.status_code == 403

    def test_empty_name_returns_422(self, client: TestClient) -> None:
        resp = client.post("/api/v1/teams", json={"name": ""})
        assert resp.status_code == 422

    def test_missing_name_returns_422(self, client: TestClient) -> None:
        resp = client.post("/api/v1/teams", json={})
        assert resp.status_code == 422


class TestGetTeam:
    def test_returns_200(self, client: TestClient) -> None:
        with (
            patch(
                "modulo.api.routes.teams.get_team",
                return_value=_make_team(),
            ),
            patch("modulo.api.routes.teams.set_rls_org"),
            patch("modulo.api.routes.teams.set_rls_user_context"),
        ):
            resp = client.get(f"/api/v1/teams/{_TEAM_ID}")
        assert resp.status_code == 200
        assert resp.json()["id"] == str(_TEAM_ID)

    def test_not_found_returns_404(self, client: TestClient) -> None:
        with (
            patch("modulo.api.routes.teams.get_team", return_value=None),
            patch("modulo.api.routes.teams.set_rls_org"),
            patch("modulo.api.routes.teams.set_rls_user_context"),
        ):
            resp = client.get(f"/api/v1/teams/{uuid.uuid4()}")
        assert resp.status_code == 404


class TestUpdateTeam:
    def test_returns_200(self, client: TestClient) -> None:
        team = _make_team(name="Updated")
        with (
            patch("modulo.api.routes.teams.update_team", return_value=team),
            patch("modulo.api.routes.teams.set_rls_org"),
            patch("modulo.api.routes.teams.set_rls_user_context"),
        ):
            resp = client.patch(
                f"/api/v1/teams/{_TEAM_ID}", json={"name": "Updated"}
            )
        assert resp.status_code == 200
        assert resp.json()["name"] == "Updated"

    def test_not_found_returns_404(self, client: TestClient) -> None:
        with (
            patch("modulo.api.routes.teams.update_team", return_value=None),
            patch("modulo.api.routes.teams.set_rls_org"),
            patch("modulo.api.routes.teams.set_rls_user_context"),
        ):
            resp = client.patch(
                f"/api/v1/teams/{uuid.uuid4()}", json={"name": "x"}
            )
        assert resp.status_code == 404

    def test_empty_name_returns_422(self, client: TestClient) -> None:
        resp = client.patch(f"/api/v1/teams/{_TEAM_ID}", json={"name": ""})
        assert resp.status_code == 422


class TestDeleteTeam:
    def test_returns_204(self, client: TestClient) -> None:
        with (
            patch("modulo.api.routes.teams.delete_team", return_value=True),
            patch("modulo.api.routes.teams.set_rls_org"),
            patch("modulo.api.routes.teams.set_rls_user_context"),
        ):
            resp = client.delete(f"/api/v1/teams/{_TEAM_ID}")
        assert resp.status_code == 204

    def test_not_found_returns_404(self, client: TestClient) -> None:
        with (
            patch("modulo.api.routes.teams.delete_team", return_value=False),
            patch("modulo.api.routes.teams.set_rls_org"),
            patch("modulo.api.routes.teams.set_rls_user_context"),
        ):
            resp = client.delete(f"/api/v1/teams/{uuid.uuid4()}")
        assert resp.status_code == 404


class TestAddMember:
    def test_returns_201(self, client: TestClient) -> None:
        target_user = MagicMock()
        target_user.id = _USER_ID
        target_user.org_role = "admin"
        with (
            patch(
                "modulo.api.routes.teams.add_team_member",
                return_value=_make_membership(),
            ),
            patch("modulo.api.routes.teams.get_user_by_id_org", new_callable=AsyncMock) as m_get_user,
            patch("modulo.api.routes.teams.get_team", return_value=_make_team()),
            patch("modulo.api.routes.teams.set_rls_org"),
            patch("modulo.api.routes.teams.set_rls_user_context"),
        ):
            m_get_user.return_value = target_user
            resp = client.post(
                f"/api/v1/teams/{_TEAM_ID}/members",
                json={"user_id": str(_USER_ID), "role": "viewer"},
            )
        assert resp.status_code == 201

    def test_operator_returns_403(self, operator_client: TestClient) -> None:
        resp = operator_client.post(
            f"/api/v1/teams/{_TEAM_ID}/members",
            json={"user_id": str(_USER_ID), "role": "viewer"},
        )
        assert resp.status_code == 403

    def test_invalid_user_id_returns_422(self, client: TestClient) -> None:
        resp = client.post(
            f"/api/v1/teams/{_TEAM_ID}/members",
            json={"user_id": "not-a-uuid", "role": "viewer"},
        )
        assert resp.status_code == 422

    def test_invalid_role_pattern_returns_422(self, client: TestClient) -> None:
        resp = client.post(
            f"/api/v1/teams/{_TEAM_ID}/members",
            json={"user_id": str(_USER_ID), "role": "superadmin"},
        )
        assert resp.status_code == 422

    def test_role_exceeds_org_role_returns_422(self, client: TestClient) -> None:
        target_user = MagicMock()
        target_user.id = _USER_ID
        target_user.org_role = "viewer"
        with (
            patch("modulo.api.routes.teams.get_user_by_id_org", new_callable=AsyncMock) as m_get_user,
            patch("modulo.api.routes.teams.add_team_member"),
            patch("modulo.api.routes.teams.get_team", return_value=_make_team()),
            patch("modulo.api.routes.teams.set_rls_org"),
            patch("modulo.api.routes.teams.set_rls_user_context"),
        ):
            m_get_user.return_value = target_user
            resp = client.post(
                f"/api/v1/teams/{_TEAM_ID}/members",
                json={"user_id": str(_USER_ID), "role": "admin"},
            )
        assert resp.status_code == 422
        data = resp.json()
        assert "exceeds" in data["detail"].lower()

    def test_role_within_org_role_succeeds(self, client: TestClient) -> None:
        target_user = MagicMock()
        target_user.id = _USER_ID
        target_user.org_role = "admin"
        with (
            patch("modulo.api.routes.teams.get_user_by_id_org", new_callable=AsyncMock) as m_get_user,
            patch(
                "modulo.api.routes.teams.add_team_member",
                return_value=_make_membership(role="operator"),
            ),
            patch("modulo.api.routes.teams.get_team", return_value=_make_team()),
            patch("modulo.api.routes.teams.set_rls_org"),
            patch("modulo.api.routes.teams.set_rls_user_context"),
        ):
            m_get_user.return_value = target_user
            resp = client.post(
                f"/api/v1/teams/{_TEAM_ID}/members",
                json={"user_id": str(_USER_ID), "role": "operator"},
            )
        assert resp.status_code == 201

    def test_user_not_found_returns_404(self, client: TestClient) -> None:
        with (
            patch("modulo.api.routes.teams.get_user_by_id_org", new_callable=AsyncMock) as m_get_user,
            patch("modulo.api.routes.teams.set_rls_org"),
            patch("modulo.api.routes.teams.set_rls_user_context"),
        ):
            m_get_user.return_value = None
            resp = client.post(
                f"/api/v1/teams/{_TEAM_ID}/members",
                json={"user_id": str(uuid.uuid4()), "role": "viewer"},
            )
        assert resp.status_code == 404


class TestListMembers:
    def test_returns_200(self, client: TestClient) -> None:
        page_result = MagicMock(
            items=[_make_membership()], total=1, page=1, page_size=20
        )
        with (
            patch(
                "modulo.api.routes.teams.list_team_members",
                return_value=page_result,
            ),
            patch("modulo.api.routes.teams.set_rls_org"),
            patch("modulo.api.routes.teams.set_rls_user_context"),
        ):
            resp = client.get(f"/api/v1/teams/{_TEAM_ID}/members")
        assert resp.status_code == 200
        data = resp.json()
        assert data["total"] == 1
        assert len(data["items"]) == 1

    def test_empty_members(self, client: TestClient) -> None:
        page_result = MagicMock(items=[], total=0, page=1, page_size=20)
        with (
            patch(
                "modulo.api.routes.teams.list_team_members",
                return_value=page_result,
            ),
            patch("modulo.api.routes.teams.set_rls_org"),
            patch("modulo.api.routes.teams.set_rls_user_context"),
        ):
            resp = client.get(f"/api/v1/teams/{_TEAM_ID}/members")
        assert resp.status_code == 200
        assert resp.json()["total"] == 0


class TestRemoveMember:
    def test_returns_204(self, client: TestClient) -> None:
        with (
            patch(
                "modulo.api.routes.teams.get_membership",
                return_value=_make_membership(),
            ),
            patch(
                "modulo.api.routes.teams.remove_team_member", return_value=True
            ),
            patch("modulo.api.routes.teams.set_rls_org"),
            patch("modulo.api.routes.teams.set_rls_user_context"),
        ):
            resp = client.delete(
                f"/api/v1/teams/{_TEAM_ID}/members/{_MEMBERSHIP_ID}"
            )
        assert resp.status_code == 204

    def test_not_found_returns_404(self, client: TestClient) -> None:
        with (
            patch(
                "modulo.api.routes.teams.get_membership",
                return_value=None,
            ),
            patch(
                "modulo.api.routes.teams.remove_team_member", return_value=False
            ),
            patch("modulo.api.routes.teams.set_rls_org"),
            patch("modulo.api.routes.teams.set_rls_user_context"),
        ):
            resp = client.delete(
                f"/api/v1/teams/{_TEAM_ID}/members/{uuid.uuid4()}"
            )
        assert resp.status_code == 404


class TestAdminCreateTeam:
    def test_admin_creates_team_returns_201(self, client: TestClient) -> None:
        with (
            patch(
                "modulo.api.routes.admin.create_team",
                return_value=_make_team(name="Admin Team"),
            ),
            patch("modulo.api.routes.admin.set_rls_org"),
        ):
            resp = client.post(
                "/api/v1/admin/teams",
                json={"name": "Admin Team"},
            )
        assert resp.status_code == 201
        assert resp.json()["name"] == "Admin Team"

    def test_operator_returns_403(self, operator_client: TestClient) -> None:
        resp = operator_client.post(
            "/api/v1/admin/teams",
            json={"name": "Admin Team"},
        )
        assert resp.status_code == 403

    def test_empty_name_returns_422(self, client: TestClient) -> None:
        resp = client.post("/api/v1/admin/teams", json={"name": ""})
        assert resp.status_code == 422
