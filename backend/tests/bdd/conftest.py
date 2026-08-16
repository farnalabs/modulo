"""BDD/E2E test fixtures — pytest-bdd, Playwright, and TestClient setup."""

import os
import uuid
from collections.abc import AsyncGenerator, Generator
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.exc import IntegrityError

os.environ.setdefault("MODULO_CSRF_ENABLED", "false")
os.environ.setdefault("REDIS_URL", "")
os.environ.setdefault("MODULO_AUTH_RATE_LIMIT_ENABLED", "false")
os.environ.setdefault("DATABASE_URL", "sqlite+aiosqlite:///./test.db")
os.environ.setdefault("MODULO_DB", "sqlite")
os.environ.setdefault("SECRET_KEY", "a" * 32)
os.environ.setdefault("FERNET_KEY", "b" * 32)

from modulo.auth.jwt import AuthenticatedPrincipal
from modulo.settings import Settings

_VALID_32 = "a" * 32
ORG_ID = uuid.UUID("00000000-0000-0000-0000-000000000001")
USER_ID = uuid.UUID("00000000-0000-0000-0000-000000000002")
ALT_ORG_ID = uuid.UUID("00000000-0000-0000-0000-000000000003")


# ---------------------------------------------------------------------------
# Playwright fixtures (E2E with ?theme=agent)
# ---------------------------------------------------------------------------


@pytest.fixture(scope="session")
def browser_context_args(browser_context_args):
    return {
        **browser_context_args,
        "viewport": {"width": 1440, "height": 900},
        "color_scheme": "dark",
    }


@pytest.fixture(scope="session")
def base_url() -> str:
    return "http://localhost:5173"


# ---------------------------------------------------------------------------
# Mock helpers (reused across step definitions)
# ---------------------------------------------------------------------------


def make_settings() -> Settings:
    return Settings(
        database_url="postgresql+asyncpg://localhost/test",
        secret_key=_VALID_32,
        fernet_key=_VALID_32,
        modulo_admin_password="testpass",
        modulo_license_key="test-license-key",
        modulo_csrf_enabled=False,
    )


def make_mock_session() -> AsyncMock:
    session = AsyncMock()
    begin_cm = AsyncMock()
    begin_cm.__aenter__ = AsyncMock(return_value=None)
    begin_cm.__aexit__ = AsyncMock(return_value=False)
    session.begin = MagicMock(return_value=begin_cm)
    scalar_mock = MagicMock()
    scalar_mock.all = MagicMock(return_value=[])
    team_mock = MagicMock()
    team_mock.id = uuid.uuid4()
    team_mock.organisation_id = ORG_ID
    team_mock.name = "test-team"
    # Account-shaped attributes the auth routes read when a query returns this
    # row: JWT issuance serialises email/is_system_admin into the token claims,
    # so they must be real values — a bare MagicMock breaks json.dumps.
    team_mock.email = "testuser@example.com"
    team_mock.is_system_admin = True
    hitl_result = AsyncMock()
    hitl_result.scalar_one_or_none = MagicMock(return_value=team_mock)
    hitl_result.scalar_one = MagicMock(return_value=0)
    hitl_result.scalar = MagicMock(return_value=0)
    hitl_result.scalars = MagicMock(return_value=scalar_mock)
    hitl_result.first = MagicMock(return_value=MagicMock())
    session.execute.return_value = hitl_result
    return session


def make_mock_pipeline(**kwargs: Any) -> MagicMock:
    p = MagicMock()
    p.id = kwargs.get("id", uuid.uuid4())
    p.organisation_id = kwargs.get("org_id", ORG_ID)
    p.name = kwargs.get("name", "Test Pipeline")
    p.description = kwargs.get("description")
    p.visibility = kwargs.get("visibility", "org")
    p.max_concurrent_runs = kwargs.get("max_concurrent_runs", 5)
    p.lock_wait_timeout_seconds = kwargs.get("lock_wait_timeout_seconds", 300)
    p.node_timeout_seconds = kwargs.get("node_timeout_seconds", 300)
    p.run_context_defaults = kwargs.get("run_context_defaults", {})
    p.rate_limit_config = kwargs.get("rate_limit_config")
    p.created_by = uuid.uuid4()
    p.created_at = kwargs.get("created_at", datetime.now(UTC))
    p.updated_at = kwargs.get("updated_at", datetime.now(UTC))
    return p


