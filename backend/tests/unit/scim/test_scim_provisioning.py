"""Unit tests for SCIM 2.0 provisioning endpoints (/scim/v2/Users, /scim/v2/Groups)."""

import uuid
from collections.abc import Callable, Generator
from unittest.mock import ANY, AsyncMock, MagicMock, patch

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from farna.api.main import app
from farna.api.routes.scim import _MSG_NO_ACTIVE_ADMIN_EXISTS, _parse_member_uuid, get_scim_audit_principal
from farna.auth.jwt import TenantPrincipal
from farna.auth.scim_auth import ScimPrincipal, get_scim_plan_context
from farna.core.feature_flags import CommunityTier, DbPlanContext, FeatureFlagRegistry
from farna.settings import get_settings
from tests.unit.api.mock_session import configure_mock_session
from tests.unit.scim.helpers import (
    AUTH_HEADERS,
    GROUP_CREATE_BODY,
    ORG_ID,
    PATCH_GROUP_ADD_MEMBER,
    PATCH_USER_BODY,
    TEAM_ID,
    USER_CREATE_BODY,
    USER_ID,
    make_membership,
    make_mock_session,
    make_settings,
    make_team,
    make_user,
    only_org_owns,
)


@pytest.fixture(autouse=True)
def scim_feature_enabled() -> Generator[None, None, None]:
    app.dependency_overrides[get_scim_plan_context] = lambda: DbPlanContext(FeatureFlagRegistry(current_tier="team"))
    yield
    app.dependency_overrides.clear()


@pytest.fixture
def client(scim_client: Callable[..., TestClient]) -> TestClient:
    return scim_client()


@pytest.fixture
def unauth_client() -> Generator[TestClient, None, None]:
    # A zero-arg wrapper: ``make_settings`` takes ``**overrides``, which FastAPI
    # would otherwise expose as a required query parameter on the routes.
    app.dependency_overrides[get_settings] = lambda: make_settings()
    yield TestClient(app)
    app.dependency_overrides.clear()


_MOCK_USER = make_user()

_MOCK_USER_LIST = ([_MOCK_USER], 1)

_MOCK_TEAM = make_team()

_MOCK_TEAM_LIST = ([_MOCK_TEAM], 1)

_MOCK_MEMBERSHIP = make_membership()

_MOCK_MEMBERSHIPS = [_MOCK_MEMBERSHIP]


# ── Auth Edge Cases ──────────────────────────────────────────────────


class TestAuthEdgeCases:
    def test_missing_token_returns_501(self, scim_client: Callable[..., TestClient]) -> None:
        client = scim_client(settings=lambda: make_settings(modulo_scim_token=""), principal=False)
        resp = client.get(
            "/scim/v2/ServiceProviderConfig",
            headers={"Authorization": "Bearer some-token"},
        )
        assert resp.status_code == 501

    def test_invalid_token_returns_401(self, scim_client: Callable[..., TestClient]) -> None:
        client = scim_client(principal=False)
        resp = client.get(
            "/scim/v2/ServiceProviderConfig",
            headers={"Authorization": "Bearer wrong-token"},
        )
        assert resp.status_code == 401

    def test_invalid_default_org_uuid_returns_500(self, scim_client: Callable[..., TestClient]) -> None:
        client = scim_client(settings=lambda: make_settings(modulo_scim_default_org_id="not-a-uuid"), principal=False)
        resp = client.get("/scim/v2/Users", headers=AUTH_HEADERS)
        assert resp.status_code == 500

    def test_no_org_in_db_returns_500(self, scim_client: Callable[..., TestClient]) -> None:
        session = make_mock_session()
        session.execute = AsyncMock(return_value=AsyncMock(scalar_one_or_none=MagicMock(return_value=None)))

        client = scim_client(session=session, principal=False)
        resp = client.get("/scim/v2/Users", headers=AUTH_HEADERS)
        assert resp.status_code == 500


# ── Pagination / Filter Edge Cases ───────────────────────────────────


