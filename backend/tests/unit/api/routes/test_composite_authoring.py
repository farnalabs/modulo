"""Unit tests for composite authoring flow.

Covers:
- Composite editor endpoints (GET/PUT /{id}/editor)
- Publish endpoint (POST /{id}/publish)
- Save-as-composite endpoint (POST /pipelines/{id}/save-as-composite)
- Auto-detection of parameter placeholders
"""

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
_TEMPLATE_ID = uuid.UUID("00000000-0000-0000-0000-000000000003")
_PIPELINE_ID = uuid.UUID("00000000-0000-0000-0000-000000000004")
_AGENT_ID = uuid.UUID("00000000-0000-0000-0000-000000000005")
_NOW = datetime(2025, 1, 1, tzinfo=UTC)

# Shared mock session reference for save-as-composite tests
_mock_session: AsyncMock | None = None


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
    t.description = overrides.get("description", None)
    t.sub_pipeline_graph_json = overrides.get("sub_pipeline_graph_json", {"nodes": [], "edges": []})
    t.parameter_ports_json = overrides.get("parameter_ports_json", [])
    t.input_schema_id = overrides.get("input_schema_id", None)
    t.output_schema_id = overrides.get("output_schema_id", None)
    t.version = overrides.get("version", "1.0.0")
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


def _make_pipeline_mock(**overrides: object) -> MagicMock:
    p = MagicMock()
    p.id = overrides.get("id", _PIPELINE_ID)
    p.organisation_id = _ORG_ID
    p.name = overrides.get("name", "Test Pipeline")
    p.description = overrides.get("description", None)
    p.graph_nodes_json = overrides.get(
        "graph_nodes_json",
        [
            {
                "id": str(uuid.UUID("00000000-0000-0000-0000-000000000010")),
                "node_type": "agent", "agent_id": str(_AGENT_ID), "label": "Agent 1",
            },
            {
                "id": str(uuid.UUID("00000000-0000-0000-0000-000000000011")),
                "node_type": "manual", "label": "Manual 1",
            },
        ],
    )
    p.version = overrides.get("version", "1.0.0")
    return p


def _make_agent_mock() -> MagicMock:
    a = MagicMock()
    a.id = _AGENT_ID
    a.organisation_id = _ORG_ID
    a.name = "Test Agent"
    a.prompt_template = (
        "Analyze this input and provide {{parameter.tone}} feedback "
        "with {{parameter.max_length}} words."
    )
    return a


@pytest.fixture()
def client() -> Generator[TestClient, None, None]:
    global _mock_session
    _mock_session = _make_mock_session()

    session = _mock_session
    async def override_session() -> AsyncGenerator[AsyncMock, None]:
        yield session

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
    _mock_session = None


@pytest.fixture()
def unauth_client() -> Generator[TestClient, None, None]:
    app.dependency_overrides[get_settings] = _make_settings
    yield TestClient(app)
    app.dependency_overrides.clear()


class TestCompositeEditor:
    """GET/PUT /api/v1/composite-templates/{id}/editor"""

    def test_get_editor_returns_graph(self, client: TestClient) -> None:
        template = _make_template(
            sub_pipeline_graph_json={
                "nodes": [{"id": "n1", "node_type": "agent"}],
                "edges": [{"id": "e1", "source": "n1", "target": "n2"}],
            },
        )
        with (
            patch("modulo.api.routes.composite_templates.get_composite_template", return_value=template),
            patch("modulo.api.routes.composite_templates.set_rls_org"),
        ):
            resp = client.get(f"/api/v1/composite-templates/{_TEMPLATE_ID}/editor")
        assert resp.status_code == 200
        data = resp.json()
        assert len(data["nodes"]) == 1
        assert data["nodes"][0]["id"] == "n1"
        assert len(data["edges"]) == 1

    def test_get_editor_404(self, client: TestClient) -> None:
        with (
            patch("modulo.api.routes.composite_templates.get_composite_template", return_value=None),
            patch("modulo.api.routes.composite_templates.set_rls_org"),
        ):
            resp = client.get(f"/api/v1/composite-templates/{uuid.uuid4()}/editor")
        assert resp.status_code == 404

    def test_save_editor_saves_graph(self, client: TestClient) -> None:
        template = _make_template(
            sub_pipeline_graph_json={
                "nodes": [{"id": "n1", "node_type": "agent"}],
                "edges": [],
            },
        )
        with (
            patch("modulo.api.routes.composite_templates.update_composite_template", return_value=template),
            patch("modulo.api.routes.composite_templates.set_rls_org"),
        ):
            resp = client.put(
                f"/api/v1/composite-templates/{_TEMPLATE_ID}/editor",
                json={
                    "nodes": [{"id": "n1", "node_type": "agent"}],
                    "edges": [],
                },
            )
        assert resp.status_code == 200
        data = resp.json()
        assert len(data["nodes"]) == 1

    def test_save_editor_404(self, client: TestClient) -> None:
        with (
            patch("modulo.api.routes.composite_templates.update_composite_template", return_value=None),
            patch("modulo.api.routes.composite_templates.set_rls_org"),
        ):
            resp = client.put(
                f"/api/v1/composite-templates/{uuid.uuid4()}/editor",
                json={"nodes": [], "edges": []},
            )
        assert resp.status_code == 404


