"""Unit tests: pipeline execution routes return 501 on ProgrammingError.

Tests that route handlers in runs.py and pipelines.py gracefully return
501 Not Implemented when the database raises ProgrammingError
(e.g. missing table because migrations haven't run yet).
"""

import uuid
from collections.abc import AsyncGenerator
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.exc import ProgrammingError

from modulo.api.dependencies import _get_engine, get_db_session
from modulo.api.main import app
from modulo.auth.dependencies import get_current_user
from modulo.auth.jwt import AuthenticatedPrincipal
from modulo.settings import Settings, get_settings

_VALID_32 = "a" * 32
_ORG_ID = uuid.UUID("00000000-0000-0000-0000-000000000001")
_USER_ID = uuid.UUID("00000000-0000-0000-0000-000000000002")
_PIPELINE_ID = uuid.UUID("00000000-0000-0000-0000-000000000010")
_RUN_ID = uuid.UUID("00000000-0000-0000-0000-000000000020")
_SNAPSHOT_ID = uuid.UUID("00000000-0000-0000-0000-000000000030")
_NODE_ID = uuid.UUID("00000000-0000-0000-0000-000000000040")


def _make_settings() -> Settings:
    return Settings(
        database_url="postgresql+asyncpg://localhost/test",
        secret_key=_VALID_32,
        fernet_key=_VALID_32,
        modulo_admin_password="testpass",
        modulo_csrf_enabled=False,
    )


def _make_session_raising_programming_error() -> AsyncMock:
    session = AsyncMock()
    begin_cm = AsyncMock()
    begin_cm.__aenter__ = AsyncMock(side_effect=ProgrammingError("relation does not exist", None, None))
    begin_cm.__aexit__ = AsyncMock(return_value=False)
    session.begin = MagicMock(return_value=begin_cm)
    bind_mock = MagicMock()
    bind_mock.dialect.name = "postgresql"
    session.get_bind = AsyncMock(return_value=bind_mock)
    return session


@pytest.fixture()
def admin_client() -> TestClient:
    app.dependency_overrides[get_settings] = _make_settings
    app.dependency_overrides[_get_engine] = lambda: MagicMock()
    app.dependency_overrides[get_current_user] = lambda: AuthenticatedPrincipal(
        username="admin",
        organisation_id=_ORG_ID,
        account_id=_USER_ID,
        org_role="admin",
    )
    client = TestClient(app)
    yield client
    app.dependency_overrides.clear()


def _override_session(session) -> None:
    async def _get_session() -> AsyncGenerator[AsyncMock, None]:
        yield session

    app.dependency_overrides[get_db_session] = _get_session


# ---------------------------------------------------------------------------
# Pipeline CRUD — ProgrammingError→501
# ---------------------------------------------------------------------------


class TestListPipelinesProgrammingError:
    def test_list_returns_501_on_programming_error(self, admin_client: TestClient) -> None:
        session = _make_session_raising_programming_error()
        _override_session(session)
        resp = admin_client.get("/api/v1/pipelines")
        assert resp.status_code == 501
        assert "migrations" in resp.json()["detail"].lower()


class TestCreatePipelineProgrammingError:
    def test_create_returns_501_on_programming_error(self, admin_client: TestClient) -> None:
        session = _make_session_raising_programming_error()
        _override_session(session)
        resp = admin_client.post("/api/v1/pipelines", json={"name": "Test"})
        assert resp.status_code == 501
        assert "migrations" in resp.json()["detail"].lower()


class TestGetPipelineProgrammingError:
    def test_get_returns_501_on_programming_error(self, admin_client: TestClient) -> None:
        session = _make_session_raising_programming_error()
        _override_session(session)
        resp = admin_client.get(f"/api/v1/pipelines/{_PIPELINE_ID}")
        assert resp.status_code == 501
        assert "migrations" in resp.json()["detail"].lower()


class TestGetPipelineGraphProgrammingError:
    def test_get_graph_returns_501_on_programming_error(self, admin_client: TestClient) -> None:
        session = _make_session_raising_programming_error()
        _override_session(session)
        resp = admin_client.get(f"/api/v1/pipelines/{_PIPELINE_ID}/graph")
        assert resp.status_code == 501
        assert "migrations" in resp.json()["detail"].lower()


class TestUpdatePipelineProgrammingError:
    def test_update_returns_501_on_programming_error(self, admin_client: TestClient) -> None:
        session = _make_session_raising_programming_error()
        _override_session(session)
        resp = admin_client.patch(f"/api/v1/pipelines/{_PIPELINE_ID}", json={"name": "Updated"})
        assert resp.status_code == 501
        assert "migrations" in resp.json()["detail"].lower()


class TestDeletePipelineProgrammingError:
    def test_delete_returns_501_on_programming_error(self, admin_client: TestClient) -> None:
        session = _make_session_raising_programming_error()
        _override_session(session)
        resp = admin_client.delete(f"/api/v1/pipelines/{_PIPELINE_ID}")
        assert resp.status_code == 501
        assert "migrations" in resp.json()["detail"].lower()


# ---------------------------------------------------------------------------
# Run CRUD — ProgrammingError→501
# ---------------------------------------------------------------------------


