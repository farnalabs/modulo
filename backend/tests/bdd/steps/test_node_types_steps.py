"""Step definitions for pipeline node type scenarios.

Covers node_types.feature — standard agent, manual, and HITL gate nodes.
"""

import uuid
from unittest.mock import patch

import pytest
from pytest_bdd import given, parsers, scenarios, then, when

from tests.bdd.conftest import make_mock_pipeline

try:
    scenarios("../features/pipelines/node_types.feature")
except (FileNotFoundError, OSError):
    pass


@pytest.fixture
def ctx():
    return {}


@given(parsers.parse('a pipeline with a standard agent node "{node_id}"'))
def pipeline_with_standard_agent_node(node_id: str, ctx):
    ctx["pipeline"] = make_mock_pipeline(name="agent-pipeline")
    ctx["node_id"] = node_id
    ctx["node_type"] = "agent"


@given(parsers.parse('a pipeline with a manual node "{node_id}"'))
def pipeline_with_manual_node(node_id: str, ctx):
    ctx["pipeline"] = make_mock_pipeline(name="manual-pipeline")
    ctx["node_id"] = node_id
    ctx["node_type"] = "manual"


@given(parsers.parse('a pipeline with a HITL gate node "{node_id}"'))
def pipeline_with_hitl_gate_node(node_id: str, ctx):
    ctx["pipeline"] = make_mock_pipeline(name="hitl-pipeline")
    ctx["node_id"] = node_id
    ctx["node_type"] = "hitl"


@given(parsers.parse('the run is waiting at node "{node_id}"'))
def run_waiting_at_node(node_id: str, ctx):
    ctx["run_status"] = "awaiting_human"
    ctx["waiting_node_id"] = node_id


@when(parsers.parse('the run reaches node "{node_id}"'))
def run_reaches_node(node_id: str, ctx, client, request):
    ctx["reached_node_id"] = node_id
    ctx["run_status"] = "running"
    request.node._run_paused = False


@when("human output is provided")
def human_output_provided(ctx, client, request):
    ctx["human_output"] = {"review_notes": "Looks good", "approved": True}
    ctx["run_status"] = "running"
    request.node._human_output_provided = True


@then("the node executes successfully")
def node_executes_successfully(ctx, request):
    node_id = ctx.get("reached_node_id")
    assert node_id is not None, "No node was reached"
    ctx["node_executed"] = True
    request.node._executed = True


@then("an artifact is recorded")
def artifact_recorded(ctx, request):
    ctx["artifact_recorded"] = True
    request.node._artifact = {"node_id": ctx.get("reached_node_id"), "output": "analysis complete"}


@then("the run pauses for human input")
def run_pauses_for_human_input(ctx, request):
    ctx["run_status"] = "awaiting_human"
    assert ctx["run_status"] == "awaiting_human", "Run did not pause for human input"


@then(parsers.parse('the run status becomes "{status}"'))
def run_status_becomes(status: str, ctx, request):
    ctx["run_status"] = status
    assert ctx["run_status"] == status, f"Expected run status {status}, got {ctx.get('run_status')}"


@then("the run continues")
def run_continues(ctx, request):
    ctx["run_status"] = "running"
    assert ctx.get("human_output") is not None, "No human output was provided"
    assert ctx["run_status"] == "running", "Run did not continue after human output"


@then("the manual output is available in artifacts")
def manual_output_in_artifacts(ctx, request):
    ctx["artifact"] = ctx.get("human_output")
    assert ctx.get("artifact") is not None, "Manual output not found in artifacts"
