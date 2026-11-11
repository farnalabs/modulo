"""Unit tests for SCIM error handling.

Verifies all 12 SCIM route handlers return correct error status codes:
- ProgrammingError → 501 (missing DB table)
- SQLAlchemyError → 503 (connection/deadlock failure)
- Generic Exception → 500 (unexpected Python error)
"""

import uuid
from collections.abc import Callable
from unittest.mock import AsyncMock, patch

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.exc import ProgrammingError, SQLAlchemyError

from tests.unit.scim.helpers import (
    AUTH_HEADERS,
    GROUP_CREATE_BODY,
    PATCH_USER_BODY,
    TEAM_ID,
    USER_CREATE_BODY,
    USER_ID,
    make_settings,
    make_user,
)

_PROGRAMMING_ERROR = ProgrammingError("mock statement", [], Exception("mock table does not exist"))
_SQLALCHEMY_ERROR = SQLAlchemyError("mock", "connection error", Exception("mock connection failure"))

_MOCK_USER = make_user()


class _AllFeatures:
    def feature_enabled(self, name: str) -> bool:
        return True

    def list_enabled_features(self) -> list:
        return []

    def tier(self) -> str:
        return "team"

    def has_license_key(self) -> bool:
        return True


async def _override_plan_context() -> _AllFeatures:
    return _AllFeatures()


@pytest.fixture
def client(scim_client: Callable[..., TestClient]) -> TestClient:
    return scim_client(plan_context=_override_plan_context)


_SCIM_ROUTES: list[tuple[str, str, str, str | None, object]] = [
    ("list_users", "GET", "/scim/v2/Users", "farna.api.routes.scim.scim_list_users", None),
    ("create_user", "POST", "/scim/v2/Users", "farna.db.crud.account.get_account_by_email", USER_CREATE_BODY),
    ("get_user", "GET", "/scim/v2/Users/{id}", "farna.api.routes.scim.scim_get_user", None),
    ("replace_user", "PUT", "/scim/v2/Users/{id}", "farna.api.routes.scim.scim_get_user", USER_CREATE_BODY),
    ("patch_user", "PATCH", "/scim/v2/Users/{id}", "farna.api.routes.scim.scim_get_user", PATCH_USER_BODY),
    ("delete_user", "DELETE", "/scim/v2/Users/{id}", "farna.api.routes.scim.scim_delete_user_by_id", None),
    ("list_groups", "GET", "/scim/v2/Groups", "farna.api.routes.scim.scim_list_groups", None),
    ("create_group", "POST", "/scim/v2/Groups", "farna.db.crud.team.get_team_by_name", GROUP_CREATE_BODY),
    ("get_group", "GET", "/scim/v2/Groups/{id}", "farna.api.routes.scim.scim_get_group", None),
    ("replace_group", "PUT", "/scim/v2/Groups/{id}", "farna.api.routes.scim.scim_get_group", GROUP_CREATE_BODY),
    ("patch_group", "PATCH", "/scim/v2/Groups/{id}", "farna.api.routes.scim.scim_get_group", PATCH_USER_BODY),
    ("delete_group", "DELETE", "/scim/v2/Groups/{id}", "farna.api.routes.scim.scim_delete_group_by_id", None),
]

_ENTITY_IDS: dict[str, uuid.UUID | None] = {
    "list_users": None,
    "create_user": None,
    "get_user": USER_ID,
    "replace_user": USER_ID,
    "patch_user": USER_ID,
    "delete_user": USER_ID,
    "list_groups": None,
    "create_group": None,
    "get_group": TEAM_ID,
    "replace_group": TEAM_ID,
    "patch_group": TEAM_ID,
    "delete_group": TEAM_ID,
}


class TestScimDatabaseErrors:
    """Parametrized: 12 routes with 3 error types = 36 cases collapsed into one test."""

    @pytest.mark.parametrize(
        ("route_name", "error_type", "expected_status", "detail_check"),
        [pytest.param(r, "programming", 501, "migrations", id=f"{r}_501") for r, _, _, _, _ in _SCIM_ROUTES]
        + [pytest.param(r, "sqlalchemy", 503, "database error", id=f"{r}_503") for r, _, _, _, _ in _SCIM_ROUTES]
        + [pytest.param(r, "exception", 500, None, id=f"{r}_500") for r, _, _, _, _ in _SCIM_ROUTES],
    )
    def test_error_returns_expected_status(
        self,
        client: TestClient,
        route_name: str,
        error_type: str,
        expected_status: int,
        detail_check: str | None,
    ) -> None:
        route_info = {r[0]: r for r in _SCIM_ROUTES}[route_name]
        _, method, url_template, mock_target, body = route_info
        entity_id = _ENTITY_IDS[route_name]

        if error_type == "programming":
            side_effect = _PROGRAMMING_ERROR
        elif error_type == "sqlalchemy":
            side_effect = _SQLALCHEMY_ERROR
        else:
            side_effect = ValueError("mock ValueError")

        url = url_template.format(id=entity_id) if entity_id else url_template

        with (
            patch(mock_target, side_effect=side_effect),
            patch("farna.api.routes.scim.set_rls_org"),
            patch("farna.api.routes.scim.assert_not_last_admin", new_callable=AsyncMock),
            patch(
                "farna.api.routes.scim._resolve_scim_admin_caller",
                new_callable=AsyncMock,
                return_value=USER_ID,
            ),
        ):
            if method == "GET":
                resp = client.get(url, headers=AUTH_HEADERS)
            elif method == "POST":
                resp = client.post(url, json=body, headers=AUTH_HEADERS)
            elif method == "PUT":
                resp = client.put(url, json=body, headers=AUTH_HEADERS)
            elif method == "PATCH":
                resp = client.patch(url, json=body, headers=AUTH_HEADERS)
            elif method == "DELETE":
                resp = client.delete(url, headers=AUTH_HEADERS)

        assert resp.status_code == expected_status, f"Expected {expected_status}, got {resp.status_code}"
        if detail_check:
            detail = resp.json().get("detail", "")
            assert detail_check in detail.lower(), f"Expected detail mentioning '{detail_check}', got: {detail}"


# ── _get_base_url tests (unique setup — no public URL configured) ─────────


class TestGetBaseUrl:
    """_get_base_url raises 500 when modulo_public_url is not configured."""

    @pytest.fixture
    def client(self, scim_client: Callable[..., TestClient]) -> TestClient:
        return scim_client(
            settings=lambda: make_settings(modulo_public_url=""),
            plan_context=_override_plan_context,
        )

    def test_list_users_returns_500(self, client: TestClient) -> None:
        with (
            patch("farna.api.routes.scim.scim_list_users", return_value=([_MOCK_USER], 1)),
            patch("farna.api.routes.scim.set_rls_org"),
        ):
            resp = client.get("/scim/v2/Users", headers=AUTH_HEADERS)
        assert resp.status_code == 500
        assert "MODULO_PUBLIC_URL" in resp.json().get("detail", "")

    def test_get_user_returns_500(self, client: TestClient) -> None:
        with (
            patch("farna.api.routes.scim.scim_get_user", return_value=_MOCK_USER),
            patch("farna.api.routes.scim.set_rls_org"),
        ):
            resp = client.get(f"/scim/v2/Users/{USER_ID}", headers=AUTH_HEADERS)
        assert resp.status_code == 500
        assert "MODULO_PUBLIC_URL" in resp.json().get("detail", "")
