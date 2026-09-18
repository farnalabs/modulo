"""BDD step definitions: Schema Migration (dry-run, plan, apply)."""

import asyncio
import contextlib
import json
import uuid
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

from pytest_bdd import given, parsers, scenarios, then, when

from modulo.core.schema_registry.migration import MigrationRegistry, SchemaMigration

with contextlib.suppress(FileNotFoundError, OSError):
    scenarios("../features/schemas/schema_migration.feature")

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


# ---------------------------------------------------------------------------
# MigrationRegistry best-effort partial chain (direct component scenarios)
# ---------------------------------------------------------------------------


def _visiting_migration(source: str, target: str) -> SchemaMigration:
    """Build a migration that records its step in ``data["visited"]``."""
    label = f"{source}->{target}"

    def _migrate(data: dict[str, Any]) -> dict[str, Any]:
        result = dict(data)
        result["visited"] = [*result.get("visited", []), label]
        return result

    return SchemaMigration(source_version=source, target_version=target, func=_migrate)


def _registry_ctx(request) -> MigrationRegistry:
    registry: MigrationRegistry | None = getattr(request.node, "_migration_registry", None)
    if registry is None:
        registry = MigrationRegistry()
        request.node._migration_registry = registry
    return registry


@given("a migration registry with no registered migrations")
def step_empty_migration_registry(request) -> None:
    request.node._migration_registry = MigrationRegistry()
    request.node._migration_partial_result = None


@given(parsers.parse('a migration registry with a migration registered from "{source}" to "{target}"'))
def step_register_migration(source: str, target: str, request) -> None:
    registry = _registry_ctx(request)

    def _register() -> None:
        asyncio.run(registry.register(source, target, _visiting_migration(source, target).func))

    _register()


@given(parsers.parse('a migration registered from "{source}" to "{target}"'))
def step_register_additional_migration(source: str, target: str, request) -> None:
    registry = _registry_ctx(request)

    def _register() -> None:
        asyncio.run(registry.register(source, target, _visiting_migration(source, target).func))

    _register()


@when(parsers.parse('I apply a partial migration from "{source}" to "{target}" on data {data_json}'))
def step_apply_partial_migration(source: str, target: str, data_json: str, request) -> None:
    registry = _registry_ctx(request)

    async def _apply() -> tuple[dict[str, Any], list[str]]:
        return await registry.apply_partial(json.loads(data_json), source, target)

    request.node._migration_partial_result = asyncio.run(_apply())


@when(parsers.parse('I dry-run a partial migration from "{source}" to "{target}" on data {data_json}'))
def step_dry_run_partial_migration(source: str, target: str, data_json: str, request) -> None:
    registry = _registry_ctx(request)
    data = json.loads(data_json)

    async def _dry_run() -> tuple[list[dict[str, Any]], list[str]]:
        return await registry.dry_run_partial(data, source, target)

    steps, gaps = asyncio.run(_dry_run())
    request.node._migration_dry_run_result = (steps, gaps)
    request.node._migration_partial_result = (data, gaps)


@then(parsers.parse('the partial migration applied the steps "{steps_csv}"'))
def step_partial_applied_steps(steps_csv: str, request) -> None:
    expected = [s.strip() for s in steps_csv.split(",")]
    result = request.node._migration_partial_result
    assert result is not None, "no partial migration result for this scenario"
    migrated, _gaps = result
    visited = migrated.get("visited", [])
    assert visited == expected, f"expected visited steps {expected}, got {visited}"


@then("the partial migration reports a chain gap")
def step_partial_reports_gap(request) -> None:
    result = request.node._migration_partial_result
    assert result is not None, "no partial migration result for this scenario"
    _migrated, gaps = result
    assert len(gaps) > 0, f"expected chain gaps, got none: {result}"


@then("the partial migration reports no chain gaps")
def step_partial_reports_no_gap(request) -> None:
    result = request.node._migration_partial_result
    assert result is not None, "no partial migration result for this scenario"
    _migrated, gaps = result
    assert gaps == [], f"expected no chain gaps, got {gaps}"


@then(parsers.parse('the dry-run describes "{count}" steps'))
def step_dry_run_describes_steps(count: str, request) -> None:
    result = request.node._migration_dry_run_result
    assert result is not None, "no dry-run result for this scenario"
    steps, gaps = result
    assert len(steps) == int(count), f"expected {count} dry-run steps, got {len(steps)}"
    assert gaps == [], f"expected no chain gaps, got {gaps}"


@then(parsers.parse('the migrated data still contains "{field}"'))
def step_partial_migrated_data_still_contains(field: str, request) -> None:
    result = request.node._migration_partial_result
    assert result is not None, "no partial migration result for this scenario"
    migrated, _gaps = result
    assert field in migrated, f"Expected '{field}' preserved: {migrated}"


@given(parsers.parse("a source schema version with fields {fields_json}"))
def step_source_schema_with_fields(fields_json: str, request) -> None:
    fields = json.loads(fields_json)
    schema = _make_schema("source-schema")
    version = _make_schema_version(schema.id, fields)
    _MOCK_SCHEMAS["source"] = schema
    _MOCK_VERSIONS["source"] = version
    request.node._source_schema_ctx = {"schema": schema, "version": version, "fields": fields}


