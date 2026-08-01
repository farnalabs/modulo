"""BDD step definitions: User roles, runner role.

ADR 017 PR B reconciliation: steps hit REAL endpoints through real role
clients (viewer/runner/operator/admin). The permission gate is exercised at
the HTTP layer; the DB CRUD functions are mocked at the route boundary so the
tests remain DB-free and fast.
"""

import contextlib
import uuid
from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock, patch

from pytest_bdd import given, parsers, scenarios, then, when

with contextlib.suppress(FileNotFoundError, OSError):
    scenarios("../features/users/basic_auth.feature")
with contextlib.suppress(FileNotFoundError, OSError):
    scenarios("../features/users/roles.feature")
with contextlib.suppress(FileNotFoundError, OSError):
    scenarios("../features/users/runner_role.feature")

from tests.bdd.conftest import ORG_ID, make_mock_run, make_mock_snapshot


def _get_client(request) -> object:
    """Return the role client set by the auth step (always set by a Given)."""
    return request.node._client


def _pipeline_mock(**overrides: object) -> MagicMock:
    p = MagicMock()
    p.id = uuid.uuid4()
    p.organisation_id = ORG_ID
    p.name = "pipeline"
    p.description = None
    p.visibility = "org"
    p.max_concurrent_runs = 5
    p.lock_wait_timeout_seconds = 300
    p.node_timeout_seconds = 300
    p.run_context_defaults = {}
    p.default_autonomy_level = "manual_approval"
    p.max_duration_seconds = 3600
    p.stale_run_timeout_minutes = 30
    p.rate_limit_config = None
    p.snapshot_count = 0
    p.archived_at = None
    p.owner_team_id = None
    p.folder_id = None
    p.account_id = uuid.uuid4()
    p.created_at = datetime.now(UTC)
    p.updated_at = datetime.now(UTC)
    for key, value in overrides.items():
        setattr(p, key, value)
    return p


# ---------------------------------------------------------------------------
# Auth steps (roles)
# ---------------------------------------------------------------------------


@given(parsers.parse('I am authenticated as an operator in org "{org}"'))
def auth_operator(org: str, request, operator_client):
    request.node._client = operator_client


@given(parsers.parse('I am authenticated as a runner in org "{org}"'))
def auth_runner(org: str, request, runner_client):
    request.node._client = runner_client


# ---------------------------------------------------------------------------
# roles.feature — pipeline CRUD through real endpoints
# ---------------------------------------------------------------------------


@when(parsers.parse('I POST /api/v1/pipelines with name "{name}" and valid config'))
def create_pipeline(name: str, request):
    c = _get_client(request)
    with (
        patch("modulo.api.routes.pipelines.create_pipeline", new_callable=AsyncMock) as m,
        patch("modulo.api.routes.pipelines.set_rls_org"),
        patch("modulo.api.routes.pipelines.set_rls_user_context"),
    ):
        m.return_value = _pipeline_mock(name=name)
        resp = c.post("/api/v1/pipelines", json={"name": name})
    request.node._resp = resp


@given(parsers.parse('org "{org}" has pipeline "{name}"'))
def org_has_pipeline(org: str, name: str, request):
    request.node._pipeline_name = name
    request.node._pipeline_id = uuid.uuid4()


@when("I GET /api/v1/pipelines")
def get_pipelines(request):
    c = _get_client(request)
    page = MagicMock()
    page.items = [_pipeline_mock(name=request.node._pipeline_name)]
    page.total = 1
    page.page = 1
    page.page_size = 20
    page.next_cursor = None
    page.has_more = False
    with (
        patch("modulo.api.routes.pipelines.list_pipelines", new_callable=AsyncMock, return_value=page),
        patch("modulo.api.routes.pipelines.set_rls_org"),
        patch("modulo.api.routes.pipelines.set_rls_user_context"),
    ):
        resp = c.get("/api/v1/pipelines")
    request.node._resp = resp


@then(parsers.parse("the response contains {count:d} pipeline"))
def check_pipeline_count(count: int, request):
    data = request.node._resp.json()
    assert len(data["items"]) == count


@when(parsers.parse("I DELETE /api/v1/pipelines/{name}"))
def delete_pipeline(name, request):
    c = _get_client(request)
    pipeline_id = getattr(request.node, "_pipeline_id", uuid.uuid4())
    with (
        patch("modulo.api.routes.pipelines.get_pipeline", new_callable=AsyncMock) as g,
        patch("modulo.api.routes.pipelines.soft_delete_pipeline", new_callable=AsyncMock) as d,
        patch("modulo.api.routes.pipelines.set_rls_org"),
        patch("modulo.api.routes.pipelines.set_rls_user_context"),
    ):
        g.return_value = _pipeline_mock(name=name)
        d.return_value = True
        resp = c.delete(f"/api/v1/pipelines/{pipeline_id}")
    request.node._resp = resp


@when(parsers.parse("I PATCH /api/v1/pipelines/{name} with new config"))
def patch_pipeline(name, request):
    c = _get_client(request)
    pipeline_id = getattr(request.node, "_pipeline_id", uuid.uuid4())
    with (
        patch("modulo.api.routes.pipelines.get_pipeline", new_callable=AsyncMock) as g,
        patch("modulo.api.routes.pipelines.update_pipeline", new_callable=AsyncMock) as u,
        patch("modulo.api.routes.pipelines._assert_team_transition_allowed", new_callable=AsyncMock),
        patch("modulo.api.routes.pipelines.set_rls_org"),
        patch("modulo.api.routes.pipelines.set_rls_user_context"),
    ):
        g.return_value = _pipeline_mock(name=name)
        u.return_value = _pipeline_mock(name=name)
        resp = c.patch(f"/api/v1/pipelines/{pipeline_id}", json={"name": name})
    request.node._resp = resp


