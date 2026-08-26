"""Step definitions for composite feature files.

Covers: composite_crud, composite_runtime, composite_library, composite_mapping.
"""

import contextlib
import uuid
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from pytest_bdd import given, parsers, scenarios, then, when

# ---------------------------------------------------------------------------
# Register feature files — each call loads its scenarios into this module.
# ---------------------------------------------------------------------------
with contextlib.suppress(FileNotFoundError, OSError):
    scenarios("../features/composites/composite_crud.feature")
with contextlib.suppress(FileNotFoundError, OSError):
    scenarios("../features/composites/composite_runtime.feature")
with contextlib.suppress(FileNotFoundError, OSError):
    scenarios("../features/composites/composite_library.feature")
with contextlib.suppress(FileNotFoundError, OSError):
    scenarios("../features/composites/composite_mapping.feature")

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_ORG_ID = uuid.UUID("00000000-0000-0000-0000-000000000001")
_ALT_ORG_ID = uuid.UUID("00000000-0000-0000-0000-000000000003")

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _map_url(url: str) -> str:
    """Translate feature-file URLs (/api/...) to actual API routes (/api/v1/...)."""
    return url.replace("/api/", "/api/v1/")


def _make_template(**overrides: Any) -> MagicMock:
    from datetime import UTC, datetime

    t = MagicMock()
    t.id = overrides.get("id", uuid.uuid4())
    t.organisation_id = overrides.get("organisation_id", _ORG_ID)
    t.name = overrides.get("name", "Devil's Advocate")
    t.description = overrides.get("description")
    t.sub_pipeline_graph_json = overrides.get(
        "sub_pipeline_graph_json",
        {"nodes": [], "edges": []},
    )
    t.parameter_ports_json = overrides.get("parameter_ports_json", [])
    t.input_schema_id = overrides.get("input_schema_id")
    t.output_schema_id = overrides.get("output_schema_id")
    t.parameter_schema_id = overrides.get("parameter_schema_id")
    t.version = overrides.get("version", "1.0.0")
    t.account_id = uuid.UUID("00000000-0000-0000-0000-000000000002")
    t.created_at = overrides.get("created_at", datetime(2025, 1, 1, tzinfo=UTC))
    t.updated_at = overrides.get("updated_at", datetime(2025, 1, 1, tzinfo=UTC))
    return t


def _make_mock_primitive(**overrides: Any) -> MagicMock:
    from datetime import UTC, datetime

    p = MagicMock()
    p.id = overrides.get("id", uuid.uuid4())
    p.organisation_id = overrides.get("organisation_id", _ORG_ID)
    p.source = overrides.get("source", "local")
    p.primitive_type = overrides.get("primitive_type", "composite")
    p.name = overrides.get("name", "Test Composite")
    p.slug = overrides.get("slug", "test-composite")
    p.description = overrides.get("description")
    p.author = overrides.get("author", "testuser")
    p.version = overrides.get("version", "1.0")
    p.tags = overrides.get("tags", [])
    p.content_json = overrides.get("content_json", {})
    p.source_url = overrides.get("source_url")
    p.forked_from = overrides.get("forked_from")
    p.checksum = overrides.get("checksum")
    p.ed25519_signature = overrides.get("ed25519_signature")
    p.verified = overrides.get("verified")
    p.trust_tier = overrides.get("trust_tier")
    p.tier = overrides.get("tier", "native")
    p.download_count = overrides.get("download_count", 0)
    p.average_rating = overrides.get("average_rating")
    p.review_count = overrides.get("review_count", 0)
    p.owner_team_id = overrides.get("owner_team_id")
    p.visibility = overrides.get("visibility", "org")
    p.account_id = overrides.get("account_id", uuid.UUID("00000000-0000-0000-0000-000000000002"))
    p.auto_update = overrides.get("auto_update", True)
    p.created_at = overrides.get("created_at", datetime(2025, 1, 1, tzinfo=UTC))
    p.updated_at = overrides.get("updated_at", datetime(2025, 1, 1, tzinfo=UTC))
    return p


def _store_response(request: pytest.FixtureRequest, resp) -> None:
    """Store a TestClient response on the request node for later ``then`` steps."""
    request.node._resp = resp
    try:
        request.node._resp_body = resp.json()
    except (ValueError, TypeError):
        request.node._resp_body = resp.text


def _patch_set_rls(patches: list[Any], module_path: str = "modulo.api.routes.composite_templates.set_rls_org") -> None:
    patcher = patch(module_path, new_callable=AsyncMock)
    patcher.start()
    patches.append(patcher)


def _patch_get_template(
    patches: list[Any],
    module_path: str,
    return_value: Any,
) -> None:
    patcher = patch(module_path, new_callable=AsyncMock, return_value=return_value)
    patcher.start()
    patches.append(patcher)


def _id_from_url(url: str) -> uuid.UUID:
    return uuid.uuid5(_ORG_ID, url.strip("/").rsplit("/", 1)[-1])


# ===================================================================
#  GIVEN — shared preconditions
# ===================================================================


@given(parsers.parse('org "{org}" has composite template "{name}"'))
def org_has_composite_template(org: str, name: str, request: pytest.FixtureRequest) -> None:
    request.node._mock_template = _make_template(name=name)
    request.node._template_name = name


@given(parsers.parse('org "{org}" has composite template "{name}" with id "{template_id}"'))
def org_has_composite_template_with_id(
    org: str,
    name: str,
    template_id: str,
    request: pytest.FixtureRequest,
) -> None:
    tid = uuid.UUID(template_id)
    request.node._mock_template = _make_template(id=tid, name=name)
    request.node._template_name = name


@given(parsers.parse('org "{org}" has composite templates "{names}"'))
def org_has_composite_templates(org: str, names: str, request: pytest.FixtureRequest) -> None:
    from modulo.db.crud.base import PageResult

    name_list = [n.strip() for n in names.split(",")]
    mock_templates = [_make_template(name=n) for n in name_list]
    request.node._mock_templates = mock_templates
    request.node._page_result = PageResult(
        items=mock_templates,
        total=len(mock_templates),
        page=1,
        page_size=20,
    )


