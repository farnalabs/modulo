"""Unit tests for POST/GET /api/v1/runs endpoints."""

import uuid
from collections.abc import AsyncGenerator, Generator
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import AsyncSession

from modulo.api.dependencies import _get_engine, _get_session_factory, get_db_session, get_plan_context
from modulo.api.main import app
from modulo.api.middleware.sensitive_mask import SENSITIVE_VALUE_MASK
from modulo.api.routes.runs import RunNotFoundError, _validate_run_input_basics
from modulo.auth.dependencies import get_current_tenant_user, get_current_tenant_user_or_api_key, get_current_user
from modulo.auth.jwt import AuthenticatedPrincipal, TenantPrincipal
from modulo.core.exceptions import OrgDeletedError
from modulo.settings import Settings, get_settings

_VALID_32 = "a" * 32
_ORG_ID = uuid.UUID("00000000-0000-0000-0000-000000000001")
_USER_ID = uuid.UUID("00000000-0000-0000-0000-000000000002")
_PIPELINE_ID = uuid.uuid4()
_RUN_ID = uuid.uuid4()
_SNAPSHOT_ID = uuid.uuid4()
_THREAD_ID = str(uuid.uuid4())


async def test_run_input_uses_legacy_target_when_new_target_is_null():
    graph = {
        "nodes": [{"id": "only-node"}],
        "edges": [{"target_node_id": None, "target": "only-node"}],
    }

    with pytest.raises(HTTPException, match="cycle detected"):
        await _validate_run_input_basics(AsyncMock(), graph, MagicMock(), {})


# ---------------------------------------------------------------------------
# Mock helpers
# ---------------------------------------------------------------------------


def _make_settings() -> Settings:
    return Settings(
        database_url="postgresql+asyncpg://localhost/test",
        secret_key=_VALID_32,
        fernet_key=_VALID_32,
        modulo_admin_password="testpass",
        redis_url="redis://localhost:6379/0",
    )


def _make_pipeline() -> MagicMock:
    p = MagicMock()
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
    p.rate_limit_config = None
    p.max_duration_seconds = None
    p.archived_at = None
    p.snapshot_count = 0
    p.created_by = uuid.uuid4()
    p.account_id = p.created_by
    p.created_at = datetime.now(UTC)
    p.updated_at = datetime.now(UTC)
    return p


def _make_run(
    status: str = "pending",
    *,
    error_detail: str | None = None,
    error_code: str | None = None,
    total_cost_usd: Decimal | None = None,
    total_tokens: int | None = None,
    node_token_usage: dict[str, Any] | None = None,
    cost_breakdown: list[dict[str, Any]] | None = None,
) -> MagicMock:
    r = MagicMock()
    r.id = _RUN_ID
    r.pipeline_id = _PIPELINE_ID
    r.pipeline_name = "Test Pipeline"
    r.pipeline = None
    r.status = status
    r.langgraph_thread_id = _THREAD_ID
    r.error_detail = error_detail
    r.error_code = error_code
    r.total_cost_usd = total_cost_usd
    r.total_tokens = total_tokens
    r.node_token_usage = node_token_usage
    r.cost_breakdown = cost_breakdown
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
    # Set up execute to return a Result-like object whose scalar_one_or_none
    # returns None by default (individual tests override via patch).
    exec_result = MagicMock()
    exec_result.scalar_one_or_none.return_value = None
    exec_result.scalars.return_value.first.return_value = None
    exec_result.all.return_value = []
    session.execute = AsyncMock(return_value=exec_result)
    return session


@asynccontextmanager
async def _noop_engine_ctx():
    yield MagicMock()


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def mock_session() -> AsyncMock:
    return _make_mock_session()


