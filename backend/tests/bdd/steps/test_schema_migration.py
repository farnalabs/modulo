"""BDD step definitions: Schema Migration (dry-run, plan, apply)."""

import json
import uuid
from unittest.mock import MagicMock, patch

from pytest_bdd import given, parsers, scenarios, then, when

try:
    scenarios("../features/schemas/schema_migration.feature")
except (FileNotFoundError, OSError):
    pass

_ORG_ID = uuid.UUID("00000000-0000-0000-0000-000000000001")
_SCHEMA_DEFS: dict[str, dict] = {}
_MOCK_SCHEMAS: dict[str, MagicMock] = {}
_MOCK_VERSIONS: dict[str, MagicMock] = {}


def _make_schema(name: str) -> MagicMock:
    s = MagicMock()
    s.id = uuid.uuid4()
    s.organisation_id = _ORG_ID
    s.name = name
    s.description = ""
    return s


def _make_schema_version(schema_id: uuid.UUID, fields: dict[str, str]) -> MagicMock:
    sv = MagicMock()
    sv.id = uuid.uuid4()
    sv.schema_id = schema_id
    sv.version = "1.0"
    sv.definition_json = {
        "type": "object",
        "properties": {name: {"type": t} for name, t in fields.items()},
    }
    return sv


@given(
    parsers.parse(
        'a source schema version with fields {fields_json}',
    ),
    target_fixture="source_schema_ctx",
)
def step_source_schema_with_fields(fields_json: str, request) -> dict:
    fields = json.loads(fields_json)
    schema = _make_schema("source-schema")
    version = _make_schema_version(schema.id, fields)
    _MOCK_SCHEMAS["source"] = schema
    _MOCK_VERSIONS["source"] = version
    return {"schema": schema, "version": version, "fields": fields}


@given(
    parsers.parse(
        'a target schema version with fields {fields_json}',
    ),
    target_fixture="target_schema_ctx",
)
def step_target_schema_with_fields(fields_json: str, request) -> dict:
    fields = json.loads(fields_json)
    schema = _make_schema("target-schema")
    version = _make_schema_version(schema.id, fields)
    _MOCK_SCHEMAS["target"] = schema
    _MOCK_VERSIONS["target"] = version
    return {"schema": schema, "version": version, "fields": fields}


@given(parsers.parse("a source definition with field {field_json}"), target_fixture="source_def")
def step_source_definition(field_json: str) -> dict:
    parsed = json.loads(field_json)
    return {"type": "object", "properties": {k: {"type": v} for k, v in parsed.items()}}


@given(parsers.parse("a target definition with field {field_json}"), target_fixture="target_def")
def step_target_definition(field_json: str) -> dict:
    parsed = json.loads(field_json)
    return {"type": "object", "properties": {k: {"type": v} for k, v in parsed.items()}}


@when(parsers.parse("I POST /api/v1/schemas/migrate with dry_run=true"))
def step_migrate_dry_run(request, client):
    _call_migrate(request, client, dry_run=True)


@when(
    parsers.parse(
        "I POST /api/v1/schemas/migrate with dry_run=true and data {data_json}",
    ),
)
def step_migrate_dry_run_with_data(data_json: str, request, client):
    _call_migrate(request, client, dry_run=True, data_override=json.loads(data_json))


@when("I POST /api/v1/schemas/migrate/plan")
def step_migrate_plan(request, client):
    source_def = getattr(request.node, "_source_def", {})
    target_def = getattr(request.node, "_target_def", {})

    resp = client.post("/api/v1/schemas/migrate/plan", json={
        "from_definition": source_def,
        "to_definition": target_def,
    })
    request.node._resp = resp


@then("the response includes a migration plan")
def step_response_has_plan(request):
    data = request.node._resp.json()
    assert "plan" in data, f"Response missing 'plan': {data}"
    plan = data["plan"]
    for key in ("field_additions", "field_removals", "type_changes", "renames"):
        assert key in plan, f"Plan missing '{key}': {plan}"


@then("the response includes dry_run: true")
def step_response_dry_run_flag(request):
    data = request.node._resp.json()
    plan = data.get("plan", {})
    assert plan.get("dry_run") is True, f"Plan missing dry_run=true: {plan}"


@then("the migrated_data equals the original input")
def step_migrated_data_equals_original(request):
    data = request.node._resp.json()
    original = getattr(request.node, "_original_data", {})
    assert data["migrated_data"] == original, (
        f"migrated_data changed during dry_run: {data['migrated_data']} != {original}"
    )


@then('the migrated_data still contains "full_name"')
def step_migrated_data_contains_full_name(request):
    data = request.node._resp.json()
    assert "full_name" in data["migrated_data"], (
        f"Dry-run should not remove full_name: {data['migrated_data']}"
    )


@then(
    parsers.parse('the plan contains a rename from "{old_name}" to "{new_name}"'),
)
def step_plan_contains_rename(old_name: str, new_name: str, request):
    data = request.node._resp.json()
    plan = data if "field_additions" in data else data.get("plan", {})
    renames = plan.get("renames", {})
    assert renames.get(old_name) == new_name, (
        f"Expected rename {old_name} -> {new_name}, got {renames}"
    )


@then(
    parsers.parse('the plan lists "{field}" in field_additions'),
)
def step_plan_lists_field_addition(field: str, request):
    data = request.node._resp.json()
    plan = data if "field_additions" in data else data.get("plan", {})
    additions = plan.get("field_additions", {})
    assert field in additions, (
        f"Expected '{field}' in field_additions, got {additions}"
    )


def _call_migrate(request, client, dry_run: bool = False, data_override: dict | None = None) -> None:
    source_ctx = getattr(request.node, "_source_schema_ctx", None)
    target_ctx = getattr(request.node, "_target_schema_ctx", None)

    if not source_ctx or not target_ctx:
        source_schema = _make_schema("source-schema")
        source_version = _make_schema_version(source_schema.id, {"name": "string"})
        target_schema = _make_schema("target-schema")
        target_version = _make_schema_version(target_schema.id, {"name": "string"})
        source_ctx = {"schema": source_schema, "version": source_version}
        target_ctx = {"schema": target_schema, "version": target_version}

    data_payload = data_override or {"name": "test"}
    request.node._original_data = data_payload

    with (
        patch("modulo.api.routes.schemas.set_rls_org"),
        patch("modulo.api.routes.schemas.get_schema") as mock_get_schema,
        patch("modulo.api.routes.schemas._get_latest_version") as mock_latest,
    ):
        def _get_schema_side(session, schema_id):
            for ctx in (source_ctx, target_ctx):
                if ctx["schema"].id == schema_id:
                    return ctx["schema"]
            return None

        mock_get_schema.side_effect = _get_schema_side

        def _latest_side(session, schema_id):
            for ctx in (source_ctx, target_ctx):
                if ctx["schema"].id == schema_id:
                    return ctx["version"]
            return None

        mock_latest.side_effect = _latest_side

        qs = "?dry_run=true" if dry_run else ""
        resp = client.post(
            f"/api/v1/schemas/migrate{qs}",
            json={
                "from_schema_id": str(source_ctx["schema"].id),
                "to_schema_id": str(target_ctx["schema"].id),
                "data": data_payload,
            },
        )
    request.node._resp = resp
