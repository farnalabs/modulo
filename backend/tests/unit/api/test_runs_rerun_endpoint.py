"""Unit tests for POST /api/v1/runs/{run_id}/rerun (FAR-788).

The rerun re-executes a terminal run against its ORIGINAL snapshot with a
server-side copy of the source run's input payload, stamped
``trigger_type='rerun'`` and ``parent_run_id=<source run id>``:

* only terminal source runs are rerunnable (409 otherwise),
* the snapshot is pinned (no fresh snapshot from the live graph),
* the payload is deep-copied server-side (the request body carries nothing),
* the pipeline's trigger rate limit is BYPASSED (operator-initiated recovery,
  not a new trigger),
* lineage lands on the created row (parent_run_id) and in the response.
"""

import uuid
from collections.abc import AsyncGenerator, Generator
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import AsyncSession

from modulo.api.dependencies import _get_engine, _get_session_factory, get_db_session, get_plan_context
from modulo.api.main import app
from modulo.api.routes import runs as runs_module
from modulo.auth.dependencies import get_current_tenant_user, get_current_tenant_user_or_api_key, get_current_user
from modulo.auth.jwt import AuthenticatedPrincipal, TenantPrincipal
from modulo.settings import Settings, get_settings

_VALID_32 = "a" * 32
_ORG_ID = uuid.UUID("00000000-0000-0000-0000-000000000001")
_USER_ID = uuid.UUID("00000000-0000-0000-0000-000000000002")
_SOURCE_RUN_ID = uuid.uuid4()
_PIPELINE_ID = uuid.uuid4()
_SNAPSHOT_ID = uuid.uuid4()
_THREAD_ID = str(uuid.uuid4())


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


def _make_source_run(
    status: str = "complete",
    *,
    input_payload: dict[str, Any] | None = None,
) -> MagicMock:
    r = MagicMock()
    r.id = _SOURCE_RUN_ID
    r.pipeline_id = _PIPELINE_ID
    r.pipeline = None
    r.status = status
    r.langgraph_thread_id = _THREAD_ID
    r.error_detail = None
    r.error_code = None
    r.total_cost_usd = Decimal("0.010000")
    r.total_tokens = 10
    r.node_token_usage = {}
    r.cost_breakdown = None
    r.trigger_type = "manual"
    r.trigger_id = None
    r.account_id = None
    r.heartbeat_at = None
    r.work_item_refs = None
    r.parent_run_id = None
    r.snapshot_id = _SNAPSHOT_ID
    r.input_payload = input_payload if input_payload is not None else {"k": "v"}
    r.run_classification = None
    r.blocked_partial_summary = None
    r.guardrail_summary_json = None
    return r


def _make_rerun_run() -> MagicMock:
    r = MagicMock()
    r.id = uuid.uuid4()
    r.pipeline_id = _PIPELINE_ID
    r.pipeline = None
    r.status = "pending"
    r.langgraph_thread_id = _THREAD_ID
    r.error_detail = None
    r.error_code = None
    r.total_cost_usd = None
    r.total_tokens = None
    r.node_token_usage = None
    r.cost_breakdown = None
    r.trigger_type = "rerun"
    r.trigger_id = None
    r.account_id = _USER_ID
    r.heartbeat_at = None
    r.work_item_refs = None
    r.parent_run_id = _SOURCE_RUN_ID
    r.snapshot_id = _SNAPSHOT_ID
    r.run_classification = None
    r.blocked_partial_summary = None
    r.guardrail_summary_json = None
    r.created_at = datetime.now(UTC)
    r.started_at = None
    r.completed_at = None
    return r


def _make_snapshot() -> MagicMock:
    snapshot = MagicMock()
    snapshot.id = _SNAPSHOT_ID
    snapshot.graph_json = {
        "nodes": [{"id": "node-a", "role": None}],
        "edges": [],
    }
    return snapshot


def _make_mock_session(snapshot: MagicMock | None) -> AsyncMock:
    """Async session whose single in-flow execute serves the snapshot SELECT."""
    session = AsyncMock(spec=AsyncSession)
    begin_cm = AsyncMock()
    begin_cm.__aenter__ = AsyncMock(return_value=None)
    begin_cm.__aexit__ = AsyncMock(return_value=False)
    session.begin = MagicMock(return_value=begin_cm)
    exec_result = MagicMock()
    exec_result.scalar_one_or_none.return_value = snapshot
    session.execute = AsyncMock(return_value=exec_result)
    return session


@pytest.fixture(autouse=True)
def _stub_run_markers(monkeypatch: pytest.MonkeyPatch) -> None:
    """The rerun endpoint derives gate_fired via the FAR-583 repo reader,
    which cannot run against the mocked session — stub the markers read."""

    async def _markers(session: Any, *, run_id: Any, organisation_id: Any = None) -> Any:
        return None

    monkeypatch.setattr(runs_module, "read_run_markers_with_fallback", _markers)


@pytest.fixture
def mock_session() -> AsyncMock:
    return _make_mock_session(_make_snapshot())


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


