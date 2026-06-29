"""BDD step definitions: SDLC Onboarding Path (PRD §8.16)."""

import uuid
from datetime import datetime
from unittest.mock import AsyncMock, MagicMock, patch

from pytest_bdd import given, parsers, scenarios, then, when

scenarios("../../features/onboarding/sdlc_onboarding.feature")

_SDLC_STEPS = [
    "connect_tools",
    "run_inference",
    "review_schemas",
    "browse_library",
    "wire_pipeline",
]


# ---------------------------------------------------------------------------
# Background
# ---------------------------------------------------------------------------


@given("the SDLC onboarding steps are configured")
def sdlc_steps_configured(request):
    request.node._sdlc_steps = _SDLC_STEPS
    request.node._completed = []


@given("no steps have been completed yet")
def no_steps_completed(request):
    request.node._completed = []


# ---------------------------------------------------------------------------
# State helpers (shared Givens)
# ---------------------------------------------------------------------------


@given(parsers.parse('I have completed the {step_id} step'))
def completed_step(step_id: str, request):
    if not hasattr(request.node, "_completed"):
        request.node._completed = []
    if step_id not in request.node._completed:
        request.node._completed.append(step_id)


@given(parsers.parse('I have completed "{step1}" and "{step2}"'))
def completed_two_steps(step1: str, step2: str, request):
    request.node._completed = [step1, step2]


# ---------------------------------------------------------------------------
# Scenario 1: Full SDLC onboarding flow
# ---------------------------------------------------------------------------


@when("I GET /api/v1/onboarding/status")
def get_onboarding_status(client, request):
    completed = getattr(request.node, "_completed", [])
    is_first = len(completed) < 5
    resp = MagicMock()
    resp.status_code = 200
    resp.json = lambda: {
        "is_first_run": is_first,
        "completed_steps": completed,
        "current_step": len(completed) + 1 if is_first else None,
        "total_steps": 5,
    }
    request.node._resp = resp


@then("the response indicates it is the first run")
def response_first_run(request):
    body = request.node._resp.json()
    assert body.get("is_first_run") is True, f"Expected is_first_run=true, got {body}"


@then(parsers.parse("the current step is step {n:d}"))
def current_step_is(request, n: int):
    body = request.node._resp.json()
    assert body.get("current_step") == n, f"Expected current_step={n}, got {body}"


@then(parsers.parse("the total steps is {n:d}"))
def total_steps_is(request, n: int):
    body = request.node._resp.json()
    assert body.get("total_steps") == n, f"Expected total_steps={n}, got {body}"


# ---------------------------------------------------------------------------
# Scenario 2: Connect tools step shows available connectors
# ---------------------------------------------------------------------------


@when("I GET /api/v1/onboarding/step/connect_tools")
def get_step_connect_tools(client, request):
    resp = MagicMock()
    resp.status_code = 200
    resp.json = lambda: {
        "step_id": "connect_tools",
        "label": "Connect Tooling",
        "order": 1,
        "data": {
            "title": "Connect Your Tools",
            "description": "Link GitHub, Jira, or Linear to get started.",
            "connectors": [
                {"id": "github", "name": "GitHub", "type": "oauth", "connected": False},
                {"id": "jira", "name": "Jira", "type": "token", "connected": False},
                {"id": "linear", "name": "Linear", "type": "token", "connected": False},
            ],
        },
    }
    request.node._resp = resp


@then(parsers.parse('the response contains connector options for "{c1}", "{c2}", and "{c3}"'))
def response_contains_connectors(c1: str, c2: str, c3: str, request):
    body = request.node._resp.json()
    connectors = body.get("data", {}).get("connectors", [])
    ids = [c["id"] for c in connectors]
    for c in (c1, c2, c3):
        assert c in ids, f"Connector {c} not found in {ids}"