class TestPaginationEdgeCases:
    def test_count_exceeds_max_returns_422(self, client: TestClient) -> None:
        resp = client.get(
            "/scim/v2/Users?count=200",
            headers=AUTH_HEADERS,
        )
        assert resp.status_code == 422

    def test_count_zero_returns_422(self, client: TestClient) -> None:
        resp = client.get(
            "/scim/v2/Users?count=0",
            headers=AUTH_HEADERS,
        )
        assert resp.status_code == 422

    def test_filter_by_email_returns_matching(self, client: TestClient) -> None:
        user_a = make_user(id=uuid.uuid4(), email="alice@example.com", display_name="Alice")

        with (
            patch(
                "farna.api.routes.scim.scim_list_users",
                return_value=([user_a], 1),
            ),
            patch("farna.api.routes.scim.set_rls_org"),
        ):
            resp = client.get(
                "/scim/v2/Users?filter=alice",
                headers=AUTH_HEADERS,
            )
        assert resp.status_code == 200
        data = resp.json()
        assert data["totalResults"] == 1
        assert data["Resources"][0]["userName"] == "alice@example.com"

    def test_filter_no_match_returns_empty(self, client: TestClient) -> None:
        with (
            patch(
                "farna.api.routes.scim.scim_list_users",
                return_value=([], 0),
            ),
            patch("farna.api.routes.scim.set_rls_org"),
        ):
            resp = client.get(
                "/scim/v2/Users?filter=zzzzzzzzz",
                headers=AUTH_HEADERS,
            )
        assert resp.status_code == 200
        data = resp.json()
        assert data["totalResults"] == 0
        assert not data["Resources"]

    def test_second_page_returns_empty(self, client: TestClient) -> None:
        with (
            patch(
                "farna.api.routes.scim.scim_list_users",
                return_value=([], 1),
            ),
            patch("farna.api.routes.scim.set_rls_org"),
        ):
            resp = client.get(
                "/scim/v2/Users?startIndex=2&count=20",
                headers=AUTH_HEADERS,
            )
        assert resp.status_code == 200
        data = resp.json()
        assert data["totalResults"] == 1
        assert not data["Resources"]

    def test_groups_filter_no_match(self, client: TestClient) -> None:
        with (
            patch(
                "farna.api.routes.scim.scim_list_groups",
                return_value=([], 0),
            ),
            patch("farna.api.routes.scim.set_rls_org"),
            patch(
                "farna.api.routes.scim.scim_list_group_members",
                return_value=[],
            ),
        ):
            resp = client.get(
                "/scim/v2/Groups?filter=nonexistent",
                headers=AUTH_HEADERS,
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
            headers=AUTH_HEADERS,
        )
        assert resp.status_code == 422

    def test_create_user_invalid_schemas_returns_422(self, client: TestClient) -> None:
        body = {**USER_CREATE_BODY, "schemas": ["urn:ietf:params:scim:schemas:core:2.0:Group"]}
        with (
            patch("farna.db.crud.account.get_account_by_email", return_value=None),
            patch("farna.api.routes.scim.set_rls_org"),
            patch(
                "farna.api.routes.scim.scim_create_user",
                return_value=_MOCK_USER,
            ),
        ):
            resp = client.post(
                "/scim/v2/Users",
                json=body,
                headers=AUTH_HEADERS,
            )
        assert resp.status_code == 201

    def test_create_group_missing_displayname_returns_422(self, client: TestClient) -> None:
        body = {"schemas": ["urn:ietf:params:scim:schemas:core:2.0:Group"]}
        resp = client.post(
            "/scim/v2/Groups",
            json=body,
            headers=AUTH_HEADERS,
        )
        assert resp.status_code == 422

    def test_create_group_invalid_member_ref_is_skipped(self, client: TestClient) -> None:
        body = {**GROUP_CREATE_BODY, "members": [{"value": "not-a-uuid", "type": "User"}]}
        with (
            patch("farna.db.crud.team.get_team_by_name", return_value=None),
            patch("farna.db.crud.org_membership.list_memberships_for_org", return_value=[]),
            patch("farna.api.routes.scim.scim_create_group", return_value=_MOCK_TEAM),
            patch("farna.api.routes.scim.set_rls_org"),
        ):
            resp = client.post(
                "/scim/v2/Groups",
                json=body,
                headers=AUTH_HEADERS,
            )
        assert resp.status_code == 201


# ── PATCH Edge Cases ─────────────────────────────────────────────────


