"""Resilience & integration robustness tests for SCIM 2.0 provisioning.

Covers SQLAlchemyError→503 handling, _get_base_url 500 on unset URL,
and CRUD-level error paths not covered by existing unit tests.
"""

import uuid
from collections.abc import AsyncGenerator, Generator
from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.exc import SQLAlchemyError

from modulo.api.dependencies import _get_engine, get_db_session
from modulo.api.main import app
from modulo.auth.scim_auth import ScimPrincipal, get_scim_principal
from modulo.settings import Settings, get_settings

_VALID_32 = "a" * 32
_ORG_ID = uuid.UUID("00000000-0000-0000-0000-000000000001")
_USER_ID = uuid.UUID("00000000-0000-0000-0000-000000000002")
_TEAM_ID = uuid.UUID("00000000-0000-0000-0000-000000000003")
_NOW = datetime(2025, 1, 1, tzinfo=UTC)
_SCIM_TOKEN = "test-scim-token-12345"

_SQLALCHEMY_ERROR = SQLAlchemyError("mock", "mock", "mock db connection failure")

_MOCK_USER = MagicMock()
_MOCK_USER.id = _USER_ID
_MOCK_USER.organisation_id = _ORG_ID
_MOCK_USER.email = "jane@example.com"
_MOCK_USER.display_name = "Jane Doe"
_MOCK_USER.active = True
_MOCK_USER.org_role = "runner"
_MOCK_USER.auth_provider = "scim"
_MOCK_USER.created_at = _NOW
_MOCK_USER.updated_at = _NOW

_MOCK_TEAM = MagicMock()
_MOCK_TEAM.id = _TEAM_ID
_MOCK_TEAM.organisation_id = _ORG_ID
_MOCK_TEAM.name = "Engineering"
_MOCK_TEAM.created_by = _USER_ID
_MOCK_TEAM.created_at = _NOW
_MOCK_TEAM.updated_at = _NOW

_MOCK_MEMBERSHIP = MagicMock()
_MOCK_MEMBERSHIP.id = uuid.uuid4()
_MOCK_MEMBERSHIP.team_id = _TEAM_ID
_MOCK_MEMBERSHIP.user_id = _USER_ID

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

_PATCH_BODY = {
    "schemas": ["urn:ietf:params:scim:api:messages:2.0:PatchOp"],
    "Operations": [{"op": "replace", "path": "active", "value": False}],
}


def _make_settings() -> Settings:
    return Settings(
        database_url="postgresql+asyncpg://localhost/test",
        secret_key=_VALID_32,
        fernet_key=_VALID_32,
        modulo_admin_password="testpass",
        modulo_license_key="enterprise-license",
        modulo_scim_token=_SCIM_TOKEN,
        modulo_public_url="http://localhost:8000",
    )


def _make_settings_no_public_url() -> Settings:
    return Settings(
        database_url="postgresql+asyncpg://localhost/test",
        secret_key=_VALID_32,
        fernet_key=_VALID_32,
        modulo_admin_password="testpass",
        modulo_license_key="enterprise-license",
        modulo_scim_token=_SCIM_TOKEN,
        modulo_public_url="",
    )


def _make_mock_session() -> AsyncMock:
    session = AsyncMock()
    begin_cm = AsyncMock()
    begin_cm.__aenter__ = AsyncMock(return_value=None)
    begin_cm.__aexit__ = AsyncMock(return_value=False)
    session.begin = MagicMock(return_value=begin_cm)
    return session


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


def _assert_503(resp):
    assert resp.status_code == 503, f"Expected 503, got {resp.status_code}"
    detail = resp.json().get("detail", "")
    assert "database error" in detail.lower(), f"Expected 'database error', got: {detail}"


# ===========================================================================
# SQLAlchemyError → 503 for all 12 User/Group endpoints
# ===========================================================================


class TestListUsersSQLAlchemyError:
    def test_returns_503(self, client: TestClient) -> None:
        with (
            patch("modulo.api.routes.scim.scim_list_users", side_effect=_SQLALCHEMY_ERROR),
            patch("modulo.api.routes.scim.set_rls_org"),
        ):
            resp = client.get(
                "/scim/v2/Users",
                headers={"Authorization": f"Bearer {_SCIM_TOKEN}"},
            )
        _assert_503(resp)


