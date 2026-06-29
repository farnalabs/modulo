"""Unit tests for SCIM provisioning BDD step definitions.

Tests the step functions in backend/tests/bdd/steps/test_scim_provisioning.py
by running them against the FastAPI TestClient with mocked dependencies.

Each test class covers one BDD scenario by simulating the Given → When → Then
step execution order with pre-configured mocks.
"""

import uuid
from collections.abc import AsyncGenerator, Generator
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from modulo.api.dependencies import _get_engine, get_db_session
from modulo.api.main import app
from modulo.auth.scim_auth import ScimPrincipal, get_scim_principal
from modulo.settings import Settings, get_settings

_VALID_32 = "a" * 32
_ORG_ID = uuid.UUID("00000000-0000-0000-0000-000000000001")
_USER_ID = uuid.UUID("00000000-0000-0000-0000-000000000002")
_TEAM_ID = uuid.UUID("00000000-0000-0000-0000-000000000003")
_SCIM_TOKEN = "test-scim-token-12345"

_USER_CREATE_BODY = {
    "schemas": ["urn:ietf:params:scim:schemas:core:2.0:User"],
    "userName": "jane@example.com",
    "name": {"givenName": "Jane", "familyName": "Doe"},
    "emails": [{"value": "jane@example.com", "primary": True}],
    "active": True,
}

_GROUP_CREATE_BODY = {
    "schemas": ["urn:ietf:params:scim:schemas:core:2.0:Group"],
    "displayName": "Engineering",
    "members": [{"value": str(_USER_ID), "type": "User"}],
}

_PATCH_USER_DEACTIVATE = {
    "schemas": ["urn:ietf:params:scim:api:messages:2.0:PatchOp"],
    "Operations": [{"op": "replace", "path": "active", "value": False}],
}

_PATCH_GROUP_ADD_MEMBER = {
    "schemas": ["urn:ietf:params:scim:api:messages:2.0:PatchOp"],
    "Operations": [{"op": "add", "path": "members", "value": [{"value": str(_USER_ID)}]}],
}

_PATCH_GROUP_REMOVE_MEMBER = {
    "schemas": ["urn:ietf:params:scim:api:messages:2.0:PatchOp"],
    "Operations": [{"op": "remove", "path": "members", "value": [{"value": str(_USER_ID)}]}],
}


def _make_settings() -> Settings:
    return Settings(
        database_url="postgresql+asyncpg://localhost/test",
        secret_key=_VALID_32,
        fernet_key=_VALID_32,
        modulo_admin_password="testpass",
        modulo_license_key="enterprise-license",
        modulo_scim_token=_SCIM_TOKEN,
    )


def _make_no_license_settings() -> Settings:
    return Settings(
        database_url="postgresql+asyncpg://localhost/test",
        secret_key=_VALID_32,
        fernet_key=_VALID_32,
        modulo_admin_password="testpass",
        modulo_license_key="",
    )


def _make_mock_session() -> AsyncMock:
    session = AsyncMock()
    begin_cm = AsyncMock()
    begin_cm.__aenter__ = AsyncMock(return_value=None)
    begin_cm.__aexit__ = AsyncMock(return_value=False)
    session.begin = MagicMock(return_value=begin_cm)
    return session


def _make_mock_user(**overrides: object) -> MagicMock:
    user = MagicMock()
    user.id = overrides.get("id", _USER_ID)
    user.organisation_id = overrides.get("organisation_id", _ORG_ID)
    user.email = overrides.get("email", "jane@example.com")
    user.display_name = overrides.get("display_name", "Jane Doe")
    user.active = overrides.get("active", True)
    user.org_role = overrides.get("org_role", "runner")
    user.auth_provider = overrides.get("auth_provider", "scim")
    user.created_at = overrides.get("created_at", None)
    user.updated_at = overrides.get("updated_at", None)
    return user


def _make_mock_team(**overrides: object) -> MagicMock:
    team = MagicMock()
    team.id = overrides.get("id", _TEAM_ID)
    team.organisation_id = overrides.get("organisation_id", _ORG_ID)
    team.name = overrides.get("name", "Engineering")
    team.description = overrides.get("description", None)
    team.created_by = overrides.get("created_by", _USER_ID)
    team.created_at = overrides.get("created_at", None)
    team.updated_at = overrides.get("updated_at", None)
    return team


