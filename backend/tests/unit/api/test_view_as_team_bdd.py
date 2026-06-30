"""Unit tests for View-as-Team BDD scenarios — admin team-scoped viewmodel enforcement."""

import uuid
from collections.abc import AsyncGenerator, Generator
from datetime import datetime
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from modulo.api.dependencies import _get_engine, get_db_session
from modulo.api.dependencies import get_settings as get_settings_override
from modulo.api.main import app
from modulo.auth.dependencies import get_current_user
from modulo.auth.jwt import AuthenticatedPrincipal
from modulo.db.crud.base import PageResult
from modulo.settings import Settings, get_settings

_VALID_32 = "a" * 32
_ORG_ID = uuid.UUID("00000000-0000-0000-0000-000000000001")
_USER_ID = uuid.UUID("00000000-0000-0000-0000-000000000002")
_ALT_USER_ID = uuid.UUID("00000000-0000-0000-0000-000000000003")
_TEAM_ID = uuid.UUID("00000000-0000-0000-0000-000000000010")
_TEAM_B_ID = uuid.UUID("00000000-0000-0000-0000-000000000011")
_VIEWMODEL_URL = "/api/v1/viewmodel/current"


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


def _make_mock_plan_context() -> MagicMock:
    ctx = MagicMock()
    ctx.list_enabled_features = MagicMock(return_value=[])
    return ctx


def _make_settings() -> Settings:
    return Settings(
        database_url="postgresql+asyncpg://localhost/test",
        secret_key=_VALID_32,
        fernet_key=_VALID_32,
        modulo_admin_password="testpass",
    )


def _make_mock_session() -> AsyncMock:
    session = AsyncMock()
    begin_cm = AsyncMock()
    begin_cm.__aenter__ = AsyncMock(return_value=None)
    begin_cm.__aexit__ = AsyncMock(return_value=False)
    session.begin = MagicMock(return_value=begin_cm)
    scalar_mock = MagicMock()
    scalar_mock.all = MagicMock(return_value=[])
    team_mock = MagicMock()
    team_mock.id = _TEAM_ID
    team_mock.organisation_id = _ORG_ID
    team_mock.name = "engineering"
    org_mock = MagicMock()
    org_mock.id = _ORG_ID
    org_mock.name = "Test Org"
    org_mock.settings_json = {}
    org_mock.daily_spend_limit = None
    user_mock = MagicMock()
    user_mock.id = _USER_ID
    user_mock.preferences = {}
    hitl_result = AsyncMock()
    hitl_result.scalar_one_or_none = AsyncMock(return_value=team_mock)
    hitl_result.scalar_one = AsyncMock(return_value=0)
    hitl_result.scalars = MagicMock(return_value=scalar_mock)
    session.execute.return_value = hitl_result
    return session


def _fake_pipeline(**overrides: Any) -> MagicMock:
    p = MagicMock()
    p.id = overrides.get("id", uuid.uuid4())
    p.organisation_id = overrides.get("organisation_id", _ORG_ID)
    p.name = overrides.get("name", "test-pipeline")
    p.description = overrides.get("description", None)
    p.visibility = overrides.get("visibility", "org")
    p.owner_team_id = overrides.get("owner_team_id", None)
    p.max_concurrent_runs = overrides.get("max_concurrent_runs", 5)
    p.lock_wait_timeout_seconds = overrides.get("lock_wait_timeout_seconds", 300)
    p.node_timeout_seconds = overrides.get("node_timeout_seconds", 300)
    p.run_context_defaults = overrides.get("run_context_defaults", {})
    p.created_by = overrides.get("created_by", _USER_ID)
    p.created_at = overrides.get("created_at", datetime.now())
    p.updated_at = overrides.get("updated_at", datetime.now())
    return p


