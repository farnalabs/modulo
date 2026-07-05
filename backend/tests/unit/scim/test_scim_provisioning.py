"""Unit tests for SCIM 2.0 provisioning endpoints (/scim/v2/Users, /scim/v2/Groups)."""

import uuid
from collections.abc import AsyncGenerator, Generator
from datetime import UTC, datetime
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
_NOW = datetime(2025, 1, 1, tzinfo=UTC)

_SCIM_TOKEN = "test-scim-token-12345"


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


@pytest.fixture()
def unauth_client() -> Generator[TestClient, None, None]:
    app.dependency_overrides[get_settings] = _make_settings
    yield TestClient(app)
    app.dependency_overrides.clear()


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

_MOCK_USER_LIST = ([_MOCK_USER], 1)

_MOCK_TEAM = MagicMock()
_MOCK_TEAM.id = _TEAM_ID
_MOCK_TEAM.organisation_id = _ORG_ID
_MOCK_TEAM.name = "Engineering"
_MOCK_TEAM.description = None
_MOCK_TEAM.created_by = _USER_ID
_MOCK_TEAM.created_at = _NOW
_MOCK_TEAM.updated_at = _NOW

_MOCK_TEAM_LIST = ([_MOCK_TEAM], 1)

_MOCK_MEMBERSHIP = MagicMock()
_MOCK_MEMBERSHIP.id = uuid.uuid4()
_MOCK_MEMBERSHIP.team_id = _TEAM_ID
_MOCK_MEMBERSHIP.user_id = _USER_ID
_MOCK_MEMBERSHIP.role = "member"
_MOCK_MEMBERSHIP.created_at = _NOW

_MOCK_MEMBERSHIPS = [_MOCK_MEMBERSHIP]

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

_PATCH_USER_BODY = {
    "schemas": ["urn:ietf:params:scim:api:messages:2.0:PatchOp"],
    "Operations": [{"op": "replace", "path": "active", "value": False}],
}

_PATCH_GROUP_ADD_MEMBER = {
    "schemas": ["urn:ietf:params:scim:api:messages:2.0:PatchOp"],
    "Operations": [{"op": "add", "path": "members", "value": [{"value": str(_USER_ID)}]}],
}


# ── Auth Edge Cases ──────────────────────────────────────────────────


class TestAuthEdgeCases:
    """SCIM token auth edge cases beyond 401-without-header."""

    def test_missing_token_returns_501(self) -> None:
        def _settings_no_scim_token() -> Settings:
            return Settings(
                database_url="postgresql+asyncpg://localhost/test",
                secret_key=_VALID_32,
                fernet_key=_VALID_32,
                modulo_admin_password="testpass",
                modulo_license_key="enterprise-license",
                modulo_scim_token="",
                modulo_public_url="http://localhost:8000",
            )

        app.dependency_overrides[get_settings] = _settings_no_scim_token
        app.dependency_overrides[get_db_session] = lambda: _make_mock_session()
        app.dependency_overrides[_get_engine] = lambda: MagicMock()
        resp = TestClient(app).get(
            "/scim/v2/ServiceProviderConfig",
            headers={"Authorization": "Bearer some-token"},
        )
        app.dependency_overrides.clear()
        assert resp.status_code == 501

    def test_invalid_token_returns_401(self) -> None:
        app.dependency_overrides[get_settings] = _make_settings
        resp = TestClient(app).get(
            "/scim/v2/ServiceProviderConfig",
            headers={"Authorization": "Bearer wrong-token"},
        )
        app.dependency_overrides.clear()
        assert resp.status_code == 401

    def test_invalid_default_org_uuid_returns_500(self) -> None:
        def _settings_bad_org_uuid() -> Settings:
            return Settings(
                database_url="postgresql+asyncpg://localhost/test",
                secret_key=_VALID_32,
                fernet_key=_VALID_32,
                modulo_admin_password="testpass",
                modulo_license_key="enterprise-license",
                modulo_scim_token=_SCIM_TOKEN,
                modulo_scim_default_org_id="not-a-uuid",
                modulo_public_url="http://localhost:8000",
            )

        app.dependency_overrides[get_settings] = _settings_bad_org_uuid
        app.dependency_overrides[get_db_session] = lambda: _make_mock_session()
        app.dependency_overrides[_get_engine] = lambda: MagicMock()
        resp = TestClient(app).get(
            "/scim/v2/Users",
            headers={"Authorization": f"Bearer {_SCIM_TOKEN}"},
        )
        app.dependency_overrides.clear()
        assert resp.status_code == 500

    def test_no_org_in_db_returns_500(self) -> None:
        mock_session = _make_mock_session()
        mock_session.execute = AsyncMock(return_value=AsyncMock(scalar_one_or_none=MagicMock(return_value=None)))

        async def override_session() -> AsyncGenerator[AsyncMock, None]:
            yield mock_session

        app.dependency_overrides[get_settings] = _make_settings
        app.dependency_overrides[get_db_session] = override_session
        app.dependency_overrides[_get_engine] = lambda: MagicMock()
        resp = TestClient(app).get(
            "/scim/v2/Users",
            headers={"Authorization": f"Bearer {_SCIM_TOKEN}"},
        )
        app.dependency_overrides.clear()
        assert resp.status_code == 500


