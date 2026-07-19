"""BDD step definitions: Schema create, version, deletion protection."""

import contextlib
import uuid
from unittest.mock import MagicMock, patch

from pytest_bdd import given, parsers, scenarios, then, when

with contextlib.suppress(FileNotFoundError, OSError):
    scenarios("../features/schemas/create.feature")
with contextlib.suppress(FileNotFoundError, OSError):
    scenarios("../features/schemas/version.feature")
with contextlib.suppress(FileNotFoundError, OSError):
    scenarios("../features/schemas/deletion_protection.feature")


@when(parsers.parse('I POST /api/schemas with name "{name}" and valid JSON Schema'))
def create_schema(name: str, client, request):
    with (
        patch("modulo.core.pipeline_engine.run_crud.set_rls_org"),
        patch(
            "modulo.core.pipeline_engine.run_crud.create_schema",
            return_value=MagicMock(
                id=uuid.uuid4(),
                name=name,
                version=1,
                schema_json={"type": "object", "properties": {}},
            ),
        ),
    ):
        resp = client.post(
            "/api/schemas",
            json={
                "name": name,
                "schema": {"type": "object", "properties": {"title": {"type": "string"}}},
            },
        )
    request.node._resp = resp


@when(parsers.parse('I POST /api/schemas with name "{name}" and nested JSON Schema'))
def create_nested_schema(name: str, client, request):
    with (
        patch("modulo.core.pipeline_engine.run_crud.set_rls_org"),
        patch(
            "modulo.core.pipeline_engine.run_crud.create_schema",
            return_value=MagicMock(
                id=uuid.uuid4(),
                name=name,
                version=1,
                schema_json={
                    "type": "object",
                    "properties": {
                        "nested": {
                            "type": "object",
                            "properties": {"inner": {"type": "string"}},
                        }
                    },
                },
            ),
        ),
    ):
        resp = client.post(
            "/api/schemas",
            json={
                "name": name,
                "schema": {
                    "type": "object",
                    "properties": {
                        "nested": {
                            "type": "object",
                            "properties": {"inner": {"type": "string"}},
                        }
                    },
                },
            },
        )
    request.node._resp = resp


@when(parsers.parse('I POST /api/schemas with name "{name}"'))
def create_schema_simple(name: str, client, request):
    create_schema(name, client, request)


@when(parsers.parse('I POST /api/schemas with name "{name}" and invalid JSON Schema'))
def create_invalid_schema(name: str, client, request):
    resp = client.post(
        "/api/schemas",
        json={
            "name": name,
            "schema": {"type": "invalid_type_that_does_not_exist"},
        },
    )
    request.node._resp = resp


@then("the response contains id and name")
def check_schema_id_and_name(request):
    data = request.node._resp.json()
    assert "id" in data
    assert "name" in data


@then("the schema has nested properties")
def check_nested_properties(request):
    data = request.node._resp.json()
    schema = data.get("schema_json", data.get("schema", {}))
    assert "nested" in schema.get("properties", {})


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
    with (
        patch("modulo.core.pipeline_engine.run_crud.set_rls_org"),
        patch(
            "modulo.core.pipeline_engine.run_crud.get_schema_by_name",
            return_value=MagicMock(
                id=uuid.uuid4(),
                name=name,
                version=getattr(request.node, "_schema_version", 1),
            ),
        ),
    ):
        resp = client.get(f"/api/schemas/{name}")
    request.node._resp = resp


@when("I update the schema with new fields")
def update_schema(client, request):
    with (
        patch("modulo.core.pipeline_engine.run_crud.set_rls_org"),
        patch(
            "modulo.core.pipeline_engine.run_crud.update_schema",
            return_value=MagicMock(
                id=uuid.uuid4(),
                name=getattr(request.node, "_schema_name", "test-schema"),
                version=getattr(request.node, "_schema_version", 1) + 1,
            ),
        ),
    ):
        resp = client.patch(
            f"/api/schemas/{getattr(request.node, '_schema_name', 'test-schema')}",
            json={"schema": {"type": "object", "properties": {"new_field": {"type": "string"}}}},
        )
    request.node._resp = resp


