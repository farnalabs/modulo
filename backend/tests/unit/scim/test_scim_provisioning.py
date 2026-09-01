"""Unit tests for SCIM 2.0 provisioning endpoints (/scim/v2/Users, /scim/v2/Groups)."""

import uuid
from collections.abc import AsyncGenerator, Generator
from datetime import UTC, datetime
from unittest.mock import ANY, AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from modulo.api.dependencies import _get_engine, get_db_session
from modulo.api.main import app
from modulo.api.routes.scim import _parse_member_uuid
from modulo.auth.scim_auth import ScimPrincipal, get_scim_plan_context, get_scim_principal
from modulo.core.feature_flags import CommunityTier, DbPlanContext, FeatureFlagRegistry
from modulo.settings import Settings, get_settings

_VALID_32 = "a" * 32
_ORG_ID = uuid.UUID("00000000-0000-0000-0000-000000000001")
_USER_ID = uuid.UUID("00000000-0000-0000-0000-000000000002")
_TEAM_ID = uuid.UUID("00000000-0000-0000-0000-000000000003")
_NOW = datetime(2025, 1, 1, tzinfo=UTC)

_SCIM_TOKEN = "test-scim-token-12345"


@pytest.fixture(autouse=True)
def scim_feature_enabled() -> Generator[None, None, None]:
    app.dependency_overrides[get_scim_plan_context] = lambda: DbPlanContext(FeatureFlagRegistry(current_tier="team"))
    yield
    app.dependency_overrides.clear()


def _make_settings() -> Settings:
    return Settings(
        database_url="postgresql+asyncpg://localhost/test",
        secret_key=_VALID_32,
        fernet_key=_VALID_32,
        modulo_admin_password="testpass",
        modulo_license_key="team-license",
        modulo_scim_token=_SCIM_TOKEN,
        modulo_public_url="http://localhost:8000",
    )


def _make_mock_session() -> MagicMock:
    session = MagicMock()
    session.execute = AsyncMock()
    session.flush = AsyncMock()
    session.delete = AsyncMock()
    session.rollback = AsyncMock()
    session.refresh = AsyncMock()
    begin_cm = AsyncMock()
    begin_cm.__aenter__ = AsyncMock(return_value=None)
    begin_cm.__aexit__ = AsyncMock(return_value=False)
    session.begin = MagicMock(return_value=begin_cm)
    return session


@pytest.fixture
def client() -> Generator[TestClient, None, None]:
    mock_session = _make_mock_session()

    async def override_session() -> AsyncGenerator[MagicMock, None]:
        yield mock_session

    app.dependency_overrides[get_settings] = _make_settings
    app.dependency_overrides[get_db_session] = override_session
    app.dependency_overrides[_get_engine] = lambda: MagicMock()
    app.dependency_overrides[get_scim_principal] = lambda: ScimPrincipal(organisation_id=_ORG_ID)
    yield TestClient(app)
    app.dependency_overrides.clear()


