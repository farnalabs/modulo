"""Unit tests for /api/v1/node-categories endpoints."""

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
_CATEGORY_ID = uuid.UUID("00000000-0000-0000-0000-000000000003")
_NOW = datetime(2025, 1, 1, tzinfo=UTC)


def _make_settings() -> Settings:
    return Settings(
        database_url="postgresql+asyncpg://localhost/test",
        secret_key=_VALID_32,
        fernet_key=_VALID_32,
        modulo_admin_password="testpass",
    )


def _make_category(**overrides: object) -> MagicMock:
    c = MagicMock()
    c.id = overrides.get("id", _CATEGORY_ID)
    c.organisation_id = overrides.get("organisation_id", _ORG_ID)
    c.name = overrides.get("name", "LLM Call")
    c.description = overrides.get("description", None)
    c.color = overrides.get("color", "#6366f1")
    c.icon = overrides.get("icon", None)
    c.sort_order = overrides.get("sort_order", 0)
    c.created_by = overrides.get("created_by", _USER_ID)
    c.created_at = _NOW
    c.updated_at = _NOW
    return c


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
        user_id=_USER_ID,
        org_role="admin",
    )
    yield TestClient(app)
    app.dependency_overrides.clear()


@pytest.fixture()
def unauth_client() -> Generator[TestClient, None, None]:
    app.dependency_overrides[get_settings] = _make_settings
    yield TestClient(app)
    app.dependency_overrides.clear()


class TestListNodeCategories:
    def test_returns_200(self, client: TestClient) -> None:
        page_result = MagicMock(items=[_make_category()], total=1, page=1, page_size=20)
        with (
            patch("modulo.api.routes.node_categories.list_node_categories", return_value=page_result),
            patch("modulo.api.routes.node_categories.set_rls_org"),
            patch("modulo.api.routes.node_categories.set_rls_user_context"),
        ):
            resp = client.get("/api/v1/node-categories")
        assert resp.status_code == 200
        data = resp.json()
        assert data["total"] == 1
        assert len(data["items"]) == 1

    def test_returns_empty_when_none(self, client: TestClient) -> None:
        page_result = MagicMock(items=[], total=0, page=1, page_size=20)
        with (
            patch("modulo.api.routes.node_categories.list_node_categories", return_value=page_result),
            patch("modulo.api.routes.node_categories.set_rls_org"),
            patch("modulo.api.routes.node_categories.set_rls_user_context"),
        ):
            resp = client.get("/api/v1/node-categories")
        assert resp.status_code == 200
        assert resp.json()["total"] == 0

    def test_unauthorized_returns_4xx(self, unauth_client: TestClient) -> None:
        resp = unauth_client.get("/api/v1/node-categories")
        assert resp.status_code in (401, 403)


class TestCreateNodeCategory:
    def test_returns_201(self, client: TestClient) -> None:
        category = _make_category(name="LLM Call")
        with (
            patch("modulo.api.routes.node_categories.create_node_category", return_value=category) as create,
            patch("modulo.api.routes.node_categories.set_rls_org"),
            patch("modulo.api.routes.node_categories.set_rls_user_context"),
        ):
            resp = client.post("/api/v1/node-categories", json={"name": "LLM Call"})
        assert resp.status_code == 201
        assert resp.json()["name"] == "LLM Call"
        create.assert_awaited_once()

    def test_with_all_fields(self, client: TestClient) -> None:
        category = _make_category(
            name="Connector Read",
            description="Reads data from an external connector",
            color="#22c55e",
            icon="database",
            sort_order=1,
        )
        with (
            patch("modulo.api.routes.node_categories.create_node_category", return_value=category),
            patch("modulo.api.routes.node_categories.set_rls_org"),
            patch("modulo.api.routes.node_categories.set_rls_user_context"),
        ):
            resp = client.post(
                "/api/v1/node-categories",
                json={
                    "name": "Connector Read",
                    "description": "Reads data from an external connector",
                    "color": "#22c55e",
                    "icon": "database",
                    "sort_order": 1,
                },
            )
        assert resp.status_code == 201
        body = resp.json()
        assert body["name"] == "Connector Read"
        assert body["description"] == "Reads data from an external connector"
        assert body["color"] == "#22c55e"
        assert body["icon"] == "database"
        assert body["sort_order"] == 1

    def test_uses_default_color(self, client: TestClient) -> None:
        category = _make_category(name="Default Color")
        with (
            patch("modulo.api.routes.node_categories.create_node_category", return_value=category),
            patch("modulo.api.routes.node_categories.set_rls_org"),
            patch("modulo.api.routes.node_categories.set_rls_user_context"),
        ):
            resp = client.post("/api/v1/node-categories", json={"name": "Default Color"})
        assert resp.status_code == 201
        assert resp.json()["color"] == "#6366f1"

    def test_empty_name_returns_422(self, client: TestClient) -> None:
        resp = client.post("/api/v1/node-categories", json={"name": ""})
        assert resp.status_code == 422

    def test_missing_name_returns_422(self, client: TestClient) -> None:
        resp = client.post("/api/v1/node-categories", json={})
        assert resp.status_code == 422

    def test_invalid_color_returns_422(self, client: TestClient) -> None:
        resp = client.post("/api/v1/node-categories", json={"name": "Test", "color": "red"})
        assert resp.status_code == 422

    def test_unauthorized_returns_4xx(self, unauth_client: TestClient) -> None:
        resp = unauth_client.post("/api/v1/node-categories", json={"name": "Test"})
        assert resp.status_code in (401, 403)


