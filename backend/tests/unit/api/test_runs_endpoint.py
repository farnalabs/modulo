"""Unit tests for POST/GET /api/v1/runs endpoints."""

import uuid
from collections.abc import AsyncGenerator, Generator
from contextlib import asynccontextmanager
from decimal import Decimal
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import AsyncSession

from modulo.api.dependencies import _get_engine, get_db_session
from modulo.api.main import app
from modulo.auth.dependencies import get_current_user
from modulo.auth.jwt import AuthenticatedPrincipal
from modulo.settings import Settings, get_settings

_VALID_32 = "a" * 32
_ORG_ID = uuid.UUID("00000000-0000-0000-0000-000000000001")
_USER_ID = uuid.UUID("00000000-0000-0000-0000-000000000002")
_PIPELINE_ID = uuid.uuid4()
_RUN_ID = uuid.uuid4()
_SNAPSHOT_ID = uuid.uuid4()
_THREAD_ID = str(uuid.uuid4())


# ---------------------------------------------------------------------------
# Mock helpers
# ---------------------------------------------------------------------------


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
    return p


def _make_run(
    status: str = "pending",
    *,
    error_detail: str | None = None,
    error_code: str | None = None,
    total_cost_usd: Decimal | None = None,
    total_tokens: int | None = None,
    node_token_usage: dict[str, Any] | None = None,
) -> MagicMock:
    r = MagicMock()
    r.id = _RUN_ID
    r.pipeline_id = _PIPELINE_ID
    r.status = status
    r.langgraph_thread_id = _THREAD_ID
    r.error_detail = error_detail
    r.error_code = error_code
    r.total_cost_usd = total_cost_usd
    r.total_tokens = total_tokens
    r.node_token_usage = node_token_usage
    return r


def _make_snapshot() -> MagicMock:
    snapshot = MagicMock()
    snapshot.id = _SNAPSHOT_ID
    snapshot.graph_json = {
        "nodes": [{"id": "node-a", "role": None}],
        "edges": [],
    }
    return snapshot


def _make_mock_session() -> AsyncMock:
    """Async session that supports `async with session.begin()`."""
    session = AsyncMock(spec=AsyncSession)
    begin_cm = AsyncMock()
    begin_cm.__aenter__ = AsyncMock(return_value=None)
    begin_cm.__aexit__ = AsyncMock(return_value=False)
    session.begin = MagicMock(return_value=begin_cm)
    return session


@asynccontextmanager
async def _noop_engine_ctx():
    yield MagicMock()


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


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

    yield TestClient(app)

    app.dependency_overrides.clear()


@pytest.fixture()
def unauth_client() -> Generator[TestClient, None, None]:
    """Client with no authentication override — relies on real auth."""
    app.dependency_overrides[get_settings] = _make_settings
    yield TestClient(app)
    app.dependency_overrides.clear()


# ---------------------------------------------------------------------------
# POST /api/v1/runs — success
# ---------------------------------------------------------------------------


def test_trigger_run_returns_202(client: TestClient) -> None:
    pipeline = _make_pipeline()
    run = _make_run()

    with (
        patch("modulo.api.routes.runs.get_pipeline", return_value=pipeline),
        patch(
            "modulo.api.routes.runs.create_snapshot_from_live_graph",
            return_value=_make_snapshot(),
        ),
        patch("modulo.api.routes.runs.create_run", return_value=run),
        patch("modulo.api.routes.runs.set_rls_org") as set_org,
        patch("modulo.api.routes.runs.PipelineExecutor") as mock_executor_cls,
    ):
        mock_executor = AsyncMock()
        mock_executor.execute = AsyncMock(return_value=_make_run(status="complete"))
        mock_executor_cls.return_value = mock_executor

        resp = client.post(
            "/api/v1/runs",
            json={"pipeline_id": str(_PIPELINE_ID), "input_payload": {"k": "v"}},
        )

    assert resp.status_code == 202
    body = resp.json()
    assert body["run_id"] == str(_RUN_ID)
    assert body["status"] == "pending"
    assert body["pipeline_id"] == str(_PIPELINE_ID)
    assert body["langgraph_thread_id"] == _THREAD_ID
    assert set_org.await_args_list[0].args[1] == _ORG_ID