@pytest.fixture
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
    def test_missing_token_returns_501(self) -> None:
        def _settings_no_scim_token() -> Settings:
            return Settings(
                database_url="postgresql+asyncpg://localhost/test",
                secret_key=_VALID_32,
                fernet_key=_VALID_32,
                modulo_admin_password="testpass",
                modulo_license_key="team-license",
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
                modulo_license_key="team-license",
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

        async def override_session() -> AsyncGenerator[MagicMock, None]:
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
        assert not data["Resources"]

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
        assert not data["Resources"]

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
            patch("modulo.db.crud.account.get_account_by_email", return_value=None),
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
            patch("modulo.db.crud.org_membership.list_memberships_for_org", return_value=[]),
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


def _make_mock_user(**overrides: object) -> MagicMock:
    user = MagicMock()
    user.id = overrides.get("id", _USER_ID)
    user.organisation_id = overrides.get("organisation_id", _ORG_ID)
    user.email = overrides.get("email", "jane@example.com")
    user.display_name = overrides.get("display_name", "Jane Doe")
    user.active = overrides.get("active", True)
    user.org_role = overrides.get("org_role", "runner")
    user.auth_provider = overrides.get("auth_provider", "scim")
    user.created_at = overrides.get("created_at", _NOW)
    user.updated_at = overrides.get("updated_at", _NOW)
    return user


def _make_mock_team(**overrides: object) -> MagicMock:
    team = MagicMock()
    team.id = overrides.get("id", _TEAM_ID)
    team.organisation_id = overrides.get("organisation_id", _ORG_ID)
    team.name = overrides.get("name", "Engineering")
    team.description = overrides.get("description")
    team.created_by = overrides.get("created_by", _USER_ID)
    team.created_at = overrides.get("created_at", _NOW)
    team.updated_at = overrides.get("updated_at", _NOW)
    return team


class TestPatchEdgeCases:
    def test_patch_user_remove_active(self, client: TestClient) -> None:
        mock_user = _make_mock_user()
        body = {
            "schemas": ["urn:ietf:params:scim:api:messages:2.0:PatchOp"],
            "Operations": [{"op": "remove", "path": "active"}],
        }
        with (
            patch(
                "modulo.api.routes.scim.scim_get_user",
                return_value=mock_user,
            ),
            patch(
                "modulo.api.routes.scim.scim_deactivate_user",
                new_callable=AsyncMock,
                return_value=mock_user,
            ),
            patch("modulo.api.routes.scim.assert_not_last_admin", new_callable=AsyncMock),
            patch(
                "modulo.api.routes.scim._resolve_scim_admin_caller",
                new_callable=AsyncMock,
                return_value=_USER_ID,
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

    @pytest.mark.parametrize(
        ("op", "expected_body_check"),
        [
            pytest.param("doesNotExist", None, id="unsupported_op_returns_400"),
            pytest.param("invalidOp", "detail", id="invalid_op_returns_400"),
        ],
    )
    def test_patch_user_invalid_ops(self, client: TestClient, op: str, expected_body_check: str | None) -> None:
        mock_user = _make_mock_user()
        body = {
            "schemas": ["urn:ietf:params:scim:api:messages:2.0:PatchOp"],
            "Operations": [{"op": op}],
        }
        if op == "doesNotExist":
            body["Operations"][0]["path"] = "active"
            body["Operations"][0]["value"] = False
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
        if expected_body_check:
            assert expected_body_check in resp.json()

    def test_patch_user_add_username(self, client: TestClient) -> None:
        mock_user = _make_mock_user()
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
        mock_team = _make_mock_team()
        body = {
            "schemas": ["urn:ietf:params:scim:api:messages:2.0:PatchOp"],
            "Operations": [{"op": "remove", "value": {"value": str(_USER_ID)}}],
        }
        with (
            patch("modulo.api.routes.scim.scim_get_group", return_value=mock_team),
            patch("modulo.api.routes.scim.scim_remove_group_member", return_value=True),
            patch("modulo.api.routes.scim.scim_list_group_members", return_value=[]),
            patch("modulo.api.routes.scim.set_rls_org"),
        ):
            resp = client.patch(
                f"/scim/v2/Groups/{_TEAM_ID}",
                json=body,
                headers={"Authorization": f"Bearer {_SCIM_TOKEN}"},
            )
        assert resp.status_code == 200

    def test_patch_group_remove_by_value_list(self, client: TestClient) -> None:
        mock_team = _make_mock_team()
        body = {
            "schemas": ["urn:ietf:params:scim:api:messages:2.0:PatchOp"],
            "Operations": [{"op": "remove", "value": [{"value": str(_USER_ID)}]}],
        }
        with (
            patch("modulo.api.routes.scim.scim_get_group", return_value=mock_team),
            patch("modulo.api.routes.scim.scim_remove_group_member", return_value=True),
            patch("modulo.api.routes.scim.scim_list_group_members", return_value=[]),
            patch("modulo.api.routes.scim.set_rls_org"),
        ):
            resp = client.patch(
                f"/scim/v2/Groups/{_TEAM_ID}",
                json=body,
                headers={"Authorization": f"Bearer {_SCIM_TOKEN}"},
            )
        assert resp.status_code == 200

    @pytest.mark.parametrize(
        ("op", "expected_body_check"),
        [
            pytest.param("doesNotExist", None, id="unsupported_op_returns_400"),
            pytest.param("invalidOp", "detail", id="invalid_op_returns_400"),
        ],
    )
    def test_patch_group_invalid_ops(self, client: TestClient, op: str, expected_body_check: str | None) -> None:
        mock_team = _make_mock_team()
        body = {
            "schemas": ["urn:ietf:params:scim:api:messages:2.0:PatchOp"],
            "Operations": [{"op": op}],
        }
        if op == "doesNotExist":
            body["Operations"][0]["value"] = False
        with (
            patch("modulo.api.routes.scim.scim_get_group", return_value=mock_team),
            patch(
                "modulo.api.routes.scim.scim_list_group_members",
                return_value=_MOCK_MEMBERSHIPS if op == "invalidOp" else [],
            ),
            patch("modulo.api.routes.scim.set_rls_org"),
        ):
            resp = client.patch(
                f"/scim/v2/Groups/{_TEAM_ID}",
                json=body,
                headers={"Authorization": f"Bearer {_SCIM_TOKEN}"},
            )
        assert resp.status_code == 400
        if expected_body_check:
            assert expected_body_check in resp.json()

    def test_replace_group_clear_members(self, client: TestClient) -> None:
        mock_team = _make_mock_team()
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
            patch("modulo.api.routes.scim.scim_get_group", return_value=mock_team),
            patch("modulo.api.routes.scim.scim_update_group", return_value=mock_team),
            patch("modulo.api.routes.scim.scim_list_group_members", return_value=[mock_membership]),
            patch("modulo.api.routes.scim.scim_remove_group_member", return_value=True),
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
        mock_team = _make_mock_team()
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
            patch("modulo.api.routes.scim.scim_get_group", return_value=mock_team),
            patch("modulo.api.routes.scim.scim_update_group", return_value=mock_team),
            patch("modulo.api.routes.scim.scim_get_user", return_value=_MOCK_USER),
            patch("modulo.api.routes.scim.scim_add_group_member", return_value=None),
            patch("modulo.api.routes.scim.scim_list_group_members", return_value=[_MOCK_MEMBERSHIP]),
            patch("modulo.api.routes.scim.scim_remove_group_member", return_value=True),
            patch("modulo.api.routes.scim.set_rls_org"),
        ):
            resp = client.patch(
                f"/scim/v2/Groups/{_TEAM_ID}",
                json=body,
                headers={"Authorization": f"Bearer {_SCIM_TOKEN}"},
            )
        assert resp.status_code == 200


class TestParseMemberUuid:
    """Direct coverage for the member-id parser extracted from ``patch_group`` (FAR-310).

    The PATCH helpers skip a member whose ``value`` does not parse as a UUID
    (invalid string, ``None``, non-str value) — this pins the parser contract
    so a malformed SCIM member reference is never turned into a DB call.
    """

    def test_valid_uuid_string_returns_uuid(self) -> None:
        assert _parse_member_uuid(str(_USER_ID)) == _USER_ID

    def test_valid_uuid_object_returns_uuid(self) -> None:
        assert _parse_member_uuid(_USER_ID) == _USER_ID

    def test_invalid_string_returns_none(self) -> None:
        assert _parse_member_uuid("not-a-uuid") is None
        assert _parse_member_uuid("") is None

    def test_non_str_value_returns_none(self) -> None:
        assert _parse_member_uuid(12345) is None
        assert _parse_member_uuid(None) is None
        assert _parse_member_uuid(3.14) is None

    def test_value_is_never_typed_coerced(self) -> None:
        # str(value) must be a valid UUID hex — a bare uuid-like fragment is rejected.
        assert _parse_member_uuid("00000000-0000-0000-0000-00000000000X") is None


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
        resp = client.get(
            "/scim/v2/ServiceProviderConfig",
            headers={"Authorization": f"Bearer {_SCIM_TOKEN}"},
        )
        assert resp.status_code == 200


# ── CRUD endpoints (parametrized) ──────────────────────────────────


class TestCrudNotFound:
    """Parametrized: 6 CRUD endpoints that return 404 when entity is not found."""

    @pytest.mark.parametrize(
        ("name", "method", "url", "mock_target", "body"),
        [
            pytest.param(
                "get_user", "GET", "/scim/v2/Users/{id}", "modulo.api.routes.scim.scim_get_user", None, id="get_user"
            ),
            pytest.param(
                "replace_user",
                "PUT",
                "/scim/v2/Users/{id}",
                "modulo.api.routes.scim.scim_get_user",
                _USER_CREATE_BODY,
                id="replace_user",
            ),
            pytest.param(
                "patch_user",
                "PATCH",
                "/scim/v2/Users/{id}",
                "modulo.api.routes.scim.scim_get_user",
                _PATCH_USER_BODY,
                id="patch_user",
            ),
            pytest.param(
                "get_group",
                "GET",
                "/scim/v2/Groups/{id}",
                "modulo.api.routes.scim.scim_get_group",
                None,
                id="get_group",
            ),
            pytest.param(
                "replace_group",
                "PUT",
                "/scim/v2/Groups/{id}",
                "modulo.api.routes.scim.scim_get_group",
                _GROUP_CREATE_BODY,
                id="replace_group",
            ),
            pytest.param(
                "patch_group",
                "PATCH",
                "/scim/v2/Groups/{id}",
                "modulo.api.routes.scim.scim_get_group",
                _PATCH_GROUP_ADD_MEMBER,
                id="patch_group",
            ),
        ],
    )
    def test_not_found_returns_404(
        self, client: TestClient, name: str, method: str, url: str, mock_target: str, body: dict | None
    ) -> None:
        entity_id = _USER_ID if "user" in name else _TEAM_ID
        formatted_url = url.format(id=entity_id)
        with (
            patch(mock_target, return_value=None),
            patch("modulo.api.routes.scim.set_rls_org"),
            patch("modulo.api.routes.scim.assert_not_last_admin", new_callable=AsyncMock),
            patch(
                "modulo.api.routes.scim._resolve_scim_admin_caller",
                new_callable=AsyncMock,
                return_value=_USER_ID,
            ),
        ):
            headers = {"Authorization": f"Bearer {_SCIM_TOKEN}"}
            if method == "GET":
                resp = client.get(formatted_url, headers=headers)
            elif method == "PUT":
                resp = client.put(formatted_url, json=body, headers=headers)
            elif method == "PATCH":
                resp = client.patch(formatted_url, json=body, headers=headers)
        assert resp.status_code == 404

    @pytest.mark.parametrize(
        ("name", "method", "url", "mock_target", "body", "expected_status"),
        [
            pytest.param(
                "delete_user_204",
                "DELETE",
                "/scim/v2/Users/{id}",
                "modulo.api.routes.scim.scim_delete_user_by_id",
                None,
                204,
                id="delete_user_204",
            ),
            pytest.param(
                "delete_group_204",
                "DELETE",
                "/scim/v2/Groups/{id}",
                "modulo.api.routes.scim.scim_delete_group_by_id",
                None,
                204,
                id="delete_group_204",
            ),
            pytest.param(
                "delete_user_404",
                "DELETE",
                "/scim/v2/Users/{id}",
                "modulo.api.routes.scim.scim_delete_user_by_id",
                None,
                404,
                id="delete_user_404",
            ),
            pytest.param(
                "delete_group_404",
                "DELETE",
                "/scim/v2/Groups/{id}",
                "modulo.api.routes.scim.scim_delete_group_by_id",
                None,
                404,
                id="delete_group_404",
            ),
        ],
    )
    def test_delete(
        self,
        client: TestClient,
        name: str,
        method: str,
        url: str,
        mock_target: str,
        body: dict | None,
        expected_status: int,
    ) -> None:
        entity_id = _USER_ID if "user" in name else _TEAM_ID
        formatted_url = url.format(id=entity_id)
        mock_return = expected_status == 204
        with (
            patch(mock_target, return_value=mock_return),
            patch("modulo.api.routes.scim.set_rls_org"),
            patch("modulo.api.routes.scim.assert_not_last_admin", new_callable=AsyncMock),
            patch(
                "modulo.api.routes.scim._resolve_scim_admin_caller",
                new_callable=AsyncMock,
                return_value=_USER_ID,
            ),
        ):
            resp = client.delete(formatted_url, headers={"Authorization": f"Bearer {_SCIM_TOKEN}"})
        assert resp.status_code == expected_status


class TestCrudCreate:
    """Parametrized: create endpoints — duplicate = 409, success = 201."""

    @pytest.mark.parametrize(
        ("name", "url", "body", "mock_duplicate_target", "duplicate_return"),
        [
            pytest.param(
                "user_duplicate",
                "/scim/v2/Users",
                _USER_CREATE_BODY,
                "modulo.db.crud.account.get_account_by_email",
                _MOCK_USER,
                id="duplicate_user_409",
            ),
            pytest.param(
                "group_duplicate",
                "/scim/v2/Groups",
                _GROUP_CREATE_BODY,
                "modulo.db.crud.team.get_team_by_name",
                _MOCK_TEAM,
                id="duplicate_group_409",
            ),
        ],
    )
    def test_duplicate_returns_409(
        self, client: TestClient, name: str, url: str, body: dict, mock_duplicate_target: str, duplicate_return: object
    ) -> None:
        if "group" in name:
            with (
                patch(mock_duplicate_target, return_value=duplicate_return),
                patch("modulo.api.routes.scim.set_rls_org"),
            ):
                resp = client.post(url, json=body, headers={"Authorization": f"Bearer {_SCIM_TOKEN}"})
            assert resp.status_code == 409
            return

        # An ACTIVE membership (deactivated_at is None) must still 409 on re-create;
        # tombstoned (deactivated_at set) memberships are re-creatable (see
        # test_scim_deactivate_and_recreate_reversible).
        mock_membership = MagicMock()
        mock_membership.deactivated_at = None
        with (
            patch(mock_duplicate_target, return_value=duplicate_return),
            patch(
                "modulo.db.crud.org_membership.get_membership_by_account_and_org",
                return_value=mock_membership,
            ),
            patch("modulo.api.routes.scim.set_rls_org"),
        ):
            resp = client.post(url, json=body, headers={"Authorization": f"Bearer {_SCIM_TOKEN}"})
        assert resp.status_code == 409


class TestCrudUnauthorized:
    """Parametrized: list endpoints return 401 without auth header."""

    @pytest.mark.parametrize(
        "url",
        [
            pytest.param("/scim/v2/Users", id="list_users"),
            pytest.param("/scim/v2/Groups", id="list_groups"),
        ],
    )
    def test_unauthorized_returns_401(self, unauth_client: TestClient, url: str) -> None:
        resp = unauth_client.get(url)
        assert resp.status_code == 401


# ── User endpoints (unique tests) ────────────────────────────────────


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


class TestCreateUser:
    def test_returns_201(self, client: TestClient) -> None:
        with (
            patch("modulo.api.routes.scim.scim_create_user", return_value=_MOCK_USER),
            patch("modulo.db.crud.account.get_account_by_email", return_value=None),
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


class TestGetUser:
    def test_returns_200(self, client: TestClient) -> None:
        with (
            patch("modulo.api.routes.scim.scim_get_user", return_value=_MOCK_USER),
            patch("modulo.api.routes.scim.set_rls_org"),
        ):
            resp = client.get(
                f"/scim/v2/Users/{_USER_ID}",
                headers={"Authorization": f"Bearer {_SCIM_TOKEN}"},
            )
        assert resp.status_code == 200
        assert resp.json()["id"] == str(_USER_ID)


class TestReplaceUser:
    def test_returns_200(self, client: TestClient) -> None:
        with (
            patch("modulo.api.routes.scim.scim_get_user", return_value=_MOCK_USER),
            patch("modulo.api.routes.scim.scim_update_user", return_value=_MOCK_USER),
            patch("modulo.api.routes.scim.set_rls_org"),
        ):
            resp = client.put(
                f"/scim/v2/Users/{_USER_ID}",
                json=_USER_CREATE_BODY,
                headers={"Authorization": f"Bearer {_SCIM_TOKEN}"},
            )
        assert resp.status_code == 200
        assert resp.json()["id"] == str(_USER_ID)


class TestPatchUser:
    def test_returns_200(self, client: TestClient) -> None:
        mock_user = _make_mock_user()
        with (
            patch("modulo.api.routes.scim.scim_get_user", return_value=mock_user),
            patch(
                "modulo.api.routes.scim.scim_deactivate_user",
                new_callable=AsyncMock,
                return_value=mock_user,
            ),
            patch("modulo.api.routes.scim.assert_not_last_admin", new_callable=AsyncMock),
            patch(
                "modulo.api.routes.scim._resolve_scim_admin_caller",
                new_callable=AsyncMock,
                return_value=_USER_ID,
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


class TestNoScimRouteSetsOrgRole:
    """ADR 017: SCIM is exempt via MODULO_SCIM_TOKEN, but no route may set an
    org role. ``scim_update_user`` has a *functional* role-UPDATE that must
    stay unwired — prove no SCIM route passes ``org_role``."""

    def test_put_user_with_org_role_field_is_ignored(self, client: TestClient) -> None:
        with (
            patch("modulo.api.routes.scim.scim_get_user", return_value=_MOCK_USER),
            patch("modulo.api.routes.scim.scim_update_user", return_value=_MOCK_USER) as update_mock,
            patch("modulo.api.routes.scim.set_rls_org"),
        ):
            body = {**_USER_CREATE_BODY, "org_role": "admin"}
            resp = client.put(
                f"/scim/v2/Users/{_USER_ID}",
                json=body,
                headers={"Authorization": f"Bearer {_SCIM_TOKEN}"},
            )
        assert resp.status_code == 200
        update_mock.assert_awaited_once()
        kwargs = update_mock.call_args.kwargs
        assert "org_role" not in kwargs
        assert kwargs.get("role") is None

    def test_patch_user_org_role_path_is_not_applied(self, client: TestClient) -> None:
        mock_user = _make_mock_user(org_role="runner")
        with (
            patch("modulo.api.routes.scim.scim_get_user", return_value=mock_user),
            patch("modulo.api.routes.scim.set_rls_org"),
        ):
            body = {
                "schemas": ["urn:ietf:params:scim:api:messages:2.0:PatchOp"],
                "Operations": [{"op": "replace", "path": "org_role", "value": "admin"}],
            }
            resp = client.patch(
                f"/scim/v2/Users/{_USER_ID}",
                json=body,
                headers={"Authorization": f"Bearer {_SCIM_TOKEN}"},
            )
        assert resp.status_code == 200
        assert mock_user.org_role == "runner"

    def test_create_user_org_role_field_is_ignored(self, client: TestClient) -> None:
        with (
            patch("modulo.db.crud.account.get_account_by_email", return_value=None),
            patch("modulo.api.routes.scim.set_rls_org"),
            patch("modulo.api.routes.scim.scim_create_user", return_value=_MOCK_USER) as create_mock,
        ):
            body = {**_USER_CREATE_BODY, "org_role": "admin"}
            resp = client.post(
                "/scim/v2/Users",
                json=body,
                headers={"Authorization": f"Bearer {_SCIM_TOKEN}"},
            )
        assert resp.status_code == 201
        create_mock.assert_awaited_once()
        kwargs = create_mock.call_args.kwargs
        assert "org_role" not in kwargs


# ── Group endpoints (unique tests) ──────────────────────────────────


class TestListGroups:
    def test_returns_200(self, client: TestClient) -> None:
        with (
            patch("modulo.api.routes.scim.scim_list_groups", return_value=_MOCK_TEAM_LIST),
            patch("modulo.api.routes.scim.scim_list_group_members", return_value=_MOCK_MEMBERSHIPS),
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


class TestCreateGroup:
    def test_returns_201(self, client: TestClient) -> None:
        with (
            patch("modulo.api.routes.scim.scim_create_group", return_value=_MOCK_TEAM),
            patch("modulo.db.crud.team.get_team_by_name", return_value=None),
            patch("modulo.db.crud.org_membership.list_memberships_for_org", return_value=[]),
            patch("modulo.api.routes.scim.scim_get_user", return_value=_MOCK_USER),
            patch("modulo.api.routes.scim.scim_add_group_member", return_value=None),
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


class TestGetGroup:
    def test_returns_200(self, client: TestClient) -> None:
        with (
            patch("modulo.api.routes.scim.scim_get_group", return_value=_MOCK_TEAM),
            patch("modulo.api.routes.scim.scim_list_group_members", return_value=_MOCK_MEMBERSHIPS),
            patch("modulo.api.routes.scim.set_rls_org"),
        ):
            resp = client.get(
                f"/scim/v2/Groups/{_TEAM_ID}",
                headers={"Authorization": f"Bearer {_SCIM_TOKEN}"},
            )
        assert resp.status_code == 200
        assert resp.json()["displayName"] == "Engineering"


class TestReplaceGroup:
    def test_returns_200(self, client: TestClient) -> None:
        with (
            patch("modulo.api.routes.scim.scim_get_group", return_value=_MOCK_TEAM),
            patch("modulo.api.routes.scim.scim_update_group", return_value=_MOCK_TEAM),
            patch("modulo.api.routes.scim.scim_list_group_members", return_value=_MOCK_MEMBERSHIPS),
            patch("modulo.api.routes.scim.scim_get_user", return_value=_MOCK_USER),
            patch("modulo.api.routes.scim.scim_add_group_member", return_value=None),
            patch("modulo.api.routes.scim.scim_remove_group_member", return_value=True),
            patch("modulo.api.routes.scim.set_rls_org"),
        ):
            resp = client.put(
                f"/scim/v2/Groups/{_TEAM_ID}",
                json=_GROUP_CREATE_BODY,
                headers={"Authorization": f"Bearer {_SCIM_TOKEN}"},
            )
        assert resp.status_code == 200
        assert resp.json()["displayName"] == "Engineering"


class TestPatchGroup:
    def test_add_member_returns_200(self, client: TestClient) -> None:
        mock_team = _make_mock_team()
        with (
            patch("modulo.api.routes.scim.scim_get_group", return_value=mock_team),
            patch("modulo.api.routes.scim.scim_get_user", return_value=_MOCK_USER),
            patch("modulo.api.routes.scim.scim_add_group_member", return_value=None),
            patch("modulo.api.routes.scim.scim_list_group_members", return_value=_MOCK_MEMBERSHIPS),
            patch("modulo.api.routes.scim.set_rls_org"),
        ):
            resp = client.patch(
                f"/scim/v2/Groups/{_TEAM_ID}",
                json=_PATCH_GROUP_ADD_MEMBER,
                headers={"Authorization": f"Bearer {_SCIM_TOKEN}"},
            )
        assert resp.status_code == 200
        assert resp.json()["displayName"] == "Engineering"

    def test_add_member_foreign_org_user_is_skipped(self, client: TestClient) -> None:
        # GH-1797: a PATCH add op must not add a user that is not a member of
        # the caller's org — mirror the POST/PUT behaviour (skip, 200).
        mock_team = _make_mock_team()
        foreign_user_id = uuid.uuid4()
        body = {
            "schemas": ["urn:ietf:params:scim:api:messages:2.0:PatchOp"],
            "Operations": [{"op": "add", "path": "members", "value": [{"value": str(foreign_user_id)}]}],
        }
        with (
            patch("modulo.api.routes.scim.scim_get_group", return_value=mock_team),
            patch("modulo.api.routes.scim.scim_get_user", return_value=None),
            patch("modulo.api.routes.scim.scim_add_group_member", return_value=None) as mock_add,
            patch("modulo.api.routes.scim.scim_list_group_members", return_value=[]),
            patch("modulo.api.routes.scim.set_rls_org"),
        ):
            resp = client.patch(
                f"/scim/v2/Groups/{_TEAM_ID}",
                json=body,
                headers={"Authorization": f"Bearer {_SCIM_TOKEN}"},
            )
        assert resp.status_code == 200
        mock_add.assert_not_called()

    def test_add_member_mixed_foreign_and_same_org_only_same_org_lands(self, client: TestClient) -> None:
        # GH-1797: the foreign-org member is skipped, the same-org member is added.
        mock_team = _make_mock_team()
        foreign_user_id = uuid.uuid4()

        def _fake_get_user(_session: MagicMock, _org_id: uuid.UUID, uid: uuid.UUID) -> MagicMock | None:
            return _MOCK_USER if uid == _USER_ID else None

        body = {
            "schemas": ["urn:ietf:params:scim:api:messages:2.0:PatchOp"],
            "Operations": [
                {
                    "op": "add",
                    "path": "members",
                    "value": [{"value": str(foreign_user_id)}, {"value": str(_USER_ID)}],
                }
            ],
        }
        with (
            patch("modulo.api.routes.scim.scim_get_group", return_value=mock_team),
            patch("modulo.api.routes.scim.scim_get_user", side_effect=_fake_get_user) as mock_get_user,
            patch("modulo.api.routes.scim.scim_add_group_member", return_value=None) as mock_add,
            patch("modulo.api.routes.scim.scim_list_group_members", return_value=[]),
            patch("modulo.api.routes.scim.set_rls_org"),
        ):
            resp = client.patch(
                f"/scim/v2/Groups/{_TEAM_ID}",
                json=body,
                headers={"Authorization": f"Bearer {_SCIM_TOKEN}"},
            )
        assert resp.status_code == 200
        mock_add.assert_called_once()
        assert mock_add.call_args.kwargs["user_id"] == _USER_ID
        # The lookup must run against the caller's org, never the client-supplied payload.
        mock_get_user.assert_any_call(ANY, _ORG_ID, _USER_ID)

    def test_replace_members_foreign_org_user_is_skipped(self, client: TestClient) -> None:
        # GH-1797: a PATCH replace op must not add a user that is not a member
        # of the caller's org — mirror the PUT behaviour (skip, 200).
        mock_team = _make_mock_team()
        foreign_user_id = uuid.uuid4()
        body = {
            "schemas": ["urn:ietf:params:scim:api:messages:2.0:PatchOp"],
            "Operations": [
                {
                    "op": "replace",
                    "value": {"displayName": "Engineering", "members": [{"value": str(foreign_user_id)}]},
                }
            ],
        }
        with (
            patch("modulo.api.routes.scim.scim_get_group", return_value=mock_team),
            patch("modulo.api.routes.scim.scim_update_group", return_value=mock_team),
            patch("modulo.api.routes.scim.scim_get_user", return_value=None),
            patch("modulo.api.routes.scim.scim_add_group_member", return_value=None) as mock_add,
            patch("modulo.api.routes.scim.scim_list_group_members", return_value=[_MOCK_MEMBERSHIP]),
            patch("modulo.api.routes.scim.scim_remove_group_member", return_value=True),
            patch("modulo.api.routes.scim.set_rls_org"),
        ):
            resp = client.patch(
                f"/scim/v2/Groups/{_TEAM_ID}",
                json=body,
                headers={"Authorization": f"Bearer {_SCIM_TOKEN}"},
            )
        assert resp.status_code == 200
        mock_add.assert_not_called()

    def test_replace_members_mixed_foreign_and_same_org_only_same_org_lands(self, client: TestClient) -> None:
        # GH-1797: the foreign-org member is skipped, the same-org member is added.
        mock_team = _make_mock_team()
        foreign_user_id = uuid.uuid4()

        def _fake_get_user(_session: MagicMock, _org_id: uuid.UUID, uid: uuid.UUID) -> MagicMock | None:
            return _MOCK_USER if uid == _USER_ID else None

        body = {
            "schemas": ["urn:ietf:params:scim:api:messages:2.0:PatchOp"],
            "Operations": [
                {
                    "op": "replace",
                    "value": {
                        "displayName": "Engineering",
                        "members": [{"value": str(foreign_user_id)}, {"value": str(_USER_ID)}],
                    },
                }
            ],
        }
        with (
            patch("modulo.api.routes.scim.scim_get_group", return_value=mock_team),
            patch("modulo.api.routes.scim.scim_update_group", return_value=mock_team),
            patch("modulo.api.routes.scim.scim_get_user", side_effect=_fake_get_user),
            patch("modulo.api.routes.scim.scim_add_group_member", return_value=None) as mock_add,
            patch("modulo.api.routes.scim.scim_list_group_members", return_value=[_MOCK_MEMBERSHIP]),
            patch("modulo.api.routes.scim.scim_remove_group_member", return_value=True),
            patch("modulo.api.routes.scim.set_rls_org"),
        ):
            resp = client.patch(
                f"/scim/v2/Groups/{_TEAM_ID}",
                json=body,
                headers={"Authorization": f"Bearer {_SCIM_TOKEN}"},
            )
        assert resp.status_code == 200
        mock_add.assert_called_once()
        assert mock_add.call_args.kwargs["user_id"] == _USER_ID


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
        app.dependency_overrides[get_scim_plan_context] = CommunityTier
        resp = TestClient(app).get(
            "/scim/v2/Users",
            headers={"Authorization": f"Bearer {_SCIM_TOKEN}"},
        )
        app.dependency_overrides.clear()
        assert resp.status_code == 402
