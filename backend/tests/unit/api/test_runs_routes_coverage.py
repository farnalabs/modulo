"""Route-level coverage tests for the runs endpoints (FAR-574).

Complements ``test_runs_endpoint.py`` (trigger/detail/cancel/diff/recover/
override contracts), ``test_node_output.py`` (node output masking),
``test_node_observe.py`` and ``test_prompt_reveal.py`` by covering the
stats/heatmap, IO, export-fixture, workspace-lease, workspace-events and
node-output error surfaces plus the route error convention
(ProgrammingErrorâ†'501 / IntegrityErrorâ†'409 / SQLAlchemyErrorâ†'503 /
Exceptionâ†'500) and the remaining helper branches.
"""

from __future__ import annotations

import asyncio
import json
import uuid
from collections.abc import AsyncGenerator, Generator
from contextlib import ExitStack, contextmanager
from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.exc import IntegrityError, ProgrammingError, SQLAlchemyError

from modulo.api.dependencies import _get_engine, _get_session_factory, get_db_session, get_plan_context
from modulo.api.main import app
from modulo.api.routes import runs as runs_module
from modulo.auth.dependencies import get_current_tenant_user, get_current_tenant_user_or_api_key, get_current_user
from modulo.auth.jwt import AuthenticatedPrincipal, TenantPrincipal
from modulo.core.rate_limiter import TokenBucketRegistry
from modulo.settings import Settings, get_settings

_VALID_32 = "a" * 32
_ORG_ID = uuid.UUID("00000000-0000-0000-0000-000000000001")
_USER_ID = uuid.UUID("00000000-0000-0000-0000-000000000002")
_PIPELINE_ID = uuid.uuid4()
_RUN_ID = uuid.uuid4()

_PROG = ProgrammingError("s", {}, Exception())
_SQL = SQLAlchemyError("boom")
_RUNTIME = RuntimeError("kaboom")
_INTEGRITY = IntegrityError("s", {}, Exception())


def _make_settings() -> Settings:
    return Settings(
        database_url="postgresql+asyncpg://localhost/test",
        secret_key=_VALID_32,
        fernet_key=_VALID_32,
        modulo_admin_password="testpass",
        redis_url="redis://localhost:6379/0",
    )


def _make_run(status: str = "complete") -> MagicMock:
    r = MagicMock()
    r.id = _RUN_ID
    r.pipeline_id = _PIPELINE_ID
    r.pipeline = None
    r.status = status
    r.langgraph_thread_id = str(uuid.uuid4())
    r.error_detail = None
    r.error_code = None
    r.total_cost_usd = None
    r.total_tokens = None
    r.node_token_usage = None
    r.cost_breakdown = None
    r.trigger_type = "manual"
    r.trigger_id = None
    r.account_id = None
    r.heartbeat_at = None
    r.work_item_refs = None
    r.parent_run_id = None
    r.snapshot_id = uuid.uuid4()
    r.run_number = 1
    r.created_at = datetime.now(UTC)
    r.started_at = None
    r.completed_at = None
    r.input_payload = {"k": "v"}
    r.outputs_json = {}
    r.node_telemetry_json = None
    r.run_classification = None
    r.blocked_partial_summary = None
    r.guardrail_summary_json = None
    r.raw_output_markers = None
    return r


def _result(
    scalar_one_or_none: object = None,
    scalar: object = None,
    rows: list | None = None,
) -> MagicMock:
    r = MagicMock()
    r.scalar_one_or_none = MagicMock(return_value=scalar_one_or_none)
    r.scalar = MagicMock(return_value=scalar)
    r.scalars.return_value.all = MagicMock(return_value=rows if rows is not None else [])
    r.all = MagicMock(return_value=rows if rows is not None else [])
    return r


def _queue_execute(session: AsyncMock, results: list[MagicMock]) -> None:
    """Route ``session.execute`` through a result queue, minus auth noise.

    The ``require_permission`` kill-switch read on
    ``organisations.authz_enforce`` is answered with an empty result and never
    consumes the queued results.
    """
    authz_result = _result()

    async def _execute(stmt: object, *_args: object, **_kwargs: object) -> MagicMock:
        if "authz_enforce" in str(stmt):
            return authz_result
        if not results:
            raise AssertionError("Unexpected session.execute(): the result queue is exhausted")
        return results.pop(0)

    session.execute = AsyncMock(side_effect=_execute)


class _BeginRaiser:
    def __init__(self, exc: Exception) -> None:
        self._exc = exc

    async def __aenter__(self) -> None:
        raise self._exc

    async def __aexit__(self, *args: object) -> None:
        return None


def _make_session(*, begin_exc: Exception | None = None) -> AsyncMock:
    session = AsyncMock()
    if begin_exc is not None:
        session.begin = MagicMock(return_value=_BeginRaiser(begin_exc))
    else:
        begin_cm = AsyncMock()
        begin_cm.__aenter__ = AsyncMock(return_value=None)
        begin_cm.__aexit__ = AsyncMock(return_value=False)
        session.begin = MagicMock(return_value=begin_cm)
    # Safe default result for incidental reads (kill-switch, claim lookups):
    # no rows, no scalars, nothing found.
    exec_result = MagicMock()
    exec_result.scalar_one_or_none.return_value = None
    exec_result.scalar.return_value = 0
    exec_result.scalars.return_value.all.return_value = []
    exec_result.scalars.return_value.first.return_value = None
    exec_result.all.return_value = []
    exec_result.fetchone.return_value = None
    session.execute = AsyncMock(return_value=exec_result)
    session.refresh = AsyncMock(return_value=None)
    session.add = MagicMock(return_value=None)
    return session


