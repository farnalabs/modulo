"""Unit tests for /api/v1/views endpoints."""

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
        modulo_license_key="test-license-key",
    )


def _make_view(**overrides: object) -> MagicMock:
    v = MagicMock()
    v.id = overrides.get("id", _VIEW_ID)
    v.organisation_id = overrides.get("organisation_id", _ORG_ID)
    v.name = overrides.get("name", "Test View")
    v.description = overrides.get("description")
    v.view_type = overrides.get("view_type", "run_list")
    v.filters = overrides.get("filters", {})
    v.columns = overrides.get("columns")
    v.sort_by = overrides.get("sort_by")
    v.sort_order = overrides.get("sort_order", "desc")
    v.created_by = overrides.get("created_by", _USER_ID)
    v.created_at = _NOW
    v.updated_at = _NOW
    return v


def _make_mock_session() -> AsyncMock:
    session = AsyncMock()
    begin_cm = AsyncMock()
    begin_cm.__aenter__ = AsyncMock(return_value=None)
    begin_cm.__aexit__ = AsyncMock(return_value=False)
    session.begin = MagicMock(return_value=begin_cm)
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
    mock_plan = MagicMock()
    mock_plan.feature_enabled.return_value = True
    app.dependency_overrides[get_plan_context] = lambda: mock_plan
    yield TestClient(app)
    app.dependency_overrides.clear()


@pytest.fixture()
def unauth_client() -> Generator[TestClient, None, None]:
    app.dependency_overrides[get_settings] = _make_settings
    mock_plan = MagicMock()
    mock_plan.feature_enabled.return_value = True
    app.dependency_overrides[get_plan_context] = lambda: mock_plan
    yield TestClient(app)
    app.dependency_overrides.clear()


class TestListViews:
    def test_returns_200(self, client: TestClient) -> None:
        page_result = MagicMock(items=[_make_view()], total=1, page=1, page_size=20)
        with (
            patch("modulo.api.routes.views.list_views", return_value=page_result),
            patch("modulo.api.routes.views.set_rls_org"),
            patch("modulo.api.routes.views.set_rls_user_context"),
        ):
            resp = client.get("/api/v1/views")
        assert resp.status_code == 200
        data = resp.json()
        assert data["total"] == 1
        assert len(data["items"]) == 1

    def test_filters_by_type(self, client: TestClient) -> None:
        page_result = MagicMock(items=[_make_view(view_type="audit_log")], total=1, page=1, page_size=20)
        with (
            patch("modulo.api.routes.views.list_views", return_value=page_result) as list_mock,
            patch("modulo.api.routes.views.set_rls_org"),
            patch("modulo.api.routes.views.set_rls_user_context"),
        ):
            resp = client.get("/api/v1/views?view_type=audit_log")
        assert resp.status_code == 200
        assert resp.json()["total"] == 1
        list_mock.assert_awaited_once()
        args = list_mock.call_args[1]
        assert args.get("view_type") == "audit_log"

    def test_returns_empty_when_no_views(self, client: TestClient) -> None:
        page_result = MagicMock(items=[], total=0, page=1, page_size=20)
        with (
            patch("modulo.api.routes.views.list_views", return_value=page_result),
            patch("modulo.api.routes.views.set_rls_org"),
            patch("modulo.api.routes.views.set_rls_user_context"),
        ):
            resp = client.get("/api/v1/views")
        assert resp.status_code == 200
        assert resp.json()["total"] == 0

    def test_unauthorized_returns_4xx(self, unauth_client: TestClient) -> None:
        resp = unauth_client.get("/api/v1/views")
        assert resp.status_code in (401, 403)


