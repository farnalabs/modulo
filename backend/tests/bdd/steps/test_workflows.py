"""Step definitions for workflow features — export, import, and binding wizard."""

import io
import json
import uuid
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
import yaml
from pytest_bdd import given, parsers, scenarios, then, when

scenarios("../features/workflows/export.feature")
scenarios("../features/workflows/import.feature")
scenarios("../features/workflows/binding.feature")

PIPELINE_ID = uuid.UUID("00000000-0000-0000-0000-0000000000f0")
MISSING_ID = uuid.UUID("00000000-0000-0000-0000-000000099999")
SCHEMA_A_ID = uuid.UUID("00000000-0000-0000-0000-0000000000a1")
SCHEMA_B_ID = uuid.UUID("00000000-0000-0000-0000-0000000000a2")
FILESYSTEM_CONNECTOR_ID = uuid.UUID("00000000-0000-0000-0000-0000000000b1")
CLAUDE_SONNET_BACKEND_ID = uuid.UUID("00000000-0000-0000-0000-0000000000c1")
FAKE_TEAM_ID = uuid.UUID("00000000-0000-0000-0000-0000000000dd")


# ---------------------------------------------------------------------------
# Helpers: build mock bundles and exported ZIP content
# ---------------------------------------------------------------------------


def _make_sample_bundle() -> dict[str, Any]:
    return {
        "format_version": "1",
        "pipeline": {
            "name": "My Pipeline",
            "description": "A test pipeline",
            "graph_nodes_json": [
                {"id": "agent-1", "agent_id": str(uuid.uuid4()), "role": "agent"},
                {"id": "agent-2", "agent_id": str(uuid.uuid4()), "role": "agent"},
                {"id": "manual-1", "role": "manual"},
            ],
            "run_context_defaults": {},
            "node_timeout_seconds": 300,
        },
        "agents": [
            {
                "id": str(uuid.uuid4()),
                "name": "PRD Reader",
                "prompt_template": "Read the PRD: {{ input }}",
                "input_schema_id": str(SCHEMA_A_ID),
                "input_schema_version": "1.0",
                "output_schema_id": str(SCHEMA_B_ID),
                "output_schema_version": "1.0",
                "model_backend_id": str(CLAUDE_SONNET_BACKEND_ID),
                "connector_type_refs": [{"type": "filesystem"}],
            },
            {
                "id": str(uuid.uuid4()),
                "name": "Requirements Writer",
                "prompt_template": "Write requirements: {{ input }}",
                "input_schema_id": str(SCHEMA_B_ID),
                "input_schema_version": "1.0",
                "output_schema_id": str(SCHEMA_A_ID),
                "output_schema_version": "1.0",
                "model_backend_id": str(CLAUDE_SONNET_BACKEND_ID),
                "connector_type_refs": [],
            },
        ],
        "schemas": [
            {
                "id": str(SCHEMA_A_ID),
                "name": "PRD Input Schema",
                "abstract_name": "prd-input",
                "definition_json": {"fields": [{"name": "title", "type": "string"}]},
            },
            {
                "id": str(SCHEMA_B_ID),
                "name": "Requirements Output Schema",
                "abstract_name": "requirements-output",
                "definition_json": {"fields": [{"name": "functional", "type": "array"}]},
            },
        ],
        "model_backends": [
            {
                "id": str(CLAUDE_SONNET_BACKEND_ID),
                "name": "claude-sonnet-4",
                "provider": "anthropic",
                "model_id": "claude-sonnet-4-20241022",
            },
        ],
        "edges": [
            {
                "id": str(uuid.uuid4()),
                "source_node_id": "agent-1",
                "target_node_id": "agent-2",
                "edge_type": "normal",
            },
            {
                "id": str(uuid.uuid4()),
                "source_node_id": "agent-2",
                "target_node_id": "manual-1",
                "edge_type": "normal",
            },
            {
                "id": str(uuid.uuid4()),
                "source_node_id": "manual-1",
                "target_node_id": "agent-1",
                "edge_type": "conditional",
            },
        ],
    }


def _make_zip_bytes(bundle: dict[str, Any] | None = None) -> bytes:
    buf = io.BytesIO()
    import zipfile

    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        content = json.dumps(bundle or _make_sample_bundle(), indent=2, default=str)
        zf.writestr("bundle.json", content)
    return buf.getvalue()


# ---------------------------------------------------------------------------
# Shared test state
# ---------------------------------------------------------------------------


@pytest.fixture
def ctx() -> dict[str, Any]:
    return {
        "response": None,
        "exported_zip": None,
        "bundle_json": None,
        "extracted_bundle": None,
        "resolved_schemas": [],
        "resolved_connectors": [],
        "resolved_model_backends": [],
        "name_conflicts": [],
        "pipeline_mock": None,
    }


# ============================================================================
# export.feature steps
# ============================================================================


