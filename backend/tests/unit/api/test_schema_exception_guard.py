"""Tests for schema route catch-all Exception→500 guards."""

import uuid
from collections.abc import AsyncGenerator, Generator
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from modulo.api.dependencies import get_db_session, get_plan_context
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
    mock_plan = MagicMock()
    mock_plan.feature_enabled.return_value = True
    app.dependency_overrides[get_plan_context] = lambda: mock_plan
    yield TestClient(app)
    app.dependency_overrides.clear()


class TestSchemaListExceptionGuard:
    def test_list_schemas_exception_returns_500(self, client: TestClient) -> None:
        mock_list = AsyncMock(side_effect=ValueError("unexpected data"))
        with patch("modulo.api.routes.schemas.list_schemas", mock_list), patch("modulo.api.routes.schemas.set_rls_org"):
            resp = client.get("/api/v1/schemas")
        assert resp.status_code == 500
        assert "unexpected error" in resp.json()["detail"].lower()


class TestSchemaCreateExceptionGuard:
    SCHEMA_CREATE_JSON = {"name": "TestSchema", "description": "test"}

    def test_create_schema_exception_returns_500(self, client: TestClient) -> None:
        mock_create = AsyncMock(side_effect=TypeError("wrong type"))
        with (
            patch("modulo.api.routes.schemas.create_schema", mock_create),
            patch("modulo.api.routes.schemas.set_rls_org"),
        ):
            resp = client.post("/api/v1/schemas", json=self.SCHEMA_CREATE_JSON)
        assert resp.status_code == 500
        assert "unexpected error" in resp.json()["detail"].lower()


class TestSchemaGetExceptionGuard:
    SCHEMA_ID = str(uuid.UUID("00000000-0000-0000-0000-000000000099"))

    def test_get_schema_exception_returns_500(self, client: TestClient) -> None:
        mock_get = AsyncMock(side_effect=ValueError("bad data"))
        with patch("modulo.api.routes.schemas.get_schema", mock_get), patch("modulo.api.routes.schemas.set_rls_org"):
            resp = client.get(f"/api/v1/schemas/{self.SCHEMA_ID}")
        assert resp.status_code == 500
        assert "unexpected error" in resp.json()["detail"].lower()


class TestSchemaUpdateExceptionGuard:
    SCHEMA_ID = str(uuid.UUID("00000000-0000-0000-0000-000000000099"))
    SCHEMA_UPDATE_JSON = {"name": "RenamedSchema"}

    def test_update_schema_exception_returns_500(self, client: TestClient) -> None:
        mock_update = AsyncMock(side_effect=ValueError("bad update"))
        with (
            patch("modulo.api.routes.schemas.update_schema", mock_update),
            patch("modulo.api.routes.schemas.set_rls_org"),
        ):
            resp = client.patch(f"/api/v1/schemas/{self.SCHEMA_ID}", json=self.SCHEMA_UPDATE_JSON)
        assert resp.status_code == 500
        assert "unexpected error" in resp.json()["detail"].lower()


class TestSchemaDeprecateExceptionGuard:
    SCHEMA_ID = str(uuid.UUID("00000000-0000-0000-0000-000000000099"))

    def test_deprecate_schema_exception_returns_500(self, client: TestClient) -> None:
        mock_deprecate = AsyncMock(side_effect=ValueError("bad deprecate"))
        with (
            patch("modulo.api.routes.schemas.deprecate_schema", mock_deprecate),
            patch("modulo.api.routes.schemas.set_rls_org"),
        ):
            resp = client.patch(f"/api/v1/schemas/{self.SCHEMA_ID}/deprecate")
        assert resp.status_code == 500
        assert "unexpected error" in resp.json()["detail"].lower()


class TestSchemaDeleteExceptionGuard:
    SCHEMA_ID = str(uuid.UUID("00000000-0000-0000-0000-000000000099"))

    def test_delete_schema_exception_returns_500(self, client: TestClient) -> None:
        mock_delete = AsyncMock(side_effect=ValueError("bad delete"))
        with (
            patch("modulo.api.routes.schemas.delete_schema", mock_delete),
            patch("modulo.api.routes.schemas.set_rls_org"),
        ):
            resp = client.delete(f"/api/v1/schemas/{self.SCHEMA_ID}")
        assert resp.status_code == 500
        assert "unexpected error" in resp.json()["detail"].lower()


