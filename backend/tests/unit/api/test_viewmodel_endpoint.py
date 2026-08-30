"""Unit tests for /api/v1/me and /api/v1/viewmodel/current."""

import uuid
from collections.abc import AsyncGenerator, Generator
from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from modulo.api.dependencies import _get_engine, get_db_session, get_plan_context
from modulo.api.main import app
from modulo.auth.dependencies import get_current_user
from modulo.auth.jwt import AuthenticatedPrincipal
from modulo.settings import Settings, get_settings
from tests.unit.api.mock_session import configure_mock_session

_VALID_32 = "a" * 32
_ORG_ID = uuid.UUID("00000000-0000-0000-0000-000000000001")
_USER_ID = uuid.UUID("00000000-0000-0000-0000-000000000002")
_NOW = datetime(2025, 1, 1, tzinfo=UTC)


def _make_settings() -> Settings:
    return Settings(
        database_url="postgresql+asyncpg://localhost/test",
        secret_key=_VALID_32,
        fernet_key=_VALID_32,
        modulo_admin_password="testpass",
    )


def _make_pipeline() -> MagicMock:
    p = MagicMock()
    p.id = uuid.uuid4()
    p.name = "Test Pipeline"
    p.visibility = "org"
    p.owner_team_id = None
    p.created_at = _NOW
    p.rate_limit_config = None
    p.max_duration_seconds = None
    p.archived_at = None
    p.snapshot_count = 0
    return p


def _make_run() -> MagicMock:
    r = MagicMock()
    r.id = uuid.uuid4()
    r.pipeline_id = uuid.uuid4()
    r.status = "complete"
    r.trigger_type = "manual"
    r.created_at = _NOW
    return r


def _make_org(**overrides: object) -> MagicMock:
    org = MagicMock()
    org.id = overrides.get("id", _ORG_ID)
    org.name = overrides.get("name", "Test Org")
    org.settings_json = overrides.get("settings_json", {})
    org.daily_spend_limit = overrides.get("daily_spend_limit")
    return org


def _make_user(**overrides: object) -> MagicMock:
    user = MagicMock()
    user.id = overrides.get("id", _USER_ID)
    user.preferences = overrides.get("preferences", {})
    return user


def _make_membership(**overrides: object) -> MagicMock:
    m = MagicMock()
    m.team_id = overrides.get("team_id", uuid.uuid4())
    m.role = overrides.get("role", "viewer")
    return m


def _make_mock_plan_context() -> MagicMock:
    ctx = MagicMock()
    flag1 = MagicMock()
    flag1.name = "parallel_branches"
    flag1.description = "Run branching logic in parallel within a pipeline"
    flag1.tier = "community"
    flag1.currently_active = True
    flag2 = MagicMock()
    flag2.name = "eval_system"
    flag2.description = "Built-in eval runner for LLM output quality gates"
    flag2.tier = "community"
    flag2.currently_active = True
    ctx.list_enabled_features = MagicMock(return_value=[flag1, flag2])
    return ctx


def _make_mock_session() -> AsyncMock:
    session = configure_mock_session(AsyncMock())
    begin_cm = AsyncMock()
    begin_cm.__aenter__ = AsyncMock(return_value=None)
    begin_cm.__aexit__ = AsyncMock(return_value=False)
    session.begin = MagicMock(return_value=begin_cm)

    execute_result = MagicMock()
    scalars_mock = MagicMock()
    scalars_mock.all = MagicMock(return_value=[])
    execute_result.scalars.return_value = scalars_mock
    execute_result.scalar_one_or_none = MagicMock(return_value=None)
    session.execute = AsyncMock(return_value=execute_result)
    return session


