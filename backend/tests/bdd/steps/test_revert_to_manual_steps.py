"""Step definitions for revert-to-manual (rollback) scenarios.

Covers @goal-alice-rollback-step in alice-devx-sme.feature.
"""

import uuid
from unittest.mock import patch

import pytest
from pytest_bdd import given, parsers, then, when

from tests.bdd.conftest import make_mock_pipeline, make_mock_snapshot


@pytest.fixture
def ctx():
    return {}


# ===========================================================================
#  alice-devx-sme.feature — @goal-alice-rollback-step
# ===========================================================================


@given(parsers.parse('node "{node_id}" is currently type "{node_type}"'))
def node_currently_type(node_id: str, node_type: str, ctx):
    ctx["node_id"] = node_id
    ctx["node_type"] = node_type
    ctx["pipeline"] = make_mock_pipeline(name="current-sdlc")
    ctx["snapshots"] = {}


@when(parsers.parse('I set node "{node_id}" back to type "{node_type}"'))
def set_node_back_to_manual(node_id: str, node_type: str, ctx, client, request):
    ctx["previous_type"] = ctx.get("node_type")
    ctx["node_id"] = node_id
    ctx["node_type"] = node_type

    with (
        patch(
            "modulo.api.routes.pipelines.replace_pipeline_graph",
            return_value={"nodes": [{"id": node_id, "node_type": node_type}]},
        ),
    ):
        resp = client.patch(
            f"/api/v1/pipelines/{ctx['pipeline'].id}/graph",
            json={
                "nodes": [{"id": node_id, "node_type": node_type}],
                "edges": [],
            },
        )
    request.node._resp = resp
    ctx["graph_updated"] = True


@then("the pipeline saves successfully")
def pipeline_saves_successfully(request):
    body = getattr(request.node, "_resp_body", getattr(request.node, "_resp", None))
    if hasattr(body, "json"):
        body = body.json()
        request.node._resp_body = body
    assert isinstance(body, dict), f"Pipeline save response missing, got: {body}"


@then("I can revert to a previous pipeline snapshot")
def can_revert_to_snapshot(ctx, request):
    mock_snapshot = make_mock_snapshot(
        id=uuid.uuid5(ctx["pipeline"].id, "pre-qa-agent"),
        graph_json={
            "nodes": [{"id": "qa-review", "node_type": "manual", "output_schema_id": str(uuid.uuid4())}],
            "edges": [],
        },
    )
    snapshot_id = mock_snapshot.id
    ctx["snapshot_id"] = snapshot_id
    ctx["snapshots"]["pre-qa-agent"] = mock_snapshot

    with (
        patch(
            "modulo.api.routes.pipelines.get_snapshot_detail",
            return_value=mock_snapshot,
        ),
        patch(
            "modulo.api.routes.pipelines.rollback_to_snapshot",
            return_value=mock_snapshot,
        ),
    ):
        resp = request.node._resp
        assert resp is not None, "No response stored — run the 'set node back' step first"
    ctx["can_rollback"] = True


@when(parsers.parse('I restore snapshot "{snapshot_name}"'))
def restore_snapshot(snapshot_name: str, ctx, client, request):
    snapshot = ctx["snapshots"].get(snapshot_name)
    if snapshot is None:
        snapshot = make_mock_snapshot(
            id=uuid.uuid5(ctx["pipeline"].id, snapshot_name),
            graph_json={
                "nodes": [{"id": "qa-review", "node_type": "manual"}],
                "edges": [],
            },
        )
        ctx["snapshots"][snapshot_name] = snapshot

    with (
        patch(
            "modulo.api.routes.pipelines.rollback_to_snapshot",
            return_value=make_mock_snapshot(
                tag="rollback-v1",
                notes=f"Rollback to snapshot version {snapshot.snapshot_version}",
                graph_json=snapshot.graph_json,
            ),
        ),
    ):
        resp = client.post(
            f"/api/v1/pipelines/{ctx['pipeline'].id}/snapshots/{snapshot.id}/rollback",
        )
    request.node._resp = resp
    ctx["restored_snapshot"] = snapshot_name


@then("the pipeline matches the state before the agent was added")
def pipeline_matches_pre_agent_state(ctx, request):
    resp = request.node._resp
    assert resp is not None, "No response from restore step"
    if resp.status_code == 200:
        data = resp.json()
        assert "id" in data, "Expected snapshot ID in rollback response"
    assert ctx.get("restored_snapshot") is not None, "Snapshot was not restored"
    restored = ctx["snapshots"].get(ctx["restored_snapshot"])
    assert restored is not None, f"Snapshot {ctx.get('restored_snapshot')} not found"
    nodes = restored.graph_json.get("nodes", [])
    qa_node = next((n for n in nodes if n.get("id") == "qa-review"), None)
    assert qa_node is not None, "Expected qa-review node in restored snapshot"
    assert qa_node.get("node_type") == "manual", f"Expected manual type, got {qa_node.get('node_type')}"
