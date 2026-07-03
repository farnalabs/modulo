"""BDD step definitions: Schema Inference (AI-assisted schema drafting)."""

import uuid
from datetime import datetime
from unittest.mock import MagicMock, PropertyMock, patch

from pytest_bdd import given, parsers, scenarios, then, when

try:
    scenarios("../features/schemas/schema_inference.feature")
except (FileNotFoundError, OSError):
    pass

try:
    scenarios("../features/connectors/schema_inference.feature")
except (FileNotFoundError, OSError):
    pass

_ORG_ID = uuid.UUID("00000000-0000-0000-0000-000000000001")
_CONNECTOR_INSTANCES: dict[str, dict] = {}
_MOCK_SCHEMAS: dict[str, MagicMock] = {}
_MOCK_VERSIONS: list[MagicMock] = []


def _make_connector(name: str) -> MagicMock:
    ci = MagicMock()
    ci.id = uuid.uuid4()
    ci.name = name
    ci.organisation_id = _ORG_ID
    ci.connector_type_id = "github" if "github" in name.lower() else "jira"
    ci.config_json = {}
    ci.credentials_ciphertext = b"encrypted"
    ci.visibility = "org"
    ci.allowed_operations = None
    _CONNECTOR_INSTANCES[name] = {"mock": ci, "id": ci.id, "name": name}
    return ci


def _make_connector_of_type(name: str, type_id: str) -> MagicMock:
    ci = MagicMock()
    ci.id = uuid.uuid4()
    ci.name = name
    ci.organisation_id = _ORG_ID
    ci.connector_type_id = type_id
    ci.config_json = {}
    ci.credentials_ciphertext = b"encrypted"
    ci.visibility = "org"
    ci.allowed_operations = None
    _CONNECTOR_INSTANCES[name] = {"mock": ci, "id": ci.id, "name": name}
    return ci


def _make_backend() -> MagicMock:
    mb = MagicMock()
    mb.id = uuid.uuid4()
    mb.provider = "anthropic"
    mb.model_id = "claude-sonnet-4-20250514"
    mb.credentials_ciphertext = b"encrypted"
    mb.default_params = {}
    return mb


def _base_infer_patches(mock_ci, mock_mb, records, expected_schema, backend_id=None):
    if mock_mb is None:
        page_result = MagicMock(items=[], total=0, page=1, page_size=1)
    else:
        page_result = MagicMock(items=[mock_mb], total=1, page=1, page_size=1)
    if backend_id is None:
        backend_id = uuid.uuid4()

    return [
        patch("modulo.api.routes.schemas.get_connector_instance", return_value=mock_ci),
        patch("modulo.api.routes.schemas.list_model_backends", return_value=page_result),
        patch("modulo.api.routes.schemas.set_rls_org"),
        patch("modulo.api.routes.schemas.ConnectorHub.sample", return_value=records),
        patch("modulo.api.routes.schemas.SchemaInferenceService.infer", return_value=expected_schema),
        patch("modulo.api.routes.schemas.ConnectorHub.initialise"),
        patch("modulo.api.routes.schemas.ModelBackendHub.initialise"),
        patch(
            "modulo.api.routes.schemas.ModelBackendHub.backend_ids",
            new_callable=PropertyMock(return_value=frozenset({backend_id})),
        ),
        patch("modulo.api.routes.schemas.ModelBackendHub.get", return_value=MagicMock()),
        patch("modulo.api.routes.schemas.create_secrets_backend"),
        patch("modulo.core.audit_logger.append_audit_event"),
    ]


@given(parsers.parse('a connector instance "{name}" with sample data'))
def step_connector_with_samples(name: str, request):
    mock_ci = _make_connector(name)
    request.node._connector_name = name
    request.node._mock_ci = mock_ci
    request.node._records = [
        {"id": 1, "title": "Fix login bug", "completed": False, "priority": 3},
        {"id": 2, "title": "Add tests", "completed": True, "priority": 1},
    ]


@given(parsers.parse('a connector instance "{name}" with connector type "{type_id}"'))
def step_connector_with_type(name: str, type_id: str, request):
    mock_ci = _make_connector_of_type(name, type_id)
    request.node._connector_name = name
    request.node._mock_ci = mock_ci
    request.node._records = [
        {"id": 1, "title": "Test item", "completed": False},
    ]


@given("a model backend is configured")
def step_model_backend_configured(request):
    request.node._model_backend = _make_backend()


