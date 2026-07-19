"""Unit tests for pipeline copy (clone) error handling.

Covers: 404 (source not found), 422 (duplicate name), 403 (insufficient role)."""

import uuid
from collections.abc import AsyncGenerator, Generator
from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from modulo.api.dependencies import _get_engine, get_db_session, get_plan_context
from modulo.api.main import app
from modulo.auth.dependencies import get_current_tenant_user, get_current_user
from modulo.auth.jwt import AuthenticatedPrincipal, TenantPrincipal
from modulo.settings import Settings, get_settings

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
    p.id = _PIPELINE_ID
    p.organisation_id = _ORG_ID
    p.name = "Test Pipeline"
    p.description = None
    p.visibility = "org"
    p.owner_team_id = None
    p.max_concurrent_runs = 5
    p.lock_wait_timeout_seconds = 300
    p.node_timeout_seconds = 300
    p.run_context_defaults = {}
    p.default_autonomy_level = "manual_approval"
    p.created_by = uuid.uuid4()
    p.created_at = _NOW
    p.updated_at = _NOW
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


@pytest.fixture()
def viewer_client() -> Generator[TestClient, None, None]:
    """Client with viewer role — cannot clone pipelines."""
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


# ---------------------------------------------------------------------------
# POST /api/v1/pipelines/{id}/clone — non-existent source
# ---------------------------------------------------------------------------

FAKE_PIPELINE_ID = uuid.uuid4()


def test_clone_pipeline_not_found_returns_404_with_detail(client: TestClient) -> None:
    """Non-existent source pipeline should return 404 with a descriptive detail."""
    with (
        patch("modulo.api.routes.pipelines.get_pipeline", return_value=None),
        patch("modulo.api.routes.pipelines.check_pipeline_name_available"),
        patch("modulo.api.routes.pipelines.set_rls_org"),
        patch("modulo.api.routes.pipelines.set_rls_user_context"),
    ):
        resp = client.post(f"/api/v1/pipelines/{FAKE_PIPELINE_ID}/clone", json={})

    assert resp.status_code == 404
    body = resp.json()
    msg = body.get("detail", body.get("error", {}).get("message", ""))
    assert "pipeline_copy_failed" in msg
    assert "not found" in msg.lower()


# ---------------------------------------------------------------------------
# POST /api/v1/pipelines/{id}/clone — duplicate name
# ---------------------------------------------------------------------------


def test_clone_pipeline_duplicate_name_returns_422(client: TestClient) -> None:
    """Attempting to copy with a name that already exists should return 422."""
    source = _make_pipeline()

    with (
        patch("modulo.api.routes.pipelines.get_pipeline", return_value=source),
        patch(
            "modulo.api.routes.pipelines.check_pipeline_name_available",
            return_value=False,
        ),
        patch("modulo.api.routes.pipelines.clone_pipeline"),
        patch("modulo.api.routes.pipelines.set_rls_org"),
        patch("modulo.api.routes.pipelines.set_rls_user_context"),
    ):
        resp = client.post(
            f"/api/v1/pipelines/{_PIPELINE_ID}/clone",
            json={"name": "Existing Pipeline Name"},
        )

    assert resp.status_code == 422
    body = resp.json()
    msg = body.get("detail", body.get("error", {}).get("message", ""))
    assert "already exists" in msg


def test_clone_pipeline_default_name_taken_returns_422(client: TestClient) -> None:
    """When no name override is given and the default 'Copy of...' name is taken."""
    source = _make_pipeline()
    source.name = "My Pipeline"

    with (
        patch("modulo.api.routes.pipelines.get_pipeline", return_value=source),
        patch(
            "modulo.api.routes.pipelines.check_pipeline_name_available",
            return_value=False,
        ),
        patch("modulo.api.routes.pipelines.clone_pipeline"),
        patch("modulo.api.routes.pipelines.set_rls_org"),
        patch("modulo.api.routes.pipelines.set_rls_user_context"),
    ):
        resp = client.post(f"/api/v1/pipelines/{_PIPELINE_ID}/clone", json={})

    assert resp.status_code == 422
    body = resp.json()
    msg = body.get("detail", body.get("error", {}).get("message", ""))
    assert "Copy of My Pipeline" in msg
    assert "already exists" in msg


# ---------------------------------------------------------------------------
# POST /api/v1/pipelines/{id}/clone — insufficient permissions
# ---------------------------------------------------------------------------


def test_clone_pipeline_viewer_denied_returns_403(viewer_client: TestClient) -> None:
    """Viewer role should be denied with 403."""
    resp = viewer_client.post(f"/api/v1/pipelines/{_PIPELINE_ID}/clone", json={})

    assert resp.status_code == 403
    body = resp.json()
    msg = body.get("detail", body.get("error", {}).get("message", ""))
    assert "clone" in msg.lower() or "member" in msg.lower()


# ---------------------------------------------------------------------------
# POST /api/v1/pipelines/{id}/clone — unexpected error
# ---------------------------------------------------------------------------


def test_clone_pipeline_internal_error_returns_500(client: TestClient) -> None:
    """Unexpected exceptions during clone should return 500."""
    source = _make_pipeline()

    with (
        patch("modulo.api.routes.pipelines.get_pipeline", return_value=source),
        patch(
            "modulo.api.routes.pipelines.check_pipeline_name_available",
            return_value=True,
        ),
        patch(
            "modulo.api.routes.pipelines.clone_pipeline",
            side_effect=RuntimeError("DB connection lost"),
        ),
        patch("modulo.api.routes.pipelines.set_rls_org"),
        patch("modulo.api.routes.pipelines.set_rls_user_context"),
    ):
        resp = client.post(f"/api/v1/pipelines/{_PIPELINE_ID}/clone", json={})

    assert resp.status_code == 500
    body = resp.json()
    msg = body.get("detail", body.get("error", {}).get("message", ""))
    assert "unexpected error" in msg.lower()


# ---------------------------------------------------------------------------
# POST /api/v1/pipelines/{id}/clone — source disappears mid-copy
# ---------------------------------------------------------------------------


def test_clone_pipeline_disappears_returns_404(client: TestClient) -> None:
    """If source is found at check-time but gone at copy-time, return 404."""
    source = _make_pipeline()

    with (
        patch("modulo.api.routes.pipelines.get_pipeline", return_value=source),
        patch(
            "modulo.api.routes.pipelines.check_pipeline_name_available",
            return_value=True,
        ),
        patch(
            "modulo.api.routes.pipelines.clone_pipeline",
            return_value=None,
        ),
        patch("modulo.api.routes.pipelines.set_rls_org"),
        patch("modulo.api.routes.pipelines.set_rls_user_context"),
    ):
        resp = client.post(f"/api/v1/pipelines/{_PIPELINE_ID}/clone", json={})

    assert resp.status_code == 404
    body = resp.json()
    msg = body.get("detail", body.get("error", {}).get("message", ""))
    assert "disappeared" in msg.lower()
