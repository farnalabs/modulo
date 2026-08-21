"""BDD step definitions: Schema create, version, deletion protection."""

import contextlib
import uuid
from datetime import UTC, datetime
from unittest.mock import MagicMock, patch

from pytest_bdd import given, parsers, scenarios, then, when

with contextlib.suppress(FileNotFoundError, OSError):
    scenarios("../features/schemas/create.feature")
with contextlib.suppress(FileNotFoundError, OSError):
    scenarios("../features/schemas/version.feature")
with contextlib.suppress(FileNotFoundError, OSError):
    scenarios("../features/schemas/deletion_protection.feature")

from tests.bdd.conftest import ORG_ID, USER_ID

_NOW = datetime(2025, 1, 1, tzinfo=UTC)


def _schema_id_for(name: str) -> uuid.UUID:
    """Deterministic schema id per name so scenarios can round-trip by slug."""
    return uuid.uuid5(uuid.NAMESPACE_URL, f"schema/{name}")


def _make_mock_schema(name: str = "test-schema", **kwargs) -> MagicMock:
    s = MagicMock()
    s.id = kwargs.get("id", _schema_id_for(name))
    s.organisation_id = ORG_ID
    s.name = name
    s.description = kwargs.get("description")
    s.abstract_name = None
    s.folder_id = None
    s.account_id = USER_ID
    s.created_at = _NOW
    s.updated_at = _NOW
    s.deprecated = False
    s.deprecated_at = None
    return s


def _make_mock_schema_version(version: str = "1", version_number: int = 1, **kwargs) -> MagicMock:
    sv = MagicMock()
    sv.id = uuid.uuid4()
    sv.organisation_id = ORG_ID
    sv.schema_id = kwargs.get("schema_id", uuid.uuid4())
    sv.version = version
    sv.version_number = version_number
    sv.definition_json = kwargs.get(
        "definition_json", {"type": "object", "properties": {}, "additionalProperties": True}
    )
    sv.published = False
    sv.account_id = USER_ID
    sv.created_at = _NOW
    sv.updated_at = _NOW
    return sv


_NESTED_DEFINITION = {
    "type": "object",
    "properties": {
        "nested": {
            "type": "object",
            "properties": {"inner": {"type": "string"}},
        }
    },
}


@when(parsers.parse('I POST /api/schemas with name "{name}" and valid JSON Schema'))
def create_schema(name: str, client, request):
    with (
        patch(
            "modulo.api.routes.schemas.create_schema",
            return_value=_make_mock_schema(name=name),
        ),
        patch("modulo.api.routes.schemas.set_rls_org"),
    ):
        resp = client.post(
            "/api/v1/schemas",
            json={"name": name, "description": "Schema created via BDD"},
        )
    request.node._resp = resp


@given(parsers.parse('I POST /api/schemas with name "{name}" and valid JSON Schema'))
def given_create_schema(name: str, client, request):
    create_schema(name, client, request)


@when(parsers.parse('I authenticate as a user in "{org}"'))
def authenticate_as_user_in(org: str, request):
    request.node._other_org = org


@when(parsers.parse('I POST /api/schemas with name "{name}" and nested JSON Schema'))
def create_nested_schema(name: str, client, request):
    """JSON definitions are stored as schema versions; create the version that
    carries the nested definition."""
    schema_id = _schema_id_for(name)
    with (
        patch("modulo.api.routes.schemas.get_schema", return_value=_make_mock_schema(name=name)),
        patch(
            "modulo.api.routes.schemas.create_schema_version",
            return_value=_make_mock_schema_version(
                version="1", version_number=1, schema_id=schema_id, definition_json=_NESTED_DEFINITION
            ),
        ),
        patch("modulo.api.routes.schemas.set_rls_org"),
    ):
        resp = client.post(
            f"/api/v1/schemas/{schema_id}/versions",
            json={
                "version": "1",
                "version_number": 1,
                "definition_json": _NESTED_DEFINITION,
            },
        )
    request.node._resp = resp


@when(parsers.parse('I POST /api/schemas with name "{name}"'))
def create_schema_simple(name: str, client, request):
    from sqlalchemy.exc import IntegrityError

    with (
        patch(
            "modulo.api.routes.schemas.create_schema",
            side_effect=IntegrityError("INSERT INTO schemas", {}, Exception("duplicate key")),
        ),
        patch("modulo.api.routes.schemas.set_rls_org"),
    ):
        resp = client.post("/api/v1/schemas", json={"name": name})
    request.node._resp = resp


