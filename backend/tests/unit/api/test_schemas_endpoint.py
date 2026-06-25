"""Unit tests for /api/v1/schemas endpoints."""

import uuid
from collections.abc import AsyncGenerator, Generator
from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from modulo.api.dependencies import _get_engine, get_db_session
from modulo.api.main import app
from modulo.auth.dependencies import get_current_user
from modulo.auth.jwt import AuthenticatedPrincipal
from modulo.db.crud.schema import SchemaDeletionProtectedError
from modulo.settings import Settings, get_settings

_VALID_32 = "a" * 32
_ORG_ID = uuid.UUID("00000000-0000-0000-0000-000000000001")
_SCHEMA_ID = uuid.uuid4()
_NOW = datetime(2025, 1, 1, tzinfo=UTC)


def _make_settings() -> Settings:
    return Settings(
        database_url="postgresql+asyncpg://localhost/test",
        secret_key=_VALID_32,
        fernet_key=_VALID_32,
        modulo_admin_password="testpass",
    )


def _make_schema() -> MagicMock:
    s = MagicMock()
    s.id = _SCHEMA_ID
    s.organisation_id = _ORG_ID
    s.name = "Test Schema"
    s.description = None
    s.abstract_name = None
    s.created_by = uuid.uuid4()
    s.created_at = _NOW
    s.updated_at = _NOW
    return s


def _make_schema_version(schema_id: uuid.UUID) -> MagicMock:
    sv = MagicMock()
    sv.id = uuid.uuid4()
    sv.organisation_id = _ORG_ID
    sv.schema_id = schema_id
    sv.version = "1.0"
    sv.version_number = 1
    sv.definition_json = {"type": "object"}
    sv.published = False
    sv.created_by = uuid.uuid4()
    sv.created_at = _NOW
    sv.updated_at = _NOW
    return sv


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
        user_id=uuid.UUID("00000000-0000-0000-0000-000000000002"),
        org_role="admin",
    )
    yield TestClient(app)
    app.dependency_overrides.clear()


@pytest.fixture()
def unauth_client() -> Generator[TestClient, None, None]:
    app.dependency_overrides[get_settings] = _make_settings
    yield TestClient(app)
    app.dependency_overrides.clear()


# ---------------------------------------------------------------------------
# Schema CRUD
# ---------------------------------------------------------------------------


def test_list_schemas_returns_200(client: TestClient) -> None:
    page_result = MagicMock(items=[_make_schema()], total=1, page=1, page_size=20)
    with (
        patch("modulo.api.routes.schemas.list_schemas", return_value=page_result),
        patch("modulo.api.routes.schemas.set_rls_org"),
    ):
        resp = client.get("/api/v1/schemas")
    assert resp.status_code == 200
    assert resp.json()["total"] == 1


def test_create_schema_returns_201(client: TestClient) -> None:
    with (
        patch("modulo.api.routes.schemas.create_schema", return_value=_make_schema()),
        patch("modulo.api.routes.schemas.set_rls_org"),
    ):
        resp = client.post("/api/v1/schemas", json={"name": "Test Schema"})
    assert resp.status_code == 201
    assert resp.json()["name"] == "Test Schema"


def test_get_schema_returns_200(client: TestClient) -> None:
    with (
        patch("modulo.api.routes.schemas.get_schema", return_value=_make_schema()),
        patch("modulo.api.routes.schemas.set_rls_org"),
    ):
        resp = client.get(f"/api/v1/schemas/{_SCHEMA_ID}")
    assert resp.status_code == 200


def test_get_schema_not_found_returns_404(client: TestClient) -> None:
    with (
        patch("modulo.api.routes.schemas.get_schema", return_value=None),
        patch("modulo.api.routes.schemas.set_rls_org"),
    ):
        resp = client.get(f"/api/v1/schemas/{uuid.uuid4()}")
    assert resp.status_code == 404


def test_update_schema_returns_200(client: TestClient) -> None:
    schema = _make_schema()
    schema.name = "Updated"
    with (
        patch("modulo.api.routes.schemas.update_schema", return_value=schema),
        patch("modulo.api.routes.schemas.set_rls_org"),
    ):
        resp = client.patch(f"/api/v1/schemas/{_SCHEMA_ID}", json={"name": "Updated"})
    assert resp.status_code == 200