def make_mock_run(**kwargs: Any) -> MagicMock:
    r = MagicMock()
    r.id = kwargs.get("id", uuid.uuid4())
    r.pipeline_id = kwargs.get("pipeline_id", uuid.uuid4())
    r.status = kwargs.get("status", "pending")
    r.langgraph_thread_id = str(uuid.uuid4())
    r.error_detail = kwargs.get("error_detail")
    r.error_code = kwargs.get("error_code")
    r.input_hash = kwargs.get("input_hash", "0" * 64)
    r.trigger_type = kwargs.get("trigger_type", "manual")
    r.final_state = kwargs.get("final_state")
    r.run_number = kwargs.get("run_number", 1)
    r.total_tokens = kwargs.get("total_tokens", 0)
    r.total_cost_usd = kwargs.get("total_cost_usd")
    r.node_token_usage = kwargs.get("node_token_usage")
    r.pipeline = kwargs.get("pipeline")
    return r


def make_mock_snapshot(**kwargs: Any) -> MagicMock:
    s = MagicMock()
    s.id = kwargs.get("id", uuid.uuid4())
    s.graph_json = kwargs.get(
        "graph_json",
        {
            "nodes": [{"id": "node-a", "role": None}],
            "edges": [],
        },
    )
    s.run_context_defaults = kwargs.get("run_context_defaults", {})
    s.connector_bindings_json = kwargs.get("connector_bindings", [])
    s.schema_pins_json = kwargs.get("schema_pins", [])
    s.prompt_pins_json = kwargs.get("prompt_pins", [])
    s.model_backend_pins_json = kwargs.get("backend_pins", [])
    return s


# ---------------------------------------------------------------------------
# TestClient fixture (API-level BDD steps)
# ---------------------------------------------------------------------------


@pytest.fixture
def mock_session() -> AsyncMock:
    return make_mock_session()


@pytest.fixture
def client(mock_session: AsyncMock) -> Generator[TestClient, None, None]:
    yield from _make_test_client(
        mock_session,
        username="testuser",
        organisation_id=ORG_ID,
        account_id=USER_ID,
        org_role="admin",
    )


@pytest.fixture
def unauth_client(mock_session: AsyncMock) -> Generator[TestClient, None, None]:
    from modulo.api.dependencies import _get_engine, get_db_session
    from modulo.api.main import app
    from modulo.settings import get_settings

    async def override_session() -> AsyncGenerator[AsyncMock, None]:
        yield mock_session

    app.dependency_overrides[get_settings] = make_settings
    app.dependency_overrides[get_db_session] = override_session
    app.dependency_overrides[_get_engine] = lambda: MagicMock()
    yield TestClient(app)

    app.dependency_overrides.clear()


# ---------------------------------------------------------------------------
# Common step definitions (shared across all step files)
# ---------------------------------------------------------------------------

from pytest_bdd import given, parsers, then, when  # noqa: E402

from unittest.mock import patch  # noqa: E402

from pytest_bdd import given, parsers, then, when  # noqa: E402


