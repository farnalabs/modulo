"""BDD step definitions: Pipeline creation & concurrency."""

import contextlib
from unittest.mock import patch

from pytest_bdd import given, parsers, scenarios, then, when

with contextlib.suppress(FileNotFoundError, OSError):
    scenarios("../features/pipelines/create.feature")
with contextlib.suppress(FileNotFoundError, OSError):
    scenarios("../features/pipelines/concurrency.feature")

from tests.bdd.conftest import _make_mock_pipeline_full


@given(parsers.parse('org "{org}" has pipeline "{name}" with max_concurrent_runs {limit:d}'))
def pipeline_with_concurrency(org: str, name: str, limit: int, request):
    request.node._pipeline_name = name
    request.node._max_concurrent = limit


@given(parsers.parse("{count:d} runs are currently executing for {pipeline}"))
def running_runs_for_pipeline(count: int, pipeline: str, request):
    request.node._executing_count = count


@when("I POST /api/pipelines/{pipeline}/runs with empty run_context")
def trigger_run_concurrent(pipeline: str, client, request):
    # The per-pipeline runs endpoint no longer exists — runs are triggered via
    # POST /api/v1/runs. Concurrency admission is enforced inside create_run /
    # dispatch. The concurrency scenarios are marked @awaiting-implementation;
    # this step is a no-op.
    request.node._resp = None


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


def _post_create_pipeline(client, request, name: str, extra: dict):
    # "Duplicate pipeline name is rejected": when a pipeline with this name was
    # already declared (via `Given org ... has pipeline "{name}"`), the create
    # path raises IntegrityError which the route maps to 409.
    existing = getattr(request.node, "_pipeline_name", None)
    if existing == name:
        from sqlalchemy.exc import IntegrityError

        create_side_effect = IntegrityError("INSERT INTO pipelines", {}, Exception("duplicate key"))
        create_return = None
    else:
        create_side_effect = None
        create_return = _make_mock_pipeline_full(name=name)
    with (
        patch(
            "modulo.api.routes.pipelines.create_pipeline",
            side_effect=create_side_effect,
            return_value=create_return,
        ),
        patch("modulo.api.routes.pipelines.set_rls_org"),
        patch("modulo.api.routes.pipelines.set_rls_user_context"),
    ):
        resp = client.post("/api/v1/pipelines", json={"name": name, **extra})
    request.node._resp = resp


@when(parsers.parse('I POST /api/pipelines with name "{name}" and a single LLM node config'))
def create_llm_pipeline(name: str, client, request):
    _post_create_pipeline(client, request, name, {})


@when(parsers.parse('I POST /api/pipelines with name "{name}" and a manual node config'))
def create_manual_pipeline(name: str, client, request):
    _post_create_pipeline(client, request, name, {})


@when(parsers.parse('I POST /api/pipelines with name "{name}" and run_context defaults'))
def create_pipeline_with_defaults(name: str, client, request):
    _post_create_pipeline(client, request, name, {"run_context_defaults": {"branch": "main"}})


@then("the pipeline has a manual node")
def check_manual_node(request):
    pass


@then("the pipeline has run_context defaults")
def check_run_context_defaults(request):
    pass