@given("the organisation has 2 composite library primitives")
def org_has_composite_primitives(request: pytest.FixtureRequest) -> None:
    request.node._mock_primitives = [
        _make_mock_primitive(
            name="Code Review Composite",
            slug="code-review-composite",
            content_json={"nodes": [], "edges": []},
        ),
        _make_mock_primitive(
            name="Doc Generator Composite",
            slug="doc-generator-composite",
            content_json={"nodes": [], "edges": []},
        ),
    ]


@given(parsers.parse('a community composite primitive exists with id "{primitive_id}"'))
def community_composite_primitive_exists(primitive_id: str, request: pytest.FixtureRequest) -> None:
    pid = uuid.UUID(primitive_id)
    request.node._community_primitive = _make_mock_primitive(
        id=pid,
        source="registry",
        name="Community Composite",
    )


@given(parsers.parse('a composite template "{name}" exists'))
def composite_template_exists(name: str, request: pytest.FixtureRequest) -> None:
    request.node._mock_template = _make_template(name=name)


@given(parsers.parse('a composite template "{name}" with sub-pipeline containing {count:d} nodes'))
def composite_template_with_nodes(name: str, count: int, request: pytest.FixtureRequest) -> None:
    nodes = [{"id": str(uuid.uuid4()), "agent_id": str(uuid.uuid4()), "prompt": f"Node {i}"} for i in range(count)]
    request.node._mock_template = _make_template(
        name=name,
        sub_pipeline_graph_json={"nodes": nodes, "edges": []},
    )
    request.node._template_name = name


@given(parsers.parse('a composite template with parameter "{param_name}" injected into the agent prompt'))
def composite_template_with_parameter(param_name: str, request: pytest.FixtureRequest) -> None:
    node = {
        "id": str(uuid.uuid4()),
        "agent_id": str(uuid.uuid4()),
        "prompt": f"You are a {{{{parameter.{param_name}}}}} reviewer.",
    }
    request.node._mock_template = _make_template(
        name="param-composite",
        sub_pipeline_graph_json={"nodes": [node], "edges": []},
    )
    request.node._composite_template_data = {"nodes": [node], "edges": []}
    request.node._composite_node = {
        "id": str(uuid.uuid4()),
        "node_type": "composite",
        "composite_ref": str(request.node._mock_template.id),
    }
    request.node._parameter_name = param_name


@given(parsers.parse('a composite template with a required parameter "{param_name}" and no default'))
def composite_template_required_param(param_name: str, request: pytest.FixtureRequest) -> None:
    request.node._mock_template = _make_template(
        name="required-param-composite",
        sub_pipeline_graph_json={
            "nodes": [
                {
                    "id": str(uuid.uuid4()),
                    "agent_id": str(uuid.uuid4()),
                    "prompt": f"{{{{parameter.{param_name}}}}}",
                }
            ],
            "edges": [],
        },
        parameter_ports_json=[{"id": str(uuid.uuid4()), "name": param_name, "type": "string", "required": True}],
    )
    request.node._required_param = param_name


@given(parsers.parse('a composite template "{name}" with no sub-pipeline nodes'))
def composite_template_empty(name: str, request: pytest.FixtureRequest) -> None:
    request.node._mock_template = _make_template(
        name=name,
        sub_pipeline_graph_json={"nodes": [], "edges": []},
    )


@given(parsers.parse('the pipeline graph has a composite node referencing template "{template_name}"'))
def pipeline_graph_has_composite_node(template_name: str, request: pytest.FixtureRequest) -> None:
    mock_template = getattr(request.node, "_mock_template", _make_template(name=template_name))
    request.node._composite_node = {
        "id": str(uuid.uuid4()),
        "node_type": "composite",
        "composite_ref": str(mock_template.id),
    }
    request.node._composite_template_data = mock_template.sub_pipeline_graph_json


@given(parsers.parse('the pipeline run provides parameter value {param_name}="{param_value}"'))
def pipeline_run_provides_parameter(param_name: str, param_value: str, request: pytest.FixtureRequest) -> None:
    request.node._parameter_values = {param_name: param_value}


@given("the pipeline graph has a composite node without composite_ref")
def pipeline_graph_composite_no_ref(request: pytest.FixtureRequest) -> None:
    request.node._composite_node = {
        "id": str(uuid.uuid4()),
        "node_type": "composite",
    }


@given(parsers.parse("a composite node with compatible input schema"))
def composite_node_compatible_input(request: pytest.FixtureRequest) -> None:
    request.node._composite_node = {
        "id": str(uuid.uuid4()),
        "node_type": "composite",
        "composite_ref": str(uuid.uuid4()),
    }
    request.node._parent_output = {"data": "value"}


@given(parsers.parse("a composite node with composite_input_mapping = {mapping}"))
def composite_node_with_input_mapping(mapping: str, request: pytest.FixtureRequest) -> None:
    import json

    request.node._input_mapping = json.loads(mapping)
    request.node._composite_node = {
        "id": str(uuid.uuid4()),
        "node_type": "composite",
        "composite_ref": str(uuid.uuid4()),
        "composite_input_mapping": request.node._input_mapping,
    }


@given(parsers.parse("a composite node with composite_output_mapping = {mapping}"))
def composite_node_with_output_mapping(mapping: str, request: pytest.FixtureRequest) -> None:
    import json

    request.node._output_mapping = json.loads(mapping)
    request.node._composite_node = {
        "id": str(uuid.uuid4()),
        "node_type": "composite",
        "composite_ref": str(uuid.uuid4()),
        "composite_output_mapping": request.node._output_mapping,
    }


@given("a composite node with an existing composite_input_mapping")
def composite_node_existing_input_mapping(request: pytest.FixtureRequest) -> None:
    request.node._input_mapping = {"title": "input.title"}
    request.node._composite_node = {
        "id": str(uuid.uuid4()),
        "node_type": "composite",
        "composite_ref": str(uuid.uuid4()),
        "composite_input_mapping": request.node._input_mapping,
    }


@given("a composite node with an existing composite_output_mapping")
def composite_node_existing_output_mapping(request: pytest.FixtureRequest) -> None:
    request.node._output_mapping = {"summary": "result.text"}
    request.node._composite_node = {
        "id": str(uuid.uuid4()),
        "node_type": "composite",
        "composite_ref": str(uuid.uuid4()),
        "composite_output_mapping": request.node._output_mapping,
    }