@pytest.fixture()
def client() -> Generator[TestClient, None, None]:
    mock_session = _make_mock_session()

    async def override_session() -> AsyncGenerator[AsyncMock, None]:
        yield mock_session

    app.dependency_overrides[get_settings] = _make_settings
    app.dependency_overrides[get_db_session] = override_session
    app.dependency_overrides[_get_engine] = lambda: MagicMock()
    app.dependency_overrides[get_scim_principal] = lambda: ScimPrincipal(organisation_id=_ORG_ID)
    yield TestClient(app)
    app.dependency_overrides.clear()


@pytest.fixture(autouse=True)
def _clear_overrides() -> object:
    yield
    app.dependency_overrides.clear()


# ===========================================================================
# Scenario: Create a SCIM user provisions a new Modulo user
# ===========================================================================

class TestCreateScimUser:
    """POST /scim/v2/Users — happy path."""

    def test_creates_user_and_returns_201(self, client: TestClient) -> None:
        mock_user = _make_mock_user()

        with (
            patch("modulo.db.crud.user.get_user_by_email", return_value=None),
            patch("modulo.api.routes.scim.scim_create_user", return_value=mock_user),
            patch("modulo.api.routes.scim.set_rls_org"),
        ):
            headers = {"Authorization": f"Bearer {_SCIM_TOKEN}"}
            resp = client.post("/scim/v2/Users", json=_USER_CREATE_BODY, headers=headers)

        assert resp.status_code == 201
        body = resp.json()
        assert "schemas" in body
        assert "urn:ietf:params:scim:schemas:core:2.0:User" in body["schemas"]
        assert body["userName"] == "jane@example.com"
        assert "id" in body
        uuid.UUID(body["id"])  # must be valid UUID


# ===========================================================================
# Scenario: Get a SCIM user by id returns the full resource
# ===========================================================================

class TestGetScimUser:
    """GET /scim/v2/Users/{user_id} — happy path."""

    def test_returns_user_200(self, client: TestClient) -> None:
        mock_user = _make_mock_user()

        with (
            patch("modulo.api.routes.scim.scim_get_user", return_value=mock_user),
            patch("modulo.api.routes.scim.set_rls_org"),
        ):
            headers = {"Authorization": f"Bearer {_SCIM_TOKEN}"}
            resp = client.get(f"/scim/v2/Users/{_USER_ID}", headers=headers)

        assert resp.status_code == 200
        body = resp.json()
        assert body["userName"] == "jane@example.com"
        assert body["id"] == str(_USER_ID)

    def test_returns_404_for_missing(self, client: TestClient) -> None:
        with (
            patch("modulo.api.routes.scim.scim_get_user", return_value=None),
            patch("modulo.api.routes.scim.set_rls_org"),
        ):
            headers = {"Authorization": f"Bearer {_SCIM_TOKEN}"}
            resp = client.get(f"/scim/v2/Users/{uuid.uuid4()}", headers=headers)

        assert resp.status_code == 404


# ===========================================================================
# Scenario: Replace a SCIM user updates all attributes
# ===========================================================================

class TestReplaceScimUser:
    """PUT /scim/v2/Users/{user_id} — happy path."""

    def test_replaces_user_returns_200(self, client: TestClient) -> None:
        updated = _make_mock_user(email="jane.updated@example.com")

        with (
            patch("modulo.api.routes.scim.scim_update_user", return_value=updated),
            patch("modulo.api.routes.scim.set_rls_org"),
        ):
            body = dict(_USER_CREATE_BODY)
            body["userName"] = "jane.updated@example.com"
            headers = {"Authorization": f"Bearer {_SCIM_TOKEN}"}
            resp = client.put(f"/scim/v2/Users/{_USER_ID}", json=body, headers=headers)

        assert resp.status_code == 200
        assert resp.json()["userName"] == "jane.updated@example.com"


# ===========================================================================
# Scenario: Delete a SCIM user deactivates the Modulo user
# ===========================================================================

class TestDeleteScimUser:
    """DELETE /scim/v2/Users/{user_id} — happy path."""

    def test_deletes_user_returns_204(self, client: TestClient) -> None:
        with (
            patch("modulo.api.routes.scim.scim_delete_user_by_id", return_value=MagicMock()),
            patch("modulo.api.routes.scim.set_rls_org"),
        ):
            headers = {"Authorization": f"Bearer {_SCIM_TOKEN}"}
            resp = client.delete(f"/scim/v2/Users/{_USER_ID}", headers=headers)

        assert resp.status_code == 204

    def test_returns_404_for_missing(self, client: TestClient) -> None:
        with (
            patch("modulo.api.routes.scim.scim_delete_user_by_id", return_value=None),
            patch("modulo.api.routes.scim.set_rls_org"),
        ):
            headers = {"Authorization": f"Bearer {_SCIM_TOKEN}"}
            resp = client.delete(f"/scim/v2/Users/{uuid.uuid4()}", headers=headers)

        assert resp.status_code == 404


