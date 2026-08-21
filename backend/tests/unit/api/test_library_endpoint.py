"""Unit tests for /api/v1/libraries endpoints."""

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
from modulo.db.crud.base import PageResult
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


def _make_mock_session() -> AsyncMock:
    session = configure_mock_session(AsyncMock())
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
        username="vieweruser",
        organisation_id=_ORG_ID,
        account_id=_USER_ID,
        org_role="viewer",
    )
    mock_plan = MagicMock()
    mock_plan.feature_enabled.return_value = True
    app.dependency_overrides[get_plan_context] = lambda: mock_plan
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


def _make_listable_primitive(
    *,
    pid: uuid.UUID | None = None,
    primitive_type: str = "workflow",
    name: str = "PR Review Workflow",
    tier: str = "native",
) -> MagicMock:
    p = _make_primitive(pid=pid, primitive_type=primitive_type, name=name)
    p.source_url = None
    p.forked_from = None
    p.checksum = None
    p.ed25519_signature = None
    p.verified = None
    p.trust_tier = None
    p.tier = tier
    p.download_count = 0
    p.average_rating = None
    p.review_count = 0
    p.owner_team_id = None
    p.account_id = _USER_ID
    p.auto_update = True
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
    p.rate_limit_config = None
    p.max_duration_seconds = None
    p.archived_at = None
    p.snapshot_count = 0
    p.created_by = _USER_ID
    p.created_at = _NOW
    p.updated_at = _NOW
    return p


# ---------------------------------------------------------------------------
# GET /api/v1/libraries — multi-type filtering
# ---------------------------------------------------------------------------


def _stub_list_call(
    *,
    primitive_type: str | None = None,
    primitive_types: str | None = None,
    source: str | None = None,
    search: str | None = None,
    client: TestClient,
    prims: list[MagicMock] | None = None,
) -> MagicMock:
    result = PageResult(
        items=prims or [_make_listable_primitive()],
        total=1,
        page=1,
        page_size=20,
    )
    with (
        patch("modulo.api.routes.library.list_primitives", new_callable=AsyncMock, return_value=result) as mock_list,
        patch("modulo.api.routes.library.set_rls_org", new_callable=AsyncMock),
        patch("modulo.api.routes.library.set_rls_user_context", new_callable=AsyncMock),
    ):
        params: dict[str, str] = {}
        if primitive_type is not None:
            params["primitive_type"] = primitive_type
        if primitive_types is not None:
            params["primitive_types"] = primitive_types
        if source is not None:
            params["source"] = source
        if search is not None:
            params["search"] = search
        resp = client.get("/api/v1/libraries", params=params)
        assert resp.status_code == 200, resp.text
        return mock_list


def test_list_libraries_parses_multi_type_query_param(client: TestClient) -> None:
    mock_list = _stub_list_call(client=client, primitive_types="workflow,agent,schema")

    assert mock_list.await_args is not None
    call_kwargs = mock_list.await_args.kwargs
    assert call_kwargs["primitive_types"] == ["workflow", "agent", "schema"]
    assert call_kwargs["primitive_type"] is None


def test_list_libraries_multi_type_whitespace_normalized(client: TestClient) -> None:
    mock_list = _stub_list_call(client=client, primitive_types=" workflow , agent ,, schema ")

    assert mock_list.await_args is not None
    call_kwargs = mock_list.await_args.kwargs
    assert call_kwargs["primitive_types"] == ["workflow", "agent", "schema"]


def test_list_libraries_empty_primitive_types_keeps_no_filter_path(client: TestClient) -> None:
    mock_list = _stub_list_call(client=client, primitive_types=",  ,")

    assert mock_list.await_args is not None
    call_kwargs = mock_list.await_args.kwargs
    assert call_kwargs["primitive_types"] is None
    assert call_kwargs["primitive_type"] is None


def test_list_libraries_backwards_compatible_single_primitive_type(client: TestClient) -> None:
    mock_list = _stub_list_call(client=client, primitive_type="workflow")

    assert mock_list.await_args is not None
    call_kwargs = mock_list.await_args.kwargs
    assert call_kwargs["primitive_type"] == "workflow"
    assert call_kwargs["primitive_types"] is None