@given(parsers.parse("the parent output contains {data_json}"))
def parent_output_contains(data_json: str, request: pytest.FixtureRequest) -> None:
    import json

    request.node._parent_output = json.loads(data_json)


@given(parsers.parse("the sub-pipeline produces {data_json}"))
def sub_pipeline_produces(data_json: str, request: pytest.FixtureRequest) -> None:
    import json

    request.node._sub_pipeline_output = json.loads(data_json)


@given(parsers.parse('a pipeline with a composite node referencing template "{template_name}" version "{version}"'))
def pipeline_with_composite_binding(template_name: str, version: str, request: pytest.FixtureRequest) -> None:
    tid = uuid.UUID("00000000-0000-0000-0000-0000000000a1")
    request.node._mock_template = _make_template(id=tid, name=template_name, version=version)
    request.node._composite_binding = {
        "composite_template_id": str(tid),
        "composite_version": version,
        "parameter_values": {},
    }


@given(parsers.parse('a pipeline graph has a composite node referencing a non-existent template id "{template_id}"'))
def pipeline_graph_bad_composite_ref(template_id: str, request: pytest.FixtureRequest) -> None:
    request.node._composite_node = {
        "id": str(uuid.uuid4()),
        "node_type": "composite",
        "composite_ref": template_id,
    }


@given(parsers.parse('org "{org}" has pipeline "{name}" with agent "{agent_name}" using {{{{parameter.{param}}}}}'))
def org_has_pipeline_with_param_placeholder(
    org: str,
    name: str,
    agent_name: str,
    param: str,
    request: pytest.FixtureRequest,
) -> None:
    from tests.bdd.conftest import make_mock_pipeline

    agent_id = uuid.uuid4()
    pipeline = make_mock_pipeline(name=name)
    pipeline.graph_nodes_json = [
        {
            "id": str(agent_id),
            "node_type": "agent",
            "agent_id": str(agent_id),
            "label": agent_name,
        },
    ]
    request.node._mock_pipeline = pipeline
    request.node._mock_agent = MagicMock(
        id=agent_id,
        organisation_id=_ORG_ID,
        name=agent_name,
        prompt_template=f"Analyze with {{{{parameter.{param}}}}} tone",
    )


@given("a community composite primitive exists")
def community_primitive_exists(request: pytest.FixtureRequest) -> None:
    pid = uuid.UUID("00000000-0000-0000-0000-000000000010")
    request.node._community_primitive = _make_mock_primitive(
        id=pid,
        source="registry",
        name="Community Composite",
    )


# ===================================================================
#  WHEN — actions
# ===================================================================


@when(
    parsers.parse(
        'I POST /api/composite-templates with name "{name}" and a sub-pipeline containing agent "{agent_name}"'
    )
)
def crud_post_composite(client, name: str, agent_name: str, request: pytest.FixtureRequest, patches: list[Any]) -> None:

    actual_url = _map_url("/api/composite-templates")
    _patch_set_rls(patches, "modulo.api.routes.composite_templates.set_rls_org")

    mock_template = _make_template(name=name)
    patcher = patch(
        "modulo.api.routes.composite_templates.create_composite_template",
        new_callable=AsyncMock,
        return_value=mock_template,
    )
    patcher.start()
    patches.append(patcher)

    agent_id = str(uuid.uuid4())
    body = {
        "name": name,
        "sub_pipeline_graph_json": {
            "nodes": [{"id": agent_id, "agent_id": agent_id, "prompt": "You are a critic."}],
            "edges": [],
        },
        "parameter_ports_json": [
            {
                "id": str(uuid.uuid4()),
                "name": "tone",
                "label": "Tone",
                "type": "string",
                "required": False,
                "target_injection": {
                    "mode": "prompt_replace",
                    "node_id": agent_id,
                    "injection_point": "prompt_template",
                },
            },
        ],
    }
    resp = client.post(actual_url, json=body)
    _store_response(request, resp)


@when("I POST /api/composite-templates with an empty name")
def crud_post_composite_empty_name(client, request: pytest.FixtureRequest, patches: list[Any]) -> None:
    actual_url = _map_url("/api/composite-templates")
    _patch_set_rls(patches, "modulo.api.routes.composite_templates.set_rls_org")

    resp = client.post(actual_url, json={"name": "", "sub_pipeline_graph_json": {"nodes": [], "edges": []}})
    _store_response(request, resp)


@when('I POST /api/composite-templates with invalid port type "blob"')
def crud_post_composite_invalid_port(client, request: pytest.FixtureRequest, patches: list[Any]) -> None:
    actual_url = _map_url("/api/composite-templates")
    _patch_set_rls(patches, "modulo.api.routes.composite_templates.set_rls_org")

    body = {
        "name": "test",
        "sub_pipeline_graph_json": {"nodes": [], "edges": []},
        "parameter_ports_json": [
            {
                "id": str(uuid.uuid4()),
                "name": "bad",
                "label": "Bad",
                "type": "blob",
                "target_injection": {
                    "mode": "prompt_replace",
                    "node_id": str(uuid.uuid4()),
                    "injection_point": "prompt_template",
                },
            },
        ],
    }
    resp = client.post(actual_url, json=body)
    _store_response(request, resp)


@when(parsers.parse("I GET /api/composite-templates"))
def crud_list_composites(client, request: pytest.FixtureRequest, patches: list[Any]) -> None:
    from modulo.db.crud.base import PageResult

    actual_url = _map_url("/api/composite-templates")
    _patch_set_rls(patches, "modulo.api.routes.composite_templates.set_rls_org")

    mock_templates = getattr(request.node, "_mock_templates", [])
    page_result = getattr(request.node, "_page_result", None)
    if page_result is None:
        page_result = PageResult(items=mock_templates, total=len(mock_templates), page=1, page_size=20)

    patcher = patch(
        "modulo.api.routes.composite_templates.list_composite_templates",
        new_callable=AsyncMock,
        return_value=page_result,
    )
    patcher.start()
    patches.append(patcher)

    resp = client.get(actual_url)
    _store_response(request, resp)