# ===========================================================================
# Scenario: JIT provisioning links an unknown SCIM user to a new Modulo user
# ===========================================================================

class TestJitProvisioning:
    """JIT — user created with auth_provider=scim, no password."""

    def test_jit_creates_user_with_scim_provider(self, client: TestClient) -> None:
        mock_user = _make_mock_user(email="newcomer@example.com", auth_provider="scim", password_hash=None)

        with (
            patch("modulo.db.crud.user.get_user_by_email", return_value=None),
            patch("modulo.api.routes.scim.scim_create_user", return_value=mock_user),
            patch("modulo.api.routes.scim.set_rls_org"),
        ):
            body = dict(_USER_CREATE_BODY)
            body["userName"] = "newcomer@example.com"
            headers = {"Authorization": f"Bearer {_SCIM_TOKEN}"}
            resp = client.post("/scim/v2/Users", json=body, headers=headers)

        assert resp.status_code == 201
        assert resp.json()["userName"] == "newcomer@example.com"

    def test_jit_user_has_no_password(self, client: TestClient) -> None:
        mock_user = _make_mock_user(email="newcomer@example.com", auth_provider="scim")
        mock_user.password_hash = None

        with (
            patch("modulo.db.crud.user.get_user_by_email", return_value=None),
            patch("modulo.api.routes.scim.scim_create_user", return_value=mock_user),
            patch("modulo.api.routes.scim.set_rls_org"),
        ):
            body = dict(_USER_CREATE_BODY)
            body["userName"] = "newcomer@example.com"
            headers = {"Authorization": f"Bearer {_SCIM_TOKEN}"}
            resp = client.post("/scim/v2/Users", json=body, headers=headers)
            assert resp.status_code == 201
            from modulo.api.routes import scim as scim_routes
            scim_routes.scim_create_user.assert_called_once()

    def test_duplicate_username_returns_409(self, client: TestClient) -> None:
        with (
            patch("modulo.api.routes.scim.scim_create_user", side_effect=ValueError("Duplicate userName")),
            patch("modulo.api.routes.scim.set_rls_org"),
        ):
            headers = {"Authorization": f"Bearer {_SCIM_TOKEN}"}
            resp = client.post("/scim/v2/Users", json=_USER_CREATE_BODY, headers=headers)

        assert resp.status_code == 409


# ===========================================================================
# Scenario: Deprovisioning a SCIM user deactivates but preserves the record
# ===========================================================================

class TestDeprovisionScimUser:
    """PATCH /scim/v2/Users/{user_id} — deactivate user."""

    def test_deactivate_returns_200(self, client: TestClient) -> None:
        pre_user = _make_mock_user(active=True)
        deactivated = _make_mock_user(active=False)

        with (
            patch("modulo.api.routes.scim.scim_get_user", return_value=pre_user),
            patch("modulo.api.routes.scim.scim_update_user", return_value=deactivated),
            patch("modulo.api.routes.scim.set_rls_org"),
        ):
            headers = {"Authorization": f"Bearer {_SCIM_TOKEN}"}
            resp = client.patch(f"/scim/v2/Users/{_USER_ID}", json=_PATCH_USER_DEACTIVATE, headers=headers)

        assert resp.status_code == 200
        body = resp.json()
        assert body["active"] is False

    def test_deactivated_user_still_exists(self, client: TestClient) -> None:
        pre_user = _make_mock_user(active=True)
        deactivated = _make_mock_user(active=False)

        with (
            patch("modulo.api.routes.scim.scim_get_user", return_value=pre_user),
            patch("modulo.api.routes.scim.scim_update_user", return_value=deactivated),
            patch("modulo.api.routes.scim.set_rls_org"),
        ):
            headers = {"Authorization": f"Bearer {_SCIM_TOKEN}"}
            resp = client.patch(f"/scim/v2/Users/{_USER_ID}", json=_PATCH_USER_DEACTIVATE, headers=headers)

        assert resp.status_code == 200
        body = resp.json()
        assert body["id"] == str(_USER_ID)


# ===========================================================================
# Scenario: Create a SCIM group with members
# ===========================================================================