class TestCompositePublish:
    """POST /api/v1/composite-templates/{id}/publish"""

    def test_publish_sets_version_default(self, client: TestClient) -> None:
        template = _make_template(version="1.0.0")
        with (
            patch("modulo.api.routes.composite_templates.update_composite_template", return_value=template),
            patch("modulo.api.routes.composite_templates.set_rls_org"),
        ):
            resp = client.post(f"/api/v1/composite-templates/{_TEMPLATE_ID}/publish", json={})
        assert resp.status_code == 200
        data = resp.json()
        assert data["version"] == "1.0.0"
        assert data["published"] is True

    def test_publish_with_custom_version(self, client: TestClient) -> None:
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
        data = resp.json()
        assert data["version"] == "2.0.0"

    def test_publish_404(self, client: TestClient) -> None:
        with (
            patch("modulo.api.routes.composite_templates.update_composite_template", return_value=None),
            patch("modulo.api.routes.composite_templates.set_rls_org"),
        ):
            resp = client.post(f"/api/v1/composite-templates/{uuid.uuid4()}/publish", json={})
        assert resp.status_code == 404

    def test_publish_unauthorized(self, unauth_client: TestClient) -> None:
        resp = unauth_client.post(f"/api/v1/composite-templates/{_TEMPLATE_ID}/publish", json={})
        assert resp.status_code in (401, 403)


class TestSaveAsComposite:
    """POST /api/v1/pipelines/{id}/save-as-composite"""

    def test_save_as_composite_creates_template(self, client: TestClient) -> None:
        pipeline = _make_pipeline_mock()
        template = _make_template(name="Saved Composite", version="0.1.0")

        empty_execute = MagicMock()
        empty_execute.scalars.return_value.all.return_value = []

        selected_ids = [
            "00000000-0000-0000-0000-000000000010",
            "00000000-0000-0000-0000-000000000011",
        ]

        with (
            patch("modulo.api.routes.pipelines.get_pipeline", return_value=pipeline),
            patch("modulo.api.routes.pipelines.set_rls_org"),
            patch("modulo.api.routes.pipelines.set_rls_user_context"),
            patch("modulo.api.routes.pipelines.create_composite_template", return_value=template),
        ):
            assert _mock_session is not None
            _mock_session.execute = AsyncMock(return_value=empty_execute)
            resp = client.post(
                f"/api/v1/pipelines/{_PIPELINE_ID}/save-as-composite",
                json={
                    "name": "Saved Composite",
                    "description": "A saved composite",
                    "selected_node_ids": selected_ids,
                },
            )
        assert resp.status_code == 201
        data = resp.json()
        assert data["name"] == "Saved Composite"
        assert data["version"] == "0.1.0"

    def test_save_as_composite_auto_detects_parameters(self, client: TestClient) -> None:
        pipeline = _make_pipeline_mock()
        agent = _make_agent_mock()
        template = _make_template(name="Param Composite", version="0.1.0")

        scalars_result = MagicMock()
        scalars_result.scalars.return_value.all.return_value = [agent]

        selected_ids = ["00000000-0000-0000-0000-000000000010"]

        with (
            patch("modulo.api.routes.pipelines.get_pipeline", return_value=pipeline),
            patch("modulo.api.routes.pipelines.set_rls_org"),
            patch("modulo.api.routes.pipelines.set_rls_user_context"),
            patch("modulo.api.routes.pipelines.create_composite_template", return_value=template),
        ):
            assert _mock_session is not None
            _mock_session.execute = AsyncMock(return_value=scalars_result)
            resp = client.post(
                f"/api/v1/pipelines/{_PIPELINE_ID}/save-as-composite",
                json={
                    "name": "Param Composite",
                    "selected_node_ids": selected_ids,
                },
            )
        assert resp.status_code == 201
        data = resp.json()
        ports = data.get("parameter_ports", [])
        port_names = [p["name"] for p in ports]
        assert "tone" in port_names
        assert "max_length" in port_names

    def test_save_as_composite_no_valid_nodes_returns_422(self, client: TestClient) -> None:
        pipeline = _make_pipeline_mock()

        with (
            patch("modulo.api.routes.pipelines.get_pipeline", return_value=pipeline),
            patch("modulo.api.routes.pipelines.set_rls_org"),
            patch("modulo.api.routes.pipelines.set_rls_user_context"),
        ):
            resp = client.post(
                f"/api/v1/pipelines/{_PIPELINE_ID}/save-as-composite",
                json={
                    "name": "Test",
                    "selected_node_ids": [str(uuid.uuid4())],
                },
            )
        assert resp.status_code == 422

    def test_save_as_composite_pipeline_not_found(self, client: TestClient) -> None:
        with (
            patch("modulo.api.routes.pipelines.get_pipeline", return_value=None),
            patch("modulo.api.routes.pipelines.set_rls_org"),
            patch("modulo.api.routes.pipelines.set_rls_user_context"),
        ):
            resp = client.post(
                f"/api/v1/pipelines/{_PIPELINE_ID}/save-as-composite",
                json={
                    "name": "Test",
                    "selected_node_ids": [str(uuid.uuid4())],
                },
            )
        assert resp.status_code == 404

    def test_save_as_composite_unauthorized(self, unauth_client: TestClient) -> None:
        resp = unauth_client.post(
            f"/api/v1/pipelines/{_PIPELINE_ID}/save-as-composite",
            json={"name": "Test", "selected_node_ids": [str(uuid.uuid4())]},
        )
        assert resp.status_code in (401, 403)