# ---------------------------------------------------------------------------
# Success paths
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("source_status", ["complete", "failed"])
def test_rerun_completed_and_failed_sources_return_202(client: TestClient, source_status: str) -> None:
    source_run = _make_source_run(status=source_status)
    rerun = _make_rerun_run()

    with (
        patch("modulo.api.routes.runs.get_run", return_value=source_run),
        patch("modulo.api.routes.runs.get_pipeline", return_value=_make_pipeline()),
        patch("modulo.api.routes.runs.create_run", return_value=rerun),
        patch("modulo.api.routes.runs.set_rls_org"),
        patch("modulo.api.routes.runs.set_rls_user_context"),
        patch("modulo.api.routes.runs.dispatch_run", new_callable=AsyncMock) as dispatch,
    ):
        resp = client.post(f"/api/v1/runs/{_SOURCE_RUN_ID}/rerun", json={})

    assert resp.status_code == 202
    body = resp.json()
    assert body["trigger_type"] == "rerun"
    assert body["status"] == "pending"
    assert body["pipeline_id"] == str(_PIPELINE_ID)
    dispatch.assert_awaited_once()
    assert dispatch.await_args.args[0] == str(rerun.id)
    assert dispatch.await_args.args[1] == str(_ORG_ID)
    assert dispatch.await_args.kwargs["queue"] == "runs"


def test_rerun_pins_source_snapshot_and_copies_payload(client: TestClient) -> None:
    source_payload = {"k": "v", "nested": {"a": 1}}
    source_run = _make_source_run(input_payload=source_payload)
    rerun = _make_rerun_run()

    with (
        patch("modulo.api.routes.runs.get_run", return_value=source_run),
        patch("modulo.api.routes.runs.get_pipeline", return_value=_make_pipeline()) as get_pipeline_mock,
        patch("modulo.api.routes.runs.create_run", return_value=rerun) as create_run_mock,
        patch("modulo.api.routes.runs.set_rls_org"),
        patch("modulo.api.routes.runs.set_rls_user_context"),
        patch("modulo.api.routes.runs.dispatch_run", new_callable=AsyncMock),
    ):
        resp = client.post(f"/api/v1/runs/{_SOURCE_RUN_ID}/rerun", json={})

    assert resp.status_code == 202
    kwargs = create_run_mock.await_args.kwargs
    # Snapshot pin: the rerun executes the SOURCE run's snapshot, not a fresh
    # one from the live graph.
    assert kwargs["snapshot_id"] == _SNAPSHOT_ID
    # Lineage + vocabulary.
    assert kwargs["trigger_type"] == "rerun"
    assert kwargs["parent_run_id"] == _SOURCE_RUN_ID
    # FAR-620 attribution: the rerun belongs to the CALLER's account.
    assert kwargs["account_id"] == _USER_ID
    # Server-side payload copy: same VALUE, different object — the request
    # body never carries a payload, so callers cannot smuggle one in.
    assert kwargs["input_payload"] == source_payload
    assert kwargs["input_payload"] is not source_run.input_payload
    # RLS scoping on the pipeline read.
    get_pipeline_mock.assert_awaited_once()
    assert get_pipeline_mock.await_args.kwargs["organisation_id"] == _ORG_ID


def test_rerun_payload_copy_isolated_from_source(client: TestClient) -> None:
    source_run = _make_source_run(input_payload={"k": "v"})
    rerun = _make_rerun_run()

    with (
        patch("modulo.api.routes.runs.get_run", return_value=source_run),
        patch("modulo.api.routes.runs.get_pipeline", return_value=_make_pipeline()),
        patch("modulo.api.routes.runs.create_run", return_value=rerun) as create_run_mock,
        patch("modulo.api.routes.runs.set_rls_org"),
        patch("modulo.api.routes.runs.set_rls_user_context"),
        patch("modulo.api.routes.runs.dispatch_run", new_callable=AsyncMock),
    ):
        resp = client.post(f"/api/v1/runs/{_SOURCE_RUN_ID}/rerun", json={})

    assert resp.status_code == 202
    stored = create_run_mock.await_args.kwargs["input_payload"]
    # Mutating the source's payload AFTER the call must not affect the copy.
    source_run.input_payload["mutated"] = True
    assert stored == {"k": "v"}


def test_rerun_bypasses_pipeline_rate_limit(client: TestClient) -> None:
    """The rate limit that governs NEW triggers must not gate a rerun."""
    pipeline = _make_pipeline()
    pipeline.rate_limit_config = {"max_triggers": 1, "window_seconds": 60}
    source_run = _make_source_run()
    rerun = _make_rerun_run()

    with (
        patch("modulo.api.routes.runs.get_run", return_value=source_run),
        patch("modulo.api.routes.runs.get_pipeline", return_value=pipeline),
        patch("modulo.api.routes.runs.create_run", return_value=rerun),
        patch("modulo.api.routes.runs._enforce_trigger_rate_limit") as rate_limit_mock,
        patch("modulo.api.routes.runs.set_rls_org"),
        patch("modulo.api.routes.runs.set_rls_user_context"),
        patch("modulo.api.routes.runs.dispatch_run", new_callable=AsyncMock),
    ):
        resp = client.post(f"/api/v1/runs/{_SOURCE_RUN_ID}/rerun", json={})

    assert resp.status_code == 202
    rate_limit_mock.assert_not_called()