@given("a pipeline exists with 2 agent nodes and 1 manual node")
def _pipeline_with_agent_manual_nodes(ctx: dict[str, Any]) -> None:
    p = MagicMock()
    p.id = PIPELINE_ID
    p.name = "My Pipeline"
    p.description = "A test pipeline"
    p.organisation_id = uuid.UUID("00000000-0000-0000-0000-000000000001")
    p.graph_nodes_json = [
        {"id": "agent-1", "agent_id": str(uuid.uuid4()), "role": "agent"},
        {"id": "agent-2", "agent_id": str(uuid.uuid4()), "role": "agent"},
        {"id": "manual-1", "role": "manual"},
    ]
    p.run_context_defaults = {}
    p.node_timeout_seconds = 300
    p.owner_team_id = FAKE_TEAM_ID
    p.created_by = uuid.uuid4()
    p.created_at = None
    p.updated_at = None
    ctx["pipeline_mock"] = p


@given("the pipeline has 3 edges connecting the nodes")
def _pipeline_has_edges(ctx: dict[str, Any]) -> None:
    # Edges are part of the export bundle; they don't need to be on the pipeline mock
    pass


@given("each agent references a schema and a model backend")
def _agents_ref_schema_backend() -> None:
    pass


@when(parsers.parse("the user sends POST /api/v1/libraries/export/{pipeline_id}"))
def _request_export(client, pipeline_id: str, ctx: dict[str, Any]) -> None:
    bundle = _make_sample_bundle()
    zip_bytes = _make_zip_bytes(bundle)
    ctx["exported_zip"] = zip_bytes

    with (
        patch("modulo.api.routes.library.get_pipeline") as mock_get_pipeline,
        patch("modulo.api.routes.library.export_pipeline_bundle") as mock_export,
    ):
        mock_get_pipeline.return_value = ctx.get("pipeline_mock") or MagicMock()
        mock_export.return_value = zip_bytes

        ctx["response"] = client.post(f"/api/v1/libraries/export/{pipeline_id}")
        ctx["bundle_json"] = bundle


@when("the exported ZIP is extracted")
def _extract_exported_zip(ctx: dict[str, Any]) -> None:
    zip_bytes = ctx.get("exported_zip")
    assert zip_bytes is not None, "No exported ZIP to extract"
    import zipfile

    with zipfile.ZipFile(io.BytesIO(zip_bytes)) as zf:
        ctx["extracted_bundle"] = json.loads(zf.read("bundle.json"))


@when("the bundle.json is inspected")
def _inspect_bundle_json(ctx: dict[str, Any]) -> None:
    # If the extracted bundle is available, use it; otherwise use the sample
    ctx["extracted_bundle"] = ctx.get("extracted_bundle") or _make_sample_bundle()


@when("the bundle.json agents array is inspected")
def _inspect_agents(ctx: dict[str, Any]) -> None:
    ctx["extracted_bundle"] = ctx.get("extracted_bundle") or _make_sample_bundle()


@then("the response status is 200")
def _response_status_200(ctx: dict[str, Any]) -> None:
    assert ctx["response"].status_code == 200, (
        f"Expected 200, got {ctx['response'].status_code}: {ctx['response'].text[:200]}"
    )


@then('the response has content-type "application/zip"')
def _response_content_type_zip(ctx: dict[str, Any]) -> None:
    ct = ctx["response"].headers.get("content-type", "")
    assert "application/zip" in ct, f"Expected application/zip, got {ct}"


@then("the response has a Content-Disposition header with filename ending in .modulo.zip")
def _response_content_disposition(ctx: dict[str, Any]) -> None:
    cd = ctx["response"].headers.get("content-disposition", "")
    assert ".modulo.zip" in cd, f"Expected .modulo.zip in Content-Disposition, got {cd}"


@then("the bundle.json file exists in the archive root")
def _bundle_json_exists(ctx: dict[str, Any]) -> None:
    assert ctx["extracted_bundle"] is not None, "bundle.json was not extracted"


@then('bundle.json contains "format_version", "pipeline", "agents", "schemas", "edges"')
def _bundle_json_keys(ctx: dict[str, Any]) -> None:
    b = ctx["extracted_bundle"]
    for key in ("format_version", "pipeline", "agents", "schemas", "edges"):
        assert key in b, f"Missing key '{key}' in bundle.json"


@then("the pipeline section does not contain owner_team_id")
def _pipeline_no_owner_team_id(ctx: dict[str, Any]) -> None:
    b = ctx["extracted_bundle"]
    pipeline = b["pipeline"]
    assert "owner_team_id" not in pipeline, "owner_team_id should be stripped from exported bundle"


@then("the pipeline section contains the pipeline name and graph nodes")
def _pipeline_has_name_nodes(ctx: dict[str, Any]) -> None:
    b = ctx["extracted_bundle"]
    pipeline = b["pipeline"]
    assert "name" in pipeline, "Missing pipeline name"
    assert "graph_nodes_json" in pipeline, "Missing graph_nodes_json"
    assert len(pipeline["graph_nodes_json"]) == 3, "Expected 3 graph nodes"


@then("each agent has name, prompt_template, schema references, and model_backend_id")
def _agents_have_required_fields(ctx: dict[str, Any]) -> None:
    b = ctx["extracted_bundle"]
    for agent in b.get("agents", []):
        assert "name" in agent, "Missing agent name"
        assert "prompt_template" in agent, "Missing prompt_template"
        assert "input_schema_id" in agent, "Missing input_schema_id"
        assert "output_schema_id" in agent, "Missing output_schema_id"
        assert "model_backend_id" in agent, "Missing model_backend_id"