@pytest.fixture
def client(mock_session: AsyncMock) -> Generator[TestClient, None, None]:
    mock_engine = MagicMock()

    async def override_session() -> AsyncGenerator[AsyncMock, None]:
        yield mock_session

    app.dependency_overrides[get_settings] = _make_settings
    app.dependency_overrides[get_db_session] = override_session
    app.dependency_overrides[_get_engine] = lambda: mock_engine

    class _MockFactory:
        def __init__(self, s: AsyncMock) -> None:
            self._session = s

        def __call__(self):
            return self

        async def __aenter__(self) -> AsyncMock:
            return self._session

        async def __aexit__(self, *args: object) -> None:
            pass

    app.dependency_overrides[_get_session_factory] = lambda: _MockFactory(mock_session)
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


@pytest.fixture
def unauth_client() -> Generator[TestClient, None, None]:
    """Client with no authentication override — relies on real auth."""
    app.dependency_overrides[get_settings] = _make_settings
    mock_plan = MagicMock()
    mock_plan.feature_enabled.return_value = True
    app.dependency_overrides[get_plan_context] = lambda: mock_plan
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
        patch("modulo.api.routes.runs.dispatch_run", new_callable=AsyncMock),
    ):
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
        patch("modulo.api.routes.runs.dispatch_run", new_callable=AsyncMock),
    ):
        resp = client.post(
            "/api/v1/runs",
            json={"pipeline_id": str(_PIPELINE_ID)},
        )

    assert "langgraph_thread_id" in resp.json()
    assert create_run_mock.await_args.kwargs["snapshot_id"] == _SNAPSHOT_ID
    assert create_snapshot.await_args.kwargs["account_id"] == _USER_ID


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
# POST /api/v1/runs — deleted / missing organisation
# ---------------------------------------------------------------------------


def test_trigger_run_deleted_org_returns_409(client: TestClient) -> None:
    pipeline = _make_pipeline()

    with (
        patch("modulo.api.routes.runs.get_pipeline", return_value=pipeline),
        patch(
            "modulo.api.routes.runs.create_snapshot_from_live_graph",
            return_value=_make_snapshot(),
        ),
        patch(
            "modulo.api.routes.runs.create_run",
            side_effect=OrgDeletedError(org_id=_ORG_ID, deleted=True),
        ),
        patch("modulo.api.routes.runs.set_rls_org"),
    ):
        resp = client.post(
            "/api/v1/runs",
            json={"pipeline_id": str(_PIPELINE_ID), "input_payload": {"k": "v"}},
        )

    assert resp.status_code == 409
    assert "deleted" in resp.json()["detail"]


