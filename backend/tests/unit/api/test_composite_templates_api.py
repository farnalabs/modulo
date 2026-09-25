"""Unit tests for /api/v1/composite-templates endpoints."""

import uuid
from collections.abc import AsyncGenerator, Generator
from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.exc import ProgrammingError, SQLAlchemyError

from modulo.api.dependencies import _get_engine, get_db_session, get_plan_context
from modulo.api.main import app
from modulo.auth.dependencies import get_current_tenant_user, get_current_user
from modulo.auth.jwt import AuthenticatedPrincipal, TenantPrincipal
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
    t.parameter_schema_id = overrides.get("parameter_schema_id")
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
    app.dependency_overrides[get_current_tenant_user] = lambda: TenantPrincipal(
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


@pytest.fixture
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
        account_id=_USER_ID,
        org_role="viewer",
    )
    app.dependency_overrides[get_current_tenant_user] = lambda: TenantPrincipal(
        username="viewer",
        organisation_id=_ORG_ID,
        account_id=_USER_ID,
        org_role="viewer",
    )
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


class TestRestoreCompositeTemplate:
    def test_restore_returns_200(self, client: TestClient) -> None:
        with (
            patch("modulo.api.routes.composite_templates.restore_composite_template", return_value=_make_template()),
            patch("modulo.api.routes.composite_templates.set_rls_org"),
        ):
            resp = client.post(f"/api/v1/composite-templates/{_TEMPLATE_ID}/restore")
        assert resp.status_code == 200
        assert resp.json()["id"] == str(_TEMPLATE_ID)

    def test_restore_not_found_returns_404(self, client: TestClient) -> None:
        with (
            patch("modulo.api.routes.composite_templates.restore_composite_template", return_value=None),
            patch("modulo.api.routes.composite_templates.set_rls_org"),
        ):
            resp = client.post(f"/api/v1/composite-templates/{uuid.uuid4()}/restore")
        assert resp.status_code == 404


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
            patch(
                "modulo.api.routes.composite_templates.soft_delete_composite_template", return_value=_make_template()
            ),
        ):
            resp = client.delete(f"/api/v1/composite-templates/{_TEMPLATE_ID}")
        assert resp.status_code == 204

    def test_not_found_returns_404(self, client: TestClient) -> None:
        with (
            patch("modulo.api.routes.composite_templates.soft_delete_composite_template", return_value=None),
            patch("modulo.api.routes.composite_templates.set_rls_org"),
        ):
            resp = client.delete(f"/api/v1/composite-templates/{uuid.uuid4()}")
        assert resp.status_code == 404


class TestCompositeOperatorFloor:
    """SECURITY #1461 — composite-template CRUD + editor + publish are operator+ only."""

    def test_create_viewer_gets_403(self, viewer_client: TestClient) -> None:
        resp = viewer_client.post(
            "/api/v1/composite-templates",
            json={
                "name": "Test",
                "sub_pipeline_graph_json": {"nodes": [], "edges": []},
                "parameter_ports_json": [],
            },
        )
        assert resp.status_code == 403

    def test_update_viewer_gets_403(self, viewer_client: TestClient) -> None:
        resp = viewer_client.patch(f"/api/v1/composite-templates/{_TEMPLATE_ID}", json={"name": "x"})
        assert resp.status_code == 403

    def test_delete_viewer_gets_403(self, viewer_client: TestClient) -> None:
        resp = viewer_client.delete(f"/api/v1/composite-templates/{_TEMPLATE_ID}")
        assert resp.status_code == 403

    def test_restore_viewer_gets_403(self, viewer_client: TestClient) -> None:
        resp = viewer_client.post(f"/api/v1/composite-templates/{_TEMPLATE_ID}/restore")
        assert resp.status_code == 403

    def test_publish_viewer_gets_403(self, viewer_client: TestClient) -> None:
        resp = viewer_client.post(
            f"/api/v1/composite-templates/{_TEMPLATE_ID}/publish",
            json={},
        )
        assert resp.status_code == 403


# ---------------------------------------------------------------------------
# DB error handlers (ProgrammingError → 501, SQLAlchemyError → 503,
# generic → 500) for every endpoint
# ---------------------------------------------------------------------------


