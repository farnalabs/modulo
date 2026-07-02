"""BDD step definitions for schemas.feature — schema CRUD, validation, and import."""

import json
import uuid
from datetime import UTC, datetime
from unittest.mock import MagicMock, patch

from pytest_bdd import given, parsers, scenarios, then, when

scenarios("../features/library/schemas.feature")

_MOCK_SCHEMA_ID = uuid.uuid4()
_ORG_ID = uuid.UUID("00000000-0000-0000-0000-000000000001")
_USER_ID = uuid.UUID("00000000-0000-0000-0000-000000000002")
_NOW = datetime(2025, 1, 1, tzinfo=UTC)


def _make_mock_schema(name: str = "meeting-notes", description: str | None = None) -> MagicMock:
    s = MagicMock()
    s.id = _MOCK_SCHEMA_ID
    s.organisation_id = _ORG_ID
    s.name = name
    s.description = description
    s.abstract_name = None
    s.account_id = _USER_ID
    s.created_at = _NOW
    s.updated_at = _NOW
    s.deprecated = False
    s.deprecated_at = None
    return s


def _make_mock_schema_version(version: str = "1.0") -> MagicMock:
    sv = MagicMock()
    sv.id = uuid.uuid4()
    sv.organisation_id = _ORG_ID
    sv.schema_id = _MOCK_SCHEMA_ID
    sv.version = version
    sv.version_number = 1
    sv.definition_json = {"type": "object", "title": "Test", "properties": {}}
    sv.published = True
    sv.account_id = _USER_ID
    sv.created_at = _NOW
    sv.updated_at = _NOW
    return sv


# ============================================================================
# Given steps
# ============================================================================


@given(parsers.parse('a schema "{name}" exists'))
def _schema_exists(name: str) -> None:
    pass


@given("22 library schemas exist")
def _twenty_two_schemas_exist() -> None:
    pass


@given("no agents reference the schema")
def _no_agents_reference() -> None:
    pass


@given("the schema has 2 versions")
def _schema_has_two_versions() -> None:
    pass


@given(parsers.parse('the schema has a version "{version}"'))
def _schema_has_version(version: str) -> None:
    pass


# ============================================================================
# When steps
# ============================================================================


@when("the user sends POST /api/v1/schemas with body")
def _post_create_schema(client, request, docstring):
    data = json.loads(docstring)
    mock_schema = _make_mock_schema(name=data["name"], description=data.get("description"))
    with (
        patch("modulo.api.routes.schemas.create_schema", return_value=mock_schema),
        patch("modulo.api.routes.schemas.set_rls_org"),
    ):
        resp = client.post("/api/v1/schemas", json=data)
    request.node._resp = resp


@when("the user sends POST /api/v1/schemas/{schema_id}/versions with body")
def _post_create_schema_version(client, request, docstring):
    data = json.loads(docstring)
    mock_schema = _make_mock_schema()
    mock_sv = _make_mock_schema_version(version=data["version"])
    with (
        patch("modulo.api.routes.schemas.get_schema", return_value=mock_schema),
        patch("modulo.api.routes.schemas.create_schema_version", return_value=mock_sv),
        patch("modulo.api.routes.schemas.set_rls_org"),
    ):
        resp = client.post(f"/api/v1/schemas/{_MOCK_SCHEMA_ID}/versions", json=data)
    request.node._resp = resp


@when("the user requests GET /api/v1/schemas")
def _get_list_schemas(client, request):
    mock_schemas = [_make_mock_schema(name=f"schema-{i}") for i in range(22)]
    page_result = MagicMock(items=mock_schemas, total=22, page=1, page_size=20)
    with (
        patch("modulo.api.routes.schemas.list_schemas", return_value=page_result),
        patch("modulo.api.routes.schemas.set_rls_org"),
    ):
        resp = client.get("/api/v1/schemas")
    request.node._resp = resp


@when("the user requests GET /api/v1/schemas?page=1&page_size=10")
def _get_list_schemas_paginated(client, request):
    mock_schemas = [_make_mock_schema(name=f"schema-{i}") for i in range(10)]
    page_result = MagicMock(items=mock_schemas, total=22, page=1, page_size=10)
    with (
        patch("modulo.api.routes.schemas.list_schemas", return_value=page_result),
        patch("modulo.api.routes.schemas.set_rls_org"),
    ):
        resp = client.get("/api/v1/schemas?page=1&page_size=10")
    request.node._resp = resp


@when("the user requests GET /api/v1/schemas/{schema_id}")
def _get_schema(client, request):
    with (
        patch("modulo.api.routes.schemas.get_schema", return_value=_make_mock_schema()),
        patch("modulo.api.routes.schemas.set_rls_org"),
    ):
        resp = client.get(f"/api/v1/schemas/{_MOCK_SCHEMA_ID}")
    request.node._resp = resp


@when("the user requests GET /api/v1/schemas/00000000-0000-0000-0000-000000099999")
def _get_schema_not_found(client, request):
    with (
        patch("modulo.api.routes.schemas.get_schema", return_value=None),
        patch("modulo.api.routes.schemas.set_rls_org"),
    ):
        resp = client.get("/api/v1/schemas/00000000-0000-0000-0000-000000099999")
    request.node._resp = resp


@when("the user sends PATCH /api/v1/schemas/{schema_id} with body")
def _patch_update_schema(client, request, docstring):
    data = json.loads(docstring)
    mock_schema = _make_mock_schema(description=data.get("description"))
    with (
        patch("modulo.api.routes.schemas.update_schema", return_value=mock_schema),
        patch("modulo.api.routes.schemas.set_rls_org"),
    ):
        resp = client.patch(f"/api/v1/schemas/{_MOCK_SCHEMA_ID}", json=data)
    request.node._resp = resp