def test_list_libraries_default_excludes_in_dev(client: TestClient) -> None:
    """The list endpoint defaults to the service in_dev exclusion (excluded_tiers=None)."""
    result = PageResult(items=[_make_listable_primitive()], total=1, page=1, page_size=20)
    with (
        patch("modulo.api.routes.library.list_primitives", new_callable=AsyncMock, return_value=result) as mock_list,
        patch("modulo.api.routes.library.set_rls_org", new_callable=AsyncMock),
        patch("modulo.api.routes.library.set_rls_user_context", new_callable=AsyncMock),
    ):
        resp = client.get("/api/v1/libraries")
    assert resp.status_code == 200, resp.text
    assert mock_list.await_args is not None
    assert mock_list.await_args.kwargs["excluded_tiers"] is None


def test_list_libraries_include_in_dev_passes_empty_exclusions(client: TestClient) -> None:
    """?include_in_dev=true reveals In-Dev primitives in the actual response JSON."""
    in_dev = _make_listable_primitive(tier="in_dev")
    result = PageResult(items=[in_dev], total=1, page=1, page_size=20)
    with (
        patch("modulo.api.routes.library.list_primitives", new_callable=AsyncMock, return_value=result) as mock_list,
        patch("modulo.api.routes.library.set_rls_org", new_callable=AsyncMock),
        patch("modulo.api.routes.library.set_rls_user_context", new_callable=AsyncMock),
    ):
        resp = client.get("/api/v1/libraries", params={"include_in_dev": "true"})
    assert resp.status_code == 200, resp.text
    assert mock_list.await_args is not None
    assert not mock_list.await_args.kwargs["excluded_tiers"]
    tiers = [item["tier"] for item in resp.json()["items"]]
    assert "in_dev" in tiers, f"Expected an in_dev primitive in the response, got tiers: {tiers}"


def test_list_libraries_include_in_dev_denied_for_viewer(viewer_client: TestClient) -> None:
    """Viewers can search the library but must NOT be able to reveal In-Dev items."""
    resp = viewer_client.get("/api/v1/libraries", params={"include_in_dev": "true"})
    assert resp.status_code == 403
    assert "library.search.in_dev" in resp.json()["detail"]


def test_list_libraries_include_in_dev_operator_reveals_in_dev(client: TestClient) -> None:
    """An operator+ principal (admin fixture) can list In-Dev library primitives."""
    in_dev = _make_listable_primitive(tier="in_dev")
    result = PageResult(items=[in_dev], total=1, page=1, page_size=20)
    with (
        patch("modulo.api.routes.library.list_primitives", new_callable=AsyncMock, return_value=result),
        patch("modulo.api.routes.library.set_rls_org", new_callable=AsyncMock),
        patch("modulo.api.routes.library.set_rls_user_context", new_callable=AsyncMock),
    ):
        resp = client.get("/api/v1/libraries", params={"include_in_dev": "true"})
    assert resp.status_code == 200, resp.text
    assert resp.json()["items"][0]["tier"] == "in_dev"


def test_list_libraries_include_in_dev_false_keeps_exclusion(client: TestClient) -> None:
    """?include_in_dev=false behaves exactly like omitting the parameter."""
    result = PageResult(items=[_make_listable_primitive()], total=1, page=1, page_size=20)
    with (
        patch("modulo.api.routes.library.list_primitives", new_callable=AsyncMock, return_value=result) as mock_list,
        patch("modulo.api.routes.library.set_rls_org", new_callable=AsyncMock),
        patch("modulo.api.routes.library.set_rls_user_context", new_callable=AsyncMock),
    ):
        resp = client.get("/api/v1/libraries", params={"include_in_dev": "false"})
    assert resp.status_code == 200, resp.text
    assert mock_list.await_args is not None
    assert mock_list.await_args.kwargs["excluded_tiers"] is None


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
    assert call_kwargs["account_id"] == _USER_ID
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
