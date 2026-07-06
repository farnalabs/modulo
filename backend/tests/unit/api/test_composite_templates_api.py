"""Unit tests for /api/v1/composite-templates endpoints."""

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
_TEMPLATE_ID = uuid.UUID("00000000-0000-0000-0000-000000000003")
_NOW = datetime(2025, 1, 1, tzinfo=UTC)


def _make_settings() -> Settings:
    return Settings(
        database_url="postgresql+asyncpg://localhost/test",
        secret_key=_VALID_32,
        fernet_key=_VALID_32,
        modulo_admin_password="testpass",
    )


def _make_template(**overrides: object) -> MagicMock:
    t = MagicMock()
    t.id = overrides.get("id", _TEMPLATE_ID)
    t.organisation_id = overrides.get("organisation_id", _ORG_ID)
    t.name = overrides.get("name", "Devil's Advocate")
    t.description = overrides.get("description")
    t.sub_pipeline_graph_json = overrides.get("sub_pipeline_graph_json", {"nodes": [], "edges": []})
    t.parameter_ports_json = overrides.get("parameter_ports_json", [])
    t.input_schema_id = overrides.get("input_schema_id")
    t.output_schema_id = overrides.get("output_schema_id")
    t.version = overrides.get("version", "1.0.0")
    t.created_by = overrides.get("created_by", _USER_ID)
    t.account_id = _USER_ID
    t.created_at = _NOW
    t.updated_at = _NOW
    return t


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


class TestListCompositeTemplates:
    def test_returns_200(self, client: TestClient) -> None:
        page_result = MagicMock(items=[_make_template()], total=1, page=1, page_size=20)
        with (
            patch("modulo.api.routes.composite_templates.list_composite_templates", return_value=page_result),
            patch("modulo.api.routes.composite_templates.set_rls_org"),
        ):
            resp = client.get("/api/v1/composite-templates")
        assert resp.status_code == 200
        data = resp.json()
        assert data["total"] == 1
        assert len(data["items"]) == 1

    def test_returns_empty_when_none(self, client: TestClient) -> None:
        page_result = MagicMock(items=[], total=0, page=1, page_size=20)
        with (
            patch("modulo.api.routes.composite_templates.list_composite_templates", return_value=page_result),
            patch("modulo.api.routes.composite_templates.set_rls_org"),
        ):
            resp = client.get("/api/v1/composite-templates")
        assert resp.status_code == 200
        assert resp.json()["total"] == 0

    def test_unauthorized_returns_4xx(self, unauth_client: TestClient) -> None:
        resp = unauth_client.get("/api/v1/composite-templates")
        assert resp.status_code in (401, 403)


class TestCreateCompositeTemplate:
    def test_returns_201(self, client: TestClient) -> None:
        template = _make_template(name="Test Composite")
        with (
            patch("modulo.api.routes.composite_templates.create_composite_template", return_value=template) as create,
            patch("modulo.api.routes.composite_templates.set_rls_org"),
        ):
            resp = client.post(
                "/api/v1/composite-templates",
                json={
                    "name": "Test Composite",
                    "sub_pipeline_graph_json": {"nodes": [], "edges": []},
                    "parameter_ports_json": [],
                },
            )
        assert resp.status_code == 201
        assert resp.json()["name"] == "Test Composite"
        create.assert_awaited_once()

    def test_with_all_fields(self, client: TestClient) -> None:
        template = _make_template(
            name="Full Composite",
            description="A full composite template",
            sub_pipeline_graph_json={"nodes": [{"id": "n1"}], "edges": []},
            parameter_ports_json=[{"id": "p1", "name": "prompt", "type": "string"}],
        )
        with (
            patch("modulo.api.routes.composite_templates.create_composite_template", return_value=template),
            patch("modulo.api.routes.composite_templates.set_rls_org"),
        ):
            resp = client.post(
                "/api/v1/composite-templates",
                json={
                    "name": "Full Composite",
                    "description": "A full composite template",
                    "sub_pipeline_graph_json": {"nodes": [{"id": "n1"}], "edges": []},
                    "parameter_ports_json": [
                        {
                            "id": "p1",
                            "name": "prompt",
                            "label": "Prompt",
                            "type": "string",
                            "target_injection": {
                                "mode": "prompt_replace",
                                "node_id": "n1",
                                "injection_point": "prompt_template",
                            },
                        }
                    ],
                },
            )
        assert resp.status_code == 201
        body = resp.json()
        assert body["name"] == "Full Composite"
        assert body["description"] == "A full composite template"

    def test_empty_name_returns_422(self, client: TestClient) -> None:
        resp = client.post(
            "/api/v1/composite-templates",
            json={"name": "", "sub_pipeline_graph_json": {}, "parameter_ports_json": []},
        )
        assert resp.status_code == 422

    def test_missing_name_returns_422(self, client: TestClient) -> None:
        resp = client.post(
            "/api/v1/composite-templates",
            json={"sub_pipeline_graph_json": {}, "parameter_ports_json": []},
        )
        assert resp.status_code == 422

    def test_missing_sub_pipeline_graph_json_returns_422(self, client: TestClient) -> None:
        resp = client.post(
            "/api/v1/composite-templates",
            json={"name": "Test", "parameter_ports_json": []},
        )
        assert resp.status_code == 422

    def test_unauthorized_returns_4xx(self, unauth_client: TestClient) -> None:
        resp = unauth_client.post(
            "/api/v1/composite-templates",
            json={"name": "Test", "sub_pipeline_graph_json": {}, "parameter_ports_json": []},
        )
        assert resp.status_code in (401, 403)


