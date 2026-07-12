"""Unit tests for SCIM error handling.

Verifies all 12 SCIM route handlers return correct error status codes:
- ProgrammingError → 501 (missing DB table)
- SQLAlchemyError → 503 (connection/deadlock failure)
- Generic Exception → 500 (unexpected Python error)
"""

import uuid
from collections.abc import AsyncGenerator, Generator
from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.exc import ProgrammingError, SQLAlchemyError

from modulo.api.dependencies import _get_engine, get_db_session, get_plan_context
from modulo.auth.scim_auth import ScimPrincipal, get_scim_principal
from modulo.settings import Settings, get_settings

_NOW = datetime(2025, 1, 1, tzinfo=UTC)

_VALID_32 = "a" * 32
_ORG_ID = uuid.UUID("00000000-0000-0000-0000-000000000001")
_USER_ID = uuid.UUID("00000000-0000-0000-0000-000000000002")
_TEAM_ID = uuid.UUID("00000000-0000-0000-0000-000000000003")
_SCIM_TOKEN = "test-scim-token-12345"

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

_PROGRAMMING_ERROR = ProgrammingError("mock statement", [], Exception("mock table does not exist"))


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


def _make_mock_session() -> AsyncMock:
    session = AsyncMock()
    begin_cm = AsyncMock()
    begin_cm.__aenter__ = AsyncMock(return_value=None)
    begin_cm.__aexit__ = AsyncMock(return_value=False)
    session.begin = MagicMock(return_value=begin_cm)
    return session


class _AllFeatures:
    """Stub that enables every feature — used to override ``get_plan_context``."""
    def feature_enabled(self, name: str) -> bool:
        return True
    def list_enabled_features(self) -> list:
        return []
    def tier(self) -> str:
        return "enterprise"
    def has_license_key(self) -> bool:
        return True


async def _override_plan_context() -> _AllFeatures:
    return _AllFeatures()


@pytest.fixture()
def client() -> Generator[TestClient, None, None]:
    from modulo.api.main import app

    mock_session = _make_mock_session()

    async def override_session() -> AsyncGenerator[AsyncMock, None]:
        yield mock_session

    app.dependency_overrides[get_settings] = _make_settings
    app.dependency_overrides[get_db_session] = override_session
    app.dependency_overrides[_get_engine] = lambda: MagicMock()
    app.dependency_overrides[get_scim_principal] = lambda: ScimPrincipal(organisation_id=_ORG_ID)
    app.dependency_overrides[get_plan_context] = _override_plan_context
    yield TestClient(app)
    app.dependency_overrides.clear()


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


def _assert_501(resp):
    assert resp.status_code == 501, f"Expected 501, got {resp.status_code}"
    detail = resp.json().get("detail", "")
    assert "migrations" in detail.lower(), f"Expected detail mentioning migrations, got: {detail}"


class TestListUsersProgrammingError:
    def test_returns_501(self, client: TestClient) -> None:
        with (
            patch("modulo.api.routes.scim.scim_list_users", side_effect=_PROGRAMMING_ERROR),
            patch("modulo.api.routes.scim.set_rls_org"),
        ):
            resp = client.get(
                "/scim/v2/Users",
                headers={"Authorization": f"Bearer {_SCIM_TOKEN}"},
            )
        _assert_501(resp)


class TestCreateUserProgrammingError:
    def test_returns_501(self, client: TestClient) -> None:
        with (
            patch(
                "modulo.db.crud.account.get_account_by_email",
                side_effect=_PROGRAMMING_ERROR,
            ),
            patch("modulo.api.routes.scim.set_rls_org"),
        ):
            resp = client.post(
                "/scim/v2/Users",
                json=_USER_CREATE_BODY,
                headers={"Authorization": f"Bearer {_SCIM_TOKEN}"},
            )
        _assert_501(resp)


class TestGetUserProgrammingError:
    def test_returns_501(self, client: TestClient) -> None:
        with (
            patch("modulo.api.routes.scim.scim_get_user", side_effect=_PROGRAMMING_ERROR),
            patch("modulo.api.routes.scim.set_rls_org"),
        ):
            resp = client.get(
                f"/scim/v2/Users/{_USER_ID}",
                headers={"Authorization": f"Bearer {_SCIM_TOKEN}"},
            )
        _assert_501(resp)


