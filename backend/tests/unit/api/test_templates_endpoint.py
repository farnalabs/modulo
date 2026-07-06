"""Unit tests for /api/v1/templates and /api/v1/pipelines/from-template endpoints."""

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
_NOW = datetime(2025, 1, 1, tzinfo=UTC)


def _make_settings() -> Settings:
    return Settings(
        database_url="postgresql+asyncpg://localhost/test",
        secret_key=_VALID_32,
        fernet_key=_VALID_32,
        modulo_admin_password="testpass",
    )


def _make_template_primitive(**overrides: object) -> MagicMock:
    p = MagicMock()
    defaults = {
        "id": uuid.uuid4(),
        "name": "Test Template",
        "description": "A test pipeline template",
        "category": "code-review",
        "tags": ["pipeline_template", "test"],
        "primitive_type": "pipeline_template",
        "content_json": {
            "agents": [
                {
                    "name": "Agent One",
                    "description": "First agent",
                    "prompt_template": "Do something",
                    "connector_type_refs": [],
                    "required_environment_capabilities": [],
                }
            ],
            "graph_nodes": [
                {
                    "id": "node-1",
                    "node_type": "agent",
                    "agent_index": 0,
                    "label": "Agent One",
                    "position": {"x": 100, "y": 100},
                }
            ],
            "edges": [],
            "connector_type_refs": [],
            "schema_refs": [],
            "category": "code-review",
        },
        "created_at": _NOW,
        "updated_at": _NOW,
    }
    defaults.update(overrides)
    for k, v in defaults.items():
        setattr(p, k, v)
    return p


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


# ---------------------------------------------------------------------------
# GET /api/v1/templates
# ---------------------------------------------------------------------------


def test_list_templates_returns_200(client: TestClient) -> None:
    template_prim = _make_template_primitive()
    page_result = MagicMock()
    page_result.items = [template_prim]
    page_result.total = 1
    page_result.page = 1
    page_result.page_size = 20

    with patch("modulo.api.routes.templates.list_templates", new_callable=AsyncMock) as mock_list:
        mock_list.return_value = page_result
        resp = client.get("/api/v1/templates")

    assert resp.status_code == 200
    data = resp.json()
    assert data["total"] == 1
    assert len(data["items"]) == 1
    item = data["items"][0]
    assert item["name"] == "Test Template"
    assert item["agent_count"] == 1
    mock_list.assert_awaited_once()


def test_list_templates_with_category_filter(client: TestClient) -> None:
    template_prim = _make_template_primitive(category="release")
    page_result = MagicMock()
    page_result.items = [template_prim]
    page_result.total = 1
    page_result.page = 1
    page_result.page_size = 20

    with patch("modulo.api.routes.templates.list_templates", new_callable=AsyncMock) as mock_list:
        mock_list.return_value = page_result
        resp = client.get("/api/v1/templates?category=release")

    assert resp.status_code == 200
    data = resp.json()
    assert data["items"][0]["category"] == "release"
    mock_list.assert_awaited_once()


def test_list_templates_search(client: TestClient) -> None:
    template_prim = _make_template_primitive(name="PR Review")
    page_result = MagicMock()
    page_result.items = [template_prim]
    page_result.total = 1
    page_result.page = 1
    page_result.page_size = 20

    with patch("modulo.api.routes.templates.list_templates", new_callable=AsyncMock) as mock_list:
        mock_list.return_value = page_result
        resp = client.get("/api/v1/templates?search=PR")

    assert resp.status_code == 200
    assert len(resp.json()["items"]) == 1
    mock_list.assert_awaited_once()


def test_list_templates_empty(client: TestClient) -> None:
    page_result = MagicMock()
    page_result.items = []
    page_result.total = 0
    page_result.page = 1
    page_result.page_size = 20

    with patch("modulo.api.routes.templates.list_templates", new_callable=AsyncMock) as mock_list:
        mock_list.return_value = page_result
        resp = client.get("/api/v1/templates")

    assert resp.status_code == 200
    assert resp.json()["total"] == 0


def test_list_templates_requires_auth(unauth_client: TestClient) -> None:
    resp = unauth_client.get("/api/v1/templates")
    assert resp.status_code == 403 or resp.status_code == 401


# ---------------------------------------------------------------------------
# POST /api/v1/pipelines/from-template/{id}
# ---------------------------------------------------------------------------


