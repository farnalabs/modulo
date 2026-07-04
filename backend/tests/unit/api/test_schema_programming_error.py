"""Unit tests: ProgrammingError→501 and SQLAlchemyError→503 for all schema DB routes."""
import uuid
from collections.abc import AsyncGenerator, Generator
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
_SCHEMA_ID = uuid.UUID("00000000-0000-0000-0000-000000000100")
_CONNECTOR_ID = uuid.UUID("00000000-0000-0000-0000-000000000010")

_SCHEMA_ID_STR = str(_SCHEMA_ID)
_CONNECTOR_ID_STR = str(_CONNECTOR_ID)


def _make_settings() -> Settings:
    return Settings(
        database_url="postgresql+asyncpg://localhost/test",
        secret_key=_VALID_32,
        fernet_key=_VALID_32,
        modulo_admin_password="testpass",
    )


@pytest.fixture()
def programming_error_client() -> Generator[TestClient, None, None]:
    mock_session = AsyncMock()
    begin_cm = AsyncMock()
    begin_cm.__aenter__ = AsyncMock(side_effect=ProgrammingError("mock", {}, "table not found"))
    begin_cm.__aexit__ = AsyncMock(return_value=False)
    mock_session.begin = MagicMock(return_value=begin_cm)

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


@pytest.fixture()
def sqla_error_client() -> Generator[TestClient, None, None]:
    mock_session = AsyncMock()
    begin_cm = AsyncMock()
    begin_cm.__aenter__ = AsyncMock(side_effect=SQLAlchemyError("mock", {}, "connection failed"))
    begin_cm.__aexit__ = AsyncMock(return_value=False)
    mock_session.begin = MagicMock(return_value=begin_cm)

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


class TestSchemaProgrammingError:
    """Each route raises ProgrammingError → should return 501 with migration hint."""

    def test_list_schemas_returns_501(self, programming_error_client: TestClient) -> None:
        resp = programming_error_client.get("/api/v1/schemas")
        assert resp.status_code == 501
        assert "migrations" in resp.text.lower()

    def test_create_schema_returns_501(self, programming_error_client: TestClient) -> None:
        resp = programming_error_client.post("/api/v1/schemas", json={"name": "test"})
        assert resp.status_code == 501
        assert "migrations" in resp.text.lower()

    def test_get_schema_returns_501(self, programming_error_client: TestClient) -> None:
        resp = programming_error_client.get(f"/api/v1/schemas/{_SCHEMA_ID_STR}")
        assert resp.status_code == 501
        assert "migrations" in resp.text.lower()

    def test_update_schema_returns_501(self, programming_error_client: TestClient) -> None:
        resp = programming_error_client.patch(
            f"/api/v1/schemas/{_SCHEMA_ID_STR}", json={"name": "changed"}
        )
        assert resp.status_code == 501
        assert "migrations" in resp.text.lower()

    def test_deprecate_schema_returns_501(self, programming_error_client: TestClient) -> None:
        resp = programming_error_client.patch(f"/api/v1/schemas/{_SCHEMA_ID_STR}/deprecate")
        assert resp.status_code == 501
        assert "migrations" in resp.text.lower()

    def test_delete_schema_returns_501(self, programming_error_client: TestClient) -> None:
        resp = programming_error_client.delete(f"/api/v1/schemas/{_SCHEMA_ID_STR}")
        assert resp.status_code == 501
        assert "migrations" in resp.text.lower()

    def test_list_schema_versions_returns_501(self, programming_error_client: TestClient) -> None:
        resp = programming_error_client.get(f"/api/v1/schemas/{_SCHEMA_ID_STR}/versions")
        assert resp.status_code == 501
        assert "migrations" in resp.text.lower()

    def test_create_schema_version_returns_501(self, programming_error_client: TestClient) -> None:
        resp = programming_error_client.post(
            f"/api/v1/schemas/{_SCHEMA_ID_STR}/versions",
            json={"version": "2.0", "version_number": 2, "definition_json": {"type": "object"}},
        )
        assert resp.status_code == 501
        assert "migrations" in resp.text.lower()

    def test_get_schema_version_returns_501(self, programming_error_client: TestClient) -> None:
        resp = programming_error_client.get(f"/api/v1/schemas/{_SCHEMA_ID_STR}/versions/1.0")
        assert resp.status_code == 501
        assert "migrations" in resp.text.lower()

    def test_list_schema_fields_returns_501(self, programming_error_client: TestClient) -> None:
        resp = programming_error_client.get(f"/api/v1/schemas/{_SCHEMA_ID_STR}/fields")
        assert resp.status_code == 501
        assert "migrations" in resp.text.lower()

    def test_infer_schema_returns_501(self, programming_error_client: TestClient) -> None:
        resp = programming_error_client.post(
            "/api/v1/schemas/infer",
            json={
                "connector_instance_id": _CONNECTOR_ID_STR,
                "sample_query": {"resource": "issues"},
            },
        )
        assert resp.status_code == 501
        assert "migrations" in resp.text.lower()

    def test_generate_schema_returns_501(self, programming_error_client: TestClient) -> None:
        resp = programming_error_client.post(
            "/api/v1/schemas/generate", json={"description": "test"}
        )
        assert resp.status_code == 501
        assert "migrations" in resp.text.lower()

    def test_migrate_data_returns_501(self, programming_error_client: TestClient) -> None:
        resp = programming_error_client.post(
            "/api/v1/schemas/migrate",
            json={
                "from_schema_id": _SCHEMA_ID_STR,
                "to_schema_id": _SCHEMA_ID_STR,
                "data": {"foo": "bar"},
            },
        )
        assert resp.status_code == 501
        assert "migrations" in resp.text.lower()