def _make_mock_pipeline_full(name: str = "Test Pipeline", **kwargs: Any) -> MagicMock:
    """Full-shaped Pipeline ORM mock matching PipelineResponse validation."""
    p = MagicMock()
    p.id = kwargs.get("id", uuid.uuid4())
    p.organisation_id = ORG_ID
    p.name = name
    p.description = kwargs.get("description")
    p.visibility = kwargs.get("visibility", "org")
    p.max_concurrent_runs = kwargs.get("max_concurrent_runs", 5)
    p.lock_wait_timeout_seconds = kwargs.get("lock_wait_timeout_seconds", 300)
    p.node_timeout_seconds = kwargs.get("node_timeout_seconds", 300)
    p.run_context_defaults = kwargs.get("run_context_defaults", {})
    p.default_autonomy_level = kwargs.get("default_autonomy_level")
    p.max_duration_seconds = None
    p.stale_run_timeout_minutes = 30
    p.rate_limit_config = kwargs.get("rate_limit_config")
    p.retry_policy = kwargs.get("retry_policy", {})
    p.snapshot_count = 0
    p.archived_at = None
    p.owner_team_id = None
    p.folder_id = None
    p.connector_rebind_required = False
    p.account_id = USER_ID
    p.created_at = datetime.now(UTC)
    p.updated_at = datetime.now(UTC)
    return p


@given(parsers.parse('I am authenticated as an admin in org "{org}"'))
def _bdd_auth_admin_in_org(org: str, request, client) -> None:
    """No-op — the ``client`` fixture already provides an admin principal."""
    request.node._client = client


@given(parsers.parse('I am authenticated in org "{org}"'))
def _bdd_auth_in_org(org: str, request, client) -> None:
    """Auth fixture handles this; set the default admin client for @when steps."""
    request.node._client = client


@given(parsers.parse('I am authenticated as a viewer in org "{org}"'))
def _bdd_auth_viewer_in_org(org: str, request, viewer_client) -> None:
    """Flag viewer authentication and set the role client for @when steps."""
    request.node._viewer_auth = True
    request.node._client = viewer_client


@given("the organisation exists")
def _bdd_org_exists() -> None:
    """No-op — DB fixtures handle org creation."""


@when(parsers.parse('I POST /api/pipelines with name "{name}" and valid config'))
@given(parsers.parse('I POST /api/pipelines with name "{name}" and valid config'))
def _bdd_create_pipeline(name: str, client, request) -> None:
    """Shared create-pipeline step used by create.feature and org_scoping.feature.

    When a pipeline with this name was already declared (via `Given org "..."
    has pipeline "{name}"`), the create path raises IntegrityError which the
    route maps to 409.
    """
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
        resp = client.post("/api/v1/pipelines", json={"name": name})
    request.node._resp = resp


@then(parsers.parse("the response status is {status:d}"))
def _bdd_check_response_status(status: int, request) -> None:
    """Check response status code."""
    resp = request.node._resp
    assert resp.status_code == status, f"Expected status {status}, got {resp.status_code}"


@then(parsers.parse('the response has name "{expected}"'))
def _bdd_check_response_name(expected: str, request) -> None:
    """Check that the stored response body carries the expected ``name`` field.

    Shared by the auth api-keys scenarios and the eval-suite CRUD scenarios;
    living here (an ancestor of every BDD module) keeps the step text defined
    exactly once.
    """
    body = request.node._resp.json()
    actual = body.get("name")
    assert actual == expected, f"Expected name {expected!r}, got {actual!r}"


# ---------------------------------------------------------------------------
# Team CRUD / membership steps — shared by auth/rbac.feature (test_auth.py)
# and the sibling team step modules (test_team_crud.py, test_team_membership.py,
# test_team_create.py).
#
# These drive the real ``/api/v1/teams`` routes with only the DB CRUD
# functions patched, so the scenarios assert the actual API contract —
# status codes, response shapes, and the router's own ``require_permission`` /
# ``require_feature`` gates. Living here (an ancestor of every BDD module)
# keeps each step text defined exactly once instead of being redefined in
# test_auth.py and the sibling modules with divergent implementations.
# ---------------------------------------------------------------------------