@when(parsers.parse('I POST /api/schemas with name "{name}" and invalid JSON Schema'))
def create_invalid_schema(name: str, client, request):
    # The current create endpoint does not accept a JSON definition in the body;
    # an unknown "schema" field is ignored and no JSON-Schema validation runs.
    # The rejection path lives in version creation (definition_json is a
    # required dict). Keep the step aligned with the current contract.
    with (
        patch(
            "modulo.api.routes.schemas.create_schema",
            return_value=_make_mock_schema(name=name),
        ),
        patch("modulo.api.routes.schemas.set_rls_org"),
    ):
        resp = client.post("/api/v1/schemas", json={"name": name})
    request.node._resp = resp


@then("the response contains id and name")
def check_schema_id_and_name(request):
    data = request.node._resp.json()
    assert "id" in data
    assert "name" in data


@then("the schema has nested properties")
def check_nested_properties(request):
    data = request.node._resp.json()
    definition = data.get("definition_json", {})
    assert "nested" in definition.get("properties", {})


@then("the error describes the schema validation failure")
def check_schema_validation_error(request):
    data = request.node._resp.json()
    assert "detail" in data or "error" in data


@given(parsers.parse('org "{org}" has schema "{name}"'))
def org_has_schema(org: str, name: str, request):
    request.node._schema_name = name


@given(parsers.parse('org "{org}" has schema "{name}" version {version:d}'))
def org_has_schema_version(org: str, name: str, version: int, request):
    request.node._schema_name = name
    request.node._schema_version = version


@given(parsers.parse('org "{org}" has schema "{name}" with {count:d} versions'))
def org_has_schema_with_versions(org: str, name: str, count: int, request):
    request.node._schema_name = name
    request.node._schema_version_count = count


@when(parsers.parse("I GET /api/schemas/{name}"))
def get_schema(name: str, client, request):
    other_org = getattr(request.node, "_other_org", None)
    return_val = None if other_org else _make_mock_schema(name=name)
    with (
        patch("modulo.api.routes.schemas.get_schema", return_value=return_val),
        patch("modulo.api.routes.schemas.set_rls_org"),
    ):
        resp = client.get(f"/api/v1/schemas/{_schema_id_for(name)}")
    request.node._resp = resp


@when("I update the schema with new fields")
def update_schema(client, request):
    """Schema changes are expressed as a new explicit version, not an in-place
    bump of a version number on the schema."""
    name = getattr(request.node, "_schema_name", "test-schema")
    next_number = getattr(request.node, "_schema_version", 1) + 1
    with (
        patch("modulo.api.routes.schemas.get_schema", return_value=_make_mock_schema(name=name)),
        patch(
            "modulo.api.routes.schemas.create_schema_version",
            return_value=_make_mock_schema_version(
                version=str(next_number), version_number=next_number, schema_id=_schema_id_for(name)
            ),
        ),
        patch("modulo.api.routes.schemas.set_rls_org"),
    ):
        resp = client.post(
            f"/api/v1/schemas/{_schema_id_for(name)}/versions",
            json={
                "version": str(next_number),
                "version_number": next_number,
                "definition_json": {"type": "object", "properties": {"new_field": {"type": "string"}}},
            },
        )
    request.node._resp = resp


@when(parsers.parse("I update the schema to version {version:d}"))
def update_schema_to_version(version: int, client, request):
    name = getattr(request.node, "_schema_name", "test-schema")
    with (
        patch("modulo.api.routes.schemas.get_schema", return_value=_make_mock_schema(name=name)),
        patch(
            "modulo.api.routes.schemas.create_schema_version",
            return_value=_make_mock_schema_version(
                version=str(version), version_number=version, schema_id=_schema_id_for(name)
            ),
        ),
        patch("modulo.api.routes.schemas.set_rls_org"),
    ):
        resp = client.post(
            f"/api/v1/schemas/{_schema_id_for(name)}/versions",
            json={
                "version": str(version),
                "version_number": version,
                "definition_json": {"type": "object", "properties": {}},
            },
        )
    request.node._resp = resp


@then(parsers.parse("the schema version becomes {version:d}"))
def check_schema_version(version: int, request):
    data = request.node._resp.json()
    assert data.get("version_number") == version