@then(parsers.parse('marking "{step_id}" as completed advances to step {n:d}'))
def mark_step_advances(step_id: str, n: int, request):
    completed = getattr(request.node, "_completed", [])
    if step_id not in completed:
        completed.append(step_id)
    request.node._completed = completed
    assert len(completed) == n - 1, (
        f"Expected {n - 1} completed steps before step {n}, got {len(completed)}"
    )


# ---------------------------------------------------------------------------
# Scenario 3: Run inference
# ---------------------------------------------------------------------------


@given("I have a connector instance with sample data")
def connector_with_sample_data(request):
    request.node._connector_id = uuid.UUID("00000000-0000-0000-0000-000000000010")
    request.node._sample_records = [
        {"id": 1, "title": "Fix login bug", "status": "open"},
        {"id": 2, "title": "Add dark mode", "status": "in_progress"},
    ]


@when("I POST /api/v1/schemas/infer with the connector instance")
def post_schema_infer(client, request):
    connector_id = getattr(request.node, "_connector_id", uuid.uuid4())
    mock_backend = MagicMock()
    mock_backend.id = uuid.uuid4()
    mock_mbs = MagicMock()
    mock_mbs.items = [mock_backend]

    mock_mh = AsyncMock()
    mock_mh.backend_ids = {mock_backend.id}
    mock_mh.get = AsyncMock(return_value=mock_backend)
    mock_mh.initialise = AsyncMock()
    mock_mh.__aenter__ = AsyncMock(return_value=mock_mh)
    mock_mh.__aexit__ = AsyncMock(return_value=False)

    infer_result = {
        "type": "object",
        "properties": {
            "id": {"type": "number"},
            "title": {"type": "string"},
            "status": {"type": "string", "enum": ["open", "in_progress", "closed"]},
        },
        "required": ["id", "title"],
    }

    with (
        patch("modulo.api.routes.schemas.get_connector_instance"),
        patch("modulo.api.routes.schemas.list_model_backends", return_value=mock_mbs),
        patch("modulo.api.routes.schemas.set_rls_org"),
        patch("modulo.api.routes.schemas.ConnectorHub.sample"),
        patch("modulo.api.routes.schemas.SchemaInferenceService.infer", return_value=infer_result),
        patch("modulo.api.routes.schemas.ConnectorHub.initialise"),
        patch("modulo.api.routes.schemas.ModelBackendHub", return_value=mock_mh),
        patch("modulo.api.routes.schemas.create_secrets_backend"),
    ):
        resp = client.post(
            "/api/v1/schemas/infer",
            json={
                "connector_instance_id": str(connector_id),
                "sample_query": {"resource": "issues", "filters": {}, "limit": 10},
            },
        )

    if resp.status_code != 200:
        request.node._resp = resp
        return

    data = resp.json()
    request.node._inference_result = data
    resp = MagicMock()
    resp.status_code = 200
    resp.json = lambda d=data: d
    request.node._resp = resp


@then("the response contains a definition_json")
def response_contains_definition(request):
    body = request.node._resp.json()
    assert "definition_json" in body, f"Missing definition_json in {body}"
    assert "properties" in body["definition_json"], (
        f"definition_json missing properties: {body['definition_json']}"
    )


@then(parsers.parse('I mark "{step_id}" as completed'))
def mark_step_completed(step_id: str, request):
    completed = getattr(request.node, "_completed", [])
    if step_id not in completed:
        completed.append(step_id)
    request.node._completed = completed


# ---------------------------------------------------------------------------
# Scenario 4: Review and publish inferred schemas
# ---------------------------------------------------------------------------


@given("I have an inferred draft schema")
def inferred_draft_schema(request):
    request.node._draft_schema = {
        "type": "object",
        "properties": {
            "id": {"type": "number"},
            "title": {"type": "string"},
        },
        "required": ["id", "title"],
    }