# ── Pagination / Filter Edge Cases ───────────────────────────────────


class TestPaginationEdgeCases:
    def test_count_exceeds_max_returns_422(self, client: TestClient) -> None:
        resp = client.get(
            "/scim/v2/Users?count=200",
            headers={"Authorization": f"Bearer {_SCIM_TOKEN}"},
        )
        assert resp.status_code == 422

    def test_count_zero_returns_422(self, client: TestClient) -> None:
        resp = client.get(
            "/scim/v2/Users?count=0",
            headers={"Authorization": f"Bearer {_SCIM_TOKEN}"},
        )
        assert resp.status_code == 422

    def test_filter_by_email_returns_matching(self, client: TestClient) -> None:
        user_a = MagicMock()
        user_a.id = uuid.uuid4()
        user_a.organisation_id = _ORG_ID
        user_a.email = "alice@example.com"
        user_a.display_name = "Alice"
        user_a.active = True
        user_a.org_role = "runner"
        user_a.auth_provider = "scim"
        user_a.created_at = _NOW
        user_a.updated_at = _NOW

        user_b = MagicMock()
        user_b.id = uuid.uuid4()
        user_b.organisation_id = _ORG_ID
        user_b.email = "bob@other.com"
        user_b.display_name = "Bob"
        user_b.active = True
        user_b.org_role = "runner"
        user_b.auth_provider = "scim"
        user_b.created_at = _NOW
        user_b.updated_at = _NOW

        with (
            patch(
                "modulo.api.routes.scim.scim_list_users",
                return_value=([user_a], 1),
            ),
            patch("modulo.api.routes.scim.set_rls_org"),
        ):
            resp = client.get(
                "/scim/v2/Users?filter=alice",
                headers={"Authorization": f"Bearer {_SCIM_TOKEN}"},
            )
        assert resp.status_code == 200
        data = resp.json()
        assert data["totalResults"] == 1
        assert data["Resources"][0]["userName"] == "alice@example.com"

    def test_filter_no_match_returns_empty(self, client: TestClient) -> None:
        with (
            patch(
                "modulo.api.routes.scim.scim_list_users",
                return_value=([], 0),
            ),
            patch("modulo.api.routes.scim.set_rls_org"),
        ):
            resp = client.get(
                "/scim/v2/Users?filter=zzzzzzzzz",
                headers={"Authorization": f"Bearer {_SCIM_TOKEN}"},
            )
        assert resp.status_code == 200
        data = resp.json()
        assert data["totalResults"] == 0
        assert data["Resources"] == []

    def test_second_page_returns_empty(self, client: TestClient) -> None:
        with (
            patch(
                "modulo.api.routes.scim.scim_list_users",
                return_value=([], 1),
            ),
            patch("modulo.api.routes.scim.set_rls_org"),
        ):
            resp = client.get(
                "/scim/v2/Users?startIndex=2&count=20",
                headers={"Authorization": f"Bearer {_SCIM_TOKEN}"},
            )
        assert resp.status_code == 200
        data = resp.json()
        assert data["totalResults"] == 1
        assert data["Resources"] == []

    def test_groups_filter_no_match(self, client: TestClient) -> None:
        with (
            patch(
                "modulo.api.routes.scim.scim_list_groups",
                return_value=([], 0),
            ),
            patch("modulo.api.routes.scim.set_rls_org"),
            patch(
                "modulo.api.routes.scim.scim_list_group_members",
                return_value=[],
            ),
        ):
            resp = client.get(
                "/scim/v2/Groups?filter=nonexistent",
                headers={"Authorization": f"Bearer {_SCIM_TOKEN}"},
            )
        assert resp.status_code == 200
        data = resp.json()
        assert data["totalResults"] == 0


# ── Input Validation Edge Cases ──────────────────────────────────────