def test_trigger_run_body_includes_thread_id(client: TestClient) -> None:
    pipeline = _make_pipeline()
    run = _make_run()

    with (
        patch("modulo.api.routes.runs.get_pipeline", return_value=pipeline),
        patch(
            "modulo.api.routes.runs.create_snapshot_from_live_graph",
            return_value=_make_snapshot(),
        ) as create_snapshot,
        patch("modulo.api.routes.runs.create_run", return_value=run) as create_run_mock,
        patch("modulo.api.routes.runs.set_rls_org"),
        patch("modulo.api.routes.runs.PipelineExecutor") as mock_executor_cls,
    ):
        mock_executor_cls.return_value.execute = AsyncMock(return_value=run)
        resp = client.post(
            "/api/v1/runs",
            json={"pipeline_id": str(_PIPELINE_ID)},
        )

    assert "langgraph_thread_id" in resp.json()
    assert create_run_mock.await_args.kwargs["snapshot_id"] == _SNAPSHOT_ID
    assert create_snapshot.await_args.kwargs["created_by"] == _USER_ID


# ---------------------------------------------------------------------------
# POST /api/v1/runs — pipeline not found
# ---------------------------------------------------------------------------


def test_trigger_run_pipeline_not_found_returns_404(client: TestClient) -> None:
    with (
        patch("modulo.api.routes.runs.get_pipeline", return_value=None),
        patch("modulo.api.routes.runs.set_rls_org"),
    ):
        resp = client.post(
            "/api/v1/runs",
            json={"pipeline_id": str(uuid.uuid4())},
        )

    assert resp.status_code == 404


# ---------------------------------------------------------------------------
# POST /api/v1/runs — unauthenticated
# ---------------------------------------------------------------------------


def test_trigger_run_unauthenticated_returns_4xx(unauth_client: TestClient) -> None:
    resp = unauth_client.post(
        "/api/v1/runs",
        json={"pipeline_id": str(_PIPELINE_ID)},
    )
    assert resp.status_code in (401, 403)


# ---------------------------------------------------------------------------
# GET /api/v1/runs/{run_id} — success
# ---------------------------------------------------------------------------


def test_get_run_returns_200(client: TestClient) -> None:
    run = _make_run(status="running")

    with (
        patch("modulo.api.routes.runs.get_run", return_value=run),
        patch("modulo.api.routes.runs.set_rls_org") as set_org,
    ):
        resp = client.get(f"/api/v1/runs/{_RUN_ID}")

    assert resp.status_code == 200
    body = resp.json()
    assert body["run_id"] == str(_RUN_ID)
    assert body["status"] == "running"
    assert body["pipeline_id"] == str(_PIPELINE_ID)
    assert set_org.await_args.args[1] == _ORG_ID


def test_get_run_returns_current_status(client: TestClient) -> None:
    run = _make_run(status="complete")

    with (
        patch("modulo.api.routes.runs.get_run", return_value=run),
        patch("modulo.api.routes.runs.set_rls_org"),
    ):
        resp = client.get(f"/api/v1/runs/{_RUN_ID}")

    assert resp.json()["status"] == "complete"


# ---------------------------------------------------------------------------
# GET /api/v1/runs/{run_id} — not found
# ---------------------------------------------------------------------------


def test_get_run_not_found_returns_404(client: TestClient) -> None:
    with (
        patch("modulo.api.routes.runs.get_run", return_value=None),
        patch("modulo.api.routes.runs.set_rls_org"),
    ):
        resp = client.get(f"/api/v1/runs/{uuid.uuid4()}")

    assert resp.status_code == 404


# ---------------------------------------------------------------------------
# GET /api/v1/runs/{run_id} — unauthenticated
# ---------------------------------------------------------------------------


def test_get_run_unauthenticated_returns_4xx(unauth_client: TestClient) -> None:
    resp = unauth_client.get(f"/api/v1/runs/{_RUN_ID}")
    assert resp.status_code in (401, 403)


# ---------------------------------------------------------------------------
# POST /api/v1/runs/{run_id}/cancel — success
# ---------------------------------------------------------------------------


def test_cancel_run_returns_202(client: TestClient) -> None:
    run = _make_run(status="running")

    with (
        patch("modulo.api.routes.runs.get_run", return_value=run),
        patch("modulo.api.routes.runs.request_cancellation") as mock_cancel,
        patch("modulo.api.routes.runs.set_rls_org"),
    ):
        resp = client.post(f"/api/v1/runs/{_RUN_ID}/cancel")

    assert resp.status_code == 202
    mock_cancel.assert_awaited_once()


def test_cancel_run_already_terminal_returns_409(client: TestClient) -> None:
    run = _make_run(status="complete")

    with (
        patch("modulo.api.routes.runs.get_run", return_value=run),
        patch("modulo.api.routes.runs.set_rls_org"),
    ):
        resp = client.post(f"/api/v1/runs/{_RUN_ID}/cancel")

    assert resp.status_code == 409


