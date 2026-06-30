"""Unit tests for /api/v1/viewmodel/views and view integration in viewmodel/current."""

import uuid
from collections.abc import AsyncGenerator, Generator
from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from modulo.api.dependencies import _get_engine, get_db_session
from modulo.api.main import app
from modulo.auth.dependencies import get_current_user
from modulo.auth.jwt import AuthenticatedPrincipal
from modulo.settings import Settings, get_settings

_VALID_32 = "a" * 32
_ORG_ID = uuid.UUID("00000000-0000-0000-0000-000000000001")
_USER_ID = uuid.UUID("00000000-0000-0000-0000-000000000002")
_VIEW_ID = uuid.UUID("00000000-0000-0000-0000-000000000003")
_NOW = datetime(2025, 1, 1, tzinfo=UTC)


def _make_settings() -> Settings:
    return Settings(
        database_url="postgresql+asyncpg://localhost/test",
        secret_key=_VALID_32,
        fernet_key=_VALID_32,
        modulo_admin_password="testpass",
    )


def _make_view(**overrides: object) -> MagicMock:
    v = MagicMock()
    v.id = overrides.get("id", _VIEW_ID)
    v.organisation_id = overrides.get("organisation_id", _ORG_ID)
    v.name = overrides.get("name", "Test View")
    v.description = overrides.get("description", None)
    v.view_type = overrides.get("view_type", "run_list")
    v.filters = overrides.get("filters", {})
    v.columns = overrides.get("columns", None)
    v.sort_by = overrides.get("sort_by", None)
    v.sort_order = overrides.get("sort_order", "desc")
    v.created_by = overrides.get("created_by", _USER_ID)
    v.created_at = _NOW
    v.updated_at = _NOW
    return v


def _make_pipeline() -> MagicMock:
    p = MagicMock()
    p.id = uuid.uuid4()
    p.name = "Test Pipeline"
    p.visibility = "org"
    p.created_at = _NOW
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
    org.daily_spend_limit = overrides.get("daily_spend_limit", None)
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
    flag1.tier = "free"
    flag1.currently_active = True
    flag2 = MagicMock()
    flag2.name = "eval_system"
    flag2.description = "Built-in eval runner for LLM output quality gates"
    flag2.tier = "free"
    flag2.currently_active = True
    ctx.list_enabled_features = MagicMock(return_value=[flag1, flag2])
    return ctx


def _make_mock_session() -> AsyncMock:
    session = AsyncMock()
    begin_cm = AsyncMock()
    begin_cm.__aenter__ = AsyncMock(return_value=None)
    begin_cm.__aexit__ = AsyncMock(return_value=False)
    session.begin = MagicMock(return_value=begin_cm)

    execute_result = MagicMock()
    scalars_mock = MagicMock()
    scalars_mock.all = MagicMock(return_value=[])
    execute_result.scalars.return_value = scalars_mock
    execute_result.scalar_one_or_none = AsyncMock(return_value=None)
    session.execute = AsyncMock(return_value=execute_result)
    return session


@pytest.fixture()
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
    yield TestClient(app)
    app.dependency_overrides.clear()


@pytest.fixture()
def unauth_client() -> Generator[TestClient, None, None]:
    app.dependency_overrides[get_settings] = _make_settings
    yield TestClient(app)
    app.dependency_overrides.clear()


# ---------------------------------------------------------------------------
# GET /api/v1/viewmodel/views
# ---------------------------------------------------------------------------


class TestViewModelListViews:
    def test_returns_200_with_enriched_views(self, client: TestClient) -> None:
        run_view = _make_view(name="Run View", view_type="run_list", id=uuid.uuid4())
        pipeline_view = _make_view(name="Pipeline View", view_type="pipeline_list", id=uuid.uuid4())
        audit_view = _make_view(name="Audit View", view_type="audit_log", id=uuid.uuid4())

        views_page = MagicMock(items=[run_view, pipeline_view, audit_view], total=3, page=1, page_size=100)

        with (
            patch("modulo.api.routes.viewmodel.list_views", return_value=views_page),
            patch("modulo.api.routes.viewmodel.set_rls_org"),
            patch("modulo.api.routes.viewmodel.set_rls_user_context"),
        ):
            resp = client.get("/api/v1/viewmodel/views")

        assert resp.status_code == 200
        body = resp.json()
        assert body["total"] == 3
        assert len(body["items"]) == 3
        assert len(body["run_list_views"]) == 1
        assert len(body["pipeline_list_views"]) == 1
        assert len(body["audit_log_views"]) == 1

    def test_enriches_with_created_by_me(self, client: TestClient) -> None:
        own_view = _make_view(name="My View", view_type="run_list", created_by=_USER_ID, id=uuid.uuid4())
        other_view = _make_view(name="Other View", view_type="run_list", created_by=uuid.uuid4(), id=uuid.uuid4())

        views_page = MagicMock(items=[own_view, other_view], total=2, page=1, page_size=100)

        with (
            patch("modulo.api.routes.viewmodel.list_views", return_value=views_page),
            patch("modulo.api.routes.viewmodel.set_rls_org"),
            patch("modulo.api.routes.viewmodel.set_rls_user_context"),
        ):
            resp = client.get("/api/v1/viewmodel/views")

        assert resp.status_code == 200
        items = resp.json()["items"]
        my_item = next(i for i in items if i["name"] == "My View")
        other_item = next(i for i in items if i["name"] == "Other View")
        assert my_item["created_by_me"] is True
        assert other_item["created_by_me"] is False

    def test_returns_empty_when_no_views(self, client: TestClient) -> None:
        views_page = MagicMock(items=[], total=0, page=1, page_size=100)

        with (
            patch("modulo.api.routes.viewmodel.list_views", return_value=views_page),
            patch("modulo.api.routes.viewmodel.set_rls_org"),
            patch("modulo.api.routes.viewmodel.set_rls_user_context"),
        ):
            resp = client.get("/api/v1/viewmodel/views")

        assert resp.status_code == 200
        body = resp.json()
        assert body["total"] == 0
        assert len(body["items"]) == 0
        assert len(body["run_list_views"]) == 0
        assert len(body["pipeline_list_views"]) == 0
        assert len(body["audit_log_views"]) == 0

    def test_unauthorized_returns_4xx(self, unauth_client: TestClient) -> None:
        resp = unauth_client.get("/api/v1/viewmodel/views")
        assert resp.status_code in (401, 403)