class TestPatchEdgeCases:
    def test_patch_user_remove_active(self, client: TestClient) -> None:
        mock_user = make_user()
        body = {
            "schemas": ["urn:ietf:params:scim:api:messages:2.0:PatchOp"],
            "Operations": [{"op": "remove", "path": "active"}],
        }
        with (
            patch(
                "farna.api.routes.scim.scim_get_user",
                return_value=mock_user,
            ),
            patch(
                "farna.api.routes.scim.scim_deactivate_user",
                new_callable=AsyncMock,
                return_value=mock_user,
            ),
            patch("farna.api.routes.scim.assert_not_last_admin", new_callable=AsyncMock),
            patch(
                "farna.api.routes.scim._resolve_scim_admin_caller",
                new_callable=AsyncMock,
                return_value=USER_ID,
            ),
            patch("farna.api.routes.scim.set_rls_org"),
        ):
            resp = client.patch(
                f"/scim/v2/Users/{USER_ID}",
                json=body,
                headers=AUTH_HEADERS,
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
        mock_user = make_user()
        body = {
            "schemas": ["urn:ietf:params:scim:api:messages:2.0:PatchOp"],
            "Operations": [{"op": op}],
        }
        if op == "doesNotExist":
            body["Operations"][0]["path"] = "active"
            body["Operations"][0]["value"] = False
        with (
            patch(
                "farna.api.routes.scim.scim_get_user",
                return_value=mock_user,
            ),
            patch("farna.api.routes.scim.set_rls_org"),
        ):
            resp = client.patch(
                f"/scim/v2/Users/{USER_ID}",
                json=body,
                headers=AUTH_HEADERS,
            )
        assert resp.status_code == 400
        if expected_body_check:
            assert expected_body_check in resp.json()

    def test_patch_user_add_username(self, client: TestClient) -> None:
        mock_user = make_user()
        body = {
            "schemas": ["urn:ietf:params:scim:api:messages:2.0:PatchOp"],
            "Operations": [{"op": "add", "value": {"userName": "new-jane@example.com"}}],
        }
        with (
            patch(
                "farna.api.routes.scim.scim_get_user",
                return_value=mock_user,
            ),
            patch("farna.api.routes.scim.set_rls_org"),
        ):
            resp = client.patch(
                f"/scim/v2/Users/{USER_ID}",
                json=body,
                headers=AUTH_HEADERS,
            )
        assert resp.status_code == 200
        assert mock_user.email == "new-jane@example.com"

    @pytest.mark.parametrize(
        "member_value",
        [
            pytest.param({"value": str(USER_ID)}, id="single_member_dict"),
            pytest.param([{"value": str(USER_ID)}], id="member_list"),
        ],
    )
    def test_patch_group_remove_member_accepts_both_value_shapes(self, client: TestClient, member_value: dict[str, object] | list[dict[str, str]]) -> None:
        mock_team = make_team()
        body = {
            "schemas": ["urn:ietf:params:scim:api:messages:2.0:PatchOp"],
            "Operations": [{"op": "remove", "value": member_value}],
        }
        with (
            patch("farna.api.routes.scim.scim_get_group", return_value=mock_team),
            patch("farna.api.routes.scim.scim_remove_group_member", return_value=True) as remove_member,
            patch("farna.api.routes.scim.scim_list_group_members", return_value=[]),
            patch("farna.api.routes.scim.set_rls_org"),
        ):
            resp = client.patch(
                f"/scim/v2/Groups/{TEAM_ID}",
                json=body,
                headers=AUTH_HEADERS,
            )
        assert resp.status_code == 200
        remove_member.assert_awaited_once()
        assert remove_member.call_args.args[2] == USER_ID

    @pytest.mark.parametrize(
        ("op", "expected_body_check"),
        [
            pytest.param("doesNotExist", None, id="unsupported_op_returns_400"),
            pytest.param("invalidOp", "detail", id="invalid_op_returns_400"),
        ],
    )
    def test_patch_group_invalid_ops(self, client: TestClient, op: str, expected_body_check: str | None) -> None:
        mock_team = make_team()
        body = {
            "schemas": ["urn:ietf:params:scim:api:messages:2.0:PatchOp"],
            "Operations": [{"op": op}],
        }
        if op == "doesNotExist":
            body["Operations"][0]["value"] = False
        with (
            patch("farna.api.routes.scim.scim_get_group", return_value=mock_team),
            patch(
                "farna.api.routes.scim.scim_list_group_members",
                return_value=_MOCK_MEMBERSHIPS if op == "invalidOp" else [],
            ),
            patch("farna.api.routes.scim.set_rls_org"),
        ):
            resp = client.patch(
                f"/scim/v2/Groups/{TEAM_ID}",
                json=body,
                headers=AUTH_HEADERS,
            )
        assert resp.status_code == 400
        if expected_body_check:
            assert expected_body_check in resp.json()

    def test_replace_group_clear_members(self, client: TestClient) -> None:
        mock_team = make_team()
        put_body = {
            "schemas": ["urn:ietf:params:scim:schemas:core:2.0:Group"],
            "displayName": "Engineering",
            "members": [],
        }
        mock_membership = make_membership()

        with (
            patch("farna.api.routes.scim.scim_get_group", return_value=mock_team),
            patch("farna.api.routes.scim.scim_update_group", return_value=mock_team),
            patch("farna.api.routes.scim.scim_list_group_members", return_value=[mock_membership]),
            patch("farna.api.routes.scim.scim_remove_group_member", return_value=True),
            patch("farna.api.routes.scim.set_rls_org"),
        ):
            resp = client.put(
                f"/scim/v2/Groups/{TEAM_ID}",
                json=put_body,
                headers=AUTH_HEADERS,
            )
        assert resp.status_code == 200
        assert resp.json()["displayName"] == "Engineering"

    def test_patch_group_replace_members(self, client: TestClient) -> None:
        mock_team = make_team()
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
            patch("farna.api.routes.scim.scim_get_group", return_value=mock_team),
            patch("farna.api.routes.scim.scim_update_group", return_value=mock_team),
            patch("farna.api.routes.scim.scim_get_user", return_value=_MOCK_USER),
            patch("farna.api.routes.scim.scim_add_group_member", return_value=None),
            patch("farna.api.routes.scim.scim_list_group_members", return_value=[_MOCK_MEMBERSHIP]),
            patch("farna.api.routes.scim.scim_remove_group_member", return_value=True),
            patch("farna.api.routes.scim.set_rls_org"),
        ):
            resp = client.patch(
                f"/scim/v2/Groups/{TEAM_ID}",
                json=body,
                headers=AUTH_HEADERS,
            )
        assert resp.status_code == 200