class TestGetCompositeTemplate:
    def test_returns_200(self, client: TestClient) -> None:
        with (
            patch("modulo.api.routes.composite_templates.get_composite_template", return_value=_make_template()),
            patch("modulo.api.routes.composite_templates.set_rls_org"),
        ):
            resp = client.get(f"/api/v1/composite-templates/{_TEMPLATE_ID}")
        assert resp.status_code == 200
        assert resp.json()["id"] == str(_TEMPLATE_ID)

    def test_not_found_returns_404(self, client: TestClient) -> None:
        with (
            patch("modulo.api.routes.composite_templates.get_composite_template", return_value=None),
            patch("modulo.api.routes.composite_templates.set_rls_org"),
        ):
            resp = client.get(f"/api/v1/composite-templates/{uuid.uuid4()}")
        assert resp.status_code == 404


class TestUpdateCompositeTemplate:
    def test_returns_200(self, client: TestClient) -> None:
        template = _make_template(name="Updated Name")
        with (
            patch("modulo.api.routes.composite_templates.update_composite_template", return_value=template),
            patch("modulo.api.routes.composite_templates.set_rls_org"),
        ):
            resp = client.patch(f"/api/v1/composite-templates/{_TEMPLATE_ID}", json={"name": "Updated Name"})
        assert resp.status_code == 200
        assert resp.json()["name"] == "Updated Name"

    def test_partial_update(self, client: TestClient) -> None:
        template = _make_template(name="Test", version="2.0.0")
        with (
            patch("modulo.api.routes.composite_templates.update_composite_template", return_value=template),
            patch("modulo.api.routes.composite_templates.set_rls_org"),
        ):
            resp = client.patch(f"/api/v1/composite-templates/{_TEMPLATE_ID}", json={"version": "2.0.0"})
        assert resp.status_code == 200
        assert resp.json()["version"] == "2.0.0"

    def test_not_found_returns_404(self, client: TestClient) -> None:
        with (
            patch("modulo.api.routes.composite_templates.update_composite_template", return_value=None),
            patch("modulo.api.routes.composite_templates.set_rls_org"),
        ):
            resp = client.patch(f"/api/v1/composite-templates/{uuid.uuid4()}", json={"name": "x"})
        assert resp.status_code == 404


class TestDeleteCompositeTemplate:
    def test_returns_204(self, client: TestClient) -> None:
        with (
            patch("modulo.api.routes.composite_templates.delete_composite_template", return_value=True),
            patch("modulo.api.routes.composite_templates.set_rls_org"),
        ):
            resp = client.delete(f"/api/v1/composite-templates/{_TEMPLATE_ID}")
        assert resp.status_code == 204

    def test_not_found_returns_404(self, client: TestClient) -> None:
        with (
            patch("modulo.api.routes.composite_templates.delete_composite_template", return_value=False),
            patch("modulo.api.routes.composite_templates.set_rls_org"),
        ):
            resp = client.delete(f"/api/v1/composite-templates/{uuid.uuid4()}")
        assert resp.status_code == 404