class _MockFactory:
    def __init__(self, session: AsyncMock) -> None:
        self._session = session

    def __call__(self) -> _MockFactory:
        return self

    async def __aenter__(self) -> AsyncMock:
        return self._session

    async def __aexit__(self, *args: object) -> None:
        pass


def _install_overrides(session: AsyncMock, *, org_role: str = "admin") -> None:
    async def override_session() -> AsyncGenerator[AsyncMock, None]:
        yield session

    app.dependency_overrides[get_settings] = _make_settings
    app.dependency_overrides[get_db_session] = override_session
    app.dependency_overrides[_get_engine] = lambda: MagicMock()
    app.dependency_overrides[_get_session_factory] = lambda: _MockFactory(session)
    app.dependency_overrides[get_current_user] = lambda: AuthenticatedPrincipal(
        username=f"{org_role}@test",
        organisation_id=_ORG_ID,
        account_id=_USER_ID,
        org_role=org_role,
    )
    app.dependency_overrides[get_current_tenant_user] = lambda: TenantPrincipal(
        username=f"{org_role}@test",
        organisation_id=_ORG_ID,
        account_id=_USER_ID,
        org_role=org_role,
    )
    app.dependency_overrides[get_current_tenant_user_or_api_key] = lambda: TenantPrincipal(
        username=f"{org_role}@test",
        organisation_id=_ORG_ID,
        account_id=_USER_ID,
        org_role=org_role,
    )
    mock_plan = MagicMock()
    mock_plan.feature_enabled.return_value = True
    app.dependency_overrides[get_plan_context] = lambda: mock_plan


@pytest.fixture(autouse=True)
def _patch_route_rls():
    with (
        patch("modulo.api.routes.runs.set_rls_org", new_callable=AsyncMock),
        patch("modulo.api.routes.runs.set_rls_user_context", new_callable=AsyncMock),
    ):
        yield


@pytest.fixture
def client() -> Generator[tuple[TestClient, AsyncMock], None, None]:
    session = _make_session()
    _install_overrides(session)
    yield TestClient(app), session
    app.dependency_overrides.clear()


@pytest.fixture
def runner_client() -> Generator[TestClient, None, None]:
    """A runner principal â€” denied by the observe/recover/override role gates."""
    session = _make_session()
    _install_overrides(session, org_role="runner")
    yield TestClient(app)
    app.dependency_overrides.clear()


@contextmanager
def _fresh_client(begin_exc: Exception | None = None) -> Generator[TestClient, None, None]:
    session = _make_session(begin_exc=begin_exc)
    _install_overrides(session)
    yield TestClient(app)
    app.dependency_overrides.clear()


# ---------------------------------------------------------------------------
# GET /runs/stats + /runs/stats/heatmap
# ---------------------------------------------------------------------------


def test_run_stats_returns_crud_payload(client: tuple[TestClient, AsyncMock]) -> None:
    http, _session = client
    stats = {"total_runs": 3, "by_status": {"complete": 3}}
    with patch("modulo.api.routes.runs.get_run_stats", new_callable=AsyncMock, return_value=stats):
        resp = http.get("/api/v1/runs/stats", params={"period": "30d"})

    assert resp.status_code == 200, resp.text
    assert resp.json() == stats


def test_run_stats_rejects_bad_period(client: tuple[TestClient, AsyncMock]) -> None:
    http, _session = client

    resp = http.get("/api/v1/runs/stats", params={"period": "60d"})

    assert resp.status_code == 422


@pytest.mark.parametrize(("exc", "expected"), [(_PROG, 501), (_SQL, 503), (_RUNTIME, 500)])
def test_run_stats_error_mapping(exc: Exception, expected: int) -> None:
    with (
        _fresh_client() as http,
        patch("modulo.api.routes.runs.get_run_stats", new_callable=AsyncMock, side_effect=exc),
    ):
        resp = http.get("/api/v1/runs/stats")

    assert resp.status_code == expected, resp.text


def test_run_heatmap_returns_crud_payload(client: tuple[TestClient, AsyncMock]) -> None:
    http, _session = client
    heatmap = [{"date": "2026-09-01", "count": 2}]
    with patch("modulo.api.routes.runs.get_run_heatmap", new_callable=AsyncMock, return_value=heatmap):
        resp = http.get("/api/v1/runs/stats/heatmap", params={"year": 2026})

    assert resp.status_code == 200, resp.text
    assert resp.json() == heatmap


@pytest.mark.parametrize(("exc", "expected"), [(_PROG, 501), (_SQL, 503), (_RUNTIME, 500)])
def test_run_heatmap_error_mapping(exc: Exception, expected: int) -> None:
    with (
        _fresh_client() as http,
        patch("modulo.api.routes.runs.get_run_heatmap", new_callable=AsyncMock, side_effect=exc),
    ):
        resp = http.get("/api/v1/runs/stats/heatmap")

    assert resp.status_code == expected, resp.text


