"""Unit tests for POST /api/v1/runs/{run_id}/nodes/{node_id}/observe."""

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
_RUN_ID = uuid.uuid4()


def _make_settings() -> Settings:
    return Settings(
        database_url="postgresql+asyncpg://localhost/test",
        secret_key=_VALID_32,
        fernet_key=_VALID_32,
        modulo_admin_password="testpass",
    )


def _make_run(*, status: str = "complete") -> MagicMock:
    r = MagicMock()
    r.id = _RUN_ID
    r.status = status
    return r


def _make_observation(
    *,
    observed_by: uuid.UUID | None = _USER_ID,
    observed_at: datetime | None = None,
) -> MagicMock:
    obs = MagicMock()
    obs.human_observed_by = observed_by
    obs.human_observed_at = observed_at or datetime(2026, 6, 29, 12, 0, 0, tzinfo=UTC)
    return obs


def _make_mock_session() -> AsyncMock:
    session = AsyncMock()
    begin_cm = AsyncMock()
    begin_cm.__aenter__ = AsyncMock(return_value=None)
    begin_cm.__aexit__ = AsyncMock(return_value=False)
    session.begin = MagicMock(return_value=begin_cm)
    return session


@pytest.fixture()
def mock_session() -> AsyncMock:
    return _make_mock_session()


@pytest.fixture()
def client(mock_session: AsyncMock) -> Generator[TestClient, None, None]:
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
def operator_client(mock_session: AsyncMock) -> Generator[TestClient, None, None]:
    async def override_session() -> AsyncGenerator[AsyncMock, None]:
        yield mock_session

    app.dependency_overrides[get_settings] = _make_settings
    app.dependency_overrides[get_db_session] = override_session
    app.dependency_overrides[_get_engine] = lambda: MagicMock()
    app.dependency_overrides[get_current_user] = lambda: AuthenticatedPrincipal(
        username="operator",
        organisation_id=_ORG_ID,
        user_id=_USER_ID,
        org_role="operator",
    )

    yield TestClient(app)

    app.dependency_overrides.clear()


@pytest.fixture()
def runner_client(mock_session: AsyncMock) -> Generator[TestClient, None, None]:
    async def override_session() -> AsyncGenerator[AsyncMock, None]:
        yield mock_session

    app.dependency_overrides[get_settings] = _make_settings
    app.dependency_overrides[get_db_session] = override_session
    app.dependency_overrides[_get_engine] = lambda: MagicMock()
    app.dependency_overrides[get_current_user] = lambda: AuthenticatedPrincipal(
        username="runner",
        organisation_id=_ORG_ID,
        user_id=_USER_ID,
        org_role="runner",
    )

    yield TestClient(app)

    app.dependency_overrides.clear()


@pytest.fixture()
def viewer_client(mock_session: AsyncMock) -> Generator[TestClient, None, None]:
    async def override_session() -> AsyncGenerator[AsyncMock, None]:
        yield mock_session

    app.dependency_overrides[get_settings] = _make_settings
    app.dependency_overrides[get_db_session] = override_session
    app.dependency_overrides[_get_engine] = lambda: MagicMock()
    app.dependency_overrides[get_current_user] = lambda: AuthenticatedPrincipal(
        username="viewer",
        organisation_id=_ORG_ID,
        user_id=_USER_ID,
        org_role="viewer",
    )

    yield TestClient(app)

    app.dependency_overrides.clear()


class TestObserveNode:
    def test_admin_observes_node(self, client: TestClient) -> None:
        run = _make_run()
        obs = _make_observation()

        with (
            patch("modulo.api.routes.runs.get_run", return_value=run),
            patch("modulo.api.routes.runs.observe_node", return_value=obs) as mock_observe,
            patch("modulo.api.routes.runs.set_rls_org"),
        ):
            resp = client.post(f"/api/v1/runs/{_RUN_ID}/nodes/planner/observe")

        assert resp.status_code == 200
        body = resp.json()
        assert body["run_id"] == str(_RUN_ID)
        assert body["node_id"] == "planner"
        assert body["human_observed_at"] == "2026-06-29T12:00:00+00:00"
        assert body["human_observed_by"] == str(_USER_ID)
        mock_observe.assert_awaited_once()

    def test_operator_observes_node(self, operator_client: TestClient) -> None:
        run = _make_run()
        obs = _make_observation()

        with (
            patch("modulo.api.routes.runs.get_run", return_value=run),
            patch("modulo.api.routes.runs.observe_node", return_value=obs),
            patch("modulo.api.routes.runs.set_rls_org"),
        ):
            resp = operator_client.post(f"/api/v1/runs/{_RUN_ID}/nodes/planner/observe")

        assert resp.status_code == 200
        assert resp.json()["human_observed_by"] == str(_USER_ID)

    def test_runner_gets_403(self, runner_client: TestClient) -> None:
        with (
            patch("modulo.api.routes.runs.get_run"),
            patch("modulo.api.routes.runs.set_rls_org"),
        ):
            resp = runner_client.post(f"/api/v1/runs/{_RUN_ID}/nodes/planner/observe")

        assert resp.status_code == 403

    def test_viewer_gets_403(self, viewer_client: TestClient) -> None:
        with (
            patch("modulo.api.routes.runs.get_run"),
            patch("modulo.api.routes.runs.set_rls_org"),
        ):
            resp = viewer_client.post(f"/api/v1/runs/{_RUN_ID}/nodes/planner/observe")

        assert resp.status_code == 403

    def test_run_not_found_returns_404(self, client: TestClient) -> None:
        with (
            patch("modulo.api.routes.runs.get_run", return_value=None),
            patch("modulo.api.routes.runs.set_rls_org"),
        ):
            resp = client.post(f"/api/v1/runs/{uuid.uuid4()}/nodes/planner/observe")

        assert resp.status_code == 404
        assert resp.json()["detail"] == "Run not found"

    def test_observe_is_idempotent(self, client: TestClient) -> None:
        run = _make_run()
        obs = _make_observation()

        with (
            patch("modulo.api.routes.runs.get_run", return_value=run),
            patch("modulo.api.routes.runs.observe_node", return_value=obs) as mock_observe,
            patch("modulo.api.routes.runs.set_rls_org"),
        ):
            resp1 = client.post(f"/api/v1/runs/{_RUN_ID}/nodes/planner/observe")
            resp2 = client.post(f"/api/v1/runs/{_RUN_ID}/nodes/planner/observe")

        assert resp1.status_code == 200
        assert resp2.status_code == 200
        assert mock_observe.await_count == 2
        assert resp1.json() == resp2.json()