def _make_mock_team(name: str, description: str = "") -> MagicMock:
    team = MagicMock()
    team.id = uuid.uuid4()
    team.organisation_id = ORG_ID
    team.name = name
    team.description = description
    team.account_id = USER_ID
    team.created_at = datetime(2025, 1, 1, tzinfo=UTC)
    team.updated_at = datetime(2025, 1, 1, tzinfo=UTC)
    return team


def _make_mock_membership(team_id: uuid.UUID, user_id: uuid.UUID, role: str) -> MagicMock:
    membership = MagicMock()
    membership.id = uuid.uuid4()
    membership.team_id = team_id
    membership.account_id = user_id
    membership.role = role
    membership.created_at = datetime(2025, 1, 1, tzinfo=UTC)
    return membership


def _active_client(request: Any, client: Any = None) -> Any:
    """Return the client matching the active auth Given step.

    The conftest auth steps stash the principal client on
    ``request.node._client`` (``viewer_client`` for viewer scenarios, the
    admin ``client`` otherwise), so steps never branch on scenario names.

    The ``client`` argument is optional: requesting it as a step fixture would
    instantiate the admin TestClient *after* a viewer Given has set its
    principal (both clients share the app-wide ``dependency_overrides``), so it
    is only resolved lazily when no auth Given has stashed a client.
    """
    stored = getattr(request.node, "_client", None)
    if stored is not None:
        return stored
    if client is None:
        client = request.getfixturevalue("client")
    return client


def _store_response(request: Any, ctx: dict[str, Any], resp: Any) -> None:
    """Record a response so shared ``@then`` steps can inspect it."""
    request.node._resp = resp  # test_connectors.py convention
    request.node.response = resp  # test_auth.py convention
    ctx["response"] = resp  # test_library.py convention


@given(parsers.parse('a team "{team}" exists'))
def _bdd_team_exists(team: str, ctx: dict[str, Any]) -> None:
    ctx.setdefault("teams", {})[team] = _make_mock_team(team)


@given(parsers.parse('a user "{name}" exists'))
def _bdd_user_exists(name: str, ctx: dict[str, Any]) -> None:
    ctx.setdefault("users", {})[name] = {"id": uuid.uuid4(), "org_role": "admin"}


@given(parsers.parse('a user "{name}" exists with org role "{role}"'))
def _bdd_user_exists_with_role(name: str, role: str, ctx: dict[str, Any]) -> None:
    ctx.setdefault("users", {})[name] = {"id": uuid.uuid4(), "org_role": role}


@given(parsers.parse('user "{name}" is already a member of team "{team}"'))
def _bdd_user_already_member(name: str, team: str, ctx: dict[str, Any]) -> None:
    ctx.setdefault("members", {}).setdefault(team, set()).add(name)
    ctx.setdefault("memberships", {})[f"{name}:{team}"] = {
        "user_id": uuid.uuid4(),
        "team_id": uuid.uuid4(),
        "role": "operator",
    }


@when(parsers.re(r'I create a team with name "(?P<name>[^"]*)" and description "(?P<desc>[^"]*)"'))
def _bdd_create_team(request: Any, name: str, desc: str, ctx: dict[str, Any], client: Any = None) -> None:
    """POST /api/v1/teams — the route's ``require_permission("team.create")``
    gate returns 403 for the viewer principal and its ``CreateTeamRequest``
    validation returns 422 for empty names, so no scenario-name matching is
    needed here."""
    existing = ctx.get("teams", {}).get(name)
    created = _make_mock_team(name, desc)
    with (
        patch("modulo.api.routes.teams.get_team_by_name", new_callable=AsyncMock, return_value=existing),
        patch("modulo.api.routes.teams.create_team", new_callable=AsyncMock, return_value=created),
    ):
        resp = _active_client(request, client).post("/api/v1/teams", json={"name": name, "description": desc})
    _store_response(request, ctx, resp)


