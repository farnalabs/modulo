"""Error-path unit tests for /api/v1/viewmodel/current and /api/v1/viewmodel/views."""

import uuid
from collections.abc import AsyncGenerator, Generator
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.exc import ProgrammingError

from modulo.api.dependencies import _get_engine, get_db_session
from modulo.api.main import app
from modulo.auth.dependencies import get_current_user
from modulo.auth.jwt import AuthenticatedPrincipal
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


def _make_mock_session() -> AsyncMock:
    session = AsyncMock()
    begin_cm = AsyncMock()
    begin_cm.__aenter__ = AsyncMock(return_value=None)
    begin_cm.__aexit__ = AsyncMock(return_value=False)
    session.begin = MagicMock(return_value=begin_cm)

    execute_result = MagicMock()
    scalars_mock = MagicMock()
    scalars_mock.all = MagicMock(return_value=[])
    execute_result.scalars.return_value = scalars_mock
    execute_result.scalar_one_or_none = AsyncMock(return_value=None)
    session.execute = AsyncMock(return_value=execute_result)
    return session


def _make_org(**overrides: object) -> MagicMock:
    org = MagicMock()
    org.id = overrides.get("id", _ORG_ID)
    org.name = overrides.get("name", "Test Org")
    org.settings_json = overrides.get("settings_json", {})
    org.daily_spend_limit = overrides.get("daily_spend_limit", None)
    return org


def _make_user(**overrides: object) -> MagicMock:
    user = MagicMock()
    user.id = overrides.get("id", _USER_ID)
    user.preferences = overrides.get("preferences", {})
    return user


def _make_mock_plan_context() -> MagicMock:
    ctx = MagicMock()
    flag = MagicMock()
    flag.name = "parallel_branches"
    flag.description = "Run branching logic in parallel within a pipeline"
    flag.tier = "community"
    flag.currently_active = True
    ctx.list_enabled_features = MagicMock(return_value=[flag])
    return ctx


@pytest.fixture()
def client() -> Generator[TestClient, None, None]:
    mock_session = _make_mock_session()

    async def override_session() -> AsyncGenerator[AsyncMock, None]:
        yield mock_session

    app.dependency_overrides[get_settings] = _make_settings
    app.dependency_overrides[get_db_session] = override_session
    app.dependency_overrides[_get_engine] = lambda: MagicMock()
    app.dependency_overrides[get_current_user] = lambda: AuthenticatedPrincipal(
        username="testuser",
        organisation_id=_ORG_ID,
        account_id=_USER_ID,
        org_role="admin",
    )
    yield TestClient(app)
    app.dependency_overrides.clear()