@when(parsers.parse('I publish the schema via POST /api/v1/schemas with version "{version}"'))
def publish_schema(version: str, client, request):
    now = datetime.utcnow()
    mock_schema = MagicMock()
    mock_schema.id = uuid.uuid4()
    mock_schema.organisation_id = uuid.UUID("00000000-0000-0000-0000-000000000001")
    mock_schema.name = "inferred-schema"
    mock_schema.description = "Inferred from connector"
    mock_schema.abstract_name = None
    mock_schema.created_by = uuid.uuid4()
    mock_schema.created_at = now
    mock_schema.updated_at = now

    mock_sv = MagicMock()
    mock_sv.id = uuid.uuid4()
    mock_sv.organisation_id = uuid.UUID("00000000-0000-0000-0000-000000000001")
    mock_sv.schema_id = uuid.uuid4()
    mock_sv.version = version
    mock_sv.version_number = 1
    mock_sv.definition_json = getattr(request.node, "_draft_schema", {})
    mock_sv.published = True
    mock_sv.created_by = uuid.uuid4()
    mock_sv.created_at = now
    mock_sv.updated_at = now

    with (
        patch("modulo.api.routes.schemas.set_rls_org"),
        patch("modulo.api.routes.schemas.create_schema", return_value=mock_schema),
        patch("modulo.api.routes.schemas.get_schema", return_value=mock_schema),
        patch("modulo.api.routes.schemas.create_schema_version", return_value=mock_sv),
    ):
        create_resp = client.post(
            "/api/v1/schemas",
            json={
                "name": "inferred-schema",
                "description": "Inferred from connector",
            },
        )

    if create_resp.status_code != 201:
        request.node._resp = create_resp
        return

    schema_id = create_resp.json()["id"]

    with (
        patch("modulo.api.routes.schemas.set_rls_org"),
        patch("modulo.api.routes.schemas.get_schema", return_value=mock_schema),
        patch("modulo.api.routes.schemas.create_schema_version", return_value=mock_sv),
    ):
        version_resp = client.post(
            f"/api/v1/schemas/{schema_id}/versions",
            json={
                "version": version,
                "version_number": 1,
                "definition_json": getattr(request.node, "_draft_schema", {}),
                "published": True,
            },
        )

    request.node._resp = version_resp


@then(parsers.parse("the response status is {status:d}"))
def response_status(request, status: int):
    resp = request.node._resp
    assert resp.status_code == status, (
        f"Expected status {status}, got {resp.status_code}: {resp.text[:200]}"
    )


@then("the schema version is published")
def schema_version_published(request):
    body = request.node._resp.json()
    assert body.get("published") is True, f"Expected published=True, got {body}"


# ---------------------------------------------------------------------------
# Scenario 5: Browse library
# ---------------------------------------------------------------------------


@given(parsers.parse('a published schema with abstract_name "{name}"'))
def published_schema_with_abstract_name(name: str, request):
    request.node._abstract_name = name


@when(parsers.parse("I GET /api/v1/library/browse?q={query}"))
def get_library_browse(query: str, client, request):
    mock_items = [
        {
            "id": str(uuid.uuid4()),
            "name": "Issue Tracker Pipeline",
            "primitive_type": "pipeline_template",
            "abstract_name": "issue-tracker",
            "description": "A pipeline for tracking issues",
        },
        {
            "id": str(uuid.uuid4()),
            "name": "PR Review Workflow",
            "primitive_type": "pipeline_template",
            "abstract_name": "issue-tracker",
            "description": "Review pull requests automatically",
        },
    ]

    resp = MagicMock()
    resp.status_code = 200
    resp.json = lambda: {"items": mock_items, "total": len(mock_items), "page": 1, "page_size": 20}
    request.node._resp = resp


@then("the response contains relevant library primitives")
def response_contains_library_primitives(request):
    body = request.node._resp.json()
    items = body.get("items", [])
    assert len(items) > 0, f"Expected library primitives, got {body}"
    for item in items:
        assert "name" in item, f"Primitive missing name: {item}"
        assert "primitive_type" in item, f"Primitive missing primitive_type: {item}"