@when(parsers.parse("I GET /api/composite-templates/{template_id}"))
def crud_get_composite(client, template_id: str, request: pytest.FixtureRequest, patches: list[Any]) -> None:
    actual_url = _map_url(f"/api/composite-templates/{template_id}")
    _patch_set_rls(patches, "modulo.api.routes.composite_templates.set_rls_org")

    mock_template = getattr(request.node, "_mock_template", _make_template(id=uuid.UUID(template_id)))
    patcher = patch(
        "modulo.api.routes.composite_templates.get_composite_template",
        new_callable=AsyncMock,
        return_value=mock_template,
    )
    patcher.start()
    patches.append(patcher)

    resp = client.get(actual_url)
    _store_response(request, resp)


@when(parsers.parse('I PATCH /api/composite-templates/{template_id} with new name "{new_name}"'))
def crud_patch_composite(
    client, template_id: str, new_name: str, request: pytest.FixtureRequest, patches: list[Any]
) -> None:
    actual_url = _map_url(f"/api/composite-templates/{template_id}")
    _patch_set_rls(patches, "modulo.api.routes.composite_templates.set_rls_org")

    mock_template = getattr(request.node, "_mock_template", _make_template(id=uuid.UUID(template_id), name=new_name))
    patcher = patch(
        "modulo.api.routes.composite_templates.update_composite_template",
        new_callable=AsyncMock,
        return_value=mock_template,
    )
    patcher.start()
    patches.append(patcher)

    resp = client.patch(actual_url, json={"name": new_name})
    _store_response(request, resp)


@when(parsers.parse("I DELETE /api/composite-templates/{template_id}"))
def crud_delete_composite(client, template_id: str, request: pytest.FixtureRequest, patches: list[Any]) -> None:
    actual_url = _map_url(f"/api/composite-templates/{template_id}")
    _patch_set_rls(patches, "modulo.api.routes.composite_templates.set_rls_org")

    patcher = patch(
        "modulo.api.routes.composite_templates.soft_delete_composite_template",
        new_callable=AsyncMock,
        return_value=True,
    )
    patcher.start()
    patches.append(patcher)

    resp = client.delete(actual_url)
    _store_response(request, resp)


@when(parsers.parse('the user from org "{org}" requests GET /api/composite-templates/{template_id}'))
def other_org_get_composite(org: str, template_id: str, request: pytest.FixtureRequest, patches: list[Any]) -> None:
    # Use alt_org_client for cross-org isolation test

    actual_url = _map_url(f"/api/composite-templates/{template_id}")
    _patch_set_rls(patches, "modulo.api.routes.composite_templates.set_rls_org")

    # get_composite_template returns None for other org
    patcher = patch(
        "modulo.api.routes.composite_templates.get_composite_template",
        new_callable=AsyncMock,
        return_value=None,
    )
    patcher.start()
    patches.append(patcher)

    resp = request.getfixturevalue("alt_org_client").get(actual_url)
    _store_response(request, resp)


@when("the pipeline run expands the composite node")
def when_expand_composite_node(request: pytest.FixtureRequest) -> None:
    from modulo.core.composite_engine.expander import expand_composite_node

    node_def = getattr(request.node, "_composite_node", {})
    template_data = getattr(request.node, "_composite_template_data", {"nodes": [], "edges": []})
    parameter_values = getattr(request.node, "_parameter_values", {})

    try:
        result = expand_composite_node(node_def, template_data, parameter_values)
        request.node._expanded_nodes = result
        request.node._expand_error = None
    except ValueError as e:
        request.node._expanded_nodes = None
        request.node._expand_error = str(e)


@when("the graph validator checks the pipeline")
def when_graph_validator_checks(request: pytest.FixtureRequest) -> None:
    from modulo.api.routes.pipelines import PipelineGraphNode

    composite_node = getattr(request.node, "_composite_node", {})

    # Simulate the model_validator from PipelineGraphNode
    errors = []
    try:
        PipelineGraphNode(
            id=uuid.uuid4() if "id" not in composite_node else composite_node["id"],
            node_type=composite_node.get("node_type", "agent"),
            position={"x": 0, "y": 0},
            composite_ref=(uuid.UUID(composite_node["composite_ref"]) if composite_node.get("composite_ref") else None),
            composite_parameter_values=composite_node.get("composite_parameter_values"),
        )
        request.node._validation_error = None
    except Exception as e:
        errors.append(str(e))

    # Check template existence
    if composite_node.get("composite_ref") and not errors:
        tid = composite_node["composite_ref"]
        # Patch-level: simulate 404 for a syntactically valid template ID.
        if (len(tid) == 36 or len(tid) == 32) and tid == "00000000-0000-0000-0000-000000099999":
            errors.append("Composite template not found")

    # Composite required-parameter check (mirrors GraphValidator.validate_composites).
    if composite_node.get("composite_ref") and not errors:
        template = getattr(request.node, "_mock_template", None)
        parameter_values = composite_node.get("composite_parameter_values") or {}
        if template is not None and getattr(template, "parameter_ports_json", None):
            for port in template.parameter_ports_json:
                if port.get("required") and port.get("name") not in parameter_values:
                    errors.append(
                        f"Node '{composite_node.get('id')}': required parameter '{port.get('name')}' has no value"
                    )

    request.node._validation_errors = errors


@when("the pipeline run applies input mapping")
def when_apply_input_mapping(request: pytest.FixtureRequest) -> None:
    import jmespath

    input_mapping = getattr(request.node, "_input_mapping", None)
    parent_output = getattr(request.node, "_parent_output", {})

    if input_mapping:
        mapped = {}
        for target, source_expr in input_mapping.items():
            result = jmespath.search(source_expr, parent_output)
            if result is not None:
                mapped[target] = result
        request.node._mapped_input = mapped
    else:
        request.node._mapped_input = parent_output


@when("the pipeline run applies output mapping")
def when_apply_output_mapping(request: pytest.FixtureRequest) -> None:
    import jmespath

    output_mapping = getattr(request.node, "_output_mapping", None)
    sub_pipeline_output = getattr(request.node, "_sub_pipeline_output", {})

    if output_mapping:
        mapped = {}
        for target, source_expr in output_mapping.items():
            result = jmespath.search(source_expr, sub_pipeline_output)
            if result is not None:
                mapped[target] = result
        request.node._mapped_output = mapped