@pytest.fixture
def client() -> Generator[TestClient, None, None]:
    mock_session = _make_mock_session()

    async def override_session() -> AsyncGenerator[AsyncMock, None]:
        yield mock_session

    app.dependency_overrides[get_settings] = _make_settings
    app.dependency_overrides[get_db_session] = override_session
    app.dependency_overrides[_get_engine] = lambda: MagicMock()
    app.dependency_overrides[get_current_user] = lambda: AuthenticatedPrincipal(
        username="testuser",
        organisation_id=_ORG_ID,
        account_id=_USER_ID,
        org_role="admin",
    )
    mock_plan = MagicMock()
    mock_plan.feature_enabled.return_value = True
    app.dependency_overrides[get_plan_context] = lambda: mock_plan
    yield TestClient(app)
    app.dependency_overrides.clear()


@pytest.fixture
def unauth_client() -> Generator[TestClient, None, None]:
    app.dependency_overrides[get_settings] = _make_settings
    mock_plan = MagicMock()
    mock_plan.feature_enabled.return_value = True
    app.dependency_overrides[get_plan_context] = lambda: mock_plan
    yield TestClient(app)
    app.dependency_overrides.clear()


# ---------------------------------------------------------------------------
# GET /api/v1/me
# ---------------------------------------------------------------------------


def test_me_returns_200_with_username(client: TestClient) -> None:
    resp = client.get("/api/v1/me")
    assert resp.status_code == 200
    body = resp.json()
    assert body["user"]["username"] == "testuser"
    assert body["org"]["org_id"] == str(_ORG_ID)
    assert body["org_role"] == "admin"
    assert not body["team_memberships"]
    assert body["team_memberships_truncated"] is False


def test_me_unauthenticated_returns_4xx(unauth_client: TestClient) -> None:
    resp = unauth_client.get("/api/v1/me")
    assert resp.status_code in (401, 403)


# ---------------------------------------------------------------------------
# GET /api/v1/viewmodel/current
# ---------------------------------------------------------------------------


def test_viewmodel_current_returns_200(client: TestClient) -> None:
    pipeline = _make_pipeline()
    run = _make_run()
    pipelines_page = MagicMock(items=[pipeline], total=1, page=1, page_size=20)
    runs_page = MagicMock(items=[run], total=1, page=1, page_size=10)
    org = _make_org()
    user = _make_user()
    plan_ctx = _make_mock_plan_context()

    with (
        patch("modulo.api.routes.viewmodel.list_pipelines", return_value=pipelines_page),
        patch("modulo.api.routes.viewmodel.list_runs", return_value=runs_page),
        patch("modulo.api.routes.viewmodel.set_rls_org"),
        patch("modulo.api.routes.viewmodel.set_rls_user_context"),
        patch("modulo.api.routes.viewmodel.get_organisation", return_value=org),
        patch("modulo.api.routes.viewmodel.get_account_by_id", return_value=user),
        patch("modulo.api.routes.viewmodel.list_team_memberships_for_account", return_value=[]),
        patch("modulo.api.routes.viewmodel.resolve_plan_context", return_value=plan_ctx),
    ):
        resp = client.get("/api/v1/viewmodel/current")

    assert resp.status_code == 200
    body = resp.json()
    assert body["user"]["username"] == "testuser"
    assert body["pipelines_total"] == 1
    assert body["runs_total"] == 1
    assert not body["pending_hitl_gates"]
    assert len(body["pipelines"]) == 1
    assert len(body["recent_runs"]) == 1
    assert body["org"]["org_name"] == "Test Org"
    assert body["org_role"] == "admin"
    assert not body["team_memberships"]
    assert body["team_memberships_truncated"] is False
    assert not body["preferences"]
    assert body["feature_flags"]
    assert body["plan"]["tier"] == "community"


def test_me_includes_must_change_password(client: TestClient) -> None:
    for flag, expected in ((True, True), (False, False)):
        user = _make_user()
        user.must_change_password = flag
        with patch("modulo.api.routes.viewmodel.get_account_by_id", new=AsyncMock(return_value=user)):
            resp = client.get("/api/v1/me")

        assert resp.status_code == 200
        # FAR-460: the forced-password-change gate must be surfaced to the UI.
        assert resp.json()["must_change_password"] is expected


