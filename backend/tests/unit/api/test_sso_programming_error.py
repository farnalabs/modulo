"""Unit tests: ProgrammingError on SSO admin routes returns 501."""
import uuid
from collections.abc import AsyncGenerator, Generator
from unittest.mock import AsyncMock, MagicMock, patch

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
_PROVIDER_ID = uuid.UUID("00000000-0000-0000-0000-000000000010")


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


@pytest.fixture()
def client() -> Generator[TestClient, None, None]:
    mock_session = AsyncMock()
    begin_cm = AsyncMock()
    begin_cm.__aenter__ = AsyncMock(return_value=None)
    begin_cm.__aexit__ = AsyncMock(return_value=False)
    mock_session.begin = MagicMock(return_value=begin_cm)

    async def override_session() -> AsyncGenerator[AsyncMock, None]:
        yield mock_session

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


class TestListProvidersProgrammingError:
    @patch("modulo.api.routes.admin_sso.list_providers", new=AsyncMock(side_effect=ProgrammingError("mock", {}, "")))
    def test_list_providers_returns_501(self, client: TestClient) -> None:
        resp = client.get("/api/v1/admin/sso/providers")
        assert resp.status_code == 501
        assert "migrations" in resp.text.lower()


class TestCreateProviderProgrammingError:
    @patch("modulo.api.routes.admin_sso.create_provider", new=AsyncMock(side_effect=ProgrammingError("mock", {}, "")))
    def test_create_provider_returns_501(self, client: TestClient) -> None:
        resp = client.post(
            "/api/v1/admin/sso/providers",
            json={"provider_type": "oidc", "name": "Test Provider"},
        )
        assert resp.status_code == 501
        assert "migrations" in resp.text.lower()


class TestUpdateProviderProgrammingError:
    @patch("modulo.api.routes.admin_sso.update_provider", new=AsyncMock(side_effect=ProgrammingError("mock", {}, "")))
    def test_update_provider_returns_501(self, client: TestClient) -> None:
        resp = client.put(
            f"/api/v1/admin/sso/providers/{_PROVIDER_ID}",
            json={"name": "Updated"},
        )
        assert resp.status_code == 501
        assert "migrations" in resp.text.lower()


class TestDeleteProviderProgrammingError:
    @patch("modulo.api.routes.admin_sso.delete_provider", new=AsyncMock(side_effect=ProgrammingError("mock", {}, "")))
    def test_delete_provider_returns_501(self, client: TestClient) -> None:
        resp = client.delete(f"/api/v1/admin/sso/providers/{_PROVIDER_ID}")
        assert resp.status_code == 501
        assert "migrations" in resp.text.lower()


class TestToggleProviderProgrammingError:
    @patch("modulo.api.routes.admin_sso.toggle_provider", new=AsyncMock(side_effect=ProgrammingError("mock", {}, "")))
    def test_toggle_provider_returns_501(self, client: TestClient) -> None:
        resp = client.put(f"/api/v1/admin/sso/providers/{_PROVIDER_ID}/toggle")
        assert resp.status_code == 501
        assert "migrations" in resp.text.lower()


class TestTestConnectionProgrammingError:
    @patch("modulo.api.routes.admin_sso.get_provider", new=AsyncMock(side_effect=ProgrammingError("mock", {}, "")))
    @patch("modulo.api.routes.admin_sso.set_group_mappings", new=AsyncMock())
    def test_test_connection_returns_501(self, client: TestClient) -> None:
        resp = client.post(f"/api/v1/admin/sso/providers/{_PROVIDER_ID}/test")
        assert resp.status_code == 501
        assert "migrations" in resp.text.lower()


class TestSetGroupMappingsProgrammingError:
    @patch("modulo.api.routes.admin_sso.set_group_mappings", new=AsyncMock(side_effect=ProgrammingError("mock", {}, "")))
    def test_set_group_mappings_returns_501(self, client: TestClient) -> None:
        resp = client.put(
            f"/api/v1/admin/sso/providers/{_PROVIDER_ID}/group-mappings",
            json={"mappings": []},
        )
        assert resp.status_code == 501
        assert "migrations" in resp.text.lower()


class TestGetGroupMappingsProgrammingError:
    @patch("modulo.api.routes.admin_sso.get_provider", new=AsyncMock(side_effect=ProgrammingError("mock", {}, "")))
    def test_get_group_mappings_returns_501(self, client: TestClient) -> None:
        resp = client.get(f"/api/v1/admin/sso/providers/{_PROVIDER_ID}/group-mappings")
        assert resp.status_code == 501
        assert "migrations" in resp.text.lower()