class TestParseMemberUuid:
    """Direct coverage for the member-id parser extracted from ``patch_group`` (FAR-310).

    The PATCH helpers skip a member whose ``value`` does not parse as a UUID
    (invalid string, ``None``, non-str value) — this pins the parser contract
    so a malformed SCIM member reference is never turned into a DB call.
    """

    def test_valid_uuid_string_returns_uuid(self) -> None:
        assert _parse_member_uuid(str(USER_ID)) == USER_ID

    def test_valid_uuid_object_returns_uuid(self) -> None:
        assert _parse_member_uuid(USER_ID) == USER_ID

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
            headers=AUTH_HEADERS,
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["schemas"] == ["urn:ietf:params:scim:schemas:core:2.0:ServiceProviderConfig"]
        assert data["patch"]["supported"] is True

    def test_capability_document_declares_unsupported_features(self, scim_client: Callable[..., TestClient]) -> None:
        # SCIM clients probe this document before discovery, so it must publish
        # the full capability set even when MODULO_PUBLIC_URL is unset (that is
        # only needed by the resource routes' entity locations).
        client = scim_client(settings=lambda: make_settings(modulo_public_url=""))
        resp = client.get("/scim/v2/ServiceProviderConfig", headers=AUTH_HEADERS)
        assert resp.status_code == 200
        data = resp.json()
        assert data["bulk"]["supported"] is False
        assert data["filter"] == {"supported": True, "maxResults": 100}
        assert data["changePassword"]["supported"] is False
        assert data["sort"]["supported"] is False
        assert data["etag"]["supported"] is False
        assert data["authenticationSchemes"][0]["type"] == "bearer"
        assert data["authenticationSchemes"][0]["primary"] is True


# ── CRUD endpoints (parametrized) ──────────────────────────────────


