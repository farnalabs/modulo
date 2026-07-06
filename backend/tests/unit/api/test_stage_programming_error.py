"""Unit tests for /api/v1/stages endpoints — ProgrammingError → 501, SQLAlchemyError → 503."""

import uuid
from collections.abc import AsyncGenerator, Generator
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.exc import ProgrammingError, SQLAlchemyError

from modulo.api.dependencies import _get_engine, get_db_session, get_plan_context
from modulo.api.main import app
from modulo.auth.dependencies import get_current_user
from modulo.auth.jwt import AuthenticatedPrincipal
from modulo.settings import Settings, get_settings

_VALID_32 = "a" * 32
_ORG_ID = uuid.UUID("00000000-0000-0000-0000-000000000001")
_USER_ID = uuid.UUID("00000000-0000-0000-0000-000000000002")
_STAGE_ID = uuid.UUID("00000000-0000-0000-0000-000000000003")
_DB_ERROR = SQLAlchemyError("mock connection error")
_PROG_ERROR = ProgrammingError("mock", "mock", "mock")


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


class TestListStagesProgrammingError:
    ENDPOINT = "/api/v1/stages"

    def test_returns_501_on_programming_error(self, client: TestClient) -> None:
        with (
            patch("modulo.api.routes.stages.list_stages", side_effect=_PROG_ERROR),
            patch("modulo.api.routes.stages.set_rls_org"),
            patch("modulo.api.routes.stages.set_rls_user_context"),
        ):
            resp = client.get(self.ENDPOINT)
        assert resp.status_code == 501

    def test_returns_503_on_sqlalchemy_error(self, client: TestClient) -> None:
        with (
            patch("modulo.api.routes.stages.list_stages", side_effect=_DB_ERROR),
            patch("modulo.api.routes.stages.set_rls_org"),
            patch("modulo.api.routes.stages.set_rls_user_context"),
        ):
            resp = client.get(self.ENDPOINT)
        assert resp.status_code == 503


class TestCreateStageProgrammingError:
    ENDPOINT = "/api/v1/stages"
    PAYLOAD = {"name": "Test Stage"}

    def test_returns_501_on_programming_error(self, client: TestClient) -> None:
        with (
            patch("modulo.api.routes.stages.create_stage", side_effect=_PROG_ERROR),
            patch("modulo.api.routes.stages.set_rls_org"),
            patch("modulo.api.routes.stages.set_rls_user_context"),
        ):
            resp = client.post(self.ENDPOINT, json=self.PAYLOAD)
        assert resp.status_code == 501

    def test_returns_503_on_sqlalchemy_error(self, client: TestClient) -> None:
        with (
            patch("modulo.api.routes.stages.create_stage", side_effect=_DB_ERROR),
            patch("modulo.api.routes.stages.set_rls_org"),
            patch("modulo.api.routes.stages.set_rls_user_context"),
        ):
            resp = client.post(self.ENDPOINT, json=self.PAYLOAD)
        assert resp.status_code == 503


class TestGetStageProgrammingError:
    ENDPOINT = f"/api/v1/stages/{_STAGE_ID}"

    def test_returns_501_on_programming_error(self, client: TestClient) -> None:
        with (
            patch("modulo.api.routes.stages.get_stage", side_effect=_PROG_ERROR),
            patch("modulo.api.routes.stages.set_rls_org"),
            patch("modulo.api.routes.stages.set_rls_user_context"),
        ):
            resp = client.get(self.ENDPOINT)
        assert resp.status_code == 501

    def test_returns_503_on_sqlalchemy_error(self, client: TestClient) -> None:
        with (
            patch("modulo.api.routes.stages.get_stage", side_effect=_DB_ERROR),
            patch("modulo.api.routes.stages.set_rls_org"),
            patch("modulo.api.routes.stages.set_rls_user_context"),
        ):
            resp = client.get(self.ENDPOINT)
        assert resp.status_code == 503


class TestUpdateStageProgrammingError:
    ENDPOINT = f"/api/v1/stages/{_STAGE_ID}"
    PAYLOAD = {"name": "Updated Stage"}

    def test_returns_501_on_programming_error(self, client: TestClient) -> None:
        with (
            patch("modulo.api.routes.stages.update_stage", side_effect=_PROG_ERROR),
            patch("modulo.api.routes.stages.set_rls_org"),
            patch("modulo.api.routes.stages.set_rls_user_context"),
        ):
            resp = client.patch(self.ENDPOINT, json=self.PAYLOAD)
        assert resp.status_code == 501

    def test_returns_503_on_sqlalchemy_error(self, client: TestClient) -> None:
        with (
            patch("modulo.api.routes.stages.update_stage", side_effect=_DB_ERROR),
            patch("modulo.api.routes.stages.set_rls_org"),
            patch("modulo.api.routes.stages.set_rls_user_context"),
        ):
            resp = client.patch(self.ENDPOINT, json=self.PAYLOAD)
        assert resp.status_code == 503


class TestDeleteStageProgrammingError:
    ENDPOINT = f"/api/v1/stages/{_STAGE_ID}"

    def test_returns_501_on_programming_error(self, client: TestClient) -> None:
        with (
            patch("modulo.api.routes.stages.delete_stage", side_effect=_PROG_ERROR),
            patch("modulo.api.routes.stages.set_rls_org"),
            patch("modulo.api.routes.stages.set_rls_user_context"),
        ):
            resp = client.delete(self.ENDPOINT)
        assert resp.status_code == 501

    def test_returns_503_on_sqlalchemy_error(self, client: TestClient) -> None:
        with (
            patch("modulo.api.routes.stages.delete_stage", side_effect=_DB_ERROR),
            patch("modulo.api.routes.stages.set_rls_org"),
            patch("modulo.api.routes.stages.set_rls_user_context"),
        ):
            resp = client.delete(self.ENDPOINT)
        assert resp.status_code == 503
