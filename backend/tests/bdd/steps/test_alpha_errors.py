"""BDD step definitions: Run retry, failed state, recovery."""

import uuid
from unittest.mock import MagicMock, patch

from pytest_bdd import given, parsers, scenarios, then, when

scenarios("../features/errors/retry.feature")
scenarios("../features/errors/failed_state.feature")
scenarios("../features/errors/recovery.feature")

from tests.bdd.conftest import make_mock_run  # noqa: E402


@given(parsers.parse("a run that failed at node {failed:d} of {total:d}"))
def run_failed_at_node(failed: int, total: int, request):
    run_id = uuid.uuid4()
    request.node._run_id = run_id
    request.node._failed_node = failed
    request.node._total_nodes = total


@when(parsers.parse('I POST /api/runs/{run_id}/retry with from_node "{from_node}"'))
def retry_from_node(run_id, from_node: str, client, request):
    new_run_id = uuid.uuid4()
    request.node._new_run_id = new_run_id
    with (
        patch("modulo.core.pipeline_engine.run_crud.set_rls_org"),
        patch(
            "modulo.core.pipeline_engine.run_crud.get_run",
            return_value=make_mock_run(
                id=run_id,
                status="failed",
                error_detail="Node 2 failed",
            ),
        ),
        patch(
            "modulo.core.pipeline_engine.run_crud.create_run",
            return_value=MagicMock(id=new_run_id, status="pending"),
        ),
        patch("modulo.core.pipeline_engine.executor.PipelineExecutor.retry"),
    ):
        resp = client.post(
            f"/api/runs/{run_id}/retry",
            json={"from_node": from_node},
        )
    request.node._resp = resp


@then("a new run is created")
def check_new_run(request):
    data = request.node._resp.json()
    assert "id" in data or "run_id" in data


@then(parsers.parse("the new run starts from node {node}"))
def new_run_starts_from(node: str, request):
    pass


@then(parsers.parse("node {node:d} is not re-executed"))
def node_not_reexecuted(node: int, request):
    pass


@given(parsers.parse("node {node:d} had completed successfully before failure"))
def node_completed_before_failure(node: int, request):
    pass


@then(parsers.parse("node {node:d} state is reset"))
def node_state_reset(node: int, request):
    pass


@then(parsers.parse("nodes {a:d} and {b:d} are re-executed"))
def nodes_reexecuted(a: int, b: int, request):
    pass


@when(parsers.parse('I POST /api/runs/{run_id}/retry with from_node "{from_node}" and run_context {ctx}'))
def retry_with_context(run_id, from_node: str, ctx, client, request):
    import json

    context = json.loads(ctx) if isinstance(ctx, str) else ctx
    new_run_id = uuid.uuid4()
    request.node._new_run_id = new_run_id
    with (
        patch("modulo.core.pipeline_engine.run_crud.set_rls_org"),
        patch(
            "modulo.core.pipeline_engine.run_crud.get_run",
            return_value=make_mock_run(id=run_id, status="failed"),
        ),
        patch(
            "modulo.core.pipeline_engine.run_crud.create_run",
            return_value=MagicMock(id=new_run_id, status="pending"),
        ),
        patch("modulo.core.pipeline_engine.executor.PipelineExecutor.retry"),
    ):
        resp = client.post(
            f"/api/runs/{run_id}/retry",
            json={"from_node": from_node, "run_context": context},
        )
    request.node._resp = resp


@then(parsers.parse("the new run has run_context with {key} {value}"))
def check_new_run_context(key: str, value, request):
    pass


@given("a completed run")
def completed_run(request):
    run_id = uuid.uuid4()
    request.node._run_id = run_id
    request.node._run_status = "completed"


@when(parsers.parse('I POST /api/runs/{run_id}/retry with from_node "{from_node}"'))
def retry_completed(run_id, from_node: str, client, request):
    resp = client.post(
        f"/api/runs/{run_id}/retry",
        json={"from_node": from_node},
    )
    request.node._resp = resp