@given("a connector with mixed-type sample records")
def step_connector_mixed_types(request):
    mock_ci = _make_connector("mixed-types-source")
    request.node._mock_ci = mock_ci
    request.node._connector_name = "mixed-types-source"
    request.node._records = [
        {"title": "Bug report", "priority": 1, "completed": False, "tags": ["bug"]},
        {"title": "Feature request", "priority": 2, "completed": True, "tags": ["feature", "enhancement"]},
        {"title": "Documentation", "priority": 3, "completed": False, "tags": ["docs"]},
    ]
    request.node._model_backend = _make_backend()
    request.node._expected_schema = {
        "type": "object",
        "properties": {
            "title": {"type": "string", "description": "Item title"},
            "priority": {"type": "number", "description": "Priority level"},
            "completed": {"type": "boolean", "description": "Completion status"},
            "tags": {"type": "array", "items": {"type": "string"}, "description": "Tags"},
        },
        "required": ["title", "tags"],
    }


@given("sample records with a field having few distinct values")
def step_connector_enum_field(request):
    mock_ci = _make_connector("status-source")
    request.node._mock_ci = mock_ci
    request.node._connector_name = "status-source"
    request.node._records = [
        {"id": 1, "status": "open", "title": "Task 1"},
        {"id": 2, "status": "in_progress", "title": "Task 2"},
        {"id": 3, "status": "open", "title": "Task 3"},
        {"id": 4, "status": "closed", "title": "Task 4"},
        {"id": 5, "status": "in_progress", "title": "Task 5"},
    ]
    request.node._model_backend = _make_backend()
    request.node._expected_schema = {
        "type": "object",
        "properties": {
            "id": {"type": "number", "description": "Item ID"},
            "status": {
                "type": "string",
                "description": "Status",
                "enum": ["open", "in_progress", "closed"],
            },
            "title": {"type": "string", "description": "Item title"},
        },
        "required": ["id", "status"],
    }


@given(parsers.parse('a connector instance "{name}" with sample data'))
def step_connector_with_samples_alt(name: str, request):
    step_connector_with_samples(name, request)


@when(parsers.parse("I POST /api/v1/schemas/infer with the connector instance"))
def step_infer_schema(request, client):
    mock_ci = request.node._mock_ci
    records = request.node._records
    mock_mb = request.node._model_backend
    expected_schema = getattr(request.node, "_expected_schema", {
        "type": "object",
        "properties": {"title": {"type": "string"}, "completed": {"type": "boolean"}},
        "required": ["title"],
    })
    ci_id = str(mock_ci.id) if mock_ci is not None else str(uuid.uuid4())

    with contextlib_patch_multi(_base_infer_patches(mock_ci, mock_mb, records, expected_schema)):
        resp = client.post(
            "/api/v1/schemas/infer",
            json={
                "connector_instance_id": ci_id,
                "sample_query": {"resource": "issues", "filters": {}, "limit": 10},
            },
        )
    request.node._resp = resp
    request.node._inferred_definition = expected_schema


@when(parsers.parse("I POST /api/v1/schemas/infer with the connector instance and no limit"))
def step_infer_schema_no_limit(request, client):
    mock_ci = request.node._mock_ci
    records = request.node._records
    mock_mb = request.node._model_backend
    expected_schema = {
        "type": "object",
        "properties": {"title": {"type": "string"}, "completed": {"type": "boolean"}},
        "required": ["title"],
    }
    backend_id = uuid.uuid4()
    page_result = MagicMock(items=[mock_mb], total=1, page=1, page_size=1)

    def capture_limit(connector_id, resource, filters, limit):
        request.node._captured_limit = limit
        return records

    with (
        patch("modulo.api.routes.schemas.get_connector_instance", return_value=mock_ci),
        patch("modulo.api.routes.schemas.list_model_backends", return_value=page_result),
        patch("modulo.api.routes.schemas.set_rls_org"),
        patch("modulo.api.routes.schemas.ConnectorHub.sample", side_effect=capture_limit),
        patch("modulo.api.routes.schemas.SchemaInferenceService.infer", return_value=expected_schema),
        patch("modulo.api.routes.schemas.ConnectorHub.initialise"),
        patch("modulo.api.routes.schemas.ModelBackendHub.initialise"),
        patch(
            "modulo.api.routes.schemas.ModelBackendHub.backend_ids",
            new_callable=PropertyMock(return_value=frozenset({backend_id})),
        ),
        patch("modulo.api.routes.schemas.ModelBackendHub.get", return_value=MagicMock()),
        patch("modulo.api.routes.schemas.create_secrets_backend"),
        patch("modulo.core.audit_logger.append_audit_event"),
    ):
        resp = client.post(
            "/api/v1/schemas/infer",
            json={
                "connector_instance_id": str(mock_ci.id),
                "sample_query": {"resource": "issues", "filters": {}},
            },
        )
    request.node._resp = resp
    request.node._inferred_definition = expected_schema


