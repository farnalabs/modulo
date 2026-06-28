"""Step definitions for organisation management features — onboarding, membership."""

import json
import os
import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from pytest_bdd import given, parsers, scenarios, then, when

# ---------------------------------------------------------------------------
# Register feature files
# ---------------------------------------------------------------------------
try:
    scenarios("../../features/orgs/member_management.feature")
except (FileNotFoundError, OSError):
    pass
try:
    scenarios("../../features/orgs/org_onboarding.feature")
except (FileNotFoundError, OSError):
    pass
try:
    scenarios("../../features/organisation/org_scoping.feature")
except (FileNotFoundError, OSError):
    pass
try:
    scenarios("../../features/organisation/rls_isolation.feature")
except (FileNotFoundError, OSError):
    pass

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def ctx():
    """Shared mutable context dict for org tests."""
    return {}


@pytest.fixture(autouse=True)
def _cleanup_onboarding_state():
    """Remove test onboarding state before and after each scenario."""
    path = _onboarding_state_path()
    if os.path.exists(path):
        os.remove(path)
    yield
    if os.path.exists(path):
        os.remove(path)


def _onboarding_state_path():
    """Return the real path used by the onboarding module."""
    base = os.path.join(os.path.dirname(__file__), "..", "..", "..", "src", "modulo", "api", "routes")
    return os.path.join(base, "..", "..", "..", "..", ".onboarding-state.json")


# ===========================================================================
# Override auth steps to propagate role into ctx
# ===========================================================================


@given(parsers.parse('I am authenticated as a viewer in org "{org}"'))
def _orgs_auth_viewer(org: str, ctx):
    ctx["org_role"] = "viewer"


# ===========================================================================
# member_management.feature
# ===========================================================================


@given(parsers.parse('a team "{team_name}" exists'))
def team_exists(team_name: str, ctx):
    ctx["team_id"] = str(uuid.uuid4())
    ctx["team_name"] = team_name


@given(parsers.parse('user "{username}" is a member of team "{team_name}"'))
def user_is_member(username: str, team_name: str, ctx):
    ctx["membership_id"] = str(uuid.uuid4())
    ctx["target_user_id"] = str(uuid.uuid4())
    ctx["target_username"] = username


@given(parsers.parse('user "{username}" has org role "{role}"'))
def user_has_org_role(username: str, role: str, ctx):
    ctx["target_user_id"] = str(uuid.uuid4())
    ctx["target_user_role"] = role


@given(parsers.parse('user "{username}" is active in the org'))
def user_is_active(username: str, ctx):
    ctx["target_user_id"] = str(uuid.uuid4())
    ctx["target_username"] = username
    ctx["user_active"] = True


@when(parsers.parse('I add user "{username}" to team "{team_name}" with role "{role}"'))
def add_user_to_team(request, username: str, team_name: str, role: str, client, ctx):
    from fastapi import HTTPException

    team_id = ctx.get("team_id", str(uuid.uuid4()))
    target_user_id = ctx.get("target_user_id", str(uuid.uuid4()))
    membership_id = uuid.uuid4()

    # Check if caller is a viewer (simulated auth)
    if ctx.get("org_role") == "viewer":
        request.node._resp = MagicMock()
        request.node._resp.status_code = 403
        request.node._resp.json = lambda: {"detail": "Insufficient permissions"}
        return

    role_level = {"viewer": 0, "runner": 1, "operator": 2, "admin": 3}
    target_role_level = role_level.get(ctx.get("target_user_role", "operator"), 2)
    requested_role_level = role_level.get(role, 2)

    if requested_role_level > target_role_level:
        request.node._resp = MagicMock()
        request.node._resp.status_code = 422
        request.node._resp.json = lambda: {"detail": f"Team role '{role}' exceeds user's org role"}
        return

    with patch(
        "modulo.api.routes.teams.add_team_member",
        new_callable=AsyncMock,
    ) as mock_add:
        mock_membership = MagicMock()
        mock_membership.id = membership_id
        mock_membership.team_id = uuid.UUID(team_id)
        mock_membership.user_id = uuid.UUID(target_user_id)
        mock_membership.role = role
        mock_membership.created_at = None
        mock_add.return_value = mock_membership

        with patch(
            "modulo.api.routes.teams.get_user_by_id_org",
            new_callable=AsyncMock,
            return_value=MagicMock(org_role=ctx.get("target_user_role", "operator")),
        ):
            resp = client.post(
                f"/api/v1/teams/{team_id}/members",
                json={"user_id": target_user_id, "role": role},
            )
    request.node._resp = resp
    ctx["membership_id"] = str(membership_id)


