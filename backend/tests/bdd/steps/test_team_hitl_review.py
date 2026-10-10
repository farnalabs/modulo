"""Step definitions for Team-Scoped HITL Gate features."""

import asyncio
import contextlib
import uuid
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from pytest_bdd import given, parsers, scenarios, then, when

# ---------------------------------------------------------------------------
# Active features
# ---------------------------------------------------------------------------
with contextlib.suppress(FileNotFoundError, OSError):
    scenarios("../features/teams/team_hitl_review.feature")

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def ctx():
    """Shared mutable context dict for team HITL gate tests."""
    return {}


def _mcp_ctx_seams(org_id, run_mock, gate_mock=None, config=None):
    """Build the patch context for MCP HITL tool implementations.

    ``validate_current_auth``, the request-context accessors, the agent-tool
    scope gate, the DB session factory, and the run loader are patched so the
    real `_review_hitl_impl` / `_get_hitl_review_impl` bodies execute against a
    staged run; the tenant config resolvers patch at their SOURCE module
    (both implementations import them locally at call time).
    """

    @asynccontextmanager
    async def fake_session(_org_id):
        yield MagicMock()

    org_id_val = org_id

    def ctx_org():
        return org_id_val

    def ctx_user():
        return uuid.uuid4()

    def no_scope_check(tool_name, action=None):
        return None

    patches = [
        patch("modulo.api.mcp_server.validate_current_auth", new_callable=AsyncMock, return_value=True),
        patch("modulo.api.mcp_server._ctx_org_id_val", new=ctx_org),
        patch("modulo.api.mcp_server._ctx_user_id_val", new=ctx_user),
        patch("modulo.api.mcp_server._check_agent_tool_scope", new=no_scope_check),
        patch("modulo.api.mcp_server._session", new=fake_session),
        patch("modulo.api.mcp_server._load_hitl_run", new_callable=AsyncMock, return_value=run_mock),
    ]
    if gate_mock is not None:
        mcp_mgr = MagicMock()
        mcp_mgr.get_gate = AsyncMock(return_value=gate_mock)
        patches.append(patch("modulo.api.mcp_server.HITLManager", return_value=mcp_mgr))
    if config is not None:
        patches.append(
            patch(
                "modulo.db.crud.hitl_review_config.resolve_hitl_review_config",
                new_callable=AsyncMock,
                return_value=config,
            )
        )
    return patches


# ============================================================================
# Team — HITL Gate
# ============================================================================


@given(parsers.parse('a team "{team_name}" exists'))
def team_exists(team_name: str, ctx):
    ctx["team_name"] = team_name
    ctx["team_id"] = uuid.uuid4()


@given(parsers.parse('user "{username}" is a member of team "{team_name}" with role "{role}"'))
def user_is_team_member(username: str, team_name: str, role: str, ctx):
    ctx["username"] = username
    ctx["user_id"] = uuid.uuid4()
    ctx["team_role"] = role


@given(parsers.parse('user "{username}" is not a member of team "{team_name}"'))
def user_not_team_member(username: str, team_name: str, ctx):
    ctx["username"] = username
    ctx["user_id"] = uuid.uuid4()


@given(parsers.parse('a run "{run_name}" is awaiting human at gate "{review_id}" with required_team_id "{team_name}"'))
def run_awaiting_with_team(run_name: str, review_id: str, team_name: str, ctx):
    ctx["run_name"] = run_name
    ctx["run_id"] = uuid.uuid4()
    ctx["review_id"] = review_id
    ctx["run_status"] = "awaiting_human"

    mock_gate = MagicMock()
    mock_gate.run_id = ctx["run_id"]
    mock_gate.review_id = review_id
    mock_gate.pipeline_id = uuid.uuid4()
    mock_gate.organisation_id = uuid.UUID("00000000-0000-0000-0000-000000000001")
    mock_gate.claimed_by = None
    mock_gate.claimed_at = None
    mock_gate.expires_at = None
    mock_gate.claim_token = None
    mock_gate.decision = None
    mock_gate.decision_at = None
    mock_gate.required_team_id = ctx.get("team_id")
    ctx["mock_gate"] = mock_gate


@given(
    parsers.parse(
        'a run "{run_name}" is awaiting human at gate "{review_id}"'
        ' with required_team_id "{team_name}" and human_only true'
    )
)
def run_awaiting_with_team_and_human_only(run_name: str, review_id: str, team_name: str, ctx):
    ctx["run_name"] = run_name
    ctx["run_id"] = uuid.uuid4()
    ctx["review_id"] = review_id
    ctx["human_only"] = True
    ctx["run_status"] = "awaiting_human"

    mock_gate = MagicMock()
    mock_gate.run_id = ctx["run_id"]
    mock_gate.review_id = review_id
    mock_gate.pipeline_id = uuid.uuid4()
    mock_gate.organisation_id = uuid.UUID("00000000-0000-0000-0000-000000000001")
    mock_gate.claimed_by = None
    mock_gate.claimed_at = None
    mock_gate.expires_at = None
    mock_gate.claim_token = None
    mock_gate.decision = None
    mock_gate.decision_at = None
    mock_gate.required_team_id = ctx.get("team_id")
    ctx["mock_gate"] = mock_gate