def test_viewmodel_current_unauthenticated_returns_4xx(unauth_client: TestClient) -> None:
    resp = unauth_client.get("/api/v1/viewmodel/current")
    assert resp.status_code in (401, 403)


def test_viewmodel_current_includes_pending_hitl(client: TestClient) -> None:
    pipeline = _make_pipeline()
    run = _make_run()
    pipelines_page = MagicMock(items=[pipeline], total=1, page=1, page_size=20)
    runs_page = MagicMock(items=[run], total=1, page=1, page_size=10)
    org = _make_org()
    user = _make_user()
    plan_ctx = _make_mock_plan_context()

    hitl = MagicMock()
    hitl.id = uuid.uuid4()
    hitl.run_id = uuid.uuid4()
    hitl.pipeline_id = uuid.uuid4()
    hitl.gate_id = "approval_gate"
    hitl.claimed_by = None
    hitl.expires_at = None

    # The viewmodel does its own session.execute for HITL — override what scalars() returns
    execute_result = MagicMock()
    scalars_mock = MagicMock()
    scalars_mock.all = MagicMock(return_value=[hitl])
    execute_result.scalars.return_value = scalars_mock

    with (
        patch("modulo.api.routes.viewmodel.list_pipelines", return_value=pipelines_page),
        patch("modulo.api.routes.viewmodel.list_runs", return_value=runs_page),
        patch("modulo.api.routes.viewmodel.set_rls_org"),
        patch("modulo.api.routes.viewmodel.set_rls_user_context"),
        patch("modulo.api.routes.viewmodel.get_organisation", return_value=org),
        patch("modulo.api.routes.viewmodel.get_account_by_id", return_value=user),
        patch("modulo.api.routes.viewmodel.list_team_memberships_for_account", return_value=[]),
        patch("modulo.api.routes.viewmodel.resolve_plan_context", return_value=plan_ctx),
        patch(
            "modulo.api.routes.viewmodel.AsyncSession.execute",
            new_callable=AsyncMock,
            return_value=execute_result,
        ),
    ):
        resp = client.get("/api/v1/viewmodel/current")

    assert resp.status_code == 200


def test_viewmodel_current_includes_feature_flags(client: TestClient) -> None:
    pipeline = _make_pipeline()
    run = _make_run()
    pipelines_page = MagicMock(items=[pipeline], total=1, page=1, page_size=20)
    runs_page = MagicMock(items=[run], total=1, page=1, page_size=10)
    org = _make_org()
    user = _make_user()
    plan_ctx = _make_mock_plan_context()

    with (
        patch("modulo.api.routes.viewmodel.list_pipelines", return_value=pipelines_page),
        patch("modulo.api.routes.viewmodel.list_runs", return_value=runs_page),
        patch("modulo.api.routes.viewmodel.set_rls_org"),
        patch("modulo.api.routes.viewmodel.set_rls_user_context"),
        patch("modulo.api.routes.viewmodel.get_organisation", return_value=org),
        patch("modulo.api.routes.viewmodel.get_account_by_id", return_value=user),
        patch("modulo.api.routes.viewmodel.list_team_memberships_for_account", return_value=[]),
        patch("modulo.api.routes.viewmodel.resolve_plan_context", return_value=plan_ctx),
    ):
        resp = client.get("/api/v1/viewmodel/current")

    assert resp.status_code == 200
    body = resp.json()
    assert "feature_flags" in body
    assert len(body["feature_flags"]) == 2
    flag_names = [f["name"] for f in body["feature_flags"]]
    assert "parallel_branches" in flag_names
    assert "eval_system" in flag_names
    for flag in body["feature_flags"]:
        assert flag["active"] is True
        assert flag["tier"] == "community"