@when("I list teams")
def _bdd_list_teams(request: Any, ctx: dict[str, Any], client: Any = None) -> None:
    teams = list(ctx.get("teams", {}).values())
    page = SimpleNamespace(items=teams, total=len(teams), page=1, page_size=20)
    with patch("modulo.api.routes.teams.list_teams", new_callable=AsyncMock, return_value=page):
        resp = _active_client(request, client).get("/api/v1/teams")
    _store_response(request, ctx, resp)


@when(parsers.parse('I get team "{name}"'))
def _bdd_get_team(request: Any, name: str, ctx: dict[str, Any], client: Any = None) -> None:
    team = ctx.get("teams", {}).get(name)
    team_id = team.id if team is not None else uuid.uuid4()
    with patch("modulo.api.routes.teams.get_team", new_callable=AsyncMock, return_value=team):
        resp = _active_client(request, client).get(f"/api/v1/teams/{team_id}")
    _store_response(request, ctx, resp)


@when(parsers.parse('I get team by id "{team_id}"'))
def _bdd_get_team_by_id(request: Any, team_id: str, ctx: dict[str, Any], client: Any = None) -> None:
    with patch("modulo.api.routes.teams.get_team", new_callable=AsyncMock, return_value=None):
        resp = _active_client(request, client).get(f"/api/v1/teams/{team_id}")
    _store_response(request, ctx, resp)


@when(parsers.parse('I delete team by id "{team_id}"'))
def _bdd_delete_team_by_id(request: Any, team_id: str, ctx: dict[str, Any], client: Any = None) -> None:
    with patch("modulo.api.routes.teams.delete_team", new_callable=AsyncMock, return_value=False):
        resp = _active_client(request, client).delete(f"/api/v1/teams/{team_id}")
    _store_response(request, ctx, resp)


@when(parsers.parse('I rename team "{name}" to "{new_name}"'))
def _bdd_rename_team(request: Any, name: str, new_name: str, ctx: dict[str, Any], client: Any = None) -> None:
    team = ctx.get("teams", {}).get(name, _make_mock_team(name))
    conflict = ctx.get("teams", {}).get(new_name)
    if conflict is not None and conflict.id != team.id:
        with patch("modulo.api.routes.teams.get_team_by_name", new_callable=AsyncMock, return_value=conflict):
            resp = _active_client(request, client).patch(f"/api/v1/teams/{team.id}", json={"name": new_name})
    else:
        updated = _make_mock_team(new_name)
        updated.id = team.id
        with (
            patch("modulo.api.routes.teams.get_team_by_name", new_callable=AsyncMock, return_value=None),
            patch("modulo.api.routes.teams.update_team", new_callable=AsyncMock, return_value=updated),
        ):
            resp = _active_client(request, client).patch(f"/api/v1/teams/{team.id}", json={"name": new_name})
    _store_response(request, ctx, resp)


