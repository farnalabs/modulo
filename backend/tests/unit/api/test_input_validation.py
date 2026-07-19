"""Application-specific handler tests for route success paths.

Kept tests verify handler integration (create_pipeline, create_schema work),
valid eval types pass schema validation, valid import bundle shapes pass,
and custom duplicate-node-ID validation rejects duplicates.
"""

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
from tests.unit.api.mock_session import configure_mock_session

_VALID_32 = "a" * 32
_ORG_ID = uuid.UUID("00000000-0000-0000-0000-000000000001")
_USER_ID = uuid.UUID("00000000-0000-0000-0000-000000000002")

# ---------------------------------------------------------------------------
# Shared fixtures
# ---------------------------------------------------------------------------


def _make_settings() -> Settings:
    return Settings(
        database_url="postgresql+asyncpg://localhost/test",
        secret_key=_VALID_32,
        fernet_key=_VALID_32,
        modulo_admin_password="testpass",
        modulo_license_key="test-license-key",
    )


def _make_mock_session() -> AsyncMock:
    session = configure_mock_session(AsyncMock())
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


# ---------------------------------------------------------------------------
# Pipeline create validation
# ---------------------------------------------------------------------------


def test_pipeline_create_valid_minimal(client: TestClient) -> None:
    pipeline = MagicMock()
    pipeline.id = uuid.uuid4()
    pipeline.organisation_id = _ORG_ID
    pipeline.name = "Test"
    pipeline.description = None
    pipeline.visibility = "org"
    pipeline.owner_team_id = None
    pipeline.folder_id = None
    pipeline.max_concurrent_runs = 5
    pipeline.lock_wait_timeout_seconds = 300
    pipeline.node_timeout_seconds = 300
    pipeline.run_context_defaults = {}
    pipeline.default_autonomy_level = "manual_approval"
    pipeline.snapshot_count = 0
    pipeline.archived_at = None
    pipeline.created_by = uuid.uuid4()
    pipeline.account_id = uuid.uuid4()
    pipeline.created_at = datetime(2025, 1, 1, tzinfo=UTC)
    pipeline.updated_at = datetime(2025, 1, 1, tzinfo=UTC)

    with (
        patch("modulo.api.routes.pipelines.create_pipeline", return_value=pipeline),
        patch("modulo.api.routes.pipelines.set_rls_org"),
    ):
        resp = client.post("/api/v1/pipelines", json={"name": "Test"})
    assert resp.status_code == 201


# ---------------------------------------------------------------------------
# Schema create validation
# ---------------------------------------------------------------------------


def test_schema_create_valid(client: TestClient) -> None:
    mock_schema = MagicMock()
    mock_schema.id = uuid.uuid4()
    mock_schema.organisation_id = _ORG_ID
    mock_schema.name = "Test"
    mock_schema.description = None
    mock_schema.abstract_name = None
    mock_schema.account_id = uuid.uuid4()
    mock_schema.created_at = datetime(2025, 1, 1, tzinfo=UTC)
    mock_schema.updated_at = datetime(2025, 1, 1, tzinfo=UTC)

    with (
        patch("modulo.api.routes.schemas.create_schema", return_value=mock_schema),
        patch("modulo.api.routes.schemas.set_rls_org"),
    ):
        resp = client.post("/api/v1/schemas", json={"name": "Test"})
    assert resp.status_code == 201


# ---------------------------------------------------------------------------
# Eval definition validation
# ---------------------------------------------------------------------------


def test_eval_create_valid_eval_types(client: TestClient) -> None:
    for eval_type in ("llm_judge", "regex", "json_schema", "custom_function"):
        with patch("modulo.api.routes.evals.set_rls_org"):
            resp = client.post(
                "/api/v1/evals",
                json={
                    "pipeline_id": str(uuid.uuid4()),
                    "name": "Test Eval",
                    "eval_type": eval_type,
                },
            )
        # Should pass Pydantic validation (not 422). May fail handler
        # with 403 (admin check) but that's fine -- test is about validation.
        assert resp.status_code != 422, f"eval_type={eval_type} got 422: {resp.json()}"


# ---------------------------------------------------------------------------
# Library import analyse — was using raw dict body
# ---------------------------------------------------------------------------


def test_analyse_import_bundle_valid_shape(client: TestClient) -> None:
    """Valid body shape should not get 422 (may fail at handler level)."""
    bundle = {"pipeline": {"name": "Test"}}
    with patch("modulo.api.routes.library.set_rls_org"):
        resp = client.post("/api/v1/libraries/import/analyse", json={"bundle": bundle})
    assert resp.status_code != 422


# ---------------------------------------------------------------------------
# Pipeline graph — nodes & edges bounds
# ---------------------------------------------------------------------------


def test_pipeline_graph_duplicate_node_ids(client: TestClient) -> None:
    dup_id = str(uuid.uuid4())
    nodes = [
        {"id": dup_id, "node_type": "agent", "agent_id": str(uuid.uuid4()), "position": {"x": 0, "y": 0}},
        {"id": dup_id, "node_type": "agent", "agent_id": str(uuid.uuid4()), "position": {"x": 100, "y": 0}},
    ]
    resp = client.patch(
        f"/api/v1/pipelines/{uuid.uuid4()}/graph",
        json={
            "nodes": nodes,
            "edges": [],
        },
    )
    assert resp.status_code == 422
