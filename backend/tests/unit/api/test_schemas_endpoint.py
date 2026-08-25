"""Unit tests for /api/v1/schemas endpoints."""

import json
import uuid
from collections.abc import AsyncGenerator, Generator
from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.exc import IntegrityError, ProgrammingError

from modulo.api.dependencies import _get_engine, get_db_session, get_plan_context
from modulo.api.main import app
from modulo.auth.dependencies import get_current_user
from modulo.auth.jwt import AuthenticatedPrincipal
from modulo.db.crud.schema import SchemaDeletionProtectedError
from modulo.settings import Settings, get_settings
from tests.unit.api.mock_session import configure_mock_session

_SCHEMA_PATCH_PREFIX = "modulo.api.routes.schemas."

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
    s.folder_id = None
    s.account_id = uuid.uuid4()
    s.created_by = s.account_id
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
    sv.account_id = uuid.uuid4()
    sv.created_by = sv.account_id
    sv.created_at = _NOW
    sv.updated_at = _NOW
    return sv


def _make_mock_session() -> AsyncMock:
    session = AsyncMock()
    configure_mock_session(session)
    begin_cm = AsyncMock()
    begin_cm.__aenter__ = AsyncMock(return_value=None)
    begin_cm.__aexit__ = AsyncMock(return_value=False)
    session.begin = MagicMock(return_value=begin_cm)
    return session


@pytest.fixture
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
    mock_plan = MagicMock()
    mock_plan.feature_enabled.return_value = True
    app.dependency_overrides[get_plan_context] = lambda: mock_plan
    yield TestClient(app)
    app.dependency_overrides.clear()


@pytest.fixture
def unauth_client() -> Generator[TestClient, None, None]:
    app.dependency_overrides[get_settings] = _make_settings
    mock_plan = MagicMock()
    mock_plan.feature_enabled.return_value = True
    app.dependency_overrides[get_plan_context] = lambda: mock_plan
    yield TestClient(app)
    app.dependency_overrides.clear()


# ---------------------------------------------------------------------------
# Schema CRUD
# ---------------------------------------------------------------------------


def _schema_crud_cases() -> list[dict[str, object]]:
    updated = _make_schema()
    updated.name = "Updated"
    return [
        {
            "id": "list",
            "method": "GET",
            "url": "/api/v1/schemas",
            "body": None,
            "patches": [("list_schemas", MagicMock(items=[_make_schema()], total=1, page=1, page_size=20))],
            "expected_status": 200,
        },
        {
            "id": "create",
            "method": "POST",
            "url": "/api/v1/schemas",
            "body": {"name": "Test Schema"},
            "patches": [("create_schema", _make_schema())],
            "expected_status": 201,
        },
        {
            "id": "get",
            "method": "GET",
            "url": f"/api/v1/schemas/{_SCHEMA_ID}",
            "body": None,
            "patches": [("get_schema", _make_schema())],
            "expected_status": 200,
        },
        {
            "id": "get_not_found",
            "method": "GET",
            "url": f"/api/v1/schemas/{uuid.uuid4()}",
            "body": None,
            "patches": [("get_schema", None)],
            "expected_status": 404,
        },
        {
            "id": "update",
            "method": "PATCH",
            "url": f"/api/v1/schemas/{_SCHEMA_ID}",
            "body": {"name": "Updated"},
            "patches": [("update_schema", updated)],
            "expected_status": 200,
        },
        {
            "id": "delete",
            "method": "DELETE",
            "url": f"/api/v1/schemas/{_SCHEMA_ID}",
            "body": None,
            "patches": [("delete_schema", True)],
            "expected_status": 204,
        },
        {
            "id": "delete_not_found",
            "method": "DELETE",
            "url": f"/api/v1/schemas/{uuid.uuid4()}",
            "body": None,
            "patches": [("delete_schema", False)],
            "expected_status": 404,
        },
    ]