# ---------------------------------------------------------------------------
# GET /runs/{run_id}/io â€” 404 + error mapping
# ---------------------------------------------------------------------------


def test_run_io_unknown_run_returns_404(client: tuple[TestClient, AsyncMock]) -> None:
    http, _session = client
    with patch("modulo.api.routes.runs.get_run", new_callable=AsyncMock, return_value=None):
        resp = http.get(f"/api/v1/runs/{_RUN_ID}/io")

    assert resp.status_code == 404
    assert resp.json()["detail"] == "Run not found"


@pytest.mark.parametrize(("exc", "expected"), [(_PROG, 501), (_INTEGRITY, 409), (_SQL, 503), (_RUNTIME, 500)])
def test_run_io_error_mapping(exc: Exception, expected: int) -> None:
    with _fresh_client() as http, patch("modulo.api.routes.runs.get_run", new_callable=AsyncMock, side_effect=exc):
        resp = http.get(f"/api/v1/runs/{_RUN_ID}/io")

    assert resp.status_code == expected, resp.text


def test_run_io_happy_path_normalizes_and_masks(client: tuple[TestClient, AsyncMock]) -> None:
    http, _session = client
    run = _make_run()
    snapshot = MagicMock()
    snapshot.graph_json = {"nodes": [{"id": "n1", "label": "Node One"}]}
    with (
        patch("modulo.api.routes.runs.get_run", new_callable=AsyncMock, return_value=run),
        patch("modulo.api.routes.runs._load_snapshot_for_run", new_callable=AsyncMock, return_value=snapshot),
    ):
        resp = http.get(f"/api/v1/runs/{_RUN_ID}/io")

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["run_id"] == str(_RUN_ID)
    assert body["node_labels"] == {"n1": "Node One"}
    assert "fixture_map" in body


# ---------------------------------------------------------------------------
# GET /runs/{run_id}/export-fixture
# ---------------------------------------------------------------------------


def test_export_fixture_returns_fixture_payload(client: tuple[TestClient, AsyncMock]) -> None:
    http, _session = client
    run = _make_run()
    run.outputs_json = {"n1": "node output"}
    snapshot = MagicMock()
    snapshot.graph_json = {"nodes": [{"id": "n1", "label": "Node One"}]}
    with (
        patch("modulo.api.routes.runs.get_run", new_callable=AsyncMock, return_value=run),
        patch("modulo.api.routes.runs._load_snapshot_for_run", new_callable=AsyncMock, return_value=snapshot),
    ):
        resp = http.get(f"/api/v1/runs/{_RUN_ID}/export-fixture")

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["fixture_name"] == f"run_{str(_RUN_ID)[:8]}_io"
    assert body["pipeline_id"] == str(_PIPELINE_ID)
    assert body["snapshot_graph_json"] == {"nodes": [{"id": "n1", "label": "Node One"}]}
    assert body["fixture_map"]


def test_export_fixture_unknown_run_returns_404(client: tuple[TestClient, AsyncMock]) -> None:
    http, _session = client
    with patch("modulo.api.routes.runs.get_run", new_callable=AsyncMock, return_value=None):
        resp = http.get(f"/api/v1/runs/{_RUN_ID}/export-fixture")

    assert resp.status_code == 404


@pytest.mark.parametrize(("exc", "expected"), [(_PROG, 501), (_INTEGRITY, 409), (_SQL, 503), (_RUNTIME, 500)])
def test_export_fixture_error_mapping(exc: Exception, expected: int) -> None:
    with _fresh_client() as http, patch("modulo.api.routes.runs.get_run", new_callable=AsyncMock, side_effect=exc):
        resp = http.get(f"/api/v1/runs/{_RUN_ID}/export-fixture")

    assert resp.status_code == expected, resp.text


# ---------------------------------------------------------------------------
# GET /runs/{run_id}/workspace-lease
# ---------------------------------------------------------------------------


def _lease_row() -> MagicMock:
    lease = MagicMock()
    lease.id = uuid.uuid4()
    lease.organisation_id = _ORG_ID
    lease.environment_profile_id = uuid.uuid4()
    lease.run_id = _RUN_ID
    lease.provider_ref = "e2b-123"
    lease.status = "active"
    lease.lease_started_at = datetime.now(UTC)
    lease.lease_expires_at = None
    lease.resource_usage_json = {"cpu_s": 1.5}
    return lease


def test_workspace_lease_returns_lease(client: tuple[TestClient, AsyncMock]) -> None:
    http, session = client
    _queue_execute(session, [_result(scalar_one_or_none=_lease_row())])

    resp = http.get(f"/api/v1/runs/{_RUN_ID}/workspace-lease")

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["provider_ref"] == "e2b-123"
    assert body["status"] == "active"
    assert body["resource_usage"] == {"cpu_s": 1.5}


def test_workspace_lease_absent_returns_null(client: tuple[TestClient, AsyncMock]) -> None:
    http, session = client
    _queue_execute(session, [_result(scalar_one_or_none=None)])

    resp = http.get(f"/api/v1/runs/{_RUN_ID}/workspace-lease")

    assert resp.status_code == 200, resp.text
    assert resp.json() is None