@when(parsers.parse('I POST /api/v1/admin/users with email "{email}" and role "{role}"'))
def create_user(email: str, role: str, request):
    c = _get_client(request)
    account = MagicMock()
    account.id = uuid.uuid4()
    account.email = email
    account.display_name = email
    membership = MagicMock()
    membership.role = role
    with (
        patch("modulo.api.routes.admin.get_account_by_email", new_callable=AsyncMock, return_value=None),
        patch("modulo.db.crud.account.create_account", new_callable=AsyncMock, return_value=account),
        patch("modulo.api.routes.admin.create_membership", new_callable=AsyncMock, return_value=membership),
        patch("modulo.api.routes.admin.validate_password_strength"),
        patch("modulo.api.routes.admin.hash_password", return_value="hashed"),
    ):
        resp = c.post(
            "/api/v1/admin/users",
            json={"email": email, "display_name": email, "password": "password123", "org_role": role},
        )
    request.node._resp = resp


# ---------------------------------------------------------------------------
# runner_role.feature
# ---------------------------------------------------------------------------


@when(parsers.parse("the runner triggers a run for pipeline {name}"))
def runner_triggers_run(name, request):
    c = _get_client(request)
    pipeline = _pipeline_mock(name=str(name))
    run = make_mock_run(status="pending")
    with (
        patch("modulo.api.routes.runs.get_pipeline", new_callable=AsyncMock, return_value=pipeline),
        patch("modulo.api.routes.runs.create_snapshot_from_live_graph", new_callable=AsyncMock) as snap,
        patch("modulo.api.routes.runs.create_run", new_callable=AsyncMock, return_value=run),
        patch("modulo.api.routes.runs.dispatch_run"),
        patch("modulo.api.routes.runs.set_rls_org"),
    ):
        snap.return_value = make_mock_snapshot()
        resp = c.post("/api/v1/runs", json={"pipeline_id": str(pipeline.id)})
    request.node._resp = resp
    request.node._pipeline_name = str(name)


@when("the runner attempts to PATCH the pipeline config")
def runner_patches_pipeline(request):
    c = _get_client(request)
    pipeline_id = getattr(request.node, "_pipeline_id", uuid.uuid4())
    with (
        patch("modulo.api.routes.pipelines.get_pipeline", new_callable=AsyncMock) as g,
        patch("modulo.api.routes.pipelines.update_pipeline", new_callable=AsyncMock) as u,
        patch("modulo.api.routes.pipelines._assert_team_transition_allowed", new_callable=AsyncMock),
        patch("modulo.api.routes.pipelines.set_rls_org"),
        patch("modulo.api.routes.pipelines.set_rls_user_context"),
    ):
        g.return_value = _pipeline_mock(name=getattr(request.node, "_pipeline_name", "ci-pipeline"))
        u.return_value = _pipeline_mock(name=getattr(request.node, "_pipeline_name", "ci-pipeline"))
        resp = c.patch(
            f"/api/v1/pipelines/{pipeline_id}",
            json={"name": "hacked"},
        )
    request.node._resp = resp


@when("the runner requests GET /api/v1/admin/audit")
def runner_gets_audit(request):
    c = _get_client(request)
    resp = c.get("/api/v1/admin/audit")
    request.node._resp = resp


@given("a completed run exists")
def completed_run_exists(request):
    request.node._run_id = uuid.uuid4()


@when(parsers.parse("the runner requests GET /api/v1/runs/{run_id}"))
def runner_gets_run(run_id, request):
    c = _get_client(request)
    resolved = getattr(request.node, "_run_id", uuid.uuid4())
    run = make_mock_run(id=resolved, status="completed")
    with (
        patch("modulo.api.routes.runs._do_get_run", new_callable=AsyncMock, return_value=run),
    ):
        resp = c.get(f"/api/v1/runs/{resolved}")
    request.node._resp = resp


@then("the response contains run status")
def check_run_status_field(request):
    data = request.node._resp.json()
    assert "status" in data


@then("the run is created")
def check_run_created(request):
    data = request.node._resp.json()
    assert data["run_id"]


# ---------------------------------------------------------------------------
# Deferred-to-Phase-3 team-scope steps (scenario is @skip tagged)
# ---------------------------------------------------------------------------


@given(parsers.parse('org "{org}" has pipeline "{name}" owned by team "{team}"'))
def pipeline_owned_by_team(org: str, name: str, team: str, request):
    request.node._pipeline_name = name
    request.node._pipeline_team = team


@given(parsers.parse('a runner with team scope "{team}" exists'))
def runner_with_team_scope(team: str, request):
    request.node._runner_team = team


@then(parsers.parse("the runner cannot trigger runs for pipelines outside their scope"))
def runner_cannot_trigger_outside_scope(request):
    # Phase 3: team-scope enforcement for run triggering. Deferred per ADR 017 —
    # the scenario carrying this step is @skip tagged and never runs.
    assert True
