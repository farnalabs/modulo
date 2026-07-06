"""Unit tests: ProgrammingError on org admin routes returns 501."""

import uuid
from collections.abc import AsyncGenerator, Generator
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.exc import ProgrammingError

from modulo.api.dependencies import _get_engine, get_db_session, get_plan_context
from modulo.api.main import app
from modulo.auth.dependencies import get_current_user
from modulo.auth.jwt import AuthenticatedPrincipal
from modulo.core.feature_flags import PlanContext
from modulo.settings import Settings, get_settings

_VALID_32 = "a" * 32
_ORG_ID = uuid.UUID("00000000-0000-0000-0000-000000000001")
_USER_ID = uuid.UUID("00000000-0000-0000-0000-000000000002")


def _make_settings() -> Settings:
    return Settings(
        database_url="postgresql+asyncpg://localhost/test",
        secret_key=_VALID_32,
        fernet_key=_VALID_32,
        modulo_admin_password="testpass",
    )


def _make_plan_context() -> PlanContext:
    ctx = MagicMock(spec=PlanContext)
    ctx.feature_enabled.return_value = True
    return ctx


def _execute_side_effect(*args: object, **kwargs: object) -> None:
    raise ProgrammingError("mock", {}, "")


@pytest.fixture()
def org_id_str() -> str:
    return str(_ORG_ID)


@pytest.fixture()
def broken_session() -> AsyncMock:
    mock_session = AsyncMock()
    mock_session.execute.side_effect = _execute_side_effect
    mock_session.begin = MagicMock(
        return_value=AsyncMock(
            __aenter__=AsyncMock(side_effect=_execute_side_effect),
            __aexit__=AsyncMock(return_value=False),
        )
    )
    return mock_session


@pytest.fixture()
def client_admin(broken_session: AsyncMock) -> Generator[TestClient, None, None]:
    async def override_session() -> AsyncGenerator[AsyncMock, None]:
        yield broken_session

    app.dependency_overrides[get_settings] = _make_settings
    app.dependency_overrides[get_db_session] = override_session
    app.dependency_overrides[_get_engine] = lambda: MagicMock()
    app.dependency_overrides[get_plan_context] = lambda: _make_plan_context()
    app.dependency_overrides[get_current_user] = lambda: AuthenticatedPrincipal(
        username="admin",
        organisation_id=_ORG_ID,
        account_id=_USER_ID,
        org_role="admin",
    )
    yield TestClient(app)
    app.dependency_overrides.clear()


@pytest.fixture()
def client_system_admin(broken_session: AsyncMock) -> Generator[TestClient, None, None]:
    async def override_session() -> AsyncGenerator[AsyncMock, None]:
        yield broken_session

    app.dependency_overrides[get_settings] = _make_settings
    app.dependency_overrides[get_db_session] = override_session
    app.dependency_overrides[_get_engine] = lambda: MagicMock()
    app.dependency_overrides[get_plan_context] = lambda: _make_plan_context()
    app.dependency_overrides[get_current_user] = lambda: AuthenticatedPrincipal(
        username="sysadmin",
        organisation_id=_ORG_ID,
        account_id=_USER_ID,
        org_role="admin",
        is_system_admin=True,
    )
    yield TestClient(app)
    app.dependency_overrides.clear()


class TestAdminOrgsProgrammingError:
    def test_create_org_returns_501(self, client_system_admin: TestClient) -> None:
        resp = client_system_admin.post(
            "/api/v1/admin/orgs",
            json={"name": "Test Org", "slug": "test-org"},
        )
        assert resp.status_code == 501
        assert "migrations" in resp.text.lower()

    def test_list_orgs_returns_501(self, client_system_admin: TestClient) -> None:
        resp = client_system_admin.get("/api/v1/admin/orgs")
        assert resp.status_code == 501
        assert "migrations" in resp.text.lower()

    def test_create_org_user_returns_501(self, client_system_admin: TestClient, org_id_str: str) -> None:
        resp = client_system_admin.post(
            f"/api/v1/admin/orgs/{org_id_str}/users",
            json={
                "email": "test@example.com",
                "display_name": "Test User",
                "password": "password123",
                "org_role": "runner",
            },
        )
        assert resp.status_code == 501
        assert "migrations" in resp.text.lower()

    def test_delete_org_returns_501(self, client_system_admin: TestClient, org_id_str: str) -> None:
        resp = client_system_admin.delete(f"/api/v1/admin/orgs/{org_id_str}")
        assert resp.status_code == 501
        assert "migrations" in resp.text.lower()

    def test_get_org_license_returns_501(self, client_system_admin: TestClient, org_id_str: str) -> None:
        resp = client_system_admin.get(f"/api/v1/admin/orgs/{org_id_str}/license")
        assert resp.status_code == 501
        assert "migrations" in resp.text.lower()

    def test_set_org_license_returns_501(self, client_system_admin: TestClient, org_id_str: str) -> None:
        resp = client_system_admin.put(
            f"/api/v1/admin/orgs/{org_id_str}/license",
            json={"license_key": "test-key"},
        )
        assert resp.status_code == 501
        assert "migrations" in resp.text.lower()

    def test_remove_org_license_returns_501(self, client_system_admin: TestClient, org_id_str: str) -> None:
        resp = client_system_admin.delete(f"/api/v1/admin/orgs/{org_id_str}/license")
        assert resp.status_code == 501
        assert "migrations" in resp.text.lower()


class TestAdminOrgProfileProgrammingError:
    def test_get_org_returns_501(self, client_admin: TestClient) -> None:
        resp = client_admin.get("/api/v1/admin/org")
        assert resp.status_code == 501
        assert "migrations" in resp.text.lower()

    def test_update_org_returns_501(self, client_admin: TestClient) -> None:
        resp = client_admin.put("/api/v1/admin/org", json={"name": "Updated Org"})
        assert resp.status_code == 501
        assert "migrations" in resp.text.lower()

    def test_regenerate_api_key_returns_501(self, client_admin: TestClient) -> None:
        resp = client_admin.post("/api/v1/admin/org/regenerate-api-key")
        assert resp.status_code == 501
        assert "migrations" in resp.text.lower()


class TestOrgDeletionProgrammingError:
    def test_request_deletion_returns_501(self, client_admin: TestClient) -> None:
        resp = client_admin.post("/api/v1/admin/org/deletion-request")
        assert resp.status_code == 501
        assert "migrations" in resp.text.lower()

    def test_confirm_deletion_returns_501(self, client_admin: TestClient) -> None:
        resp = client_admin.post("/api/v1/admin/org/deletion-confirm", json={"token": "test-token"})
        assert resp.status_code == 501
        assert "migrations" in resp.text.lower()

    def test_cancel_deletion_returns_501(self, client_admin: TestClient) -> None:
        resp = client_admin.patch("/api/v1/admin/org/deletion-cancel")
        assert resp.status_code == 501
        assert "migrations" in resp.text.lower()

    def test_export_org_data_returns_501(self, client_admin: TestClient) -> None:
        resp = client_admin.get("/api/v1/admin/org/export")
        assert resp.status_code == 501
        assert "migrations" in resp.text.lower()

    def test_delete_org_immediate_returns_501(self, client_admin: TestClient) -> None:
        resp = client_admin.delete("/api/v1/admin/org")
        assert resp.status_code == 501
        assert "migrations" in resp.text.lower()