@pytest.mark.parametrize(("exc", "expected"), [(_PROG, 501), (_INTEGRITY, 409), (_SQL, 503), (_RUNTIME, 500)])
def test_workspace_lease_error_mapping(exc: Exception, expected: int) -> None:
    with _fresh_client(begin_exc=exc) as http:
        resp = http.get(f"/api/v1/runs/{_RUN_ID}/workspace-lease")

    assert resp.status_code == expected, resp.text


# ---------------------------------------------------------------------------
# GET /runs/{run_id}/workspace-events
# ---------------------------------------------------------------------------


def _event_row(event_type: str = "workspace_provisioned") -> MagicMock:
    evt = MagicMock()
    evt.event_type = event_type
    evt.payload_json = {"detail": "sandbox ready"}
    evt.created_at = datetime.now(UTC)
    return evt


def test_workspace_events_returns_timeline(client: tuple[TestClient, AsyncMock]) -> None:
    http, session = client
    _queue_execute(session, [_result(rows=[_event_row()])])

    resp = http.get(f"/api/v1/runs/{_RUN_ID}/workspace-events")

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert len(body) == 1
    assert body[0]["event"] == "provisioned"
    assert body[0]["detail"] == "sandbox ready"
    assert body[0]["timestamp"]


def test_workspace_events_empty_returns_empty_list(client: tuple[TestClient, AsyncMock]) -> None:
    http, session = client
    _queue_execute(session, [_result(rows=[])])

    resp = http.get(f"/api/v1/runs/{_RUN_ID}/workspace-events")

    assert resp.status_code == 200
    assert not resp.json()


@pytest.mark.parametrize(("exc", "expected"), [(_PROG, 501), (_INTEGRITY, 409), (_SQL, 503), (_RUNTIME, 500)])
def test_workspace_events_error_mapping(exc: Exception, expected: int) -> None:
    with _fresh_client(begin_exc=exc) as http:
        resp = http.get(f"/api/v1/runs/{_RUN_ID}/workspace-events")

    assert resp.status_code == expected, resp.text


# ---------------------------------------------------------------------------
# GET /runs/{run_id}/nodes/{node_id}/output â€” error mapping
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(("exc", "expected"), [(_PROG, 501), (_INTEGRITY, 409), (_SQL, 503), (_RUNTIME, 500)])
def test_node_output_error_mapping(exc: Exception, expected: int) -> None:
    with _fresh_client() as http, patch("modulo.api.routes.runs.get_run", new_callable=AsyncMock, side_effect=exc):
        resp = http.get(f"/api/v1/runs/{_RUN_ID}/nodes/n1/output")

    assert resp.status_code == expected, resp.text


# ---------------------------------------------------------------------------
# POST /runs/{run_id}/nodes/{node_id}/observe â€” error mapping (both blocks)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(("exc", "expected"), [(_PROG, 501), (_INTEGRITY, 409), (_SQL, 503), (_RUNTIME, 500)])
def test_observe_run_lookup_error_mapping(exc: Exception, expected: int) -> None:
    with _fresh_client() as http, patch("modulo.api.routes.runs.get_run", new_callable=AsyncMock, side_effect=exc):
        resp = http.post(f"/api/v1/runs/{_RUN_ID}/nodes/n1/observe")

    assert resp.status_code == expected, resp.text


@pytest.mark.parametrize(("exc", "expected"), [(_PROG, 501), (_INTEGRITY, 409), (_SQL, 503), (_RUNTIME, 500)])
def test_observe_write_error_mapping(exc: Exception, expected: int) -> None:
    with (
        _fresh_client() as http,
        patch("modulo.api.routes.runs.get_run", new_callable=AsyncMock, return_value=_make_run()),
        patch("modulo.api.routes.runs.observe_node", new_callable=AsyncMock, side_effect=exc),
    ):
        resp = http.post(f"/api/v1/runs/{_RUN_ID}/nodes/n1/observe")

    assert resp.status_code == expected, resp.text


# ---------------------------------------------------------------------------
# POST /runs/{run_id}/nodes/{node_id}/recover â€” role gate + error mapping
# ---------------------------------------------------------------------------


def test_recover_node_denied_for_operator(runner_client: TestClient) -> None:
    resp = runner_client.post(
        f"/api/v1/runs/{_RUN_ID}/nodes/n1/recover",
        json={"input_data": {"x": 1}},
    )

    assert resp.status_code == 403
    assert "Only operators and admins" in resp.json()["detail"]


def _recover_patches(exc: Exception) -> list:
    return [
        patch("modulo.api.routes.runs.get_run", new_callable=AsyncMock, return_value=_make_run()),
        patch("modulo.api.routes.runs.recover_node", new_callable=AsyncMock, side_effect=exc),
        patch(
            "modulo.api.routes.runs.dispatch_run",
            new_callable=AsyncMock,
            return_value=("enqueued", "job-id"),
        ),
    ]


def _enter_all(patches: list) -> object:
    stack = ExitStack()
    for p in patches:
        stack.enter_context(p)
    return stack