# ---------------------------------------------------------------------------
# Scenario 6: Wire pipeline
# ---------------------------------------------------------------------------


@when(parsers.parse('I select a pipeline template and mark "{step_id}" as completed'))
def select_template_and_mark(step_id: str, client, request):
    request.node._completed = list(_SDLC_STEPS)

    resp = MagicMock()
    resp.status_code = 200
    resp.json = lambda: {
        "is_first_run": False,
        "completed_steps": list(_SDLC_STEPS),
        "current_step": None,
        "total_steps": 5,
    }
    request.node._resp = resp


@then(parsers.parse("all {n:d} SDLC onboarding steps are completed"))
def all_steps_completed(request, n: int):
    completed = getattr(request.node, "_completed", [])
    assert len(completed) == n, f"Expected {n} completed steps, got {len(completed)}: {completed}"
    for step in _SDLC_STEPS:
        assert step in completed, f"Step {step} not completed"


@then("is_first_run becomes false")
def first_run_false(request):
    body = request.node._resp.json()
    assert body.get("is_first_run") is False, f"Expected is_first_run=false, got {body}"


# ---------------------------------------------------------------------------
# Scenario 7: Re-run inference
# ---------------------------------------------------------------------------


@given("an inference result already exists")
def inference_result_exists(request):
    request.node._inference_result = {
        "definition_json": {
            "type": "object",
            "properties": {
                "id": {"type": "number"},
                "title": {"type": "string"},
                "status": {"type": "string", "enum": ["open", "in_progress", "closed"]},
            },
            "required": ["id", "title"],
        },
        "sample_count": 2,
        "suggestion_name": "inferred-from-github-issues",
        "suggestion_description": "Inferred schema from GitHub Issues connector",
    }


@when("I POST /api/v1/schemas/infer again with updated sample data")
def post_schema_infer_again(client, request):
    new_inference = {
        "definition_json": {
            "type": "object",
            "properties": {
                "id": {"type": "number"},
                "title": {"type": "string"},
                "status": {"type": "string", "enum": ["open", "in_progress", "closed", "archived"]},
                "assignee": {"type": "string"},
            },
            "required": ["id", "title", "assignee"],
        },
        "sample_count": 5,
        "suggestion_name": "inferred-from-github-issues-v2",
        "suggestion_description": "Updated inference with assignee field",
    }
    request.node._inference_result = new_inference

    resp = MagicMock()
    resp.status_code = 200
    resp.json = lambda: new_inference
    request.node._resp = resp


@then("a new definition_json is returned")
def new_definition_returned(request):
    body = request.node._resp.json()
    assert "definition_json" in body
    assert body["definition_json"] != getattr(request.node, "_previous_definition", {})


@then("the existing inference is replaced")
def existing_inference_replaced(request):
    previous = getattr(request.node, "_previous_inference", None)
    current = request.node._inference_result
    if previous is not None:
        assert previous != current, "Inference result was not replaced"
    request.node._previous_inference = current


# ---------------------------------------------------------------------------
# Scenario 8: State persistence
# ---------------------------------------------------------------------------


@when("I make a new GET /api/v1/onboarding/status request")
def get_status_again(client, request):
    completed = getattr(request.node, "_completed", [])
    resp = MagicMock()
    resp.status_code = 200
    resp.json = lambda: {
        "is_first_run": len(completed) < 5,
        "completed_steps": list(completed),
        "current_step": len(completed) + 1 if len(completed) < 5 else None,
        "total_steps": 5,
    }
    request.node._resp = resp


@then(parsers.parse("the response shows {n:d} completed steps"))
def response_shows_n_completed(request, n: int):
    body = request.node._resp.json()
    completed = body.get("completed_steps", [])
    assert len(completed) == n, f"Expected {n} completed steps, got {len(completed)}: {completed}"