class TestInputValidation:
    def test_create_user_missing_username_returns_422(self, client: TestClient) -> None:
        body = {"schemas": ["urn:ietf:params:scim:schemas:core:2.0:User"]}
        resp = client.post(
            "/scim/v2/Users",
            json=body,
            headers={"Authorization": f"Bearer {_SCIM_TOKEN}"},
        )
        assert resp.status_code == 422

    def test_create_user_invalid_schemas_returns_422(self, client: TestClient) -> None:
        body = {**_USER_CREATE_BODY, "schemas": ["urn:ietf:params:scim:schemas:core:2.0:Group"]}
        with (
            patch("modulo.db.crud.user.get_user_by_email", return_value=None),
            patch("modulo.api.routes.scim.set_rls_org"),
            patch(
                "modulo.api.routes.scim.scim_create_user",
                return_value=_MOCK_USER,
            ),
        ):
            resp = client.post(
                "/scim/v2/Users",
                json=body,
                headers={"Authorization": f"Bearer {_SCIM_TOKEN}"},
            )
        # FastAPI does not validate schemas content; it just accepts
        assert resp.status_code == 201

    def test_create_group_missing_displayname_returns_422(self, client: TestClient) -> None:
        body = {"schemas": ["urn:ietf:params:scim:schemas:core:2.0:Group"]}
        resp = client.post(
            "/scim/v2/Groups",
            json=body,
            headers={"Authorization": f"Bearer {_SCIM_TOKEN}"},
        )
        assert resp.status_code == 422

    def test_create_group_invalid_member_ref_is_skipped(self, client: TestClient) -> None:
        body = {**_GROUP_CREATE_BODY, "members": [{"value": "not-a-uuid", "type": "User"}]}
        with (
            patch("modulo.db.crud.team.get_team_by_name", return_value=None),
            patch("modulo.db.crud.user.list_users_for_org", return_value=[_MOCK_USER]),
            patch("modulo.api.routes.scim.scim_create_group", return_value=_MOCK_TEAM),
            patch("modulo.api.routes.scim.set_rls_org"),
        ):
            resp = client.post(
                "/scim/v2/Groups",
                json=body,
                headers={"Authorization": f"Bearer {_SCIM_TOKEN}"},
            )
        assert resp.status_code == 201


# ── PATCH Edge Cases ─────────────────────────────────────────────────


