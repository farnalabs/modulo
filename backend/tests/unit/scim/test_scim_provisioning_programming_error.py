"""Unit tests for SCIM ProgrammingError→501 handling.

Verifies that all 12 SCIM route handlers return 501 Not Implemented
when a ProgrammingError (missing DB table) is raised inside the
async session block.
"""

import uuid
from collections.abc import AsyncGenerator, Generator
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.exc import ProgrammingError

from modulo.api.dependencies import _get_engine, get_db_session
from modulo.api.main import app
from modulo.auth.scim_auth import ScimPrincipal, get_scim_principal
from modulo.settings import Settings, get_settings

_VALID_32 = "a" * 32
_ORG_ID = uuid.UUID("00000000-0000-0000-0000-000000000001")
_USER_ID = uuid.UUID("00000000-0000-0000-0000-000000000002")
_TEAM_ID = uuid.UUID("00000000-0000-0000-0000-000000000003")
_SCIM_TOKEN = "test-scim-token-12345"

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