@when(parsers.parse('user "{username}" claims the HITL review "{review_id}" on run "{run_name}"'))
def user_claims_gate(username: str, review_id: str, run_name: str, ctx, request, client):
    """Drive the real claim route for a team-scoped gate.

    ``HITLManager.claim`` is patched to mirror the manager's team-role
    enforcement: a member with a runner/operator team role claims
    successfully; a viewer membership (or no membership) raises
    ``NotTeamMemberError``, which the router maps to a real 403.
    ``transition_run`` (the post-claim status flip) is patched — the claim
    transaction itself still runs against the real route.
    """
    from modulo.core.hitl_manager import NotTeamMemberError

    _ = username, review_id, run_name
    is_member = ctx.get("team_role") in {"runner", "operator"}

    with (
        patch("modulo.api.routes.hitl.HITLManager") as mock_mgr_cls,
        patch("modulo.api.routes.hitl.transition_run", new_callable=AsyncMock),
    ):
        if is_member:
            claimed_gate = MagicMock()
            claimed_gate.run_id = ctx["run_id"]
            claimed_gate.review_id = ctx["review_id"]
            claimed_gate.claim_token = "valid_token_" + uuid.uuid4().hex
            claimed_gate.expires_at = datetime.now(UTC) + timedelta(minutes=15)
            mock_mgr_cls.return_value.claim = AsyncMock(return_value=claimed_gate)
        else:
            mock_mgr_cls.return_value.claim = AsyncMock(
                side_effect=NotTeamMemberError(
                    run_id=ctx["run_id"],
                    review_id=ctx["review_id"],
                    team_id=ctx.get("team_id", uuid.uuid4()),
                    user_id=ctx.get("user_id", uuid.uuid4()),
                )
            )
        resp = client.post(
            f"/api/v1/runs/{ctx['run_id']}/hitl/{ctx['review_id']}/claim",
            json={"expiry_minutes": 15},
        )
    ctx["_resp"] = resp
    request.node._resp = resp
    if resp.status_code == 200:
        ctx["claim_token"] = resp.json()["claim_token"]


@when(parsers.parse('an MCP client attempts to approve gate "{review_id}" on run "{run_name}" as user "{username}"'))
def mcp_attempts_approve(review_id: str, run_name: str, username: str, ctx, request):
    """Drive the real MCP ``review_hitl`` implementation for a human_only gate.

    The MCP tool's orchestrating seams (auth, scope gate, DB session, run
    loader) are patched so the real implementation body executes; the tenant
    HITL config resolves to ``human_only: true`` so the shared
    ``human_only_denial`` policy denies the (inherently non-browser) MCP
    approval. A claim_token is synthesized because an approve action cannot
    reach the human_only check without one.
    """
    from modulo.api.mcp_server import _review_hitl_impl

    _ = run_name, username
    org_id = uuid.UUID("00000000-0000-0000-0000-000000000001")
    run_mock = MagicMock()
    run_mock.id = ctx["run_id"]
    run_mock.status = "awaiting_human"
    config = {
        "human_only": True,
        "required_team_id": str(ctx.get("team_id")),
        "label": review_id,
    }

    with contextlib.ExitStack() as stack:
        for seam in _mcp_ctx_seams(org_id, run_mock, config=config):
            stack.enter_context(seam)
        body = asyncio.run(_review_hitl_impl(str(ctx["run_id"]), review_id, "approve", "stale-token", None, None))
    ctx["_body"] = body


@when(parsers.parse('I request the gate context for run "{run_name}" gate "{review_id}"'))
def request_gate_context(run_name: str, review_id: str, ctx, request):
    """Drive the real MCP ``get_hitl_review`` implementation.

    The run loader returns a run awaiting a human, the gate row is
    unclaimed, and the tenant config resolves the gate's required team; the
    real implementation assembles the context dict (including
    ``review_config.required_team_id``).
    """
    from modulo.api.mcp_server import _get_hitl_review_impl

    _ = run_name
    org_id = uuid.UUID("00000000-0000-0000-0000-000000000001")
    run_mock = MagicMock()
    run_mock.id = ctx["run_id"]
    run_mock.status = "awaiting_human"

    gate_mock = MagicMock()
    gate_mock.account_id = None
    gate_mock.claimed_at = None
    gate_mock.expires_at = None
    gate_mock.decision = None
    gate_mock.decision_at = None

    config = {
        "label": review_id,
        "condition": None,
        "human_only": False,
        "claim_expiry_minutes": 15,
        "reject_target": None,
        "on_reject": None,
        "required_team_id": str(ctx.get("team_id")),
    }

    with contextlib.ExitStack() as stack:
        for seam in _mcp_ctx_seams(org_id, run_mock, gate_mock=gate_mock, config=config):
            stack.enter_context(seam)
        body = asyncio.run(_get_hitl_review_impl(str(ctx["run_id"]), review_id))
    ctx["_body"] = body


