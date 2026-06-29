"""Unit tests for /api/v1/me and /api/v1/viewmodel/current."""

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


def _make_pipeline() -> MagicMock:
    p = MagicMock()
    p.id = uuid.uuid4()
    p.name = "Test Pipeline"
    p.visibility = "org"
    p.created_at = _NOW
    return p


def _make_run() -> MagicMock:
    r = MagicMock()
    r.id = uuid.uuid4()
    r.pipeline_id = uuid.uuid4()
    r.status = "complete"
    r.trigger_type = "manual"
    r.created_at = _NOW
    return r


def _make_mock_session() -> AsyncMock:
    session = AsyncMock()
    begin_cm = AsyncMock()
    begin_cm.__aenter__ = AsyncMock(return_value=None)
    begin_cm.__aexit__ = AsyncMock(return_value=False)
    session.begin = MagicMock(return_value=begin_cm)

    execute_result = MagicMock()
    scalars_mock = AsyncMock()
    scalars_mock.all = AsyncMock(return_value=[])
    execute_result.scalars.return_value = scalars_mock
    session.execute = AsyncMock(return_value=execute_result)
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


# ---------------------------------------------------------------------------
# GET /api/v1/me
# ---------------------------------------------------------------------------


def test_me_returns_200_with_username(client: TestClient) -> None:
    resp = client.get("/api/v1/me")
    assert resp.status_code == 200
    body = resp.json()
    assert body["user"]["username"] == "testuser"
    assert body["org"]["org_id"] == str(_ORG_ID)
    assert body["org_role"] == "admin"
    assert body["team_memberships"] == []
    assert body["team_memberships_truncated"] is False


def test_me_unauthenticated_returns_4xx(unauth_client: TestClient) -> None:
    resp = unauth_client.get("/api/v1/me")
    assert resp.status_code in (401, 403)


# ---------------------------------------------------------------------------
# GET /api/v1/viewmodel/current
# ---------------------------------------------------------------------------


def test_viewmodel_current_returns_200(client: TestClient) -> None:
    pipeline = _make_pipeline()
    run = _make_run()
    pipelines_page = MagicMock(items=[pipeline], total=1, page=1, page_size=20)
    runs_page = MagicMock(items=[run], total=1, page=1, page_size=10)

    with (
        patch("modulo.api.routes.viewmodel.list_pipelines", return_value=pipelines_page),
        patch("modulo.api.routes.viewmodel.list_runs", return_value=runs_page),
        patch("modulo.api.routes.viewmodel.set_rls_org"),
    ):
        resp = client.get("/api/v1/viewmodel/current")

    assert resp.status_code == 200
    body = resp.json()
    assert body["user"]["username"] == "testuser"
    assert body["pipelines_total"] == 1
    assert body["runs_total"] == 1
    assert body["pending_hitl_gates"] == []
    assert len(body["pipelines"]) == 1
    assert len(body["recent_runs"]) == 1


def test_viewmodel_current_unauthenticated_returns_4xx(unauth_client: TestClient) -> None:
    resp = unauth_client.get("/api/v1/viewmodel/current")
    assert resp.status_code in (401, 403)


def test_viewmodel_current_includes_pending_hitl(client: TestClient) -> None:
    pipeline = _make_pipeline()
    run = _make_run()
    pipelines_page = MagicMock(items=[pipeline], total=1, page=1, page_size=20)
    runs_page = MagicMock(items=[run], total=1, page=1, page_size=10)

    hitl = MagicMock()
    hitl.id = uuid.uuid4()
    hitl.run_id = uuid.uuid4()
    hitl.pipeline_id = uuid.uuid4()
    hitl.gate_id = "approval_gate"
    hitl.claimed_by = None
    hitl.expires_at = None

    # The viewmodel does its own session.execute for HITL — override what scalars() returns
    execute_result = MagicMock()
    scalars_mock = AsyncMock()
    scalars_mock.all = AsyncMock(return_value=[hitl])
    execute_result.scalars.return_value = scalars_mock

    with (
        patch("modulo.api.routes.viewmodel.list_pipelines", return_value=pipelines_page),
        patch("modulo.api.routes.viewmodel.list_runs", return_value=runs_page),
        patch("modulo.api.routes.viewmodel.set_rls_org"),
        patch(
            "modulo.api.routes.viewmodel.AsyncSession.execute",
            new_callable=AsyncMock,
            return_value=execute_result,
        ),
    ):
        resp = client.get("/api/v1/viewmodel/current")

    assert resp.status_code == 200
