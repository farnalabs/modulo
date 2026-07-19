"""Unit tests for GET /api/v1/runs/{run_id}/export-fixture."""

import uuid
from collections.abc import AsyncGenerator, Generator
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import AsyncSession

from modulo.api.dependencies import _get_engine, get_db_session, get_plan_context
from modulo.api.main import app
from modulo.api.routes.runs import _build_fixture_map
from modulo.auth.dependencies import get_current_tenant_user, get_current_user
from modulo.auth.jwt import AuthenticatedPrincipal, TenantPrincipal
from modulo.settings import Settings, get_settings

_VALID_32 = "a" * 32
_ORG_ID = uuid.UUID("00000000-0000-0000-0000-000000000001")
_USER_ID = uuid.UUID("00000000-0000-0000-0000-000000000002")
_RUN_ID = uuid.uuid4()
_PIPELINE_ID = uuid.uuid4()
_SNAPSHOT_ID = uuid.uuid4()


def _make_settings() -> Settings:
    return Settings(
        database_url="postgresql+asyncpg://localhost/test",
        secret_key=_VALID_32,
        fernet_key=_VALID_32,
        modulo_admin_password="testpass",
    )


def _make_run(
    status: str = "complete",
    *,
    input_payload: dict[str, Any] | None = None,
    outputs_json: dict[str, Any] | None = None,
) -> MagicMock:
    r = MagicMock()
    r.id = _RUN_ID
    r.pipeline_id = _PIPELINE_ID
    r.snapshot_id = _SNAPSHOT_ID
    r.status = status
    r.input_payload = input_payload
    r.outputs_json = outputs_json
    return r


def _make_snapshot(graph_json: dict[str, Any] | None = None) -> MagicMock:
    s = MagicMock()
    s.id = _SNAPSHOT_ID
    s.graph_json = graph_json or {"nodes": [], "edges": []}
    return s


def _make_mock_session() -> AsyncMock:
    session = AsyncMock(spec=AsyncSession)
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
    mock_engine = MagicMock()

    async def override_session() -> AsyncGenerator[AsyncMock, None]:
        yield mock_session

    app.dependency_overrides[get_settings] = _make_settings
    app.dependency_overrides[get_db_session] = override_session
    app.dependency_overrides[_get_engine] = lambda: mock_engine
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
def unauth_client() -> Generator[TestClient, None, None]:
    app.dependency_overrides[get_settings] = _make_settings
    mock_plan = MagicMock()
    mock_plan.feature_enabled.return_value = True
    app.dependency_overrides[get_plan_context] = lambda: mock_plan
    yield TestClient(app)
    app.dependency_overrides.clear()


