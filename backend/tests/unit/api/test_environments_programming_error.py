"""Unit tests: environment profile CRUD routes return 501/503/409 on DB errors.

Tests that all 6 environment profile route handlers gracefully return:
- 501 Not Implemented when the database raises ProgrammingError
  (e.g. missing table because migrations haven't run yet)
- 503 SERVICE_UNAVAILABLE when SQLAlchemyError is raised
  (e.g. connection loss, deadlock)
- 409 Conflict when IntegrityError is raised on create/update
  (e.g. concurrent duplicate name)
"""

import uuid
from collections.abc import AsyncGenerator
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.exc import IntegrityError, ProgrammingError, SQLAlchemyError

from modulo.api.dependencies import _get_engine, get_db_session
from modulo.api.main import app
from modulo.auth.dependencies import get_current_user
from modulo.auth.jwt import AuthenticatedPrincipal
from modulo.settings import Settings, get_settings

_VALID_32 = "a" * 32
_ORG_ID = uuid.UUID("00000000-0000-0000-0000-000000000001")
_USER_ID = uuid.UUID("00000000-0000-0000-0000-000000000002")
_PROFILE_ID = uuid.UUID("00000000-0000-0000-0000-000000000010")


def _make_settings() -> Settings:
    return Settings(
        database_url="postgresql+asyncpg://localhost/test",
        secret_key=_VALID_32,
        fernet_key=_VALID_32,
        modulo_admin_password="testpass",
    )


def _make_session_raising(exc_class: type[Exception]) -> AsyncMock:
    session = AsyncMock()
    begin_cm = AsyncMock()
    begin_cm.__aenter__ = AsyncMock(side_effect=exc_class("db error", None, None))
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


class TestListProfilesProgrammingError:
    URL = "/api/v1/environments"

    def test_list_returns_501_on_programming_error(self, admin_client: TestClient) -> None:
        session = _make_session_raising(ProgrammingError)
        _override_session(session)
        resp = admin_client.get(self.URL)
        assert resp.status_code == 501
        assert "migrations" in resp.json()["detail"].lower()

    def test_list_returns_503_on_sqlalchemy_error(self, admin_client: TestClient) -> None:
        session = _make_session_raising(SQLAlchemyError)
        _override_session(session)
        resp = admin_client.get(self.URL)
        assert resp.status_code == 503


class TestCreateProfileDBErrors:
    URL = "/api/v1/environments"
    PAYLOAD = {
        "name": "test-env",
        "image_ref": "python:3.12-slim",
    }

    def test_create_returns_501_on_programming_error(self, admin_client: TestClient) -> None:
        session = _make_session_raising(ProgrammingError)
        _override_session(session)
        resp = admin_client.post(self.URL, json=self.PAYLOAD)
        assert resp.status_code == 501
        assert "migrations" in resp.json()["detail"].lower()

    def test_create_returns_409_on_integrity_error(self, admin_client: TestClient) -> None:
        session = _make_session_raising(IntegrityError)
        _override_session(session)
        resp = admin_client.post(self.URL, json=self.PAYLOAD)
        assert resp.status_code == 409
        assert "already exists" in resp.json()["detail"].lower()

    def test_create_returns_503_on_sqlalchemy_error(self, admin_client: TestClient) -> None:
        session = _make_session_raising(SQLAlchemyError)
        _override_session(session)
        resp = admin_client.post(self.URL, json=self.PAYLOAD)
        assert resp.status_code == 503


class TestGetProfileProgrammingError:
    URL = "/api/v1/environments"

    def test_get_returns_501_on_programming_error(self, admin_client: TestClient) -> None:
        session = _make_session_raising(ProgrammingError)
        _override_session(session)
        resp = admin_client.get(f"{self.URL}/{_PROFILE_ID}")
        assert resp.status_code == 501
        assert "migrations" in resp.json()["detail"].lower()

    def test_get_returns_503_on_sqlalchemy_error(self, admin_client: TestClient) -> None:
        session = _make_session_raising(SQLAlchemyError)
        _override_session(session)
        resp = admin_client.get(f"{self.URL}/{_PROFILE_ID}")
        assert resp.status_code == 503


class TestUpdateProfileDBErrors:
    URL = "/api/v1/environments"

    def test_update_returns_501_on_programming_error(self, admin_client: TestClient) -> None:
        session = _make_session_raising(ProgrammingError)
        _override_session(session)
        resp = admin_client.patch(f"{self.URL}/{_PROFILE_ID}", json={"name": "updated"})
        assert resp.status_code == 501
        assert "migrations" in resp.json()["detail"].lower()

    def test_update_returns_409_on_integrity_error(self, admin_client: TestClient) -> None:
        session = _make_session_raising(IntegrityError)
        _override_session(session)
        resp = admin_client.patch(f"{self.URL}/{_PROFILE_ID}", json={"name": "duplicate"})
        assert resp.status_code == 409
        assert "already exists" in resp.json()["detail"].lower()

    def test_update_returns_503_on_sqlalchemy_error(self, admin_client: TestClient) -> None:
        session = _make_session_raising(SQLAlchemyError)
        _override_session(session)
        resp = admin_client.patch(f"{self.URL}/{_PROFILE_ID}", json={"name": "updated"})
        assert resp.status_code == 503


class TestDeleteProfileProgrammingError:
    URL = "/api/v1/environments"

    def test_delete_returns_501_on_programming_error(self, admin_client: TestClient) -> None:
        session = _make_session_raising(ProgrammingError)
        _override_session(session)
        resp = admin_client.delete(f"{self.URL}/{_PROFILE_ID}")
        assert resp.status_code == 501
        assert "migrations" in resp.json()["detail"].lower()

    def test_delete_returns_503_on_sqlalchemy_error(self, admin_client: TestClient) -> None:
        session = _make_session_raising(SQLAlchemyError)
        _override_session(session)
        resp = admin_client.delete(f"{self.URL}/{_PROFILE_ID}")
        assert resp.status_code == 503


class TestProfileTestEndpointProgrammingError:
    URL = "/api/v1/environments"

    def test_test_returns_501_on_programming_error(self, admin_client: TestClient) -> None:
        session = _make_session_raising(ProgrammingError)
        _override_session(session)
        resp = admin_client.post(f"{self.URL}/{_PROFILE_ID}/test")
        assert resp.status_code == 501
        assert "migrations" in resp.json()["detail"].lower()

    def test_test_returns_503_on_sqlalchemy_error(self, admin_client: TestClient) -> None:
        session = _make_session_raising(SQLAlchemyError)
        _override_session(session)
        resp = admin_client.post(f"{self.URL}/{_PROFILE_ID}/test")
        assert resp.status_code == 503