class TestReplaceUserProgrammingError:
    def test_returns_501(self, client: TestClient) -> None:
        with (
            patch("modulo.api.routes.scim.scim_get_user", side_effect=_PROGRAMMING_ERROR),
            patch("modulo.api.routes.scim.set_rls_org"),
        ):
            resp = client.put(
                f"/scim/v2/Users/{_USER_ID}",
                json=_USER_CREATE_BODY,
                headers={"Authorization": f"Bearer {_SCIM_TOKEN}"},
            )
        _assert_501(resp)


class TestPatchUserProgrammingError:
    def test_returns_501(self, client: TestClient) -> None:
        with (
            patch("modulo.api.routes.scim.scim_get_user", side_effect=_PROGRAMMING_ERROR),
            patch("modulo.api.routes.scim.set_rls_org"),
        ):
            resp = client.patch(
                f"/scim/v2/Users/{_USER_ID}",
                json=_PATCH_BODY,
                headers={"Authorization": f"Bearer {_SCIM_TOKEN}"},
            )
        _assert_501(resp)


class TestDeleteUserProgrammingError:
    def test_returns_501(self, client: TestClient) -> None:
        with (
            patch(
                "modulo.api.routes.scim.scim_delete_user_by_id",
                side_effect=_PROGRAMMING_ERROR,
            ),
            patch("modulo.api.routes.scim.set_rls_org"),
        ):
            resp = client.delete(
                f"/scim/v2/Users/{_USER_ID}",
                headers={"Authorization": f"Bearer {_SCIM_TOKEN}"},
            )
        _assert_501(resp)


class TestListGroupsProgrammingError:
    def test_returns_501(self, client: TestClient) -> None:
        with (
            patch(
                "modulo.api.routes.scim.scim_list_groups",
                side_effect=_PROGRAMMING_ERROR,
            ),
            patch("modulo.api.routes.scim.set_rls_org"),
        ):
            resp = client.get(
                "/scim/v2/Groups",
                headers={"Authorization": f"Bearer {_SCIM_TOKEN}"},
            )
        _assert_501(resp)


class TestCreateGroupProgrammingError:
    def test_returns_501(self, client: TestClient) -> None:
        with (
            patch(
                "modulo.db.crud.team.get_team_by_name",
                side_effect=_PROGRAMMING_ERROR,
            ),
            patch("modulo.api.routes.scim.set_rls_org"),
        ):
            resp = client.post(
                "/scim/v2/Groups",
                json=_GROUP_CREATE_BODY,
                headers={"Authorization": f"Bearer {_SCIM_TOKEN}"},
            )
        _assert_501(resp)


class TestGetGroupProgrammingError:
    def test_returns_501(self, client: TestClient) -> None:
        with (
            patch(
                "modulo.api.routes.scim.scim_get_group",
                side_effect=_PROGRAMMING_ERROR,
            ),
            patch("modulo.api.routes.scim.set_rls_org"),
        ):
            resp = client.get(
                f"/scim/v2/Groups/{_TEAM_ID}",
                headers={"Authorization": f"Bearer {_SCIM_TOKEN}"},
            )
        _assert_501(resp)


class TestReplaceGroupProgrammingError:
    def test_returns_501(self, client: TestClient) -> None:
        with (
            patch(
                "modulo.api.routes.scim.scim_get_group",
                side_effect=_PROGRAMMING_ERROR,
            ),
            patch("modulo.api.routes.scim.set_rls_org"),
        ):
            resp = client.put(
                f"/scim/v2/Groups/{_TEAM_ID}",
                json=_GROUP_CREATE_BODY,
                headers={"Authorization": f"Bearer {_SCIM_TOKEN}"},
            )
        _assert_501(resp)


class TestPatchGroupProgrammingError:
    def test_returns_501(self, client: TestClient) -> None:
        with (
            patch(
                "modulo.api.routes.scim.scim_get_group",
                side_effect=_PROGRAMMING_ERROR,
            ),
            patch("modulo.api.routes.scim.set_rls_org"),
        ):
            resp = client.patch(
                f"/scim/v2/Groups/{_TEAM_ID}",
                json=_PATCH_BODY,
                headers={"Authorization": f"Bearer {_SCIM_TOKEN}"},
            )
        _assert_501(resp)


class TestDeleteGroupProgrammingError:
    def test_returns_501(self, client: TestClient) -> None:
        with (
            patch(
                "modulo.api.routes.scim.scim_delete_group_by_id",
                side_effect=_PROGRAMMING_ERROR,
            ),
            patch("modulo.api.routes.scim.set_rls_org"),
        ):
            resp = client.delete(
                f"/scim/v2/Groups/{_TEAM_ID}",
                headers={"Authorization": f"Bearer {_SCIM_TOKEN}"},
            )
        _assert_501(resp)