class TestCrudNotFound:
    """Parametrized: 6 CRUD endpoints that return 404 when entity is not found."""

    @pytest.mark.parametrize(
        ("name", "method", "url", "mock_target", "body"),
        [
            pytest.param("get_user", "GET", "/scim/v2/Users/{id}", "farna.api.routes.scim.scim_get_user", None, id="get_user"),
            pytest.param(
                "replace_user",
                "PUT",
                "/scim/v2/Users/{id}",
                "farna.api.routes.scim.scim_get_user",
                USER_CREATE_BODY,
                id="replace_user",
            ),
            pytest.param(
                "patch_user",
                "PATCH",
                "/scim/v2/Users/{id}",
                "farna.api.routes.scim.scim_get_user",
                PATCH_USER_BODY,
                id="patch_user",
            ),
            pytest.param(
                "get_group",
                "GET",
                "/scim/v2/Groups/{id}",
                "farna.api.routes.scim.scim_get_group",
                None,
                id="get_group",
            ),
            pytest.param(
                "replace_group",
                "PUT",
                "/scim/v2/Groups/{id}",
                "farna.api.routes.scim.scim_get_group",
                GROUP_CREATE_BODY,
                id="replace_group",
            ),
            pytest.param(
                "patch_group",
                "PATCH",
                "/scim/v2/Groups/{id}",
                "farna.api.routes.scim.scim_get_group",
                PATCH_GROUP_ADD_MEMBER,
                id="patch_group",
            ),
        ],
    )
    def test_not_found_returns_404(self, client: TestClient, name: str, method: str, url: str, mock_target: str, body: dict | None) -> None:
        entity_id = USER_ID if "user" in name else TEAM_ID
        formatted_url = url.format(id=entity_id)
        with (
            patch(mock_target, return_value=None),
            patch("farna.api.routes.scim.set_rls_org"),
            patch("farna.api.routes.scim.assert_not_last_admin", new_callable=AsyncMock),
            patch(
                "farna.api.routes.scim._resolve_scim_admin_caller",
                new_callable=AsyncMock,
                return_value=USER_ID,
            ),
        ):
            headers = AUTH_HEADERS
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
                "farna.api.routes.scim.scim_delete_user_by_id",
                None,
                204,
                id="delete_user_204",
            ),
            pytest.param(
                "delete_group_204",
                "DELETE",
                "/scim/v2/Groups/{id}",
                "farna.api.routes.scim.scim_delete_group_by_id",
                None,
                204,
                id="delete_group_204",
            ),
            pytest.param(
                "delete_user_404",
                "DELETE",
                "/scim/v2/Users/{id}",
                "farna.api.routes.scim.scim_delete_user_by_id",
                None,
                404,
                id="delete_user_404",
            ),
            pytest.param(
                "delete_group_404",
                "DELETE",
                "/scim/v2/Groups/{id}",
                "farna.api.routes.scim.scim_delete_group_by_id",
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
        entity_id = USER_ID if "user" in name else TEAM_ID
        formatted_url = url.format(id=entity_id)
        mock_return = expected_status == 204
        with (
            patch(mock_target, return_value=mock_return),
            patch("farna.api.routes.scim.set_rls_org"),
            patch("farna.api.routes.scim.assert_not_last_admin", new_callable=AsyncMock),
            patch(
                "farna.api.routes.scim._resolve_scim_admin_caller",
                new_callable=AsyncMock,
                return_value=USER_ID,
            ),
        ):
            resp = client.delete(formatted_url, headers=AUTH_HEADERS)
        assert resp.status_code == expected_status


class TestCrudCreate:
    """Parametrized: create endpoints — duplicate = 409, success = 201."""

    @pytest.mark.parametrize(
        ("name", "url", "body", "mock_duplicate_target", "duplicate_return"),
        [
            pytest.param(
                "user_duplicate",
                "/scim/v2/Users",
                USER_CREATE_BODY,
                "farna.db.crud.account.get_account_by_email",
                _MOCK_USER,
                id="duplicate_user_409",
            ),
            pytest.param(
                "group_duplicate",
                "/scim/v2/Groups",
                GROUP_CREATE_BODY,
                "farna.db.crud.team.get_team_by_name",
                _MOCK_TEAM,
                id="duplicate_group_409",
            ),
        ],
    )
    def test_duplicate_returns_409(self, client: TestClient, name: str, url: str, body: dict, mock_duplicate_target: str, duplicate_return: object) -> None:
        if "group" in name:
            with (
                patch(mock_duplicate_target, return_value=duplicate_return),
                patch("farna.api.routes.scim.set_rls_org"),
            ):
                resp = client.post(url, json=body, headers=AUTH_HEADERS)
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
                "farna.db.crud.org_membership.get_membership_by_account_and_org",
                return_value=mock_membership,
            ),
            patch("farna.api.routes.scim.set_rls_org"),
        ):
            resp = client.post(url, json=body, headers=AUTH_HEADERS)
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
            patch("farna.api.routes.scim.scim_list_users", return_value=_MOCK_USER_LIST),
            patch("farna.api.routes.scim.set_rls_org"),
        ):
            resp = client.get(
                "/scim/v2/Users",
                headers=AUTH_HEADERS,
            )
        assert resp.status_code == 200
        data = resp.json()
        assert data["totalResults"] == 1
        assert len(data["Resources"]) == 1
        assert data["Resources"][0]["userName"] == "jane@example.com"


class TestCreateUser:
    def test_returns_201(self, client: TestClient) -> None:
        with (
            patch("farna.api.routes.scim.scim_create_user", return_value=_MOCK_USER),
            patch("farna.db.crud.account.get_account_by_email", return_value=None),
            patch("farna.api.routes.scim.set_rls_org"),
        ):
            resp = client.post(
                "/scim/v2/Users",
                json=USER_CREATE_BODY,
                headers=AUTH_HEADERS,
            )
        assert resp.status_code == 201
        data = resp.json()
        assert data["userName"] == "jane@example.com"
        assert data["active"] is True


class TestGetUser:
    def test_returns_200(self, client: TestClient) -> None:
        with (
            patch("farna.api.routes.scim.scim_get_user", return_value=_MOCK_USER),
            patch("farna.api.routes.scim.set_rls_org"),
        ):
            resp = client.get(
                f"/scim/v2/Users/{USER_ID}",
                headers=AUTH_HEADERS,
            )
        assert resp.status_code == 200
        assert resp.json()["id"] == str(USER_ID)


class TestReplaceUser:
    def test_returns_200(self, client: TestClient) -> None:
        with (
            patch("farna.api.routes.scim.scim_get_user", return_value=_MOCK_USER),
            patch("farna.api.routes.scim.scim_update_user", return_value=_MOCK_USER),
            patch("farna.api.routes.scim.set_rls_org"),
        ):
            resp = client.put(
                f"/scim/v2/Users/{USER_ID}",
                json=USER_CREATE_BODY,
                headers=AUTH_HEADERS,
            )
        assert resp.status_code == 200
        assert resp.json()["id"] == str(USER_ID)