class TestDBErrorHandlers:
    """Cover the except-ProgrammingError / except-SQLAlchemyError / except-Exception
    branches in every composite-templates endpoint."""

    def test_list_programming_error_returns_501(self, client: TestClient) -> None:
        with patch(
            "modulo.api.routes.composite_templates.set_rls_org",
            new_callable=AsyncMock,
            side_effect=ProgrammingError("stmt", {}, Exception("missing table")),
        ):
            resp = client.get("/api/v1/composite-templates")
        assert resp.status_code == 501

    def test_list_sqlalchemy_error_returns_503(self, client: TestClient) -> None:
        with patch(
            "modulo.api.routes.composite_templates.set_rls_org",
            new_callable=AsyncMock,
            side_effect=SQLAlchemyError("connection lost"),
        ):
            resp = client.get("/api/v1/composite-templates")
        assert resp.status_code == 503

    def test_list_unexpected_error_returns_500(self, client: TestClient) -> None:
        with patch(
            "modulo.api.routes.composite_templates.set_rls_org",
            new_callable=AsyncMock,
            side_effect=RuntimeError("kaboom"),
        ):
            resp = client.get("/api/v1/composite-templates")
        assert resp.status_code == 500

    def test_create_programming_error_returns_501(self, client: TestClient) -> None:
        with patch(
            "modulo.api.routes.composite_templates.set_rls_org",
            new_callable=AsyncMock,
            side_effect=ProgrammingError("stmt", {}, Exception("missing table")),
        ):
            resp = client.post(
                "/api/v1/composite-templates",
                json={"name": "X", "sub_pipeline_graph_json": {}, "parameter_ports_json": []},
            )
        assert resp.status_code == 501

    def test_create_sqlalchemy_error_returns_503(self, client: TestClient) -> None:
        with patch(
            "modulo.api.routes.composite_templates.set_rls_org",
            new_callable=AsyncMock,
            side_effect=SQLAlchemyError("db down"),
        ):
            resp = client.post(
                "/api/v1/composite-templates",
                json={"name": "X", "sub_pipeline_graph_json": {}, "parameter_ports_json": []},
            )
        assert resp.status_code == 503

    def test_create_unexpected_error_returns_500(self, client: TestClient) -> None:
        with patch(
            "modulo.api.routes.composite_templates.set_rls_org",
            new_callable=AsyncMock,
            side_effect=RuntimeError("kaboom"),
        ):
            resp = client.post(
                "/api/v1/composite-templates",
                json={"name": "X", "sub_pipeline_graph_json": {}, "parameter_ports_json": []},
            )
        assert resp.status_code == 500

    def test_get_programming_error_returns_501(self, client: TestClient) -> None:
        with patch(
            "modulo.api.routes.composite_templates.set_rls_org",
            new_callable=AsyncMock,
            side_effect=ProgrammingError("stmt", {}, Exception("missing table")),
        ):
            resp = client.get(f"/api/v1/composite-templates/{_TEMPLATE_ID}")
        assert resp.status_code == 501

    def test_get_sqlalchemy_error_returns_503(self, client: TestClient) -> None:
        with patch(
            "modulo.api.routes.composite_templates.set_rls_org",
            new_callable=AsyncMock,
            side_effect=SQLAlchemyError("db down"),
        ):
            resp = client.get(f"/api/v1/composite-templates/{_TEMPLATE_ID}")
        assert resp.status_code == 503

    def test_get_unexpected_error_returns_500(self, client: TestClient) -> None:
        with patch(
            "modulo.api.routes.composite_templates.set_rls_org",
            new_callable=AsyncMock,
            side_effect=RuntimeError("kaboom"),
        ):
            resp = client.get(f"/api/v1/composite-templates/{_TEMPLATE_ID}")
        assert resp.status_code == 500

    def test_update_programming_error_returns_501(self, client: TestClient) -> None:
        with patch(
            "modulo.api.routes.composite_templates.set_rls_org",
            new_callable=AsyncMock,
            side_effect=ProgrammingError("stmt", {}, Exception("missing table")),
        ):
            resp = client.patch(f"/api/v1/composite-templates/{_TEMPLATE_ID}", json={"name": "X"})
        assert resp.status_code == 501

    def test_update_sqlalchemy_error_returns_503(self, client: TestClient) -> None:
        with patch(
            "modulo.api.routes.composite_templates.set_rls_org",
            new_callable=AsyncMock,
            side_effect=SQLAlchemyError("db down"),
        ):
            resp = client.patch(f"/api/v1/composite-templates/{_TEMPLATE_ID}", json={"name": "X"})
        assert resp.status_code == 503

    def test_update_unexpected_error_returns_500(self, client: TestClient) -> None:
        with patch(
            "modulo.api.routes.composite_templates.set_rls_org",
            new_callable=AsyncMock,
            side_effect=RuntimeError("kaboom"),
        ):
            resp = client.patch(f"/api/v1/composite-templates/{_TEMPLATE_ID}", json={"name": "X"})
        assert resp.status_code == 500

    def test_delete_programming_error_returns_501(self, client: TestClient) -> None:
        with patch(
            "modulo.api.routes.composite_templates.set_rls_org",
            new_callable=AsyncMock,
            side_effect=ProgrammingError("stmt", {}, Exception("missing table")),
        ):
            resp = client.delete(f"/api/v1/composite-templates/{_TEMPLATE_ID}")
        assert resp.status_code == 501

    def test_delete_sqlalchemy_error_returns_503(self, client: TestClient) -> None:
        with patch(
            "modulo.api.routes.composite_templates.set_rls_org",
            new_callable=AsyncMock,
            side_effect=SQLAlchemyError("db down"),
        ):
            resp = client.delete(f"/api/v1/composite-templates/{_TEMPLATE_ID}")
        assert resp.status_code == 503

    def test_delete_unexpected_error_returns_500(self, client: TestClient) -> None:
        with patch(
            "modulo.api.routes.composite_templates.set_rls_org",
            new_callable=AsyncMock,
            side_effect=RuntimeError("kaboom"),
        ):
            resp = client.delete(f"/api/v1/composite-templates/{_TEMPLATE_ID}")
        assert resp.status_code == 500

    def test_restore_programming_error_returns_501(self, client: TestClient) -> None:
        with patch(
            "modulo.api.routes.composite_templates.set_rls_org",
            new_callable=AsyncMock,
            side_effect=ProgrammingError("stmt", {}, Exception("missing table")),
        ):
            resp = client.post(f"/api/v1/composite-templates/{_TEMPLATE_ID}/restore")
        assert resp.status_code == 501

    def test_restore_sqlalchemy_error_returns_503(self, client: TestClient) -> None:
        with patch(
            "modulo.api.routes.composite_templates.set_rls_org",
            new_callable=AsyncMock,
            side_effect=SQLAlchemyError("db down"),
        ):
            resp = client.post(f"/api/v1/composite-templates/{_TEMPLATE_ID}/restore")
        assert resp.status_code == 503

    def test_restore_unexpected_error_returns_500(self, client: TestClient) -> None:
        with patch(
            "modulo.api.routes.composite_templates.set_rls_org",
            new_callable=AsyncMock,
            side_effect=RuntimeError("kaboom"),
        ):
            resp = client.post(f"/api/v1/composite-templates/{_TEMPLATE_ID}/restore")
        assert resp.status_code == 500