# ── SQLAlchemyError → 503 tests ─────────────────────────────────


_SQLALCHEMY_ERROR = SQLAlchemyError("mock", "connection error", Exception("mock connection failure"))


def _assert_503(resp):
    assert resp.status_code == 503, f"Expected 503, got {resp.status_code}"
    detail = resp.json().get("detail", "")
    assert "database error" in detail.lower(), f"Expected detail mentioning database error, got: {detail}"


class TestListUsersSQLAlchemyError:
    def test_returns_503(self, client: TestClient) -> None:
        with (
            patch("modulo.api.routes.scim.scim_list_users", side_effect=_SQLALCHEMY_ERROR),
            patch("modulo.api.routes.scim.set_rls_org"),
        ):
            resp = client.get("/scim/v2/Users", headers={"Authorization": f"Bearer {_SCIM_TOKEN}"})
        _assert_503(resp)


class TestCreateUserSQLAlchemyError:
    def test_returns_503(self, client: TestClient) -> None:
        with (
            patch("modulo.db.crud.account.get_account_by_email", side_effect=_SQLALCHEMY_ERROR),
            patch("modulo.api.routes.scim.set_rls_org"),
        ):
            resp = client.post(
                "/scim/v2/Users", json=_USER_CREATE_BODY, headers={"Authorization": f"Bearer {_SCIM_TOKEN}"}
            )
        _assert_503(resp)


class TestGetUserSQLAlchemyError:
    def test_returns_503(self, client: TestClient) -> None:
        with (
            patch("modulo.api.routes.scim.scim_get_user", side_effect=_SQLALCHEMY_ERROR),
            patch("modulo.api.routes.scim.set_rls_org"),
        ):
            resp = client.get(f"/scim/v2/Users/{_USER_ID}", headers={"Authorization": f"Bearer {_SCIM_TOKEN}"})
        _assert_503(resp)


class TestReplaceUserSQLAlchemyError:
    def test_returns_503(self, client: TestClient) -> None:
        with (
            patch("modulo.api.routes.scim.scim_get_user", side_effect=_SQLALCHEMY_ERROR),
            patch("modulo.api.routes.scim.set_rls_org"),
        ):
            resp = client.put(
                f"/scim/v2/Users/{_USER_ID}", json=_USER_CREATE_BODY, headers={"Authorization": f"Bearer {_SCIM_TOKEN}"}
            )
        _assert_503(resp)


class TestPatchUserSQLAlchemyError:
    def test_returns_503(self, client: TestClient) -> None:
        with (
            patch("modulo.api.routes.scim.scim_get_user", side_effect=_SQLALCHEMY_ERROR),
            patch("modulo.api.routes.scim.set_rls_org"),
        ):
            resp = client.patch(
                f"/scim/v2/Users/{_USER_ID}", json=_PATCH_BODY, headers={"Authorization": f"Bearer {_SCIM_TOKEN}"}
            )
        _assert_503(resp)


class TestDeleteUserSQLAlchemyError:
    def test_returns_503(self, client: TestClient) -> None:
        with (
            patch("modulo.api.routes.scim.scim_delete_user_by_id", side_effect=_SQLALCHEMY_ERROR),
            patch("modulo.api.routes.scim.set_rls_org"),
        ):
            resp = client.delete(f"/scim/v2/Users/{_USER_ID}", headers={"Authorization": f"Bearer {_SCIM_TOKEN}"})
        _assert_503(resp)


class TestListGroupsSQLAlchemyError:
    def test_returns_503(self, client: TestClient) -> None:
        with (
            patch("modulo.api.routes.scim.scim_list_groups", side_effect=_SQLALCHEMY_ERROR),
            patch("modulo.api.routes.scim.set_rls_org"),
        ):
            resp = client.get("/scim/v2/Groups", headers={"Authorization": f"Bearer {_SCIM_TOKEN}"})
        _assert_503(resp)


class TestCreateGroupSQLAlchemyError:
    def test_returns_503(self, client: TestClient) -> None:
        with (
            patch("modulo.db.crud.team.get_team_by_name", side_effect=_SQLALCHEMY_ERROR),
            patch("modulo.api.routes.scim.set_rls_org"),
        ):
            resp = client.post(
                "/scim/v2/Groups", json=_GROUP_CREATE_BODY, headers={"Authorization": f"Bearer {_SCIM_TOKEN}"}
            )
        _assert_503(resp)