@pytest.mark.parametrize("case", _schema_crud_cases(), ids=lambda c: c["id"])
def test_schema_crud(client: TestClient, case: dict[str, object]) -> None:
    method = case["method"]
    url = case["url"]
    body = case.get("body")
    expected_status = case["expected_status"]
    patchers = [patch(f"{_SCHEMA_PATCH_PREFIX}{fn}", return_value=ret) for fn, ret in case["patches"]]
    patchers.append(patch(f"{_SCHEMA_PATCH_PREFIX}set_rls_org"))
    for p in patchers:
        p.start()
    try:
        if method == "GET":
            resp = client.get(url)
        elif method == "POST":
            resp = client.post(url, json=body or {})
        elif method == "PATCH":
            resp = client.patch(url, json=body or {})
        elif method == "DELETE":
            resp = client.delete(url)
        else:
            raise ValueError(f"Unsupported method: {method}")
        assert resp.status_code == expected_status, f"Expected {expected_status}, got {resp.status_code}: {resp.text}"
    finally:
        for p in patchers:
            p.stop()


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


def _schema_version_crud_cases() -> list[dict[str, object]]:
    schema = _make_schema()
    sv = _make_schema_version(_SCHEMA_ID)
    page_result = MagicMock(items=[sv], total=1, page=1, page_size=20)
    return [
        {
            "id": "list",
            "method": "GET",
            "url": f"/api/v1/schemas/{_SCHEMA_ID}/versions",
            "body": None,
            "patches": [("get_schema", schema), ("list_schema_versions", page_result)],
            "expected_status": 200,
        },
        {
            "id": "list_not_found",
            "method": "GET",
            "url": f"/api/v1/schemas/{uuid.uuid4()}/versions",
            "body": None,
            "patches": [("get_schema", None)],
            "expected_status": 404,
        },
        {
            "id": "create",
            "method": "POST",
            "url": f"/api/v1/schemas/{_SCHEMA_ID}/versions",
            "body": {"version": "1.0", "version_number": 1, "definition_json": {"type": "object"}},
            "patches": [("get_schema", schema), ("create_schema_version", sv)],
            "expected_status": 201,
        },
        {
            "id": "get",
            "method": "GET",
            "url": f"/api/v1/schemas/{_SCHEMA_ID}/versions/1.0",
            "body": None,
            "patches": [("get_schema_version", sv)],
            "expected_status": 200,
        },
        {
            "id": "get_not_found",
            "method": "GET",
            "url": f"/api/v1/schemas/{_SCHEMA_ID}/versions/99.0",
            "body": None,
            "patches": [("get_schema_version", None)],
            "expected_status": 404,
        },
    ]


@pytest.mark.parametrize("case", _schema_version_crud_cases(), ids=lambda c: c["id"])
def test_schema_version_crud(client: TestClient, case: dict[str, object]) -> None:
    method = case["method"]
    url = case["url"]
    body = case.get("body")
    expected_status = case["expected_status"]
    patchers = [patch(f"{_SCHEMA_PATCH_PREFIX}{fn}", return_value=ret) for fn, ret in case["patches"]]
    patchers.append(patch(f"{_SCHEMA_PATCH_PREFIX}set_rls_org"))
    for p in patchers:
        p.start()
    try:
        if method == "GET":
            resp = client.get(url)
        elif method == "POST":
            resp = client.post(url, json=body or {})
        elif method == "PATCH":
            resp = client.patch(url, json=body or {})
        elif method == "DELETE":
            resp = client.delete(url)
        else:
            raise ValueError(f"Unsupported method: {method}")
        assert resp.status_code == expected_status, f"Expected {expected_status}, got {resp.status_code}: {resp.text}"
    finally:
        for p in patchers:
            p.stop()


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


def test_migrate_data_records_audit_event(client: TestClient) -> None:
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
        patch("modulo.api.routes.schemas.append_audit_event_isolated", new_callable=AsyncMock) as mock_append,
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
    mock_append.assert_awaited_once()
    call = mock_append.await_args
    assert call.kwargs["event_type"] == "schema_migration_completed"
    assert call.kwargs["resource_type"] == "schema"
    assert call.kwargs["resource_id"] == to_schema.id
    payload = call.kwargs["payload"]
    assert payload["from_schema_id"] == str(from_schema.id)
    assert payload["to_schema_id"] == str(to_schema.id)
    assert payload["dry_run"] is False
    assert payload["field_removals"] == 1
    assert payload["field_additions"] == 1
    assert payload["renames"] == 0
    assert payload["type_changes"] == 0