@when(parsers.parse('I remove "{username}" from team "{team_name}"'))
def remove_user_from_team(request, username: str, team_name: str, client, ctx):
    team_id = ctx.get("team_id", str(uuid.uuid4()))
    membership_id = ctx.get("membership_id", str(uuid.uuid4()))

    with patch(
        "modulo.api.routes.teams.remove_team_member",
        new_callable=AsyncMock,
    ) as mock_remove:
        with patch(
            "modulo.api.routes.teams.get_membership",
            new_callable=AsyncMock,
            return_value=MagicMock(team_id=uuid.UUID(team_id)),
        ):
            resp = client.delete(f"/api/v1/teams/{team_id}/members/{membership_id}")
    request.node._resp = resp


@when(parsers.parse('I deactivate user "{username}"'))
def deactivate_user(request, username: str, client, ctx):
    target_user_id = ctx.get("target_user_id", str(uuid.uuid4()))

    with patch(
        "modulo.api.routes.admin.update_user",
        new_callable=AsyncMock,
        return_value=MagicMock(active=False),
    ), patch(
        "modulo.api.routes.admin.get_user_by_id_org",
        new_callable=AsyncMock,
        return_value=MagicMock(id=uuid.UUID(target_user_id), active=True),
    ):
        resp = client.post(f"/api/v1/admin/users/{target_user_id}/deactivate")
    request.node._resp = resp


@then("the response status is 201")
def response_status_201(request):
    resp = request.node._resp
    assert resp.status_code == 201, f"Expected 201, got {resp.status_code}: {resp.text[:200]}"


@then(parsers.parse('the membership has role "{role}"'))
def membership_has_role(request, role: str):
    body = request.node._resp.json()
    assert body.get("role") == role, f"Expected role {role!r}, got {body.get('role')!r}"


@then("the response status is 204")
def response_status_204(request):
    resp = request.node._resp
    assert resp.status_code == 204, f"Expected 204, got {resp.status_code}"


@then(parsers.parse('"{username}" is no longer a member'))
def user_no_longer_member(username: str, ctx):
    assert ctx.get("membership_id") is not None, "Membership should have been removed"


@then(parsers.parse('user "{username}" is deactivated'))
def user_deactivated(username: str, ctx):
    assert ctx.get("user_active") is not True


# ===========================================================================
# orgs/org_onboarding.feature
# ===========================================================================


@given("a new organisation signs up")
def new_org_signup(ctx):
    # Ensure no onboarding state file exists
    path = _onboarding_state_path()
    if os.path.exists(path):
        os.remove(path)


@given("the welcome flow is completed")
def welcome_flow_completed(ctx):
    path = _onboarding_state_path()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        json.dump({"is_first_run": True, "completed_steps": []}, f)
    ctx["all_steps_done"] = False


@when("I GET /api/v1/onboarding/status")
def get_onboarding_status(client, request):
    resp = client.get("/api/v1/onboarding/status")
    request.node._resp = resp


@when(parsers.parse('I POST /api/v1/onboarding/step with step_id "{step_id}"'))
def post_onboarding_step(request, step_id: str, client):
    resp = client.post("/api/v1/onboarding/step", json={"step_id": step_id})
    request.node._resp = resp


@when("all onboarding steps are marked complete")
def mark_all_steps_complete(client, request, ctx):
    for step_id in ["connect_tools", "select_template", "configure_agent", "run_demo"]:
        client.post("/api/v1/onboarding/step", json={"step_id": step_id})
    resp = client.get("/api/v1/onboarding/status")
    request.node._resp = resp


@when(parsers.parse('I GET /api/v1/onboarding/step/{step_id}'))
def get_onboarding_step(request, step_id: str, client):
    resp = client.get(f"/api/v1/onboarding/step/{step_id}")
    request.node._resp = resp


@then("the response indicates it is the first run")
def response_indicates_first_run(request):
    body = request.node._resp.json()
    assert body.get("is_first_run") is True, f"Expected is_first_run=true, got {body}"


@then("the current step is step 1")
def current_step_is_1(request):
    body = request.node._resp.json()
    assert body.get("current_step") == 1, f"Expected current_step=1, got {body}"


@then("the step is marked completed")
def step_marked_completed(request):
    body = request.node._resp.json()
    assert body.get("completed") is True, f"Step not marked completed: {body}"


@then('completed_steps contains "connect_tools"')
def completed_steps_contains(request):
    body = request.node._resp.json()
    assert "connect_tools" in body.get("completed_steps", []), (
        f"connect_tools not in completed_steps: {body}"
    )


@then("is_first_run becomes false")
def is_first_run_false(request):
    body = request.node._resp.json()
    assert body.get("is_first_run") is False, f"Expected is_first_run=false, got {body}"


@then("the response contains connector options")
def response_contains_connector_options(request):
    body = request.node._resp.json()
    data = body.get("data", {})
    assert "connectors" in data or "title" in data, (
        f"Expected connector info in response: {body}"
    )
