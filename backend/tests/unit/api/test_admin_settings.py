"""Unit tests for /api/v1/admin org, user, team, billing endpoints."""

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
from modulo.db.crud.base import PageResult
from modulo.settings import Settings, get_settings

_VALID_32 = "a" * 32
_ORG_ID = uuid.UUID("00000000-0000-0000-0000-000000000001")
_USER_ID = uuid.UUID("00000000-0000-0000-0000-000000000002")
_OTHER_USER_ID = uuid.UUID("00000000-0000-0000-0000-000000000099")
_TEAM_ID = uuid.UUID("00000000-0000-0000-0000-000000000003")
_NOW = datetime(2025, 6, 1, tzinfo=UTC)


def _make_settings() -> Settings:
    return Settings(
        database_url="postgresql+asyncpg://localhost/test",
        secret_key=_VALID_32,
        fernet_key=_VALID_32,
        modulo_admin_password="testpass",
    )


def _make_mock_session() -> AsyncMock:
    session = AsyncMock()
    begin_cm = AsyncMock()
    begin_cm.__aenter__ = AsyncMock(return_value=None)
    begin_cm.__aexit__ = AsyncMock(return_value=False)
    session.begin = MagicMock(return_value=begin_cm)
    session.execute = AsyncMock(
        return_value=MagicMock(
            scalar=MagicMock(return_value=0),
            all=MagicMock(return_value=[]),
        )
    )
    return session


def _fake_user(
    user_id: uuid.UUID = _USER_ID,
    email: str = "admin@test.com",
    display_name: str = "Admin User",
    org_role: str = "admin",
    active: bool = True,
    auth_provider: str = "local",
    last_login: datetime | None = None,
) -> MagicMock:
    user = MagicMock()
    user.id = user_id
    user.email = email
    user.display_name = display_name
    user.org_role = org_role
    user.active = active
    user.auth_provider = auth_provider
    user.created_at = _NOW
    user.last_login = last_login
    return user


def _fake_team(
    team_id: uuid.UUID = _TEAM_ID,
    name: str = "Engineering",
    description: str | None = "Engineering team",
) -> MagicMock:
    team = MagicMock()
    team.id = team_id
    team.name = name
    team.description = description
    team.created_by = _USER_ID
    team.created_at = _NOW
    return team


