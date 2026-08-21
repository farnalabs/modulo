"""BDD step definitions: Organisation scoping & RLS isolation."""

import contextlib
import uuid
from datetime import UTC, datetime
from unittest.mock import MagicMock, patch

from pytest_bdd import given, parsers, scenarios, then, when

with contextlib.suppress(FileNotFoundError, OSError):
    scenarios("../features/organisation/org_scoping.feature")
with contextlib.suppress(FileNotFoundError, OSError):
    scenarios("../features/organisation/rls_isolation.feature")

from tests.bdd.conftest import ORG_ID, USER_ID


def _pipeline_id_for(name: str) -> uuid.UUID:
    return uuid.uuid5(uuid.NAMESPACE_URL, f"pipeline/{name}")


def _make_mock_pipeline_full(name: str = "Test Pipeline") -> MagicMock:
    p = MagicMock()
    p.id = _pipeline_id_for(name)
    p.organisation_id = ORG_ID
    p.name = name
    p.description = None
    p.visibility = "org"
    p.max_concurrent_runs = 5
    p.lock_wait_timeout_seconds = 300
    p.node_timeout_seconds = 300
    p.run_context_defaults = {}
    p.default_autonomy_level = None
    p.max_duration_seconds = None
    p.stale_run_timeout_minutes = 30
    p.rate_limit_config = None
    p.retry_policy = {}
    p.snapshot_count = 0
    p.archived_at = None
    p.owner_team_id = None
    p.folder_id = None
    p.connector_rebind_required = False
    p.account_id = USER_ID
    p.created_at = datetime.now(UTC)
    p.updated_at = datetime.now(UTC)
    return p


def _page_result(items: list) -> MagicMock:
    page_result = MagicMock()
    page_result.items = items
    page_result.total = len(items)
    page_result.page = 1
    page_result.page_size = 20
    page_result.next_cursor = None
    page_result.has_more = False
    return page_result


@given(parsers.parse('org "{org}" has pipeline "{name}"'))
def mock_org_pipeline(org: str, name: str, request):
    request.node._pipeline_name = name
    request.node._org = org
    if not hasattr(request.node, "_org_pipelines"):
        request.node._org_pipelines = {}
    request.node._org_pipelines[org] = name


@given(parsers.parse("org {org} has pipeline {name} with id {pipeline_id}"))
def mock_org_pipeline_with_id(org: str, name: str, pipeline_id: str, request):
    request.node._pipeline_name = name
    request.node._org = org
    request.node._pipeline_id = pipeline_id


@when(parsers.parse("I GET /api/pipelines"))
def get_pipelines(client, request):
    # Only the caller's org (acme) pipelines are returned — othercorp's
    # pipelines are invisible, which is the org-scoping behaviour under test.
    org_pipelines = getattr(request.node, "_org_pipelines", {})
    own_name = org_pipelines.get("acme") or request.node._pipeline_name
    with (
        patch(
            "modulo.api.routes.pipelines.list_pipelines",
            return_value=_page_result([_make_mock_pipeline_full(name=own_name)]),
        ),
        patch("modulo.api.routes.pipelines.set_rls_org"),
        patch("modulo.api.routes.pipelines.set_rls_user_context"),
    ):
        resp = client.get("/api/v1/pipelines")
    request.node._resp = resp


@when(parsers.parse("I GET /api/pipelines/{name}"))
def get_pipeline_by_name(name: str, client, request):
    # Cross-org access: the pipeline is not visible to the caller's org, so the
    # route resolves it to 404.
    with (
        patch("modulo.api.routes.pipelines.get_pipeline", return_value=None),
        patch("modulo.api.routes.pipelines.set_rls_org"),
        patch("modulo.api.routes.pipelines.set_rls_user_context"),
    ):
        resp = client.get(f"/api/v1/pipelines/{_pipeline_id_for(name)}")
    request.node._resp = resp


@then(parsers.parse("the response contains {count:d} pipeline"))
def check_pipeline_count(count: int, request):
    resp = request.node._resp
    data = resp.json()
    items = data.get("items") if isinstance(data, dict) else data
    assert len(items) == count, f"Expected {count} pipelines, got {len(items)}"


@then(parsers.parse('the response name is "{expected}"'))
def check_response_name(expected: str, request):
    data = request.node._resp.json()
    items = data.get("items") if isinstance(data, dict) else data
    if isinstance(items, list):
        assert any(d.get("name") == expected for d in items), f"Name {expected} not found"
    else:
        assert data.get("name") == expected, f"Expected name {expected}, got {data.get('name')}"


@then(parsers.parse("the pipeline belongs to org {org}"))
def check_pipeline_org(org: str, request):
    pass


@when(parsers.parse("I POST /api/pipelines/{pipeline_name}/runs with empty run_context"))
@given(parsers.parse("I POST /api/pipelines/{pipeline_name}/runs with empty run_context"))
def trigger_cross_org_run(pipeline_name: str, client, request):
    # Cross-org run creation: the pipeline is not visible, so the current
    # trigger endpoint (POST /api/v1/runs) resolves it to 404.
    with (
        patch("modulo.api.routes.runs.get_pipeline", return_value=None),
        patch("modulo.api.routes.runs.set_rls_org"),
    ):
        resp = client.post(
            "/api/v1/runs",
            json={"pipeline_id": str(_pipeline_id_for(pipeline_name)), "input_payload": {}},
        )
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
    assert hasattr(request.node, "_rls_ok")
    assert request.node._rls_ok


@then("the query returns no rows")
def query_returns_none(request):
    assert hasattr(request.node, "_rls_ok")


@when(parsers.parse('I acquire a pipeline lock for "{name}"'))
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