class TestSchemaVersionListExceptionGuard:
    SCHEMA_ID = str(uuid.UUID("00000000-0000-0000-0000-000000000099"))

    def test_list_versions_exception_returns_500(self, client: TestClient) -> None:
        mock_list = AsyncMock(side_effect=ValueError("bad versions"))
        with (
            patch("modulo.api.routes.schemas.list_schema_versions", mock_list),
            patch("modulo.api.routes.schemas.set_rls_org"),
        ):
            resp = client.get(f"/api/v1/schemas/{self.SCHEMA_ID}/versions")
        assert resp.status_code == 500
        assert "unexpected error" in resp.json()["detail"].lower()


class TestSchemaVersionCreateExceptionGuard:
    SCHEMA_ID = str(uuid.UUID("00000000-0000-0000-0000-000000000099"))
    VERSION_CREATE_JSON = {"version": "1.0.0", "version_number": 1, "definition_json": {"type": "object"}}

    def test_create_version_exception_returns_500(self, client: TestClient) -> None:
        with (
            patch("modulo.api.routes.schemas.get_schema", return_value=MagicMock()),
            patch(
                "modulo.api.routes.schemas.create_schema_version",
                AsyncMock(side_effect=ValueError("bad version")),
            ),
            patch("modulo.api.routes.schemas.set_rls_org"),
        ):
            resp = client.post(f"/api/v1/schemas/{self.SCHEMA_ID}/versions", json=self.VERSION_CREATE_JSON)
        assert resp.status_code == 500
        assert "unexpected error" in resp.json()["detail"].lower()


class TestSchemaVersionGetExceptionGuard:
    SCHEMA_ID = str(uuid.UUID("00000000-0000-0000-0000-000000000099"))

    def test_get_version_exception_returns_500(self, client: TestClient) -> None:
        mock_get = AsyncMock(side_effect=ValueError("bad version get"))
        with (
            patch("modulo.api.routes.schemas.get_schema_version", mock_get),
            patch("modulo.api.routes.schemas.set_rls_org"),
        ):
            resp = client.get(f"/api/v1/schemas/{self.SCHEMA_ID}/versions/1.0.0")
        assert resp.status_code == 500
        assert "unexpected error" in resp.json()["detail"].lower()


class TestSchemaFieldsExceptionGuard:
    SCHEMA_ID = str(uuid.UUID("00000000-0000-0000-0000-000000000099"))

    def test_fields_exception_returns_500(self, client: TestClient) -> None:
        mock_get = AsyncMock(side_effect=ValueError("bad fields"))
        with (
            patch("modulo.api.routes.schemas.get_schema", mock_get),
            patch("modulo.api.routes.schemas.set_rls_org"),
        ):
            resp = client.get(f"/api/v1/schemas/{self.SCHEMA_ID}/fields")
        assert resp.status_code == 500
        assert "unexpected error" in resp.json()["detail"].lower()


class TestSchemaMigrateExceptionGuard:
    SCHEMA_ID = str(uuid.UUID("00000000-0000-0000-0000-000000000099"))
    SCHEMA_TARGET_ID = str(uuid.UUID("00000000-0000-0000-0000-000000000100"))

    def test_migrate_db_exception_returns_500(self, client: TestClient) -> None:
        mock_get = AsyncMock(side_effect=ValueError("bad"))
        with (
            patch("modulo.api.routes.schemas.get_schema", mock_get),
            patch("modulo.api.routes.schemas.set_rls_org"),
        ):
            resp = client.post(
                "/api/v1/schemas/migrate",
                json={
                    "from_schema_id": self.SCHEMA_ID,
                    "to_schema_id": self.SCHEMA_TARGET_ID,
                    "data": {},
                },
            )
        assert resp.status_code == 500
        assert "unexpected error" in resp.json()["detail"].lower()


class TestSchemaMigratePlanExceptionGuard:
    @patch("modulo.api.routes.schemas.create_migration", side_effect=ValueError("bad schema"))
    def test_migration_plan_create_migration_exception_returns_500(
        self, mock_create: MagicMock, client: TestClient
    ) -> None:
        resp = client.post(
            "/api/v1/schemas/migrate/plan",
            json={
                "from_definition": {"type": "object", "properties": {}},
                "to_definition": {"type": "object", "properties": {"name": {"type": "string"}}},
            },
        )
        assert resp.status_code == 500
        assert "failed to compute migration plan" in resp.json()["detail"].lower()