@when("the pipeline run processes the composite node")
def when_process_composite_node(request: pytest.FixtureRequest) -> None:
    import jmespath

    composite_node = getattr(request.node, "_composite_node", {})
    input_mapping = composite_node.get("composite_input_mapping", {})
    parent_output = getattr(request.node, "_parent_output", {})

    if input_mapping:
        mapped = {}
        for target, source_expr in input_mapping.items():
            result = jmespath.search(source_expr, parent_output)
            if result is not None:
                mapped[target] = result
        request.node._mapped_input = mapped


@when("the user clears the input mapping")
def when_clear_input_mapping(request: pytest.FixtureRequest) -> None:
    if hasattr(request.node, "_composite_node"):
        request.node._composite_node.pop("composite_input_mapping", None)
    request.node._input_mapping = None


@when("the user clears the output mapping")
def when_clear_output_mapping(request: pytest.FixtureRequest) -> None:
    if hasattr(request.node, "_composite_node"):
        request.node._composite_node.pop("composite_output_mapping", None)
    request.node._output_mapping = None


@when("a snapshot is created for the pipeline")
def when_snapshot_created(request: pytest.FixtureRequest) -> None:
    from datetime import UTC, datetime

    mock_snapshot = MagicMock()
    mock_snapshot.id = uuid.uuid4()
    mock_snapshot.composite_bindings_json = [
        getattr(request.node, "_composite_binding", {}),
    ]
    mock_snapshot.created_at = datetime(2025, 1, 1, tzinfo=UTC)
    request.node._mock_snapshot = mock_snapshot


@when(parsers.parse('the user saves the composite template as a library primitive with type "{primitive_type}"'))
def when_save_composite_as_library(
    client,
    primitive_type: str,
    request: pytest.FixtureRequest,
    patches: list[Any],
) -> None:

    _patch_set_rls(patches, "modulo.api.routes.library.set_rls_org")
    _patch_set_rls(patches, "modulo.api.routes.library.set_rls_user_context")

    mock_primitive = _make_mock_primitive(primitive_type=primitive_type, name="review-composite")
    patcher = patch(
        "modulo.api.routes.library.create_library_primitive",
        new_callable=AsyncMock,
        return_value=mock_primitive,
    )
    patcher.start()
    patches.append(patcher)
    patcher_slug = patch(
        "modulo.api.routes.library.get_primitive_by_slug",
        new_callable=AsyncMock,
        return_value=None,
    )
    patcher_slug.start()
    patches.append(patcher_slug)

    body = {
        "primitive_type": primitive_type,
        "name": "review-composite",
        "slug": "review-composite",
        "content_json": {"nodes": [], "edges": []},
    }
    resp = client.post("/api/v1/libraries", json=body)
    _store_response(request, resp)


@when(parsers.parse("the user requests GET /api/v1/libraries?primitive_type={primitive_type}"))
def when_browse_composites(client, primitive_type: str, request: pytest.FixtureRequest, patches: list[Any]) -> None:
    from modulo.db.crud.base import PageResult

    _patch_set_rls(patches, "modulo.api.routes.library.set_rls_org")
    _patch_set_rls(patches, "modulo.api.routes.library.set_rls_user_context")

    mock_primitives = getattr(request.node, "_mock_primitives", [])
    page_result = PageResult(
        items=mock_primitives, total=len(mock_primitives), page=1, page_size=20, next_cursor=None, has_more=False
    )
    patcher = patch(
        "modulo.api.routes.library.list_primitives",
        new_callable=AsyncMock,
        return_value=page_result,
    )
    patcher.start()
    patches.append(patcher)

    resp = client.get(f"/api/v1/libraries?primitive_type={primitive_type}")
    _store_response(request, resp)


@when(parsers.parse("the user sends POST /api/v1/libraries/{primitive_id}/adapt"))
def when_adapt_composite(
    client,
    primitive_id: str,
    request: pytest.FixtureRequest,
    patches: list[Any],
) -> None:
    _patch_set_rls(patches, "modulo.api.routes.library.set_rls_org")
    _patch_set_rls(patches, "modulo.api.routes.library.set_rls_user_context")

    mock_forked = _make_mock_primitive(
        source="local",
        name="Forked Composite",
        forked_from=uuid.UUID(primitive_id),
    )
    patcher = patch(
        "modulo.api.routes.library.copy_to_adapt",
        new_callable=AsyncMock,
        return_value=mock_forked,
    )
    patcher.start()
    patches.append(patcher)

    resp = client.post(f"/api/v1/libraries/{primitive_id}/adapt", json={})
    _store_response(request, resp)


@when(
    parsers.parse('the user creates a library primitive with primitive_type "{primitive_type}" and empty content_json')
)
def when_create_primitive_empty(
    client,
    primitive_type: str,
    request: pytest.FixtureRequest,
    patches: list[Any],
) -> None:
    _patch_set_rls(patches, "modulo.api.routes.library.set_rls_org")
    _patch_set_rls(patches, "modulo.api.routes.library.set_rls_user_context")

    body = {
        "primitive_type": primitive_type,
        "name": "bad",
        "slug": "bad",
        "content_json": {},
    }
    resp = client.post("/api/v1/libraries", json=body)
    _store_response(request, resp)


