"""Unit tests: library API routes return 503 on SQLAlchemyError.

Tests that all 7 DB-accessing routes that previously only caught
ProgrammingError now also catch SQLAlchemyError and return 503 Service
Unavailable when the database raises a transient error (connection failure,
deadlock, timeout).
"""

from collections.abc import AsyncGenerator
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.exc import SQLAlchemyError

from modulo.api.dependencies import _get_engine, get_db_session
from modulo.api.main import app
from modulo.auth.dependencies import get_current_user
from modulo.auth.jwt import AuthenticatedPrincipal
from modulo.settings import Settings, get_settings

_VALID_32 = "a" * 32
_ORG_ID = "00000000-0000-0000-0000-000000000001"
_USER_ID = "00000000-0000-0000-0000-000000000002"
_PRIMITIVE_ID = "00000000-0000-0000-0000-000000000003"
_PIPELINE_ID = "00000000-0000-0000-0000-000000000004"


def _make_settings() -> Settings:
    return Settings(
        database_url="postgresql+asyncpg://localhost/test",
        secret_key=_VALID_32,
        fernet_key=_VALID_32,
        modulo_admin_password="testpass",
    )


def _make_session_raising_sqlalchemy_error() -> AsyncMock:
    session = AsyncMock()
    begin_cm = AsyncMock()
    begin_cm.__aenter__ = AsyncMock(side_effect=SQLAlchemyError("connection timeout"))
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


class TestListPrimitivesSQLAlchemyError:
    def test_list_primitives_returns_503_on_sqlalchemy_error(self, admin_client: TestClient) -> None:
        session = _make_session_raising_sqlalchemy_error()
        _override_session(session)
        resp = admin_client.get("/api/v1/libraries")
        assert resp.status_code == 503
        assert "unavailable" in resp.json()["detail"].lower()


class TestGetPrimitiveSQLAlchemyError:
    def test_get_primitive_returns_503_on_sqlalchemy_error(self, admin_client: TestClient) -> None:
        session = _make_session_raising_sqlalchemy_error()
        _override_session(session)
        resp = admin_client.get(f"/api/v1/libraries/{_PRIMITIVE_ID}")
        assert resp.status_code == 503
        assert "unavailable" in resp.json()["detail"].lower()


class TestCreatePrimitiveSQLAlchemyError:
    def test_create_primitive_returns_503_on_sqlalchemy_error(self, admin_client: TestClient) -> None:
        session = _make_session_raising_sqlalchemy_error()
        _override_session(session)
        resp = admin_client.post(
            "/api/v1/libraries",
            json={
                "primitive_type": "schema",
                "name": "Test Schema",
                "slug": "test-schema",
                "content_json": {},
            },
        )
        assert resp.status_code == 503
        assert "unavailable" in resp.json()["detail"].lower()


class TestUpdatePrimitiveSQLAlchemyError:
    def test_update_primitive_returns_503_on_sqlalchemy_error(self, admin_client: TestClient) -> None:
        session = _make_session_raising_sqlalchemy_error()
        _override_session(session)
        resp = admin_client.patch(
            f"/api/v1/libraries/{_PRIMITIVE_ID}",
            json={"name": "Updated Schema"},
        )
        assert resp.status_code == 503
        assert "unavailable" in resp.json()["detail"].lower()


class TestDeletePrimitiveSQLAlchemyError:
    def test_delete_primitive_returns_503_on_sqlalchemy_error(self, admin_client: TestClient) -> None:
        session = _make_session_raising_sqlalchemy_error()
        _override_session(session)
        resp = admin_client.delete(f"/api/v1/libraries/{_PRIMITIVE_ID}")
        assert resp.status_code == 503
        assert "unavailable" in resp.json()["detail"].lower()


class TestCopyToAdaptSQLAlchemyError:
    def test_copy_to_adapt_returns_503_on_sqlalchemy_error(self, admin_client: TestClient) -> None:
        with patch(
            "modulo.api.routes.library.copy_to_adapt",
            new_callable=AsyncMock,
            side_effect=SQLAlchemyError("connection timeout"),
        ):
            resp = admin_client.post(f"/api/v1/libraries/{_PRIMITIVE_ID}/adapt", json={})
        assert resp.status_code == 503
        assert "unavailable" in resp.json()["detail"].lower()


class TestExportPipelineSQLAlchemyError:
    def test_export_pipeline_returns_503_on_sqlalchemy_error(self, admin_client: TestClient) -> None:
        session = _make_session_raising_sqlalchemy_error()
        _override_session(session)
        resp = admin_client.post(f"/api/v1/libraries/export/{_PIPELINE_ID}")
        assert resp.status_code == 503
        assert "unavailable" in resp.json()["detail"].lower()
