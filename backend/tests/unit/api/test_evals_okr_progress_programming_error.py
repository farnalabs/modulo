"""Unit test: okr_progress returns 501 on ProgrammingError, 503 on SQLAlchemyError.

Tests that GET /evals/okr-progress/{suite_id} gracefully returns
501 Not Implemented when the database raises ProgrammingError,
and 503 when SQLAlchemyError is raised.
"""

import uuid
from collections.abc import AsyncGenerator
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.exc import ProgrammingError, SQLAlchemyError

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


def _make_session_raising_programming_error() -> AsyncMock:
    session = AsyncMock()
    begin_cm = AsyncMock()
    begin_cm.__aenter__ = AsyncMock(return_value=None)
    begin_cm.__aexit__ = AsyncMock(return_value=False)
    session.begin = MagicMock(return_value=begin_cm)
    session.in_transaction = MagicMock(return_value=True)
    session.execute = AsyncMock(side_effect=ProgrammingError("relation does not exist", None, None))
    bind_mock = MagicMock()
    bind_mock.dialect.name = "postgresql"
    session.get_bind = AsyncMock(return_value=bind_mock)
    return session


def _make_session_raising_sqlalchemy_error() -> AsyncMock:
    session = AsyncMock()
    begin_cm = AsyncMock()
    begin_cm.__aenter__ = AsyncMock(return_value=None)
    begin_cm.__aexit__ = AsyncMock(return_value=False)
    session.begin = MagicMock(return_value=begin_cm)
    session.in_transaction = MagicMock(return_value=True)
    session.execute = AsyncMock(side_effect=SQLAlchemyError("connection failed", None, None))
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


def test_okr_progress_returns_501_on_programming_error(admin_client: TestClient) -> None:
    session = _make_session_raising_programming_error()

    async def _override_session() -> AsyncGenerator[AsyncMock, None]:
        yield session

    app.dependency_overrides[get_db_session] = _override_session
    resp = admin_client.get("/api/v1/admin/evals/okr-progress/test-suite")
    assert resp.status_code == 501
    assert "migrations" in resp.json()["detail"].lower()


def test_okr_progress_returns_503_on_sqlalchemy_error(admin_client: TestClient) -> None:
    session = _make_session_raising_sqlalchemy_error()

    async def _override_session() -> AsyncGenerator[AsyncMock, None]:
        yield session

    app.dependency_overrides[get_db_session] = _override_session
    resp = admin_client.get("/api/v1/admin/evals/okr-progress/test-suite")
    assert resp.status_code == 503
    assert "database" in resp.json()["detail"].lower()