# ---------------------------------------------------------------------------
# GET /api/v1/viewmodel/current — view integration
# ---------------------------------------------------------------------------


class TestViewModelCurrentViewIntegration:
    def test_includes_views_when_not_specified(self, client: TestClient) -> None:
        run_view = _make_view(name="Run View", view_type="run_list", id=uuid.uuid4())
        pipeline = _make_pipeline()
        run = _make_run()

        views_page = MagicMock(items=[run_view], total=1, page=1, page_size=100)
        pipelines_page = MagicMock(items=[pipeline], total=1, page=1, page_size=20)
        runs_page = MagicMock(items=[run], total=1, page=1, page_size=10)
        org = _make_org()
        user = _make_user()
        plan_ctx = _make_mock_plan_context()

        with (
            patch("modulo.api.routes.viewmodel.list_views", return_value=views_page),
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
        assert body["views"] is not None
        assert len(body["views"]) == 1
        assert body["current_view"] is None

    def test_includes_current_view_when_specified(self, client: TestClient) -> None:
        view_id = uuid.uuid4()
        view = _make_view(name="Active View", view_type="run_list", id=view_id, created_by=_USER_ID)
        pipeline = _make_pipeline()
        run = _make_run()

        views_page = MagicMock(items=[view], total=1, page=1, page_size=100)
        pipelines_page = MagicMock(items=[pipeline], total=1, page=1, page_size=20)
        runs_page = MagicMock(items=[run], total=1, page=1, page_size=10)
        org = _make_org()
        user = _make_user()
        plan_ctx = _make_mock_plan_context()

        with (
            patch("modulo.api.routes.viewmodel.list_views", return_value=views_page),
            patch("modulo.api.routes.viewmodel.list_pipelines", return_value=pipelines_page),
            patch("modulo.api.routes.viewmodel.list_runs", return_value=runs_page),
            patch("modulo.api.routes.viewmodel.set_rls_org"),
            patch("modulo.api.routes.viewmodel.set_rls_user_context"),
            patch("modulo.api.routes.viewmodel.get_organisation", return_value=org),
            patch("modulo.api.routes.viewmodel.get_account_by_id", return_value=user),
            patch("modulo.api.routes.viewmodel.list_team_memberships_for_account", return_value=[]),
            patch("modulo.api.routes.viewmodel.resolve_plan_context", return_value=plan_ctx),
            patch("modulo.api.routes.viewmodel.get_view", return_value=view),
        ):
            resp = client.get(f"/api/v1/viewmodel/current?current_view_id={view_id}")

        assert resp.status_code == 200
        body = resp.json()
        assert body["current_view"] is not None
        assert body["current_view"]["id"] == str(view_id)
        assert body["current_view"]["name"] == "Active View"
        assert body["current_view"]["created_by_me"] is True

    def test_ignores_missing_current_view(self, client: TestClient) -> None:
        pipeline = _make_pipeline()
        run = _make_run()

        views_page = MagicMock(items=[], total=0, page=1, page_size=100)
        pipelines_page = MagicMock(items=[pipeline], total=1, page=1, page_size=20)
        runs_page = MagicMock(items=[run], total=1, page=1, page_size=10)
        org = _make_org()
        user = _make_user()
        plan_ctx = _make_mock_plan_context()

        missing_id = uuid.uuid4()

        with (
            patch("modulo.api.routes.viewmodel.list_views", return_value=views_page),
            patch("modulo.api.routes.viewmodel.list_pipelines", return_value=pipelines_page),
            patch("modulo.api.routes.viewmodel.list_runs", return_value=runs_page),
            patch("modulo.api.routes.viewmodel.set_rls_org"),
            patch("modulo.api.routes.viewmodel.set_rls_user_context"),
            patch("modulo.api.routes.viewmodel.get_organisation", return_value=org),
            patch("modulo.api.routes.viewmodel.get_account_by_id", return_value=user),
            patch("modulo.api.routes.viewmodel.list_team_memberships_for_account", return_value=[]),
            patch("modulo.api.routes.viewmodel.resolve_plan_context", return_value=plan_ctx),
            patch("modulo.api.routes.viewmodel.get_view", return_value=None),
        ):
            resp = client.get(f"/api/v1/viewmodel/current?current_view_id={missing_id}")

        assert resp.status_code == 200
        body = resp.json()
        assert body["current_view"] is None