@when(parsers.parse('I add user "{name}" to team "{team}" with role "{role}"'))
def _bdd_add_user_to_team(
    request: Any, name: str, team: str, role: str, ctx: dict[str, Any], client: Any = None
) -> None:
    """POST /api/v1/teams/{team_id}/members through the real route."""
    teams = ctx.get("teams", {})
    team_mock = teams.get(team)
    user = ctx.get("users", {}).get(name, {})
    user_id = user.get("id", uuid.uuid4())

    if team_mock is None:
        with (
            patch("modulo.api.routes.teams.get_team", new_callable=AsyncMock, return_value=None),
            patch("modulo.api.routes.teams.add_team_member", new_callable=AsyncMock),
        ):
            resp = _active_client(request, client).post(
                f"/api/v1/teams/{uuid.uuid4()}/members",
                json={"user_id": str(user_id), "role": role},
            )
        _store_response(request, ctx, resp)
        return

    already_member = name in ctx.setdefault("members", {}).get(team, set())
    target_membership = MagicMock()
    target_membership.role = user.get("org_role", "admin")
    account = MagicMock()
    account.id = user_id
    membership = _make_mock_membership(team_mock.id, user_id, role)

    active = _active_client(request, client)

    duplicate_error = {"side_effect": IntegrityError("stmt", {}, Exception("duplicate member"))}
    membership_value = {"return_value": membership}
    with (
        patch("modulo.api.routes.teams.get_team", new_callable=AsyncMock, return_value=team_mock),
        patch("modulo.db.crud.account.get_account_by_id", new_callable=AsyncMock, return_value=account),
        patch(
            "modulo.db.crud.org_membership.get_membership_by_account_and_org",
            new_callable=AsyncMock,
            return_value=target_membership,
        ),
        patch(
            "modulo.api.routes.teams.add_team_member",
            new_callable=AsyncMock,
            **(duplicate_error if already_member else membership_value),
        ),
    ):
        resp = active.post(
            f"/api/v1/teams/{team_mock.id}/members",
            json={"user_id": str(user_id), "role": role},
        )
    _store_response(request, ctx, resp)
    if resp.status_code == 201:
        ctx.setdefault("members", {}).setdefault(team, set()).add(name)
        ctx.setdefault("memberships", {})[f"{name}:{team}"] = {
            "user_id": user_id,
            "team_id": team_mock.id,
            "role": role,
        }


@when(parsers.parse('I remove user "{name}" from team "{team}"'))
def _bdd_remove_user_from_team(request: Any, name: str, team: str, ctx: dict[str, Any], client: Any = None) -> None:
    team_mock = ctx.get("teams", {}).get(team)
    user = ctx.get("users", {}).get(name, {})
    user_id = user.get("id", uuid.uuid4())
    if team_mock is None:
        with (
            patch("modulo.api.routes.teams.get_membership", new_callable=AsyncMock, return_value=None),
            patch("modulo.api.routes.teams.remove_team_member", new_callable=AsyncMock),
        ):
            resp = _active_client(request, client).delete(f"/api/v1/teams/{uuid.uuid4()}/members/{uuid.uuid4()}")
        _store_response(request, ctx, resp)
        return
    membership = _make_mock_membership(team_mock.id, user_id, "viewer")
    with (
        patch("modulo.api.routes.teams.get_membership", new_callable=AsyncMock, return_value=membership),
        patch("modulo.api.routes.teams.remove_team_member", new_callable=AsyncMock),
    ):
        resp = _active_client(request, client).delete(f"/api/v1/teams/{team_mock.id}/members/{membership.id}")
    _store_response(request, ctx, resp)


@then(parsers.parse('the response contains a team with name "{name}"'))
def _bdd_response_contains_team(request: Any, name: str) -> None:
    body = request.node._resp.json()
    items = body.get("items")
    if items is None:
        assert body.get("name") == name, f"Expected team {name!r}, got {body.get('name')!r}"
        return
    names = [item.get("name") for item in items]
    assert name in names, f"Expected team {name!r} in response, got: {names}"


@then("the team has an account_id")
def _bdd_team_has_account_id(request: Any) -> None:
    body = request.node._resp.json()
    assert body.get("account_id") is not None, f"Team missing account_id: {body}"


@then("the response contains a list of teams")
def _bdd_response_contains_team_list(request: Any) -> None:
    body = request.node._resp.json()
    assert "items" in body, f"Response missing items list: {body}"
    assert isinstance(body["items"], list)


@then(parsers.parse('the error detail mentions "{text}"'))
def _bdd_error_detail_mentions(text: str, request: Any) -> None:
    body = request.node._resp.json()
    detail = body.get("detail", "")
    assert text in detail, f"Expected the error detail to mention {text!r}, got {detail!r}"

@then("the response contains id and slug")
def _bdd_response_id_and_slug(request) -> None:
    """Shared assertion for pipeline create responses (create.feature + org_scoping.feature)."""
    data = request.node._resp.json()
    assert "id" in data, "Response missing id"
    assert "name" in data, "Response missing name"