class TestCreateUserSQLAlchemyError:
    def test_returns_503(self, client: TestClient) -> None:
        with (
            patch(
                "modulo.db.crud.account.get_account_by_email",
                side_effect=_SQLALCHEMY_ERROR,
            ),
            patch("modulo.api.routes.scim.set_rls_org"),
        ):
            resp = client.post(
                "/scim/v2/Users",
                json=_USER_CREATE_BODY,
                headers={"Authorization": f"Bearer {_SCIM_TOKEN}"},
            )
        _assert_503(resp)


class TestGetUserSQLAlchemyError:
    def test_returns_503(self, client: TestClient) -> None:
        with (
            patch("modulo.api.routes.scim.scim_get_user", side_effect=_SQLALCHEMY_ERROR),
            patch("modulo.api.routes.scim.set_rls_org"),
        ):
            resp = client.get(
                f"/scim/v2/Users/{_USER_ID}",
                headers={"Authorization": f"Bearer {_SCIM_TOKEN}"},
            )
        _assert_503(resp)


class TestReplaceUserSQLAlchemyError:
    def test_returns_503(self, client: TestClient) -> None:
        with (
            patch("modulo.api.routes.scim.scim_get_user", side_effect=_SQLALCHEMY_ERROR),
            patch("modulo.api.routes.scim.set_rls_org"),
        ):
            resp = client.put(
                f"/scim/v2/Users/{_USER_ID}",
                json=_USER_CREATE_BODY,
                headers={"Authorization": f"Bearer {_SCIM_TOKEN}"},
            )
        _assert_503(resp)


class TestPatchUserSQLAlchemyError:
    def test_returns_503(self, client: TestClient) -> None:
        with (
            patch("modulo.api.routes.scim.scim_get_user", side_effect=_SQLALCHEMY_ERROR),
            patch("modulo.api.routes.scim.set_rls_org"),
        ):
            resp = client.patch(
                f"/scim/v2/Users/{_USER_ID}",
                json=_PATCH_BODY,
                headers={"Authorization": f"Bearer {_SCIM_TOKEN}"},
            )
        _assert_503(resp)


class TestDeleteUserSQLAlchemyError:
    def test_returns_503(self, client: TestClient) -> None:
        with (
            patch(
                "modulo.api.routes.scim.scim_delete_user_by_id",
                side_effect=_SQLALCHEMY_ERROR,
            ),
            patch("modulo.api.routes.scim.set_rls_org"),
        ):
            resp = client.delete(
                f"/scim/v2/Users/{_USER_ID}",
                headers={"Authorization": f"Bearer {_SCIM_TOKEN}"},
            )
        _assert_503(resp)


class TestListGroupsSQLAlchemyError:
    def test_returns_503(self, client: TestClient) -> None:
        with (
            patch(
                "modulo.api.routes.scim.scim_list_groups",
                side_effect=_SQLALCHEMY_ERROR,
            ),
            patch("modulo.api.routes.scim.set_rls_org"),
        ):
            resp = client.get(
                "/scim/v2/Groups",
                headers={"Authorization": f"Bearer {_SCIM_TOKEN}"},
            )
        _assert_503(resp)


class TestCreateGroupSQLAlchemyError:
    def test_returns_503(self, client: TestClient) -> None:
        with (
            patch(
                "modulo.db.crud.team.get_team_by_name",
                side_effect=_SQLALCHEMY_ERROR,
            ),
            patch("modulo.api.routes.scim.set_rls_org"),
        ):
            resp = client.post(
                "/scim/v2/Groups",
                json=_GROUP_CREATE_BODY,
                headers={"Authorization": f"Bearer {_SCIM_TOKEN}"},
            )
        _assert_503(resp)