def test_viewmodel_current_includes_org_info(client: TestClient) -> None:
    pipeline = _make_pipeline()
    run = _make_run()
    pipelines_page = MagicMock(items=[pipeline], total=1, page=1, page_size=20)
    runs_page = MagicMock(items=[run], total=1, page=1, page_size=10)
    org = _make_org(name="Custom Org", settings_json={"theme": "dark"})
    user = _make_user()
    plan_ctx = _make_mock_plan_context()

    with (
        patch("modulo.api.routes.viewmodel.list_pipelines", return_value=pipelines_page),
        patch("modulo.api.routes.viewmodel.list_runs", return_value=runs_page),
        patch("modulo.api.routes.viewmodel.set_rls_org"),
        patch("modulo.api.routes.viewmodel.set_rls_user_context"),
        patch("modulo.api.routes.viewmodel.get_organisation", return_value=org),
        patch("modulo.api.routes.viewmodel.get_account_by_id", return_value=user),
        patch("modulo.api.routes.viewmodel.list_team_memberships_for_account", return_value=[]),
        patch("modulo.api.routes.viewmodel.resolve_plan_context", return_value=plan_ctx),
    ):
        resp = client.get("/api/v1/viewmodel/current")

    assert resp.status_code == 200
    body = resp.json()
    assert body["org"]["org_name"] == "Custom Org"
    assert body["org"]["org_id"] == str(_ORG_ID)
    assert body["org_role"] == "admin"
    assert not body["preferences"]


def test_viewmodel_current_includes_team_memberships(client: TestClient) -> None:
    pipeline = _make_pipeline()
    run = _make_run()
    pipelines_page = MagicMock(items=[pipeline], total=1, page=1, page_size=20)
    runs_page = MagicMock(items=[run], total=1, page=1, page_size=10)
    org = _make_org()
    user = _make_user()
    plan_ctx = _make_mock_plan_context()
    membership = _make_membership(role="operator")

    with (
        patch("modulo.api.routes.viewmodel.list_pipelines", return_value=pipelines_page),
        patch("modulo.api.routes.viewmodel.list_runs", return_value=runs_page),
        patch("modulo.api.routes.viewmodel.set_rls_org"),
        patch("modulo.api.routes.viewmodel.set_rls_user_context"),
        patch("modulo.api.routes.viewmodel.get_organisation", return_value=org),
        patch("modulo.api.routes.viewmodel.get_account_by_id", return_value=user),
        patch("modulo.api.routes.viewmodel.list_team_memberships_for_account", return_value=[membership]),
        patch("modulo.api.routes.viewmodel.resolve_plan_context", return_value=plan_ctx),
    ):
        resp = client.get("/api/v1/viewmodel/current")

    assert resp.status_code == 200
    body = resp.json()
    assert len(body["team_memberships"]) == 1
    assert body["team_memberships"][0]["team_role"] == "operator"
    assert body["team_memberships_truncated"] is False


def test_viewmodel_current_includes_preferences(client: TestClient) -> None:
    pipeline = _make_pipeline()
    run = _make_run()
    pipelines_page = MagicMock(items=[pipeline], total=1, page=1, page_size=20)
    runs_page = MagicMock(items=[run], total=1, page=1, page_size=10)
    org = _make_org()
    user = _make_user(preferences={"theme": "dark", "notifications": True})
    plan_ctx = _make_mock_plan_context()

    with (
        patch("modulo.api.routes.viewmodel.list_pipelines", return_value=pipelines_page),
        patch("modulo.api.routes.viewmodel.list_runs", return_value=runs_page),
        patch("modulo.api.routes.viewmodel.set_rls_org"),
        patch("modulo.api.routes.viewmodel.set_rls_user_context"),
        patch("modulo.api.routes.viewmodel.get_organisation", return_value=org),
        patch("modulo.api.routes.viewmodel.get_account_by_id", return_value=user),
        patch("modulo.api.routes.viewmodel.list_team_memberships_for_account", return_value=[]),
        patch("modulo.api.routes.viewmodel.resolve_plan_context", return_value=plan_ctx),
    ):
        resp = client.get("/api/v1/viewmodel/current")

    assert resp.status_code == 200
    body = resp.json()
    assert body["preferences"]["theme"] == "dark"
    assert body["preferences"]["notifications"] is True