@when(parsers.parse('I POST /api/v1/schemas/infer with connector id "{name}"'))
def step_infer_schema_by_name(name: str, request, client):
    mock_ci = request.node._mock_ci
    records = request.node._records
    mock_mb = request.node._model_backend
    expected_schema = {
        "type": "object",
        "properties": {"title": {"type": "string"}, "completed": {"type": "boolean"}},
        "required": ["title"],
    }

    with contextlib_patch_multi(_base_infer_patches(mock_ci, mock_mb, records, expected_schema)):
        resp = client.post(
            "/api/v1/schemas/infer",
            json={
                "connector_instance_id": str(mock_ci.id),
                "sample_query": {"resource": "issues", "filters": {}, "limit": 10},
            },
        )
    request.node._resp = resp
    request.node._inferred_definition = expected_schema
    request.node._connector_name = name


@when("I infer a schema from the connector")
def step_infer_schema_publish_flow(request, client):
    mock_ci = request.node._mock_ci
    records = request.node._records
    mock_mb = request.node._model_backend
    expected_schema = {
        "type": "object",
        "properties": {
            "title": {"type": "string", "description": "Issue title"},
            "completed": {"type": "boolean", "description": "Completion status"},
        },
        "required": ["title"],
    }

    with contextlib_patch_multi(_base_infer_patches(mock_ci, mock_mb, records, expected_schema)):
        resp = client.post(
            "/api/v1/schemas/infer",
            json={
                "connector_instance_id": str(mock_ci.id),
                "sample_query": {"resource": "issues", "filters": {}, "limit": 10},
            },
        )
    request.node._inferred_response = resp
    request.node._inferred_definition = expected_schema


@when(parsers.parse('I create a schema "{name}" from the draft'))
def step_create_schema_from_draft(name: str, request, client):
    mock_schema = MagicMock()
    mock_schema.id = uuid.uuid4()
    mock_schema.organisation_id = _ORG_ID
    mock_schema.name = name
    mock_schema.description = "Inferred schema"
    mock_schema.abstract_name = None
    mock_schema.created_by = uuid.UUID("00000000-0000-0000-0000-000000000002")
    mock_schema.created_at = datetime.now()
    mock_schema.updated_at = datetime.now()
    _MOCK_SCHEMAS[name] = mock_schema

    with (
        patch("modulo.api.routes.schemas.set_rls_org"),
        patch("modulo.api.routes.schemas.create_schema", return_value=mock_schema),
    ):
        resp = client.post(
            "/api/v1/schemas",
            json={"name": name, "description": "Inferred from connector"},
        )
    request.node._schema_name = name
    request.node._schema_id = mock_schema.id
    request.node._resp = resp


@when(parsers.parse('I publish version "{version}" of the schema with the inferred definition'))
def step_publish_schema_version(version: str, request, client):
    mock_sv = MagicMock()
    mock_sv.id = uuid.uuid4()
    mock_sv.organisation_id = _ORG_ID
    mock_sv.schema_id = request.node._schema_id
    mock_sv.version = version
    mock_sv.version_number = 1
    mock_sv.definition_json = request.node._inferred_definition
    mock_sv.published = True
    mock_sv.created_by = uuid.UUID("00000000-0000-0000-0000-000000000002")
    mock_sv.created_at = datetime.now()
    mock_sv.updated_at = datetime.now()
    _MOCK_VERSIONS.append(mock_sv)

    with (
        patch("modulo.api.routes.schemas.set_rls_org"),
        patch("modulo.api.routes.schemas.get_schema", return_value=_MOCK_SCHEMAS.get(request.node._schema_name)),
        patch("modulo.api.routes.schemas.create_schema_version", return_value=mock_sv),
    ):
        resp = client.post(
            f"/api/v1/schemas/{request.node._schema_id}/versions",
            json={
                "version": version,
                "version_number": 1,
                "definition_json": request.node._inferred_definition,
                "published": True,
            },
        )
    request.node._resp = resp


@then("the response contains definition_json")
def step_response_has_definition_json(request):
    data = request.node._resp.json()
    assert "definition_json" in data, f"Response missing definition_json: {data}"
    assert isinstance(data["definition_json"], dict)


@then("the response contains sample_count and suggestion_name")
def step_response_has_sample_and_name(request):
    data = request.node._resp.json()
    assert "sample_count" in data, f"Response missing sample_count: {data}"
    assert "suggestion_name" in data, f"Response missing suggestion_name: {data}"
    assert isinstance(data["sample_count"], int)
    assert isinstance(data["suggestion_name"], str)