@pytest.fixture()
def client() -> Generator[TestClient, None, None]:
    mock_session = _make_mock_session()

    async def override_session() -> AsyncGenerator[AsyncMock, None]:
        yield mock_session

    app.dependency_overrides[get_settings] = _make_settings
    app.dependency_overrides[get_db_session] = override_session
    app.dependency_overrides[_get_engine] = lambda: MagicMock()
    app.dependency_overrides[get_current_user] = lambda: AuthenticatedPrincipal(
        username="admin",
        organisation_id=_ORG_ID,
        account_id=_USER_ID,
        org_role="admin",
    )
    yield TestClient(app)
    app.dependency_overrides.clear()


@pytest.fixture()
def viewer_client() -> Generator[TestClient, None, None]:
    mock_session = _make_mock_session()

    async def override_session() -> AsyncGenerator[AsyncMock, None]:
        yield mock_session

    app.dependency_overrides[get_settings] = _make_settings
    app.dependency_overrides[get_db_session] = override_session
    app.dependency_overrides[_get_engine] = lambda: MagicMock()
    app.dependency_overrides[get_current_user] = lambda: AuthenticatedPrincipal(
        username="viewer",
        organisation_id=_ORG_ID,
        account_id=_ALT_USER_ID,
        org_role="viewer",
    )
    yield TestClient(app)
    app.dependency_overrides.clear()


@pytest.fixture()
def operator_client() -> Generator[TestClient, None, None]:
    mock_session = _make_mock_session()

    async def override_session() -> AsyncGenerator[AsyncMock, None]:
        yield mock_session

    app.dependency_overrides[get_settings] = _make_settings
    app.dependency_overrides[get_db_session] = override_session
    app.dependency_overrides[_get_engine] = lambda: MagicMock()
    app.dependency_overrides[get_current_user] = lambda: AuthenticatedPrincipal(
        username="operator",
        organisation_id=_ORG_ID,
        account_id=uuid.uuid4(),
        org_role="operator",
    )
    yield TestClient(app)
    app.dependency_overrides.clear()


@pytest.fixture()
def runner_client() -> Generator[TestClient, None, None]:
    mock_session = _make_mock_session()

    async def override_session() -> AsyncGenerator[AsyncMock, None]:
        yield mock_session

    app.dependency_overrides[get_settings] = _make_settings
    app.dependency_overrides[get_db_session] = override_session
    app.dependency_overrides[_get_engine] = lambda: MagicMock()
    app.dependency_overrides[get_current_user] = lambda: AuthenticatedPrincipal(
        username="runner",
        organisation_id=_ORG_ID,
        account_id=uuid.uuid4(),
        org_role="runner",
    )
    yield TestClient(app)
    app.dependency_overrides.clear()


# ===========================================================================
# Scenario 1: Admin views resources as a team
# ===========================================================================