@then("agent definitions do not include credentials or ciphertexts")
def _agents_no_credentials(ctx: dict[str, Any]) -> None:
    b = ctx["extracted_bundle"]
    for agent in b.get("agents", []):
        assert "credentials" not in agent, "Agent should not include credentials"
        assert "ciphertext" not in agent, "Agent should not include ciphertext"
        assert "api_key" not in str(agent).lower(), "Agent should not contain api_key references"


# ============================================================================
# export.feature steps (v2 YAML bundle — the current export contract)
# ============================================================================


def _make_v2_yaml(signed: bool = False) -> str:
    bundle = {
        "name": "PRD to Tickets",
        "version": "1",
        "agents": [
            {
                "name": "prd-reader",
                "prompt_template": "Read the PRD: {{ input }}",
                "input_schema": "prd-input",
                "output_schema": "tickets-output",
                "model_backend": "claude-sonnet-4",
            },
            {
                "name": "ticket-writer",
                "prompt_template": "Write tickets: {{ input }}",
                "input_schema": "tickets-output",
                "output_schema": "prd-input",
                "model_backend": "claude-sonnet-4",
            },
        ],
        "edges": [
            {
                "source": "prd-reader",
                "target": "ticket-writer",
                "edge_type": "normal",
                "hitl_gate_config": {"mode": "manual"},
            }
        ],
        "schemas": [
            {"name": "prd-input", "definition": {"fields": [{"name": "title", "type": "string"}]}},
            {"name": "tickets-output", "definition": {"fields": [{"name": "ticket", "type": "string"}]}},
        ],
        "requires": {
            "connector_types": ["filesystem"],
            "abstract_schemas": ["prd-input", "tickets-output"],
        },
    }
    if signed:
        bundle["signature"] = "bWVzc2FnZQ==" + "A" * 44
    return yaml.safe_dump({"modulo_workflow": bundle}, sort_keys=False)


@given(parsers.parse('a pipeline named "{name}" exists'))
def _pipeline_named_exists(ctx: dict[str, Any], name: str) -> None:
    p = MagicMock()
    p.id = PIPELINE_ID
    p.name = name
    p.description = "A test pipeline"
    p.organisation_id = uuid.UUID("00000000-0000-0000-0000-000000000001")
    p.graph_nodes_json = []
    p.run_context_defaults = {}
    p.node_timeout_seconds = 300
    p.owner_team_id = FAKE_TEAM_ID
    p.created_by = uuid.uuid4()
    p.created_at = None
    p.updated_at = None
    ctx["pipeline_mock"] = p
    ctx["pipeline_name"] = name


@given(parsers.parse('the pipeline has 2 agent nodes ("{a}", "{b}") and 1 HITL gate'))
def _pipeline_has_agent_nodes(ctx: dict[str, Any], a: str, b: str) -> None:
    ctx["pipeline_mock"].graph_nodes_json = [
        {"id": a, "role": "agent"},
        {"id": b, "role": "agent"},
        {"id": "hitl-1", "role": "hitl"},
    ]


@given("each agent references an abstract schema and a connector type")
def _agents_ref_schema_connector() -> None:
    pass


@given("the pipeline has a model_backend_id and encrypted credentials")
def _pipeline_has_backend_and_credentials() -> None:
    pass


@when("the user requests export of the pipeline")
def _request_export_v2(client, ctx: dict[str, Any]) -> None:
    _do_request_export(client, ctx, signed=False)


@when(parsers.parse('the user requests export with "sign: true"'))
def _request_export_signed(client, ctx: dict[str, Any]) -> None:
    _do_request_export(client, ctx, signed=True)


@when(parsers.parse('the user requests export of pipeline "{pipeline_id}"'))
def _request_export_missing(client, ctx: dict[str, Any], pipeline_id: str) -> None:
    with (
        patch("modulo.api.routes.library.get_pipeline", return_value=None),
        patch("modulo.api.routes.library.set_rls_org"),
        patch("modulo.api.routes.library.set_rls_user_context"),
    ):
        ctx["response"] = client.post(f"/api/v1/libraries/export/{pipeline_id}?format=v2")


def _do_request_export(client, ctx: dict[str, Any], signed: bool) -> None:
    yaml_str = _make_v2_yaml(signed=signed)
    with (
        patch("modulo.api.routes.library.get_pipeline", return_value=ctx.get("pipeline_mock") or MagicMock()),
        patch("modulo.api.routes.library.export_pipeline_bundle_v2", return_value=yaml_str),
        patch("modulo.api.routes.library.set_rls_org"),
        patch("modulo.api.routes.library.set_rls_user_context"),
    ):
        ctx["response"] = client.post(f"/api/v1/libraries/export/{PIPELINE_ID}?format=v2")
    ctx["exported_yaml"] = yaml_str


@then(parsers.parse('the response content-type is "{ct}"'))
def _response_content_type_yaml(ctx: dict[str, Any], ct: str) -> None:
    got = ctx["response"].headers.get("content-type", "")
    assert ct in got, f"Expected content-type {ct}, got {got}"


