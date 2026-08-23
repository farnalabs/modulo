"""Tests for the admin system config API."""

from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy.exc import IntegrityError, ProgrammingError

from modulo.api.dependencies import get_db_session, get_plan_context
from modulo.auth.dependencies import get_current_user
from modulo.auth.jwt import AuthenticatedPrincipal
from tests.unit.api.mock_session import configure_mock_session

ORG_ID = uuid4()
USER_ID = uuid4()
SYSTEM_ADMIN = AuthenticatedPrincipal(
    username="sysadmin@test",
    organisation_id=ORG_ID,
    account_id=USER_ID,
    org_role="admin",
    is_system_admin=True,
)
REGULAR_ADMIN = AuthenticatedPrincipal(
    username="admin@test",
    organisation_id=ORG_ID,
    account_id=uuid4(),
    org_role="admin",
    is_system_admin=False,
)


@pytest.fixture
def mock_session():
    from unittest.mock import MagicMock

    session = AsyncMock()
    configure_mock_session(session)
    begin_cm = AsyncMock()
    begin_cm.__aenter__ = AsyncMock(return_value=None)
    begin_cm.__aexit__ = AsyncMock(return_value=False)
    session.begin = MagicMock(return_value=begin_cm)
    session.flush.return_value = None
    execute_result = MagicMock()
    execute_result.scalar_one_or_none.return_value = None
    execute_result.scalars.return_value.all.return_value = []
    session.execute.return_value = execute_result
    return session


@pytest.fixture
def client_sys_admin(mock_session):
    from modulo.api.main import app

    mock_plan = MagicMock()
    mock_plan.feature_enabled.return_value = True
    app.dependency_overrides[get_plan_context] = lambda: mock_plan
    app.dependency_overrides[get_db_session] = lambda: mock_session
    app.dependency_overrides[get_current_user] = lambda: SYSTEM_ADMIN
    transport = ASGITransport(app=app)
    client = AsyncClient(transport=transport, base_url="http://test")
    yield client
    app.dependency_overrides.clear()


@pytest.fixture
def client_regular_admin(mock_session):
    from modulo.api.main import app

    mock_plan = MagicMock()
    mock_plan.feature_enabled.return_value = True
    app.dependency_overrides[get_plan_context] = lambda: mock_plan
    app.dependency_overrides[get_db_session] = lambda: mock_session
    app.dependency_overrides[get_current_user] = lambda: REGULAR_ADMIN
    transport = ASGITransport(app=app)
    client = AsyncClient(transport=transport, base_url="http://test")
    yield client
    app.dependency_overrides.clear()


class TestAdminListConfig:
    @pytest.mark.anyio
    async def test_system_admin_can_list(self, client_sys_admin, mock_session):
        resp = await client_sys_admin.get("/api/v1/system-admin/config")
        assert resp.status_code == 200
        assert not resp.json()

    @pytest.mark.anyio
    async def test_regular_admin_gets_403(self, client_regular_admin):
        resp = await client_regular_admin.get("/api/v1/system-admin/config")
        assert resp.status_code == 403

    @pytest.mark.anyio
    async def test_returns_entries(self, client_sys_admin, mock_session):
        from modulo.db.models.system_config import SystemConfig

        entry = SystemConfig(key="app_name", value="modulo")
        mock_session.execute.return_value.scalars.return_value.all.return_value = [entry]
        resp = await client_sys_admin.get("/api/v1/system-admin/config")
        assert resp.status_code == 200
        data = resp.json()
        assert len(data) == 1
        assert data[0]["key"] == "app_name"
        assert data[0]["value"] == "modulo"

    @pytest.mark.anyio
    async def test_sensitive_key_value_is_masked(self, client_sys_admin, mock_session):
        from modulo.db.models.system_config import SystemConfig

        entries = [
            SystemConfig(key="slack_api_token", value="xoxb-123"),
            SystemConfig(key="app_name", value="modulo"),
        ]
        mock_session.execute.return_value.scalars.return_value.all.return_value = entries
        resp = await client_sys_admin.get("/api/v1/system-admin/config")
        assert resp.status_code == 200
        data = {e["key"]: e["value"] for e in resp.json()}
        assert data["slack_api_token"] == "\u2022\u2022\u2022\u2022\u2022\u2022"
        assert data["app_name"] == "modulo"

    @pytest.mark.anyio
    async def test_non_string_sensitive_value_not_masked(self, client_sys_admin, mock_session):
        from modulo.db.models.system_config import SystemConfig

        entry = SystemConfig(key="feature_flags", value={"analytics": True})
        mock_session.execute.return_value.scalars.return_value.all.return_value = [entry]
        resp = await client_sys_admin.get("/api/v1/system-admin/config")
        assert resp.status_code == 200
        data = resp.json()
        assert data[0]["value"] == {"analytics": True}

    @pytest.mark.anyio
    async def test_list_config_programming_error_returns_501(self, client_sys_admin, mock_session):
        mock_session.execute.side_effect = ProgrammingError("", "", "")
        resp = await client_sys_admin.get("/api/v1/system-admin/config")
        assert resp.status_code == 501