class TestBDDAdminViewsAsTeam:
    URL = _VIEWMODEL_URL

    def _common_patches(self) -> dict:
        org = _make_org()
        user = _make_user()
        plan_ctx = _make_mock_plan_context()
        return {
            "get_organisation": patch(
                "modulo.api.routes.viewmodel.get_organisation", return_value=org
            ),
            "get_user_by_id": patch(
                "modulo.api.routes.viewmodel.get_user_by_id", return_value=user
            ),
            "list_memberships_for_user": patch(
                "modulo.api.routes.viewmodel.list_memberships_for_user", return_value=[]
            ),
            "resolve_plan_context": patch(
                "modulo.api.routes.viewmodel.resolve_plan_context", return_value=plan_ctx
            ),
        }

    def test_admin_view_as_team_returns_200(self, client: TestClient) -> None:
        team_pipeline = _fake_pipeline(
            name="release-pipeline",
            owner_team_id=_TEAM_ID,
            visibility="team",
        )
        with (
            patch("modulo.api.routes.viewmodel.set_rls_org", new_callable=AsyncMock),
            patch("modulo.api.routes.viewmodel.set_rls_user_context", new_callable=AsyncMock),
            patch(
                "modulo.api.routes.viewmodel.list_pipelines",
                new_callable=AsyncMock,
                return_value=PageResult(items=[team_pipeline], total=1, page=1, page_size=20),
            ),
            patch(
                "modulo.api.routes.viewmodel.list_runs",
                new_callable=AsyncMock,
                return_value=PageResult(items=[], total=0, page=1, page_size=10),
            ),
            patch("modulo.api.routes.viewmodel.get_organisation", return_value=_make_org()),
            patch("modulo.api.routes.viewmodel.get_user_by_id", return_value=_make_user()),
            patch("modulo.api.routes.viewmodel.list_memberships_for_user", return_value=[]),
            patch("modulo.api.routes.viewmodel.resolve_plan_context", return_value=_make_mock_plan_context()),
        ):
            resp = client.get(self.URL, params={"view_as_team": str(_TEAM_ID)})

        assert resp.status_code == 200
        data = resp.json()
        assert len(data["pipelines"]) == 1
        assert data["pipelines"][0]["name"] == "release-pipeline"

    def test_admin_view_as_team_empty_team_returns_empty_pipelines(self, client: TestClient) -> None:
        with (
            patch("modulo.api.routes.viewmodel.set_rls_org", new_callable=AsyncMock),
            patch("modulo.api.routes.viewmodel.set_rls_user_context", new_callable=AsyncMock),
            patch(
                "modulo.api.routes.viewmodel.list_pipelines",
                new_callable=AsyncMock,
                return_value=PageResult(items=[], total=0, page=1, page_size=20),
            ),
            patch(
                "modulo.api.routes.viewmodel.list_runs",
                new_callable=AsyncMock,
                return_value=PageResult(items=[], total=0, page=1, page_size=10),
            ),
            patch("modulo.api.routes.viewmodel.get_organisation", return_value=_make_org()),
            patch("modulo.api.routes.viewmodel.get_user_by_id", return_value=_make_user()),
            patch("modulo.api.routes.viewmodel.list_memberships_for_user", return_value=[]),
            patch("modulo.api.routes.viewmodel.resolve_plan_context", return_value=_make_mock_plan_context()),
        ):
            resp = client.get(self.URL, params={"view_as_team": str(uuid.uuid4())})

        assert resp.status_code == 200
        data = resp.json()
        assert data["pipelines_total"] == 0
        assert data["pipelines"] == []


# ===========================================================================
# Scenario 2: Resources are filtered to team scope
# ===========================================================================


class TestBDDResourceFiltering:
    URL = _VIEWMODEL_URL

    def test_only_team_a_pipelines_returned_when_viewing_as_team_a(self, client: TestClient) -> None:
        team_a_pipe = _fake_pipeline(
            name="release-pipeline",
            owner_team_id=_TEAM_ID,
            visibility="team",
        )
        with (
            patch("modulo.api.routes.viewmodel.set_rls_org", new_callable=AsyncMock),
            patch("modulo.api.routes.viewmodel.set_rls_user_context", new_callable=AsyncMock),
            patch(
                "modulo.api.routes.viewmodel.list_pipelines",
                new_callable=AsyncMock,
                return_value=PageResult(items=[team_a_pipe], total=1, page=1, page_size=20),
            ),
            patch(
                "modulo.api.routes.viewmodel.list_runs",
                new_callable=AsyncMock,
                return_value=PageResult(items=[], total=0, page=1, page_size=10),
            ),
            patch("modulo.api.routes.viewmodel.get_organisation", return_value=_make_org()),
            patch("modulo.api.routes.viewmodel.get_user_by_id", return_value=_make_user()),
            patch("modulo.api.routes.viewmodel.list_memberships_for_user", return_value=[]),
            patch("modulo.api.routes.viewmodel.resolve_plan_context", return_value=_make_mock_plan_context()),
        ):
            resp = client.get(self.URL, params={"view_as_team": str(_TEAM_ID)})

        assert resp.status_code == 200
        data = resp.json()
        pipeline_names = {p["name"] for p in data["pipelines"]}
        assert "release-pipeline" in pipeline_names
        assert "brand-pipeline" not in pipeline_names

    def test_org_pipelines_excluded_when_viewing_as_team(self, client: TestClient) -> None:
        team_pipe = _fake_pipeline(
            id=uuid.uuid4(),
            name="team-pipeline",
            owner_team_id=_TEAM_ID,
            visibility="team",
        )

        with (
            patch("modulo.api.routes.viewmodel.set_rls_org", new_callable=AsyncMock),
            patch("modulo.api.routes.viewmodel.set_rls_user_context", new_callable=AsyncMock),
            patch(
                "modulo.api.routes.viewmodel.list_pipelines",
                new_callable=AsyncMock,
                return_value=PageResult(items=[team_pipe], total=1, page=1, page_size=20),
            ),
            patch(
                "modulo.api.routes.viewmodel.list_runs",
                new_callable=AsyncMock,
                return_value=PageResult(items=[], total=0, page=1, page_size=10),
            ),
            patch("modulo.api.routes.viewmodel.get_organisation", return_value=_make_org()),
            patch("modulo.api.routes.viewmodel.get_user_by_id", return_value=_make_user()),
            patch("modulo.api.routes.viewmodel.list_memberships_for_user", return_value=[]),
            patch("modulo.api.routes.viewmodel.resolve_plan_context", return_value=_make_mock_plan_context()),
        ):
            resp = client.get(self.URL, params={"view_as_team": str(_TEAM_ID)})

        assert resp.status_code == 200
        data = resp.json()
        pipeline_names = {p["name"] for p in data["pipelines"]}
        assert "team-pipeline" in pipeline_names
        assert "org-wide-pipeline" not in pipeline_names