def test_cancel_run_not_found_returns_404(client: TestClient) -> None:
    with (
        patch("modulo.api.routes.runs.get_run", return_value=None),
        patch("modulo.api.routes.runs.set_rls_org"),
    ):
        resp = client.post(f"/api/v1/runs/{uuid.uuid4()}/cancel")

    assert resp.status_code == 404


# ---------------------------------------------------------------------------
# RunResponse — new field serialization
# ---------------------------------------------------------------------------


def test_run_response_serializes_error_detail(client: TestClient) -> None:
    run = _make_run(
        status="failed",
        error_detail="LLM provider returned 429 Too Many Requests",
        error_code="rate_limited",
    )
    with (
        patch("modulo.api.routes.runs.get_run", return_value=run),
        patch("modulo.api.routes.runs.set_rls_org"),
    ):
        resp = client.get(f"/api/v1/runs/{_RUN_ID}")

    assert resp.status_code == 200
    body = resp.json()
    assert body["error_detail"] == "LLM provider returned 429 Too Many Requests"
    assert body["error_code"] == "rate_limited"


def test_run_response_error_detail_none_when_run_succeeded(client: TestClient) -> None:
    run = _make_run(status="complete", error_detail=None, error_code=None)
    with (
        patch("modulo.api.routes.runs.get_run", return_value=run),
        patch("modulo.api.routes.runs.set_rls_org"),
    ):
        resp = client.get(f"/api/v1/runs/{_RUN_ID}")

    body = resp.json()
    assert body["error_detail"] is None
    assert body["error_code"] is None


def test_run_response_populates_total_cost(client: TestClient) -> None:
    run = _make_run(status="complete", total_cost_usd=Decimal("1.234567"))
    with (
        patch("modulo.api.routes.runs.get_run", return_value=run),
        patch("modulo.api.routes.runs.set_rls_org"),
    ):
        resp = client.get(f"/api/v1/runs/{_RUN_ID}")

    body = resp.json()
    assert body["total_cost_usd"] == "1.234567"


def test_run_response_total_cost_none_when_not_available(client: TestClient) -> None:
    run = _make_run(status="pending", total_cost_usd=None)
    with (
        patch("modulo.api.routes.runs.get_run", return_value=run),
        patch("modulo.api.routes.runs.set_rls_org"),
    ):
        resp = client.get(f"/api/v1/runs/{_RUN_ID}")

    body = resp.json()
    assert body["total_cost_usd"] is None


def test_run_response_populates_token_consumption(client: TestClient) -> None:
    run = _make_run(status="complete", total_tokens=1500)
    with (
        patch("modulo.api.routes.runs.get_run", return_value=run),
        patch("modulo.api.routes.runs.set_rls_org"),
    ):
        resp = client.get(f"/api/v1/runs/{_RUN_ID}")

    body = resp.json()
    assert body["token_consumption"] == {"total_tokens": 1500}


def test_run_response_token_consumption_none_when_no_tokens(client: TestClient) -> None:
    run = _make_run(status="pending", total_tokens=None)
    with (
        patch("modulo.api.routes.runs.get_run", return_value=run),
        patch("modulo.api.routes.runs.set_rls_org"),
    ):
        resp = client.get(f"/api/v1/runs/{_RUN_ID}")

    body = resp.json()
    assert body["token_consumption"] is None


def test_run_response_populates_node_token_usage(client: TestClient) -> None:
    run = _make_run(
        status="complete",
        node_token_usage={
            "planner": {"input_tokens": 150, "output_tokens": 450, "total_tokens": 600, "cost_usd": 0.015},
            "coder": {"input_tokens": 1200, "output_tokens": 3200, "total_tokens": 4400, "cost_usd": 0.108},
        },
    )
    with (
        patch("modulo.api.routes.runs.get_run", return_value=run),
        patch("modulo.api.routes.runs.set_rls_org"),
    ):
        resp = client.get(f"/api/v1/runs/{_RUN_ID}")

    body = resp.json()
    ntu = body["node_token_usage"]
    assert isinstance(ntu, dict)
    assert "planner" in ntu
    assert "coder" in ntu
    assert ntu["planner"]["input_tokens"] == 150
    assert ntu["planner"]["output_tokens"] == 450
    assert ntu["planner"]["total_tokens"] == 600
    assert ntu["coder"]["total_tokens"] == 4400


def test_run_response_node_token_usage_none_when_not_available(client: TestClient) -> None:
    run = _make_run(status="pending", node_token_usage=None)
    with (
        patch("modulo.api.routes.runs.get_run", return_value=run),
        patch("modulo.api.routes.runs.set_rls_org"),
    ):
        resp = client.get(f"/api/v1/runs/{_RUN_ID}")

    body = resp.json()
    assert body["node_token_usage"] is None


