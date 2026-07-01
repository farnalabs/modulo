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
        account_id=uuid.UUID("00000000-0000-0000-0000-000000000002"),
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


def test_delete_schema_force_returns_204(client: TestClient) -> None:
    """force=True should delete even when references exist."""
    with (
        patch("modulo.api.routes.schemas.delete_schema", return_value=True),
        patch("modulo.api.routes.schemas.set_rls_org"),
    ):
        resp = client.delete(f"/api/v1/schemas/{_SCHEMA_ID}?force=true")
    assert resp.status_code == 204


def test_delete_schema_force_skips_protection(client: TestClient) -> None:
    """delete_schema without force raises error; with force=True passes."""
    schema_id = uuid.uuid4()
    # Without force — should raise SchemaDeletionProtectedError
    with (
        patch(
            "modulo.api.routes.schemas.delete_schema",
            side_effect=SchemaDeletionProtectedError(schema_id),
        ),
        patch("modulo.api.routes.schemas.set_rls_org"),
    ):
        resp = client.delete(f"/api/v1/schemas/{schema_id}")
    assert resp.status_code == 409

    # With force=true — should succeed
    with (
        patch("modulo.api.routes.schemas.delete_schema", return_value=True),
        patch("modulo.api.routes.schemas.set_rls_org"),
    ):
        resp = client.delete(f"/api/v1/schemas/{schema_id}?force=true")
    assert resp.status_code == 204


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


# ---------------------------------------------------------------------------
# Schema Migration
# ---------------------------------------------------------------------------


def test_migrate_data_returns_200(client: TestClient) -> None:
    from_schema = _make_schema()
    to_schema = _make_schema()
    from_sv = _make_schema_version(from_schema.id)
    from_sv.definition_json = {
        "type": "object",
        "properties": {"name": {"type": "string"}, "legacy": {"type": "boolean"}},
    }
    to_sv = _make_schema_version(to_schema.id)
    to_sv.definition_json = {
        "type": "object",
        "properties": {"name": {"type": "string"}, "email": {"type": "string"}},
    }
    page_result = MagicMock(items=[from_sv], total=1, page=1, page_size=20)
    to_page = MagicMock(items=[to_sv], total=1, page=1, page_size=20)
    with (
        patch("modulo.api.routes.schemas.get_schema", side_effect=[from_schema, to_schema]),
        patch("modulo.api.routes.schemas.list_schema_versions", side_effect=[page_result, to_page]),
        patch("modulo.api.routes.schemas.set_rls_org"),
    ):
        resp = client.post(
            "/api/v1/schemas/migrate",
            json={
                "from_schema_id": str(from_schema.id),
                "to_schema_id": str(to_schema.id),
                "data": {"name": "Alice", "legacy": True},
            },
        )
    assert resp.status_code == 200
    body = resp.json()
    assert body["migrated_data"]["name"] == "Alice"
    assert "legacy" not in body["migrated_data"]
    assert body["migrated_data"]["email"] is None
    assert "field_removals" in body["plan"]
    assert "field_additions" in body["plan"]


def test_migrate_data_source_schema_not_found_returns_404(client: TestClient) -> None:
    with (
        patch("modulo.api.routes.schemas.get_schema", return_value=None),
        patch("modulo.api.routes.schemas.set_rls_org"),
    ):
        resp = client.post(
            "/api/v1/schemas/migrate",
            json={
                "from_schema_id": str(uuid.uuid4()),
                "to_schema_id": str(uuid.uuid4()),
                "data": {"name": "Alice"},
            },
        )
    assert resp.status_code == 404


def test_migrate_data_source_no_versions_returns_404(client: TestClient) -> None:
    from_schema = _make_schema()
    to_schema = _make_schema()
    with (
        patch("modulo.api.routes.schemas.get_schema", side_effect=[from_schema, to_schema]),
        patch(
            "modulo.api.routes.schemas.list_schema_versions",
            return_value=MagicMock(items=[], total=0, page=1, page_size=20),
        ),
        patch("modulo.api.routes.schemas.set_rls_org"),
    ):
        resp = client.post(
            "/api/v1/schemas/migrate",
            json={
                "from_schema_id": str(from_schema.id),
                "to_schema_id": str(to_schema.id),
                "data": {"name": "Alice"},
            },
        )
    assert resp.status_code == 404


def test_migration_plan_endpoint_returns_200(client: TestClient) -> None:
    resp = client.post(
        "/api/v1/schemas/migrate/plan",
        json={
            "from_definition": {
                "type": "object",
                "properties": {"full_name": {"type": "string"}, "age": {"type": "integer"}},
            },
            "to_definition": {
                "type": "object",
                "properties": {"display_name": {"type": "string"}, "email": {"type": "boolean"}},
            },
        },
    )
    assert resp.status_code == 200
    body = resp.json()
    assert "full_name" in body["renames"]
    assert body["renames"]["full_name"] == "display_name"
    assert "email" in body["field_additions"]
    assert body["field_additions"]["email"] == "boolean"
    assert "age" in body["field_removals"]


def test_migration_plan_no_changes(client: TestClient) -> None:
    resp = client.post(
        "/api/v1/schemas/migrate/plan",
        json={
            "from_definition": {
                "type": "object",
                "properties": {"name": {"type": "string"}},
            },
            "to_definition": {
                "type": "object",
                "properties": {"name": {"type": "string"}},
            },
        },
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["field_additions"] == {}
    assert body["field_removals"] == []
    assert body["renames"] == {}