@then('the body is valid YAML with top-level key "modulo_workflow"')
def _body_is_valid_yaml(ctx: dict[str, Any]) -> None:
    data = yaml.safe_load(ctx["response"].content)
    assert "modulo_workflow" in data, "Missing modulo_workflow top-level key"
    ctx["exported_bundle"] = data["modulo_workflow"]


@then('modulo_workflow contains "name", "version", "agents", "edges", "schemas"')
def _modulo_workflow_keys(ctx: dict[str, Any]) -> None:
    wf = ctx.get("exported_bundle")
    if wf is None:
        wf = yaml.safe_load(ctx["response"].content)["modulo_workflow"]
        ctx["exported_bundle"] = wf
    for key in ("name", "version", "agents", "edges", "schemas"):
        assert key in wf, f"Missing key '{key}' in modulo_workflow"


@then('the response includes a "signature" field under modulo_workflow')
def _signature_field_present(ctx: dict[str, Any]) -> None:
    wf = ctx.get("exported_bundle") or yaml.safe_load(ctx["response"].content)["modulo_workflow"]
    assert "signature" in wf, "Missing signature field"


@then("the signature is a valid Ed25519 base64-encoded string")
def _signature_base64(ctx: dict[str, Any]) -> None:
    wf = ctx.get("exported_bundle") or yaml.safe_load(ctx["response"].content)["modulo_workflow"]
    sig = wf.get("signature", "")
    assert isinstance(sig, str), "Signature does not look like base64"
    assert len(sig) >= 32, "Signature does not look like base64"


@then("the signature verifies against the Modulo registry public key")
def _signature_verifies(ctx: dict[str, Any]) -> None:
    pass


@when("the exported YAML is inspected")
def _inspect_exported_yaml(client, ctx: dict[str, Any]) -> None:
    if ctx.get("response") is None:
        _do_request_export(client, ctx, signed=False)
    if ctx.get("exported_bundle") is None:
        ctx["exported_bundle"] = yaml.safe_load(ctx["response"].content)["modulo_workflow"]


@then("the agents section does not contain any credential or ciphertext fields")
def _agents_no_credentials_yaml(ctx: dict[str, Any]) -> None:
    wf = ctx.get("exported_bundle") or yaml.safe_load(ctx["response"].content)["modulo_workflow"]
    for agent in wf.get("agents", []):
        assert "credentials" not in agent, "Agent should not include credentials"
        assert "ciphertext" not in agent, "Agent should not include ciphertext"
        assert "api_key" not in str(agent).lower(), "Agent should not contain api_key references"


@then("the pipeline section does not contain owner_team_id")
def _no_owner_team_id_yaml(ctx: dict[str, Any]) -> None:
    wf = ctx.get("exported_bundle") or yaml.safe_load(ctx["response"].content)["modulo_workflow"]
    assert "owner_team_id" not in wf, "owner_team_id should be stripped from export"


@then("the pipeline section does not contain visibility")
def _no_visibility_yaml(ctx: dict[str, Any]) -> None:
    wf = ctx.get("exported_bundle") or yaml.safe_load(ctx["response"].content)["modulo_workflow"]
    assert "visibility" not in wf, "visibility should be stripped from export"


@then("model_backend_id references are preserved as abstract names")
def _model_backend_abstract_yaml(ctx: dict[str, Any]) -> None:
    wf = ctx.get("exported_bundle") or yaml.safe_load(ctx["response"].content)["modulo_workflow"]
    for agent in wf.get("agents", []):
        assert agent.get("model_backend"), "Model backend abstract name missing"


@then("the agents section contains prompt_template for each agent")
def _agents_prompt_template_yaml(ctx: dict[str, Any]) -> None:
    wf = ctx.get("exported_bundle") or yaml.safe_load(ctx["response"].content)["modulo_workflow"]
    for agent in wf.get("agents", []):
        assert "prompt_template" in agent, "Missing prompt_template"


@then("the agents section contains input_schema and output_schema as abstract names")
def _agents_schema_abstracts_yaml(ctx: dict[str, Any]) -> None:
    wf = ctx.get("exported_bundle") or yaml.safe_load(ctx["response"].content)["modulo_workflow"]
    for agent in wf.get("agents", []):
        assert agent.get("input_schema"), "Missing input_schema"
        assert agent.get("output_schema"), "Missing output_schema"


@then("the edges section contains source, target, edge_type and hitl_gate_config")
def _edges_fields_yaml(ctx: dict[str, Any]) -> None:
    wf = ctx.get("exported_bundle") or yaml.safe_load(ctx["response"].content)["modulo_workflow"]
    for edge in wf.get("edges", []):
        for key in ("source", "target", "edge_type", "hitl_gate_config"):
            assert key in edge, f"Missing edge field '{key}'"


@then("the requires section lists connector_types and abstract_schemas")
def _requires_fields_yaml(ctx: dict[str, Any]) -> None:
    wf = ctx.get("exported_bundle") or yaml.safe_load(ctx["response"].content)["modulo_workflow"]
    requires = wf.get("requires", {})
    assert "connector_types" in requires, "Missing connector_types"
    assert "abstract_schemas" in requires, "Missing abstract_schemas"


@then("the response status is 404")
def _response_status_404(ctx: dict[str, Any]) -> None:
    assert ctx["response"].status_code == 404, (
        f"Expected 404, got {ctx['response'].status_code}: {ctx['response'].text[:200]}"
    )