def test_trigger_run_missing_org_returns_404(client: TestClient) -> None:
    pipeline = _make_pipeline()

    with (
        patch("modulo.api.routes.runs.get_pipeline", return_value=pipeline),
        patch(
            "modulo.api.routes.runs.create_snapshot_from_live_graph",
            return_value=_make_snapshot(),
        ),
        patch(
            "modulo.api.routes.runs.create_run",
            side_effect=OrgDeletedError(org_id=_ORG_ID, deleted=False),
        ),
        patch("modulo.api.routes.runs.set_rls_org"),
    ):
        resp = client.post(
            "/api/v1/runs",
            json={"pipeline_id": str(_PIPELINE_ID), "input_payload": {"k": "v"}},
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


def test_trigger_run_rejects_unknown_fields_returns_422(client: TestClient) -> None:
    """Extra body fields (e.g. the old editor-view 'prompt'/'payload') must 422, not be silently dropped."""
    resp = client.post(
        "/api/v1/runs",
        json={"pipeline_id": str(_PIPELINE_ID), "prompt": "add a health endpoint", "payload": {}},
    )
    assert resp.status_code == 422


# ---------------------------------------------------------------------------
# GET /api/v1/runs/{run_id} — success
# ---------------------------------------------------------------------------


def test_get_run_returns_200(client: TestClient) -> None:
    run = _make_run(status="running")

    with (
        patch("modulo.api.routes.runs._do_get_run", return_value=run),
        patch("modulo.api.routes.runs.set_rls_org"),
    ):
        resp = client.get(f"/api/v1/runs/{_RUN_ID}")

    assert resp.status_code == 200
    body = resp.json()
    assert body["run_id"] == str(_RUN_ID)
    assert body["status"] == "running"
    assert body["pipeline_id"] == str(_PIPELINE_ID)


def test_get_run_returns_current_status(client: TestClient) -> None:
    run = _make_run(status="complete")

    with (
        patch("modulo.api.routes.runs._do_get_run", return_value=run),
        patch("modulo.api.routes.runs.set_rls_org"),
    ):
        resp = client.get(f"/api/v1/runs/{_RUN_ID}")

    assert resp.json()["status"] == "complete"


# ---------------------------------------------------------------------------
# GET /api/v1/runs/{run_id} — not found
# ---------------------------------------------------------------------------


def test_get_run_not_found_returns_404(client: TestClient) -> None:
    with (
        patch("modulo.api.routes.runs._do_get_run", side_effect=RunNotFoundError(uuid.uuid4())),
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
        patch("modulo.api.routes.runs._do_get_run", return_value=run),
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
        patch("modulo.api.routes.runs._do_get_run", return_value=run),
        patch("modulo.api.routes.runs.set_rls_org"),
    ):
        resp = client.get(f"/api/v1/runs/{_RUN_ID}")

    body = resp.json()
    assert body["error_detail"] is None
    assert body["error_code"] is None


def test_run_response_populates_total_cost(client: TestClient) -> None:
    run = _make_run(status="complete", total_cost_usd=Decimal("1.234567"))
    with (
        patch("modulo.api.routes.runs._do_get_run", return_value=run),
        patch("modulo.api.routes.runs.set_rls_org"),
    ):
        resp = client.get(f"/api/v1/runs/{_RUN_ID}")

    body = resp.json()
    assert body["total_cost_usd"] == "1.234567"


def test_run_response_total_cost_none_when_not_available(client: TestClient) -> None:
    run = _make_run(status="pending", total_cost_usd=None)
    with (
        patch("modulo.api.routes.runs._do_get_run", return_value=run),
        patch("modulo.api.routes.runs.set_rls_org"),
    ):
        resp = client.get(f"/api/v1/runs/{_RUN_ID}")

    body = resp.json()
    assert body["total_cost_usd"] is None


def test_run_response_populates_token_consumption(client: TestClient) -> None:
    run = _make_run(status="complete", total_tokens=1500)
    with (
        patch("modulo.api.routes.runs._do_get_run", return_value=run),
        patch("modulo.api.routes.runs.set_rls_org"),
    ):
        resp = client.get(f"/api/v1/runs/{_RUN_ID}")

    body = resp.json()
    assert body["token_consumption"] == {"total_tokens": 1500}


def test_run_response_token_consumption_none_when_no_tokens(client: TestClient) -> None:
    run = _make_run(status="pending", total_tokens=None)
    with (
        patch("modulo.api.routes.runs._do_get_run", return_value=run),
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
        patch("modulo.api.routes.runs._do_get_run", return_value=run),
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
        patch("modulo.api.routes.runs._do_get_run", return_value=run),
        patch("modulo.api.routes.runs.set_rls_org"),
    ):
        resp = client.get(f"/api/v1/runs/{_RUN_ID}")

    body = resp.json()
    assert body["node_token_usage"] is None


def test_run_response_populates_trace_id(client: TestClient) -> None:
    run = _make_run(status="running")
    with (
        patch("modulo.api.routes.runs._do_get_run", return_value=run),
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
        patch("modulo.api.routes.runs._do_get_run", return_value=run),
        patch("modulo.api.routes.runs.set_rls_org"),
    ):
        resp1 = client.get(f"/api/v1/runs/{_RUN_ID}")
        resp2 = client.get(f"/api/v1/runs/{_RUN_ID}")

    assert resp1.json()["trace_id"] == resp2.json()["trace_id"]


# ---------------------------------------------------------------------------
# Child-run cost rollup — child_runs_cost_usd / aggregate_cost_usd
# ---------------------------------------------------------------------------


def test_get_run_includes_child_cost_rollup(client: TestClient) -> None:
    run = _make_run(status="complete", total_cost_usd=Decimal("1.000000"))
    with (
        patch("modulo.api.routes.runs._do_get_run", return_value=run),
        patch(
            "modulo.api.routes.runs.get_child_run_rollup",
            new_callable=AsyncMock,
            return_value={_RUN_ID: (Decimal("0.500000"), 3)},
        ),
        patch("modulo.api.routes.runs.set_rls_org"),
    ):
        resp = client.get(f"/api/v1/runs/{_RUN_ID}")

    body = resp.json()
    # total_cost_usd keeps its own-run semantics — never mutated by the rollup.
    assert body["total_cost_usd"] == "1.000000"
    assert body["child_runs_cost_usd"] == "0.500000"
    assert body["child_runs_count"] == 3
    assert body["aggregate_cost_usd"] == "1.500000"


def test_get_run_child_cost_zero_when_no_children(client: TestClient) -> None:
    run = _make_run(status="complete", total_cost_usd=Decimal("2.000000"))
    with (
        patch("modulo.api.routes.runs._do_get_run", return_value=run),
        patch(
            "modulo.api.routes.runs.get_child_run_rollup",
            new_callable=AsyncMock,
            return_value={},
        ),
        patch("modulo.api.routes.runs.set_rls_org"),
    ):
        resp = client.get(f"/api/v1/runs/{_RUN_ID}")

    body = resp.json()
    assert body["total_cost_usd"] == "2.000000"
    assert body["child_runs_cost_usd"] == "0.000000"
    assert body["child_runs_count"] == 0
    assert body["aggregate_cost_usd"] == "2.000000"


def test_get_run_aggregate_zero_when_no_own_or_child_cost(client: TestClient) -> None:
    run = _make_run(status="pending", total_cost_usd=None)
    with (
        patch("modulo.api.routes.runs._do_get_run", return_value=run),
        patch(
            "modulo.api.routes.runs.get_child_run_rollup",
            new_callable=AsyncMock,
            return_value={},
        ),
        patch("modulo.api.routes.runs.set_rls_org"),
    ):
        resp = client.get(f"/api/v1/runs/{_RUN_ID}")

    body = resp.json()
    assert body["total_cost_usd"] is None
    assert body["child_runs_cost_usd"] == "0.000000"
    assert body["child_runs_count"] == 0
    assert body["aggregate_cost_usd"] == "0.000000"


def _make_listable_run(
    run_id: uuid.UUID,
    *,
    status: str = "complete",
    total_cost_usd: Decimal | None = None,
) -> MagicMock:
    r = _make_run(status=status, total_cost_usd=total_cost_usd)
    r.id = run_id
    r.trigger_type = "manual"
    r.run_number = 1
    r.created_at = None
    r.started_at = None
    r.completed_at = None
    r.account_id = None
    return r


def _make_page(items: list[Any], total: int) -> MagicMock:
    page = MagicMock()
    page.items = items
    page.total = total
    page.page = 1
    page.page_size = 20
    page.next_cursor = None
    page.has_more = False
    return page


def test_list_runs_includes_child_cost_rollup(client: TestClient) -> None:
    parent_id = uuid.uuid4()
    child_id = uuid.uuid4()
    parent = _make_listable_run(parent_id, total_cost_usd=Decimal("1.000000"))
    child = _make_listable_run(child_id, total_cost_usd=Decimal("0.500000"))

    with (
        patch(
            "modulo.api.routes.runs.db_list_runs",
            new_callable=AsyncMock,
            return_value=_make_page([parent, child], 2),
        ),
        patch(
            "modulo.api.routes.runs.get_child_run_rollup",
            new_callable=AsyncMock,
            return_value={parent_id: (Decimal("0.500000"), 2)},
        ),
        patch("modulo.api.routes.runs.set_rls_org"),
    ):
        resp = client.get("/api/v1/runs")

    assert resp.status_code == 200
    body = resp.json()
    items = body["items"]
    parent_item = next(i for i in items if i["run_id"] == str(parent_id))
    child_item = next(i for i in items if i["run_id"] == str(child_id))
    # Parent: own cost unchanged, child cost rolled up, count reported.
    assert parent_item["total_cost_usd"] == "1.000000"
    assert parent_item["child_runs_cost_usd"] == "0.500000"
    assert parent_item["child_runs_count"] == 2
    assert parent_item["aggregate_cost_usd"] == "1.500000"
    # Child (no children of its own): zeros.
    assert child_item["total_cost_usd"] == "0.500000"
    assert child_item["child_runs_cost_usd"] == "0.000000"
    assert child_item["child_runs_count"] == 0
    assert child_item["aggregate_cost_usd"] == "0.500000"


def test_list_runs_child_cost_zero_when_no_children(client: TestClient) -> None:
    run_id = uuid.uuid4()
    run = _make_listable_run(run_id, total_cost_usd=Decimal("3.000000"))

    with (
        patch(
            "modulo.api.routes.runs.db_list_runs",
            new_callable=AsyncMock,
            return_value=_make_page([run], 1),
        ),
        patch(
            "modulo.api.routes.runs.get_child_run_rollup",
            new_callable=AsyncMock,
            return_value={},
        ),
        patch("modulo.api.routes.runs.set_rls_org"),
    ):
        resp = client.get("/api/v1/runs")

    body = resp.json()
    item = body["items"][0]
    assert item["total_cost_usd"] == "3.000000"
    assert item["child_runs_cost_usd"] == "0.000000"
    assert item["child_runs_count"] == 0
    assert item["aggregate_cost_usd"] == "3.000000"


def test_list_runs_includes_truncated_error_detail_preview(client: TestClient) -> None:
    run_id = uuid.uuid4()
    run = _make_listable_run(run_id)
    run.error_code = "task_failure"
    run.error_detail = "e" * 500

    with (
        patch(
            "modulo.api.routes.runs.db_list_runs",
            new_callable=AsyncMock,
            return_value=_make_page([run], 1),
        ),
        patch(
            "modulo.api.routes.runs.get_child_run_rollup",
            new_callable=AsyncMock,
            return_value={},
        ),
        patch("modulo.api.routes.runs.set_rls_org"),
    ):
        resp = client.get("/api/v1/runs")

    assert resp.status_code == 200
    item = resp.json()["items"][0]
    assert item["error_code"] == "task_failure"
    assert item["error_detail"].endswith("…")
    assert len(item["error_detail"]) == 201


def test_list_runs_error_detail_none_for_success(client: TestClient) -> None:
    run_id = uuid.uuid4()
    run = _make_listable_run(run_id)  # error_detail defaults to None

    with (
        patch(
            "modulo.api.routes.runs.db_list_runs",
            new_callable=AsyncMock,
            return_value=_make_page([run], 1),
        ),
        patch(
            "modulo.api.routes.runs.get_child_run_rollup",
            new_callable=AsyncMock,
            return_value={},
        ),
        patch("modulo.api.routes.runs.set_rls_org"),
    ):
        resp = client.get("/api/v1/runs")

    assert resp.status_code == 200
    item = resp.json()["items"][0]
    assert item["error_code"] is None
    assert item["error_detail"] is None


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
        patch("modulo.api.routes.runs.dispatch_run", new_callable=AsyncMock),
    ):
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
    types = [line["type"] for line in body["diff_lines"]]
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