@pytest.mark.parametrize(
    ("exc", "expected"),
    [
        (IntegrityError("s", {}, Exception()), 409),
        (_PROG, 501),
        (_SQL, 503),
        (_RUNTIME, 500),
    ],
)
def test_recover_node_error_mapping(exc: Exception, expected: int) -> None:
    with _fresh_client() as http, _enter_all(_recover_patches(exc)):
        resp = http.post(f"/api/v1/runs/{_RUN_ID}/nodes/n1/recover", json={"input_data": None})

    assert resp.status_code == expected, resp.text


def test_recover_node_dispatch_failure_maps_to_500() -> None:
    with (
        _fresh_client() as http,
        patch("modulo.api.routes.runs.get_run", new_callable=AsyncMock, return_value=_make_run()),
        patch("modulo.api.routes.runs.recover_node", new_callable=AsyncMock, return_value=_make_run()),
        patch("modulo.api.routes.runs.dispatch_run", new_callable=AsyncMock, side_effect=RuntimeError("queue down")),
    ):
        resp = http.post(f"/api/v1/runs/{_RUN_ID}/nodes/n1/recover", json={"input_data": None})

    assert resp.status_code == 500, resp.text


# ---------------------------------------------------------------------------
# POST /runs/{run_id}/guardrail-override â€” role gate + error mapping
# ---------------------------------------------------------------------------


def _override_patches(exc: Exception | None = None) -> list:
    run = _make_run(status="pending")
    patches = [
        patch.object(
            runs_module,
            "_guardrail_override_rate_limiter",
            TokenBucketRegistry(rate=10 / 60.0, burst=10),
        ),
    ]
    if exc is not None:
        patches.append(patch("modulo.api.routes.runs.guardrail_override", new_callable=AsyncMock, side_effect=exc))
    else:
        patches.append(patch("modulo.api.routes.runs.guardrail_override", new_callable=AsyncMock, return_value=run))
    patches.append(
        patch(
            "modulo.api.routes.runs.dispatch_run",
            new_callable=AsyncMock,
            return_value=("enqueued", "job-id"),
        )
    )
    return patches


def test_guardrail_override_denied_for_operator(runner_client: TestClient) -> None:
    with _enter_all(_override_patches()):
        resp = runner_client.post(
            f"/api/v1/runs/{_RUN_ID}/guardrail-override",
            json={"input_data": {"x": 1}},
        )

    assert resp.status_code == 403
    assert "Only operators and admins" in resp.json()["detail"]


@pytest.mark.parametrize(("exc", "expected"), [(_INTEGRITY, 409), (_PROG, 501), (_SQL, 503), (_RUNTIME, 500)])
def test_guardrail_override_error_mapping(exc: Exception, expected: int) -> None:
    with _fresh_client() as http, _enter_all(_override_patches(exc=exc)):
        resp = http.post(f"/api/v1/runs/{_RUN_ID}/guardrail-override", json={"input_data": {"x": 1}})

    assert resp.status_code == expected, resp.text


def test_guardrail_override_dispatch_failure_maps_to_500() -> None:
    with (
        _fresh_client() as http,
        patch.object(
            runs_module,
            "_guardrail_override_rate_limiter",
            TokenBucketRegistry(rate=10 / 60.0, burst=10),
        ),
        patch("modulo.api.routes.runs.guardrail_override", new_callable=AsyncMock, return_value=_make_run()),
        patch("modulo.api.routes.runs.dispatch_run", new_callable=AsyncMock, side_effect=RuntimeError("queue down")),
    ):
        resp = http.post(f"/api/v1/runs/{_RUN_ID}/guardrail-override", json={"input_data": {"x": 1}})

    assert resp.status_code == 500, resp.text


# ---------------------------------------------------------------------------
# GET /runs (list) â€” error mapping via _do_list_runs
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(("exc", "expected"), [(_INTEGRITY, 409), (_PROG, 501), (_SQL, 503), (_RUNTIME, 500)])
def test_list_runs_error_mapping(exc: Exception, expected: int) -> None:
    with (
        _fresh_client() as http,
        patch("modulo.api.routes.runs._do_list_runs", new_callable=AsyncMock, side_effect=exc),
    ):
        resp = http.get("/api/v1/runs")

    assert resp.status_code == expected, resp.text


# ---------------------------------------------------------------------------
# GET /runs/{run_id} â€” error mapping via _do_get_run
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(("exc", "expected"), [(_INTEGRITY, 409), (_PROG, 501), (_SQL, 503), (_RUNTIME, 500)])
def test_get_run_error_mapping(exc: Exception, expected: int) -> None:
    with _fresh_client() as http, patch("modulo.api.routes.runs._do_get_run", new_callable=AsyncMock, side_effect=exc):
        resp = http.get(f"/api/v1/runs/{_RUN_ID}")

    assert resp.status_code == expected, resp.text


# ---------------------------------------------------------------------------
# POST /runs/{run_id}/cancel â€” error mapping via _cancel_run
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(("exc", "expected"), [(_INTEGRITY, 409), (_PROG, 501), (_SQL, 503), (_RUNTIME, 500)])
def test_cancel_run_error_mapping(exc: Exception, expected: int) -> None:
    with _fresh_client() as http, patch("modulo.api.routes.runs._cancel_run", new_callable=AsyncMock, side_effect=exc):
        resp = http.post(f"/api/v1/runs/{_RUN_ID}/cancel")

    assert resp.status_code == expected, resp.text