def test_run_response_populates_trace_id(client: TestClient) -> None:
    run = _make_run(status="running")
    with (
        patch("modulo.api.routes.runs.get_run", return_value=run),
        patch("modulo.api.routes.runs.set_rls_org"),
    ):
        resp = client.get(f"/api/v1/runs/{_RUN_ID}")

    body = resp.json()
    trace_id = body["trace_id"]
    assert isinstance(trace_id, str)
    assert len(trace_id) == 36  # UUID format


def test_run_response_trace_id_deterministic(client: TestClient) -> None:
    run = _make_run(status="complete")
    with (
        patch("modulo.api.routes.runs.get_run", return_value=run),
        patch("modulo.api.routes.runs.set_rls_org"),
    ):
        resp1 = client.get(f"/api/v1/runs/{_RUN_ID}")
        resp2 = client.get(f"/api/v1/runs/{_RUN_ID}")

    assert resp1.json()["trace_id"] == resp2.json()["trace_id"]


def test_run_response_all_new_fields_present_in_trigger_endpoint(client: TestClient) -> None:
    pipeline = _make_pipeline()
    run = _make_run(status="pending")

    with (
        patch("modulo.api.routes.runs.get_pipeline", return_value=pipeline),
        patch(
            "modulo.api.routes.runs.create_snapshot_from_live_graph",
            return_value=_make_snapshot(),
        ),
        patch("modulo.api.routes.runs.create_run", return_value=run),
        patch("modulo.api.routes.runs.set_rls_org"),
        patch("modulo.api.routes.runs.PipelineExecutor") as mock_executor_cls,
    ):
        mock_executor_cls.return_value.execute = AsyncMock(return_value=run)
        resp = client.post(
            "/api/v1/runs",
            json={"pipeline_id": str(_PIPELINE_ID), "input_payload": {}},
        )

    assert resp.status_code == 202
    body = resp.json()
    assert body["run_id"] == str(_RUN_ID)
    assert body["status"] == "pending"
    assert body["pipeline_id"] == str(_PIPELINE_ID)
    assert body["langgraph_thread_id"] == _THREAD_ID
    # New fields should be None for a pending run
    assert body["error_detail"] is None
    assert body["error_code"] is None
    assert body["total_cost_usd"] is None
    assert body["token_consumption"] is None
    trace_id = body.get("trace_id")
    assert trace_id is not None
    assert isinstance(trace_id, str)


# ---------------------------------------------------------------------------
# Pre-run input validation
# ---------------------------------------------------------------------------


def test_trigger_run_input_validation_cycle_detected(client: TestClient) -> None:
    """A graph with a cycle should be rejected at trigger time."""
    pipeline = _make_pipeline()
    snapshot = _make_snapshot()
    snapshot.graph_json = {
        "nodes": [{"id": "a"}, {"id": "b"}],
        "edges": [{"source_node_id": "a", "target_node_id": "b"}, {"source_node_id": "b", "target_node_id": "a"}],
    }

    with (
        patch("modulo.api.routes.runs.get_pipeline", return_value=pipeline),
        patch("modulo.api.routes.runs.create_snapshot_from_live_graph", return_value=snapshot),
        patch("modulo.api.routes.runs.set_rls_org"),
    ):
        resp = client.post(
            "/api/v1/runs",
            json={"pipeline_id": str(_PIPELINE_ID), "input_payload": {}},
        )

    assert resp.status_code == 422
    assert "cycle" in resp.json()["detail"].lower()


# ---------------------------------------------------------------------------
# _run_in_background — failure transitions run to "failed"
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_run_in_background_marks_run_failed_on_executor_error() -> None:
    executor = AsyncMock()
    executor.execute = AsyncMock(side_effect=RuntimeError("executor blew up"))
    run_id = uuid.uuid4()
    org_id = _ORG_ID
    mock_session = _make_mock_session()
    mock_session.execute = AsyncMock()

    from modulo.api.routes.runs import _run_in_background

    with (
        patch("modulo.api.routes.runs.get_settings"),
        patch("modulo.api.routes.runs.get_or_create_engine"),
        patch("modulo.api.routes.runs.async_sessionmaker") as mock_factory_cls,
        patch("modulo.api.routes.runs.update_run_status") as mock_update,
        patch("modulo.api.routes.runs.set_rls_org") as mock_rls,
    ):
        mock_factory = MagicMock()
        mock_factory.return_value.__aenter__ = AsyncMock(return_value=mock_session)
        mock_factory.return_value.__aexit__ = AsyncMock(return_value=False)
        mock_factory_cls.return_value = mock_factory

        await _run_in_background(executor, run_id, org_id, {})

        mock_rls.assert_awaited_once_with(mock_session, org_id)
        mock_update.assert_awaited_once_with(mock_session, run_id, "failed", error_code="internal_error")