class TestTriggerRunProgrammingError:
    def test_trigger_run_returns_501_on_programming_error(self, admin_client: TestClient) -> None:
        session = _make_session_raising_programming_error()
        _override_session(session)
        resp = admin_client.post(
            "/api/v1/runs",
            json={"pipeline_id": str(_PIPELINE_ID), "input_payload": {}},
        )
        assert resp.status_code == 501
        assert "migrations" in resp.json()["detail"].lower()


class TestGetRunStatusProgrammingError:
    def test_get_run_returns_501_on_programming_error(self, admin_client: TestClient) -> None:
        session = _make_session_raising_programming_error()
        _override_session(session)
        resp = admin_client.get(f"/api/v1/runs/{_RUN_ID}")
        assert resp.status_code == 501
        assert "migrations" in resp.json()["detail"].lower()


class TestCancelRunProgrammingError:
    def test_cancel_run_returns_501_on_programming_error(self, admin_client: TestClient) -> None:
        session = _make_session_raising_programming_error()
        _override_session(session)
        resp = admin_client.post(f"/api/v1/runs/{_RUN_ID}/cancel")
        assert resp.status_code == 501
        assert "migrations" in resp.json()["detail"].lower()


class TestGetRunIoProgrammingError:
    def test_get_run_io_returns_501_on_programming_error(self, admin_client: TestClient) -> None:
        session = _make_session_raising_programming_error()
        _override_session(session)
        resp = admin_client.get(f"/api/v1/runs/{_RUN_ID}/io")
        assert resp.status_code == 501
        assert "migrations" in resp.json()["detail"].lower()


class TestExportRunFixtureProgrammingError:
    def test_export_fixture_returns_501_on_programming_error(self, admin_client: TestClient) -> None:
        session = _make_session_raising_programming_error()
        _override_session(session)
        resp = admin_client.get(f"/api/v1/runs/{_RUN_ID}/export-fixture")
        assert resp.status_code == 501
        assert "migrations" in resp.json()["detail"].lower()


class TestGetRunWorkspaceLeaseProgrammingError:
    def test_workspace_lease_returns_501_on_programming_error(self, admin_client: TestClient) -> None:
        session = _make_session_raising_programming_error()
        _override_session(session)
        resp = admin_client.get(f"/api/v1/runs/{_RUN_ID}/workspace-lease")
        assert resp.status_code == 501
        assert "migrations" in resp.json()["detail"].lower()


class TestGetRunWorkspaceEventsProgrammingError:
    def test_workspace_events_returns_501_on_programming_error(self, admin_client: TestClient) -> None:
        session = _make_session_raising_programming_error()
        _override_session(session)
        resp = admin_client.get(f"/api/v1/runs/{_RUN_ID}/workspace-events")
        assert resp.status_code == 501
        assert "migrations" in resp.json()["detail"].lower()


class TestGetRunNodeOutputProgrammingError:
    def test_node_output_returns_501_on_programming_error(self, admin_client: TestClient) -> None:
        session = _make_session_raising_programming_error()
        _override_session(session)
        resp = admin_client.get(f"/api/v1/runs/{_RUN_ID}/nodes/{_NODE_ID}/output")
        assert resp.status_code == 501
        assert "migrations" in resp.json()["detail"].lower()


class TestObserveRunNodeProgrammingError:
    def test_observe_node_returns_501_on_programming_error(self, admin_client: TestClient) -> None:
        session = _make_session_raising_programming_error()
        _override_session(session)
        resp = admin_client.post(f"/api/v1/runs/{_RUN_ID}/nodes/{_NODE_ID}/observe")
        assert resp.status_code == 501
        assert "migrations" in resp.json()["detail"].lower()


class TestRecoverRunNodeProgrammingError:
    def test_recover_node_returns_501_on_programming_error(self, admin_client: TestClient) -> None:
        session = _make_session_raising_programming_error()
        _override_session(session)
        resp = admin_client.post(f"/api/v1/runs/{_RUN_ID}/nodes/{_NODE_ID}/recover", json={})
        assert resp.status_code == 501
        assert "migrations" in resp.json()["detail"].lower()


class TestDiffRunNodeOutputProgrammingError:
    def test_diff_node_output_returns_501_on_programming_error(self, admin_client: TestClient) -> None:
        session = _make_session_raising_programming_error()
        _override_session(session)
        resp = admin_client.post(
            "/api/v1/runs/diff",
            json={
                "run_id_a": str(_RUN_ID),
                "node_id_a": str(_NODE_ID),
                "run_id_b": str(_RUN_ID),
                "node_id_b": str(_NODE_ID),
            },
        )
        assert resp.status_code == 501
        assert "migrations" in resp.json()["detail"].lower()


class TestRunStatsProgrammingError:
    def test_stats_returns_501_on_programming_error(self, admin_client: TestClient) -> None:
        session = _make_session_raising_programming_error()
        _override_session(session)
        resp = admin_client.get("/api/v1/runs/stats?period=30d")
        assert resp.status_code == 501
        assert "migrations" in resp.json()["detail"].lower()


class TestRunHeatmapProgrammingError:
    def test_heatmap_returns_501_on_programming_error(self, admin_client: TestClient) -> None:
        session = _make_session_raising_programming_error()
        _override_session(session)
        resp = admin_client.get("/api/v1/runs/stats/heatmap?year=2026")
        assert resp.status_code == 501
        assert "migrations" in resp.json()["detail"].lower()