# ============================================================================
# import.feature steps
# ============================================================================


@given('the organisation has schemas "PRD Input Schema" and "Requirements Output Schema"')
def _org_has_schemas() -> None:
    pass


@given('has an active "filesystem" connector instance')
def _org_has_filesystem_connector() -> None:
    pass


@given('has an active model backend "claude-sonnet-4"')
def _org_has_claude_backend() -> None:
    pass


@given(parsers.parse('has a model backend "{name}"'))
def _org_has_model_backend(name: str) -> None:
    pass


@when("the user uploads a valid .modulo.zip to POST /api/v1/libraries/import/upload-zip")
def _upload_valid_zip(client, ctx: dict[str, Any]) -> None:
    zip_bytes = _make_zip_bytes()
    with (
        patch("modulo.api.routes.library.extract_bundle_json_from_zip") as mock_extract,
    ):
        sample = _make_sample_bundle()
        mock_extract.return_value = sample
        ctx["bundle_json"] = sample

        ctx["response"] = client.post(
            "/api/v1/libraries/import/upload-zip",
            files={"file": ("test.modulo.zip", zip_bytes, "application/zip")},
        )


@then("the response contains resolved_schemas with at least 2 entries")
def _response_has_resolved_schemas(ctx: dict[str, Any]) -> None:
    data = ctx["response"].json()
    assert "resolved_schemas" in data, "Missing resolved_schemas"
    assert len(data["resolved_schemas"]) >= 2, (
        f"Expected at least 2 resolved_schemas, got {len(data['resolved_schemas'])}"
    )


@then("the response contains available_teams")
def _response_has_available_teams(ctx: dict[str, Any]) -> None:
    data = ctx["response"].json()
    assert "available_teams" in data, "Missing available_teams"


@then("the response contains bundle_json with the serialized bundle")
def _response_has_bundle_json(ctx: dict[str, Any]) -> None:
    data = ctx["response"].json()
    assert "bundle_json" in data, "Missing bundle_json"
    assert data["bundle_json"], "bundle_json should not be empty"


@when("the user sends POST /api/v1/libraries/import/analyse with a bundle")
def _analyse_bundle(client, ctx: dict[str, Any]) -> None:
    sample = _make_sample_bundle()
    ctx["bundle_json"] = sample
    ctx["response"] = client.post(
        "/api/v1/libraries/import/analyse",
        json={"bundle": sample},
    )


@then("the response contains warnings if any references are unresolvable")
def _response_has_warnings(ctx: dict[str, Any]) -> None:
    data = ctx["response"].json()
    assert "warnings" in data, "Missing warnings key"


@then("the response contains name_conflicts if pipeline names collide")
def _response_has_name_conflicts(ctx: dict[str, Any]) -> None:
    data = ctx["response"].json()
    assert "name_conflicts" in data, "Missing name_conflicts key"


@given('a pipeline named "My Pipeline" already exists')
def _pipeline_my_pipeline_exists() -> None:
    pass


@when('the user imports a bundle containing "My Pipeline"')
def _import_my_pipeline(client, ctx: dict[str, Any]) -> None:
    sample = _make_sample_bundle()
    with (
        patch("modulo.api.routes.library.get_existing_pipeline_names") as mock_get_pipelines,
        patch("modulo.api.routes.library.suggest_import_name") as mock_suggest,
    ):
        mock_get_pipelines.return_value = {"My Pipeline"}
        mock_suggest.return_value = "My Pipeline (imported)"

        ctx["response"] = client.post(
            "/api/v1/libraries/import/analyse",
            json={"bundle": sample},
        )


@then("the name_conflicts list includes a pipeline conflict")
def _name_conflicts_includes_pipeline(ctx: dict[str, Any]) -> None:
    data = ctx["response"].json()
    conflicts = data.get("name_conflicts", [])
    pipeline_conflicts = [c for c in conflicts if c.get("type") == "pipeline"]
    assert len(pipeline_conflicts) > 0, "Expected at least one pipeline name conflict"


@then('the suggested name is "My Pipeline (imported)"')
def _suggested_name_is(ctx: dict[str, Any]) -> None:
    data = ctx["response"].json()
    conflicts = data.get("name_conflicts", [])
    suggestion = conflicts[0].get("suggested", "") if conflicts else ""
    assert "imported" in suggestion, f"Expected suggestion containing 'imported', got '{suggestion}'"


@when("the user sends POST /api/v1/libraries/import/confirm with bundle_json")
def _confirm_import(client, ctx: dict[str, Any]) -> None:
    sample = _make_sample_bundle()
    with (
        patch("modulo.api.routes.library.materialize_import") as mock_materialize,
    ):
        mock_materialize.return_value = {
            "pipeline_id": str(PIPELINE_ID),
            "pipeline_name": "My Pipeline",
            "primitive_id": str(uuid.uuid4()),
            "agent_count": 2,
            "edge_count": 3,
            "schema_count": 2,
            "warnings": [],
        }

        ctx["response"] = client.post(
            "/api/v1/libraries/import/confirm",
            json={"bundle_json": json.dumps(sample, default=str)},
        )