# ---------------------------------------------------------------------------
# POST /api/v1/runs/{run_id}/nodes/{node_id}/recover — dispatch outcome handling
# ---------------------------------------------------------------------------


def test_recover_node_enqueued_returns_200(client: TestClient) -> None:
    """A successfully enqueued recover-node resume returns 200 (replay mode
    when input_data is supplied)."""
    run = _make_run(status="running")
    with (
        patch("modulo.api.routes.runs.set_rls_org"),
        patch("modulo.api.routes.runs.recover_node", new_callable=AsyncMock, return_value=run),
        patch(
            "modulo.api.routes.runs.dispatch_run",
            new_callable=AsyncMock,
            return_value=("enqueued", "job-id"),
        ),
    ):
        resp = client.post(
            f"/api/v1/runs/{_RUN_ID}/nodes/manual-node-1/recover",
            json={"input_data": {"answer": 42}},
        )

    assert resp.status_code == 200
    body = resp.json()
    assert body["action"] == "replay"
    assert body["status"] == "running"


def test_recover_node_skip_without_input_data_returns_200(client: TestClient) -> None:
    """Omitted input_data selects skip mode and still resumes the run."""
    run = _make_run(status="running")
    with (
        patch("modulo.api.routes.runs.set_rls_org"),
        patch("modulo.api.routes.runs.recover_node", new_callable=AsyncMock, return_value=run),
        patch(
            "modulo.api.routes.runs.dispatch_run",
            new_callable=AsyncMock,
            return_value=("deduped", "job-id"),
        ),
    ):
        resp = client.post(
            f"/api/v1/runs/{_RUN_ID}/nodes/manual-node-1/recover",
            json={},
        )

    assert resp.status_code == 200
    assert resp.json()["action"] == "skip"