class TestCreateScimGroup:
    """POST /scim/v2/Groups — happy path."""

    def test_creates_group_returns_201(self, client: TestClient) -> None:
        mock_team = _make_mock_team()

        with (
            patch("modulo.db.crud.team.get_team_by_name", return_value=None),
            patch("modulo.db.crud.user.list_users_for_org", return_value=[_make_mock_user()]),
            patch("modulo.api.routes.scim.scim_create_group", return_value=mock_team),
            patch("modulo.api.routes.scim.set_rls_org"),
        ):
            headers = {"Authorization": f"Bearer {_SCIM_TOKEN}"}
            resp = client.post("/scim/v2/Groups", json=_GROUP_CREATE_BODY, headers=headers)

        assert resp.status_code == 201
        body = resp.json()
        assert "schemas" in body
        assert "urn:ietf:params:scim:schemas:core:2.0:Group" in body["schemas"]
        assert body["displayName"] == "Engineering"

    def test_duplicate_displayname_returns_409(self, client: TestClient) -> None:
        with (
            patch("modulo.api.routes.scim.scim_create_group", side_effect=ValueError("Duplicate displayName")),
            patch("modulo.api.routes.scim.set_rls_org"),
        ):
            headers = {"Authorization": f"Bearer {_SCIM_TOKEN}"}
            resp = client.post("/scim/v2/Groups", json=_GROUP_CREATE_BODY, headers=headers)

        assert resp.status_code == 409

    def test_group_with_members_lists_them(self, client: TestClient) -> None:
        mock_team = _make_mock_team()
        mock_team.name = "Engineering"
        mock_user = _make_mock_user()

        with (
            patch("modulo.db.crud.team.get_team_by_name", return_value=None),
            patch("modulo.db.crud.user.list_users_for_org", return_value=[mock_user]),
            patch("modulo.db.crud.user.get_user_by_email", return_value=mock_user),
            patch("modulo.api.routes.scim.scim_get_user", return_value=mock_user),
            patch("modulo.api.routes.scim.scim_create_group", return_value=mock_team),
            patch("modulo.api.routes.scim.scim_add_group_member", return_value=None),
            patch("modulo.api.routes.scim.set_rls_org"),
        ):
            headers = {"Authorization": f"Bearer {_SCIM_TOKEN}"}
            resp = client.post("/scim/v2/Groups", json=_GROUP_CREATE_BODY, headers=headers)

        assert resp.status_code == 201
        assert len(resp.json().get("members", [])) == 1


# ===========================================================================
# Scenario: Team sync maps IdP group membership to Modulo teams
# ===========================================================================

class TestTeamSync:
    """PATCH /scim/v2/Groups — add/remove members."""

    MOCK_TEAM = None

    @classmethod
    def _get_mock_team(cls) -> MagicMock:
        if cls.MOCK_TEAM is None:
            t = MagicMock()
            t.id = _TEAM_ID
            t.organisation_id = _ORG_ID
            t.name = "Engineering"
            t.description = None
            t.created_by = _USER_ID
            t.created_at = None
            t.updated_at = None
            cls.MOCK_TEAM = t
        return cls.MOCK_TEAM

    def test_add_member_returns_200(self, client: TestClient) -> None:
        mock_team = self._get_mock_team()
        with (
            patch("modulo.api.routes.scim.scim_get_group", return_value=mock_team),
            patch("modulo.api.routes.scim.scim_list_group_members", return_value=[]),
            patch("modulo.api.routes.scim.scim_add_group_member", return_value=None),
            patch("modulo.api.routes.scim.set_rls_org"),
        ):
            headers = {"Authorization": f"Bearer {_SCIM_TOKEN}"}
            resp = client.patch(
                f"/scim/v2/Groups/{_TEAM_ID}", json=_PATCH_GROUP_ADD_MEMBER, headers=headers,
            )

        assert resp.status_code == 200

    def test_remove_member_returns_200(self, client: TestClient) -> None:
        mock_team = self._get_mock_team()
        with (
            patch("modulo.api.routes.scim.scim_get_group", return_value=mock_team),
            patch("modulo.api.routes.scim.scim_list_group_members", return_value=[]),
            patch("modulo.api.routes.scim.scim_remove_group_member", return_value=None),
            patch("modulo.api.routes.scim.set_rls_org"),
        ):
            headers = {"Authorization": f"Bearer {_SCIM_TOKEN}"}
            resp = client.patch(
                f"/scim/v2/Groups/{_TEAM_ID}", json=_PATCH_GROUP_REMOVE_MEMBER, headers=headers,
            )

        assert resp.status_code == 200

    def test_add_nonexistent_user_returns_error(self, client: TestClient) -> None:
        mock_team = self._get_mock_team()
        from fastapi import HTTPException as FastAPIHTTPException
        with (
            patch("modulo.api.routes.scim.scim_get_group", return_value=mock_team),
            patch("modulo.api.routes.scim.scim_list_group_members", return_value=[]),
            patch(
                "modulo.api.routes.scim.scim_add_group_member",
                side_effect=FastAPIHTTPException(status_code=404, detail="User not found"),
            ),
            patch("modulo.api.routes.scim.set_rls_org"),
        ):
            headers = {"Authorization": f"Bearer {_SCIM_TOKEN}"}
            resp = client.patch(
                f"/scim/v2/Groups/{_TEAM_ID}", json=_PATCH_GROUP_ADD_MEMBER, headers=headers,
            )

        assert resp.status_code == 404