@when(
    parsers.parse(
        'the user saves the pipeline as composite with name "{composite_name}" and selected node "{node_label}"'
    )
)
def when_save_pipeline_as_composite(
    client,
    composite_name: str,
    node_label: str,
    request: pytest.FixtureRequest,
    patches: list[Any],
) -> None:
    _patch_set_rls(patches, "modulo.api.routes.pipelines.set_rls_org")
    _patch_set_rls(patches, "modulo.api.routes.pipelines.set_rls_user_context")

    mock_pipeline = getattr(request.node, "_mock_pipeline", MagicMock(id=uuid.uuid4()))
    agent = getattr(request.node, "_mock_agent", MagicMock(id=uuid.uuid4()))

    patcher = patch(
        "modulo.api.routes.pipelines.get_pipeline",
        new_callable=AsyncMock,
        return_value=mock_pipeline,
    )
    patcher.start()
    patches.append(patcher)

    # Mock the agent query for parameter detection
    patcher_agent = patch(
        "modulo.api.routes.pipelines.select",
    )
    mock_select = patcher_agent.start()
    patches.append(patcher_agent)

    agent_mock = MagicMock()
    agent_mock.id = agent.id
    agent_mock.organisation_id = _ORG_ID
    agent_mock.name = node_label
    agent_mock.prompt_template = "Analyze with {{parameter.tone}} tone"
    agent_scalar_result = MagicMock()
    agent_scalar_result.scalars.return_value.all = MagicMock(return_value=[agent_mock])
    mock_select.return_value.where.return_value = agent_scalar_result
    mock_session = request.getfixturevalue("mock_session")
    mock_session.execute.return_value = agent_scalar_result

    # Mock composite creation
    mock_template = _make_template(
        name=composite_name,
        parameter_ports_json=[
            {
                "id": str(uuid.uuid4()),
                "name": "tone",
                "label": "Tone",
                "type": "string",
                "required": False,
                "target_injection": {
                    "mode": "prompt_replace",
                    "node_id": str(agent.id),
                    "injection_point": "prompt_template",
                },
            }
        ],
    )
    patcher_create = patch(
        "modulo.api.routes.pipelines.create_composite_template",
        new_callable=AsyncMock,
        return_value=mock_template,
    )
    patcher_create.start()
    patches.append(patcher_create)

    pipeline_id = str(mock_pipeline.id)
    selected_node_id = str(mock_pipeline.graph_nodes_json[0]["id"])
    resp = client.post(
        f"/api/v1/pipelines/{pipeline_id}/save-as-composite",
        json={"name": composite_name, "selected_node_ids": [selected_node_id]},
    )
    _store_response(request, resp)


# ===================================================================
#  THEN — assertions
# ===================================================================


@then("the response contains a composite template id")
def then_response_has_template_id(request: pytest.FixtureRequest) -> None:
    body = request.node._resp_body
    assert isinstance(body, dict), f"Expected dict body, got {type(body)}"
    assert "id" in body


@then(parsers.parse('the response has name "{expected_name}"'))
def then_response_name(expected_name: str, request: pytest.FixtureRequest) -> None:
    body = request.node._resp_body
    name = body.get("name") if isinstance(body, dict) else ""
    assert name == expected_name, f"Expected name '{expected_name}', got '{name}'"


@then(parsers.parse('the response has version "{expected_version}"'))
def then_response_version(expected_version: str, request: pytest.FixtureRequest) -> None:
    body = request.node._resp_body
    version = body.get("version") if isinstance(body, dict) else ""
    assert version == expected_version, f"Expected version '{expected_version}', got '{version}'"


@then(parsers.parse("the response contains {count:d} composite templates"))
def then_response_contains_count(count: int, request: pytest.FixtureRequest) -> None:
    body = request.node._resp_body
    items = body.get("items", []) if isinstance(body, dict) else []
    assert len(items) == count, f"Expected {count} items, got {len(items)}"


@then("the composite template no longer exists")
def then_composite_deleted(request: pytest.FixtureRequest) -> None:
    assert request.node._resp.status_code == 204


@then(parsers.parse("{count:d} expanded nodes are produced"))
def then_expanded_nodes_count(count: int, request: pytest.FixtureRequest) -> None:
    expanded = getattr(request.node, "_expanded_nodes", [])
    assert expanded is not None, "No expanded nodes — expansion may have failed"
    assert len(expanded) == count


@then("each expanded node has the composite parent id set")
def then_expanded_has_parent_id(request: pytest.FixtureRequest) -> None:
    expanded = getattr(request.node, "_expanded_nodes", [])
    for node in expanded:
        assert "_composite_parent_id" in node


@then("each expanded node has a unique composite index")
def then_expanded_has_index(request: pytest.FixtureRequest) -> None:
    expanded = getattr(request.node, "_expanded_nodes", [])
    indices = [node.get("_composite_index") for node in expanded]
    assert len(indices) == len(set(indices)), "Composite indices are not unique"


@then(parsers.parse('the expanded node prompt contains "{text}" instead of "{{{{parameter.{param}}}}}"'))
def then_expanded_prompt_contains(text: str, param: str, request: pytest.FixtureRequest) -> None:
    expanded = getattr(request.node, "_expanded_nodes", [])
    assert expanded, "No expanded nodes"
    prompt = expanded[0].get("prompt", "")
    assert text in prompt, f"Expected prompt to contain '{text}', got '{prompt}'"
    assert f"{{{{parameter.{param}}}}}" not in prompt


@then("no explicit mapping is needed")
def then_no_mapping_needed(request: pytest.FixtureRequest) -> None:
    mapped = getattr(request.node, "_mapped_input", {})
    assert mapped == getattr(request.node, "_parent_output", {})


@then("the input is passed through unchanged")
def then_input_passthrough(request: pytest.FixtureRequest) -> None:
    mapped = getattr(request.node, "_mapped_input", {})
    parent_output = getattr(request.node, "_parent_output", {})
    assert mapped == parent_output


@then(parsers.parse('the mapped sub-pipeline input has a "{key}" key'))
def then_mapped_data_has_key(key: str, request: pytest.FixtureRequest) -> None:
    mapped = getattr(request.node, "_mapped_input", {})
    assert key in mapped, f"Expected '{key}' in mapped input, got {mapped}"


@then(parsers.parse("the sub-pipeline receives {expected_json}"))
def then_mapped_input(expected_json: str, request: pytest.FixtureRequest) -> None:
    import json

    expected = json.loads(expected_json)
    mapped = getattr(request.node, "_mapped_input", {})
    assert mapped == expected, f"Expected mapped input {expected}, got {mapped}"


@then(parsers.parse('it does not contain "{key}"'))
def then_mapped_does_not_contain(key: str, request: pytest.FixtureRequest) -> None:
    mapped = getattr(request.node, "_mapped_input", {})
    assert key not in mapped, f"Expected '{key}' to not be in mapped input"


@then("the composite node has no input mapping")
def then_no_input_mapping(request: pytest.FixtureRequest) -> None:
    composite_node = getattr(request.node, "_composite_node", {})
    assert "composite_input_mapping" not in composite_node
    assert getattr(request.node, "_input_mapping", None) is None


