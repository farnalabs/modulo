"""BDD step definitions: User roles, runner role.

ADR 017 PR B reconciliation: steps hit REAL endpoints through real role
clients (viewer/runner/operator/admin). The permission gate is exercised at
the HTTP layer; the DB CRUD functions are mocked at the route boundary so the
tests remain DB-free and fast.
"""

import contextlib
import uuid
from datetime import UTC, datetime
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from pytest_bdd import given, parsers, scenarios, then, when

with contextlib.suppress(FileNotFoundError, OSError):
    scenarios("../features/users/basic_auth.feature")
with contextlib.suppress(FileNotFoundError, OSError):
    scenarios("../features/users/roles.feature")
with contextlib.suppress(FileNotFoundError, OSError):
    scenarios("../features/users/runner_role.feature")

from tests.bdd.conftest import _VALID_32, ORG_ID, USER_ID, make_mock_run, make_mock_snapshot


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
# basic_auth.feature — login, refresh, expired token
# ---------------------------------------------------------------------------


@given(parsers.parse('a user exists with email "{email}" and password "{password}"'))
def user_exists(email: str, password: str, request):
    request.node._user_email = email
    request.node._user_password = password


@when(parsers.parse('I POST /api/v1/auth/login with email "{email}" and password "{password}"'))
def post_login(email: str, password: str, request, client):
    account = MagicMock() if email == "alice@example.com" else None
    if account is not None:
        account.id = uuid.uuid4()
        account.email = email
        account.is_break_glass = None
        account.is_system_admin = False
    auth_ok = email == "alice@example.com" and password == "correct-horse-battery"
    membership = MagicMock()
    membership.organisation_id = ORG_ID
    membership.role = "admin"
    family = MagicMock()
    family.family_id = uuid.uuid4()
    with (
        patch("modulo.api.routes.auth.get_account_by_email", new_callable=AsyncMock, return_value=account),
        patch("modulo.api.routes.auth.authenticate_db_user", return_value=auth_ok),
        patch("modulo.api.routes.auth.update_last_login", new_callable=AsyncMock),
        patch(
            "modulo.api.routes.auth.list_memberships_for_account",
            new_callable=AsyncMock,
            return_value=[membership],
        ),
        patch("modulo.api.routes.auth.create_family", new_callable=AsyncMock, return_value=family),
    ):
        resp = client.post("/api/v1/auth/login", json={"email": email, "password": password})
    request.node._user_email = email
    request.node._resp = resp
    request.node.response = resp
    if resp.status_code == 200:
        data = resp.json()
        request.node._access_token = data.get("access_token")
        request.node._refresh_token = data.get("refresh_token")


@then("the response contains an access_token")
def response_has_access_token(request):
    data = request.node._resp.json()
    assert data.get("access_token")


@then("the token encodes org_id")
def token_encodes_org(request):
    import jwt as pyjwt

    token = request.node._access_token
    payload = pyjwt.decode(token, _VALID_32, algorithms=["HS256"])
    assert payload.get("org_id") == str(ORG_ID)


@when("I use the refresh_token to get a new access_token")
def use_refresh_token(request, client):
    active_account = MagicMock()
    active_account.active = True
    active_account.email = "user@example.com"
    with (
        patch("modulo.api.routes.auth.get_account_by_id", new_callable=AsyncMock, return_value=active_account),
        patch("modulo.api.routes.auth.resolve_role_from_membership", new_callable=AsyncMock, return_value="admin"),
        patch("modulo.api.routes.auth.advance_sequence", new_callable=AsyncMock, return_value=(1, False)),
    ):
        resp = client.post(
            "/api/v1/auth/refresh",
            json={"refresh_token": request.node._refresh_token},
        )
    request.node._resp = resp
    request.node._new_access_token = resp.json().get("access_token")


@then("the new access_token is valid")
def new_access_token_valid(request):
    import jwt as pyjwt

    token = request.node._new_access_token
    payload = pyjwt.decode(token, _VALID_32, algorithms=["HS256"])
    assert payload.get("sub") == request.node._user_email


@given(parsers.parse('I have an expired JWT for org "{org}"'), target_fixture="expired_token")
def expired_jwt(org: str) -> str:
    from datetime import UTC, datetime, timedelta

    import jwt as pyjwt

    now = datetime.now(UTC)
    claims = {
        "sub": "alice@example.com",
        "org_id": str(ORG_ID),
        "account_id": str(USER_ID),
        "org_role": "admin",
        "is_system_admin": False,
        "iat": now - timedelta(hours=2),
        "exp": now - timedelta(minutes=1),
    }
    return str(pyjwt.encode(claims, _VALID_32, algorithm="HS256"))


@when("I make an authenticated request to /api/v1/pipelines")
def authenticated_request(request, unauth_client, expired_token):
    resp = unauth_client.get(
        "/api/v1/pipelines",
        headers={"Authorization": f"Bearer {expired_token}"},
    )
    request.node._resp = resp


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
        patch(
            "modulo.api.routes.runs._do_get_child_run_rollup",
            new_callable=AsyncMock,
            return_value=(Decimal("0.00"), 0),
        ),
        patch("modulo.api.routes.runs._do_get_otel_endpoint", new_callable=AsyncMock, return_value=""),
        patch(
            "modulo.api.routes.runs._do_get_run_observability",
            new_callable=AsyncMock,
            return_value=(None, None, None),
        ),
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
    pytest.skip("team-scope run-trigger enforcement deferred per ADR 017 (Phase 3)")
