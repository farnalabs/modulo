"""Unit tests verifying set_rls_user_context is called on all 4 admin team routes."""

import uuid
from collections.abc import AsyncGenerator
from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from modulo.api.dependencies import _get_engine, get_db_session, get_plan_context
from modulo.api.main import app
from modulo.auth.dependencies import get_current_user
from modulo.auth.jwt import AuthenticatedPrincipal
from modulo.settings import Settings, get_settings
from tests.unit.api.mock_session import configure_mock_session

_VALID_32 = "a" * 32
_ORG_ID = uuid.UUID("00000000-0000-0000-0000-000000000001")
_USER_ID = uuid.UUID("00000000-0000-0000-0000-000000000002")
_TEAM_ID = uuid.UUID("00000000-0000-0000-0000-000000000003")


class _EnterprisePlan:
    def feature_enabled(self, name: str) -> bool:
        return True

    def list_enabled_features(self) -> list:
        return []


def _make_settings() -> Settings:
    return Settings(
        database_url="postgresql+asyncpg://localhost/test",
        secret_key=_VALID_32,
        fernet_key=_VALID_32,
        modulo_admin_password="testpass",
    )


class _MockResult:
    def all(self):
        return []

    def scalar(self):
        return 0

    def scalar_one_or_none(self):
        return None


def _make_team() -> MagicMock:
    team = MagicMock()
    team.id = uuid.uuid4()
    team.name = "Test Team"
    team.description = None
    team.account_id = _USER_ID
    team.created_at = datetime.now(UTC)
    return team


def _make_session() -> AsyncMock:
    session = configure_mock_session(AsyncMock())
    begin_cm = AsyncMock()
    begin_cm.__aenter__ = AsyncMock(return_value=None)
    begin_cm.__aexit__ = AsyncMock(return_value=False)
    session.begin = MagicMock(return_value=begin_cm)
    begin_nested_cm = AsyncMock()
    begin_nested_cm.__aenter__ = AsyncMock(return_value=None)
    begin_nested_cm.__aexit__ = AsyncMock(return_value=False)
    session.begin_nested = MagicMock(return_value=begin_nested_cm)
    session.execute = AsyncMock(return_value=_MockResult())
    return session


@pytest.fixture()
def admin_client() -> TestClient:
    app.dependency_overrides[get_settings] = _make_settings
    app.dependency_overrides[_get_engine] = lambda: MagicMock()
    app.dependency_overrides[get_current_user] = lambda: AuthenticatedPrincipal(
        username="admin",
        organisation_id=_ORG_ID,
        account_id=_USER_ID,
        org_role="admin",
    )
    app.dependency_overrides[get_plan_context] = lambda: _EnterprisePlan()
    client = TestClient(app)
    yield client
    app.dependency_overrides.clear()


def _override_session(session: AsyncMock) -> None:
    async def _get_session() -> AsyncGenerator[AsyncMock, None]:
        yield session

    app.dependency_overrides[get_db_session] = _get_session


class TestAdminTeamRlsUserContext:
    """Verifies set_rls_user_context is called on all 4 admin team routes."""

    def test_admin_create_team_calls_set_rls_user_context(self, admin_client: TestClient) -> None:
        session = _make_session()
        _override_session(session)
        team = _make_team()

        with (
            patch("modulo.api.routes.admin.set_rls_user_context", new_callable=AsyncMock) as mock_rls_user,
            patch("modulo.api.routes.admin.set_rls_org", new_callable=AsyncMock),
            patch("modulo.api.routes.admin.get_team_by_name", new_callable=AsyncMock, return_value=None),
            patch("modulo.api.routes.admin.create_team", new_callable=AsyncMock, return_value=team),
        ):
            resp = admin_client.post("/api/v1/admin/teams", json={"name": "New Team"})

        assert resp.status_code == 201
        assert mock_rls_user.call_count >= 1
        mock_rls_user.assert_any_call(session, _USER_ID, "admin")

    def test_admin_list_teams_calls_set_rls_user_context(self, admin_client: TestClient) -> None:
        session = _make_session()
        _override_session(session)

        paginated = MagicMock()
        paginated.items = []
        paginated.total = 0
        paginated.page = 1
        paginated.page_size = 20

        with (
            patch("modulo.api.routes.admin.set_rls_user_context", new_callable=AsyncMock) as mock_rls_user,
            patch("modulo.api.routes.admin.set_rls_org", new_callable=AsyncMock),
            patch("modulo.api.routes.admin.list_teams", new_callable=AsyncMock, return_value=paginated),
        ):
            resp = admin_client.get("/api/v1/admin/teams")

        assert resp.status_code == 200
        assert mock_rls_user.call_count >= 1
        mock_rls_user.assert_any_call(session, _USER_ID, "admin")

    def test_admin_update_team_calls_set_rls_user_context(self, admin_client: TestClient) -> None:
        session = _make_session()
        _override_session(session)
        team = _make_team()

        with (
            patch("modulo.api.routes.admin.set_rls_user_context", new_callable=AsyncMock) as mock_rls_user,
            patch("modulo.api.routes.admin.set_rls_org", new_callable=AsyncMock),
            patch("modulo.api.routes.admin.get_team_by_name", new_callable=AsyncMock, return_value=None),
            patch("modulo.api.routes.admin.crud_update_team", new_callable=AsyncMock, return_value=team),
        ):
            resp = admin_client.put(f"/api/v1/admin/teams/{_TEAM_ID}", json={"name": "Updated Team"})

        assert resp.status_code == 200
        assert mock_rls_user.call_count >= 1
        mock_rls_user.assert_any_call(session, _USER_ID, "admin")

    def test_admin_delete_team_calls_set_rls_user_context(self, admin_client: TestClient) -> None:
        session = _make_session()
        _override_session(session)

        with (
            patch("modulo.api.routes.admin.set_rls_user_context", new_callable=AsyncMock) as mock_rls_user,
            patch("modulo.api.routes.admin.set_rls_org", new_callable=AsyncMock),
            patch("modulo.api.routes.admin.delete_team", new_callable=AsyncMock, return_value=True),
        ):
            resp = admin_client.delete(f"/api/v1/admin/teams/{_TEAM_ID}")

        assert resp.status_code == 204
        assert mock_rls_user.call_count >= 1
        mock_rls_user.assert_any_call(session, _USER_ID, "admin")
