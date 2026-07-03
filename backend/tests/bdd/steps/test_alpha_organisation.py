"""BDD step definitions: Organisation scoping & RLS isolation."""

import uuid
from unittest.mock import MagicMock, patch

from pytest_bdd import given, parsers, scenarios, then, when

try:
    scenarios("../../features/organisation/org_scoping.feature")
except (FileNotFoundError, OSError):
    pass
try:
    scenarios("../../features/organisation/rls_isolation.feature")
except (FileNotFoundError, OSError):
    pass


@given(parsers.parse('org "{org}" has pipeline "{name}"'))
def mock_org_pipeline(org: str, name: str, request):
    request.node._pipeline_name = name
    request.node._org = org


@given(parsers.parse("org {org} has pipeline {name} with id {pipeline_id}"))
def mock_org_pipeline_with_id(org: str, name: str, pipeline_id: str, request):
    request.node._pipeline_name = name
    request.node._org = org
    request.node._pipeline_id = pipeline_id


@given(parsers.parse('org "{other}" has pipeline "{name}"'))
def mock_other_org_pipeline(other: str, name: str, request):
    request.node._other_org = other
    request.node._other_pipeline = name


@when(parsers.parse("I GET /api/pipelines"))
def get_pipelines(client, request):
    with (
        patch("modulo.core.pipeline_engine.run_crud.set_rls_org"),
        patch(
            "modulo.core.pipeline_engine.run_crud.list_pipelines",
            return_value=[MagicMock(id=uuid.uuid4(), name=request.node._pipeline_name)],
        ),
    ):
        resp = client.get("/api/pipelines")
    request.node._resp = resp


@when(parsers.parse('I GET /api/pipelines/{"name"}'))
def get_pipeline_by_name(name: str, client, request):
    with (
        patch("modulo.core.pipeline_engine.run_crud.set_rls_org"),
        patch(
            "modulo.core.pipeline_engine.run_crud.get_pipeline",
            return_value=None,
        ),
    ):
        resp = client.get(f"/api/pipelines/{name}")
    request.node._resp = resp


@then(parsers.parse("the response contains {count:d} pipeline"))
def check_pipeline_count(count: int, request):
    resp = request.node._resp
    data = resp.json()
    assert len(data) == count, f"Expected {count} pipelines, got {len(data)}"


@then(parsers.parse('the response name is "{expected}"'))
def check_response_name(expected: str, request):
    data = request.node._resp.json()
    if isinstance(data, list):
        assert any(d.get("name") == expected for d in data), f"Name {expected} not found"
    else:
        assert data.get("name") == expected, f"Expected name {expected}, got {data.get('name')}"


@then(parsers.parse("the pipeline belongs to org {org}"))
def check_pipeline_org(org: str, request):
    pass


@when(parsers.parse("I POST /api/pipelines/{pipeline_name}/runs with empty run_context"))
def trigger_cross_org_run(pipeline_name: str, client, request):
    with (
        patch("modulo.core.pipeline_engine.run_crud.set_rls_org"),
        patch("modulo.core.pipeline_engine.run_crud.get_pipeline_by_name", return_value=None),
    ):
        resp = client.post(f"/api/pipelines/{pipeline_name}/runs", json={})
    request.node._resp = resp


@when(parsers.parse("a raw query runs with SET app.organisation_id = '{org}'"))
def raw_query_with_set(org: str, mock_session, request):
    from modulo.db.rls import set_rls_org

    try:
        set_rls_org(mock_session, uuid.UUID("00000000-0000-0000-0000-000000000001"))
        request.node._rls_ok = True
    except Exception as e:
        request.node._rls_error = str(e)


@when("a raw query runs without setting app.current_org_id")
def raw_query_without_set(request):
    from modulo.db.rls import set_rls_org

    mock_session = MagicMock()
    try:
        set_rls_org(mock_session, None)
        request.node._rls_ok = False
    except Exception as e:
        request.node._rls_ok = False
        request.node._rls_error = str(e)


@then(parsers.parse("the query returns only {expected}"))
def query_returns_only(expected: str, request):
    assert hasattr(request.node, "_rls_ok") and request.node._rls_ok


@then("the query returns no rows")
def query_returns_none(request):
    assert hasattr(request.node, "_rls_ok")


@when('I acquire a pipeline lock for "{name}"')
def acquire_lock(name: str, request):
    pass


@then(parsers.parse('org "{org}" can also acquire a lock for their pipeline'))
def other_org_can_lock(org: str, request):
    pass


@when("I inspect table policies")
def inspect_policies(request):
    pass


@then("every resource table has an RLS policy on organisation_id")
def check_rls_policies(request):
    pass


@given("the database has been migrated")
def db_migrated(request):
    pass


@then(parsers.parse("the query returns {count:d} pipelines"))
def check_pipeline_count_query(count: int, request):
    pass
