"""Unit tests for SQLAlchemyError→503 and IntegrityError→409 handling on Team RBAC routes."""

import uuid
from collections.abc import AsyncGenerator
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.exc import IntegrityError, SQLAlchemyError

from modulo.api.dependencies import _get_engine, get_db_session, get_plan_context
from modulo.api.main import app
from modulo.auth.dependencies import get_current_user
from modulo.auth.jwt import AuthenticatedPrincipal
from modulo.settings import Settings, get_settings

_VALID_32 = "a" * 32
_ORG_ID = uuid.UUID("00000000-0000-0000-0000-000000000001")
_USER_ID = uuid.UUID("00000000-0000-0000-0000-000000000002")
_TEAM_ID = uuid.UUID("00000000-0000-0000-0000-000000000003")
_MEMBER_ID = uuid.UUID("00000000-0000-0000-0000-000000000004")


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


def _make_session_raising(error_cls: type[Exception]) -> AsyncMock:
    session = AsyncMock()
    begin_cm = AsyncMock()
    begin_cm.__aenter__ = AsyncMock(side_effect=error_cls("mock error", None, None))
    begin_cm.__aexit__ = AsyncMock(return_value=False)
    session.begin = MagicMock(return_value=begin_cm)
    return session


def _make_session_raising_in_create(error_cls: type[Exception]) -> AsyncMock:
    """Session that fails on add_team_member / create_team session.flush."""
    session = AsyncMock()
    begin_cm = AsyncMock()
    begin_cm.__aenter__ = AsyncMock(return_value=None)
    begin_cm.__aexit__ = AsyncMock(return_value=False)
    session.begin = MagicMock(return_value=begin_cm)
    session.flush = AsyncMock(side_effect=error_cls("mock error", None, None))
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


class TestTeamsSQLAlchemyError:
    """All /api/v1/teams routes return 503 on SQLAlchemyError."""

    def test_list_teams_returns_503(self, admin_client: TestClient) -> None:
        _override_session(_make_session_raising(SQLAlchemyError))
        resp = admin_client.get("/api/v1/teams")
        assert resp.status_code == 503

    def test_create_team_returns_503(self, admin_client: TestClient) -> None:
        _override_session(_make_session_raising(SQLAlchemyError))
        resp = admin_client.post("/api/v1/teams", json={"name": "New Team"})
        assert resp.status_code == 503

    def test_get_team_returns_503(self, admin_client: TestClient) -> None:
        _override_session(_make_session_raising(SQLAlchemyError))
        resp = admin_client.get(f"/api/v1/teams/{_TEAM_ID}")
        assert resp.status_code == 503

    def test_update_team_returns_503(self, admin_client: TestClient) -> None:
        _override_session(_make_session_raising(SQLAlchemyError))
        resp = admin_client.patch(f"/api/v1/teams/{_TEAM_ID}", json={"name": "Updated"})
        assert resp.status_code == 503

    def test_delete_team_returns_503(self, admin_client: TestClient) -> None:
        _override_session(_make_session_raising(SQLAlchemyError))
        resp = admin_client.delete(f"/api/v1/teams/{_TEAM_ID}")
        assert resp.status_code == 503

    def test_list_members_returns_503(self, admin_client: TestClient) -> None:
        _override_session(_make_session_raising(SQLAlchemyError))
        resp = admin_client.get(f"/api/v1/teams/{_TEAM_ID}/members")
        assert resp.status_code == 503

    def test_add_member_returns_503(self, admin_client: TestClient) -> None:
        _override_session(_make_session_raising(SQLAlchemyError))
        resp = admin_client.post(
            f"/api/v1/teams/{_TEAM_ID}/members",
            json={"user_id": str(_USER_ID), "role": "viewer"},
        )
        assert resp.status_code == 503

    def test_remove_member_returns_503(self, admin_client: TestClient) -> None:
        _override_session(_make_session_raising(SQLAlchemyError))
        resp = admin_client.delete(f"/api/v1/teams/{_TEAM_ID}/members/{_MEMBER_ID}")
        assert resp.status_code == 503

    def test_change_member_role_returns_503(self, admin_client: TestClient) -> None:
        _override_session(_make_session_raising(SQLAlchemyError))
        resp = admin_client.patch(
            f"/api/v1/teams/{_TEAM_ID}/members/{_MEMBER_ID}",
            json={"role": "runner"},
        )
        assert resp.status_code == 503


class TestTeamsIntegrityError:
    """Team creation returns 409 on IntegrityError (concurrent duplicate name)."""

    def test_create_team_returns_409_on_integrity_error(self, admin_client: TestClient) -> None:
        _override_session(_make_session_raising_in_create(IntegrityError))
        resp = admin_client.post("/api/v1/teams", json={"name": "Duplicate Team"})
        assert resp.status_code == 409
        assert "already exists" in resp.json()["detail"].lower()