@given(parsers.parse("a target schema version with fields {fields_json}"))
def step_target_schema_with_fields(fields_json: str, request) -> None:
    fields = json.loads(fields_json)
    schema = _make_schema("target-schema")
    version = _make_schema_version(schema.id, fields)
    _MOCK_SCHEMAS["target"] = schema
    _MOCK_VERSIONS["target"] = version
    request.node._target_schema_ctx = {"schema": schema, "version": version, "fields": fields}


@given(parsers.parse("a source definition with field {field_json}"))
def step_source_definition(field_json: str, request) -> None:
    parsed = json.loads(field_json)
    request.node._source_def = {"type": "object", "properties": {k: {"type": v} for k, v in parsed.items()}}


@given(parsers.parse("a source definition with fields {field_json}"))
def step_source_definitions(field_json: str, request) -> None:
    parsed = json.loads(field_json)
    request.node._source_def = {"type": "object", "properties": {k: {"type": v} for k, v in parsed.items()}}


@given(parsers.parse("a target definition with field {field_json}"))
def step_target_definition(field_json: str, request) -> None:
    parsed = json.loads(field_json)
    request.node._target_def = {"type": "object", "properties": {k: {"type": v} for k, v in parsed.items()}}


@given(parsers.parse("a target definition with fields {field_json}"))
def step_target_definitions(field_json: str, request) -> None:
    parsed = json.loads(field_json)
    request.node._target_def = {"type": "object", "properties": {k: {"type": v} for k, v in parsed.items()}}


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


@when(parsers.parse("I POST /api/v1/schemas/migrate with data {data_json}"))
def step_migrate_with_data(data_json: str, request, client):
    _call_migrate(request, client, dry_run=False, data_override=json.loads(data_json))


@when("I POST /api/v1/schemas/migrate/plan")
def step_migrate_plan(request, client):
    source_def = getattr(request.node, "_source_def", {})
    target_def = getattr(request.node, "_target_def", {})

    with patch("modulo.api.routes.schemas.append_audit_event_isolated", new_callable=AsyncMock) as mock_audit_append:
        request.node._audit_append = mock_audit_append
        resp = client.post(
            "/api/v1/schemas/migrate/plan",
            json={
                "from_definition": source_def,
                "to_definition": target_def,
            },
        )
    request.node._resp = resp


@when("I POST /api/v1/schemas/migrate/plan unauthenticated")
def step_migrate_plan_unauthenticated(request, unauth_client):
    from modulo.api.main import app
    from modulo.auth.dependencies import get_current_tenant_user, get_current_tenant_user_or_api_key, get_current_user

    for dep in (get_current_user, get_current_tenant_user, get_current_tenant_user_or_api_key):
        app.dependency_overrides.pop(dep, None)

    source_def = getattr(request.node, "_source_def", {})
    target_def = getattr(request.node, "_target_def", {})

    resp = unauth_client.post(
        "/api/v1/schemas/migrate/plan",
        json={
            "from_definition": source_def,
            "to_definition": target_def,
        },
    )
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


@then(parsers.parse('the migrated_data still contains "{field}"'))
def step_migrated_data_still_contains(field: str, request):
    data = request.node._resp.json()
    assert field in data["migrated_data"], f"Expected '{field}' preserved: {data['migrated_data']}"


@then(
    parsers.parse('the plan contains a rename from "{old_name}" to "{new_name}"'),
)
def step_plan_contains_rename(old_name: str, new_name: str, request):
    data = request.node._resp.json()
    plan = data if "field_additions" in data else data.get("plan", {})
    renames = plan.get("renames", {})
    assert renames.get(old_name) == new_name, f"Expected rename {old_name} -> {new_name}, got {renames}"


@then(
    parsers.parse('the plan lists "{field}" in field_additions'),
)
def step_plan_lists_field_addition(field: str, request):
    data = request.node._resp.json()
    plan = data if "field_additions" in data else data.get("plan", {})
    additions = plan.get("field_additions", {})
    assert field in additions, f"Expected '{field}' in field_additions, got {additions}"


@then(parsers.parse('the migrated_data no longer contains "{field}"'))
def step_migrated_data_no_longer_contains(field: str, request):
    data = request.node._resp.json()
    assert field not in data["migrated_data"], f"Expected '{field}' to be removed: {data['migrated_data']}"


@then(parsers.parse('an audit event "{event_type}" is recorded'))
def step_audit_event_recorded(event_type: str, request):
    mock_append = getattr(request.node, "_audit_append", None)
    assert mock_append is not None, "append_audit_event was not patched for this scenario"
    call = mock_append.await_args
    assert call.kwargs.get("event_type") == event_type, f"Expected event_type {event_type}, got {call.kwargs}"


@then(parsers.parse('an audit event "{event_type}" is recorded with dry_run: true'))
def step_audit_event_recorded_dry_run(event_type: str, request):
    mock_append = getattr(request.node, "_audit_append", None)
    assert mock_append is not None, "append_audit_event was not patched for this scenario"
    call = mock_append.await_args
    assert call.kwargs.get("event_type") == event_type, f"Expected event_type {event_type}, got {call.kwargs}"
    payload = call.kwargs.get("payload") or {}
    assert payload.get("dry_run") is True, f"Expected dry_run=True in payload, got {payload}"


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
        patch("modulo.api.routes.schemas.append_audit_event_isolated", new_callable=AsyncMock) as mock_audit_append,
    ):
        request.node._audit_append = mock_audit_append

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