@when("I GET /api/v1/admin/audit/verify with a broken chain")
def _bdd_get_verify_chain_broken(client, request) -> None:
    """Shared audit verify step: a tampered chain reports tamper evidence.

    Used by ``audit/event_recording.feature``. Defined once here so both the
    alpha and full BDD suites resolve the same step text.
    """
    with (
        patch(
            "modulo.api.routes.audit.verify_chain",
            return_value={
                "valid": False,
                "total_events": 3,
                "checked_events": 2,
                "first_gap_index": 2,
                "first_tampered_id": "evt-3",
                "chain_head_match": None,
                "detail": (
                    "Audit chain break at event 2 (id evt-3): stored previous_hash (tampered-hash) "
                    "does not match the recomputed hash of the prior event (expected-hash). "
                    "The event or one before it has been tampered with."
                ),
            },
        ),
        patch("modulo.api.routes.audit.set_rls_org"),
    ):
        resp = client.get("/api/v1/admin/audit/verify")
    request.node._resp = resp


def _make_test_client(mock_session: AsyncMock, **principal_kwargs: Any) -> Generator[TestClient, None, None]:
    from modulo.api.dependencies import _get_engine, _get_session_factory, get_db_session, get_plan_context
    from modulo.api.main import app
    from modulo.auth.dependencies import (
        get_current_tenant_user,
        get_current_tenant_user_or_api_key,
        get_current_user,
    )
    from modulo.auth.jwt import TenantPrincipal
    from modulo.settings import get_settings

    class _AllFeatures:
        def feature_enabled(self, name: str) -> bool:
            return True

        def list_enabled_features(self) -> list:
            return []

        def tier(self) -> str:
            return "team"

        def has_license_key(self) -> bool:
            return True

    async def override_session() -> AsyncGenerator[AsyncMock, None]:
        yield mock_session

    async def _override_plan_context() -> _AllFeatures:
        return _AllFeatures()

    app.dependency_overrides[get_settings] = make_settings
    app.dependency_overrides[get_db_session] = override_session
    app.dependency_overrides[_get_engine] = lambda: MagicMock()
    app.dependency_overrides[_get_session_factory] = lambda: MagicMock()
    app.dependency_overrides[get_plan_context] = _override_plan_context
    if principal_kwargs:
        app.dependency_overrides[get_current_user] = lambda: AuthenticatedPrincipal(**principal_kwargs)

        async def _override_tenant() -> TenantPrincipal:
            return TenantPrincipal(**principal_kwargs)

        app.dependency_overrides[get_current_tenant_user] = _override_tenant
        app.dependency_overrides[get_current_tenant_user_or_api_key] = _override_tenant

    yield TestClient(app)

    app.dependency_overrides.clear()


@pytest.fixture
def alt_org_client(mock_session: AsyncMock) -> Generator[TestClient, None, None]:
    yield from _make_test_client(
        mock_session,
        username="otheruser",
        organisation_id=ALT_ORG_ID,
        account_id=uuid.uuid4(),
        org_role="admin",
    )


@pytest.fixture
def viewer_client(mock_session: AsyncMock) -> Generator[TestClient, None, None]:
    yield from _make_test_client(
        mock_session,
        username="viewer",
        organisation_id=ORG_ID,
        account_id=uuid.uuid4(),
        org_role="viewer",
    )


@pytest.fixture
def runner_client(mock_session: AsyncMock) -> Generator[TestClient, None, None]:
    yield from _make_test_client(
        mock_session,
        username="runner",
        organisation_id=ORG_ID,
        account_id=uuid.uuid4(),
        org_role="runner",
    )


@pytest.fixture
def operator_client(mock_session: AsyncMock) -> Generator[TestClient, None, None]:
    yield from _make_test_client(
        mock_session,
        username="operator",
        organisation_id=ORG_ID,
        account_id=uuid.uuid4(),
        org_role="operator",
    )