class TestGetGroupSQLAlchemyError:
    def test_returns_503(self, client: TestClient) -> None:
        with (
            patch("modulo.api.routes.scim.scim_get_group", side_effect=_SQLALCHEMY_ERROR),
            patch("modulo.api.routes.scim.set_rls_org"),
        ):
            resp = client.get(f"/scim/v2/Groups/{_TEAM_ID}", headers={"Authorization": f"Bearer {_SCIM_TOKEN}"})
        _assert_503(resp)


class TestReplaceGroupSQLAlchemyError:
    def test_returns_503(self, client: TestClient) -> None:
        with (
            patch("modulo.api.routes.scim.scim_get_group", side_effect=_SQLALCHEMY_ERROR),
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
            patch("modulo.api.routes.scim.scim_get_group", side_effect=_SQLALCHEMY_ERROR),
            patch("modulo.api.routes.scim.set_rls_org"),
        ):
            resp = client.patch(
                f"/scim/v2/Groups/{_TEAM_ID}", json=_PATCH_BODY, headers={"Authorization": f"Bearer {_SCIM_TOKEN}"}
            )
        _assert_503(resp)


class TestDeleteGroupSQLAlchemyError:
    def test_returns_503(self, client: TestClient) -> None:
        with (
            patch("modulo.api.routes.scim.scim_delete_group_by_id", side_effect=_SQLALCHEMY_ERROR),
            patch("modulo.api.routes.scim.set_rls_org"),
        ):
            resp = client.delete(f"/scim/v2/Groups/{_TEAM_ID}", headers={"Authorization": f"Bearer {_SCIM_TOKEN}"})
        _assert_503(resp)


# ── Exception → 500 tests ──────────────────────────────────────


def _assert_500(resp):
    assert resp.status_code == 500, f"Expected 500, got {resp.status_code}"


class TestListUsersException:
    def test_returns_500(self, client: TestClient) -> None:
        with (
            patch("modulo.api.routes.scim.scim_list_users", side_effect=ValueError("mock ValueError")),
            patch("modulo.api.routes.scim.set_rls_org"),
        ):
            resp = client.get("/scim/v2/Users", headers={"Authorization": f"Bearer {_SCIM_TOKEN}"})
        _assert_500(resp)


class TestCreateUserException:
    def test_returns_500(self, client: TestClient) -> None:
        with (
            patch("modulo.db.crud.account.get_account_by_email", side_effect=ValueError("mock ValueError")),
            patch("modulo.api.routes.scim.set_rls_org"),
        ):
            resp = client.post(
                "/scim/v2/Users", json=_USER_CREATE_BODY, headers={"Authorization": f"Bearer {_SCIM_TOKEN}"}
            )
        _assert_500(resp)


class TestGetUserException:
    def test_returns_500(self, client: TestClient) -> None:
        with (
            patch("modulo.api.routes.scim.scim_get_user", side_effect=ValueError("mock ValueError")),
            patch("modulo.api.routes.scim.set_rls_org"),
        ):
            resp = client.get(f"/scim/v2/Users/{_USER_ID}", headers={"Authorization": f"Bearer {_SCIM_TOKEN}"})
        _assert_500(resp)


class TestReplaceUserException:
    def test_returns_500(self, client: TestClient) -> None:
        with (
            patch("modulo.api.routes.scim.scim_get_user", side_effect=ValueError("mock ValueError")),
            patch("modulo.api.routes.scim.set_rls_org"),
        ):
            resp = client.put(
                f"/scim/v2/Users/{_USER_ID}", json=_USER_CREATE_BODY, headers={"Authorization": f"Bearer {_SCIM_TOKEN}"}
            )
        _assert_500(resp)


class TestPatchUserException:
    def test_returns_500(self, client: TestClient) -> None:
        with (
            patch("modulo.api.routes.scim.scim_get_user", side_effect=ValueError("mock ValueError")),
            patch("modulo.api.routes.scim.set_rls_org"),
        ):
            resp = client.patch(
                f"/scim/v2/Users/{_USER_ID}", json=_PATCH_BODY, headers={"Authorization": f"Bearer {_SCIM_TOKEN}"}
            )
        _assert_500(resp)