class TestGetGroupSQLAlchemyError:
    def test_returns_503(self, client: TestClient) -> None:
        with (
            patch(
                "modulo.api.routes.scim.scim_get_group",
                side_effect=_SQLALCHEMY_ERROR,
            ),
            patch("modulo.api.routes.scim.set_rls_org"),
        ):
            resp = client.get(
                f"/scim/v2/Groups/{_TEAM_ID}",
                headers={"Authorization": f"Bearer {_SCIM_TOKEN}"},
            )
        _assert_503(resp)


class TestReplaceGroupSQLAlchemyError:
    def test_returns_503(self, client: TestClient) -> None:
        with (
            patch(
                "modulo.api.routes.scim.scim_get_group",
                side_effect=_SQLALCHEMY_ERROR,
            ),
            patch("modulo.api.routes.scim.set_rls_org"),
        ):
            resp = client.put(
                f"/scim/v2/Groups/{_TEAM_ID}",
                json=_GROUP_CREATE_BODY,
                headers={"Authorization": f"Bearer {_SCIM_TOKEN}"},
            )
        _assert_503(resp)


class TestPatchGroupSQLAlchemyError:
    def test_returns_503(self, client: TestClient) -> None:
        with (
            patch(
                "modulo.api.routes.scim.scim_get_group",
                side_effect=_SQLALCHEMY_ERROR,
            ),
            patch("modulo.api.routes.scim.set_rls_org"),
        ):
            resp = client.patch(
                f"/scim/v2/Groups/{_TEAM_ID}",
                json=_PATCH_BODY,
                headers={"Authorization": f"Bearer {_SCIM_TOKEN}"},
            )
        _assert_503(resp)


class TestDeleteGroupSQLAlchemyError:
    def test_returns_503(self, client: TestClient) -> None:
        with (
            patch(
                "modulo.api.routes.scim.scim_delete_group_by_id",
                side_effect=_SQLALCHEMY_ERROR,
            ),
            patch("modulo.api.routes.scim.set_rls_org"),
        ):
            resp = client.delete(
                f"/scim/v2/Groups/{_TEAM_ID}",
                headers={"Authorization": f"Bearer {_SCIM_TOKEN}"},
            )
        _assert_503(resp)


# ===========================================================================
# _get_base_url raises 500 when MODULO_PUBLIC_URL is unset
# ===========================================================================


class TestGetBaseUrlMissing:
    """_get_base_url should raise 500 when modulo_public_url is not configured."""

    def test_list_users_returns_500(self) -> None:
        app.dependency_overrides[get_settings] = _make_settings_no_public_url
        app.dependency_overrides[get_db_session] = lambda: _make_mock_session()
        app.dependency_overrides[_get_engine] = lambda: MagicMock()
        app.dependency_overrides[get_scim_principal] = lambda: ScimPrincipal(organisation_id=_ORG_ID)

        with (
            patch("modulo.api.routes.scim.scim_list_users", return_value=([_MOCK_USER], 1)),
            patch("modulo.api.routes.scim.set_rls_org"),
        ):
            resp = TestClient(app).get(
                "/scim/v2/Users",
                headers={"Authorization": f"Bearer {_SCIM_TOKEN}"},
            )
        app.dependency_overrides.clear()
        assert resp.status_code == 500
        detail = resp.json().get("detail", "")
        assert "MODULO_PUBLIC_URL" in detail, f"Expected mention of MODULO_PUBLIC_URL, got: {detail}"

    def test_get_user_returns_500(self) -> None:
        app.dependency_overrides[get_settings] = _make_settings_no_public_url
        app.dependency_overrides[get_db_session] = lambda: _make_mock_session()
        app.dependency_overrides[_get_engine] = lambda: MagicMock()
        app.dependency_overrides[get_scim_principal] = lambda: ScimPrincipal(organisation_id=_ORG_ID)

        with (
            patch("modulo.api.routes.scim.scim_get_user", return_value=_MOCK_USER),
            patch("modulo.api.routes.scim.set_rls_org"),
        ):
            resp = TestClient(app).get(
                f"/scim/v2/Users/{_USER_ID}",
                headers={"Authorization": f"Bearer {_SCIM_TOKEN}"},
            )
        app.dependency_overrides.clear()
        assert resp.status_code == 500
        detail = resp.json().get("detail", "")
        assert "MODULO_PUBLIC_URL" in detail
