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

from modulo.api.dependencies import _get_engine, get_db_session
from modulo.auth.scim_auth import ScimPrincipal, get_scim_plan_context, get_scim_principal
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
_SQLALCHEMY_ERROR = SQLAlchemyError("mock", "connection error", Exception("mock connection failure"))


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


def _make_mock_session() -> MagicMock:
    session = MagicMock()
    session.execute = AsyncMock()
    session.flush = AsyncMock()
    session.delete = AsyncMock()
    session.rollback = AsyncMock()
    begin_cm = AsyncMock()
    begin_cm.__aenter__ = AsyncMock(return_value=None)
    begin_cm.__aexit__ = AsyncMock(return_value=False)
    session.begin = MagicMock(return_value=begin_cm)
    return session


class _AllFeatures:
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

    async def override_session() -> AsyncGenerator[MagicMock, None]:
        yield mock_session

    app.dependency_overrides[get_settings] = _make_settings
    app.dependency_overrides[get_db_session] = override_session
    app.dependency_overrides[_get_engine] = lambda: MagicMock()
    app.dependency_overrides[get_scim_principal] = lambda: ScimPrincipal(organisation_id=_ORG_ID)
    app.dependency_overrides[get_scim_plan_context] = _override_plan_context
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

_SCIM_ROUTES: list[tuple[str, str, str, str | None, object]] = [
    ("list_users", "GET", "/scim/v2/Users", "modulo.api.routes.scim.scim_list_users", None),
    ("create_user", "POST", "/scim/v2/Users", "modulo.db.crud.account.get_account_by_email", _USER_CREATE_BODY),
    ("get_user", "GET", "/scim/v2/Users/{id}", "modulo.api.routes.scim.scim_get_user", None),
    ("replace_user", "PUT", "/scim/v2/Users/{id}", "modulo.api.routes.scim.scim_get_user", _USER_CREATE_BODY),
    ("patch_user", "PATCH", "/scim/v2/Users/{id}", "modulo.api.routes.scim.scim_get_user", _PATCH_BODY),
    ("delete_user", "DELETE", "/scim/v2/Users/{id}", "modulo.api.routes.scim.scim_delete_user_by_id", None),
    ("list_groups", "GET", "/scim/v2/Groups", "modulo.api.routes.scim.scim_list_groups", None),
    ("create_group", "POST", "/scim/v2/Groups", "modulo.db.crud.team.get_team_by_name", _GROUP_CREATE_BODY),
    ("get_group", "GET", "/scim/v2/Groups/{id}", "modulo.api.routes.scim.scim_get_group", None),
    ("replace_group", "PUT", "/scim/v2/Groups/{id}", "modulo.api.routes.scim.scim_get_group", _GROUP_CREATE_BODY),
    ("patch_group", "PATCH", "/scim/v2/Groups/{id}", "modulo.api.routes.scim.scim_get_group", _PATCH_BODY),
    ("delete_group", "DELETE", "/scim/v2/Groups/{id}", "modulo.api.routes.scim.scim_delete_group_by_id", None),
]

_ENTITY_IDS: dict[str, uuid.UUID | None] = {
    "list_users": None,
    "create_user": None,
    "get_user": _USER_ID,
    "replace_user": _USER_ID,
    "patch_user": _USER_ID,
    "delete_user": _USER_ID,
    "list_groups": None,
    "create_group": None,
    "get_group": _TEAM_ID,
    "replace_group": _TEAM_ID,
    "patch_group": _TEAM_ID,
    "delete_group": _TEAM_ID,
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
            patch("modulo.api.routes.scim.set_rls_org"),
        ):
            if method == "GET":
                resp = client.get(url, headers={"Authorization": f"Bearer {_SCIM_TOKEN}"})
            elif method == "POST":
                resp = client.post(url, json=body, headers={"Authorization": f"Bearer {_SCIM_TOKEN}"})
            elif method == "PUT":
                resp = client.put(url, json=body, headers={"Authorization": f"Bearer {_SCIM_TOKEN}"})
            elif method == "PATCH":
                resp = client.patch(url, json=body, headers={"Authorization": f"Bearer {_SCIM_TOKEN}"})
            elif method == "DELETE":
                resp = client.delete(url, headers={"Authorization": f"Bearer {_SCIM_TOKEN}"})

        assert resp.status_code == expected_status, f"Expected {expected_status}, got {resp.status_code}"
        if detail_check:
            detail = resp.json().get("detail", "")
            assert detail_check in detail.lower(), f"Expected detail mentioning '{detail_check}', got: {detail}"


# ── _get_base_url tests (unique setup — no scim token configured) ─────────


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


class TestGetBaseUrl:
    """_get_base_url raises 500 when modulo_public_url is not configured."""

    def _setup(self) -> TestClient:
        from modulo.api.main import app

        app.dependency_overrides[get_settings] = _make_settings_no_public_url
        app.dependency_overrides[get_db_session] = lambda: _make_mock_session()
        app.dependency_overrides[_get_engine] = lambda: MagicMock()
        app.dependency_overrides[get_scim_principal] = lambda: ScimPrincipal(organisation_id=_ORG_ID)
        app.dependency_overrides[get_scim_plan_context] = _override_plan_context
        return TestClient(app)

    def _teardown(self) -> None:
        from modulo.api.main import app

        app.dependency_overrides.clear()

    def test_list_users_returns_500(self) -> None:
        client = self._setup()
        with (
            patch("modulo.api.routes.scim.scim_list_users", return_value=([_MOCK_USER], 1)),
            patch("modulo.api.routes.scim.set_rls_org"),
        ):
            resp = client.get("/scim/v2/Users", headers={"Authorization": f"Bearer {_SCIM_TOKEN}"})
        self._teardown()
        assert resp.status_code == 500
        assert "MODULO_PUBLIC_URL" in resp.json().get("detail", "")

    def test_get_user_returns_500(self) -> None:
        client = self._setup()
        with (
            patch("modulo.api.routes.scim.scim_get_user", return_value=_MOCK_USER),
            patch("modulo.api.routes.scim.set_rls_org"),
        ):
            resp = client.get(f"/scim/v2/Users/{_USER_ID}", headers={"Authorization": f"Bearer {_SCIM_TOKEN}"})
        self._teardown()
        assert resp.status_code == 500
        assert "MODULO_PUBLIC_URL" in resp.json().get("detail", "")
