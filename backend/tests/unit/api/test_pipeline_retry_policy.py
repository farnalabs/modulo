"""Unit tests for the pipeline ``retry_policy`` API schema validation.

Covers create persistence, create/update validation rejection, and clearing.
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
_PIPELINE_ID = uuid.uuid4()
_NOW = datetime(2025, 1, 1, tzinfo=UTC)


def _make_settings() -> Settings:
    return Settings(
        database_url="postgresql+asyncpg://localhost/test",
        secret_key=_VALID_32,
        fernet_key=_VALID_32,
        modulo_admin_password="testpass",
    )


def _make_pipeline() -> MagicMock:
    p = MagicMock()
    p.rate_limit_config = None
    p.retry_policy = {}
    p.max_duration_seconds = None
    p.archived_at = None
    p.snapshot_count = 0
    p.id = _PIPELINE_ID
    p.organisation_id = _ORG_ID
    p.name = "Test Pipeline"
    p.description = None
    p.visibility = "org"
    p.owner_team_id = None
    p.folder_id = None
    p.max_concurrent_runs = 5
    p.lock_wait_timeout_seconds = 300
    p.node_timeout_seconds = 300
    p.run_context_defaults = {}
    p.default_autonomy_level = "manual_approval"
    p.stale_run_timeout_minutes = 30
    p.created_by = uuid.uuid4()
    p.account_id = p.created_by
    p.created_at = _NOW
    p.updated_at = _NOW
    return p


def _make_mock_session() -> AsyncMock:
    session = configure_mock_session(AsyncMock())
    begin_cm = AsyncMock()
    begin_cm.__aenter__ = AsyncMock(return_value=None)
    begin_cm.__aexit__ = AsyncMock(return_value=False)
    session.begin = MagicMock(return_value=begin_cm)
    session.begin_nested = MagicMock(return_value=begin_cm)
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


# ---------------------------------------------------------------------------
# POST /api/v1/pipelines — retry_policy create + validation
# ---------------------------------------------------------------------------


def test_create_pipeline_persists_retry_policy(client: TestClient) -> None:
    pipeline = _make_pipeline()
    pipeline.retry_policy = {"on": ["stall"], "max_retries": 2}

    with (
        patch("modulo.api.routes.pipelines.create_pipeline", return_value=pipeline),
        patch("modulo.api.routes.pipelines.set_rls_org"),
        patch("modulo.api.routes.pipelines.set_rls_user_context"),
    ):
        resp = client.post(
            "/api/v1/pipelines",
            json={"name": "Pipeline", "retry_policy": {"on": ["stall"], "max_retries": 2}},
        )

    assert resp.status_code == 201
    assert resp.json()["retry_policy"] == {"on": ["stall"], "max_retries": 2}


def test_create_pipeline_rejects_unknown_retry_event(client: TestClient) -> None:
    resp = client.post(
        "/api/v1/pipelines",
        json={"name": "Pipeline", "retry_policy": {"on": ["bogus"], "max_retries": 2}},
    )

    assert resp.status_code == 422


def test_create_pipeline_rejects_max_retries_over_budget(client: TestClient) -> None:
    resp = client.post(
        "/api/v1/pipelines",
        json={"name": "Pipeline", "retry_policy": {"on": ["stall"], "max_retries": 9}},
    )

    assert resp.status_code == 422


def test_create_pipeline_rejects_non_integer_max_retries(client: TestClient) -> None:
    resp = client.post(
        "/api/v1/pipelines",
        json={"name": "Pipeline", "retry_policy": {"on": ["stall"], "max_retries": "lots"}},
    )

    assert resp.status_code == 422


def test_create_pipeline_accepts_empty_retry_policy(client: TestClient) -> None:
    pipeline = _make_pipeline()
    pipeline.retry_policy = {}

    with (
        patch("modulo.api.routes.pipelines.create_pipeline", return_value=pipeline),
        patch("modulo.api.routes.pipelines.set_rls_org"),
        patch("modulo.api.routes.pipelines.set_rls_user_context"),
    ):
        resp = client.post("/api/v1/pipelines", json={"name": "Pipeline", "retry_policy": {}})

    assert resp.status_code == 201
    assert not resp.json()["retry_policy"]


def test_create_pipeline_accepts_all_valid_events(client: TestClient) -> None:
    pipeline = _make_pipeline()
    pipeline.retry_policy = {"on": ["stall", "timeout", "failure", "eval_failed"], "max_retries": 5}

    with (
        patch("modulo.api.routes.pipelines.create_pipeline", return_value=pipeline),
        patch("modulo.api.routes.pipelines.set_rls_org"),
        patch("modulo.api.routes.pipelines.set_rls_user_context"),
    ):
        resp = client.post(
            "/api/v1/pipelines",
            json={
                "name": "Pipeline",
                "retry_policy": {"on": ["stall", "timeout", "failure", "eval_failed"], "max_retries": 5},
            },
        )

    assert resp.status_code == 201


# ---------------------------------------------------------------------------
# PATCH /api/v1/pipelines/{id} — retry_policy update + clear
# ---------------------------------------------------------------------------


def test_update_pipeline_sets_retry_policy(client: TestClient) -> None:
    pipeline = _make_pipeline()
    pipeline.retry_policy = {"on": ["timeout"], "max_retries": 1}

    with (
        patch("modulo.api.routes.pipelines.get_pipeline", return_value=pipeline),
        patch("modulo.api.routes.pipelines.update_pipeline", return_value=pipeline),
        patch("modulo.api.routes.pipelines.set_rls_org"),
        patch("modulo.api.routes.pipelines.set_rls_user_context"),
        patch("modulo.api.routes.pipelines._assert_team_transition_allowed", new=AsyncMock()),
        patch("modulo.api.routes.pipelines.append_audit_event", new=AsyncMock()),
    ):
        resp = client.patch(
            f"/api/v1/pipelines/{_PIPELINE_ID}",
            json={"retry_policy": {"on": ["timeout"], "max_retries": 1}},
        )

    assert resp.status_code == 200
    assert resp.json()["retry_policy"] == {"on": ["timeout"], "max_retries": 1}


def test_update_pipeline_clears_retry_policy_with_empty_dict(client: TestClient) -> None:
    pipeline = _make_pipeline()
    pipeline.retry_policy = {}

    with (
        patch("modulo.api.routes.pipelines.get_pipeline", return_value=pipeline),
        patch("modulo.api.routes.pipelines.update_pipeline", return_value=pipeline),
        patch("modulo.api.routes.pipelines.set_rls_org"),
        patch("modulo.api.routes.pipelines.set_rls_user_context"),
        patch("modulo.api.routes.pipelines._assert_team_transition_allowed", new=AsyncMock()),
        patch("modulo.api.routes.pipelines.append_audit_event", new=AsyncMock()),
    ):
        resp = client.patch(f"/api/v1/pipelines/{_PIPELINE_ID}", json={"retry_policy": {}})

    assert resp.status_code == 200
    assert not resp.json()["retry_policy"]


def test_update_pipeline_accepts_eval_failed_retry_event(client: TestClient) -> None:
    """FAR-503: PATCH with the new "eval_failed" event is accepted."""
    pipeline = _make_pipeline()
    pipeline.retry_policy = {"on": ["eval_failed"], "max_retries": 1}

    with (
        patch("modulo.api.routes.pipelines.get_pipeline", return_value=pipeline),
        patch("modulo.api.routes.pipelines.update_pipeline", return_value=pipeline),
        patch("modulo.api.routes.pipelines.set_rls_org"),
        patch("modulo.api.routes.pipelines.set_rls_user_context"),
        patch("modulo.api.routes.pipelines._assert_team_transition_allowed", new=AsyncMock()),
        patch("modulo.api.routes.pipelines.append_audit_event", new=AsyncMock()),
    ):
        resp = client.patch(
            f"/api/v1/pipelines/{_PIPELINE_ID}",
            json={"retry_policy": {"on": ["eval_failed"], "max_retries": 1}},
        )

    assert resp.status_code == 200
    assert resp.json()["retry_policy"] == {"on": ["eval_failed"], "max_retries": 1}


def test_update_pipeline_rejects_unknown_retry_event(client: TestClient) -> None:
    resp = client.patch(
        f"/api/v1/pipelines/{_PIPELINE_ID}",
        json={"retry_policy": {"on": ["nope"], "max_retries": 1}},
    )

    assert resp.status_code == 422


def test_update_pipeline_rejects_max_retries_over_budget(client: TestClient) -> None:
    resp = client.patch(
        f"/api/v1/pipelines/{_PIPELINE_ID}",
        json={"retry_policy": {"on": ["failure"], "max_retries": 6}},
    )

    assert resp.status_code == 422