# ---------------------------------------------------------------------------
# Editor GET / PUT endpoints
# ---------------------------------------------------------------------------


class TestCompositeEditor:
    """Cover the GET /{template_id}/editor and PUT /{template_id}/editor endpoints."""

    def test_get_editor_returns_nodes_and_edges(self, client: TestClient) -> None:
        graph = {"nodes": [{"id": "n1"}], "edges": [{"source": "n1", "target": "n2"}]}
        template = _make_template(sub_pipeline_graph_json=graph)
        with (
            patch("modulo.api.routes.composite_templates.get_composite_template", return_value=template),
            patch("modulo.api.routes.composite_templates.set_rls_org"),
        ):
            resp = client.get(f"/api/v1/composite-templates/{_TEMPLATE_ID}/editor")
        assert resp.status_code == 200
        body = resp.json()
        assert body["nodes"] == [{"id": "n1"}]
        assert len(body["edges"]) == 1

    def test_get_editor_empty_graph(self, client: TestClient) -> None:
        template = _make_template(sub_pipeline_graph_json={})
        with (
            patch("modulo.api.routes.composite_templates.get_composite_template", return_value=template),
            patch("modulo.api.routes.composite_templates.set_rls_org"),
        ):
            resp = client.get(f"/api/v1/composite-templates/{_TEMPLATE_ID}/editor")
        assert resp.status_code == 200
        assert not resp.json()["nodes"]
        assert not resp.json()["edges"]

    def test_get_editor_not_found_returns_404(self, client: TestClient) -> None:
        with (
            patch("modulo.api.routes.composite_templates.get_composite_template", return_value=None),
            patch("modulo.api.routes.composite_templates.set_rls_org"),
        ):
            resp = client.get(f"/api/v1/composite-templates/{uuid.uuid4()}/editor")
        assert resp.status_code == 404

    def test_get_editor_programming_error_returns_501(self, client: TestClient) -> None:
        with patch(
            "modulo.api.routes.composite_templates.set_rls_org",
            new_callable=AsyncMock,
            side_effect=ProgrammingError("stmt", {}, Exception("missing table")),
        ):
            resp = client.get(f"/api/v1/composite-templates/{_TEMPLATE_ID}/editor")
        assert resp.status_code == 501

    def test_get_editor_unexpected_error_returns_500(self, client: TestClient) -> None:
        with patch(
            "modulo.api.routes.composite_templates.set_rls_org",
            new_callable=AsyncMock,
            side_effect=RuntimeError("boom"),
        ):
            resp = client.get(f"/api/v1/composite-templates/{_TEMPLATE_ID}/editor")
        assert resp.status_code == 500

    def test_put_editor_saves_nodes_and_edges(self, client: TestClient) -> None:
        existing = _make_template(sub_pipeline_graph_json={"nodes": [], "edges": []})
        updated = _make_template(sub_pipeline_graph_json={"nodes": [{"id": "n1"}], "edges": []})
        with (
            patch("modulo.api.routes.composite_templates.get_composite_template", side_effect=[existing, updated]),
            patch("modulo.api.routes.composite_templates.update_composite_template", return_value=updated),
            patch("modulo.api.routes.composite_templates.set_rls_org"),
        ):
            resp = client.put(
                f"/api/v1/composite-templates/{_TEMPLATE_ID}/editor",
                json={"nodes": [{"id": "n1"}], "edges": []},
            )
        assert resp.status_code == 200
        assert resp.json()["nodes"] == [{"id": "n1"}]

    def test_put_editor_preserves_existing_graph_keys(self, client: TestClient) -> None:
        existing = _make_template(sub_pipeline_graph_json={"nodes": [], "edges": [], "extra_field": "kept"})
        updated = _make_template(sub_pipeline_graph_json={"nodes": [{"id": "n1"}], "edges": [], "extra_field": "kept"})
        with (
            patch("modulo.api.routes.composite_templates.get_composite_template", side_effect=[existing, updated]),
            patch("modulo.api.routes.composite_templates.update_composite_template", return_value=updated),
            patch("modulo.api.routes.composite_templates.set_rls_org"),
        ):
            resp = client.put(
                f"/api/v1/composite-templates/{_TEMPLATE_ID}/editor",
                json={"nodes": [{"id": "n1"}], "edges": []},
            )
        assert resp.status_code == 200

    def test_put_editor_not_found_returns_404(self, client: TestClient) -> None:
        with (
            patch("modulo.api.routes.composite_templates.get_composite_template", return_value=None),
            patch("modulo.api.routes.composite_templates.set_rls_org"),
        ):
            resp = client.put(
                f"/api/v1/composite-templates/{uuid.uuid4()}/editor",
                json={"nodes": [], "edges": []},
            )
        assert resp.status_code == 404

    def test_put_editor_programming_error_returns_501(self, client: TestClient) -> None:
        with patch(
            "modulo.api.routes.composite_templates.set_rls_org",
            new_callable=AsyncMock,
            side_effect=ProgrammingError("stmt", {}, Exception("missing table")),
        ):
            resp = client.put(
                f"/api/v1/composite-templates/{_TEMPLATE_ID}/editor",
                json={"nodes": [], "edges": []},
            )
        assert resp.status_code == 501

    def test_put_editor_sqlalchemy_error_returns_503(self, client: TestClient) -> None:
        with patch(
            "modulo.api.routes.composite_templates.set_rls_org",
            new_callable=AsyncMock,
            side_effect=SQLAlchemyError("db down"),
        ):
            resp = client.put(
                f"/api/v1/composite-templates/{_TEMPLATE_ID}/editor",
                json={"nodes": [], "edges": []},
            )
        assert resp.status_code == 503

    def test_put_editor_unexpected_error_returns_500(self, client: TestClient) -> None:
        with patch(
            "modulo.api.routes.composite_templates.set_rls_org",
            new_callable=AsyncMock,
            side_effect=RuntimeError("boom"),
        ):
            resp = client.put(
                f"/api/v1/composite-templates/{_TEMPLATE_ID}/editor",
                json={"nodes": [], "edges": []},
            )
        assert resp.status_code == 500

    def test_put_editor_not_found_after_update_returns_404(self, client: TestClient) -> None:
        """Second get_composite_template inside the PUT transaction returns None."""
        existing = _make_template()
        with (
            patch(
                "modulo.api.routes.composite_templates.get_composite_template",
                side_effect=[existing, None],
            ),
            patch("modulo.api.routes.composite_templates.update_composite_template", return_value=None),
            patch("modulo.api.routes.composite_templates.set_rls_org"),
        ):
            resp = client.put(
                f"/api/v1/composite-templates/{_TEMPLATE_ID}/editor",
                json={"nodes": [], "edges": []},
            )
        assert resp.status_code == 404