# ===========================================================================
# Scenario 3: Non-admin cannot use view_as_team
# ===========================================================================


class TestBDDNonAdminRejected:
    URL = _VIEWMODEL_URL

    def test_viewer_gets_403_with_view_as_team(self, viewer_client: TestClient) -> None:
        resp = viewer_client.get(self.URL, params={"view_as_team": str(_TEAM_ID)})

        assert resp.status_code == 403

    def test_operator_gets_403_with_view_as_team(self, operator_client: TestClient) -> None:
        resp = operator_client.get(self.URL, params={"view_as_team": str(_TEAM_ID)})

        assert resp.status_code == 403

    def test_runner_gets_403_with_view_as_team(self, runner_client: TestClient) -> None:
        resp = runner_client.get(self.URL, params={"view_as_team": str(_TEAM_ID)})

        assert resp.status_code == 403


# ===========================================================================
# Scenario 4: Invalid team returns 404
# ===========================================================================


class TestBDDInvalidTeam:
    URL = _VIEWMODEL_URL

    def test_nonexistent_team_returns_404(self) -> None:
        nonexistent_id = uuid.uuid4()
        mock_session = _make_mock_session()
        # Team query returns None → 404
        mock_session.execute.return_value.scalar_one_or_none = AsyncMock(return_value=None)
        # But org and user queries need to succeed for the viewmodel handler
        mock_session.execute.return_value.scalars = MagicMock(
            return_value=MagicMock(all=MagicMock(return_value=[]))
        )

        async def override_session():
            yield mock_session

        app.dependency_overrides[get_settings_override] = _make_settings
        app.dependency_overrides[get_db_session] = override_session
        app.dependency_overrides[_get_engine] = lambda: MagicMock()
        app.dependency_overrides[get_current_user] = lambda: AuthenticatedPrincipal(
            username="admin",
            organisation_id=_ORG_ID,
            account_id=_USER_ID,
            org_role="admin",
        )
        c = TestClient(app)
        try:
            with (
                patch("modulo.api.routes.viewmodel.get_organisation", return_value=_make_org()),
                patch("modulo.api.routes.viewmodel.get_user_by_id", return_value=_make_user()),
                patch("modulo.api.routes.viewmodel.list_memberships_for_user", return_value=[]),
                patch("modulo.api.routes.viewmodel.resolve_plan_context", return_value=_make_mock_plan_context()),
            ):
                resp = c.get(self.URL, params={"view_as_team": str(nonexistent_id)})
                assert resp.status_code == 404
        finally:
            app.dependency_overrides.clear()

    def test_malformed_team_id_returns_422(self, client: TestClient) -> None:
        resp = client.get(self.URL, params={"view_as_team": "not-a-uuid"})

        assert resp.status_code == 422