def test_recover_node_enqueue_failed_surfaces_500(client: TestClient) -> None:
    """A persistent enqueue failure in the recover-node path (dispatch_run
    returns ('enqueue_failed', None) after all retries) MUST surface as 500 —
    the run is left pending and would otherwise be re-dispatched by
    dispatcher_reconcile as execute_run with resume_data=None, silently losing
    the operator's replay/skip recovery and any supplied input_data (the run
    would re-execute from scratch instead of resuming at the recovered node)."""
    run = _make_run(status="running")
    with (
        patch("modulo.api.routes.runs.set_rls_org"),
        patch("modulo.api.routes.runs.recover_node", new_callable=AsyncMock, return_value=run),
        patch(
            "modulo.api.routes.runs.dispatch_run",
            new_callable=AsyncMock,
            return_value=("enqueue_failed", None),
        ),
    ):
        resp = client.post(
            f"/api/v1/runs/{_RUN_ID}/nodes/manual-node-1/recover",
            json={"input_data": {"answer": 42}},
        )

    assert resp.status_code == 500
    assert "enqueue" in resp.json()["detail"].lower()


def test_recover_node_capacity_deferred_surfaces_500(client: TestClient) -> None:
    """A capacity-deferred recover-node resume ('deferred') also surfaces as
    500 so the recovery is never silently dropped."""
    run = _make_run(status="running")
    with (
        patch("modulo.api.routes.runs.set_rls_org"),
        patch("modulo.api.routes.runs.recover_node", new_callable=AsyncMock, return_value=run),
        patch(
            "modulo.api.routes.runs.dispatch_run",
            new_callable=AsyncMock,
            return_value=("deferred", None),
        ),
    ):
        resp = client.post(
            f"/api/v1/runs/{_RUN_ID}/nodes/manual-node-1/recover",
            json={},
        )

    assert resp.status_code == 500
    assert "enqueue" in resp.json()["detail"].lower()


