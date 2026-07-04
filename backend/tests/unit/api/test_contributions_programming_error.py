"""Unit tests: contribution API routes return 501 on ProgrammingError.

Tests that all 6 DB-accessing routes gracefully return 501 Not Implemented
when the database raises ProgrammingError (e.g. missing table because
migrations haven't run yet).
"""

from collections.abc import AsyncGenerator
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
_ORG_ID = "00000000-0000-0000-0000-000000000001"
_USER_ID = "00000000-0000-0000-0000-000000000002"
_PRIMITIVE_ID = "00000000-0000-0000-0000-000000000003"


def _make_settings() -> Settings:
    return Settings(
        database_url="postgresql+asyncpg://localhost/test",
        secret_key=_VALID_32,
        fernet_key=_VALID_32,
        modulo_admin_password="testpass",
    )


def _make_session_raising_programming_error() -> AsyncMock:
    session = AsyncMock()
    begin_cm = AsyncMock()
    begin_cm.__aenter__ = AsyncMock(side_effect=ProgrammingError("relation does not exist", None, None))
    begin_cm.__aexit__ = AsyncMock(return_value=False)
    session.begin = MagicMock(return_value=begin_cm)
    bind_mock = MagicMock()
    bind_mock.dialect.name = "postgresql"
    session.get_bind = AsyncMock(return_value=bind_mock)
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
    client = TestClient(app)
    yield client
    app.dependency_overrides.clear()


def _override_session(session) -> None:
    async def _get_session() -> AsyncGenerator[AsyncMock, None]:
        yield session

    app.dependency_overrides[get_db_session] = _get_session


class TestCreateContributionProgrammingError:
    """POST /api/v1/library/contribute → 501 on ProgrammingError.

    The route handler has its own async with session.begin():, so the
    mock session raising ProgrammingError triggers at the route level.
    """

    def test_create_contribution_returns_501_on_programming_error(self, admin_client: TestClient) -> None:
        session = _make_session_raising_programming_error()
        _override_session(session)
        resp = admin_client.post(
            "/api/v1/library/contribute",
            json={
                "name": "My Fixture",
                "slug": "my-fixture",
                "fixture_map": {"input": "output"},
            },
        )
        assert resp.status_code == 501
        assert "migrations" in resp.json()["detail"].lower()


class TestSubmitForReviewProgrammingError:
    """POST /api/v1/library/contribute/{id}/submit → 501 on ProgrammingError.

    Patches the service function because the route handler passes
    account_id=... but submit_contribution_for_review expects
    created_by=... (pre-existing bug in arg name).
    """

    def test_submit_for_review_returns_501_on_programming_error(self, admin_client: TestClient) -> None:
        with patch(
            "modulo.api.routes.contributions.submit_contribution_for_review",
            new_callable=AsyncMock,
            side_effect=ProgrammingError("mock", {}, ""),
        ):
            resp = admin_client.post(f"/api/v1/library/contribute/{_PRIMITIVE_ID}/submit")
        assert resp.status_code == 501
        assert "migrations" in resp.json()["detail"].lower()


class TestPublishContributionProgrammingError:
    """POST /api/v1/library/contribute/{id}/publish → 501 on ProgrammingError.

    Service function publish_contribution has its own async with session.begin():,
    so the mock session raising ProgrammingError triggers inside the service
    and propagates to the route handler's catch.
    """

    def test_publish_contribution_returns_501_on_programming_error(self, admin_client: TestClient) -> None:
        session = _make_session_raising_programming_error()
        _override_session(session)
        resp = admin_client.post(f"/api/v1/library/contribute/{_PRIMITIVE_ID}/publish")
        assert resp.status_code == 501
        assert "migrations" in resp.json()["detail"].lower()


class TestSubmitVersionProgrammingError:
    """POST /api/v1/library/contribute/{id}/versions → 501 on ProgrammingError.

    The route handler has its own async with session.begin():, so the
    mock session raising ProgrammingError triggers at the route level.
    """

    def test_submit_version_returns_501_on_programming_error(self, admin_client: TestClient) -> None:
        session = _make_session_raising_programming_error()
        _override_session(session)
        resp = admin_client.post(
            f"/api/v1/library/contribute/{_PRIMITIVE_ID}/versions",
            json={
                "name": "Versioned Fixture",
                "slug": "versioned-fixture",
                "fixture_map": {"prompt": "response"},
            },
        )
        assert resp.status_code == 501
        assert "migrations" in resp.json()["detail"].lower()


class TestListVersionsProgrammingError:
    """GET /api/v1/library/contribute/{id}/versions → 501 on ProgrammingError.

    Service function list_contribution_versions has its own async with
    session.begin():, so the mock session raising ProgrammingError triggers
    inside the service and propagates to the route handler's catch.
    """

    def test_list_versions_returns_501_on_programming_error(self, admin_client: TestClient) -> None:
        session = _make_session_raising_programming_error()
        _override_session(session)
        resp = admin_client.get(f"/api/v1/library/contribute/{_PRIMITIVE_ID}/versions")
        assert resp.status_code == 501
        assert "migrations" in resp.json()["detail"].lower()


class TestListContributionsProgrammingError:
    """GET /api/v1/library/contribute → 501 on ProgrammingError.

    The route handler has its own async with session.begin():, so the
    mock session raising ProgrammingError triggers at the route level.
    """

    def test_list_contributions_returns_501_on_programming_error(self, admin_client: TestClient) -> None:
        session = _make_session_raising_programming_error()
        _override_session(session)
        resp = admin_client.get("/api/v1/library/contribute")
        assert resp.status_code == 501
        assert "migrations" in resp.json()["detail"].lower()