def test_migrate_data_dry_run_records_audit_event(client: TestClient) -> None:
    from_schema = _make_schema()
    to_schema = _make_schema()
    from_sv = _make_schema_version(from_schema.id)
    from_sv.definition_json = {
        "type": "object",
        "properties": {"name": {"type": "string"}},
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
        patch("modulo.api.routes.schemas.append_audit_event_isolated", new_callable=AsyncMock) as mock_append,
    ):
        resp = client.post(
            "/api/v1/schemas/migrate?dry_run=true",
            json={
                "from_schema_id": str(from_schema.id),
                "to_schema_id": str(to_schema.id),
                "data": {"name": "Alice"},
            },
        )
    assert resp.status_code == 200
    mock_append.assert_awaited_once()
    payload = mock_append.await_args.kwargs["payload"]
    assert payload["dry_run"] is True
    body = resp.json()
    assert body["plan"]["dry_run"] is True
    assert body["migrated_data"] == {"name": "Alice"}


def test_migrate_data_audit_failure_does_not_break_response(client: TestClient) -> None:
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
        patch(
            "modulo.core.audit_logger.append_audit_event",
            new_callable=AsyncMock,
            side_effect=RuntimeError("audit chain unavailable"),
        ),
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
    assert "legacy" not in body["migrated_data"]
    assert body["migrated_data"]["email"] is None


def test_migrate_data_audit_programming_error_returns_200(client: TestClient) -> None:
    from_schema = _make_schema()
    to_schema = _make_schema()
    from_sv = _make_schema_version(from_schema.id)
    from_sv.definition_json = {
        "type": "object",
        "properties": {"name": {"type": "string"}},
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
        patch(
            "modulo.core.audit_logger.append_audit_event",
            new_callable=AsyncMock,
            side_effect=ProgrammingError("statement", {}, Exception("missing table")),
        ),
    ):
        resp = client.post(
            "/api/v1/schemas/migrate",
            json={
                "from_schema_id": str(from_schema.id),
                "to_schema_id": str(to_schema.id),
                "data": {"name": "Alice"},
            },
        )
    assert resp.status_code == 200
    assert resp.json()["migrated_data"]["email"] is None


def test_migration_plan_endpoint_returns_200(client: TestClient) -> None:
    with patch("modulo.api.routes.schemas.append_audit_event_isolated", new_callable=AsyncMock):
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
    with patch("modulo.api.routes.schemas.append_audit_event_isolated", new_callable=AsyncMock):
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
    assert not body["field_additions"]
    assert not body["field_removals"]
    assert not body["renames"]


def test_migration_plan_unauthenticated_returns_4xx(unauth_client: TestClient) -> None:
    resp = unauth_client.post(
        "/api/v1/schemas/migrate/plan",
        json={
            "from_definition": {"type": "object", "properties": {"a": {"type": "string"}}},
            "to_definition": {"type": "object", "properties": {"b": {"type": "string"}}},
        },
    )
    assert resp.status_code in (401, 403)


def test_migration_plan_records_audit_event(client: TestClient) -> None:
    with patch("modulo.api.routes.schemas.append_audit_event_isolated", new_callable=AsyncMock) as mock_append:
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
    mock_append.assert_awaited_once()
    call = mock_append.await_args
    assert call.kwargs["event_type"] == "schema_migration_planned"
    assert call.kwargs["resource_type"] == "schema"
    payload = call.kwargs["payload"]
    assert payload["field_additions"] == 1
    assert payload["field_removals"] == 1
    assert payload["type_changes"] == 0
    assert payload["renames"] == 1