# ---------------------------------------------------------------------------
# POST /api/v1/runs/diff — node output diff across runs (task-agent-output-diff)
# ---------------------------------------------------------------------------


def test_diff_node_output_success(client: TestClient) -> None:
    run_id_a = uuid.uuid4()
    run_id_b = uuid.uuid4()

    run_a = _make_run(status="complete")
    run_a.id = run_id_a
    run_a.outputs_json = {"coder": {"result": "hello"}}

    run_b = _make_run(status="complete")
    run_b.id = run_id_b
    run_b.outputs_json = {"coder": {"result": "world"}}

    with (
        patch("modulo.api.routes.runs.get_run") as mock_get_run,
        patch("modulo.api.routes.runs.set_rls_org"),
    ):
        mock_get_run.side_effect = [run_a, run_b]
        resp = client.post(
            "/api/v1/runs/diff",
            json={
                "run_id_a": str(run_id_a),
                "node_id_a": "coder",
                "run_id_b": str(run_id_b),
                "node_id_b": "coder",
            },
        )

    assert resp.status_code == 200
    body = resp.json()
    assert body["has_diff"] is True
    assert body["run_id_a"] == str(run_id_a)
    assert body["run_id_b"] == str(run_id_b)
    assert body["node_output_a"] == {"result": "hello"}
    assert body["node_output_b"] == {"result": "world"}
    types = [l["type"] for l in body["diff_lines"]]
    assert "added" in types
    assert "removed" in types
    assert "unchanged" in types


def test_diff_node_output_identical(client: TestClient) -> None:
    run_id_a = uuid.uuid4()
    run_id_b = uuid.uuid4()

    run_a = _make_run(status="complete")
    run_a.id = run_id_a
    run_a.outputs_json = {"coder": {"result": "hello"}}

    run_b = _make_run(status="complete")
    run_b.id = run_id_b
    run_b.outputs_json = {"coder": {"result": "hello"}}

    with (
        patch("modulo.api.routes.runs.get_run") as mock_get_run,
        patch("modulo.api.routes.runs.set_rls_org"),
    ):
        mock_get_run.side_effect = [run_a, run_b]
        resp = client.post(
            "/api/v1/runs/diff",
            json={
                "run_id_a": str(run_id_a),
                "node_id_a": "coder",
                "run_id_b": str(run_id_b),
                "node_id_b": "coder",
            },
        )

    assert resp.status_code == 200
    body = resp.json()
    assert body["has_diff"] is False
    for line in body["diff_lines"]:
        assert line["type"] == "unchanged"


def test_diff_node_output_run_not_found(client: TestClient) -> None:
    run_b = _make_run(status="complete")
    run_b.id = uuid.uuid4()
    run_b.outputs_json = {"coder": {"result": "world"}}

    with (
        patch("modulo.api.routes.runs.get_run") as mock_get_run,
        patch("modulo.api.routes.runs.set_rls_org"),
    ):
        mock_get_run.side_effect = [None, run_b]
        resp = client.post(
            "/api/v1/runs/diff",
            json={
                "run_id_a": str(uuid.uuid4()),
                "node_id_a": "coder",
                "run_id_b": str(run_b.id),
                "node_id_b": "coder",
            },
        )

    assert resp.status_code == 404
    assert "not found" in resp.json()["detail"].lower()


def test_diff_node_output_node_not_found(client: TestClient) -> None:
    run_id_a = uuid.uuid4()
    run_id_b = uuid.uuid4()

    run_a = _make_run(status="complete")
    run_a.id = run_id_a
    run_a.outputs_json = {"other-node": "value"}

    run_b = _make_run(status="complete")
    run_b.id = run_id_b
    run_b.outputs_json = {"coder": {"result": "world"}}

    with (
        patch("modulo.api.routes.runs.get_run") as mock_get_run,
        patch("modulo.api.routes.runs.set_rls_org"),
    ):
        mock_get_run.side_effect = [run_a, run_b]
        resp = client.post(
            "/api/v1/runs/diff",
            json={
                "run_id_a": str(run_id_a),
                "node_id_a": "coder",
                "run_id_b": str(run_id_b),
                "node_id_b": "coder",
            },
        )

    assert resp.status_code == 404
    assert "coder" in resp.json()["detail"]