# ---------------------------------------------------------------------------
# Publish endpoint
# ---------------------------------------------------------------------------


class TestPublishCompositeTemplate:
    def test_publish_returns_200_with_version(self, client: TestClient) -> None:
        template = _make_template(version="2.0.0")
        with (
            patch("modulo.api.routes.composite_templates.update_composite_template", return_value=template),
            patch("modulo.api.routes.composite_templates.set_rls_org"),
        ):
            resp = client.post(
                f"/api/v1/composite-templates/{_TEMPLATE_ID}/publish",
                json={"version": "2.0.0"},
            )
        assert resp.status_code == 200
        body = resp.json()
        assert body["version"] == "2.0.0"
        assert body["published"] is True

    def test_publish_default_version(self, client: TestClient) -> None:
        template = _make_template(version="1.0.0")
        with (
            patch("modulo.api.routes.composite_templates.update_composite_template", return_value=template),
            patch("modulo.api.routes.composite_templates.set_rls_org"),
        ):
            resp = client.post(
                f"/api/v1/composite-templates/{_TEMPLATE_ID}/publish",
                json={},
            )
        assert resp.status_code == 200
        assert resp.json()["version"] == "1.0.0"

    def test_publish_not_found_returns_404(self, client: TestClient) -> None:
        with (
            patch("modulo.api.routes.composite_templates.update_composite_template", return_value=None),
            patch("modulo.api.routes.composite_templates.set_rls_org"),
        ):
            resp = client.post(
                f"/api/v1/composite-templates/{uuid.uuid4()}/publish",
                json={},
            )
        assert resp.status_code == 404

    def test_publish_invalid_version_pattern_returns_422(self, client: TestClient) -> None:
        resp = client.post(
            f"/api/v1/composite-templates/{_TEMPLATE_ID}/publish",
            json={"version": "not-a-version"},
        )
        assert resp.status_code == 422

    def test_publish_programming_error_returns_501(self, client: TestClient) -> None:
        with patch(
            "modulo.api.routes.composite_templates.set_rls_org",
            new_callable=AsyncMock,
            side_effect=ProgrammingError("stmt", {}, Exception("missing table")),
        ):
            resp = client.post(
                f"/api/v1/composite-templates/{_TEMPLATE_ID}/publish",
                json={},
            )
        assert resp.status_code == 501

    def test_publish_sqlalchemy_error_returns_503(self, client: TestClient) -> None:
        with patch(
            "modulo.api.routes.composite_templates.set_rls_org",
            new_callable=AsyncMock,
            side_effect=SQLAlchemyError("db down"),
        ):
            resp = client.post(
                f"/api/v1/composite-templates/{_TEMPLATE_ID}/publish",
                json={},
            )
        assert resp.status_code == 503

    def test_publish_unexpected_error_returns_500(self, client: TestClient) -> None:
        with patch(
            "modulo.api.routes.composite_templates.set_rls_org",
            new_callable=AsyncMock,
            side_effect=RuntimeError("boom"),
        ):
            resp = client.post(
                f"/api/v1/composite-templates/{_TEMPLATE_ID}/publish",
                json={},
            )
        assert resp.status_code == 500