def test_migration_plan_audit_failure_does_not_break_response(client: TestClient) -> None:
    with patch(
        "modulo.core.audit_logger.append_audit_event",
        new_callable=AsyncMock,
        side_effect=RuntimeError("audit boom"),
    ):
        resp = client.post(
            "/api/v1/schemas/migrate/plan",
            json={
                "from_definition": {"type": "object", "properties": {"a": {"type": "string"}}},
                "to_definition": {"type": "object", "properties": {"b": {"type": "string"}}},
            },
        )
    assert resp.status_code == 200
    assert "field_additions" in resp.json()


# ---------------------------------------------------------------------------
# Schema deprecate
# ---------------------------------------------------------------------------


def test_deprecate_schema_returns_200(client: TestClient) -> None:
    schema = _make_schema()
    schema.deprecated = True
    schema.deprecated_at = _NOW
    with (
        patch("modulo.api.routes.schemas.deprecate_schema", return_value=schema),
        patch("modulo.api.routes.schemas.set_rls_org"),
    ):
        resp = client.patch(f"/api/v1/schemas/{_SCHEMA_ID}/deprecate")
    assert resp.status_code == 200
    assert resp.json()["deprecated"] is True


def test_deprecate_schema_not_found_returns_404(client: TestClient) -> None:
    with (
        patch("modulo.api.routes.schemas.deprecate_schema", return_value=None),
        patch("modulo.api.routes.schemas.set_rls_org"),
    ):
        resp = client.patch(f"/api/v1/schemas/{uuid.uuid4()}/deprecate")
    assert resp.status_code == 404


# ---------------------------------------------------------------------------
# Schema validate
# ---------------------------------------------------------------------------


def test_validate_schema_valid_returns_valid_true(client: TestClient) -> None:
    resp = client.post(
        "/api/v1/schemas/validate",
        json={"definition": {"type": "object", "properties": {"name": {"type": "string"}}}},
    )
    assert resp.status_code == 200
    assert resp.json()["valid"] is True
    assert not resp.json()["errors"]


def test_validate_schema_invalid_returns_valid_false(client: TestClient) -> None:
    resp = client.post(
        "/api/v1/schemas/validate",
        json={"definition": {"type": 123}},
    )
    assert resp.status_code == 200
    assert resp.json()["valid"] is False
    assert resp.json()["errors"]


def test_validate_schema_non_dict_definition_returns_422(client: TestClient) -> None:
    """definition must be a JSON object; a non-dict value is a client validation error."""
    for bad_definition in ([1, 2, 3], "string", 42, None, True):
        resp = client.post(
            "/api/v1/schemas/validate",
            json={"definition": bad_definition},
        )
        assert resp.status_code == 422, f"definition={bad_definition!r} -> {resp.status_code}: {resp.text}"


def test_validate_schema_non_object_body_returns_422(client: TestClient) -> None:
    """A raw non-object JSON body (array) is rejected as a request-validation error."""
    resp = client.post("/api/v1/schemas/validate", content="[1,2,3]")
    assert resp.status_code == 422


def test_create_schema_integrity_error_returns_409(client: TestClient) -> None:
    """Duplicate schema name per org surfaces as 409 (IntegrityError caught in route)."""
    with (
        patch(
            "modulo.api.routes.schemas.create_schema",
            side_effect=IntegrityError("INSERT", {}, Exception("duplicate key")),
        ),
        patch("modulo.api.routes.schemas.set_rls_org"),
    ):
        resp = client.post("/api/v1/schemas", json={"name": "Dup"})
    assert resp.status_code == 409
    assert resp.json()["detail"] == "A schema with this name already exists."


# ---------------------------------------------------------------------------
# Schema import
# ---------------------------------------------------------------------------


