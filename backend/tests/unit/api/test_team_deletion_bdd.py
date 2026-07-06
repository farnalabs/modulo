"""Unit tests for team deletion endpoint — BDD scenario coverage."""

import uuid
from collections.abc import AsyncGenerator, Generator
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from modulo.api.dependencies import _get_engine, get_db_session, get_plan_context
from modulo.api.main import app
from modulo.auth.dependencies import get_current_user
from modulo.auth.jwt import AuthenticatedPrincipal
from modulo.settings import Settings, get_settings

_VALID_32 = "a" * 32
_ORG_ID = uuid.UUID("00000000-0000-0000-0000-000000000001")
_USER_ID = uuid.UUID("00000000-0000-0000-0000-000000000002")
_TEAM_ID = uuid.UUID("00000000-0000-0000-0000-000000000003")


def _make_settings() -> Settings:
    return Settings(
        database_url="postgresql+asyncpg://localhost/test",
        secret_key=_VALID_32,
        fernet_key=_VALID_32,
        modulo_admin_password="testpass",
        modulo_license_key="test-license-key",
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
    mock_plan = MagicMock()
    mock_plan.feature_enabled.return_value = True
    app.dependency_overrides[get_plan_context] = lambda: mock_plan
    yield TestClient(app)
    app.dependency_overrides.clear()


@pytest.fixture()
def viewer_client() -> Generator[TestClient, None, None]:
    mock_session = _make_mock_session()

    async def override_session() -> AsyncGenerator[AsyncMock, None]:
        yield mock_session

    app.dependency_overrides[get_settings] = _make_settings
    app.dependency_overrides[get_db_session] = override_session
    app.dependency_overrides[_get_engine] = lambda: MagicMock()
    app.dependency_overrides[get_current_user] = lambda: AuthenticatedPrincipal(
        username="viewer",
        organisation_id=_ORG_ID,
        account_id=_USER_ID,
        org_role="viewer",
    )
    mock_plan = MagicMock()
    mock_plan.feature_enabled.return_value = True
    app.dependency_overrides[get_plan_context] = lambda: mock_plan
    yield TestClient(app)
    app.dependency_overrides.clear()


class TestTeamDeletionBDD:
    """BDD-aligned unit tests for team deletion endpoint."""

    def test_delete_team_no_active_runs_succeeds(self, client: TestClient) -> None:
        with (
            patch("modulo.api.routes.teams.delete_team", return_value=True),
            patch("modulo.api.routes.teams.set_rls_org"),
            patch("modulo.api.routes.teams.set_rls_user_context"),
        ):
            resp = client.delete(f"/api/v1/teams/{_TEAM_ID}")
        assert resp.status_code == 204

    def test_delete_team_with_active_runs_blocked(self, client: TestClient) -> None:
        with (
            patch(
                "modulo.api.routes.teams.delete_team",
                side_effect=HTTPException(
                    status_code=409,
                    detail="Cannot delete team with 2 active runs",
                ),
            ),
            patch("modulo.api.routes.teams.set_rls_org"),
            patch("modulo.api.routes.teams.set_rls_user_context"),
        ):
            resp = client.delete(f"/api/v1/teams/{_TEAM_ID}")
        assert resp.status_code == 409
        assert "active run" in resp.json()["detail"].lower()

    def test_error_message_shows_run_count(self, client: TestClient) -> None:
        with (
            patch(
                "modulo.api.routes.teams.delete_team",
                side_effect=HTTPException(
                    status_code=409,
                    detail="Cannot delete team with 5 active runs",
                ),
            ),
            patch("modulo.api.routes.teams.set_rls_org"),
            patch("modulo.api.routes.teams.set_rls_user_context"),
        ):
            resp = client.delete(f"/api/v1/teams/{_TEAM_ID}")
        assert resp.status_code == 409
        assert "5 active run" in resp.json()["detail"]

    def test_non_admin_returns_403(self, viewer_client: TestClient) -> None:
        resp = viewer_client.delete(f"/api/v1/teams/{_TEAM_ID}")
        assert resp.status_code == 403

    def test_not_found_returns_404(self, client: TestClient) -> None:
        with (
            patch("modulo.api.routes.teams.delete_team", return_value=False),
            patch("modulo.api.routes.teams.set_rls_org"),
            patch("modulo.api.routes.teams.set_rls_user_context"),
        ):
            resp = client.delete(f"/api/v1/teams/{uuid.uuid4()}")
        assert resp.status_code == 404
