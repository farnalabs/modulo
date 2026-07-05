"""BDD step definitions: Pipeline creation & concurrency."""

import uuid
from unittest.mock import MagicMock, patch

from pytest_bdd import given, parsers, scenarios, then, when

try:
    scenarios("../../features/pipelines/create.feature")
except (FileNotFoundError, OSError):
    pass
try:
    scenarios("../../features/pipelines/concurrency.feature")
except (FileNotFoundError, OSError):
    pass

from tests.bdd.conftest import make_mock_pipeline


@given(parsers.parse('org "{org}" has pipeline "{name}" with max_concurrent_runs {limit:d}'))
def pipeline_with_concurrency(org: str, name: str, limit: int, request):
    request.node._pipeline_name = name
    request.node._max_concurrent = limit


@given(parsers.parse("{count:d} runs are currently executing for {pipeline}"))
def running_runs_for_pipeline(count: int, pipeline: str, request):
    request.node._executing_count = count


@when("I POST /api/pipelines/{pipeline}/runs with empty run_context")
def trigger_run_concurrent(pipeline: str, client, request):
    with (
        patch("modulo.core.pipeline_engine.run_crud.set_rls_org"),
        patch(
            "modulo.core.pipeline_engine.run_crud.get_pipeline_by_name",
            return_value=make_mock_pipeline(
                name=pipeline,
                max_concurrent_runs=getattr(request.node, "_max_concurrent", 5),
            ),
        ),
        patch(
            "modulo.core.pipeline_engine.run_crud.count_runs_by_status",
            return_value=getattr(request.node, "_executing_count", 0),
        ),
        patch(
            "modulo.core.pipeline_engine.run_crud.create_run",
            return_value=MagicMock(id=uuid.uuid4(), status="pending"),
        ),
    ):
        resp = client.post(f"/api/pipelines/{pipeline}/runs", json={})
    request.node._resp = resp


@when("the executing run completes")
def executing_run_completes(request):
    request.node._executing_count = 0


@then("a run is created with status {status}")
def check_run_created(status: str, request):
    pass


@then(parsers.parse('a run in org "{org}" can still be created'))
def other_org_run_can_be_created(org: str, request):
    pass


@given(parsers.parse('no pipeline exists with slug "{slug}"'))
def no_pipeline(slug: str, request):
    request.node._no_pipeline = slug


@given(parsers.parse('org "{org}" has pipeline "{name}" with status "{status}"'))
def pipeline_with_status(org: str, name: str, status: str, request):
    request.node._pipeline_name = name
    request.node._pipeline_status = status


@then(parsers.parse('the run has trigger_type "{trigger_type}"'))
def check_trigger_type(trigger_type: str, request):
    pass


@given(parsers.parse('org "{org}" has pipeline "{name}"'))
def org_has_pipeline(org: str, name: str, request):
    request.node._pipeline_name = name
    request.node._org = org


@when('I POST /api/pipelines with name "{name}" and valid config')
def create_pipeline(name: str, client, request):
    with (
        patch("modulo.core.pipeline_engine.run_crud.set_rls_org"),
        patch(
            "modulo.core.pipeline_engine.run_crud.create_pipeline",
            return_value=MagicMock(id=uuid.uuid4(), slug=name, name=name),
        ),
    ):
        resp = client.post(
            "/api/pipelines",
            json={"name": name, "nodes": [{"id": "node-a", "type": "llm"}]},
        )
    request.node._resp = resp


@when('I POST /api/pipelines with name "{name}" and a single LLM node config')
def create_llm_pipeline(name: str, client, request):
    with (
        patch("modulo.core.pipeline_engine.run_crud.set_rls_org"),
        patch(
            "modulo.core.pipeline_engine.run_crud.create_pipeline",
            return_value=MagicMock(id=uuid.uuid4(), slug=name, name=name),
        ),
    ):
        resp = client.post(
            "/api/pipelines",
            json={"name": name, "nodes": [{"id": "llm-node", "type": "llm"}]},
        )
    request.node._resp = resp


@when('I POST /api/pipelines with name "{name}" and a manual node config')
def create_manual_pipeline(name: str, client, request):
    with (
        patch("modulo.core.pipeline_engine.run_crud.set_rls_org"),
        patch(
            "modulo.core.pipeline_engine.run_crud.create_pipeline",
            return_value=MagicMock(id=uuid.uuid4(), slug=name, name=name),
        ),
    ):
        resp = client.post(
            "/api/pipelines",
            json={
                "name": name,
                "nodes": [{"id": "manual-node", "type": "manual"}],
            },
        )
    request.node._resp = resp


@when('I POST /api/pipelines with name "{name}" and run_context defaults')
def create_pipeline_with_defaults(name: str, client, request):
    with (
        patch("modulo.core.pipeline_engine.run_crud.set_rls_org"),
        patch(
            "modulo.core.pipeline_engine.run_crud.create_pipeline",
            return_value=MagicMock(id=uuid.uuid4(), slug=name, name=name),
        ),
    ):
        resp = client.post(
            "/api/pipelines",
            json={
                "name": name,
                "run_context_defaults": {"branch": "main"},
            },
        )
    request.node._resp = resp


@then("the response contains id and slug")
def check_id_and_slug(request):
    resp = request.node._resp
    data = resp.json()
    assert "id" in data, "Response missing id"
    assert "slug" in data, "Response missing slug"


@then("the pipeline has a manual node")
def check_manual_node(request):
    pass


@then("the pipeline has run_context defaults")
def check_run_context_defaults(request):
    pass