def test_rerun_response_carries_lineage(client: TestClient) -> None:
    source_run = _make_source_run()
    rerun = _make_rerun_run()

    with (
        patch("modulo.api.routes.runs.get_run", return_value=source_run),
        patch("modulo.api.routes.runs.get_pipeline", return_value=_make_pipeline()),
        patch("modulo.api.routes.runs.create_run", return_value=rerun),
        patch("modulo.api.routes.runs.set_rls_org"),
        patch("modulo.api.routes.runs.set_rls_user_context"),
        patch("modulo.api.routes.runs.dispatch_run", new_callable=AsyncMock),
    ):
        resp = client.post(f"/api/v1/runs/{_SOURCE_RUN_ID}/rerun", json={})

    assert resp.status_code == 202
    body = resp.json()
    assert body["snapshot_id"] == str(_SNAPSHOT_ID)
    assert body["run_id"] == str(rerun.id)


# ---------------------------------------------------------------------------
# Rejection paths
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "non_terminal_status",
    ["pending", "running", "awaiting_human", "claimed", "unknown", "hitl_parked"],
)
def test_rerun_non_terminal_source_returns_409(client: TestClient, non_terminal_status: str) -> None:
    source_run = _make_source_run(status=non_terminal_status)

    with (
        patch("modulo.api.routes.runs.get_run", return_value=source_run),
        patch("modulo.api.routes.runs.create_run") as create_run_mock,
        patch("modulo.api.routes.runs.set_rls_org"),
        patch("modulo.api.routes.runs.set_rls_user_context"),
    ):
        resp = client.post(f"/api/v1/runs/{_SOURCE_RUN_ID}/rerun", json={})

    assert resp.status_code == 409
    assert "terminal" in resp.json()["detail"]
    create_run_mock.assert_not_called()


def test_rerun_missing_source_returns_404(client: TestClient) -> None:
    with (
        patch("modulo.api.routes.runs.get_run", return_value=None),
        patch("modulo.api.routes.runs.set_rls_org"),
        patch("modulo.api.routes.runs.set_rls_user_context"),
    ):
        resp = client.post(f"/api/v1/runs/{_SOURCE_RUN_ID}/rerun", json={})

    assert resp.status_code == 404


def test_rerun_pipeline_missing_returns_404(client: TestClient) -> None:
    source_run = _make_source_run()

    with (
        patch("modulo.api.routes.runs.get_run", return_value=source_run),
        patch("modulo.api.routes.runs.get_pipeline", return_value=None),
        patch("modulo.api.routes.runs.set_rls_org"),
        patch("modulo.api.routes.runs.set_rls_user_context"),
    ):
        resp = client.post(f"/api/v1/runs/{_SOURCE_RUN_ID}/rerun", json={})

    assert resp.status_code == 404


def test_rerun_snapshot_missing_on_source_returns_409(client: TestClient) -> None:
    """Defensive: a legacy/pre-migration source run with no snapshot pin."""
    source_run = _make_source_run()
    source_run.snapshot_id = None

    with (
        patch("modulo.api.routes.runs.get_run", return_value=source_run),
        patch("modulo.api.routes.runs.create_run") as create_run_mock,
        patch("modulo.api.routes.runs.set_rls_org"),
        patch("modulo.api.routes.runs.set_rls_user_context"),
    ):
        resp = client.post(f"/api/v1/runs/{_SOURCE_RUN_ID}/rerun", json={})

    assert resp.status_code == 409
    create_run_mock.assert_not_called()


def test_rerun_snapshot_row_missing_returns_409(client: TestClient, mock_session: AsyncMock) -> None:
    """The source run pins a snapshot_id, but the snapshot row is gone."""
    source_run = _make_source_run()

    exec_result = MagicMock()
    exec_result.scalar_one_or_none.return_value = None
    mock_session.execute = AsyncMock(return_value=exec_result)

    with (
        patch("modulo.api.routes.runs.get_run", return_value=source_run),
        patch("modulo.api.routes.runs.get_pipeline", return_value=_make_pipeline()),
        patch("modulo.api.routes.runs.create_run") as create_run_mock,
        patch("modulo.api.routes.runs.set_rls_org"),
        patch("modulo.api.routes.runs.set_rls_user_context"),
    ):
        resp = client.post(f"/api/v1/runs/{_SOURCE_RUN_ID}/rerun", json={})

    assert resp.status_code == 409
    assert "snapshot" in resp.json()["detail"]
    create_run_mock.assert_not_called()


# ---------------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------------


def test_rerun_unauthenticated_rejected() -> None:
    app.dependency_overrides[get_settings] = _make_settings
    mock_plan = MagicMock()
    mock_plan.feature_enabled.return_value = True
    app.dependency_overrides[get_plan_context] = lambda: mock_plan
    try:
        client = TestClient(app)
        resp = client.post(f"/api/v1/runs/{_SOURCE_RUN_ID}/rerun", json={})
        assert resp.status_code in (401, 403)
    finally:
        app.dependency_overrides.clear()