class TestAdminSetConfig:
    @pytest.mark.anyio
    async def test_system_admin_can_set(self, client_sys_admin, mock_session):
        from modulo.db.models.system_config import SystemConfig

        # New set_config first-write path: SELECT … FOR UPDATE (None → first
        # write), INSERT … ON CONFLICT DO NOTHING, then re-SELECT the stored row
        # via scalar_one(). Mirror that re-SELECT with a real SystemConfig so the
        # response round-trips.
        mock_session.execute.return_value.scalar_one_or_none.return_value = None
        mock_session.execute.return_value.scalar_one.return_value = SystemConfig(key="my_key", value="my_value")
        resp = await client_sys_admin.put(
            "/api/v1/system-admin/config/my_key",
            json={"value": "my_value"},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["key"] == "my_key"
        assert data["value"] == "my_value"

    @pytest.mark.anyio
    async def test_regular_admin_gets_403(self, client_regular_admin):
        resp = await client_regular_admin.put(
            "/api/v1/system-admin/config/my_key",
            json={"value": "my_value"},
        )
        assert resp.status_code == 403

    @pytest.mark.anyio
    async def test_updates_existing(self, client_sys_admin, mock_session):
        from modulo.db.models.system_config import SystemConfig

        existing = SystemConfig(key="my_key", value="old")
        mock_session.execute.return_value.scalar_one_or_none.return_value = existing
        # admin_set_config now routes through update_config (ON CONFLICT DO
        # UPDATE); the re-SELECT returns the post-update row.
        mock_session.execute.return_value.scalar_one.return_value = SystemConfig(key="my_key", value="updated")
        resp = await client_sys_admin.put(
            "/api/v1/system-admin/config/my_key",
            json={"value": "updated"},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["key"] == "my_key"
        assert data["value"] == "updated"

    @pytest.mark.anyio
    async def test_set_config_integrity_error_returns_409(self, client_sys_admin, mock_session):
        mock_session.execute.side_effect = IntegrityError("", "", "")
        resp = await client_sys_admin.put(
            "/api/v1/system-admin/config/my_key",
            json={"value": "my_value"},
        )
        assert resp.status_code == 409

    @pytest.mark.anyio
    async def test_set_config_programming_error_returns_501(self, client_sys_admin, mock_session):
        mock_session.execute.side_effect = ProgrammingError("", "", "")
        resp = await client_sys_admin.put(
            "/api/v1/system-admin/config/my_key",
            json={"value": "my_value"},
        )
        assert resp.status_code == 501


class TestAdminDeleteConfig:
    @pytest.mark.anyio
    async def test_system_admin_can_delete(self, client_sys_admin, mock_session):
        from modulo.db.models.system_config import SystemConfig

        existing = SystemConfig(key="del_key", value="val")
        mock_session.execute.return_value.scalar_one_or_none.return_value = existing
        resp = await client_sys_admin.delete("/api/v1/system-admin/config/del_key")
        assert resp.status_code == 204

    @pytest.mark.anyio
    async def test_regular_admin_gets_403(self, client_regular_admin):
        resp = await client_regular_admin.delete("/api/v1/system-admin/config/del_key")
        assert resp.status_code == 403

    @pytest.mark.anyio
    async def test_nonexistent_returns_404(self, client_sys_admin, mock_session):
        mock_session.execute.return_value.scalar_one_or_none.return_value = None
        resp = await client_sys_admin.delete("/api/v1/system-admin/config/missing")
        assert resp.status_code == 404

    @pytest.mark.anyio
    async def test_delete_config_programming_error_returns_501(self, client_sys_admin, mock_session):
        mock_session.execute.side_effect = ProgrammingError("", "", "")
        resp = await client_sys_admin.delete("/api/v1/system-admin/config/some_key")
        assert resp.status_code == 501