class TestGetNodeCategory:
    def test_returns_200(self, client: TestClient) -> None:
        with (
            patch("modulo.api.routes.node_categories.get_node_category", return_value=_make_category()),
            patch("modulo.api.routes.node_categories.set_rls_org"),
            patch("modulo.api.routes.node_categories.set_rls_user_context"),
        ):
            resp = client.get(f"/api/v1/node-categories/{_CATEGORY_ID}")
        assert resp.status_code == 200
        assert resp.json()["id"] == str(_CATEGORY_ID)

    def test_not_found_returns_404(self, client: TestClient) -> None:
        with (
            patch("modulo.api.routes.node_categories.get_node_category", return_value=None),
            patch("modulo.api.routes.node_categories.set_rls_org"),
            patch("modulo.api.routes.node_categories.set_rls_user_context"),
        ):
            resp = client.get(f"/api/v1/node-categories/{uuid.uuid4()}")
        assert resp.status_code == 404


class TestUpdateNodeCategory:
    def test_returns_200(self, client: TestClient) -> None:
        category = _make_category(name="Updated Name")
        with (
            patch("modulo.api.routes.node_categories.update_node_category", return_value=category),
            patch("modulo.api.routes.node_categories.set_rls_org"),
            patch("modulo.api.routes.node_categories.set_rls_user_context"),
        ):
            resp = client.patch(f"/api/v1/node-categories/{_CATEGORY_ID}", json={"name": "Updated Name"})
        assert resp.status_code == 200
        assert resp.json()["name"] == "Updated Name"

    def test_partial_update(self, client: TestClient) -> None:
        category = _make_category(name="LLM Call", color="#ef4444")
        with (
            patch("modulo.api.routes.node_categories.update_node_category", return_value=category),
            patch("modulo.api.routes.node_categories.set_rls_org"),
            patch("modulo.api.routes.node_categories.set_rls_user_context"),
        ):
            resp = client.patch(f"/api/v1/node-categories/{_CATEGORY_ID}", json={"color": "#ef4444"})
        assert resp.status_code == 200
        assert resp.json()["color"] == "#ef4444"

    def test_not_found_returns_404(self, client: TestClient) -> None:
        with (
            patch("modulo.api.routes.node_categories.update_node_category", return_value=None),
            patch("modulo.api.routes.node_categories.set_rls_org"),
            patch("modulo.api.routes.node_categories.set_rls_user_context"),
        ):
            resp = client.patch(f"/api/v1/node-categories/{uuid.uuid4()}", json={"name": "x"})
        assert resp.status_code == 404

    def test_empty_name_returns_422(self, client: TestClient) -> None:
        resp = client.patch(f"/api/v1/node-categories/{_CATEGORY_ID}", json={"name": ""})
        assert resp.status_code == 422


class TestDeleteNodeCategory:
    def test_returns_204(self, client: TestClient) -> None:
        with (
            patch("modulo.api.routes.node_categories.delete_node_category", return_value=True),
            patch("modulo.api.routes.node_categories.set_rls_org"),
            patch("modulo.api.routes.node_categories.set_rls_user_context"),
        ):
            resp = client.delete(f"/api/v1/node-categories/{_CATEGORY_ID}")
        assert resp.status_code == 204

    def test_not_found_returns_404(self, client: TestClient) -> None:
        with (
            patch("modulo.api.routes.node_categories.delete_node_category", return_value=False),
            patch("modulo.api.routes.node_categories.set_rls_org"),
            patch("modulo.api.routes.node_categories.set_rls_user_context"),
        ):
            resp = client.delete(f"/api/v1/node-categories/{uuid.uuid4()}")
        assert resp.status_code == 404