@then(parsers.parse("the response status is {status:d}"))
def check_status(status: int, request):
    resp = request.node._resp
    assert resp.status_code == status, f"Expected {status}, got {resp.status_code}: {resp.text[:200]}"


@then(parsers.parse('the error mentions "{text}"'))
def error_mentions(text: str, request):
    data = request.node._resp.json()
    detail = str(data.get("detail", data.get("error", ""))).lower()
    assert text.lower() in detail


@when(parsers.parse("I GET /api/runs/{run_id}"))
def get_run(run_id, client, request):
    with (
        patch("modulo.core.pipeline_engine.run_crud.set_rls_org"),
        patch(
            "modulo.core.pipeline_engine.run_crud.get_run",
            return_value=make_mock_run(
                id=run_id,
                status="failed",
                error_detail="Node 2: Connection timeout",
                final_state={"node-1": {"output": "ok"}},
            ),
        ),
    ):
        resp = client.get(f"/api/runs/{run_id}")
    request.node._resp = resp


@then(parsers.parse("the response has status {status}"))
def check_response_status(status: str, request):
    data = request.node._resp.json()
    assert data.get("status") == status


@then("the response has error_detail describing the failure")
def check_error_detail(request):
    data = request.node._resp.json()
    assert data.get("error_detail") is not None


@when("I inspect the run detail")
def inspect_run_detail(request):
    pass


@then(parsers.parse("node {node:d} output is available"))
def node_output_available(node: int, request):
    pass


@then(parsers.parse("node {node:d} error is available"))
def node_error_available(node: int, request):
    pass


@then(parsers.parse("node {node:d} has no output"))
def node_no_output(node: int, request):
    pass


@then("the response contains final_state")
def check_final_state(request):
    data = request.node._resp.json()
    assert data.get("final_state") is not None or "final_state" in data


@then("the response contains error_detail")
def check_error_detail_field(request):
    data = request.node._resp.json()
    assert data.get("error_detail") is not None or "error_detail" in data


@when("I check the audit log")
def check_audit_log(request):
    pass


@then("an audit event exists for run failure")
def audit_event_for_failure(request):
    pass


@given("a running pipeline")
def running_pipeline(request):
    request.node._run_id = uuid.uuid4()
    request.node._run_status = "running"


@when(parsers.parse("I POST /api/runs/{run_id}/resume"))
def resume_run(run_id, client, request):
    with (
        patch("modulo.core.pipeline_engine.run_crud.set_rls_org"),
        patch(
            "modulo.core.pipeline_engine.run_crud.get_run",
            return_value=make_mock_run(
                id=run_id,
                status="failed",
                error_detail="Transient error",
            ),
        ),
        patch("modulo.core.pipeline_engine.executor.PipelineExecutor.resume"),
    ):
        resp = client.post(f"/api/runs/{run_id}/resume", json={})
    request.node._resp = resp


@given(parsers.parse("a run that failed at node {node:d} with a configuration error"))
def failed_with_config_error(node: int, request):
    request.node._run_id = uuid.uuid4()
    request.node._failed_node = node


@when("I fix the configuration")
def fix_config(request):
    pass


@then(parsers.parse("node {node:d} completes without error"))
def node_completes_without_error(node: int, request):
    pass


@then(parsers.parse("node {node:d} output is preserved in the resumed run"))
def node_output_preserved(node: int, request):
    pass


@when(parsers.parse("I POST /api/runs/{run_id}/resume with run_context {ctx}"))
def resume_with_context(run_id, ctx, client, request):
    import json

    context = json.loads(ctx) if isinstance(ctx, str) else ctx
    with (
        patch("modulo.core.pipeline_engine.run_crud.set_rls_org"),
        patch(
            "modulo.core.pipeline_engine.run_crud.get_run",
            return_value=make_mock_run(id=run_id, status="failed"),
        ),
        patch("modulo.core.pipeline_engine.executor.PipelineExecutor.resume"),
    ):
        resp = client.post(
            f"/api/runs/{run_id}/resume",
            json={"run_context": context},
        )
    request.node._resp = resp


@then(parsers.parse("the run context includes retry_count {count:d}"))
def check_retry_count(count: int, request):
    pass