# ---------------------------------------------------------------------------
# POST /runs (trigger) â€” error mapping via _create_manual_run
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(("exc", "expected"), [(_INTEGRITY, 409), (_PROG, 501), (_SQL, 503), (_RUNTIME, 500)])
def test_trigger_run_error_mapping(exc: Exception, expected: int) -> None:
    with (
        _fresh_client() as http,
        patch("modulo.api.routes.runs._create_manual_run", new_callable=AsyncMock, side_effect=exc),
        patch("modulo.api.routes.runs.dispatch_run", new_callable=AsyncMock, return_value=("enqueued", "job")),
    ):
        resp = http.post("/api/v1/runs", json={"pipeline_id": str(_PIPELINE_ID), "input_payload": {}})

    assert resp.status_code == expected, resp.text


# ---------------------------------------------------------------------------
# POST /runs/diff â€” error mapping + run-B 404
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(("exc", "expected"), [(_INTEGRITY, 409), (_PROG, 501), (_SQL, 503), (_RUNTIME, 500)])
def test_diff_error_mapping(exc: Exception, expected: int) -> None:
    with _fresh_client() as http, patch("modulo.api.routes.runs.get_run", new_callable=AsyncMock, side_effect=exc):
        resp = http.post(
            "/api/v1/runs/diff",
            json={
                "run_id_a": str(_RUN_ID),
                "node_id_a": "n1",
                "run_id_b": str(uuid.uuid4()),
                "node_id_b": "n1",
            },
        )

    assert resp.status_code == expected, resp.text


def test_diff_run_b_missing_returns_404(client: tuple[TestClient, AsyncMock]) -> None:
    http, _session = client

    def _get_run_side_effect(_session: object, run_id: object, **_kw: object) -> object:
        return _make_run() if run_id == _RUN_ID else None

    with patch("modulo.api.routes.runs.get_run", new_callable=AsyncMock, side_effect=_get_run_side_effect):
        resp = http.post(
            "/api/v1/runs/diff",
            json={
                "run_id_a": str(_RUN_ID),
                "node_id_a": "n1",
                "run_id_b": str(uuid.uuid4()),
                "node_id_b": "n1",
            },
        )

    assert resp.status_code == 404
    assert "not found" in resp.json()["detail"]


# ---------------------------------------------------------------------------
# POST .../prompt/reveal â€” error mapping
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(("exc", "expected"), [(_INTEGRITY, 409), (_PROG, 501), (_SQL, 503), (_RUNTIME, 500)])
def test_reveal_error_mapping(exc: Exception, expected: int) -> None:
    with _fresh_client() as http, patch("modulo.api.routes.runs.get_run", new_callable=AsyncMock, side_effect=exc):
        resp = http.post(f"/api/v1/runs/{_RUN_ID}/nodes/n1/prompt/reveal")

    assert resp.status_code == expected, resp.text


def test_reveal_snapshot_load_error_maps_to_503(client: tuple[TestClient, AsyncMock]) -> None:
    http, _session = client
    with (
        patch("modulo.api.routes.runs.get_run", new_callable=AsyncMock, return_value=_make_run()),
        patch(
            "modulo.api.routes.runs._load_snapshot_for_run",
            new_callable=AsyncMock,
            side_effect=SQLAlchemyError("boom"),
        ),
    ):
        resp = http.post(f"/api/v1/runs/{_RUN_ID}/nodes/n1/prompt/reveal")

    assert resp.status_code == 503, resp.text


# ---------------------------------------------------------------------------
# Helper units â€” branches not reachable through the endpoint happy paths
# ---------------------------------------------------------------------------


def _factory_for(session: AsyncMock) -> _MockFactory:
    return _MockFactory(session)


async def test_do_get_run_returns_run_and_raises_run_not_found() -> None:
    session = _make_session()
    run = _make_run()
    _queue_execute(session, [_result(scalar_one_or_none=run)])
    principal = TenantPrincipal(username="t", organisation_id=_ORG_ID, account_id=_USER_ID, org_role="admin")
    resolved = await runs_module._do_get_run(_factory_for(session), principal, _RUN_ID)
    assert resolved is run

    session2 = _make_session()
    _queue_execute(session2, [_result(scalar_one_or_none=None)])
    with pytest.raises(runs_module.RunNotFoundError):
        await runs_module._do_get_run(_factory_for(session2), principal, _RUN_ID)


async def test_do_get_otel_endpoint_degrades_to_empty_on_failure() -> None:
    session = _make_session(begin_exc=SQLAlchemyError("boom"))
    endpoint = await runs_module._do_get_otel_endpoint(_factory_for(session), _ORG_ID)
    assert endpoint == ""


def test_select_trigger_actor_paths() -> None:
    run = _make_run()
    run.trigger_type = "manual"
    run.account_id = _USER_ID
    run.trigger_id = None
    # Manual run with a known account label.
    assert runs_module._select_trigger_actor(run, {_USER_ID: "ops@example.com"}, {}) == "ops@example.com"
    # Trigger-driven run with a trigger label.
    trigger_id = uuid.uuid4()
    run.trigger_type = "cron"
    run.account_id = None
    run.trigger_id = trigger_id
    assert runs_module._select_trigger_actor(run, {}, {trigger_id: "cron"}) == "cron"
    # No account, no trigger â†' None.
    run.trigger_id = None
    assert runs_module._select_trigger_actor(run, {}, {}) is None