@when("the user sends PATCH /api/v1/schemas/{schema_id}/deprecate")
def _patch_deprecate_schema(client, request):
    mock_schema = _make_mock_schema()
    mock_schema.deprecated = True
    mock_schema.deprecated_at = _NOW
    with (
        patch("modulo.api.routes.schemas.deprecate_schema", return_value=mock_schema),
        patch("modulo.api.routes.schemas.set_rls_org"),
    ):
        resp = client.patch(f"/api/v1/schemas/{_MOCK_SCHEMA_ID}/deprecate")
    request.node._resp = resp


@when("the user sends DELETE /api/v1/schemas/{schema_id}")
def _delete_schema(client, request):
    with (
        patch("modulo.api.routes.schemas.delete_schema", return_value=True),
        patch("modulo.api.routes.schemas.set_rls_org"),
    ):
        resp = client.delete(f"/api/v1/schemas/{_MOCK_SCHEMA_ID}")
    request.node._resp = resp


@when("the user requests GET /api/v1/schemas/{schema_id}/versions")
def _get_list_versions(client, request):
    mock_schema = _make_mock_schema()
    mock_versions = [_make_mock_schema_version(f"{i}.0") for i in range(1, 3)]
    page_result = MagicMock(items=mock_versions, total=2, page=1, page_size=20)
    with (
        patch("modulo.api.routes.schemas.get_schema", return_value=mock_schema),
        patch("modulo.api.routes.schemas.list_schema_versions", return_value=page_result),
        patch("modulo.api.routes.schemas.set_rls_org"),
    ):
        resp = client.get(f"/api/v1/schemas/{_MOCK_SCHEMA_ID}/versions")
    request.node._resp = resp


@when("the user requests GET /api/v1/schemas/{schema_id}/versions/1.0")
def _get_schema_version(client, request):
    mock_sv = _make_mock_schema_version("1.0")
    with (
        patch("modulo.api.routes.schemas.get_schema_version", return_value=mock_sv),
        patch("modulo.api.routes.schemas.set_rls_org"),
    ):
        resp = client.get(f"/api/v1/schemas/{_MOCK_SCHEMA_ID}/versions/1.0")
    request.node._resp = resp


@when("the user sends POST /api/v1/schemas/validate with body")
def _post_validate(client, request, docstring):
    data = json.loads(docstring)
    resp = client.post("/api/v1/schemas/validate", json=data)
    request.node._resp = resp


@when("the user sends POST /api/v1/schemas/import with body")
def _post_import(client, request, docstring):
    data = json.loads(docstring)
    resp = client.post("/api/v1/schemas/import", json=data)
    request.node._resp = resp


# ============================================================================
# Then steps
# ============================================================================


@then(parsers.parse('the response contains a schema with name "{name}"'))
def _response_schema_name(name: str, request) -> None:
    data = request.node._resp.json()
    assert data["name"] == name, f"Expected name '{name}', got '{data['name']}'"


@then("the response contains a schema id")
def _response_schema_id(request) -> None:
    data = request.node._resp.json()
    assert "id" in data, "Missing schema id in response"


@then(parsers.parse('the response contains a schema version with version "{version}"'))
def _response_schema_version(version: str, request) -> None:
    data = request.node._resp.json()
    assert data["version"] == version, f"Expected version '{version}', got '{data['version']}'"


@then(parsers.parse("the response contains at least {count:d} schemas"))
def _response_at_least_n_schemas(count: int, request) -> None:
    data = request.node._resp.json()
    assert data["total"] >= count, f"Expected at least {count} schemas, got {data['total']}"


@then(parsers.parse("the response contains {count:d} schemas"))
def _response_n_schemas(count: int, request) -> None:
    data = request.node._resp.json()
    assert len(data["items"]) == count, f"Expected {count} schemas, got {len(data['items'])}"


@then(parsers.parse('the response contains a schema with description "{desc}"'))
def _response_schema_description(desc: str, request) -> None:
    data = request.node._resp.json()
    assert data["description"] == desc, f"Expected description '{desc}', got '{data['description']}'"


@then("the response contains a schema that is deprecated")
def _response_schema_deprecated(request) -> None:
    data = request.node._resp.json()
    assert data.get("deprecated") is True, "Expected deprecated=True"


@then("the validation result is valid")
def _validation_valid(request) -> None:
    data = request.node._resp.json()
    assert data["valid"] is True, f"Expected valid=True, got {data}"


@then("the validation result is not valid")
def _validation_invalid(request) -> None:
    data = request.node._resp.json()
    assert data["valid"] is False, f"Expected valid=False, got {data}"


@then(parsers.parse('the imported schema has name "{name}"'))
def _imported_schema_name(name: str, request) -> None:
    data = request.node._resp.json()
    assert data.get("name") == name, f"Expected name '{name}', got '{data.get('name')}'"


@then(parsers.parse("the imported schema has {count:d} field"))
def _imported_schema_fields(count: int, request) -> None:
    data = request.node._resp.json()
    assert len(data.get("fields", [])) == count, f"Expected {count} fields, got {len(data.get('fields', []))}"


@then("the response contains 2 schema versions")
def _response_two_versions(request) -> None:
    data = request.node._resp.json()
    assert data["total"] == 2, f"Expected 2 versions, got {data['total']}"


@then(parsers.parse('the response version is "{version}"'))
def _response_version(version: str, request) -> None:
    data = request.node._resp.json()
    assert data["version"] == version, f"Expected version '{version}', got '{data['version']}'"