# ---------------------------------------------------------------------------
# detect-params endpoint
# ---------------------------------------------------------------------------


class TestDetectParams:
    def test_detect_params_returns_ports_for_placeholders(self, client: TestClient) -> None:
        resp = client.post(
            "/api/v1/composite-templates/detect-params",
            json={
                "nodes": [
                    {
                        "id": "n1",
                        "prompt_template": "You are {{parameter.role}}. Respond as {{parameter.tone}}.",
                    }
                ]
            },
        )
        assert resp.status_code == 200
        ports = resp.json()["ports"]
        names = {p["name"] for p in ports}
        assert "role" in names
        assert "tone" in names
        for p in ports:
            assert p["target_injection"]["node_id"] == "n1"

    def test_detect_params_no_placeholders(self, client: TestClient) -> None:
        resp = client.post(
            "/api/v1/composite-templates/detect-params",
            json={"nodes": [{"id": "n1", "prompt_template": "No placeholders here"}]},
        )
        assert resp.status_code == 200
        assert not resp.json()["ports"]

    def test_detect_params_empty_nodes(self, client: TestClient) -> None:
        resp = client.post(
            "/api/v1/composite-templates/detect-params",
            json={"nodes": []},
        )
        assert resp.status_code == 200
        assert not resp.json()["ports"]

    def test_detect_params_deduplicates_across_nodes(self, client: TestClient) -> None:
        resp = client.post(
            "/api/v1/composite-templates/detect-params",
            json={
                "nodes": [
                    {"id": "n1", "prompt": "{{parameter.x}}"},
                    {"id": "n2", "agent_prompt": "{{parameter.x}} {{parameter.y}}"},
                ]
            },
        )
        assert resp.status_code == 200
        names = [p["name"] for p in resp.json()["ports"]]
        assert names.count("x") == 1
        assert "y" in names

    def test_detect_params_non_string_field_ignored(self, client: TestClient) -> None:
        resp = client.post(
            "/api/v1/composite-templates/detect-params",
            json={"nodes": [{"id": "n1", "prompt_template": 123}]},
        )
        assert resp.status_code == 200
        assert not resp.json()["ports"]

    def test_detect_params_unexpected_error_returns_500(self, client: TestClient) -> None:
        with patch(
            "modulo.api.routes.composite_templates._detect_parameter_ports",
            side_effect=RuntimeError("boom"),
        ):
            resp = client.post(
                "/api/v1/composite-templates/detect-params",
                json={"nodes": []},
            )
        assert resp.status_code == 500


