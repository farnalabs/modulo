"""Tests for schema route error handling — ProgrammingError, SQLAlchemyError, IntegrityError."""
import uuid
from collections.abc import AsyncGenerator, Generator
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.exc import IntegrityError, ProgrammingError, SQLAlchemyError

from modulo.api.dependencies import get_db_session
from modulo.api.main import app
from modulo.auth.dependencies import get_current_user
from modulo.auth.jwt import AuthenticatedPrincipal
from modulo.settings import Settings, get_settings

_VALID_32 = "a" * 32
_ORG_ID = uuid.UUID("00000000-0000-0000-0000-000000000001")


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
    return session


@pytest.fixture()
def client() -> Generator[TestClient, None, None]:
    mock_session = _make_mock_session()

    async def override_session() -> AsyncGenerator[AsyncMock, None]:
        yield mock_session

    app.dependency_overrides[get_settings] = _make_settings
    app.dependency_overrides[get_db_session] = override_session
    app.dependency_overrides[get_current_user] = lambda: AuthenticatedPrincipal(
        username="testuser",
        organisation_id=_ORG_ID,
        account_id=uuid.UUID("00000000-0000-0000-0000-000000000002"),
        org_role="admin",
    )
    yield TestClient(app)
    app.dependency_overrides.clear()


class TestSchemaListProgrammingError:
    def test_list_schemas_programming_error_returns_501(self, client: TestClient) -> None:
        mock_list = AsyncMock(side_effect=ProgrammingError("stmt", "params", "table missing"))
        with patch("modulo.api.routes.schemas.list_schemas", mock_list), \
             patch("modulo.api.routes.schemas.set_rls_org"):
            resp = client.get("/api/v1/schemas")
        assert resp.status_code == 501
        assert "run database migrations" in resp.json()["detail"].lower()

    def test_list_schemas_sqlalchemy_error_returns_503(self, client: TestClient) -> None:
        mock_list = AsyncMock(side_effect=SQLAlchemyError("connection failed"))
        with patch("modulo.api.routes.schemas.list_schemas", mock_list), \
             patch("modulo.api.routes.schemas.set_rls_org"):
            resp = client.get("/api/v1/schemas")
        assert resp.status_code == 503


class TestSchemaCreateErrors:
    SCHEMA_CREATE_JSON = {"name": "TestSchema", "description": "test"}

    def test_create_schema_programming_error_returns_501(self, client: TestClient) -> None:
        mock_create = AsyncMock(side_effect=ProgrammingError("stmt", "params", "table missing"))
        with patch("modulo.api.routes.schemas.create_schema", mock_create), \
             patch("modulo.api.routes.schemas.set_rls_org"):
            resp = client.post("/api/v1/schemas", json=self.SCHEMA_CREATE_JSON)
        assert resp.status_code == 501

    def test_create_schema_sqlalchemy_error_returns_503(self, client: TestClient) -> None:
        mock_create = AsyncMock(side_effect=SQLAlchemyError("connection failed"))
        with patch("modulo.api.routes.schemas.create_schema", mock_create), \
             patch("modulo.api.routes.schemas.set_rls_org"):
            resp = client.post("/api/v1/schemas", json=self.SCHEMA_CREATE_JSON)
        assert resp.status_code == 503

    def test_create_schema_integrity_error_returns_409(self, client: TestClient) -> None:
        mock_create = AsyncMock(side_effect=IntegrityError("stmt", "params", "duplicate name"))
        with patch("modulo.api.routes.schemas.create_schema", mock_create), \
             patch("modulo.api.routes.schemas.set_rls_org"):
            resp = client.post("/api/v1/schemas", json=self.SCHEMA_CREATE_JSON)
        assert resp.status_code == 409
        assert "already exists" in resp.json()["detail"].lower()


class TestSchemaGetErrors:
    SCHEMA_ID = str(uuid.UUID("00000000-0000-0000-0000-000000000099"))

    def test_get_schema_programming_error_returns_501(self, client: TestClient) -> None:
        mock_get = AsyncMock(side_effect=ProgrammingError("stmt", "params", "table missing"))
        with patch("modulo.api.routes.schemas.get_schema", mock_get), \
             patch("modulo.api.routes.schemas.set_rls_org"):
            resp = client.get(f"/api/v1/schemas/{self.SCHEMA_ID}")
        assert resp.status_code == 501

    def test_get_schema_sqlalchemy_error_returns_503(self, client: TestClient) -> None:
        mock_get = AsyncMock(side_effect=SQLAlchemyError("connection failed"))
        with patch("modulo.api.routes.schemas.get_schema", mock_get), \
             patch("modulo.api.routes.schemas.set_rls_org"):
            resp = client.get(f"/api/v1/schemas/{self.SCHEMA_ID}")
        assert resp.status_code == 503


class TestSchemaVersionCreateErrors:
    SCHEMA_ID = str(uuid.UUID("00000000-0000-0000-0000-000000000099"))
    VERSION_CREATE_JSON = {"version": "1.0.0", "version_number": 1, "definition_json": {"type": "object"}}

    def test_create_version_programming_error_returns_501(self, client: TestClient) -> None:
        with patch("modulo.api.routes.schemas.get_schema", return_value=MagicMock()), \
             patch("modulo.api.routes.schemas.create_schema_version", AsyncMock(side_effect=ProgrammingError("stmt", "params", "table missing"))), \
             patch("modulo.api.routes.schemas.set_rls_org"):
            resp = client.post(f"/api/v1/schemas/{self.SCHEMA_ID}/versions", json=self.VERSION_CREATE_JSON)
        assert resp.status_code == 501

    def test_create_version_integrity_error_returns_409(self, client: TestClient) -> None:
        with patch("modulo.api.routes.schemas.get_schema", return_value=MagicMock()), \
             patch("modulo.api.routes.schemas.create_schema_version", AsyncMock(side_effect=IntegrityError("stmt", "params", "duplicate version"))), \
             patch("modulo.api.routes.schemas.set_rls_org"):
            resp = client.post(f"/api/v1/schemas/{self.SCHEMA_ID}/versions", json=self.VERSION_CREATE_JSON)
        assert resp.status_code == 409
        assert "already exists" in resp.json()["detail"].lower()


class TestSchemaDeleteErrors:
    SCHEMA_ID = str(uuid.UUID("00000000-0000-0000-0000-000000000099"))

    def test_delete_schema_programming_error_returns_501(self, client: TestClient) -> None:
        mock_delete = AsyncMock(side_effect=ProgrammingError("stmt", "params", "table missing"))
        with patch("modulo.api.routes.schemas.delete_schema", mock_delete), \
             patch("modulo.api.routes.schemas.set_rls_org"):
            resp = client.delete(f"/api/v1/schemas/{self.SCHEMA_ID}")
        assert resp.status_code == 501

    def test_delete_schema_sqlalchemy_error_returns_503(self, client: TestClient) -> None:
        mock_delete = AsyncMock(side_effect=SQLAlchemyError("connection failed"))
        with patch("modulo.api.routes.schemas.delete_schema", mock_delete), \
             patch("modulo.api.routes.schemas.set_rls_org"):
            resp = client.delete(f"/api/v1/schemas/{self.SCHEMA_ID}")
        assert resp.status_code == 503