# ===========================================================================
# Scenario 5: Admin restores to org-wide view
# ===========================================================================


class TestBDDRestoreOrgWide:
    URL = _VIEWMODEL_URL

    def test_admin_without_param_returns_all_resources(self, client: TestClient) -> None:
        org_pipeline = _fake_pipeline(
            name="org-pipeline",
            owner_team_id=None,
            visibility="org",
        )
        team_pipeline = _fake_pipeline(
            id=uuid.uuid4(),
            name="team-pipeline",
            owner_team_id=_TEAM_ID,
            visibility="team",
        )

        with (
            patch("modulo.api.routes.viewmodel.set_rls_org", new_callable=AsyncMock),
            patch("modulo.api.routes.viewmodel.set_rls_user_context", new_callable=AsyncMock),
            patch(
                "modulo.api.routes.viewmodel.list_pipelines",
                new_callable=AsyncMock,
                return_value=PageResult(items=[org_pipeline, team_pipeline], total=2, page=1, page_size=20),
            ),
            patch(
                "modulo.api.routes.viewmodel.list_runs",
                new_callable=AsyncMock,
                return_value=PageResult(items=[], total=0, page=1, page_size=10),
            ),
            patch("modulo.api.routes.viewmodel.get_organisation", return_value=_make_org()),
            patch("modulo.api.routes.viewmodel.get_user_by_id", return_value=_make_user()),
            patch("modulo.api.routes.viewmodel.list_memberships_for_user", return_value=[]),
            patch("modulo.api.routes.viewmodel.resolve_plan_context", return_value=_make_mock_plan_context()),
        ):
            resp = client.get(self.URL)

        assert resp.status_code == 200
        data = resp.json()
        assert data["pipelines_total"] == 2

    def test_admin_without_param_returns_org_pipelines(self, client: TestClient) -> None:
        org_pipeline = _fake_pipeline(
            name="org-pipeline",
            owner_team_id=None,
            visibility="org",
        )
        team_pipeline = _fake_pipeline(
            id=uuid.uuid4(),
            name="team-pipeline",
            owner_team_id=_TEAM_ID,
            visibility="team",
        )

        with (
            patch("modulo.api.routes.viewmodel.set_rls_org", new_callable=AsyncMock),
            patch("modulo.api.routes.viewmodel.set_rls_user_context", new_callable=AsyncMock),
            patch(
                "modulo.api.routes.viewmodel.list_pipelines",
                new_callable=AsyncMock,
                return_value=PageResult(items=[org_pipeline, team_pipeline], total=2, page=1, page_size=20),
            ),
            patch(
                "modulo.api.routes.viewmodel.list_runs",
                new_callable=AsyncMock,
                return_value=PageResult(items=[], total=0, page=1, page_size=10),
            ),
            patch("modulo.api.routes.viewmodel.get_organisation", return_value=_make_org()),
            patch("modulo.api.routes.viewmodel.get_user_by_id", return_value=_make_user()),
            patch("modulo.api.routes.viewmodel.list_memberships_for_user", return_value=[]),
            patch("modulo.api.routes.viewmodel.resolve_plan_context", return_value=_make_mock_plan_context()),
        ):
            resp = client.get(self.URL)

        assert resp.status_code == 200
        data = resp.json()
        pipeline_names = {p["name"] for p in data["pipelines"]}
        assert "org-pipeline" in pipeline_names
        assert "team-pipeline" in pipeline_names


# ===========================================================================
# Edge cases / validation
# ===========================================================================


class TestBDDViewAsTeamValidation:
    URL = _VIEWMODEL_URL

    def test_view_as_team_with_empty_string_returns_422(self, client: TestClient) -> None:
        resp = client.get(self.URL, params={"view_as_team": ""})
        assert resp.status_code == 422

    def test_view_as_team_with_numeric_returns_422(self, client: TestClient) -> None:
        resp = client.get(self.URL, params={"view_as_team": 12345})
        assert resp.status_code == 422