@given(parsers.parse('user "{username}" holds a valid claim_token for gate "{review_id}"'))
def user_holds_claim_token(username: str, review_id: str, ctx):
    _ = review_id
    ctx["username"] = username
    ctx["user_id"] = ctx.get("user_id", uuid.uuid4())
    ctx["claim_token"] = "valid_token_" + uuid.uuid4().hex

    # Create a mock gate that is claimed by this user
    mock_gate = MagicMock()
    mock_gate.run_id = ctx["run_id"]
    mock_gate.review_id = ctx["review_id"]
    mock_gate.pipeline_id = uuid.uuid4()
    mock_gate.organisation_id = uuid.UUID("00000000-0000-0000-0000-000000000001")
    mock_gate.claimed_by = ctx["user_id"]
    mock_gate.claim_token = ctx["claim_token"]
    mock_gate.decision = None
    mock_gate.required_team_id = ctx.get("team_id")
    ctx["mock_gate"] = mock_gate


@when(parsers.parse('user "{username}" approves gate "{review_id}" on run "{run_name}"'))
def user_approves_gate(username: str, review_id: str, run_name: str, ctx, request, client):
    """Drive the real approve route with the claim held by the user.

    ``HITLManager.approve`` returns the claimed gate and
    ``PipelineExecutor.resume`` is patched (the deliver-manual pattern) so the
    scenario asserts the router's actual contract: a real 200 with
    ``{"status": "approved", "run_id": ...}`` and a resumed run.
    """
    from modulo.core.pipeline_engine.executor import PipelineExecutor as RealExecutor

    _ = username, review_id, run_name

    mock_mgr = MagicMock()
    mock_mgr.approve = AsyncMock(return_value=ctx["mock_gate"])
    with (
        patch("modulo.api.routes.hitl.HITLManager", return_value=mock_mgr),
        patch("modulo.api.routes.hitl.PipelineExecutor", spec=RealExecutor) as mock_exec_cls,
    ):
        mock_exec_cls.return_value.resume = AsyncMock()
        resp = client.post(
            f"/api/v1/runs/{ctx['run_id']}/hitl/{ctx['review_id']}/approve",
            json={"claim_token": ctx["claim_token"], "notes": None},
        )
    ctx["_resp"] = resp
    request.node._resp = resp
    if resp.status_code == 200:
        ctx["run_status"] = "running"


@then("the response contains a claim_token")
def response_contains_claim_token(ctx):
    resp = ctx.get("_resp")
    if hasattr(resp, "json"):
        body = resp.json()
        assert "claim_token" in body, "Expected claim_token in response"
        assert body["claim_token"] is not None, "Expected non-null claim_token"


@then(parsers.parse('the error indicates the gate requires team "{team_name}"'))
def error_indicates_team_required(team_name: str, ctx):
    resp = ctx.get("_resp")
    if hasattr(resp, "json"):
        body = resp.json()
        detail = body.get("detail", "")
        assert team_name in detail or "team" in detail.lower(), (
            f"Expected error to mention team {team_name}, got: {detail}"
        )


@then("the error indicates the gate requires human approval")
def error_indicates_human_required(ctx):
    body = ctx.get("_body")
    if body is None:
        resp = ctx.get("_resp")
        body = resp.json() if hasattr(resp, "json") else {}
    assert body.get("error") == "human_only_gate", f"Expected a human_only_gate error, got: {body}"
    assert "human" in str(body.get("detail", "")).lower(), (
        f"Expected the error to mention human authentication, got: {body.get('detail')}"
    )


@then("the gate context exposes the required team")
def gate_context_exposes_required_team(ctx):
    body = ctx.get("_body") or {}
    review_config = body.get("review_config")
    assert review_config, f"Expected review_config in the gate context, got: {body}"
    assert review_config.get("required_team_id"), (
        f"Expected required_team_id inside review_config, got: {review_config}"
    )


@then("the run resumes execution")
def run_resumes_execution(ctx):
    assert ctx.get("run_status") == "running", f"Expected run to be running, got {ctx.get('run_status')}"