class TestPatchEdgeCases:
    def test_patch_user_remove_active(self, client: TestClient) -> None:
        mock_user = MagicMock(
            id=_USER_ID,
            organisation_id=_ORG_ID,
            email="jane@example.com",
            display_name="Jane Doe",
            active=True,
            org_role="runner",
            auth_provider="scim",
            created_at=_NOW,
            updated_at=_NOW,
        )
        body = {
            "schemas": ["urn:ietf:params:scim:api:messages:2.0:PatchOp"],
            "Operations": [{"op": "remove", "path": "active"}],
        }
        with (
            patch(
                "modulo.api.routes.scim.scim_get_user",
                return_value=mock_user,
            ),
            patch("modulo.api.routes.scim.set_rls_org"),
        ):
            resp = client.patch(
                f"/scim/v2/Users/{_USER_ID}",
                json=body,
                headers={"Authorization": f"Bearer {_SCIM_TOKEN}"},
            )
        assert resp.status_code == 200
        assert mock_user.active is False

    def test_patch_user_unsupported_op_returns_400(self, client: TestClient) -> None:
        mock_user = MagicMock(
            id=_USER_ID,
            organisation_id=_ORG_ID,
            email="jane@example.com",
            display_name="Jane Doe",
            active=True,
            org_role="runner",
            auth_provider="scim",
            created_at=_NOW,
            updated_at=_NOW,
        )
        body = {
            "schemas": ["urn:ietf:params:scim:api:messages:2.0:PatchOp"],
            "Operations": [{"op": "doesNotExist", "path": "active", "value": False}],
        }
        with (
            patch(
                "modulo.api.routes.scim.scim_get_user",
                return_value=mock_user,
            ),
            patch("modulo.api.routes.scim.set_rls_org"),
        ):
            resp = client.patch(
                f"/scim/v2/Users/{_USER_ID}",
                json=body,
                headers={"Authorization": f"Bearer {_SCIM_TOKEN}"},
            )
        assert resp.status_code == 400

    def test_patch_user_invalid_op_returns_400(self, client: TestClient) -> None:
        mock_user = MagicMock(
            id=_USER_ID,
            organisation_id=_ORG_ID,
            email="jane@example.com",
            display_name="Jane Doe",
            active=True,
            org_role="runner",
            auth_provider="scim",
            created_at=_NOW,
            updated_at=_NOW,
        )
        body = {
            "schemas": ["urn:ietf:params:scim:api:messages:2.0:PatchOp"],
            "Operations": [{"op": "invalidOp"}],
        }
        with (
            patch(
                "modulo.api.routes.scim.scim_get_user",
                return_value=mock_user,
            ),
            patch("modulo.api.routes.scim.set_rls_org"),
        ):
            resp = client.patch(
                f"/scim/v2/Users/{_USER_ID}",
                json=body,
                headers={"Authorization": f"Bearer {_SCIM_TOKEN}"},
            )
        assert resp.status_code == 400
        data = resp.json()
        assert "detail" in data

    def test_patch_user_add_username(self, client: TestClient) -> None:
        mock_user = MagicMock(
            id=_USER_ID,
            organisation_id=_ORG_ID,
            email="jane@example.com",
            display_name="Jane Doe",
            active=True,
            org_role="runner",
            auth_provider="scim",
            created_at=_NOW,
            updated_at=_NOW,
        )
        body = {
            "schemas": ["urn:ietf:params:scim:api:messages:2.0:PatchOp"],
            "Operations": [{"op": "add", "value": {"userName": "new-jane@example.com"}}],
        }
        with (
            patch(
                "modulo.api.routes.scim.scim_get_user",
                return_value=mock_user,
            ),
            patch("modulo.api.routes.scim.set_rls_org"),
        ):
            resp = client.patch(
                f"/scim/v2/Users/{_USER_ID}",
                json=body,
                headers={"Authorization": f"Bearer {_SCIM_TOKEN}"},
            )
        assert resp.status_code == 200
        assert mock_user.email == "new-jane@example.com"

    def test_patch_group_remove_by_value_dict(self, client: TestClient) -> None:
        """PATCH remove with value as dict (not path-based filter)."""
        mock_team = MagicMock()
        mock_team.id = _TEAM_ID
        mock_team.organisation_id = _ORG_ID
        mock_team.name = "Engineering"
        mock_team.created_by = _USER_ID
        mock_team.created_at = _NOW
        mock_team.updated_at = _NOW

        body = {
            "schemas": ["urn:ietf:params:scim:api:messages:2.0:PatchOp"],
            "Operations": [{"op": "remove", "value": {"value": str(_USER_ID)}}],
        }
        with (
            patch(
                "modulo.api.routes.scim.scim_get_group",
                return_value=mock_team,
            ),
            patch(
                "modulo.api.routes.scim.scim_remove_group_member",
                return_value=True,
            ),
            patch(
                "modulo.api.routes.scim.scim_list_group_members",
                return_value=[],
            ),
            patch("modulo.api.routes.scim.set_rls_org"),
        ):
            resp = client.patch(
                f"/scim/v2/Groups/{_TEAM_ID}",
                json=body,
                headers={"Authorization": f"Bearer {_SCIM_TOKEN}"},
            )
        assert resp.status_code == 200

    def test_patch_group_remove_by_value_list(self, client: TestClient) -> None:
        """PATCH remove with value as list of members."""
        mock_team = MagicMock()
        mock_team.id = _TEAM_ID
        mock_team.organisation_id = _ORG_ID
        mock_team.name = "Engineering"
        mock_team.created_by = _USER_ID
        mock_team.created_at = _NOW
        mock_team.updated_at = _NOW

        body = {
            "schemas": ["urn:ietf:params:scim:api:messages:2.0:PatchOp"],
            "Operations": [{"op": "remove", "value": [{"value": str(_USER_ID)}]}],
        }
        with (
            patch(
                "modulo.api.routes.scim.scim_get_group",
                return_value=mock_team,
            ),
            patch(
                "modulo.api.routes.scim.scim_remove_group_member",
                return_value=True,
            ),
            patch(
                "modulo.api.routes.scim.scim_list_group_members",
                return_value=[],
            ),
            patch("modulo.api.routes.scim.set_rls_org"),
        ):
            resp = client.patch(
                f"/scim/v2/Groups/{_TEAM_ID}",
                json=body,
                headers={"Authorization": f"Bearer {_SCIM_TOKEN}"},
            )
        assert resp.status_code == 200

    def test_patch_group_unsupported_op_returns_400(self, client: TestClient) -> None:
        mock_team = MagicMock()
        mock_team.id = _TEAM_ID
        mock_team.organisation_id = _ORG_ID
        mock_team.name = "Engineering"
        mock_team.created_by = _USER_ID
        mock_team.created_at = _NOW
        mock_team.updated_at = _NOW

        body = {
            "schemas": ["urn:ietf:params:scim:api:messages:2.0:PatchOp"],
            "Operations": [{"op": "doesNotExist"}],
        }
        with (
            patch(
                "modulo.api.routes.scim.scim_get_group",
                return_value=mock_team,
            ),
            patch(
                "modulo.api.routes.scim.scim_list_group_members",
                return_value=_MOCK_MEMBERSHIPS,
            ),
            patch("modulo.api.routes.scim.set_rls_org"),
        ):
            resp = client.patch(
                f"/scim/v2/Groups/{_TEAM_ID}",
                json=body,
                headers={"Authorization": f"Bearer {_SCIM_TOKEN}"},
            )
        assert resp.status_code == 400

    def test_patch_group_invalid_op_returns_400(self, client: TestClient) -> None:
        mock_team = MagicMock()
        mock_team.id = _TEAM_ID
        mock_team.organisation_id = _ORG_ID
        mock_team.name = "Engineering"
        mock_team.created_by = _USER_ID
        mock_team.created_at = _NOW
        mock_team.updated_at = _NOW
        body = {
            "schemas": ["urn:ietf:params:scim:api:messages:2.0:PatchOp"],
            "Operations": [{"op": "invalidOp"}],
        }
        with (
            patch(
                "modulo.api.routes.scim.scim_get_group",
                return_value=mock_team,
            ),
            patch(
                "modulo.api.routes.scim.scim_list_group_members",
                return_value=_MOCK_MEMBERSHIPS,
            ),
            patch("modulo.api.routes.scim.set_rls_org"),
        ):
            resp = client.patch(
                f"/scim/v2/Groups/{_TEAM_ID}",
                json=body,
                headers={"Authorization": f"Bearer {_SCIM_TOKEN}"},
            )
        assert resp.status_code == 400
        data = resp.json()
        assert "detail" in data

    def test_replace_group_clear_members(self, client: TestClient) -> None:
        """PUT Group with empty members list removes all members."""
        mock_team = MagicMock()
        mock_team.id = _TEAM_ID
        mock_team.organisation_id = _ORG_ID
        mock_team.name = "Engineering"
        mock_team.created_by = _USER_ID
        mock_team.created_at = _NOW
        mock_team.updated_at = _NOW

        put_body = {
            "schemas": ["urn:ietf:params:scim:schemas:core:2.0:Group"],
            "displayName": "Engineering",
            "members": [],
        }

        mock_membership = MagicMock()
        mock_membership.id = uuid.uuid4()
        mock_membership.team_id = _TEAM_ID
        mock_membership.user_id = _USER_ID
        mock_membership.role = "member"
        mock_membership.created_at = _NOW

        with (
            patch(
                "modulo.api.routes.scim.scim_get_group",
                return_value=mock_team,
            ),
            patch(
                "modulo.api.routes.scim.scim_update_group",
                return_value=mock_team,
            ),
            patch(
                "modulo.api.routes.scim.scim_list_group_members",
                return_value=[mock_membership],
            ),
            patch(
                "modulo.api.routes.scim.scim_remove_group_member",
                return_value=True,
            ),
            patch("modulo.api.routes.scim.set_rls_org"),
        ):
            resp = client.put(
                f"/scim/v2/Groups/{_TEAM_ID}",
                json=put_body,
                headers={"Authorization": f"Bearer {_SCIM_TOKEN}"},
            )
        assert resp.status_code == 200
        assert resp.json()["displayName"] == "Engineering"

    def test_patch_group_replace_members(self, client: TestClient) -> None:
        """PATCH replace with members array replaces all members."""
        mock_team = MagicMock()
        mock_team.id = _TEAM_ID
        mock_team.organisation_id = _ORG_ID
        mock_team.name = "Engineering"
        mock_team.created_by = _USER_ID
        mock_team.created_at = _NOW
        mock_team.updated_at = _NOW

        new_user_id = uuid.uuid4()
        body = {
            "schemas": ["urn:ietf:params:scim:api:messages:2.0:PatchOp"],
            "Operations": [
                {
                    "op": "replace",
                    "value": {
                        "displayName": "Engineering Renamed",
                        "members": [{"value": str(new_user_id)}],
                    },
                }
            ],
        }
        with (
            patch(
                "modulo.api.routes.scim.scim_get_group",
                return_value=mock_team,
            ),
            patch(
                "modulo.api.routes.scim.scim_update_group",
                return_value=mock_team,
            ),
            patch(
                "modulo.api.routes.scim.scim_get_user",
                return_value=_MOCK_USER,
            ),
            patch(
                "modulo.api.routes.scim.scim_add_group_member",
                return_value=None,
            ),
            patch(
                "modulo.api.routes.scim.scim_list_group_members",
                return_value=[_MOCK_MEMBERSHIP],
            ),
            patch(
                "modulo.api.routes.scim.scim_remove_group_member",
                return_value=True,
            ),
            patch("modulo.api.routes.scim.set_rls_org"),
        ):
            resp = client.patch(
                f"/scim/v2/Groups/{_TEAM_ID}",
                json=body,
                headers={"Authorization": f"Bearer {_SCIM_TOKEN}"},
            )
        assert resp.status_code == 200