@pytest.fixture()
def client() -> Generator[TestClient, None, None]:
    mock_session = _make_mock_session()

    async def override_session() -> AsyncGenerator[AsyncMock, None]:
        yield mock_session

    app.dependency_overrides[get_settings] = _make_settings
    app.dependency_overrides[get_db_session] = override_session
    app.dependency_overrides[_get_engine] = lambda: MagicMock()
    app.dependency_overrides[get_current_user] = lambda: AuthenticatedPrincipal(
        username="admin",
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


# ── Org Profile Tests ────────────────────────────────────────


class TestOrgProfile:
    URL = "/api/v1/admin/org"

    def test_update_org_name_success(self, client: TestClient) -> None:
        fake_org = MagicMock()
        fake_org.id = _ORG_ID
        fake_org.name = "Updated Org"
        fake_org.slug = "test-org"
        fake_org.settings_json = {}
        fake_org.plan_id = None
        fake_org.created_at = _NOW

        with (
            patch(
                "modulo.api.routes.admin.get_organisation",
                AsyncMock(return_value=fake_org),
            ),
            patch(
                "modulo.api.routes.admin.update_organisation",
                AsyncMock(return_value=fake_org),
            ),
        ):
            resp = client.put(self.URL, json={"name": "Updated Org"})

        assert resp.status_code == 200
        body = resp.json()
        assert body["name"] == "Updated Org"
        assert body["slug"] == "test-org"

    def test_update_org_unauthorized(self, unauth_client: TestClient) -> None:
        resp = unauth_client.put(self.URL, json={"name": "Test"})
        assert resp.status_code == 401

    def test_update_org_operator_forbidden(self, operator_client: TestClient) -> None:
        resp = operator_client.put(self.URL, json={"name": "Test"})
        assert resp.status_code == 403

    def test_update_org_not_found(self, client: TestClient) -> None:
        with patch(
            "modulo.api.routes.admin.get_organisation",
            AsyncMock(return_value=None),
        ):
            resp = client.put(self.URL, json={"name": "Test"})

        assert resp.status_code == 404


class TestRegenerateApiKey:
    URL = "/api/v1/admin/org/regenerate-api-key"

    def test_regenerate_success(self, client: TestClient) -> None:
        with patch(
            "modulo.auth.api_key.create_api_key",
            AsyncMock(return_value=(MagicMock(), "mk_testkey1234567890abc")),
        ):
            resp = client.post(self.URL)

        assert resp.status_code == 200
        body = resp.json()
        assert body["api_key"] == "mk_testkey1234567890abc"
        assert body["lookup_prefix"] == "testkey1"

    def test_regenerate_unauthorized(self, unauth_client: TestClient) -> None:
        resp = unauth_client.post(self.URL)
        assert resp.status_code == 401


# ── User Management Tests ────────────────────────────────────


class TestUserList:
    URL = "/api/v1/admin/users"

    def test_list_users_success(self, client: TestClient) -> None:
        fake_users = [_fake_user()]
        fake_result = PageResult(items=fake_users, total=1, page=1, page_size=20)

        with patch(
            "modulo.api.routes.admin.list_users_paginated",
            AsyncMock(return_value=fake_result),
        ):
            resp = client.get(self.URL)

        assert resp.status_code == 200
        body = resp.json()
        assert body["total"] == 1
        assert len(body["items"]) == 1
        assert body["items"][0]["email"] == "admin@test.com"

    def test_list_users_unauthorized(self, unauth_client: TestClient) -> None:
        resp = unauth_client.get(self.URL)
        assert resp.status_code == 401

    def test_list_users_operator_forbidden(self, operator_client: TestClient) -> None:
        resp = operator_client.get(self.URL)
        assert resp.status_code == 403


class TestUserUpdate:
    URL = "/api/v1/admin/users"
    TARGET_ID = "00000000-0000-0000-0000-000000000099"

    def test_update_user_role_success(self, client: TestClient) -> None:
        fake_user = _fake_user(user_id=_OTHER_USER_ID, org_role="operator")

        with patch(
            "modulo.api.routes.admin.crud_update_user",
            AsyncMock(return_value=fake_user),
        ):
            resp = client.put(f"{self.URL}/{self.TARGET_ID}", json={"org_role": "operator"})

        assert resp.status_code == 200
        assert resp.json()["org_role"] == "operator"

    def test_update_user_not_found(self, client: TestClient) -> None:
        with patch(
            "modulo.api.routes.admin.crud_update_user",
            AsyncMock(return_value=None),
        ):
            resp = client.put(
                f"{self.URL}/{self.TARGET_ID}",
                json={"org_role": "operator"},
            )

        assert resp.status_code == 404

    def test_update_user_operator_forbidden(self, operator_client: TestClient) -> None:
        resp = operator_client.put(
            f"{self.URL}/{self.TARGET_ID}",
            json={"org_role": "operator"},
        )
        assert resp.status_code == 403


class TestUserDeactivate:
    URL = "/api/v1/admin/users"

    def test_deactivate_success(self, client: TestClient) -> None:
        fake_user = _fake_user(user_id=_OTHER_USER_ID, active=False)
        target_id = str(_OTHER_USER_ID)

        fake_membership = MagicMock()
        fake_membership.id = uuid.uuid4()

        with (
            patch(
                "modulo.api.routes.admin.crud_update_user",
                AsyncMock(return_value=fake_user),
            ),
            patch(
                "modulo.api.routes.admin.list_families_for_user",
                AsyncMock(return_value=[]),
            ),
            patch(
                "modulo.api.routes.admin.list_memberships_for_user",
                AsyncMock(return_value=[fake_membership]),
            ),
            patch(
                "modulo.api.routes.admin.remove_team_member",
                AsyncMock(return_value=True),
            ),
        ):
            resp = client.post(f"{self.URL}/{target_id}/deactivate")

        assert resp.status_code == 200
        assert resp.json()["is_active"] is False

    def test_deactivate_self_forbidden(self, client: TestClient) -> None:
        resp = client.post(f"{self.URL}/{_USER_ID}/deactivate")
        assert resp.status_code == 422

    def test_deactivate_not_found(self, client: TestClient) -> None:
        target_id = uuid.uuid4()
        with (
            patch(
                "modulo.api.routes.admin.crud_update_user",
                AsyncMock(return_value=None),
            ),
            patch(
                "modulo.api.routes.admin.list_families_for_user",
                AsyncMock(return_value=[]),
            ),
            patch(
                "modulo.api.routes.admin.list_memberships_for_user",
                AsyncMock(return_value=[]),
            ),
        ):
            resp = client.post(f"{self.URL}/{target_id}/deactivate")

        assert resp.status_code == 404


class TestUserReactivate:
    URL = "/api/v1/admin/users"

    def test_reactivate_success(self, client: TestClient) -> None:
        fake_user = _fake_user(user_id=_OTHER_USER_ID, active=True)
        target_id = str(_OTHER_USER_ID)

        with patch(
            "modulo.api.routes.admin.crud_update_user",
            AsyncMock(return_value=fake_user),
        ):
            resp = client.post(f"{self.URL}/{target_id}/reactivate")

        assert resp.status_code == 200
        assert resp.json()["is_active"] is True

    def test_reactivate_operator_forbidden(self, operator_client: TestClient) -> None:
        resp = operator_client.post(f"{self.URL}/{_USER_ID}/reactivate")
        assert resp.status_code == 403


# ── Team Management Tests ────────────────────────────────────


class TestAdminTeamList:
    URL = "/api/v1/admin/teams"

    def test_list_teams_success(self, client: TestClient) -> None:
        fake_team = _fake_team()
        fake_result = PageResult(items=[fake_team], total=1, page=1, page_size=20)

        with (
            patch(
                "modulo.api.routes.admin.list_teams",
                AsyncMock(return_value=fake_result),
            ),
        ):
            resp = client.get(self.URL)

        assert resp.status_code == 200
        body = resp.json()
        assert body["total"] == 1
        assert body["items"][0]["name"] == "Engineering"

    def test_list_teams_unauthorized(self, unauth_client: TestClient) -> None:
        resp = unauth_client.get(self.URL)
        assert resp.status_code == 401


class TestAdminTeamUpdate:
    URL = "/api/v1/admin/teams"

    def test_update_team_success(self, client: TestClient) -> None:
        fake_team = _fake_team(name="Updated Team", description="Updated description")

        with patch(
            "modulo.api.routes.admin.crud_update_team",
            AsyncMock(return_value=fake_team),
        ):
            resp = client.put(
                f"{self.URL}/{_TEAM_ID}",
                json={"name": "Updated Team", "description": "Updated description"},
            )

        assert resp.status_code == 200
        assert resp.json()["name"] == "Updated Team"

    def test_update_team_not_found(self, client: TestClient) -> None:
        with patch(
            "modulo.api.routes.admin.crud_update_team",
            AsyncMock(return_value=None),
        ):
            resp = client.put(
                f"{self.URL}/{_TEAM_ID}",
                json={"name": "Test"},
            )

        assert resp.status_code == 404


class TestAdminTeamDelete:
    URL = "/api/v1/admin/teams"

    def test_delete_team_success(self, client: TestClient) -> None:
        with (
            patch(
                "modulo.api.routes.admin.delete_team",
                AsyncMock(return_value=True),
            ),
        ):
            resp = client.delete(f"{self.URL}/{_TEAM_ID}")

        assert resp.status_code == 204

    def test_delete_team_not_found(self, client: TestClient) -> None:
        with (
            patch(
                "modulo.api.routes.admin.delete_team",
                AsyncMock(return_value=False),
            ),
        ):
            resp = client.delete(f"{self.URL}/{_TEAM_ID}")

        assert resp.status_code == 404

    def test_delete_team_has_pipelines(self, client: TestClient) -> None:
        session_mock = AsyncMock()
        begin_cm = AsyncMock()
        begin_cm.__aenter__ = AsyncMock(return_value=None)
        begin_cm.__aexit__ = AsyncMock(return_value=False)
        session_mock.begin = MagicMock(return_value=begin_cm)
        session_mock.execute = AsyncMock(
            return_value=MagicMock(scalar=MagicMock(return_value=3))
        )

        async def override_session() -> AsyncGenerator[AsyncMock, None]:
            yield session_mock

        app.dependency_overrides[get_db_session] = override_session
        resp = client.delete(f"{self.URL}/{_TEAM_ID}")
        assert resp.status_code == 409
        assert "pipeline" in resp.json()["detail"].lower()
        app.dependency_overrides[get_db_session] = None


# ── Billing Overview Tests ───────────────────────────────────


class TestBillingOverview:
    URL = "/api/v1/admin/billing/overview"

    def test_billing_success(self, client: TestClient) -> None:
        fake_org = MagicMock()
        fake_org.plan_id = "pro_monthly"
        fake_org.daily_spend_limit = 100.0
        fake_org.settings_json = {"license_key": "LIC-1234-ABCD"}

        with patch(
            "modulo.api.routes.admin.get_organisation",
            AsyncMock(return_value=fake_org),
        ):
            resp = client.get(self.URL)

        assert resp.status_code == 200
        body = resp.json()
        assert body["plan_tier"] == "pro"
        assert body["plan_id"] == "pro_monthly"
        assert body["license_key"] == "LIC-1234-ABCD"

    def test_billing_free_plan(self, client: TestClient) -> None:
        fake_org = MagicMock()
        fake_org.plan_id = "free"
        fake_org.daily_spend_limit = None
        fake_org.settings_json = {}

        with patch(
            "modulo.api.routes.admin.get_organisation",
            AsyncMock(return_value=fake_org),
        ):
            resp = client.get(self.URL)

        assert resp.status_code == 200
        assert resp.json()["plan_tier"] == "free"

    def test_billing_unauthorized(self, unauth_client: TestClient) -> None:
        resp = unauth_client.get(self.URL)
        assert resp.status_code == 401

    def test_billing_operator_forbidden(self, operator_client: TestClient) -> None:
        resp = operator_client.get(self.URL)
        assert resp.status_code == 403