class TestPatchUser:
    def test_returns_200(self, client: TestClient) -> None:
        mock_user = make_user()
        with (
            patch("farna.api.routes.scim.scim_get_user", return_value=mock_user),
            patch(
                "farna.api.routes.scim.scim_deactivate_user",
                new_callable=AsyncMock,
                return_value=mock_user,
            ),
            patch("farna.api.routes.scim.assert_not_last_admin", new_callable=AsyncMock),
            patch(
                "farna.api.routes.scim._resolve_scim_admin_caller",
                new_callable=AsyncMock,
                return_value=USER_ID,
            ),
            patch("farna.api.routes.scim.set_rls_org"),
        ):
            resp = client.patch(
                f"/scim/v2/Users/{USER_ID}",
                json=PATCH_USER_BODY,
                headers=AUTH_HEADERS,
            )
        assert resp.status_code == 200
        assert mock_user.active is False


class TestNoScimRouteSetsOrgRole:
    """ADR 047: SCIM is exempt via MODULOSCIM_TOKEN, but no route may set an
    org role. ``scim_update_user`` has a *functional* role-UPDATE that must
    stay unwired — prove no SCIM route passes ``org_role``."""

    def test_put_user_with_org_role_field_is_ignored(self, client: TestClient) -> None:
        with (
            patch("farna.api.routes.scim.scim_get_user", return_value=_MOCK_USER),
            patch("farna.api.routes.scim.scim_update_user", return_value=_MOCK_USER) as update_mock,
            patch("farna.api.routes.scim.set_rls_org"),
        ):
            body = {**USER_CREATE_BODY, "org_role": "admin"}
            resp = client.put(
                f"/scim/v2/Users/{USER_ID}",
                json=body,
                headers=AUTH_HEADERS,
            )
        assert resp.status_code == 200
        update_mock.assert_awaited_once()
        kwargs = update_mock.call_args.kwargs
        assert "org_role" not in kwargs
        assert kwargs.get("role") is None

    def test_patch_user_org_role_path_is_not_applied(self, client: TestClient) -> None:
        mock_user = make_user(org_role="runner")
        with (
            patch("farna.api.routes.scim.scim_get_user", return_value=mock_user),
            patch("farna.api.routes.scim.set_rls_org"),
        ):
            body = {
                "schemas": ["urn:ietf:params:scim:api:messages:2.0:PatchOp"],
                "Operations": [{"op": "replace", "path": "org_role", "value": "admin"}],
            }
            resp = client.patch(
                f"/scim/v2/Users/{USER_ID}",
                json=body,
                headers=AUTH_HEADERS,
            )
        assert resp.status_code == 200
        assert mock_user.org_role == "runner"

    def test_create_user_org_role_field_is_ignored(self, client: TestClient) -> None:
        with (
            patch("farna.db.crud.account.get_account_by_email", return_value=None),
            patch("farna.api.routes.scim.set_rls_org"),
            patch("farna.api.routes.scim.scim_create_user", return_value=_MOCK_USER) as create_mock,
        ):
            body = {**USER_CREATE_BODY, "org_role": "admin"}
            resp = client.post(
                "/scim/v2/Users",
                json=body,
                headers=AUTH_HEADERS,
            )
        assert resp.status_code == 201
        create_mock.assert_awaited_once()
        kwargs = create_mock.call_args.kwargs
        assert "org_role" not in kwargs


# ── Group endpoints (unique tests) ──────────────────────────────────


class TestListGroups:
    def test_returns_200(self, client: TestClient) -> None:
        with (
            patch("farna.api.routes.scim.scim_list_groups", return_value=_MOCK_TEAM_LIST),
            patch("farna.api.routes.scim.scim_list_group_members", return_value=_MOCK_MEMBERSHIPS),
            patch("farna.api.routes.scim.set_rls_org"),
        ):
            resp = client.get(
                "/scim/v2/Groups",
                headers=AUTH_HEADERS,
            )
        assert resp.status_code == 200
        data = resp.json()
        assert data["totalResults"] == 1
        assert data["Resources"][0]["displayName"] == "Engineering"


class TestCreateGroup:
    def test_returns_201(self, client: TestClient) -> None:
        with (
            patch("farna.api.routes.scim.scim_create_group", return_value=_MOCK_TEAM),
            patch("farna.db.crud.team.get_team_by_name", return_value=None),
            patch("farna.db.crud.org_membership.list_memberships_for_org", return_value=[]),
            patch("farna.api.routes.scim.scim_get_user", return_value=_MOCK_USER),
            patch("farna.api.routes.scim.scim_add_group_member", return_value=None),
            patch("farna.api.routes.scim.set_rls_org"),
        ):
            resp = client.post(
                "/scim/v2/Groups",
                json=GROUP_CREATE_BODY,
                headers=AUTH_HEADERS,
            )
        assert resp.status_code == 201
        data = resp.json()
        assert data["displayName"] == "Engineering"


class TestGetGroup:
    def test_returns_200(self, client: TestClient) -> None:
        with (
            patch("farna.api.routes.scim.scim_get_group", return_value=_MOCK_TEAM),
            patch("farna.api.routes.scim.scim_list_group_members", return_value=_MOCK_MEMBERSHIPS),
            patch("farna.api.routes.scim.set_rls_org"),
        ):
            resp = client.get(
                f"/scim/v2/Groups/{TEAM_ID}",
                headers=AUTH_HEADERS,
            )
        assert resp.status_code == 200
        assert resp.json()["displayName"] == "Engineering"