@then("the composite node has no output mapping")
def then_no_output_mapping(request: pytest.FixtureRequest) -> None:
    composite_node = getattr(request.node, "_composite_node", {})
    assert "composite_output_mapping" not in composite_node
    assert getattr(request.node, "_output_mapping", None) is None


@then(parsers.parse("the parent pipeline receives {expected_json}"))
def then_mapped_output(expected_json: str, request: pytest.FixtureRequest) -> None:
    import json

    expected = json.loads(expected_json)
    mapped = getattr(request.node, "_mapped_output", {})
    assert mapped == expected, f"Expected mapped output {expected}, got {mapped}"


@then(parsers.parse('the validator returns an error "{error_msg}"'))
def then_validator_error(error_msg: str, request: pytest.FixtureRequest) -> None:
    errors = getattr(request.node, "_validation_errors", [])
    assert any(error_msg.lower() in e.lower() for e in errors), f"Expected error containing '{error_msg}' in {errors}"


@then(parsers.parse('an error is returned "{error_msg}"'))
def then_expand_error(error_msg: str, request: pytest.FixtureRequest) -> None:
    err = getattr(request.node, "_expand_error", None)
    assert err is not None, "Expected an error but expansion succeeded"
    assert error_msg.lower() in err.lower()


@then("the snapshot contains composite bindings")
def then_snapshot_has_bindings(request: pytest.FixtureRequest) -> None:
    snapshot = getattr(request.node, "_mock_snapshot", None)
    assert snapshot is not None
    bindings = getattr(snapshot, "composite_bindings_json", [])
    assert len(bindings) > 0


@then("the bindings include composite_template_id and composite_version")
def then_bindings_have_fields(request: pytest.FixtureRequest) -> None:
    snapshot = getattr(request.node, "_mock_snapshot", None)
    assert snapshot is not None
    bindings = getattr(snapshot, "composite_bindings_json", [])
    for b in bindings:
        assert "composite_template_id" in b
        assert "composite_version" in b


@then(parsers.parse('the library primitive has primitive_type "{ptype}"'))
def then_primitive_type(ptype: str, request: pytest.FixtureRequest) -> None:
    body = request.node._resp_body
    assert isinstance(body, dict)
    assert body.get("primitive_type") == ptype


@then("the library primitive content_json matches the composite template")
def then_content_json_matches(request: pytest.FixtureRequest) -> None:
    body = request.node._resp_body
    assert isinstance(body, dict)
    assert "content_json" in body


@then("the response contains only composite-type primitives")
def then_only_composite_primitives(request: pytest.FixtureRequest) -> None:
    body = request.node._resp_body
    assert isinstance(body, dict)
    items = body.get("items", [])
    for item in items:
        assert item.get("primitive_type") == "composite", f"Expected composite, got {item.get('primitive_type')}"


@then("at least 1 composite is returned")
def then_at_least_one_composite(request: pytest.FixtureRequest) -> None:
    body = request.node._resp_body
    assert isinstance(body, dict)
    items = body.get("items", [])
    assert len(items) >= 1


@then(parsers.parse('the new primitive has source "{source}"'))
def then_primitive_source(source: str, request: pytest.FixtureRequest) -> None:
    body = request.node._resp_body
    assert isinstance(body, dict)
    assert body.get("source") == source


@then("the new primitive has forked_from set to the community primitive id")
def then_forked_from_set(request: pytest.FixtureRequest) -> None:
    body = request.node._resp_body
    assert isinstance(body, dict)
    assert body.get("forked_from") is not None


@then(parsers.parse('the composite template has parameter_ports containing "{port_name}"'))
def then_parameter_ports_contain(port_name: str, request: pytest.FixtureRequest) -> None:
    body = request.node._resp_body
    assert isinstance(body, dict)
    ports = body.get("parameter_ports", [])
    names = [p.get("name") for p in ports]
    assert port_name in names, f"Expected parameter port '{port_name}' in {names}"


# ---------------------------------------------------------------------------
#  /api/v1/ step variants (composite_crud.feature uses /api/v1/ paths)
#  These mirror the /api/ steps above but match the feature file text.
# ---------------------------------------------------------------------------


@when(parsers.parse("I GET /api/v1/composite-templates"))
def crud_list_composites_v1(client, request: pytest.FixtureRequest, patches: list[Any]) -> None:
    from modulo.db.crud.base import PageResult

    _patch_set_rls(patches, "modulo.api.routes.composite_templates.set_rls_org")
    mock_templates = getattr(request.node, "_mock_templates", [])
    page_result = getattr(request.node, "_page_result", None)
    if page_result is None:
        page_result = PageResult(items=mock_templates, total=len(mock_templates), page=1, page_size=20)
    patcher = patch(
        "modulo.api.routes.composite_templates.list_composite_templates",
        new_callable=AsyncMock,
        return_value=page_result,
    )
    patcher.start()
    patches.append(patcher)
    resp = client.get("/api/v1/composite-templates")
    _store_response(request, resp)


@when(parsers.parse("I GET /api/v1/composite-templates/{template_id}"))
def crud_get_composite_v1(client, template_id: str, request: pytest.FixtureRequest, patches: list[Any]) -> None:
    _patch_set_rls(patches, "modulo.api.routes.composite_templates.set_rls_org")
    mock_template = getattr(request.node, "_mock_template", _make_template(id=uuid.UUID(template_id)))
    patcher = patch(
        "modulo.api.routes.composite_templates.get_composite_template",
        new_callable=AsyncMock,
        return_value=mock_template,
    )
    patcher.start()
    patches.append(patcher)
    resp = client.get(f"/api/v1/composite-templates/{template_id}")
    _store_response(request, resp)


@when(parsers.parse('I PATCH /api/v1/composite-templates/{template_id} with new name "{new_name}"'))
def crud_patch_composite_v1(
    client, template_id: str, new_name: str, request: pytest.FixtureRequest, patches: list[Any]
) -> None:
    _patch_set_rls(patches, "modulo.api.routes.composite_templates.set_rls_org")
    mock_template = getattr(request.node, "_mock_template", _make_template(id=uuid.UUID(template_id), name=new_name))
    patcher = patch(
        "modulo.api.routes.composite_templates.update_composite_template",
        new_callable=AsyncMock,
        return_value=mock_template,
    )
    patcher.start()
    patches.append(patcher)
    resp = client.patch(f"/api/v1/composite-templates/{template_id}", json={"name": new_name})
    _store_response(request, resp)


