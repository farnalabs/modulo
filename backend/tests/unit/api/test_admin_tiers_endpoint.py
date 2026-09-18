"""Unit tests for /api/v1/admin/tiers — admin tiers catalog listing.

FAR-880: the listing now carries ``require_permission("org.config")``
(viewer minimum) so all tenant roles keep reading the plan tier catalog
(the frontend plan store calls this for every logged-in user).
"""

import uuid
from collections.abc import AsyncGenerator, Generator
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from modulo.api.dependencies import _get_engine, get_db_session
from modulo.api.main import app
from modulo.auth.dependencies import get_current_tenant_user
from modulo.auth.jwt import TenantPrincipal
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
        redis_url="",
    )


def _make_mock_session() -> AsyncMock:
    session = AsyncMock()
    begin_cm = AsyncMock()
    begin_cm.__aenter__ = AsyncMock(return_value=None)
    begin_cm.__aexit__ = AsyncMock(return_value=False)
    session.begin = MagicMock(return_value=begin_cm)
    return session


@pytest.fixture
def client() -> Generator[TestClient, None, None]:
    mock_session = _make_mock_session()

    async def override_session() -> AsyncGenerator[AsyncMock, None]:
        yield mock_session

    app.dependency_overrides[get_settings] = _make_settings
    app.dependency_overrides[get_db_session] = override_session
    app.dependency_overrides[_get_engine] = lambda: MagicMock()
    yield TestClient(app)
    app.dependency_overrides.clear()


def _set_tenant_principal(role: str) -> None:
    app.dependency_overrides[get_current_tenant_user] = lambda: TenantPrincipal(
        username="tenantuser",
        organisation_id=_ORG_ID,
        account_id=_USER_ID,
        org_role=role,
    )


class TestListTiers:
    def test_viewer_allowed(self, client: TestClient) -> None:
        _set_tenant_principal("viewer")
        with patch("modulo.api.routes.admin_tiers.list_tiers", new=AsyncMock()) as mock_list:
            mock_list.return_value = [{"name": "Alpha", "display_name": "Alpha"}]
            resp = client.get("/api/v1/admin/tiers")
        assert resp.status_code == 200
        assert resp.json() == {"tiers": [{"name": "Alpha", "display_name": "Alpha"}]}
        mock_list.assert_awaited_once()

    def test_admin_allowed(self, client: TestClient) -> None:
        _set_tenant_principal("admin")
        with patch("modulo.api.routes.admin_tiers.list_tiers", new=AsyncMock()) as mock_list:
            mock_list.return_value = []
            resp = client.get("/api/v1/admin/tiers")
        assert resp.status_code == 200
        assert resp.json() == {"tiers": []}

    def test_unauthenticated_returns_4xx(self, client: TestClient) -> None:
        resp = client.get("/api/v1/admin/tiers")
        assert resp.status_code in (401, 403)

    def test_endpoint_carries_org_config_permission(self) -> None:
        import inspect

        from modulo.api.routes.admin_tiers import list_tiers_endpoint

        params = inspect.signature(list_tiers_endpoint).parameters
        dep = params["current_user"].default
        assert getattr(dep, "permission", None) == "org.config"
        assert getattr(dep, "permission_kind", None) == "tenant"