@then(parsers.parse('the inferred schema has "{type_name}" type for field "{field_name}"'))
def step_assert_field_type(type_name: str, field_name: str, request):
    data = request.node._resp.json()
    definition = data["definition_json"]
    properties = definition.get("properties", {})
    assert field_name in properties, f"Field '{field_name}' not found in schema properties: {list(properties.keys())}"
    assert properties[field_name]["type"] == type_name, (
        f"Expected field '{field_name}' to have type '{type_name}', got '{properties[field_name]['type']}'"
    )


@then("the inferred schema includes an enum constraint for field \"status\"")
def step_assert_enum_constraint(request):
    data = request.node._resp.json()
    definition = data["definition_json"]
    properties = definition.get("properties", {})
    assert "status" in properties, f"Field 'status' not found in schema properties: {list(properties.keys())}"
    assert "enum" in properties["status"], f"Field 'status' missing enum constraint: {properties['status']}"
    assert isinstance(properties["status"]["enum"], list)
    assert len(properties["status"]["enum"]) > 0


@then("the sample query limit defaults to 10")
def step_assert_default_limit(request):
    captured = getattr(request.node, "_captured_limit", None)
    assert captured is not None, "No limit was captured from the sample call"
    assert captured == 10, f"Expected default limit 10, got {captured}"


@then(parsers.parse('the suggestion name mentions "{text}"'))
def step_suggestion_name_mentions(text: str, request):
    data = request.node._resp.json()
    suggestion_name = data.get("suggestion_name", "")
    assert text.lower() in suggestion_name.lower(), (
        f"Suggestion name '{suggestion_name}' does not contain '{text}'"
    )


@then("the schema version is published")
def step_schema_version_published(request):
    data = request.node._resp.json()
    assert data.get("published") is True, f"Schema version not published: {data}"


# ---------------------------------------------------------------------------
# Step definitions for features/connectors/schema_inference.feature
# ---------------------------------------------------------------------------


@given("a connector instance with sample data")
def step_connector_with_samples_unnamed(request):
    step_connector_with_samples("default-connector", request)


@given("a non-existent connector instance")
def step_non_existent_connector(request):
    request.node._mock_ci = None
    request.node._connector_name = None
    request.node._records = []
    request.node._model_backend = _make_backend()


@given("no model backends are configured")
def step_no_model_backends(request):
    request.node._model_backend = None
    request.node._expected_schema = {
        "type": "object",
        "properties": {"title": {"type": "string"}, "completed": {"type": "boolean"}},
        "required": ["title"],
    }


@given("a generated schema definition")
def step_generated_schema_definition(request):
    request.node._schema_definition = {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "type": "object",
        "properties": {
            "title": {"type": "string"},
            "completed": {"type": "boolean"},
        },
        "required": ["title"],
    }


@given("a source schema and a target schema")
def step_source_and_target_schemas(request):
    request.node._source_def = {
        "type": "object",
        "properties": {"name": {"type": "string"}},
    }
    request.node._target_def = {
        "type": "object",
        "properties": {
            "name": {"type": "string"},
            "email": {"type": "string"},
        },
    }


@when("I validate the schema")
def step_validate_schema(request, client):
    definition = getattr(request.node, "_schema_definition", {})
    resp = client.post("/api/v1/schemas/validate", json={"definition": definition})
    request.node._resp = resp


@when(parsers.parse("I POST /api/v1/schemas/migrate/plan with both definitions"))
def step_migrate_plan_from_feature(request, client):
    source_def = getattr(request.node, "_source_def", {})
    target_def = getattr(request.node, "_target_def", {})
    resp = client.post(
        "/api/v1/schemas/migrate/plan",
        json={"from_definition": source_def, "to_definition": target_def},
    )
    request.node._resp = resp


@then("the response contains a definition_json")
def step_response_contains_definition_json(request):
    data = request.node._resp.json()
    assert "definition_json" in data


@then("the response has a suggestion_name")
def step_response_has_suggestion_name(request):
    data = request.node._resp.json()
    assert "suggestion_name" in data


@then("the schema is structurally valid")
def step_schema_is_structurally_valid(request):
    import pytest
    from jsonschema import Draft202012Validator, ValidationError
    definition = getattr(request.node, "_schema_definition", {})
    try:
        Draft202012Validator.check_schema(definition)
    except ValidationError as exc:
        pytest.fail(f"Schema is not valid: {exc}")


@then("the response contains field_additions and field_removals")
def step_response_has_field_additions_removals(request):
    data = request.node._resp.json()
    assert "field_additions" in data, f"Response missing field_additions: {data}"
    assert "field_removals" in data, f"Response missing field_removals: {data}"


def contextlib_patch_multi(patches):
    from contextlib import ExitStack
    stack = ExitStack()
    for p in patches:
        stack.enter_context(p)
    return stack