class TestCreateView:
    def test_returns_201(self, client: TestClient) -> None:
        view = _make_view(name="My View", view_type="run_list")
        with (
            patch("modulo.api.routes.views.create_view", return_value=view) as create,
            patch("modulo.api.routes.views.set_rls_org"),
            patch("modulo.api.routes.views.set_rls_user_context"),
        ):
            resp = client.post("/api/v1/views", json={"name": "My View", "view_type": "run_list"})
        assert resp.status_code == 201
        assert resp.json()["name"] == "My View"
        create.assert_awaited_once()

    def test_with_all_fields(self, client: TestClient) -> None:
        view = _make_view(
            name="Full View",
            view_type="pipeline_list",
            filters={"status": "active"},
            columns=["name", "status"],
            sort_by="created_at",
            sort_order="asc",
        )
        with (
            patch("modulo.api.routes.views.create_view", return_value=view),
            patch("modulo.api.routes.views.set_rls_org"),
            patch("modulo.api.routes.views.set_rls_user_context"),
        ):
            resp = client.post(
                "/api/v1/views",
                json={
                    "name": "Full View",
                    "view_type": "pipeline_list",
                    "filters": {"status": "active"},
                    "columns": ["name", "status"],
                    "sort_by": "created_at",
                    "sort_order": "asc",
                },
            )
        assert resp.status_code == 201
        body = resp.json()
        assert body["name"] == "Full View"
        assert body["filters"] == {"status": "active"}
        assert body["columns"] == ["name", "status"]
        assert body["sort_by"] == "created_at"
        assert body["sort_order"] == "asc"

    def test_empty_name_returns_422(self, client: TestClient) -> None:
        resp = client.post("/api/v1/views", json={"name": "", "view_type": "run_list"})
        assert resp.status_code == 422

    def test_invalid_view_type_returns_422(self, client: TestClient) -> None:
        resp = client.post("/api/v1/views", json={"name": "Test", "view_type": "invalid"})
        assert resp.status_code == 422

    def test_invalid_sort_order_returns_422(self, client: TestClient) -> None:
        resp = client.post("/api/v1/views", json={"name": "Test", "view_type": "run_list", "sort_order": "invalid"})
        assert resp.status_code == 422

    def test_unauthorized_returns_4xx(self, unauth_client: TestClient) -> None:
        resp = unauth_client.post("/api/v1/views", json={"name": "Test", "view_type": "run_list"})
        assert resp.status_code in (401, 403)


class TestGetView:
    def test_returns_200(self, client: TestClient) -> None:
        with (
            patch("modulo.api.routes.views.get_view", return_value=_make_view()),
            patch("modulo.api.routes.views.set_rls_org"),
            patch("modulo.api.routes.views.set_rls_user_context"),
        ):
            resp = client.get(f"/api/v1/views/{_VIEW_ID}")
        assert resp.status_code == 200
        assert resp.json()["id"] == str(_VIEW_ID)

    def test_not_found_returns_404(self, client: TestClient) -> None:
        with (
            patch("modulo.api.routes.views.get_view", return_value=None),
            patch("modulo.api.routes.views.set_rls_org"),
            patch("modulo.api.routes.views.set_rls_user_context"),
        ):
            resp = client.get(f"/api/v1/views/{uuid.uuid4()}")
        assert resp.status_code == 404


class TestUpdateView:
    def test_returns_200(self, client: TestClient) -> None:
        view = _make_view(name="Updated View")
        with (
            patch("modulo.api.routes.views.update_view", return_value=view),
            patch("modulo.api.routes.views.set_rls_org"),
            patch("modulo.api.routes.views.set_rls_user_context"),
        ):
            resp = client.patch(f"/api/v1/views/{_VIEW_ID}", json={"name": "Updated View"})
        assert resp.status_code == 200
        assert resp.json()["name"] == "Updated View"

    def test_partial_update(self, client: TestClient) -> None:
        view = _make_view(name="Original", filters={"env": "prod"})
        with (
            patch("modulo.api.routes.views.update_view", return_value=view),
            patch("modulo.api.routes.views.set_rls_org"),
            patch("modulo.api.routes.views.set_rls_user_context"),
        ):
            resp = client.patch(f"/api/v1/views/{_VIEW_ID}", json={"filters": {"env": "prod"}})
        assert resp.status_code == 200
        assert resp.json()["filters"] == {"env": "prod"}

    def test_not_found_returns_404(self, client: TestClient) -> None:
        with (
            patch("modulo.api.routes.views.update_view", return_value=None),
            patch("modulo.api.routes.views.set_rls_org"),
            patch("modulo.api.routes.views.set_rls_user_context"),
        ):
            resp = client.patch(f"/api/v1/views/{uuid.uuid4()}", json={"name": "x"})
        assert resp.status_code == 404

    def test_invalid_sort_order_returns_422(self, client: TestClient) -> None:
        resp = client.patch(f"/api/v1/views/{_VIEW_ID}", json={"sort_order": "invalid"})
        assert resp.status_code == 422


class TestDeleteView:
    def test_returns_204(self, client: TestClient) -> None:
        with (
            patch("modulo.api.routes.views.delete_view", return_value=True),
            patch("modulo.api.routes.views.set_rls_org"),
            patch("modulo.api.routes.views.set_rls_user_context"),
        ):
            resp = client.delete(f"/api/v1/views/{_VIEW_ID}")
        assert resp.status_code == 204

    def test_not_found_returns_404(self, client: TestClient) -> None:
        with (
            patch("modulo.api.routes.views.delete_view", return_value=False),
            patch("modulo.api.routes.views.set_rls_org"),
            patch("modulo.api.routes.views.set_rls_user_context"),
        ):
            resp = client.delete(f"/api/v1/views/{uuid.uuid4()}")
        assert resp.status_code == 404