# ---------------------------------------------------------------------------
# GET /api/v1/runs/{run_id}/io — normalized split surfaces (FAR-126 P2a)
# ---------------------------------------------------------------------------


def _make_io_run(
    *,
    status: str = "complete",
    run_number: int = 7,
    input_payload: dict[str, Any] | None = None,
    outputs_json: dict[str, Any] | None = None,
    node_telemetry_json: dict[str, Any] | None = None,
) -> MagicMock:
    r = MagicMock()
    r.id = _RUN_ID
    r.run_number = run_number
    r.status = status
    r.input_payload = input_payload
    r.outputs_json = outputs_json
    r.node_telemetry_json = node_telemetry_json
    return r


_LEGACY_SANDBOX_ENVELOPE: dict[str, Any] = {
    "artifacts": [
        {
            "node_id": "planner",
            "status": "completed",
            "output": {
                "status": "completed",
                "summary": "planned the work",
                "output_json": {"plan": "Step 1: analyse", "confidence": 0.9},
                "agent_stdout": "thinking out loud",
                "wall_clock_time_ms": 1200,
            },
        }
    ],
    "output": {"status": "completed", "summary": "planned the work"},
}


class TestGetRunIO:
    """GET /api/v1/runs/{run_id}/io"""

    def test_io_legacy_run_serves_envelope_verbatim(self, client: TestClient) -> None:
        run = _make_io_run(
            input_payload={"prompt": "Hello"},
            outputs_json={"planner": _LEGACY_SANDBOX_ENVELOPE},
            node_telemetry_json=None,
        )

        with (
            patch("modulo.api.routes.runs.get_run", return_value=run),
            patch("modulo.api.routes.runs.set_rls_org"),
        ):
            resp = client.get(f"/api/v1/runs/{_RUN_ID}/io")

        assert resp.status_code == 200
        body = resp.json()
        # Legacy rows keep the mixed envelope byte-identical in outputs_json.
        assert body["outputs_json"]["planner"] == _LEGACY_SANDBOX_ENVELOPE
        # node_telemetry surfaces the inner output envelope (legacy-safe).
        assert body["node_telemetry"]["planner"] == {
            "status": "completed",
            "summary": "planned the work",
        }
        assert isinstance(body["fixture_map"], dict)

    def test_io_new_shape_run_returns_pure_returns_and_telemetry(self, client: TestClient) -> None:
        pure_outputs = {
            "planner": {"plan": "Step 1: analyse", "confidence": 0.9},
            "coder": {"code": "print('hello')"},
        }
        telemetry = {
            "planner": {
                "status": "completed",
                "summary": "planned",
                "agent_stdout": "hello",
                "wall_clock_time_ms": 1200,
            },
            "coder": {"status": "completed", "summary": "coded", "agent_stdout": "log line"},
        }
        run = _make_io_run(
            input_payload={"prompt": "Hello"},
            outputs_json=pure_outputs,
            node_telemetry_json=telemetry,
        )

        with (
            patch("modulo.api.routes.runs.get_run", return_value=run),
            patch("modulo.api.routes.runs.set_rls_org"),
        ):
            resp = client.get(f"/api/v1/runs/{_RUN_ID}/io")

        assert resp.status_code == 200
        body = resp.json()
        assert body["outputs_json"]["planner"] == pure_outputs["planner"]
        assert body["outputs_json"]["coder"] == pure_outputs["coder"]
        assert body["node_telemetry"]["planner"] == telemetry["planner"]
        assert body["node_telemetry"]["coder"] == telemetry["coder"]

    def test_io_masks_both_surfaces(self, client: TestClient) -> None:
        run = _make_io_run(
            input_payload={"prompt": "Hello"},
            outputs_json={"planner": {"api_key": "sk-out-secret", "result": "ok"}},
            node_telemetry_json={
                "planner": {
                    "status": "completed",
                    "summary": "planned",
                    "secrets": {"api_key": "sk-telemetry-secret"},
                    "agent_stdout": "plain log",
                }
            },
        )

        with (
            patch("modulo.api.routes.runs.get_run", return_value=run),
            patch("modulo.api.routes.runs.set_rls_org"),
        ):
            resp = client.get(f"/api/v1/runs/{_RUN_ID}/io")

        assert resp.status_code == 200
        body = resp.json()
        assert body["outputs_json"]["planner"]["api_key"] == SENSITIVE_VALUE_MASK
        assert body["outputs_json"]["planner"]["result"] == "ok"
        telemetry = body["node_telemetry"]["planner"]
        assert telemetry["secrets"]["api_key"] == SENSITIVE_VALUE_MASK
        assert telemetry["agent_stdout"] == "plain log"