class TestExportFixture:
    """GET /api/v1/runs/{run_id}/export-fixture"""

    def test_export_fixture_returns_200_with_fixture_map(self, client: TestClient, mock_session: AsyncMock) -> None:
        run = _make_run(
            input_payload={"prompt": "Hello"},
            outputs_json={"node-1": "World"},
        )
        snapshot = _make_snapshot(graph_json={"nodes": [{"id": "a"}], "edges": []})

        mock_session.execute.return_value.scalar_one_or_none = MagicMock(side_effect=[run, snapshot])

        resp = client.get(f"/api/v1/runs/{_RUN_ID}/export-fixture")

        assert resp.status_code == 200
        body = resp.json()
        assert body["run_id"] == str(_RUN_ID)
        assert body["pipeline_id"] == str(_PIPELINE_ID)
        assert body["status"] == "complete"
        assert body["fixture_name"].startswith("run_")
        assert body["fixture_name"].endswith("_io")
        assert isinstance(body["fixture_map"], dict)
        assert isinstance(body["snapshot_graph_json"], dict)
        assert body["input_payload"] == {"prompt": "Hello"}
        assert body["outputs_json"] == {"node-1": "World"}

    def test_export_fixture_flat_outputs_produces_single_map_entry(
        self, client: TestClient, mock_session: AsyncMock
    ) -> None:
        run = _make_run(
            input_payload={"prompt": "write a poem"},
            outputs_json={"result": "Roses are red"},
        )
        snapshot = _make_snapshot()

        mock_session.execute.return_value.scalar_one_or_none = MagicMock(side_effect=[run, snapshot])

        resp = client.get(f"/api/v1/runs/{_RUN_ID}/export-fixture")
        body = resp.json()
        assert len(body["fixture_map"]) == 1
        key = " ".join(str({"prompt": "write a poem"}).split())
        assert body["fixture_map"][key] == str({"result": "Roses are red"})

    def test_export_fixture_per_node_outputs_produces_multi_entry_map(
        self, client: TestClient, mock_session: AsyncMock
    ) -> None:
        run = _make_run(
            input_payload={"prompt": "build a thing"},
            outputs_json={
                "planner": {"input": "design", "output": "blueprint"},
                "coder": {"input": "blueprint", "output": "code"},
            },
        )
        snapshot = _make_snapshot()

        mock_session.execute.return_value.scalar_one_or_none = MagicMock(side_effect=[run, snapshot])

        resp = client.get(f"/api/v1/runs/{_RUN_ID}/export-fixture")
        body = resp.json()
        assert len(body["fixture_map"]) == 2
        assert body["fixture_map"]["design"] == "blueprint"
        assert body["fixture_map"]["blueprint"] == "code"

    def test_export_fixture_run_not_found_returns_404(self, client: TestClient, mock_session: AsyncMock) -> None:
        mock_session.execute.return_value.scalar_one_or_none = MagicMock(return_value=None)

        resp = client.get(f"/api/v1/runs/{_RUN_ID}/export-fixture")

        assert resp.status_code == 404
        assert "not found" in resp.json()["detail"].lower()

    def test_export_fixture_unauthenticated_returns_4xx(self, unauth_client: TestClient) -> None:
        resp = unauth_client.get(f"/api/v1/runs/{_RUN_ID}/export-fixture")
        assert resp.status_code in (401, 403)

    def test_export_fixture_returns_graph_json_from_snapshot(self, client: TestClient, mock_session: AsyncMock) -> None:
        expected_graph = {
            "nodes": [{"id": "a", "agent_id": "agent-1"}],
            "edges": [{"source": "a", "target": "b"}],
        }
        run = _make_run()
        snapshot = _make_snapshot(graph_json=expected_graph)

        mock_session.execute.return_value.scalar_one_or_none = MagicMock(side_effect=[run, snapshot])

        resp = client.get(f"/api/v1/runs/{_RUN_ID}/export-fixture")
        body = resp.json()
        assert body["snapshot_graph_json"] == expected_graph


class TestBuildFixtureMap:
    """_build_fixture_map standalone function."""

    def test_flat_outputs(self) -> None:
        result = _build_fixture_map(
            input_payload={"q": "hello"},
            outputs_json={"answer": "hi"},
        )
        key = " ".join(str({"q": "hello"}).split())
        assert result == {key: str({"answer": "hi"})}

    def test_per_node_outputs(self) -> None:
        result = _build_fixture_map(
            input_payload={"q": "hello"},
            outputs_json={
                "node-1": {"input": "hello", "output": "world"},
            },
        )
        assert result == {"hello": "world"}

    def test_none_inputs(self) -> None:
        result = _build_fixture_map(None, None)
        assert result == {"{}": "{}"}

    def test_mixed_node_types(self) -> None:
        result = _build_fixture_map(
            input_payload={"prompt": "test"},
            outputs_json={
                "lookup": {"input": "find X", "output": "X=42"},
                "summary": "Plain text result",
            },
        )
        assert result == {"find X": "X=42"}

    def test_empty_input_payload(self) -> None:
        result = _build_fixture_map({}, {"result": "ok"})
        assert len(result) == 1
        assert result["{}"] == str({"result": "ok"})