class TestReplaceGroup:
    def test_returns_200(self, client: TestClient) -> None:
        with (
            patch("farna.api.routes.scim.scim_get_group", return_value=_MOCK_TEAM),
            patch("farna.api.routes.scim.scim_update_group", return_value=_MOCK_TEAM),
            patch("farna.api.routes.scim.scim_list_group_members", return_value=_MOCK_MEMBERSHIPS),
            patch("farna.api.routes.scim.scim_get_user", return_value=_MOCK_USER),
            patch("farna.api.routes.scim.scim_add_group_member", return_value=None),
            patch("farna.api.routes.scim.scim_remove_group_member", return_value=True),
            patch("farna.api.routes.scim.set_rls_org"),
        ):
            resp = client.put(
                f"/scim/v2/Groups/{TEAM_ID}",
                json=GROUP_CREATE_BODY,
                headers=AUTH_HEADERS,
            )
        assert resp.status_code == 200
        assert resp.json()["displayName"] == "Engineering"


class TestPatchGroup:
    def test_add_member_returns_200(self, client: TestClient) -> None:
        mock_team = make_team()
        with (
            patch("farna.api.routes.scim.scim_get_group", return_value=mock_team),
            patch("farna.api.routes.scim.scim_get_user", return_value=_MOCK_USER),
            patch("farna.api.routes.scim.scim_add_group_member", return_value=None),
            patch("farna.api.routes.scim.scim_list_group_members", return_value=_MOCK_MEMBERSHIPS),
            patch("farna.api.routes.scim.set_rls_org"),
        ):
            resp = client.patch(
                f"/scim/v2/Groups/{TEAM_ID}",
                json=PATCH_GROUP_ADD_MEMBER,
                headers=AUTH_HEADERS,
            )
        assert resp.status_code == 200
        assert resp.json()["displayName"] == "Engineering"

    def test_add_member_foreign_org_user_is_skipped(self, client: TestClient) -> None:
        # GH-1797: a PATCH add op must not add a user that is not a member of
        # the caller's org — mirror the POST/PUT behaviour (skip, 200).
        mock_team = make_team()
        foreign_user_id = uuid.uuid4()
        body = {
            "schemas": ["urn:ietf:params:scim:api:messages:2.0:PatchOp"],
            "Operations": [{"op": "add", "path": "members", "value": [{"value": str(foreign_user_id)}]}],
        }
        with (
            patch("farna.api.routes.scim.scim_get_group", return_value=mock_team),
            patch("farna.api.routes.scim.scim_get_user", return_value=None),
            patch("farna.api.routes.scim.scim_add_group_member", return_value=None) as mock_add,
            patch("farna.api.routes.scim.scim_list_group_members", return_value=[]),
            patch("farna.api.routes.scim.set_rls_org"),
        ):
            resp = client.patch(
                f"/scim/v2/Groups/{TEAM_ID}",
                json=body,
                headers=AUTH_HEADERS,
            )
        assert resp.status_code == 200
        mock_add.assert_not_called()

    def test_add_member_mixed_foreign_and_same_org_only_same_org_lands(self, client: TestClient) -> None:
        # GH-1797: the foreign-org member is skipped, the same-org member is added.
        mock_team = make_team()
        foreign_user_id = uuid.uuid4()

        _fake_get_user = only_org_owns(USER_ID)

        body = {
            "schemas": ["urn:ietf:params:scim:api:messages:2.0:PatchOp"],
            "Operations": [
                {
                    "op": "add",
                    "path": "members",
                    "value": [{"value": str(foreign_user_id)}, {"value": str(USER_ID)}],
                }
            ],
        }
        with (
            patch("farna.api.routes.scim.scim_get_group", return_value=mock_team),
            patch("farna.api.routes.scim.scim_get_user", side_effect=_fake_get_user) as mock_get_user,
            patch("farna.api.routes.scim.scim_add_group_member", return_value=None) as mock_add,
            patch("farna.api.routes.scim.scim_list_group_members", return_value=[]),
            patch("farna.api.routes.scim.set_rls_org"),
        ):
            resp = client.patch(
                f"/scim/v2/Groups/{TEAM_ID}",
                json=body,
                headers=AUTH_HEADERS,
            )
        assert resp.status_code == 200
        mock_add.assert_called_once()
        assert mock_add.call_args.kwargs["user_id"] == USER_ID
        # The lookup must run against the caller's org, never the client-supplied payload.
        mock_get_user.assert_any_call(ANY, ORG_ID, USER_ID)

    def test_replace_members_foreign_org_user_is_skipped(self, client: TestClient) -> None:
        # GH-1797: a PATCH replace op must not add a user that is not a member
        # of the caller's org — mirror the PUT behaviour (skip, 200).
        mock_team = make_team()
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
            patch("farna.api.routes.scim.scim_get_group", return_value=mock_team),
            patch("farna.api.routes.scim.scim_update_group", return_value=mock_team),
            patch("farna.api.routes.scim.scim_get_user", return_value=None),
            patch("farna.api.routes.scim.scim_add_group_member", return_value=None) as mock_add,
            patch("farna.api.routes.scim.scim_list_group_members", return_value=[_MOCK_MEMBERSHIP]),
            patch("farna.api.routes.scim.scim_remove_group_member", return_value=True),
            patch("farna.api.routes.scim.set_rls_org"),
        ):
            resp = client.patch(
                f"/scim/v2/Groups/{TEAM_ID}",
                json=body,
                headers=AUTH_HEADERS,
            )
        assert resp.status_code == 200
        mock_add.assert_not_called()

    def test_replace_members_mixed_foreign_and_same_org_only_same_org_lands(self, client: TestClient) -> None:
        # GH-1797: the foreign-org member is skipped, the same-org member is added.
        mock_team = make_team()
        foreign_user_id = uuid.uuid4()

        _fake_get_user = only_org_owns(USER_ID)

        body = {
            "schemas": ["urn:ietf:params:scim:api:messages:2.0:PatchOp"],
            "Operations": [
                {
                    "op": "replace",
                    "value": {
                        "displayName": "Engineering",
                        "members": [{"value": str(foreign_user_id)}, {"value": str(USER_ID)}],
                    },
                }
            ],
        }
        with (
            patch("farna.api.routes.scim.scim_get_group", return_value=mock_team),
            patch("farna.api.routes.scim.scim_update_group", return_value=mock_team),
            patch("farna.api.routes.scim.scim_get_user", side_effect=_fake_get_user),
            patch("farna.api.routes.scim.scim_add_group_member", return_value=None) as mock_add,
            patch("farna.api.routes.scim.scim_list_group_members", return_value=[_MOCK_MEMBERSHIP]),
            patch("farna.api.routes.scim.scim_remove_group_member", return_value=True),
            patch("farna.api.routes.scim.set_rls_org"),
        ):
            resp = client.patch(
                f"/scim/v2/Groups/{TEAM_ID}",
                json=body,
                headers=AUTH_HEADERS,
            )
        assert resp.status_code == 200
        mock_add.assert_called_once()
        assert mock_add.call_args.kwargs["user_id"] == USER_ID