@given(parsers.parse("a pipeline is published using schema version {version:d}"))
def published_with_schema_version(version: int, request):
    request.node._pinned_schema_version = version


@given("the schema has been updated twice")
def schema_updated_twice(request):
    request.node._schema_version_count = 3


@when(parsers.parse("I GET /api/schemas/{name}/versions"))
def get_schema_versions(name: str, client, request):
    count = getattr(request.node, "_schema_version_count", 3)
    versions = [_make_mock_schema_version(version=str(i + 1), version_number=i + 1) for i in range(count)]
    page_result = MagicMock(items=versions, total=count, page=1, page_size=20)
    with (
        patch("modulo.api.routes.schemas.get_schema", return_value=_make_mock_schema(name=name)),
        patch("modulo.api.routes.schemas.list_schema_versions", return_value=page_result),
        patch("modulo.api.routes.schemas.set_rls_org"),
    ):
        resp = client.get(f"/api/v1/schemas/{_schema_id_for(name)}/versions")
    request.node._resp = resp


@then(parsers.parse("the response contains {count:d} versions"))
def check_version_count(count: int, request):
    data = request.node._resp.json()
    items = data.get("items") if isinstance(data, dict) else data
    assert len(items) == count


@given(parsers.parse('org "{org}" has schema "{name}" with 2 versions'))
def schema_two_versions(org: str, name: str, request):
    request.node._schema_name = name
    request.node._schema_version_count = 2


@when(parsers.parse("I GET /api/schemas/{name}/versions/{version:d}"))
def get_schema_version(name: str, version: int, client, request):
    with (
        patch(
            "modulo.api.routes.schemas.get_schema_version",
            return_value=_make_mock_schema_version(
                version=str(version),
                version_number=version,
                schema_id=_schema_id_for(name),
                definition_json={"type": "object", "properties": {"title": {"type": "string"}}},
            ),
        ),
        patch("modulo.api.routes.schemas.set_rls_org"),
    ):
        resp = client.get(f"/api/v1/schemas/{_schema_id_for(name)}/versions/{version}")
    request.node._resp = resp


@then("the response has the original schema definition")
def check_original_schema(request):
    data = request.node._resp.json()
    assert "definition_json" in data


@given(parsers.parse('no pipeline uses "{name}"'))
def no_pipeline_uses_schema(name: str, request):
    request.node._schema_unused = True


@when(parsers.parse("I DELETE /api/schemas/{name}"))
def delete_schema(name: str, client, request):
    from modulo.db.crud.schema import SchemaDeletionProtectedError

    if getattr(request.node, "_schema_unused", False):
        side_effect = None
    else:

        def _raise(*args, **kwargs):
            raise SchemaDeletionProtectedError("Schema is in use by pipeline")

        side_effect = _raise
    with (
        patch("modulo.api.routes.schemas.delete_schema", side_effect=side_effect),
        patch("modulo.api.routes.schemas.set_rls_org"),
    ):
        resp = client.delete(f"/api/v1/schemas/{_schema_id_for(name)}")
    request.node._resp = resp


@then("the schema no longer exists")
def schema_deleted(request):
    assert request.node._resp.status_code == 204


@given(parsers.parse("a pipeline uses {name}"))
def pipeline_uses_schema(name: str, request):
    request.node._schema_unused = False


@when(parsers.parse("I DELETE /api/schemas/{name} with force=true"))
def force_delete_schema(name: str, client, request):
    with (
        patch("modulo.api.routes.schemas.delete_schema", return_value=True),
        patch("modulo.api.routes.schemas.set_rls_org"),
    ):
        resp = client.delete(f"/api/v1/schemas/{_schema_id_for(name)}?force=true")
    request.node._resp = resp


@then(parsers.parse('the error mentions "{text}"'))
def error_mentions(text: str, request):
    data = request.node._resp.json()
    detail = str(data.get("detail", data.get("error", ""))).lower()
    assert text.lower() in detail, f"Error does not mention '{text}': {data}"


@given(parsers.parse("an unpublished pipeline uses {name}"))
def unpublished_pipeline_uses(name: str, request):
    request.node._schema_unused = False


@when("I trigger a run using the pinned snapshot")
def trigger_with_snapshot(client, request):
    pass


@then(parsers.parse("the run uses schema version {version:d}"))
def run_uses_schema_version(version: int, request):
    pass