class TestDeleteUserException:
    def test_returns_500(self, client: TestClient) -> None:
        with (
            patch("modulo.api.routes.scim.scim_delete_user_by_id", side_effect=ValueError("mock ValueError")),
            patch("modulo.api.routes.scim.set_rls_org"),
        ):
            resp = client.delete(f"/scim/v2/Users/{_USER_ID}", headers={"Authorization": f"Bearer {_SCIM_TOKEN}"})
        _assert_500(resp)


class TestListGroupsException:
    def test_returns_500(self, client: TestClient) -> None:
        with (
            patch("modulo.api.routes.scim.scim_list_groups", side_effect=ValueError("mock ValueError")),
            patch("modulo.api.routes.scim.set_rls_org"),
        ):
            resp = client.get("/scim/v2/Groups", headers={"Authorization": f"Bearer {_SCIM_TOKEN}"})
        _assert_500(resp)


class TestCreateGroupException:
    def test_returns_500(self, client: TestClient) -> None:
        with (
            patch("modulo.db.crud.team.get_team_by_name", side_effect=ValueError("mock ValueError")),
            patch("modulo.api.routes.scim.set_rls_org"),
        ):
            resp = client.post(
                "/scim/v2/Groups", json=_GROUP_CREATE_BODY, headers={"Authorization": f"Bearer {_SCIM_TOKEN}"}
            )
        _assert_500(resp)


class TestGetGroupException:
    def test_returns_500(self, client: TestClient) -> None:
        with (
            patch("modulo.api.routes.scim.scim_get_group", side_effect=ValueError("mock ValueError")),
            patch("modulo.api.routes.scim.set_rls_org"),
        ):
            resp = client.get(f"/scim/v2/Groups/{_TEAM_ID}", headers={"Authorization": f"Bearer {_SCIM_TOKEN}"})
        _assert_500(resp)


class TestReplaceGroupException:
    def test_returns_500(self, client: TestClient) -> None:
        with (
            patch("modulo.api.routes.scim.scim_get_group", side_effect=ValueError("mock ValueError")),
            patch("modulo.api.routes.scim.set_rls_org"),
        ):
            resp = client.put(
                f"/scim/v2/Groups/{_TEAM_ID}",
                json=_GROUP_CREATE_BODY,
                headers={"Authorization": f"Bearer {_SCIM_TOKEN}"},
            )
        _assert_500(resp)


class TestPatchGroupException:
    def test_returns_500(self, client: TestClient) -> None:
        with (
            patch("modulo.api.routes.scim.scim_get_group", side_effect=ValueError("mock ValueError")),
            patch("modulo.api.routes.scim.set_rls_org"),
        ):
            resp = client.patch(
                f"/scim/v2/Groups/{_TEAM_ID}", json=_PATCH_BODY, headers={"Authorization": f"Bearer {_SCIM_TOKEN}"}
            )
        _assert_500(resp)


class TestDeleteGroupException:
    def test_returns_500(self, client: TestClient) -> None:
        with (
            patch("modulo.api.routes.scim.scim_delete_group_by_id", side_effect=ValueError("mock ValueError")),
            patch("modulo.api.routes.scim.set_rls_org"),
        ):
            resp = client.delete(f"/scim/v2/Groups/{_TEAM_ID}", headers={"Authorization": f"Bearer {_SCIM_TOKEN}"})
        _assert_500(resp)


# ===========================================================================
# _get_base_url raises 500 when MODULO_PUBLIC_URL is unset
# ===========================================================================


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


class TestGetBaseUrlMissing:
    """_get_base_url should raise 500 when modulo_public_url is not configured."""

    def test_list_users_returns_500(self) -> None:
        from modulo.api.main import app

        app.dependency_overrides[get_settings] = _make_settings_no_public_url
        app.dependency_overrides[get_db_session] = lambda: _make_mock_session()
        app.dependency_overrides[_get_engine] = lambda: MagicMock()
        app.dependency_overrides[get_scim_principal] = lambda: ScimPrincipal(organisation_id=_ORG_ID)
        app.dependency_overrides[get_plan_context] = _override_plan_context

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
        from modulo.api.main import app

        app.dependency_overrides[get_settings] = _make_settings_no_public_url
        app.dependency_overrides[get_db_session] = lambda: _make_mock_session()
        app.dependency_overrides[_get_engine] = lambda: MagicMock()
        app.dependency_overrides[get_scim_principal] = lambda: ScimPrincipal(organisation_id=_ORG_ID)
        app.dependency_overrides[get_plan_context] = _override_plan_context

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