# ── License gate ─────────────────────────────────────────────────────


class TestLicenseGate:
    def test_no_license_returns_402(self, scim_client: Callable[..., TestClient]) -> None:
        client = scim_client(
            settings=lambda: make_settings(modulo_license_key=""),
            plan_context=CommunityTier,
        )
        resp = client.get("/scim/v2/Users", headers=AUTH_HEADERS)
        assert resp.status_code == 402


# ── SCIM audit actor (FAR-1472) ──────────────────────────────────────


class TestScimAuditPrincipal:
    """``get_scim_audit_principal`` — the principal ``audited()`` writes with.

    The route-level stubs in ``stub_scim_audit`` replace this resolver, so its
    real behaviour (admin-caller lookup, refusal when there is no honest actor)
    is pinned here directly.
    """

    @staticmethod
    def _session_with_caller(caller: uuid.UUID | None) -> AsyncMock:
        session = configure_mock_session(AsyncMock(), allow_empty_execute=True)
        begin_cm = AsyncMock()
        begin_cm.__aenter__ = AsyncMock(return_value=None)
        begin_cm.__aexit__ = AsyncMock(return_value=False)
        session.begin = MagicMock(return_value=begin_cm)
        result = MagicMock()
        result.first.return_value = None if caller is None else (caller,)
        session.execute = AsyncMock(return_value=result)
        return session

    async def test_resolves_the_org_admin_as_the_audit_actor(self) -> None:
        session = self._session_with_caller(USER_ID)
        principal = await get_scim_audit_principal(
            scim=ScimPrincipal(organisation_id=ORG_ID),
            session=session,
        )
        assert isinstance(principal, TenantPrincipal)
        assert principal.account_id == USER_ID
        assert principal.organisation_id == ORG_ID
        assert principal.org_role == "admin"

    async def test_refuses_the_mutation_when_no_active_admin_exists(self) -> None:
        session = self._session_with_caller(None)
        with pytest.raises(HTTPException) as excinfo:
            await get_scim_audit_principal(
                scim=ScimPrincipal(organisation_id=ORG_ID),
                session=session,
            )
        assert excinfo.value.status_code == 409
        assert isinstance(excinfo.value.detail, dict)
        assert excinfo.value.detail["detail"] == _MSG_NO_ACTIVE_ADMIN_EXISTS