class TestSchemaSQLAlchemyError:
    """Each route raises generic SQLAlchemyError → should return 503."""

    def test_list_schemas_returns_503(self, sqla_error_client: TestClient) -> None:
        resp = sqla_error_client.get("/api/v1/schemas")
        assert resp.status_code == 503
        assert "temporarily" in resp.text.lower()

    def test_create_schema_returns_503(self, sqla_error_client: TestClient) -> None:
        resp = sqla_error_client.post("/api/v1/schemas", json={"name": "test"})
        assert resp.status_code == 503
        assert "temporarily" in resp.text.lower()

    def test_get_schema_returns_503(self, sqla_error_client: TestClient) -> None:
        resp = sqla_error_client.get(f"/api/v1/schemas/{_SCHEMA_ID_STR}")
        assert resp.status_code == 503
        assert "temporarily" in resp.text.lower()

    def test_update_schema_returns_503(self, sqla_error_client: TestClient) -> None:
        resp = sqla_error_client.patch(
            f"/api/v1/schemas/{_SCHEMA_ID_STR}", json={"name": "changed"}
        )
        assert resp.status_code == 503
        assert "temporarily" in resp.text.lower()

    def test_deprecate_schema_returns_503(self, sqla_error_client: TestClient) -> None:
        resp = sqla_error_client.patch(f"/api/v1/schemas/{_SCHEMA_ID_STR}/deprecate")
        assert resp.status_code == 503
        assert "temporarily" in resp.text.lower()

    def test_delete_schema_returns_503(self, sqla_error_client: TestClient) -> None:
        resp = sqla_error_client.delete(f"/api/v1/schemas/{_SCHEMA_ID_STR}")
        assert resp.status_code == 503
        assert "temporarily" in resp.text.lower()

    def test_list_schema_versions_returns_503(self, sqla_error_client: TestClient) -> None:
        resp = sqla_error_client.get(f"/api/v1/schemas/{_SCHEMA_ID_STR}/versions")
        assert resp.status_code == 503
        assert "temporarily" in resp.text.lower()

    def test_create_schema_version_returns_503(self, sqla_error_client: TestClient) -> None:
        resp = sqla_error_client.post(
            f"/api/v1/schemas/{_SCHEMA_ID_STR}/versions",
            json={"version": "2.0", "version_number": 2, "definition_json": {"type": "object"}},
        )
        assert resp.status_code == 503
        assert "temporarily" in resp.text.lower()

    def test_get_schema_version_returns_503(self, sqla_error_client: TestClient) -> None:
        resp = sqla_error_client.get(f"/api/v1/schemas/{_SCHEMA_ID_STR}/versions/1.0")
        assert resp.status_code == 503
        assert "temporarily" in resp.text.lower()

    def test_list_schema_fields_returns_503(self, sqla_error_client: TestClient) -> None:
        resp = sqla_error_client.get(f"/api/v1/schemas/{_SCHEMA_ID_STR}/fields")
        assert resp.status_code == 503
        assert "temporarily" in resp.text.lower()

    def test_infer_schema_returns_503(self, sqla_error_client: TestClient) -> None:
        resp = sqla_error_client.post(
            "/api/v1/schemas/infer",
            json={
                "connector_instance_id": _CONNECTOR_ID_STR,
                "sample_query": {"resource": "issues"},
            },
        )
        assert resp.status_code == 503
        assert "temporarily" in resp.text.lower()

    def test_generate_schema_returns_503(self, sqla_error_client: TestClient) -> None:
        resp = sqla_error_client.post(
            "/api/v1/schemas/generate", json={"description": "test"}
        )
        assert resp.status_code == 503
        assert "temporarily" in resp.text.lower()

    def test_migrate_data_returns_503(self, sqla_error_client: TestClient) -> None:
        resp = sqla_error_client.post(
            "/api/v1/schemas/migrate",
            json={
                "from_schema_id": _SCHEMA_ID_STR,
                "to_schema_id": _SCHEMA_ID_STR,
                "data": {"foo": "bar"},
            },
        )
        assert resp.status_code == 503
        assert "temporarily" in resp.text.lower()
