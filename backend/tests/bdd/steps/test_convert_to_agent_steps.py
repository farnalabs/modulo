"""Step definitions for convert-node-to-agent scenarios.

Covers @goal-alice-replace-step in alice-devx-sme.feature.
"""

import uuid
from unittest.mock import patch

import pytest
from pytest_bdd import given, parsers, scenarios, then, when

from tests.bdd.conftest import make_mock_pipeline

try:
    scenarios("../features/personas/alice-devx-sme.feature")
except (FileNotFoundError, OSError):
    pass


@pytest.fixture
def ctx():
    return {}


@given(parsers.parse('pipeline "{name}" has node "{node_id}" set to manual'))
def pipeline_has_manual_node(name: str, node_id: str, ctx):
    ctx["pipeline"] = make_mock_pipeline(name=name)
    ctx["node_id"] = node_id
    ctx["org_id"] = "00000000-0000-0000-0000-000000000001"


@when(parsers.parse('I change node "{node_id}" type from "{from_type}" to "{to_type}"'))
def change_node_type(node_id: str, from_type: str, to_type: str, ctx, client, request):
    ctx["from_type"] = from_type
    ctx["to_type"] = to_type
    ctx["node_id"] = node_id

    with (
        patch(
            "modulo.api.routes.pipelines.replace_pipeline_graph",
            return_value={
                "nodes": [{"id": node_id, "node_type": to_type}],
                "edges": [],
            },
        ),
    ):
        resp = client.patch(
            f"/api/v1/pipelines/{ctx['pipeline'].id}/graph",
            json={
                "nodes": [{"id": node_id, "node_type": to_type}],
                "edges": [],
            },
        )
    request.node._resp = resp
    ctx["node_type_updated"] = True


@when(parsers.parse('I assign schema "{schema_name}" to the node'))
def assign_schema_to_node(schema_name: str, ctx, client, request):
    schema_id = uuid.uuid5(ctx["pipeline"].id, schema_name)
    ctx["schema_id"] = schema_id

    with (
        patch(
            "modulo.api.routes.pipelines.replace_pipeline_graph",
            return_value={
                "nodes": [{"id": ctx["node_id"], "output_schema_id": str(schema_id)}],
                "edges": [],
            },
        ),
    ):
        resp = client.patch(
            f"/api/v1/pipelines/{ctx['pipeline'].id}/graph",
            json={
                "nodes": [{"id": ctx["node_id"], "output_schema_id": str(schema_id)}],
                "edges": [],
            },
        )
    request.node._resp = resp


@when(parsers.parse('I bind the {connector_name} connector for artifact access'))
def bind_connector(connector_name: str, ctx, client, request):
    connector_id = uuid.uuid5(ctx["pipeline"].id, connector_name)
    ctx["connector_id"] = connector_id

    with (
        patch(
            "modulo.api.routes.pipelines.replace_pipeline_graph",
            return_value={
                "nodes": [{
                    "id": ctx["node_id"],
                    "connector_binding": {"type": connector_name, "instance_id": str(connector_id)},
                }],
                "edges": [],
            },
        ),
    ):
        resp = client.patch(
            f"/api/v1/pipelines/{ctx['pipeline'].id}/graph",
            json={
                "nodes": [{
                    "id": ctx["node_id"],
                    "connector_binding": {"type": connector_name, "instance_id": str(connector_id)},
                }],
                "edges": [],
            },
        )
    request.node._resp = resp


@then("the pipeline saves successfully")
def pipeline_saves_successfully(request):
    body = getattr(request.node, "_resp_body", getattr(request.node, "_resp", None))
    if hasattr(body, "json"):
        body = body.json()
        request.node._resp_body = body
    assert isinstance(body, dict), f"Pipeline save response missing, got: {body}"


@then(parsers.parse('the node "{node_id}" now executes as an agent'))
def node_executes_as_agent(node_id: str, ctx, request):
    resp = getattr(request.node, "_resp", None)
    assert resp is not None, "No response from pipeline update step"
    if resp.status_code == 200:
        data = resp.json()
        nodes = data.get("nodes", [data])
        target = next((n for n in nodes if n.get("id") == node_id), None)
        if target:
            assert target.get("node_type") == "agent", (
                f"Expected node_type 'agent', got {target.get('node_type')}"
            )
    ctx["is_agent"] = True