async def test_resolve_trigger_actor_queries_labels() -> None:
    session = _make_session()
    account_row = MagicMock()
    account_row.email = "ops@example.com"
    account_row.display_name = "Ops"
    trigger_row = MagicMock()
    trigger_row.trigger_type = "cron"
    run = _make_run()
    run.account_id = _USER_ID
    run.trigger_id = uuid.uuid4()
    _queue_execute(
        session,
        [
            _result(scalar_one_or_none=account_row),
            _result(scalar_one_or_none=trigger_row),
        ],
    )
    actor = await runs_module._resolve_trigger_actor(session, run)
    assert actor == "ops@example.com"


async def test_resolve_child_runs_builds_child_entries() -> None:
    session = _make_session()
    child = MagicMock()
    child.id = uuid.uuid4()
    child.run_number = 7
    child.status = "complete"
    row = (child, "Child Pipeline")
    result = MagicMock()
    result.all = MagicMock(return_value=[row])
    session.execute = AsyncMock(return_value=result)
    run = _make_run()
    children = await runs_module._resolve_child_runs(session, run)
    assert children == [
        {
            "run_id": str(child.id),
            "run_number": 7,
            "status": "complete",
            "pipeline_name": "Child Pipeline",
        }
    ]


async def test_load_account_and_trigger_labels() -> None:
    session = _make_session()
    account = MagicMock()
    account.id = _USER_ID
    account.email = "ops@example.com"
    account.display_name = "Ops"
    account_result = MagicMock()
    account_result.scalars.return_value.all = MagicMock(return_value=[account])
    trigger = MagicMock()
    trigger.id = uuid.uuid4()
    trigger.trigger_type = "webhook"
    trigger_result = MagicMock()
    trigger_result.scalars.return_value.all = MagicMock(return_value=[trigger])
    _queue_execute(session, [account_result, trigger_result])
    run = _make_run()
    run.account_id = _USER_ID
    run.trigger_id = trigger.id

    labels = await runs_module._load_account_labels(session, [run])
    assert labels == {_USER_ID: "ops@example.com"}
    trigger_labels = await runs_module._load_trigger_labels(session, [run])
    assert trigger_labels == {trigger.id: "webhook"}

    # No ids â†' no queries.
    run.account_id = None
    run.trigger_id = None
    assert not await runs_module._load_account_labels(session, [run])
    assert not await runs_module._load_trigger_labels(session, [run])


def test_clamp_node_token_usage_union_branches() -> None:
    # Non-dict node entries pass through untouched.
    assert runs_module._clamp_node_token_usage_union({"n": "scalar"})["n"] == "scalar"
    # A finite cost within the clamp is preserved.
    clamped = runs_module._clamp_node_token_usage_union({"n": {"model_cost_raw_usd": "1.5"}})
    assert clamped["n"]["model_cost_raw_usd"] == 1.5
    # A hostile magnitude is clamped to 1e6.
    huge = runs_module._clamp_node_token_usage_union({"n": {"model_cost_raw_usd": "99999999999"}})
    assert huge["n"]["model_cost_raw_usd"] == 1000000.0
    # A non-numeric cost cannot be parsed and stays verbatim (stored union
    # holds the raw value; the display copy is never corrupted).
    junk = runs_module._clamp_node_token_usage_union({"n": {"model_cost_raw_usd": "abc"}})
    assert junk["n"]["model_cost_raw_usd"] == "abc"


def test_serialize_node_token_usage_truncates_beyond_bound() -> None:
    ntu = {f"node-{i}": {"tokens": i} for i in range(201)}
    serialized = runs_module._serialize_node_token_usage(ntu)
    assert serialized is not None
    assert serialized["node_count"] == 201
    assert "node-0" not in serialized
    assert "node-200" in serialized
    # At or below the bound, everything is kept verbatim.
    small = runs_module._serialize_node_token_usage({"a": {"tokens": 1}})
    assert small == {"a": {"tokens": 1}}
    assert runs_module._serialize_node_token_usage(None) is None


def test_resolve_trace_display_without_thread() -> None:
    run = _make_run()
    run.langgraph_thread_id = ""
    assert runs_module._resolve_trace_display(run, "https://otel") == (None, None)


def test_find_entry_candidates_rejects_empty_graph() -> None:
    from fastapi import HTTPException

    with pytest.raises(HTTPException, match="no nodes"):
        runs_module._find_entry_candidates({})


async def test_require_valid_entry_agent_rejects_missing_agent() -> None:
    from fastapi import HTTPException

    session = _make_session()
    _queue_execute(session, [_result(scalar_one_or_none=None)])
    entry_node = {"id": "n1", "agent_id": str(uuid.uuid4())}
    with pytest.raises(HTTPException, match="not found"):
        await runs_module._require_valid_entry_agent(session, entry_node)


async def test_validate_run_input_basics_rejects_non_dict_payload() -> None:
    from fastapi import HTTPException

    # The entry node must carry an agent_id â€” the input check is only reached
    # when entry-agent validation has run (the agent lookup succeeds here).
    session = _make_session()
    _queue_execute(session, [_result(scalar_one_or_none=MagicMock())])
    graph = {"nodes": [{"id": "n1", "agent_id": str(uuid.uuid4())}], "edges": []}
    with pytest.raises(HTTPException, match="JSON object"):
        await runs_module._validate_run_input_basics(session, graph, MagicMock(), "not-a-dict")