def test_import_schema_returns_200(client: TestClient) -> None:
    resp = client.post(
        "/api/v1/schemas/import",
        json={
            "content": json.dumps(
                {
                    "type": "object",
                    "title": "TestSchema",
                    "description": "A test schema",
                    "properties": {
                        "name": {"type": "string", "description": "The name"},
                        "age": {"type": "integer", "description": "The age"},
                    },
                    "required": ["name"],
                }
            )
        },
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["name"] == "TestSchema"
    assert body["description"] == "A test schema"
    assert len(body["fields"]) == 2
    assert body["fields"][0]["name"] == "name"
    assert body["fields"][0]["required"] is True
    assert body["fields"][1]["name"] == "age"
    assert body["fields"][1]["required"] is False


def test_import_schema_invalid_json_returns_400(client: TestClient) -> None:
    resp = client.post(
        "/api/v1/schemas/import",
        json={"content": "not valid json"},
    )
    assert resp.status_code == 400
    assert "Invalid JSON" in resp.json()["detail"]


def test_import_schema_invalid_schema_returns_422(client: TestClient) -> None:
    resp = client.post(
        "/api/v1/schemas/import",
        json={"content": json.dumps({"type": 123})},
    )
    assert resp.status_code == 422
    assert "Invalid JSON Schema" in resp.json()["detail"]


# ---------------------------------------------------------------------------
# Schema version creation is explicit, not auto-save (PRD 8.3)
# ---------------------------------------------------------------------------


def test_create_schema_auto_creates_latest_placeholder_version(client: TestClient) -> None:
    """Creating a schema seeds a 'latest' placeholder version (version_number 0)."""
    schema = _make_schema()

    async def _fake_create_schema(session, *, org_id, name, account_id, description=None, abstract_name=None):
        return schema

    placeholder_versions: list[MagicMock] = []

    def _fake_schema_version_model(**kwargs: object) -> MagicMock:
        placeholder = MagicMock()
        placeholder.version_number = kwargs.get("version_number")
        placeholder.version = kwargs.get("version")
        placeholder.schema_id = kwargs.get("schema_id")
        placeholder_versions.append(placeholder)
        return placeholder

    with (
        patch("modulo.api.routes.schemas.create_schema", side_effect=_fake_create_schema) as mock_create,
        patch("modulo.api.routes.schemas.set_rls_org"),
        patch("modulo.api.routes.schemas.SchemaVersionModel", side_effect=_fake_schema_version_model),
    ):
        resp = client.post("/api/v1/schemas", json={"name": "Explicit Version Schema"})
    assert resp.status_code == 201
    mock_create.assert_awaited_once()

    # A placeholder 'latest' version (version_number 0) is seeded so agents
    # have something to pin before the first explicit version is created.
    assert len(placeholder_versions) == 1
    assert placeholder_versions[0].version == "latest"
    assert placeholder_versions[0].version_number == 0
    assert placeholder_versions[0].schema_id == schema.id


def test_schema_version_creation_is_explicit_endpoint(client: TestClient) -> None:
    """A new schema version is only created through the explicit POST /versions action."""
    schema = _make_schema()
    sv = _make_schema_version(schema.id)
    sv.version = "2.0"
    sv.version_number = 2
    with (
        patch("modulo.api.routes.schemas.get_schema", return_value=schema),
        patch("modulo.api.routes.schemas.create_schema_version", return_value=sv) as mock_create,
        patch("modulo.api.routes.schemas.set_rls_org"),
    ):
        resp = client.post(
            f"/api/v1/schemas/{schema.id}/versions",
            json={"version": "2.0", "version_number": 2, "definition_json": {"type": "object"}},
        )
    assert resp.status_code == 201
    mock_create.assert_awaited_once()
    assert resp.json()["version"] == "2.0"
    assert resp.json()["version_number"] == 2


# ---------------------------------------------------------------------------
# Validate endpoint: non-dict JSON parsed body returns 400
# ---------------------------------------------------------------------------


def test_validate_schema_integrity_error_create_returns_409(client: TestClient) -> None:
    """A duplicate schema name per org surfaces as 409 (IntegrityError catch)."""
    from sqlalchemy.exc import IntegrityError

    with (
        patch(
            "modulo.api.routes.schemas.create_schema",
            side_effect=IntegrityError("stmt", {}, Exception("duplicate")),
        ),
        patch("modulo.api.routes.schemas.set_rls_org"),
    ):
        resp = client.post("/api/v1/schemas", json={"name": "Duplicate"})
    assert resp.status_code == 409
    assert "already exists" in resp.json()["detail"]