# ── ServiceProviderConfig ────────────────────────────────────────────


class TestServiceProviderConfig:
    def test_returns_200(self, client: TestClient) -> None:
        resp = client.get(
            "/scim/v2/ServiceProviderConfig",
            headers={"Authorization": f"Bearer {_SCIM_TOKEN}"},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["schemas"] == ["urn:ietf:params:scim:schemas:core:2.0:ServiceProviderConfig"]
        assert data["patch"]["supported"] is True

    def test_service_provider_config_without_public_url(self, client: TestClient) -> None:
        """ServiceProviderConfig should not crash when modulo_public_url is unset."""
        resp = client.get(
            "/scim/v2/ServiceProviderConfig",
            headers={"Authorization": f"Bearer {_SCIM_TOKEN}"},
        )
        assert resp.status_code == 200


# ── User endpoints ───────────────────────────────────────────────────


class TestListUsers:
    def test_returns_200(self, client: TestClient) -> None:
        with (
            patch("modulo.api.routes.scim.scim_list_users", return_value=_MOCK_USER_LIST),
            patch("modulo.api.routes.scim.set_rls_org"),
        ):
            resp = client.get(
                "/scim/v2/Users",
                headers={"Authorization": f"Bearer {_SCIM_TOKEN}"},
            )
        assert resp.status_code == 200
        data = resp.json()
        assert data["totalResults"] == 1
        assert len(data["Resources"]) == 1
        assert data["Resources"][0]["userName"] == "jane@example.com"

    def test_unauthorized_returns_401(self, unauth_client: TestClient) -> None:
        resp = unauth_client.get("/scim/v2/Users")
        assert resp.status_code == 401


class TestCreateUser:
    def test_returns_201(self, client: TestClient) -> None:
        with (
            patch(
                "modulo.api.routes.scim.scim_create_user",
                return_value=_MOCK_USER,
            ),
            patch(
                "modulo.db.crud.user.get_user_by_email",
                return_value=None,
            ),
            patch("modulo.api.routes.scim.set_rls_org"),
        ):
            resp = client.post(
                "/scim/v2/Users",
                json=_USER_CREATE_BODY,
                headers={"Authorization": f"Bearer {_SCIM_TOKEN}"},
            )
        assert resp.status_code == 201
        data = resp.json()
        assert data["userName"] == "jane@example.com"
        assert data["active"] is True

    def test_duplicate_returns_409(self, client: TestClient) -> None:
        with (
            patch(
                "modulo.db.crud.user.get_user_by_email",
                return_value=_MOCK_USER,
            ),
            patch("modulo.api.routes.scim.set_rls_org"),
        ):
            resp = client.post(
                "/scim/v2/Users",
                json=_USER_CREATE_BODY,
                headers={"Authorization": f"Bearer {_SCIM_TOKEN}"},
            )
        assert resp.status_code == 409


class TestGetUser:
    def test_returns_200(self, client: TestClient) -> None:
        with (
            patch(
                "modulo.api.routes.scim.scim_get_user",
                return_value=_MOCK_USER,
            ),
            patch("modulo.api.routes.scim.set_rls_org"),
        ):
            resp = client.get(
                f"/scim/v2/Users/{_USER_ID}",
                headers={"Authorization": f"Bearer {_SCIM_TOKEN}"},
            )
        assert resp.status_code == 200
        assert resp.json()["id"] == str(_USER_ID)

    def test_not_found_returns_404(self, client: TestClient) -> None:
        with (
            patch("modulo.api.routes.scim.scim_get_user", return_value=None),
            patch("modulo.api.routes.scim.set_rls_org"),
        ):
            resp = client.get(
                f"/scim/v2/Users/{uuid.uuid4()}",
                headers={"Authorization": f"Bearer {_SCIM_TOKEN}"},
            )
        assert resp.status_code == 404


class TestReplaceUser:
    def test_returns_200(self, client: TestClient) -> None:
        with (
            patch(
                "modulo.api.routes.scim.scim_get_user",
                return_value=_MOCK_USER,
            ),
            patch(
                "modulo.api.routes.scim.scim_update_user",
                return_value=_MOCK_USER,
            ),
            patch("modulo.api.routes.scim.set_rls_org"),
        ):
            resp = client.put(
                f"/scim/v2/Users/{_USER_ID}",
                json=_USER_CREATE_BODY,
                headers={"Authorization": f"Bearer {_SCIM_TOKEN}"},
            )
        assert resp.status_code == 200
        assert resp.json()["id"] == str(_USER_ID)

    def test_not_found_returns_404(self, client: TestClient) -> None:
        with (
            patch(
                "modulo.api.routes.scim.scim_get_user",
                return_value=None,
            ),
            patch("modulo.api.routes.scim.set_rls_org"),
        ):
            resp = client.put(
                f"/scim/v2/Users/{uuid.uuid4()}",
                json=_USER_CREATE_BODY,
                headers={"Authorization": f"Bearer {_SCIM_TOKEN}"},
            )
        assert resp.status_code == 404


class TestPatchUser:
    def test_returns_200(self, client: TestClient) -> None:
        mock_user = MagicMock(
            id=_USER_ID,
            organisation_id=_ORG_ID,
            email="jane@example.com",
            display_name="Jane Doe",
            active=True,
            org_role="runner",
            auth_provider="scim",
            created_at=_NOW,
            updated_at=_NOW,
        )
        with (
            patch(
                "modulo.api.routes.scim.scim_get_user",
                return_value=mock_user,
            ),
            patch("modulo.api.routes.scim.set_rls_org"),
        ):
            resp = client.patch(
                f"/scim/v2/Users/{_USER_ID}",
                json=_PATCH_USER_BODY,
                headers={"Authorization": f"Bearer {_SCIM_TOKEN}"},
            )
        assert resp.status_code == 200
        assert mock_user.active is False

    def test_not_found_returns_404(self, client: TestClient) -> None:
        with (
            patch(
                "modulo.api.routes.scim.scim_get_user",
                return_value=None,
            ),
            patch("modulo.api.routes.scim.set_rls_org"),
        ):
            resp = client.patch(
                f"/scim/v2/Users/{uuid.uuid4()}",
                json=_PATCH_USER_BODY,
                headers={"Authorization": f"Bearer {_SCIM_TOKEN}"},
            )
        assert resp.status_code == 404


class TestDeleteUser:
    def test_returns_204(self, client: TestClient) -> None:
        with (
            patch(
                "modulo.api.routes.scim.scim_delete_user_by_id",
                return_value=True,
            ),
            patch("modulo.api.routes.scim.set_rls_org"),
        ):
            resp = client.delete(
                f"/scim/v2/Users/{_USER_ID}",
                headers={"Authorization": f"Bearer {_SCIM_TOKEN}"},
            )
        assert resp.status_code == 204

    def test_not_found_returns_404(self, client: TestClient) -> None:
        with (
            patch(
                "modulo.api.routes.scim.scim_delete_user_by_id",
                return_value=False,
            ),
            patch("modulo.api.routes.scim.set_rls_org"),
        ):
            resp = client.delete(
                f"/scim/v2/Users/{uuid.uuid4()}",
                headers={"Authorization": f"Bearer {_SCIM_TOKEN}"},
            )
        assert resp.status_code == 404


# ── Group endpoints ──────────────────────────────────────────────────


class TestListGroups:
    def test_returns_200(self, client: TestClient) -> None:
        with (
            patch(
                "modulo.api.routes.scim.scim_list_groups",
                return_value=_MOCK_TEAM_LIST,
            ),
            patch(
                "modulo.api.routes.scim.scim_list_group_members",
                return_value=_MOCK_MEMBERSHIPS,
            ),
            patch("modulo.api.routes.scim.set_rls_org"),
        ):
            resp = client.get(
                "/scim/v2/Groups",
                headers={"Authorization": f"Bearer {_SCIM_TOKEN}"},
            )
        assert resp.status_code == 200
        data = resp.json()
        assert data["totalResults"] == 1
        assert data["Resources"][0]["displayName"] == "Engineering"

    def test_unauthorized_returns_401(self, unauth_client: TestClient) -> None:
        resp = unauth_client.get("/scim/v2/Groups")
        assert resp.status_code == 401


class TestCreateGroup:
    def test_returns_201(self, client: TestClient) -> None:
        with (
            patch(
                "modulo.api.routes.scim.scim_create_group",
                return_value=_MOCK_TEAM,
            ),
            patch(
                "modulo.db.crud.team.get_team_by_name",
                return_value=None,
            ),
            patch(
                "modulo.db.crud.user.list_users_for_org",
                return_value=[_MOCK_USER],
            ),
            patch(
                "modulo.api.routes.scim.scim_get_user",
                return_value=_MOCK_USER,
            ),
            patch(
                "modulo.api.routes.scim.scim_add_group_member",
                return_value=None,
            ),
            patch("modulo.api.routes.scim.set_rls_org"),
        ):
            resp = client.post(
                "/scim/v2/Groups",
                json=_GROUP_CREATE_BODY,
                headers={"Authorization": f"Bearer {_SCIM_TOKEN}"},
            )
        assert resp.status_code == 201
        data = resp.json()
        assert data["displayName"] == "Engineering"

    def test_duplicate_returns_409(self, client: TestClient) -> None:
        with (
            patch(
                "modulo.db.crud.team.get_team_by_name",
                return_value=_MOCK_TEAM,
            ),
            patch("modulo.api.routes.scim.set_rls_org"),
        ):
            resp = client.post(
                "/scim/v2/Groups",
                json=_GROUP_CREATE_BODY,
                headers={"Authorization": f"Bearer {_SCIM_TOKEN}"},
            )
        assert resp.status_code == 409


class TestGetGroup:
    def test_returns_200(self, client: TestClient) -> None:
        with (
            patch(
                "modulo.api.routes.scim.scim_get_group",
                return_value=_MOCK_TEAM,
            ),
            patch(
                "modulo.api.routes.scim.scim_list_group_members",
                return_value=_MOCK_MEMBERSHIPS,
            ),
            patch("modulo.api.routes.scim.set_rls_org"),
        ):
            resp = client.get(
                f"/scim/v2/Groups/{_TEAM_ID}",
                headers={"Authorization": f"Bearer {_SCIM_TOKEN}"},
            )
        assert resp.status_code == 200
        assert resp.json()["displayName"] == "Engineering"

    def test_not_found_returns_404(self, client: TestClient) -> None:
        with (
            patch(
                "modulo.api.routes.scim.scim_get_group",
                return_value=None,
            ),
            patch("modulo.api.routes.scim.set_rls_org"),
        ):
            resp = client.get(
                f"/scim/v2/Groups/{uuid.uuid4()}",
                headers={"Authorization": f"Bearer {_SCIM_TOKEN}"},
            )
        assert resp.status_code == 404


class TestReplaceGroup:
    def test_returns_200(self, client: TestClient) -> None:
        with (
            patch(
                "modulo.api.routes.scim.scim_get_group",
                return_value=_MOCK_TEAM,
            ),
            patch(
                "modulo.api.routes.scim.scim_update_group",
                return_value=_MOCK_TEAM,
            ),
            patch(
                "modulo.api.routes.scim.scim_list_group_members",
                return_value=_MOCK_MEMBERSHIPS,
            ),
            patch(
                "modulo.api.routes.scim.scim_get_user",
                return_value=_MOCK_USER,
            ),
            patch(
                "modulo.api.routes.scim.scim_add_group_member",
                return_value=None,
            ),
            patch(
                "modulo.api.routes.scim.scim_remove_group_member",
                return_value=True,
            ),
            patch("modulo.api.routes.scim.set_rls_org"),
        ):
            resp = client.put(
                f"/scim/v2/Groups/{_TEAM_ID}",
                json=_GROUP_CREATE_BODY,
                headers={"Authorization": f"Bearer {_SCIM_TOKEN}"},
            )
        assert resp.status_code == 200
        assert resp.json()["displayName"] == "Engineering"

    def test_not_found_returns_404(self, client: TestClient) -> None:
        with (
            patch(
                "modulo.api.routes.scim.scim_get_group",
                return_value=None,
            ),
            patch("modulo.api.routes.scim.set_rls_org"),
        ):
            resp = client.put(
                f"/scim/v2/Groups/{uuid.uuid4()}",
                json=_GROUP_CREATE_BODY,
                headers={"Authorization": f"Bearer {_SCIM_TOKEN}"},
            )
        assert resp.status_code == 404


class TestPatchGroup:
    def test_add_member_returns_200(self, client: TestClient) -> None:
        mock_team = MagicMock()
        mock_team.id = _TEAM_ID
        mock_team.organisation_id = _ORG_ID
        mock_team.name = "Engineering"
        mock_team.created_by = _USER_ID
        mock_team.created_at = _NOW
        mock_team.updated_at = _NOW
        with (
            patch(
                "modulo.api.routes.scim.scim_get_group",
                return_value=mock_team,
            ),
            patch(
                "modulo.api.routes.scim.scim_get_user",
                return_value=_MOCK_USER,
            ),
            patch(
                "modulo.api.routes.scim.scim_add_group_member",
                return_value=None,
            ),
            patch(
                "modulo.api.routes.scim.scim_list_group_members",
                return_value=_MOCK_MEMBERSHIPS,
            ),
            patch("modulo.api.routes.scim.set_rls_org"),
        ):
            resp = client.patch(
                f"/scim/v2/Groups/{_TEAM_ID}",
                json=_PATCH_GROUP_ADD_MEMBER,
                headers={"Authorization": f"Bearer {_SCIM_TOKEN}"},
            )
        assert resp.status_code == 200
        assert resp.json()["displayName"] == "Engineering"

    def test_not_found_returns_404(self, client: TestClient) -> None:
        with (
            patch(
                "modulo.api.routes.scim.scim_get_group",
                return_value=None,
            ),
            patch("modulo.api.routes.scim.set_rls_org"),
        ):
            resp = client.patch(
                f"/scim/v2/Groups/{uuid.uuid4()}",
                json=_PATCH_GROUP_ADD_MEMBER,
                headers={"Authorization": f"Bearer {_SCIM_TOKEN}"},
            )
        assert resp.status_code == 404


class TestDeleteGroup:
    def test_returns_204(self, client: TestClient) -> None:
        with (
            patch(
                "modulo.api.routes.scim.scim_delete_group_by_id",
                return_value=True,
            ),
            patch("modulo.api.routes.scim.set_rls_org"),
        ):
            resp = client.delete(
                f"/scim/v2/Groups/{_TEAM_ID}",
                headers={"Authorization": f"Bearer {_SCIM_TOKEN}"},
            )
        assert resp.status_code == 204

    def test_not_found_returns_404(self, client: TestClient) -> None:
        with (
            patch(
                "modulo.api.routes.scim.scim_delete_group_by_id",
                return_value=False,
            ),
            patch("modulo.api.routes.scim.set_rls_org"),
        ):
            resp = client.delete(
                f"/scim/v2/Groups/{uuid.uuid4()}",
                headers={"Authorization": f"Bearer {_SCIM_TOKEN}"},
            )
        assert resp.status_code == 404


# ── License gate ─────────────────────────────────────────────────────


class TestLicenseGate:
    def test_no_license_returns_402(self) -> None:
        def _settings_no_license() -> Settings:
            return Settings(
                database_url="postgresql+asyncpg://localhost/test",
                secret_key=_VALID_32,
                fernet_key=_VALID_32,
                modulo_admin_password="testpass",
                modulo_license_key="",
                modulo_scim_token=_SCIM_TOKEN,
                modulo_public_url="http://localhost:8000",
            )

        app.dependency_overrides[get_settings] = _settings_no_license
        app.dependency_overrides[get_db_session] = lambda: _make_mock_session()
        app.dependency_overrides[_get_engine] = lambda: MagicMock()
        app.dependency_overrides[get_scim_principal] = lambda: ScimPrincipal(organisation_id=_ORG_ID)
        resp = TestClient(app).get(
            "/scim/v2/Users",
            headers={"Authorization": f"Bearer {_SCIM_TOKEN}"},
        )
        app.dependency_overrides.clear()
        assert resp.status_code == 402