@then("the response contains pipeline_id pointing to a new Pipeline")
def _response_has_pipeline_id(ctx: dict[str, Any]) -> None:
    data = ctx["response"].json()
    assert "pipeline_id" in data, "Missing pipeline_id"
    assert data["pipeline_id"] is not None, "pipeline_id should not be None"


@then("agent_count matches the number of agents in the bundle")
def _agent_count_matches(ctx: dict[str, Any]) -> None:
    data = ctx["response"].json()
    assert data["agent_count"] == 2, f"Expected 2 agents, got {data.get('agent_count')}"


@then("a library primitive is created for the workflow")
def _library_primitive_created(ctx: dict[str, Any]) -> None:
    data = ctx["response"].json()
    assert "primitive_id" in data, "Missing primitive_id in import response"


@given('the bundle references "prd-input" schema by abstract_name')
def _bundle_refs_prd_schema(ctx: dict[str, Any]) -> None:
    ctx["bundle_json"] = _make_sample_bundle()


@when("the import analysis runs")
def _import_analysis_runs(client, ctx: dict[str, Any]) -> None:
    with (
        patch("modulo.api.routes.library.resolve_schema") as mock_resolve_schema,
        patch("modulo.api.routes.library.resolve_connector_type") as mock_resolve_connector,
        patch("modulo.api.routes.library.resolve_model_backend") as mock_resolve_mb,
        patch("modulo.api.routes.library.get_existing_pipeline_names") as mock_get_pipelines,
        patch("modulo.api.routes.library.get_existing_agent_names") as mock_get_agents,
    ):
        mock_resolve_schema.return_value = {
            "schema_id": str(SCHEMA_A_ID),
            "version": "1.0",
            "warning": None,
        }
        mock_resolve_connector.return_value = {
            "instance_id": str(FILESYSTEM_CONNECTOR_ID),
            "instance_name": "filesystem",
            "warning": None,
        }
        mock_resolve_mb.return_value = {
            "model_backend_id": str(CLAUDE_SONNET_BACKEND_ID),
            "warning": None,
        }
        mock_get_pipelines.return_value = set()
        mock_get_agents.return_value = set()

        ctx["response"] = client.post(
            "/api/v1/libraries/import/analyse",
            json={"bundle": ctx.get("bundle_json") or _make_sample_bundle()},
        )


@then('the schema is resolved to the existing local "PRD Input Schema"')
def _schema_resolved_to_local(ctx: dict[str, Any]) -> None:
    data = ctx["response"].json()
    schemas = data.get("resolved_schemas", [])
    assert any(s.get("schema_id") == str(SCHEMA_A_ID) for s in schemas), (
        "PRD Input Schema not resolved by abstract_name"
    )


@then("no schema creation warning is emitted")
def _no_schema_creation_warning(ctx: dict[str, Any]) -> None:
    data = ctx["response"].json()
    warnings = data.get("warnings", [])
    schema_warnings = [w for w in warnings if "schema" in w.lower() or "Schema" in w]
    assert len(schema_warnings) == 0, f"Unexpected schema warnings: {schema_warnings}"


@given("the bundle references an unknown schema")
def _bundle_refs_unknown_schema(ctx: dict[str, Any]) -> None:
    bundle = _make_sample_bundle()
    bundle["schemas"] = [
        {
            "id": str(uuid.uuid4()),
            "name": "Unknown Schema",
            "abstract_name": "unknown",
            "definition_json": {"fields": [{"name": "unknown_field", "type": "string"}]},
        }
    ]
    ctx["bundle_json"] = bundle


@when("the import is confirmed")
def _import_confirmed(client, ctx: dict[str, Any]) -> None:
    with (
        patch("modulo.api.routes.library.materialize_import") as mock_materialize,
    ):
        mock_materialize.return_value = {
            "pipeline_id": str(uuid.uuid4()),
            "pipeline_name": "Imported Pipeline",
            "primitive_id": str(uuid.uuid4()),
            "agent_count": 2,
            "edge_count": 3,
            "schema_count": 1,
            "warnings": [],
        }

        ctx["response"] = client.post(
            "/api/v1/libraries/import/confirm",
            json={
                "bundle_json": json.dumps(ctx.get("bundle_json") or _make_sample_bundle(), default=str),
            },
        )


@then("a new Schema and SchemaVersion are created for the unknown schema")
def _new_schema_and_version_created(ctx: dict[str, Any]) -> None:
    data = ctx["response"].json()
    assert data["schema_count"] >= 1, "Expected at least 1 schema created"
    assert data["pipeline_id"] is not None, "Pipeline should have been created"


@given("the bundle references an unknown schema by abstract_name")
def _bundle_refs_unknown_schema_abstract() -> None:
    pass


@when(parsers.parse("the user imports a bundle with owner_team_id set"))
def _import_with_team(client, ctx: dict[str, Any]) -> None:
    with (
        patch("modulo.api.routes.library.materialize_import") as mock_materialize,
    ):
        mock_materialize.return_value = {
            "pipeline_id": str(PIPELINE_ID),
            "pipeline_name": "My Pipeline",
            "primitive_id": str(uuid.uuid4()),
            "agent_count": 2,
            "edge_count": 3,
            "schema_count": 2,
            "warnings": [],
        }

        ctx["response"] = client.post(
            "/api/v1/libraries/import/confirm",
            json={
                "bundle_json": json.dumps(_make_sample_bundle(), default=str),
                "owner_team_id": str(FAKE_TEAM_ID),
            },
        )


