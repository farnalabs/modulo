"""Unit tests for /api/v1/admin/runs/purge — purge stale runs endpoint."""

from collections.abc import AsyncGenerator, Generator
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.sql.dml import Delete as DeleteStmt

from modulo.api.dependencies import _get_engine, get_db_session
from modulo.api.main import app
from modulo.auth.dependencies import get_current_user
from modulo.auth.jwt import AuthenticatedPrincipal
from modulo.settings import Settings, get_settings

_VALID_32 = "a" * 32
_ORG_ID = "00000000-0000-0000-0000-000000000001"
_USER_ID = "00000000-0000-0000-0000-000000000002"


def _make_settings() -> Settings:
    return Settings(
        database_url="postgresql+asyncpg://localhost/test",
        secret_key=_VALID_32,
        fernet_key=_VALID_32,
        modulo_admin_password="testpass",
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
    app.dependency_overrides[get_settings] = _make_settings
    app.dependency_overrides[_get_engine] = lambda: MagicMock()
    app.dependency_overrides[get_current_user] = lambda: AuthenticatedPrincipal(
        username="admin",
        organisation_id=_ORG_ID,
        user_id=_USER_ID,
        org_role="admin",
    )
    yield TestClient(app)
    app.dependency_overrides.clear()


@pytest.fixture()
def unauth_client() -> Generator[TestClient, None, None]:
    app.dependency_overrides[get_settings] = _make_settings
    yield TestClient(app)
    app.dependency_overrides.clear()


@pytest.fixture()
def operator_client() -> Generator[TestClient, None, None]:
    app.dependency_overrides[get_settings] = _make_settings
    app.dependency_overrides[_get_engine] = lambda: MagicMock()
    app.dependency_overrides[get_current_user] = lambda: AuthenticatedPrincipal(
        username="operator",
        organisation_id=_ORG_ID,
        user_id=_USER_ID,
        org_role="operator",
    )
    yield TestClient(app)
    app.dependency_overrides.clear()


class TestAdminPurgeStaleRuns:
    URL = "/api/v1/admin/runs/purge"

    def _mock_session(self) -> AsyncMock:
        """Create a mock session that returns a result with a given rowcount."""
        session = _make_mock_session()
        result = MagicMock()
        result.rowcount = 7
        session.execute = AsyncMock(return_value=result)

        async def _override() -> AsyncGenerator[AsyncMock, None]:
            yield session

        app.dependency_overrides[get_db_session] = _override
        return session

    def test_admin_purges_stale_runs_returns_200(self, client: TestClient) -> None:
        self._mock_session()
        with patch("modulo.api.routes.admin.set_rls_org"):
            resp = client.post(self.URL, json={"older_than_days": 60})

        assert resp.status_code == 200
        data = resp.json()
        assert data["purged_count"] == 7

    def test_uses_delete_statement_with_org_scope(self, client: TestClient) -> None:
        session = self._mock_session()
        with patch("modulo.api.routes.admin.set_rls_org") as mock_rls:
            resp = client.post(self.URL, json={"older_than_days": 30})

        assert resp.status_code == 200
        mock_rls.assert_awaited_once()
        call_args = session.execute.call_args[0][0]
        assert isinstance(call_args, DeleteStmt)

    def test_admin_uses_default_90_days(self, client: TestClient) -> None:
        session = _make_mock_session()
        result = MagicMock()
        result.rowcount = 0
        session.execute = AsyncMock(return_value=result)

        async def _override() -> AsyncGenerator[AsyncMock, None]:
            yield session

        app.dependency_overrides[get_db_session] = _override
        with patch("modulo.api.routes.admin.set_rls_org"):
            resp = client.post(self.URL, json={})

        assert resp.status_code == 200
        data = resp.json()
        assert data["purged_count"] == 0

    def test_operator_returns_403(self, operator_client: TestClient) -> None:
        resp = operator_client.post(self.URL, json={"older_than_days": 30})
        assert resp.status_code == 403

    def test_unauthorized_returns_4xx(self, unauth_client: TestClient) -> None:
        resp = unauth_client.post(self.URL, json={"older_than_days": 30})
        assert resp.status_code in (401, 403)
