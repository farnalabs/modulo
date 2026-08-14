"""Unit tests for GET /api/v1/runs/{run_id}/events (live run events, FAR-98)."""

import uuid
from collections.abc import AsyncGenerator, Generator
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from modulo.api.dependencies import _get_engine, get_db_session, get_plan_context
from modulo.api.main import app
from modulo.api.routes.runs import RunNotFoundError
from modulo.auth.dependencies import get_current_tenant_user, get_current_tenant_user_or_api_key, get_current_user
from modulo.auth.jwt import TenantPrincipal
from modulo.core.pipeline_engine.event_broker import RunEventBroker
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
        redis_url="redis://localhost:6379/0",
    )


def _make_mock_session() -> MagicMock:
    """Async session that supports `async with session.begin()` (auth dependency)."""
    session = MagicMock()
    begin_cm = MagicMock()
    begin_cm.__aenter__ = AsyncMock(return_value=None)
    begin_cm.__aexit__ = AsyncMock(return_value=False)
    session.begin = MagicMock(return_value=begin_cm)
    exec_result = MagicMock()
    exec_result.scalar_one_or_none.return_value = None
    session.execute = AsyncMock(return_value=exec_result)
    return session


@pytest.fixture
def client() -> Generator[TestClient, None, None]:
    mock_session = _make_mock_session()

    async def override_session() -> AsyncGenerator[MagicMock, None]:
        yield mock_session

    app.dependency_overrides[get_settings] = _make_settings
    app.dependency_overrides[get_db_session] = override_session
    app.dependency_overrides[_get_engine] = lambda: MagicMock()
    app.dependency_overrides[get_current_user] = lambda: TenantPrincipal(
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
    app.dependency_overrides[get_current_tenant_user_or_api_key] = lambda: TenantPrincipal(
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


def _make_run() -> MagicMock:
    run = MagicMock()
    run.id = _RUN_ID
    run.status = "running"
    return run


def test_get_run_events_returns_chunk_events_since_seq(client: TestClient) -> None:
    """Only node.stdout_chunk/node.stderr_chunk events with seq > since_seq are returned."""
    broker = RunEventBroker(_RUN_ID)
    broker.publish("node_started", {"node_id": "n1"})  # seq=1 — not a chunk event
    broker.publish("node.stdout_chunk", {"node_id": "n1", "chunk": "hello"})  # seq=2
    broker.publish("run_completed", {})  # seq=3 — not a chunk event
    broker.publish("node.stderr_chunk", {"node_id": "n1", "chunk": "warn"})  # seq=4

    with (
        patch("modulo.api.routes.runs._do_get_run", return_value=_make_run()),
        patch("modulo.api.routes.runs.get_registry") as mock_registry,
    ):
        mock_registry.return_value.get.return_value = broker
        resp = client.get(f"/api/v1/runs/{_RUN_ID}/events?since_seq=1")

    assert resp.status_code == 200
    body = resp.json()
    assert body["run_id"] == str(_RUN_ID)
    events = body["events"]
    assert [e["seq"] for e in events] == [2, 4]
    assert events[0]["event_type"] == "node.stdout_chunk"
    assert events[0]["payload"]["chunk"] == "hello"
    assert events[1]["event_type"] == "node.stderr_chunk"
    assert "ts" in events[0]


def test_get_run_events_filters_by_node_id(client: TestClient) -> None:
    broker = RunEventBroker(_RUN_ID)
    broker.publish("node.stdout_chunk", {"node_id": "n1", "chunk": "a"})
    broker.publish("node.stdout_chunk", {"node_id": "n2", "chunk": "b"})

    with (
        patch("modulo.api.routes.runs._do_get_run", return_value=_make_run()),
        patch("modulo.api.routes.runs.get_registry") as mock_registry,
    ):
        mock_registry.return_value.get.return_value = broker
        resp = client.get(f"/api/v1/runs/{_RUN_ID}/events?node_id=n2")

    assert resp.status_code == 200
    events = resp.json()["events"]
    assert len(events) == 1
    assert events[0]["payload"]["node_id"] == "n2"
    assert events[0]["payload"]["chunk"] == "b"


def test_get_run_events_empty_when_no_broker(client: TestClient) -> None:
    """A run with no in-process broker (old/completed) returns an empty list."""
    with (
        patch("modulo.api.routes.runs._do_get_run", return_value=_make_run()),
        patch("modulo.api.routes.runs.get_registry") as mock_registry,
    ):
        mock_registry.return_value.get.return_value = None
        resp = client.get(f"/api/v1/runs/{_RUN_ID}/events")

    assert resp.status_code == 200
    assert not resp.json()["events"]


def test_get_run_events_404_when_run_not_found(client: TestClient) -> None:
    with (
        patch("modulo.api.routes.runs._do_get_run", side_effect=RunNotFoundError(_RUN_ID)),
        patch("modulo.api.routes.runs.get_registry"),
    ):
        resp = client.get(f"/api/v1/runs/{_RUN_ID}/events")

    assert resp.status_code == 404