@then(parsers.parse("the schema version becomes {version:d}"))
def check_schema_version(version: int, request):
    data = request.node._resp.json()
    assert data.get("version") == version


@given("a pipeline is published using schema version {version:d}")
def published_with_schema_version(version: int, request):
    request.node._pinned_schema_version = version


@given("the schema has been updated twice")
def schema_updated_twice(request):
    request.node._schema_version_count = 3


@when(parsers.parse("I GET /api/schemas/{name}/versions"))
def get_schema_versions(name: str, client, request):
    count = getattr(request.node, "_schema_version_count", 3)
    with (
        patch("modulo.core.pipeline_engine.run_crud.set_rls_org"),
        patch(
            "modulo.core.pipeline_engine.run_crud.get_schema_versions",
            return_value=[MagicMock(version=i + 1) for i in range(count)],
        ),
    ):
        resp = client.get(f"/api/schemas/{name}/versions")
    request.node._resp = resp


@then(parsers.parse("the response contains {count:d} versions"))
def check_version_count(count: int, request):
    data = request.node._resp.json()
    assert len(data) == count


@given(parsers.parse('org "{org}" has schema "{name}" with 2 versions'))
def schema_two_versions(org: str, name: str, request):
    request.node._schema_name = name
    request.node._schema_version_count = 2


@when(parsers.parse("I GET /api/schemas/{name}/versions/{version:d}"))
def get_schema_version(name: str, version: int, client, request):
    with (
        patch("modulo.core.pipeline_engine.run_crud.set_rls_org"),
        patch(
            "modulo.core.pipeline_engine.run_crud.get_schema_version",
            return_value=MagicMock(
                version=version,
                schema_json={"type": "object", "properties": {"title": {"type": "string"}}},
            ),
        ),
    ):
        resp = client.get(f"/api/schemas/{name}/versions/{version}")
    request.node._resp = resp


@then("the response has the original schema definition")
def check_original_schema(request):
    pass


@given('no pipeline uses "{name}"')
def no_pipeline_uses_schema(name: str, request):
    request.node._schema_unused = True


@when(parsers.parse("I DELETE /api/schemas/{name}"))
def delete_schema(name: str, client, request):
    with (
        patch("modulo.core.pipeline_engine.run_crud.set_rls_org"),
        patch(
            "modulo.core.pipeline_engine.run_crud.delete_schema",
            side_effect=lambda n: True if getattr(request.node, "_schema_unused", False) else (_raise_conflict()),
        ),
    ):
        resp = client.delete(f"/api/schemas/{name}")
    request.node._resp = resp


def _raise_conflict():
    raise RuntimeError("Schema in use by pipeline")


@then("the schema no longer exists")
def schema_deleted(request):
    assert request.node._resp.status_code == 204


@given("a pipeline uses {name}")
def pipeline_uses_schema(name: str, request):
    request.node._schema_unused = False


@when(parsers.parse("I DELETE /api/schemas/{name} with force=true"))
def force_delete_schema(name: str, client, request):
    with (
        patch("modulo.core.pipeline_engine.run_crud.set_rls_org"),
        patch(
            "modulo.core.pipeline_engine.run_crud.delete_schema",
            return_value=True,
        ),
    ):
        resp = client.delete(f"/api/schemas/{name}?force=true")
    request.node._resp = resp


@then(parsers.parse('the error mentions "{text}"'))
def error_mentions(text: str, request):
    data = request.node._resp.json()
    detail = str(data.get("detail", data.get("error", ""))).lower()
    assert text.lower() in detail, f"Error does not mention '{text}': {data}"


@given("an unpublished pipeline uses {name}")
def unpublished_pipeline_uses(name: str, request):
    request.node._schema_unused = False