class TestViewModelCurrentErrorPaths:
    def test_missing_org_404(self, client: TestClient) -> None:
        user = _make_user()
        plan_ctx = _make_mock_plan_context()

        with (
            patch("modulo.api.routes.viewmodel.set_rls_org"),
            patch("modulo.api.routes.viewmodel.set_rls_user_context"),
            patch("modulo.api.routes.viewmodel.get_organisation", return_value=None),
            patch("modulo.api.routes.viewmodel.get_account_by_id", return_value=user),
            patch("modulo.api.routes.viewmodel.list_team_memberships_for_account", return_value=[]),
            patch("modulo.api.routes.viewmodel.resolve_plan_context", return_value=plan_ctx),
        ):
            resp = client.get("/api/v1/viewmodel/current")

        assert resp.status_code == 404
        assert resp.json()["detail"] == "Organisation not found"

    def test_missing_account_404(self, client: TestClient) -> None:
        org = _make_org()
        plan_ctx = _make_mock_plan_context()

        with (
            patch("modulo.api.routes.viewmodel.set_rls_org"),
            patch("modulo.api.routes.viewmodel.set_rls_user_context"),
            patch("modulo.api.routes.viewmodel.get_organisation", return_value=org),
            patch("modulo.api.routes.viewmodel.get_account_by_id", return_value=None),
            patch("modulo.api.routes.viewmodel.list_team_memberships_for_account", return_value=[]),
            patch("modulo.api.routes.viewmodel.resolve_plan_context", return_value=plan_ctx),
        ):
            resp = client.get("/api/v1/viewmodel/current")

        assert resp.status_code == 404
        assert resp.json()["detail"] == "Account not found"

    def test_view_as_team_team_not_found_404(self, client: TestClient) -> None:
        org = _make_org()
        user = _make_user()
        plan_ctx = _make_mock_plan_context()

        with (
            patch("modulo.api.routes.viewmodel.set_rls_org"),
            patch("modulo.api.routes.viewmodel.set_rls_user_context"),
            patch("modulo.api.routes.viewmodel.get_organisation", return_value=org),
            patch("modulo.api.routes.viewmodel.get_account_by_id", return_value=user),
            patch("modulo.api.routes.viewmodel.list_team_memberships_for_account", return_value=[]),
            patch("modulo.api.routes.viewmodel.resolve_plan_context", return_value=plan_ctx),
        ):
            team_id = uuid.uuid4()
            resp = client.get(f"/api/v1/viewmodel/current?view_as_team={team_id}")

        assert resp.status_code == 404
        assert resp.json()["detail"] == "Team not found"

    def test_view_as_team_no_org_400(self, client: TestClient) -> None:
        app.dependency_overrides[get_current_user] = lambda: AuthenticatedPrincipal(
            username="testuser",
            organisation_id=None,
            account_id=_USER_ID,
            org_role="admin",
        )
        team_id = uuid.uuid4()
        resp = client.get(f"/api/v1/viewmodel/current?view_as_team={team_id}")
        assert resp.status_code == 400
        assert resp.json()["detail"] == "Cannot use view_as_team without an organisation"
        app.dependency_overrides[get_current_user] = lambda: AuthenticatedPrincipal(
            username="testuser",
            organisation_id=_ORG_ID,
            account_id=_USER_ID,
            org_role="admin",
        )

    def test_no_org_not_admin_404(self, client: TestClient) -> None:
        app.dependency_overrides[get_current_user] = lambda: AuthenticatedPrincipal(
            username="testuser",
            organisation_id=None,
            account_id=_USER_ID,
            org_role="viewer",
            is_system_admin=False,
        )
        resp = client.get("/api/v1/viewmodel/current")
        assert resp.status_code == 404
        assert resp.json()["detail"] == "Organisation not found"
        app.dependency_overrides[get_current_user] = lambda: AuthenticatedPrincipal(
            username="testuser",
            organisation_id=_ORG_ID,
            account_id=_USER_ID,
            org_role="admin",
        )

    def test_programming_error_501(self, client: TestClient) -> None:
        org = _make_org()
        user = _make_user()

        with (
            patch("modulo.api.routes.viewmodel.set_rls_org"),
            patch("modulo.api.routes.viewmodel.set_rls_user_context"),
            patch("modulo.api.routes.viewmodel.get_organisation", return_value=org),
            patch("modulo.api.routes.viewmodel.get_account_by_id", return_value=user),
            patch("modulo.api.routes.viewmodel.list_team_memberships_for_account", return_value=[]),
            patch("modulo.api.routes.viewmodel.list_pipelines", side_effect=ProgrammingError("stmt", "params", "orig")),
        ):
            resp = client.get("/api/v1/viewmodel/current")

        assert resp.status_code == 501
        assert "migrations" in resp.json()["detail"].lower()


class TestViewModelViewsErrorPaths:
    def test_programming_error_501(self, client: TestClient) -> None:
        with (
            patch("modulo.api.routes.viewmodel.set_rls_org"),
            patch("modulo.api.routes.viewmodel.set_rls_user_context"),
            patch("modulo.api.routes.viewmodel.list_views", side_effect=ProgrammingError("stmt", "params", "orig")),
        ):
            resp = client.get("/api/v1/viewmodel/views")

        assert resp.status_code == 501
        assert "migrations" in resp.json()["detail"].lower()