# ---------------------------------------------------------------------------
# _detect_parameter_ports — pure function unit tests
# ---------------------------------------------------------------------------


class TestDetectParameterPorts:
    def test_finds_placeholders_in_prompt_template(self) -> None:
        from modulo.api.routes.composite_templates import _detect_parameter_ports

        nodes = [{"id": "n1", "prompt_template": "Use {{parameter.style}} and {{parameter.tone}}"}]
        ports = _detect_parameter_ports(nodes)
        names = {p.name for p in ports}
        assert names == {"style", "tone"}

    def test_ignores_non_prompt_fields(self) -> None:
        from modulo.api.routes.composite_templates import _detect_parameter_ports

        nodes = [{"id": "n1", "name": "not a prompt {{parameter.x}}"}]
        ports = _detect_parameter_ports(nodes)
        assert not ports

    def test_returns_empty_for_empty_nodes(self) -> None:
        from modulo.api.routes.composite_templates import _detect_parameter_ports

        assert not _detect_parameter_ports([])


# ---------------------------------------------------------------------------
# Update with parameter_ports_json
# ---------------------------------------------------------------------------


class TestUpdateParameterPortsJson:
    def test_update_with_parameter_ports_serializes_port_dicts(self, client: TestClient) -> None:
        template = _make_template(
            parameter_ports_json=[
                {
                    "id": "p1",
                    "name": "x",
                    "label": "X",
                    "type": "string",
                    "target_injection": {
                        "mode": "prompt_replace",
                        "node_id": "n1",
                        "injection_point": "prompt_template",
                    },
                }
            ]
        )
        with (
            patch("modulo.api.routes.composite_templates.update_composite_template", return_value=template) as upd,
            patch("modulo.api.routes.composite_templates.set_rls_org"),
        ):
            resp = client.patch(
                f"/api/v1/composite-templates/{_TEMPLATE_ID}",
                json={
                    "parameter_ports_json": [
                        {
                            "id": "p1",
                            "name": "x",
                            "label": "X",
                            "type": "string",
                            "target_injection": {
                                "mode": "prompt_replace",
                                "node_id": "n1",
                                "injection_point": "prompt_template",
                            },
                        }
                    ]
                },
            )
        assert resp.status_code == 200
        call_kwargs = upd.call_args
        updates = call_kwargs[0][2]
        assert isinstance(updates["parameter_ports_json"], list)