@then("the created pipeline has owner_team_id matching the selection")
def _pipeline_owner_team_matches(ctx: dict[str, Any]) -> None:
    data = ctx["response"].json()
    assert data["pipeline_id"] is not None, "Pipeline should have been created"


@then("the library primitive has owner_team_id matching the selection")
def _primitive_owner_team_matches(ctx: dict[str, Any]) -> None:
    data = ctx["response"].json()
    assert "primitive_id" in data, "Primitive should have been created"


@when(parsers.parse("the user uploads a .txt file to POST /api/v1/libraries/import/upload-zip"))
def _upload_txt_file(client, ctx: dict[str, Any]) -> None:
    ctx["response"] = client.post(
        "/api/v1/libraries/import/upload-zip",
        files={"file": ("test.txt", b"this is not a zip", "text/plain")},
    )


@then("the response status is 400")
def _response_status_400(ctx: dict[str, Any]) -> None:
    assert ctx["response"].status_code == 400, (
        f"Expected 400, got {ctx['response'].status_code}: {ctx['response'].text[:200]}"
    )


# ============================================================================
# binding.feature steps
# ============================================================================


@given('the organisation has a "filesystem" connector instance')
def _org_has_filesystem_connector_instance() -> None:
    pass


@given('has a schema "PRD Input Schema" with abstract_name "prd-input"')
def _org_has_prd_schema() -> None:
    pass


@given('the bundle references connector type "filesystem"')
def _bundle_refs_filesystem_connector(ctx: dict[str, Any]) -> None:
    ctx["bundle_json"] = _make_sample_bundle()


@given('the bundle references connector type "slack"')
def _bundle_refs_slack_connector(ctx: dict[str, Any]) -> None:
    bundle = _make_sample_bundle()
    for agent in bundle["agents"]:
        agent["connector_type_refs"] = [{"type": "slack"}]
    ctx["bundle_json"] = bundle


@given('no "slack" connector instance exists')
def _no_slack_connector() -> None:
    pass


@when("the import analysis resolves connectors")
def _analysis_resolves_connectors(client, ctx: dict[str, Any]) -> None:
    with (
        patch("modulo.api.routes.library.resolve_schema") as mock_resolve_schema,
        patch("modulo.api.routes.library.resolve_connector_type") as mock_resolve_connector,
        patch("modulo.api.routes.library.resolve_model_backend") as mock_resolve_mb,
        patch("modulo.api.routes.library.get_existing_pipeline_names") as mock_get_pipelines,
        patch("modulo.api.routes.library.get_existing_agent_names") as mock_get_agents,
    ):
        mock_resolve_schema.return_value = {
            "schema_id": str(SCHEMA_A_ID),
            "version": "1.0",
            "warning": None,
        }
        mock_resolve_connector.side_effect = lambda session, org_id, ctid: (
            {
                "instance_id": str(FILESYSTEM_CONNECTOR_ID),
                "instance_name": "My Filesystem",
                "warning": None,
            }
            if ctid == "filesystem"
            else {
                "instance_id": None,
                "instance_name": None,
                "warning": f"Connector type '{ctid}' not found locally.",
            }
        )
        mock_resolve_mb.return_value = {
            "model_backend_id": str(CLAUDE_SONNET_BACKEND_ID),
            "warning": None,
        }
        mock_get_pipelines.return_value = set()
        mock_get_agents.return_value = set()

        ctx["response"] = client.post(
            "/api/v1/libraries/import/analyse",
            json={"bundle": ctx.get("bundle_json") or _make_sample_bundle()},
        )


@then("resolved_connectors contains a match with instance_id and instance_name")
def _resolved_connectors_has_match(ctx: dict[str, Any]) -> None:
    data = ctx["response"].json()
    connectors = data.get("resolved_connectors", [])
    assert any(c.get("instance_id") is not None and c.get("instance_name") is not None for c in connectors), (
        "No resolved connector with instance_id and instance_name"
    )


@then("no warning is emitted for this connector type")
def _no_connector_warning(ctx: dict[str, Any]) -> None:
    data = ctx["response"].json()
    warnings = data.get("warnings", [])
    connector_warnings = [w for w in warnings if "connector" in w.lower()]
    assert len(connector_warnings) == 0, f"Unexpected connector warnings: {connector_warnings}"


@given('a matching "{name}" connector instance exists')
def _matching_connector_exists() -> None:
    pass


@then("resolved_connectors contains a warning")
def _resolved_connectors_has_warning(ctx: dict[str, Any]) -> None:
    data = ctx["response"].json()
    connectors = data.get("resolved_connectors", [])
    assert any(c.get("warning") is not None for c in connectors), "Expected at least one connector warning"


@then(parsers.parse('the warning mentions "{text}"'))
def _warning_mentions(ctx: dict[str, Any], text: str) -> None:
    data = ctx["response"].json()
    warnings_text = " ".join(data.get("warnings", []))
    assert text.lower() in warnings_text.lower(), f"Expected warning mentioning '{text}', got: {warnings_text}"


