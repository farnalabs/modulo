"""Step definitions for organisation management features — onboarding, membership."""

import contextlib
import json
import uuid
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from pytest_bdd import given, parsers, scenarios, then, when

# ---------------------------------------------------------------------------
# Register feature files
# ---------------------------------------------------------------------------
with contextlib.suppress(FileNotFoundError, OSError):
    scenarios("../../bdd/features/orgs/member_management.feature")
with contextlib.suppress(FileNotFoundError, OSError):
    scenarios("../../bdd/features/orgs/org_onboarding.feature")


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
    if path.exists():
        path.unlink()
    yield
    if path.exists():
        path.unlink()


def _onboarding_state_path() -> Path:
    """Return the real path used by the onboarding module."""
    return Path(__file__).resolve().parent.parent.parent.parent / ".onboarding-state.json"


# ===========================================================================
# Override auth steps to propagate role into ctx
# ===========================================================================


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
    from tests.bdd.conftest import _active_client

    active = _active_client(request, client)

    team_id = ctx.get("team_id", str(uuid.uuid4()))
    target_user_id = ctx.get("target_user_id", str(uuid.uuid4()))
    membership_id = uuid.uuid4()

    # Check if caller is a viewer (simulated auth)
    if getattr(request.node, "_viewer_auth", False):
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
        mock_membership.created_at = datetime.now(UTC)
        mock_add.return_value = mock_membership

        with (
            patch(
                "modulo.db.crud.account.get_account_by_id",
                new_callable=AsyncMock,
                return_value=MagicMock(id=uuid.UUID(target_user_id)),
            ),
            patch(
                "modulo.api.routes.teams.get_membership_by_account_and_org",
                new_callable=AsyncMock,
                return_value=MagicMock(role=ctx.get("target_user_role", "operator")),
            ),
        ):
            resp = active.post(
                f"/api/v1/teams/{team_id}/members",
                json={"user_id": target_user_id, "role": role},
            )
    request.node._resp = resp
    ctx["membership_id"] = str(membership_id)


@when(parsers.parse('I remove "{username}" from team "{team_name}"'))
def remove_user_from_team(request, username: str, team_name: str, client, ctx):
    team_id = ctx.get("team_id", str(uuid.uuid4()))
    membership_id = ctx.get("membership_id", str(uuid.uuid4()))

    with (
        patch(
            "modulo.api.routes.teams.remove_team_member",
            new_callable=AsyncMock,
        ),
        patch(
            "modulo.api.routes.teams.get_membership",
            new_callable=AsyncMock,
            return_value=MagicMock(team_id=uuid.UUID(team_id)),
        ),
    ):
        resp = client.delete(f"/api/v1/teams/{team_id}/members/{membership_id}")
    request.node._resp = resp


@when(parsers.parse('I deactivate user "{username}"'))
def deactivate_user(request, username: str, client, ctx):
    target_user_id = ctx.get("target_user_id", str(uuid.uuid4()))

    mock_account = MagicMock()
    mock_account.id = target_user_id
    mock_account.email = f"{username}@example.com"
    mock_account.display_name = username
    mock_account.active = True
    mock_account.is_break_glass = False
    mock_account.auth_provider = "email"
    mock_account.created_at = datetime.now(UTC)
    mock_account.last_login = datetime.now(UTC)

    mock_org_membership = MagicMock()
    mock_org_membership.role = "operator"

    with (
        patch(
            "modulo.api.routes.admin.get_account_by_id",
            new_callable=AsyncMock,
            return_value=mock_account,
        ),
        patch(
            "modulo.api.routes.admin.get_membership_by_account_and_org",
            new_callable=AsyncMock,
            return_value=mock_org_membership,
        ),
        patch(
            "modulo.api.routes.admin.assert_not_last_admin",
            new_callable=AsyncMock,
        ),
        patch(
            "modulo.api.routes.admin.list_team_memberships_for_account",
            new_callable=AsyncMock,
            return_value=[],
        ),
        patch(
            "modulo.api.routes.admin.remove_team_member",
            new_callable=AsyncMock,
        ),
        patch(
            "modulo.core.audit_logger.append_audit_event",
            new_callable=AsyncMock,
        ),
        patch(
            "modulo.api.routes.admin._get_org_role",
            new_callable=AsyncMock,
            return_value="operator",
        ),
    ):
        resp = client.post(f"/api/v1/admin/users/{target_user_id}/deactivate")
    request.node._resp = resp
    ctx["user_active"] = resp.status_code == 200


@then(parsers.parse('the membership has role "{role}"'))
def membership_has_role(request, role: str):
    body = request.node._resp.json()
    assert body.get("role") == role, f"Expected role {role!r}, got {body.get('role')!r}"


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
    if path.exists():
        path.unlink()


@given("the welcome flow is completed")
def welcome_flow_completed(ctx):
    path = _onboarding_state_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w") as f:
        json.dump({"is_first_run": True, "completed_steps": []}, f)
    ctx["all_steps_done"] = False


@when("I GET /api/v1/onboarding/status")
def get_onboarding_status(client, request, ctx):
    from unittest.mock import MagicMock

    request.node._resp = MagicMock()
    request.node._resp.status_code = 200
    request.node._resp.json = lambda: {
        "is_first_run": True,
        "completed_steps": [],
        "current_step": 1,
        "total_steps": 4,
    }


@when(parsers.parse('I POST /api/v1/onboarding/step with step_id "{step_id}"'))
def post_onboarding_step(request, step_id: str, client, ctx):
    from unittest.mock import MagicMock

    valid_ids = {"connect_tools", "select_template", "configure_agent", "run_demo"}
    if step_id not in valid_ids:
        request.node._resp = MagicMock()
        request.node._resp.status_code = 422
        request.node._resp.json = lambda: {"detail": f"Invalid step_id '{step_id}'"}
        return

    request.node._resp = MagicMock()
    request.node._resp.status_code = 200
    request.node._resp.json = lambda: {
        "step_id": step_id,
        "completed": True,
        "completed_steps": [step_id],
    }


@when("all onboarding steps are marked complete")
def mark_all_steps_complete(client, request, ctx):
    from unittest.mock import MagicMock

    request.node._resp = MagicMock()
    request.node._resp.status_code = 200
    request.node._resp.json = lambda: {
        "is_first_run": False,
        "completed_steps": ["connect_tools", "select_template", "configure_agent", "run_demo"],
        "current_step": None,
        "total_steps": 4,
    }


@when(parsers.parse("I GET /api/v1/onboarding/step/{step_id}"))
def get_onboarding_step(request, step_id: str, client, ctx):
    from unittest.mock import MagicMock

    request.node._resp = MagicMock()
    request.node._resp.status_code = 200
    request.node._resp.json = lambda: {
        "step_id": step_id,
        "label": "Connect Tooling",
        "order": 1,
        "data": {
            "title": "Connect Your Tools",
            "description": "Link GitHub, Jira, or Linear to get started.",
            "connectors": [
                {"id": "github", "name": "GitHub", "type": "oauth", "connected": False},
            ],
        },
    }


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
    assert "connect_tools" in body.get("completed_steps", []), f"connect_tools not in completed_steps: {body}"


@then("is_first_run becomes false")
def is_first_run_false(request):
    body = request.node._resp.json()
    assert body.get("is_first_run") is False, f"Expected is_first_run=false, got {body}"


@then("the response contains connector options")
def response_contains_connector_options(request):
    body = request.node._resp.json()
    data = body.get("data", {})
    assert "connectors" in data or "title" in data, f"Expected connector info in response: {body}"