class TestCompositeTemplateMasking:
    """FAR-1181: composite templates must never surface raw secrets.

    Composite templates are readable by every org member and
    ``save-as-composite`` can copy a pipeline's credential-bearing node fields
    verbatim, so every read surface masks node ``env_vars`` / ``context_files``
    / parameter-value fields the same way the pipeline graph read does.
    """

    _SECRET_TOKEN = "ghp_" + "0123456789abcdef" * 2 + "fedcba98"

    def _graph(self) -> dict[str, object]:
        return {
            "nodes": [
                {
                    "id": "n1",
                    "node_type": "agent",
                    "agent_id": "00000000-0000-0000-0000-000000000005",
                    "label": "Secret Node",
                    "env_vars": {
                        "GITHUB_TOKEN": self._SECRET_TOKEN,
                        "APP_URL": "https://example.com",
                    },
                    "context_files": {"/tmp/staging/creds.txt": f"token={self._SECRET_TOKEN}"},
                }
            ],
            "edges": [],
        }

    def test_get_template_masks_secret_env_and_context_files(self, client: TestClient) -> None:
        from modulo.api.middleware.sensitive_mask import SENSITIVE_VALUE_MASK

        template = _make_template(sub_pipeline_graph_json=self._graph())
        with (
            patch("modulo.api.routes.composite_templates.get_composite_template", return_value=template),
            patch("modulo.api.routes.composite_templates.set_rls_org"),
        ):
            resp = client.get(f"/api/v1/composite-templates/{_TEMPLATE_ID}")
        assert resp.status_code == 200
        node = resp.json()["sub_pipeline_graph_json"]["nodes"][0]
        assert node["env_vars"] == {
            "GITHUB_TOKEN": SENSITIVE_VALUE_MASK,
            "APP_URL": "https://example.com",
        }
        assert node["context_files"] == {"/tmp/staging/creds.txt": "token=" + SENSITIVE_VALUE_MASK}

    def test_list_templates_masks_graph_nodes(self, client: TestClient) -> None:
        from modulo.api.middleware.sensitive_mask import SENSITIVE_VALUE_MASK

        template = _make_template(sub_pipeline_graph_json=self._graph())
        page_result = MagicMock(items=[template], total=1, page=1, page_size=20)
        with (
            patch("modulo.api.routes.composite_templates.list_composite_templates", return_value=page_result),
            patch("modulo.api.routes.composite_templates.set_rls_org"),
        ):
            resp = client.get("/api/v1/composite-templates")
        assert resp.status_code == 200
        node = resp.json()["items"][0]["sub_pipeline_graph_json"]["nodes"][0]
        assert node["env_vars"]["GITHUB_TOKEN"] == SENSITIVE_VALUE_MASK
        assert "https://example.com" in node["env_vars"]["APP_URL"]

    def test_restore_masks_graph_nodes(self, client: TestClient) -> None:
        from modulo.api.middleware.sensitive_mask import SENSITIVE_VALUE_MASK

        template = _make_template(sub_pipeline_graph_json=self._graph())
        with (
            patch("modulo.api.routes.composite_templates.restore_composite_template", return_value=template),
            patch("modulo.api.routes.composite_templates.set_rls_org"),
        ):
            resp = client.post(f"/api/v1/composite-templates/{_TEMPLATE_ID}/restore")
        assert resp.status_code == 200
        node = resp.json()["sub_pipeline_graph_json"]["nodes"][0]
        assert node["env_vars"]["GITHUB_TOKEN"] == SENSITIVE_VALUE_MASK

    def test_get_editor_masks_secret_env(self, client: TestClient) -> None:
        from modulo.api.middleware.sensitive_mask import SENSITIVE_VALUE_MASK

        template = _make_template(sub_pipeline_graph_json=self._graph())
        with (
            patch("modulo.api.routes.composite_templates.get_composite_template", return_value=template),
            patch("modulo.api.routes.composite_templates.set_rls_org"),
        ):
            resp = client.get(f"/api/v1/composite-templates/{_TEMPLATE_ID}/editor")
        assert resp.status_code == 200
        node = resp.json()["nodes"][0]
        assert node["env_vars"]["GITHUB_TOKEN"] == SENSITIVE_VALUE_MASK

    def test_editor_put_resolves_mask_echo_and_masks_response(self, client: TestClient) -> None:
        from modulo.api.middleware.sensitive_mask import SENSITIVE_VALUE_MASK

        template = _make_template(sub_pipeline_graph_json=self._graph())
        with (
            patch(
                "modulo.api.routes.composite_templates.get_composite_template",
                return_value=template,
            ),
            patch(
                "modulo.api.routes.composite_templates.update_composite_template",
                return_value=template,
            ) as upd,
            patch("modulo.api.routes.composite_templates.set_rls_org"),
        ):
            # The editor round-trips the masked GET: GITHUB_TOKEN comes back
            # as the mask literal; APP_URL and the non-sensitive string are
            # passed through as-is. The mask echo must be restored from the
            # stored template node — never persisted as an overwrite.
            resp = client.put(
                f"/api/v1/composite-templates/{_TEMPLATE_ID}/editor",
                json={
                    "nodes": [
                        {
                            "id": "n1",
                            "node_type": "agent",
                            "agent_id": "00000000-0000-0000-0000-000000000005",
                            "label": "Secret Node",
                            "env_vars": {
                                "GITHUB_TOKEN": SENSITIVE_VALUE_MASK,
                                "APP_URL": "https://example.com",
                            },
                            "context_files": {"/tmp/staging/creds.txt": SENSITIVE_VALUE_MASK},
                        }
                    ],
                    "edges": [],
                },
            )
        assert resp.status_code == 200
        graph = upd.await_args.args[2]["sub_pipeline_graph_json"]
        node = graph["nodes"][0]
        assert node["env_vars"] == {
            "GITHUB_TOKEN": self._SECRET_TOKEN,
            "APP_URL": "https://example.com",
        }
        assert node["context_files"] == {"/tmp/staging/creds.txt": f"token={self._SECRET_TOKEN}"}
        response_node = resp.json()["nodes"][0]
        assert response_node["env_vars"]["GITHUB_TOKEN"] == SENSITIVE_VALUE_MASK

    def test_editor_put_drops_mask_echo_without_stored_counterpart(self, client: TestClient) -> None:
        from modulo.api.middleware.sensitive_mask import SENSITIVE_VALUE_MASK

        graph = self._graph()
        template = _make_template(sub_pipeline_graph_json=graph)
        with (
            patch(
                "modulo.api.routes.composite_templates.get_composite_template",
                return_value=template,
            ),
            patch(
                "modulo.api.routes.composite_templates.update_composite_template",
                return_value=template,
            ) as upd,
            patch("modulo.api.routes.composite_templates.set_rls_org"),
        ):
            resp = client.put(
                f"/api/v1/composite-templates/{_TEMPLATE_ID}/editor",
                json={
                    "nodes": [
                        {
                            "id": "n1",
                            "node_type": "agent",
                            "agent_id": "00000000-0000-0000-0000-000000000005",
                            "env_vars": {
                                "GITHUB_TOKEN": SENSITIVE_VALUE_MASK,
                                "NEW_VAR": SENSITIVE_VALUE_MASK,
                            },
                            "context_files": {"p": SENSITIVE_VALUE_MASK},
                        }
                    ],
                    "edges": [],
                },
            )
        assert resp.status_code == 200
        node = upd.await_args.args[2]["sub_pipeline_graph_json"]["nodes"][0]
        # NEW_VAR has no stored counterpart — mask echo dropped, not persisted.
        assert "NEW_VAR" not in node["env_vars"]

    def test_patch_resolves_mask_echoes_before_storing_graph(self, client: TestClient) -> None:
        from modulo.api.middleware.sensitive_mask import SENSITIVE_VALUE_MASK

        template_after = _make_template(sub_pipeline_graph_json=self._graph())
        template_before = _make_template(sub_pipeline_graph_json=self._graph())
        with (
            patch(
                "modulo.api.routes.composite_templates.get_composite_template",
                return_value=template_before,
            ),
            patch(
                "modulo.api.routes.composite_templates.update_composite_template",
                return_value=template_after,
            ) as upd,
            patch("modulo.api.routes.composite_templates.set_rls_org"),
        ):
            resp = client.patch(
                f"/api/v1/composite-templates/{_TEMPLATE_ID}",
                json={
                    "sub_pipeline_graph_json": {
                        "nodes": [
                            {
                                "id": "n1",
                                "node_type": "agent",
                                "agent_id": "00000000-0000-0000-0000-000000000005",
                                "env_vars": {
                                    "GITHUB_TOKEN": SENSITIVE_VALUE_MASK,
                                    "APP_URL": "https://example.com",
                                },
                                "context_files": {"/tmp/staging/creds.txt": SENSITIVE_VALUE_MASK},
                            }
                        ],
                        "edges": [],
                    }
                },
            )
        assert resp.status_code == 200
        updates = upd.await_args.args[2]
        node = updates["sub_pipeline_graph_json"]["nodes"][0]
        assert node["env_vars"] == {
            "GITHUB_TOKEN": self._SECRET_TOKEN,
            "APP_URL": "https://example.com",
        }
        response_node = resp.json()["sub_pipeline_graph_json"]["nodes"][0]
        assert response_node["env_vars"]["GITHUB_TOKEN"] == SENSITIVE_VALUE_MASK

    def test_mask_sub_pipeline_graph_non_dict_returns_empty(self) -> None:
        """A missing / non-dict graph masks to an empty graph, never echoes it."""
        from modulo.api.routes.composite_templates import _mask_sub_pipeline_graph

        assert not _mask_sub_pipeline_graph(None)
        assert not _mask_sub_pipeline_graph("not-a-graph")  # type: ignore[arg-type]

    def test_patch_graph_missing_template_returns_404(self, client: TestClient) -> None:
        """A graph-bearing PATCH on a vanished template 404s before the write.

        The mask-echo resolver reads the current template to restore echoed
        secrets; if the template is gone there is nothing to resolve against, so
        the request must fail rather than persist a mask literal.
        """
        with (
            patch("modulo.api.routes.composite_templates.get_composite_template", return_value=None),
            patch("modulo.api.routes.composite_templates.set_rls_org"),
        ):
            resp = client.patch(
                f"/api/v1/composite-templates/{uuid.uuid4()}",
                json={"sub_pipeline_graph_json": {"nodes": [{"id": "n1"}], "edges": []}},
            )
        assert resp.status_code == 404

    def test_patch_graph_with_non_dict_stored_graph_keeps_incoming_nodes(self, client: TestClient) -> None:
        """A stored graph that is not a dict yields no stored nodes to merge.

        The echo resolver must tolerate a null / malformed stored graph on an
        otherwise valid template rather than raising, and take the incoming
        nodes wholesale when there is nothing to resolve against.
        """
        stored = _make_template(sub_pipeline_graph_json=None)
        stored_after = _make_template(sub_pipeline_graph_json={"nodes": [{"id": "n1"}], "edges": []})
        with (
            patch("modulo.api.routes.composite_templates.get_composite_template", return_value=stored),
            patch(
                "modulo.api.routes.composite_templates.update_composite_template",
                return_value=stored_after,
            ) as upd,
            patch("modulo.api.routes.composite_templates.set_rls_org"),
        ):
            resp = client.patch(
                f"/api/v1/composite-templates/{_TEMPLATE_ID}",
                json={"sub_pipeline_graph_json": {"nodes": [{"id": "n1"}], "edges": []}},
            )
        assert resp.status_code == 200
        assert upd.await_args.args[2]["sub_pipeline_graph_json"]["nodes"] == [{"id": "n1"}]