async def test_enforce_trigger_rate_limit_paths() -> None:
    session = _make_session()
    pipeline = MagicMock()
    pipeline.id = _PIPELINE_ID
    pipeline.rate_limit_config = None
    # No rate limit configured â†' None without querying.
    assert await runs_module._enforce_trigger_rate_limit(session, pipeline, {}) is None

    pipeline.rate_limit_config = {"max_triggers": 1, "window_seconds": 60}
    with (
        patch(
            "modulo.core.trigger_engine.TriggerEngine._compute_rate_limit_key",
            MagicMock(return_value="rl-key"),
        ),
        patch(
            "modulo.core.trigger_engine.TriggerEngine._count_recent_rate_limited",
            new_callable=AsyncMock,
            return_value=0,
        ),
    ):
        assert await runs_module._enforce_trigger_rate_limit(session, pipeline, {}) == "rl-key"

    with (
        patch(
            "modulo.core.trigger_engine.TriggerEngine._compute_rate_limit_key",
            MagicMock(return_value="rl-key"),
        ),
        patch(
            "modulo.core.trigger_engine.TriggerEngine._count_recent_rate_limited",
            new_callable=AsyncMock,
            return_value=1,
        ),
    ):
        from fastapi import HTTPException

        with pytest.raises(HTTPException, match="Rate limit exceeded"):
            await runs_module._enforce_trigger_rate_limit(session, pipeline, {})


async def test_create_manual_run_missing_snapshot_maps_to_404() -> None:
    from fastapi import HTTPException

    session = _make_session()
    principal = TenantPrincipal(username="t", organisation_id=_ORG_ID, account_id=_USER_ID, org_role="admin")
    req = runs_module.TriggerRunRequest(pipeline_id=_PIPELINE_ID, input_payload={})
    with (
        patch("modulo.api.routes.runs.get_pipeline", new_callable=AsyncMock, return_value=MagicMock()),
        patch(
            "modulo.api.routes.runs.create_snapshot_from_live_graph",
            new_callable=AsyncMock,
            return_value=None,
        ),
        pytest.raises(HTTPException, match="not found"),
    ):
        await runs_module._create_manual_run(session, principal, req)


def test_build_fixture_map_per_node_shape() -> None:
    outputs = {"n1": {"input": {"a": 1}, "output": "out-1"}}
    fixture = runs_module._build_fixture_map({"a": 1}, outputs)
    assert fixture == {"{'a': 1}": "out-1"}


async def test_load_snapshot_for_run_returns_none_without_snapshot() -> None:
    session = _make_session()
    run = _make_run()
    run.snapshot_id = None
    assert await runs_module._load_snapshot_for_run(session, run) is None
    assert await runs_module._load_snapshot_for_run(session, None) is None


def test_normalize_run_and_telemetry_empty_inputs() -> None:
    assert runs_module._normalize_run_outputs(None, None) is None
    assert runs_module._normalize_node_telemetry(None, None) is None


def test_decrypt_checkpoint_dict_envelope_round_trip() -> None:
    import base64

    from cryptography.fernet import Fernet

    fernet_key = base64.urlsafe_b64encode(b"k" * 32).decode()
    f = Fernet(fernet_key.encode())
    payload = {"channel_values": {"node": "state"}}
    token = f.encrypt(json.dumps(payload).encode())
    encrypted = {"__encrypted__": True, "data": token.decode()}
    assert runs_module._decrypt_checkpoint(encrypted, fernet_key) == payload
    # A malformed encrypted dict degrades to the raw payload.
    broken = {"__encrypted__": True, "data": "not-a-token"}
    assert runs_module._decrypt_checkpoint(broken, fernet_key) == broken


async def test_get_checkpoint_state_returns_none_for_non_dict_checkpoint() -> None:
    session = _make_session()
    result = MagicMock()
    result.fetchone = MagicMock(return_value=("not-json", 1))
    session.execute = AsyncMock(return_value=result)
    state = await runs_module._get_checkpoint_state(session, "thread", _ORG_ID, _VALID_32)
    assert state is None


def test_build_messages_prefers_string_user_input() -> None:
    agent = MagicMock()
    agent.prompt_template = "be helpful"
    messages = runs_module._build_messages(
        agent,
        runs_module._MessageContext(
            input_payload={"k": "v"},
            outputs_json=None,
            checkpoint_state={"run_context": {"input": "plain text"}},
            node_id="n1",
        ),
    )
    assert messages[0] == {"role": "system", "content": "be helpful"}
    assert {"role": "user", "content": "plain text"} in messages


async def test_load_reveal_agent_rejects_unknown_node() -> None:
    from fastapi import HTTPException

    session = _make_session()
    graph = {"nodes": [{"id": "other"}]}
    with pytest.raises(HTTPException, match="not found in pipeline graph"):
        await runs_module._load_reveal_agent(session, graph, "missing-node")


def test_run_with_retry_is_transparent() -> None:
    async def ok() -> str:
        return "value"

    assert asyncio.run(runs_module._run_with_retry(ok)) == "value"