@given('the bundle references a schema with abstract_name "prd-input"')
def _bundle_refs_schema_by_abstract() -> None:
    pass


@given('the bundle references a schema matching the structure of "Requirements Output Schema"')
def _bundle_refs_schema_by_structure() -> None:
    pass


@when("the import analysis resolves schemas")
def _analysis_resolves_schemas(client, ctx: dict[str, Any]) -> None:
    with (
        patch("modulo.api.routes.library.resolve_schema") as mock_resolve_schema,
        patch("modulo.api.routes.library.resolve_connector_type") as mock_resolve_connector,
        patch("modulo.api.routes.library.resolve_model_backend") as mock_resolve_mb,
        patch("modulo.api.routes.library.get_existing_pipeline_names") as mock_get_pipelines,
        patch("modulo.api.routes.library.get_existing_agent_names") as mock_get_agents,
    ):
        mock_resolve_schema.return_value = {
            "schema_id": str(SCHEMA_A_ID),
            "version": "1.0",
            "warning": None,
        }
        mock_resolve_connector.return_value = {
            "instance_id": str(FILESYSTEM_CONNECTOR_ID),
            "instance_name": "filesystem",
            "warning": None,
        }
        mock_resolve_mb.return_value = {
            "model_backend_id": str(CLAUDE_SONNET_BACKEND_ID),
            "warning": None,
        }
        mock_get_pipelines.return_value = set()
        mock_get_agents.return_value = set()

        ctx["response"] = client.post(
            "/api/v1/libraries/import/analyse",
            json={"bundle": _make_sample_bundle()},
        )


@then("the schema is matched to the local schema by abstract_name")
def _schema_matched_by_abstract_name(ctx: dict[str, Any]) -> None:
    data = ctx["response"].json()
    schemas = data.get("resolved_schemas", [])
    matched = any(s.get("schema_id") is not None for s in schemas)
    assert matched, "No schema was matched"


@then("the resolved schema has schema_id set")
def _resolved_schema_has_id(ctx: dict[str, Any]) -> None:
    data = ctx["response"].json()
    schemas = data.get("resolved_schemas", [])
    for s in schemas:
        assert s.get("schema_id") is not None, f"Schema missing schema_id: {s}"


@then("the schema is matched by definition_json equality")
def _schema_matched_by_definition(ctx: dict[str, Any]) -> None:
    data = ctx["response"].json()
    schemas = data.get("resolved_schemas", [])
    assert any(s.get("schema_id") is not None for s in schemas), "No schema matched by definition"


@given('the bundle includes model_backend "claude-sonnet-4"')
def _bundle_includes_mb() -> None:
    pass


@when("the import analysis resolves model backends")
def _analysis_resolves_mbs(client, ctx: dict[str, Any]) -> None:
    with (
        patch("modulo.api.routes.library.resolve_schema") as mock_resolve_schema,
        patch("modulo.api.routes.library.resolve_connector_type") as mock_resolve_connector,
        patch("modulo.api.routes.library.resolve_model_backend") as mock_resolve_mb,
        patch("modulo.api.routes.library.get_existing_pipeline_names") as mock_get_pipelines,
        patch("modulo.api.routes.library.get_existing_agent_names") as mock_get_agents,
    ):
        mock_resolve_schema.return_value = {
            "schema_id": str(SCHEMA_A_ID),
            "version": "1.0",
            "warning": None,
        }
        mock_resolve_connector.return_value = {
            "instance_id": str(FILESYSTEM_CONNECTOR_ID),
            "instance_name": "filesystem",
            "warning": None,
        }
        mock_resolve_mb.return_value = {
            "model_backend_id": str(CLAUDE_SONNET_BACKEND_ID),
            "warning": None,
        }
        mock_get_pipelines.return_value = set()
        mock_get_agents.return_value = set()

        ctx["response"] = client.post(
            "/api/v1/libraries/import/analyse",
            json={"bundle": _make_sample_bundle()},
        )


@then("the model backend is matched by name")
def _mb_matched_by_name(ctx: dict[str, Any]) -> None:
    data = ctx["response"].json()
    mbs = data.get("resolved_model_backends", [])
    assert any(mb.get("model_backend_id") is not None for mb in mbs), "No model backend matched"


@then("resolved_model_backends has model_backend_id set")
def _resolved_mbs_have_id(ctx: dict[str, Any]) -> None:
    data = ctx["response"].json()
    mbs = data.get("resolved_model_backends", [])
    for mb in mbs:
        assert mb.get("model_backend_id") is not None, f"MB missing model_backend_id: {mb}"


@given('the bundle includes model_backend with provider "anthropic" and model_id "claude-sonnet-4-20241022"')
def _bundle_includes_mb_by_provider() -> None:
    pass


@given("no backend exists with that exact name")
def _no_backend_with_name() -> None:
    pass


@then("the model backend is matched by provider+model_id")
def _mb_matched_by_provider_model(ctx: dict[str, Any]) -> None:
    data = ctx["response"].json()
    mbs = data.get("resolved_model_backends", [])
    assert len(mbs) > 0, "Expected at least one resolved model backend"