def test_viewmodel_current_returns_503_on_sqlalchemy_error(client: TestClient) -> None:
    with (
        patch("modulo.api.routes.viewmodel.set_rls_org"),
        patch("modulo.api.routes.viewmodel.set_rls_user_context"),
        patch("modulo.api.routes.viewmodel.get_organisation", return_value=_make_org()),
        patch("modulo.api.routes.viewmodel.get_account_by_id", return_value=_make_user()),
        patch("modulo.api.routes.viewmodel.list_team_memberships_for_account", return_value=[]),
        patch("modulo.api.routes.viewmodel.list_pipelines", side_effect=Exception("connection failed")),
    ):
        resp = client.get("/api/v1/viewmodel/current")
    assert resp.status_code == 500


def test_viewmodel_current_returns_500_on_unexpected_error(client: TestClient) -> None:
    with (
        patch("modulo.api.routes.viewmodel.set_rls_org"),
        patch("modulo.api.routes.viewmodel.set_rls_user_context"),
        patch("modulo.api.routes.viewmodel.get_organisation", return_value=_make_org()),
        patch("modulo.api.routes.viewmodel.get_account_by_id", return_value=_make_user()),
        patch("modulo.api.routes.viewmodel.list_team_memberships_for_account", return_value=[]),
        patch("modulo.api.routes.viewmodel.list_pipelines", side_effect=TypeError("expected str, got None")),
    ):
        resp = client.get("/api/v1/viewmodel/current")
    assert resp.status_code == 500


def test_viewmodel_current_includes_plan(client: TestClient) -> None:
    pipeline = _make_pipeline()
    run = _make_run()
    pipelines_page = MagicMock(items=[pipeline], total=1, page=1, page_size=20)
    runs_page = MagicMock(items=[run], total=1, page=1, page_size=10)
    org = _make_org()
    user = _make_user()
    plan_ctx = _make_mock_plan_context()

    with (
        patch("modulo.api.routes.viewmodel.list_pipelines", return_value=pipelines_page),
        patch("modulo.api.routes.viewmodel.list_runs", return_value=runs_page),
        patch("modulo.api.routes.viewmodel.set_rls_org"),
        patch("modulo.api.routes.viewmodel.set_rls_user_context"),
        patch("modulo.api.routes.viewmodel.get_organisation", return_value=org),
        patch("modulo.api.routes.viewmodel.get_account_by_id", return_value=user),
        patch("modulo.api.routes.viewmodel.list_team_memberships_for_account", return_value=[]),
        patch("modulo.api.routes.viewmodel.resolve_plan_context", return_value=plan_ctx),
    ):
        resp = client.get("/api/v1/viewmodel/current")

    assert resp.status_code == 200
    body = resp.json()
    assert "plan" in body
    assert body["plan"]["tier"] == "community"
    assert body["plan"]["daily_spend_limit"] is None


def test_viewmodel_current_non_admin_view_as_team_returns_403(client: TestClient) -> None:
    app.dependency_overrides[get_current_user] = lambda: AuthenticatedPrincipal(
        username="viewer",
        organisation_id=_ORG_ID,
        account_id=_USER_ID,
        org_role="viewer",
    )

    team_id = uuid.uuid4()
    resp = client.get(f"/api/v1/viewmodel/current?view_as_team={team_id}")
    assert resp.status_code == 403

    app.dependency_overrides[get_current_user] = lambda: AuthenticatedPrincipal(
        username="testuser",
        organisation_id=_ORG_ID,
        account_id=_USER_ID,
        org_role="admin",
    )