def test_create_from_template_returns_201(client: TestClient) -> None:
    template_id = uuid.uuid4()
    template_prim = _make_template_primitive(
        id=template_id,
        name="PR Review Pipeline",
        description="Review PRs automatically",
        content_json={
            "agents": [
                {
                    "name": "Issue Reader",
                    "description": "Reads a GitHub issue",
                    "prompt_template": "Read: {{ input }}",
                    "connector_type_refs": [],
                    "required_environment_capabilities": [],
                }
            ],
            "graph_nodes": [
                {
                    "id": "node-1",
                    "node_type": "agent",
                    "agent_index": 0,
                    "label": "Issue Reader",
                    "position": {"x": 100, "y": 100},
                }
            ],
            "edges": [],
            "connector_type_refs": [],
            "schema_refs": [],
            "category": "code-review",
        },
    )

    mock_pipeline = MagicMock()
    mock_pipeline.id = uuid.uuid4()
    mock_pipeline.name = "PR Review Pipeline (from template)"

    with (
        patch("modulo.api.routes.templates.get_template", new_callable=AsyncMock) as mock_get,
        patch("modulo.api.routes.templates.create_pipeline", new_callable=AsyncMock) as mock_create,
    ):
        mock_get.return_value = template_prim
        mock_create.return_value = mock_pipeline

        resp = client.post(f"/api/v1/pipelines/from-template/{template_id}")

    assert resp.status_code == 201
    data = resp.json()
    assert data["pipeline_id"] == str(mock_pipeline.id)
    assert data["pipeline_name"] == mock_pipeline.name
    assert data["agent_count"] == 1
    assert data["edge_count"] == 0
    mock_get.assert_awaited_once()
    mock_create.assert_awaited_once()


def test_create_from_template_not_found(client: TestClient) -> None:
    template_id = uuid.uuid4()

    with patch("modulo.api.routes.templates.get_template", new_callable=AsyncMock) as mock_get:
        mock_get.return_value = None
        resp = client.post(f"/api/v1/pipelines/from-template/{template_id}")

    assert resp.status_code == 404


def test_create_from_template_with_hitl_gate(client: TestClient) -> None:
    template_id = uuid.uuid4()
    template_prim = _make_template_primitive(
        id=template_id,
        content_json={
            "agents": [
                {
                    "name": "Agent A",
                    "description": "First",
                    "prompt_template": "Do A",
                    "connector_type_refs": [],
                    "required_environment_capabilities": [],
                },
                {
                    "name": "Agent B",
                    "description": "Second",
                    "prompt_template": "Do B",
                    "connector_type_refs": [],
                    "required_environment_capabilities": [],
                },
            ],
            "graph_nodes": [
                {"id": "n1", "node_type": "agent", "agent_index": 0, "label": "A", "position": {"x": 0, "y": 0}},
                {"id": "n2", "node_type": "manual", "label": "Gate", "position": {"x": 200, "y": 0}},
                {"id": "n3", "node_type": "agent", "agent_index": 1, "label": "B", "position": {"x": 400, "y": 0}},
            ],
            "edges": [
                {"source_node_id": "n1", "target_node_id": "n2", "edge_type": "normal"},
                {
                    "source_node_id": "n2",
                    "target_node_id": "n3",
                    "edge_type": "normal",
                    "hitl_gate_config": {
                        "label": "Approve",
                        "description": "Review before proceeding",
                        "claim_expiry_minutes": 60,
                        "human_only": False,
                    },
                },
            ],
            "connector_type_refs": [],
            "schema_refs": [],
            "category": "code-review",
        },
    )

    mock_pipeline = MagicMock()
    mock_pipeline.id = uuid.uuid4()
    mock_pipeline.name = "Test (from template)"

    with (
        patch("modulo.api.routes.templates.get_template", new_callable=AsyncMock) as mock_get,
        patch("modulo.api.routes.templates.create_pipeline", new_callable=AsyncMock) as mock_create,
    ):
        mock_get.return_value = template_prim
        mock_create.return_value = mock_pipeline

        resp = client.post(f"/api/v1/pipelines/from-template/{template_id}")

    assert resp.status_code == 201
    data = resp.json()
    assert data["agent_count"] == 2
    assert data["edge_count"] == 2


# ---------------------------------------------------------------------------
# CRUD template helper tests
# ---------------------------------------------------------------------------


def test_template_response_from_primitive() -> None:
    from modulo.api.routes.templates import TemplateResponse

    prim = _make_template_primitive()
    response = TemplateResponse.from_primitive(prim)

    assert response.name == "Test Template"
    assert response.category == "code-review"
    assert response.agent_count == 1
    assert "nodes" in response.preview_data
    assert "edges" in response.preview_data


def test_agent_count_from_content() -> None:
    from modulo.db.crud.template import _agent_count_from_content

    content = {"agents": [{"name": "A"}, {"name": "B"}]}
    assert _agent_count_from_content(content) == 2

    assert _agent_count_from_content({}) == 0


def test_preview_data_from_content() -> None:
    from modulo.db.crud.template import _preview_data_from_content

    content = {
        "graph_nodes": [
            {"id": "n1", "label": "Node 1", "node_type": "agent"},
            {"id": "n2", "label": "Node 2", "node_type": "manual"},
        ],
        "edges": [
            {"source_node_id": "n1", "target_node_id": "n2", "edge_type": "normal"},
        ],
    }
    preview = _preview_data_from_content(content)
    assert len(preview["nodes"]) == 2
    assert len(preview["edges"]) == 1
    assert preview["nodes"][0]["label"] == "Node 1"