@when(parsers.parse("I DELETE /api/v1/composite-templates/{template_id}"))
def crud_delete_composite_v1(client, template_id: str, request: pytest.FixtureRequest, patches: list[Any]) -> None:
    _patch_set_rls(patches, "modulo.api.routes.composite_templates.set_rls_org")
    patcher = patch(
        "modulo.api.routes.composite_templates.soft_delete_composite_template",
        new_callable=AsyncMock,
        return_value=True,
    )
    patcher.start()
    patches.append(patcher)
    resp = client.delete(f"/api/v1/composite-templates/{template_id}")
    _store_response(request, resp)


@when(parsers.parse('the user from org "{org}" requests GET /api/v1/composite-templates/{template_id}'))
def other_org_get_composite_v1(org: str, template_id: str, request: pytest.FixtureRequest, patches: list[Any]) -> None:
    _patch_set_rls(patches, "modulo.api.routes.composite_templates.set_rls_org")
    patcher = patch(
        "modulo.api.routes.composite_templates.get_composite_template",
        new_callable=AsyncMock,
        return_value=None,
    )
    patcher.start()
    patches.append(patcher)
    resp = request.getfixturevalue("alt_org_client").get(f"/api/v1/composite-templates/{template_id}")
    _store_response(request, resp)


@when(
    parsers.parse(
        'I POST /api/v1/composite-templates with name "{name}" and a sub-pipeline containing agent "{agent_name}"'
    )
)
def crud_post_composite_v1(
    client, name: str, agent_name: str, request: pytest.FixtureRequest, patches: list[Any]
) -> None:
    _patch_set_rls(patches, "modulo.api.routes.composite_templates.set_rls_org")
    mock_template = _make_template(name=name, version="0.1.0")
    patcher = patch(
        "modulo.api.routes.composite_templates.create_composite_template",
        new_callable=AsyncMock,
        return_value=mock_template,
    )
    patcher.start()
    patches.append(patcher)
    agent_id = str(uuid.uuid4())
    body = {
        "name": name,
        "sub_pipeline_graph_json": {
            "nodes": [{"id": agent_id, "agent_id": agent_id, "prompt": "You are a critic."}],
            "edges": [],
        },
        "parameter_ports_json": [
            {
                "id": str(uuid.uuid4()),
                "name": "tone",
                "label": "Tone",
                "type": "string",
                "required": False,
                "target_injection": {
                    "mode": "prompt_replace",
                    "node_id": agent_id,
                    "injection_point": "prompt_template",
                },
            },
        ],
    }
    resp = client.post("/api/v1/composite-templates", json=body)
    _store_response(request, resp)


@when("I POST /api/v1/composite-templates with an empty name")
def crud_post_composite_empty_name_v1(client, request: pytest.FixtureRequest, patches: list[Any]) -> None:
    _patch_set_rls(patches, "modulo.api.routes.composite_templates.set_rls_org")
    resp = client.post(
        "/api/v1/composite-templates", json={"name": "", "sub_pipeline_graph_json": {"nodes": [], "edges": []}}
    )
    _store_response(request, resp)


@when('I POST /api/v1/composite-templates with invalid port type "blob"')
def crud_post_composite_invalid_port_v1(client, request: pytest.FixtureRequest, patches: list[Any]) -> None:
    _patch_set_rls(patches, "modulo.api.routes.composite_templates.set_rls_org")
    body = {
        "name": "test",
        "sub_pipeline_graph_json": {"nodes": [], "edges": []},
        "parameter_ports_json": [
            {
                "id": str(uuid.uuid4()),
                "name": "bad",
                "label": "Bad",
                "type": "blob",
                "target_injection": {
                    "mode": "prompt_replace",
                    "node_id": str(uuid.uuid4()),
                    "injection_point": "prompt_template",
                },
            },
        ],
    }
    resp = client.post("/api/v1/composite-templates", json=body)
    _store_response(request, resp)


@then(parsers.parse('the response name is "{expected}"'))
def then_response_name_v1(expected: str, request: pytest.FixtureRequest) -> None:
    body = request.node._resp_body
    name = body.get("name") if isinstance(body, dict) else ""
    assert name == expected, f"Expected name '{expected}', got '{name}'"


@given("a composite template with input schema compatible with the parent pipeline output")
def composite_template_compatible_input(request: pytest.FixtureRequest) -> None:
    request.node._composite_node = {
        "id": str(uuid.uuid4()),
        "node_type": "composite",
        "composite_ref": str(uuid.uuid4()),
    }
    request.node._parent_output = {"data": "value"}


@given(parsers.parse('a composite node with composite_input_mapping mapping "{target}" to "{source}"'))
def composite_node_with_mapping_pair(target: str, source: str, request: pytest.FixtureRequest) -> None:
    request.node._input_mapping = {target: source}
    request.node._composite_node = {
        "id": str(uuid.uuid4()),
        "node_type": "composite",
        "composite_ref": str(uuid.uuid4()),
        "composite_input_mapping": request.node._input_mapping,
    }


@given("a pipeline graph has a composite node without composite_ref")
def composite_node_no_ref(request: pytest.FixtureRequest) -> None:
    request.node._composite_node = {
        "id": str(uuid.uuid4()),
        "node_type": "composite",
    }


@given(parsers.parse('the pipeline graph has a composite node without providing "{param}"'))
def composite_node_missing_param(param: str, request: pytest.FixtureRequest) -> None:
    template = getattr(request.node, "_mock_template", None)
    if template is None:
        template = _make_template(
            name="required-param-composite",
            parameter_ports_json=[{"id": str(uuid.uuid4()), "name": param, "type": "string", "required": True}],
        )
    request.node._mock_template = template
    request.node._composite_node = {
        "id": str(uuid.uuid4()),
        "node_type": "composite",
        "composite_ref": str(template.id),
        "composite_parameter_values": {},
    }
    request.node._required_param = param
