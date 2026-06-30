"""Unit tests for /api/v1/libraries endpoints."""

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
_NOW = datetime(2025, 1, 1, tzinfo=UTC)


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


def _make_primitive(
    *,
    pid: uuid.UUID | None = None,
    primitive_type: str = "pipeline_template",
    name: str = "PR Review Pipeline",
    tags: list[str] | None = None,
) -> MagicMock:
    p = MagicMock()
    p.id = pid or uuid.uuid4()
    p.organisation_id = _ORG_ID
    p.source = "local"
    p.primitive_type = primitive_type
    p.name = name
    p.slug = "pr-review-pipeline"
    p.description = "Automated PR review pipeline"
    p.author = "modulo"
    p.version = "1.0"
    p.tags = tags or ["pipeline_template", "code-review"]
    p.content_json = {
        "agents": [
            {
                "name": "Issue Reader",
                "description": "Reads a GitHub issue and extracts structured spec",
                "prompt_template": "Read the following issue:\n{{ input }}",
                "connector_type_refs": [{"connector_type": "github", "capabilities": ["issue_read"]}],
            },
            {
                "name": "Code Analyzer",
                "description": "Analyzes code diff for issues",
                "prompt_template": "Analyze this code:\n{{ input }}",
                "connector_type_refs": [],
            },
        ],
        "graph_nodes": [
            {
                "id": "reader-node",
                "agent_index": 0,
                "label": "Issue Reader",
                "position": {"x": 50, "y": 100},
            },
            {
                "id": "analyzer-node",
                "agent_index": 1,
                "label": "Code Analyzer",
                "position": {"x": 350, "y": 100},
            },
            {
                "id": "review-gate",
                "node_type": "manual",
                "label": "Review Gate",
                "position": {"x": 650, "y": 100},
            },
        ],
        "edges": [
            {"source": "reader-node", "target": "analyzer-node", "edge_type": "normal"},
            {"source": "analyzer-node", "target": "review-gate", "edge_type": "normal"},
        ],
        "connector_type_refs": ["github"],
        "schema_refs": [],
        "category": "code-review",
    }
    p.visibility = "community"
    p.created_at = _NOW
    p.updated_at = _NOW
    return p


def _make_pipeline() -> MagicMock:
    p = MagicMock()
    p.id = uuid.uuid4()
    p.organisation_id = _ORG_ID
    p.name = "Pipeline from Template"
    p.description = None
    p.visibility = "org"
    p.graph_nodes_json = []
    p.max_concurrent_runs = 5
    p.lock_wait_timeout_seconds = 300
    p.node_timeout_seconds = 300
    p.run_context_defaults = {}
    p.default_autonomy_level = "manual_approval"
    p.created_by = _USER_ID
    p.created_at = _NOW
    p.updated_at = _NOW
    return p


# ---------------------------------------------------------------------------
# POST /api/v1/libraries/{id}/create-pipeline
# ---------------------------------------------------------------------------


def test_create_pipeline_from_template_returns_201(client: TestClient) -> None:
    primitive_id = uuid.uuid4()
    primitive = _make_primitive(pid=primitive_id)
    pipeline = _make_pipeline()
    pipeline.name = "PR Review Pipeline"
    pipeline.description = "My custom PR pipeline"

    with (
        patch("modulo.api.routes.library.get_primitive", return_value=primitive),
        patch("modulo.api.routes.library.create_pipeline", return_value=pipeline) as create_mock,
        patch("modulo.api.routes.library.set_rls_org"),
        patch("modulo.api.routes.library.set_rls_user_context"),
    ):
        resp = client.post(
            f"/api/v1/libraries/{primitive_id}/create-pipeline",
            json={"name": "PR Review Pipeline", "description": "My custom PR pipeline"},
        )

    assert resp.status_code == 201
    body = resp.json()
    assert body["name"] == "PR Review Pipeline"
    assert body["description"] == "My custom PR pipeline"
    assert body["template_source_id"] == str(primitive_id)
    assert body["agent_count"] == 2
    assert body["edge_count"] == 2
    assert body["ready_to_run"] is True

    # Verify create_pipeline was called with correct args
    call_kwargs = create_mock.await_args.kwargs
    assert call_kwargs["org_id"] == _ORG_ID
    assert call_kwargs["created_by"] == _USER_ID
    assert call_kwargs["name"] == "PR Review Pipeline"
    assert call_kwargs["description"] == "My custom PR pipeline"
    assert call_kwargs["run_context_defaults"]["library_source_id"] == str(primitive_id)
    assert call_kwargs["run_context_defaults"]["library_template_name"] == "PR Review Pipeline"


def test_create_pipeline_from_template_default_name(client: TestClient) -> None:
    primitive_id = uuid.uuid4()
    primitive = _make_primitive(pid=primitive_id, name="Release Checklist")
    pipeline = _make_pipeline()
    pipeline.name = "Release Checklist"

    with (
        patch("modulo.api.routes.library.get_primitive", return_value=primitive),
        patch("modulo.api.routes.library.create_pipeline", return_value=pipeline),
        patch("modulo.api.routes.library.set_rls_org"),
        patch("modulo.api.routes.library.set_rls_user_context"),
    ):
        resp = client.post(
            f"/api/v1/libraries/{primitive_id}/create-pipeline",
            json={},
        )

    assert resp.status_code == 201
    assert resp.json()["name"] == "Release Checklist"


def test_create_pipeline_from_primitive_not_found(client: TestClient) -> None:
    with (
        patch("modulo.api.routes.library.get_primitive", return_value=None),
        patch("modulo.api.routes.library.set_rls_org"),
        patch("modulo.api.routes.library.set_rls_user_context"),
    ):
        resp = client.post(
            f"/api/v1/libraries/{uuid.uuid4()}/create-pipeline",
            json={},
        )

    assert resp.status_code == 404


def test_create_pipeline_from_invalid_type(client: TestClient) -> None:
    primitive_id = uuid.uuid4()
    primitive = _make_primitive(pid=primitive_id, primitive_type="schema")

    with (
        patch("modulo.api.routes.library.get_primitive", return_value=primitive),
        patch("modulo.api.routes.library.set_rls_org"),
        patch("modulo.api.routes.library.set_rls_user_context"),
    ):
        resp = client.post(
            f"/api/v1/libraries/{primitive_id}/create-pipeline",
            json={},
        )

    assert resp.status_code == 400
    assert "pipeline_template" in resp.json()["detail"]
    assert "schema" in resp.json()["detail"]