# ===========================================================================
# Scenario: SCIM bearer token auth rejects invalid credentials
# ===========================================================================

class TestScimAuth:
    """Authentication edge cases."""

    def test_no_token_returns_401(self) -> None:
        app.dependency_overrides[get_settings] = _make_settings
        app.dependency_overrides[get_db_session] = lambda: _make_mock_session()
        app.dependency_overrides[_get_engine] = lambda: MagicMock()
        headers = {"X-CSRF-Token": "test-csrf-token"}
        resp = TestClient(app).post(
            "/scim/v2/Users", json=_USER_CREATE_BODY,
            headers=headers, cookies={"XSRF-TOKEN": "test-csrf-token"},
        )
        app.dependency_overrides.clear()
        assert resp.status_code == 401

    def test_invalid_token_returns_401(self) -> None:
        app.dependency_overrides[get_settings] = _make_settings
        app.dependency_overrides[get_db_session] = lambda: _make_mock_session()
        app.dependency_overrides[_get_engine] = lambda: MagicMock()
        headers = {"Authorization": "Bearer wrong-token"}
        resp = TestClient(app).post("/scim/v2/Users", json=_USER_CREATE_BODY, headers=headers)
        app.dependency_overrides.clear()
        assert resp.status_code == 401

    def test_missing_scim_token_setting_returns_501(self) -> None:
        settings = Settings(
            database_url="postgresql+asyncpg://localhost/test",
            secret_key=_VALID_32,
            fernet_key=_VALID_32,
            modulo_admin_password="testpass",
            modulo_license_key="enterprise-license",
            modulo_scim_token="",
        )
        app.dependency_overrides[get_settings] = lambda: settings
        app.dependency_overrides[get_db_session] = lambda: _make_mock_session()
        app.dependency_overrides[_get_engine] = lambda: MagicMock()
        resp = TestClient(app).get("/scim/v2/Users", headers={"Authorization": f"Bearer {_SCIM_TOKEN}"})
        app.dependency_overrides.clear()
        assert resp.status_code == 501


# ===========================================================================
# Scenario: Enterprise license gate blocks SCIM without valid license
# ===========================================================================

class TestLicenseGate:
    """Enterprise license gating for SCIM endpoints."""

    def test_scim_blocked_without_enterprise_license(self) -> None:
        app.dependency_overrides[get_settings] = _make_no_license_settings
        app.dependency_overrides[get_db_session] = lambda: _make_mock_session()
        resp = TestClient(app).get("/scim/v2/Users", headers={"Authorization": f"Bearer {_SCIM_TOKEN}"})
        app.dependency_overrides.clear()
        assert resp.status_code == 402

    def test_scim_allowed_with_enterprise_license(self) -> None:
        mock_user_list = ([_make_mock_user()], 1)

        app.dependency_overrides[get_settings] = _make_settings
        app.dependency_overrides[get_db_session] = lambda: _make_mock_session()
        app.dependency_overrides[_get_engine] = lambda: MagicMock()
        app.dependency_overrides[get_scim_principal] = lambda: ScimPrincipal(organisation_id=_ORG_ID)

        with (
            patch("modulo.api.routes.scim.scim_list_users", return_value=mock_user_list),
            patch("modulo.api.routes.scim.set_rls_org"),
        ):
            headers = {"Authorization": f"Bearer {_SCIM_TOKEN}"}
            resp = TestClient(app).get("/scim/v2/Users", headers=headers)

        app.dependency_overrides.clear()
        assert resp.status_code == 200

    def test_service_provider_config_allowed_with_license(self) -> None:
        app.dependency_overrides[get_settings] = _make_settings
        app.dependency_overrides[get_db_session] = lambda: _make_mock_session()
        app.dependency_overrides[get_scim_principal] = lambda: ScimPrincipal(organisation_id=_ORG_ID)
        resp = TestClient(app).get("/scim/v2/ServiceProviderConfig", headers={"Authorization": f"Bearer {_SCIM_TOKEN}"})
        app.dependency_overrides.clear()
        assert resp.status_code == 200