def test_delete_schema_returns_204(client: TestClient) -> None:
    with (
        patch("modulo.api.routes.schemas.delete_schema", return_value=True),
        patch("modulo.api.routes.schemas.set_rls_org"),
    ):
        resp = client.delete(f"/api/v1/schemas/{_SCHEMA_ID}")
    assert resp.status_code == 204


def test_delete_schema_not_found_returns_404(client: TestClient) -> None:
    with (
        patch("modulo.api.routes.schemas.delete_schema", return_value=False),
        patch("modulo.api.routes.schemas.set_rls_org"),
    ):
        resp = client.delete(f"/api/v1/schemas/{uuid.uuid4()}")
    assert resp.status_code == 404


def test_delete_schema_deletion_protected_returns_409(client: TestClient) -> None:
    with (
        patch(
            "modulo.api.routes.schemas.delete_schema",
            side_effect=SchemaDeletionProtectedError(_SCHEMA_ID),
        ),
        patch("modulo.api.routes.schemas.set_rls_org"),
    ):
        resp = client.delete(f"/api/v1/schemas/{_SCHEMA_ID}")
    assert resp.status_code == 409


# ---------------------------------------------------------------------------
# SchemaVersion CRUD
# ---------------------------------------------------------------------------


def test_list_schema_versions_returns_200(client: TestClient) -> None:
    schema = _make_schema()
    sv = _make_schema_version(_SCHEMA_ID)
    page_result = MagicMock(items=[sv], total=1, page=1, page_size=20)
    with (
        patch("modulo.api.routes.schemas.get_schema", return_value=schema),
        patch("modulo.api.routes.schemas.list_schema_versions", return_value=page_result),
        patch("modulo.api.routes.schemas.set_rls_org"),
    ):
        resp = client.get(f"/api/v1/schemas/{_SCHEMA_ID}/versions")
    assert resp.status_code == 200
    assert resp.json()["total"] == 1


def test_list_schema_versions_schema_not_found_returns_404(client: TestClient) -> None:
    with (
        patch("modulo.api.routes.schemas.get_schema", return_value=None),
        patch("modulo.api.routes.schemas.set_rls_org"),
    ):
        resp = client.get(f"/api/v1/schemas/{uuid.uuid4()}/versions")
    assert resp.status_code == 404


def test_create_schema_version_returns_201(client: TestClient) -> None:
    schema = _make_schema()
    sv = _make_schema_version(_SCHEMA_ID)
    with (
        patch("modulo.api.routes.schemas.get_schema", return_value=schema),
        patch("modulo.api.routes.schemas.create_schema_version", return_value=sv),
        patch("modulo.api.routes.schemas.set_rls_org"),
    ):
        resp = client.post(
            f"/api/v1/schemas/{_SCHEMA_ID}/versions",
            json={"version": "1.0", "version_number": 1, "definition_json": {"type": "object"}},
        )
    assert resp.status_code == 201


def test_get_schema_version_returns_200(client: TestClient) -> None:
    sv = _make_schema_version(_SCHEMA_ID)
    with (
        patch("modulo.api.routes.schemas.get_schema_version", return_value=sv),
        patch("modulo.api.routes.schemas.set_rls_org"),
    ):
        resp = client.get(f"/api/v1/schemas/{_SCHEMA_ID}/versions/1.0")
    assert resp.status_code == 200


def test_get_schema_version_not_found_returns_404(client: TestClient) -> None:
    with (
        patch("modulo.api.routes.schemas.get_schema_version", return_value=None),
        patch("modulo.api.routes.schemas.set_rls_org"),
    ):
        resp = client.get(f"/api/v1/schemas/{_SCHEMA_ID}/versions/99.0")
    assert resp.status_code == 404


def test_list_schemas_unauthenticated_returns_4xx(unauth_client: TestClient) -> None:
    resp = unauth_client.get("/api/v1/schemas")
    assert resp.status_code in (401, 403)
