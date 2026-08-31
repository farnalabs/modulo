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
from modulo.api.routes import runs as runs_module
from modulo.api.routes.runs import RunNotFoundError, _validate_run_input_basics
from modulo.auth.dependencies import get_current_tenant_user, get_current_tenant_user_or_api_key, get_current_user
from modulo.auth.jwt import AuthenticatedPrincipal, TenantPrincipal
from modulo.core.exceptions import OrgDeletedError, RateLimitConflictError
from modulo.core.pipeline_engine.recovery import (
    GuardrailOverrideError,
    GuardrailOverrideRejectedError,
    GuardrailOverrideRequiredError,
)
from modulo.core.rate_limiter import TokenBucketRegistry
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
    # Active-run observability attributes (FAR-307)
    r.trigger_type = "manual"
    r.trigger_id = None
    r.account_id = None
    r.heartbeat_at = None
    r.work_item_refs = None
    r.parent_run_id = None
    # FAR-490 run→snapshot linkage
    r.snapshot_id = None
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
# POST /api/v1/runs — rate-limit conflict (migration 0117 / #1105)
# ---------------------------------------------------------------------------


def test_trigger_run_rate_limit_conflict_returns_429(client: TestClient) -> None:
    """create_run raising RateLimitConflictError must surface as HTTP 429."""
    pipeline = _make_pipeline()

    with (
        patch("modulo.api.routes.runs.get_pipeline", return_value=pipeline),
        patch(
            "modulo.api.routes.runs.create_snapshot_from_live_graph",
            return_value=_make_snapshot(),
        ),
        patch(
            "modulo.api.routes.runs.create_run",
            side_effect=RateLimitConflictError(pipeline_id=_PIPELINE_ID, rate_limit_key="key:abc"),
        ),
        patch("modulo.api.routes.runs.set_rls_org"),
    ):
        resp = client.post(
            "/api/v1/runs",
            json={"pipeline_id": str(_PIPELINE_ID), "input_payload": {"k": "v"}},
        )

    assert resp.status_code == 429
    assert resp.json()["detail"] == "Rate limit exceeded for this pipeline"


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


def test_get_run_exposes_blocked_partial_summary(client: TestClient) -> None:
    """FAR-213: a guardrail-blocked run's blocked_partial summary is surfaced on
    the run-detail response (executed nodes + per-node publish status)."""
    summary = {
        "blocked": True,
        "blocking_eval_name": "no-secrets",
        "executed_nodes": ["node_a"],
        "nodes": [
            {
                "node_id": "node_a",
                "publish_status": "compensated",
                "output_ref": {"run_id": str(_RUN_ID), "node_id": "node_a"},
                "compensation": {"outcome": "compensated", "reason": "closed", "resource_id": "42"},
            }
        ],
    }
    run = _make_run(status="eval_failed", error_code="eval_blocked")
    run.blocked_partial_summary = summary

    with (
        patch("modulo.api.routes.runs._do_get_run", return_value=run),
        patch("modulo.api.routes.runs.set_rls_org"),
    ):
        resp = client.get(f"/api/v1/runs/{_RUN_ID}")

    assert resp.status_code == 200
    body = resp.json()
    assert body["blocked_partial_summary"] == summary
    assert body["blocked_partial_summary"]["nodes"][0]["publish_status"] == "compensated"


def test_get_run_blocked_partial_summary_non_dict_returns_null(client: TestClient) -> None:
    """FAR-213 defensive coercion: a corrupt (non-dict) column value is surfaced
    as null, never a 500."""
    run = _make_run(status="eval_failed", error_code="eval_blocked")
    run.blocked_partial_summary = "not-a-dict"

    with (
        patch("modulo.api.routes.runs._do_get_run", return_value=run),
        patch("modulo.api.routes.runs.set_rls_org"),
    ):
        resp = client.get(f"/api/v1/runs/{_RUN_ID}")

    assert resp.status_code == 200
    assert resp.json()["blocked_partial_summary"] is None


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


def test_cancel_run_budget_exceeded_returns_409(client: TestClient) -> None:
    run = _make_run(status="budget_exceeded")

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
    assert body["error_code"] == "harness.unknown"


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


def test_run_response_gate_fired_false_for_plain_complete(client: TestClient) -> None:
    """FAR-228: a plain complete run has gate_fired False and run_classification
    surfaced as stored."""
    run = _make_run(status="complete", error_detail=None, error_code=None)
    run.raw_output_markers = {}
    run.run_classification = {"value": "no_delivery", "reason": "no_work"}
    with (
        patch("modulo.api.routes.runs._do_get_run", return_value=run),
        patch("modulo.api.routes.runs.set_rls_org"),
    ):
        resp = client.get(f"/api/v1/runs/{_RUN_ID}")

    body = resp.json()
    assert body["gate_fired"] is False
    assert body["run_classification"]["reason"] == "no_work"


def test_run_response_populates_guardrail_summary(client: TestClient) -> None:
    """FAR-223 item 11: the guardrail_summary snapshot is surfaced on run detail."""
    run = _make_run(status="eval_failed")
    run.guardrail_summary_json = {
        "bound": 1,
        "evaluated": 1,
        "passed": 0,
        "violated": 1,
        "observed": 0,
        "errored": 0,
        "redacted": 0,
        "skipped": 0,
        "expected_skips": 0,
        "unexpected_skips": 0,
    }
    with (
        patch("modulo.api.routes.runs._do_get_run", return_value=run),
        patch("modulo.api.routes.runs.set_rls_org"),
    ):
        resp = client.get(f"/api/v1/runs/{_RUN_ID}")

    body = resp.json()
    assert body["guardrail_summary"] == {
        "bound": 1,
        "evaluated": 1,
        "passed": 0,
        "violated": 1,
        "observed": 0,
        "errored": 0,
        "redacted": 0,
        "skipped": 0,
        "expected_skips": 0,
        "unexpected_skips": 0,
    }


def test_run_response_guardrail_summary_none_when_absent(client: TestClient) -> None:
    """Runs without guardrail interception (or pre-migration) expose None."""
    run = _make_run(status="complete")
    run.guardrail_summary_json = None
    with (
        patch("modulo.api.routes.runs._do_get_run", return_value=run),
        patch("modulo.api.routes.runs.set_rls_org"),
    ):
        resp = client.get(f"/api/v1/runs/{_RUN_ID}")

    assert resp.json()["guardrail_summary"] is None


def test_run_response_guardrail_summary_malformed_is_none(client: TestClient) -> None:
    """A corrupt JSON value degrades to None, never a 500."""
    run = _make_run(status="complete")
    run.guardrail_summary_json = {"bound": "not-an-int"}
    with (
        patch("modulo.api.routes.runs._do_get_run", return_value=run),
        patch("modulo.api.routes.runs.set_rls_org"),
    ):
        resp = client.get(f"/api/v1/runs/{_RUN_ID}")

    assert resp.status_code == 200
    assert resp.json()["guardrail_summary"] is None


def test_run_response_gate_fired_true_on_idempotency_gate_code(client: TestClient) -> None:
    """FAR-228: a guard-B suppressed run (error_code harness.idempotency_gate)
    exposes gate_fired True."""
    run = _make_run(status="complete", error_code="harness.idempotency_gate")
    run.raw_output_markers = {}
    run.run_classification = None
    with (
        patch("modulo.api.routes.runs._do_get_run", return_value=run),
        patch("modulo.api.routes.runs.set_rls_org"),
    ):
        resp = client.get(f"/api/v1/runs/{_RUN_ID}")

    assert resp.json()["gate_fired"] is True


def test_run_response_gate_fired_true_on_raw_idempotency_gate_code(client: TestClient) -> None:
    """FAR-228 review fix: the DB stores the RAW spelling (``idempotency_gate``)
    for legacy guard-B rows — ``_run_gate_fired`` must route the read through
    ``map_legacy_code`` so they are not missed."""
    run = _make_run(status="complete", error_code="idempotency_gate")
    run.raw_output_markers = {}
    run.run_classification = None
    with (
        patch("modulo.api.routes.runs._do_get_run", return_value=run),
        patch("modulo.api.routes.runs.set_rls_org"),
    ):
        resp = client.get(f"/api/v1/runs/{_RUN_ID}")

    assert resp.json()["gate_fired"] is True


def test_run_response_gate_fired_true_on_email_classification(client: TestClient) -> None:
    """FAR-228: a run classified email_delivered exposes gate_fired True — this
    makes guard-A completions (error_code None) API-distinguishable."""
    run = _make_run(status="complete", error_code=None)
    run.raw_output_markers = {}
    run.run_classification = {"value": "delivered", "reason": "email_delivered"}
    with (
        patch("modulo.api.routes.runs._do_get_run", return_value=run),
        patch("modulo.api.routes.runs.set_rls_org"),
    ):
        resp = client.get(f"/api/v1/runs/{_RUN_ID}")

    assert resp.json()["gate_fired"] is True


def test_run_response_gate_fired_true_on_marker_delivery_done(client: TestClient) -> None:
    """FAR-228: a run whose raw-output marker carries delivery_done exposes
    gate_fired True even before classification runs."""
    run = _make_run(status="complete", error_code=None)
    run.run_classification = None
    run.raw_output_markers = {
        "run:run-1:node:n1:1": {
            "_modulo_marker": True,
            "delivery_done": True,
            "attempt_key": "run:run-1:node:n1:1",
        }
    }
    with (
        patch("modulo.api.routes.runs._do_get_run", return_value=run),
        patch("modulo.api.routes.runs.set_rls_org"),
    ):
        resp = client.get(f"/api/v1/runs/{_RUN_ID}")

    assert resp.json()["gate_fired"] is True


def test_run_response_gate_fired_true_on_success_path_marker(client: TestClient) -> None:
    """FAR-228 review fix: a delivery_done marker written on the SUCCESS path
    (status ``completed``, empty parse_error — the shape the node persists)
    drives gate_fired True via the shared marker scan, so a successful sentinel
    run is API-distinguishable without waiting for classification."""
    run = _make_run(status="complete", error_code=None)
    run.run_classification = None
    run.raw_output_markers = {
        "run:run-1:node:n1:1": {
            "_modulo_marker": True,
            "status": "completed",
            "summary": "Sandbox agent completed with delivery sentinel observed (idempotency gate)",
            "parse_error": "",
            "pr_url": "",
            "exit_code": 0,
            "attempt_key": "run:run-1:node:n1:1",
            "node_id": "n1",
            "delivery_done": True,
        }
    }
    with (
        patch("modulo.api.routes.runs._do_get_run", return_value=run),
        patch("modulo.api.routes.runs.set_rls_org"),
    ):
        resp = client.get(f"/api/v1/runs/{_RUN_ID}")

    assert resp.json()["gate_fired"] is True


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
    assert len(trace_id) == 32  # 32-hex OTel trace id (FAR-198)
    assert trace_id == uuid.uuid5(uuid.UUID("6ba7b810-9dad-11d1-80b4-00c04fd430c8"), _THREAD_ID).hex


def test_run_response_trace_id_deterministic(client: TestClient) -> None:
    run = _make_run(status="complete")
    with (
        patch("modulo.api.routes.runs._do_get_run", return_value=run),
        patch("modulo.api.routes.runs.set_rls_org"),
    ):
        resp1 = client.get(f"/api/v1/runs/{_RUN_ID}")
        resp2 = client.get(f"/api/v1/runs/{_RUN_ID}")

    assert resp1.json()["trace_id"] == resp2.json()["trace_id"]


def test_run_response_trace_url_populated_when_endpoint_configured(client: TestClient) -> None:
    run = _make_run(status="complete")
    with (
        patch("modulo.api.routes.runs._do_get_run", return_value=run),
        patch(
            "modulo.api.routes.runs._do_get_otel_endpoint",
            new_callable=AsyncMock,
            return_value="https://otel.example.com",
        ),
        patch("modulo.api.routes.runs.set_rls_org"),
    ):
        resp = client.get(f"/api/v1/runs/{_RUN_ID}")

    body = resp.json()
    assert body["trace_id"]
    assert body["trace_url"] == f"https://otel.example.com/jaeger/ui/trace/{body['trace_id']}"


def test_run_response_trace_url_absent_without_endpoint(client: TestClient) -> None:
    run = _make_run(status="complete")
    with (
        patch("modulo.api.routes.runs._do_get_run", return_value=run),
        patch("modulo.api.routes.runs._do_get_otel_endpoint", new_callable=AsyncMock, return_value=""),
        patch("modulo.api.routes.runs.set_rls_org"),
    ):
        resp = client.get(f"/api/v1/runs/{_RUN_ID}")

    assert resp.json()["trace_url"] is None


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
    assert item["error_code"] == "harness.worker_failed"
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


def test_list_runs_includes_masked_input_payload(client: TestClient) -> None:
    run_id = uuid.uuid4()
    run = _make_listable_run(run_id)
    run.input_payload = {"task": "fix bug", "api_key": "sk-secret"}

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
    assert item["input_payload"]["task"] == "fix bug"
    assert item["input_payload"]["api_key"] == SENSITIVE_VALUE_MASK


def test_list_runs_input_payload_none_when_absent(client: TestClient) -> None:
    run_id = uuid.uuid4()
    run = _make_listable_run(run_id)
    run.input_payload = None

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
    assert resp.json()["items"][0]["input_payload"] is None


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


def test_recover_node_refuses_guardrail_blocked_run(client: TestClient) -> None:
    """A guardrail-blocked run (eval_failed / eval_blocked) must NOT be
    resurrected through the generic recover endpoint — it 409s and directs the
    caller to the guardrail-override endpoint (MAJOR-1)."""
    with (
        patch("modulo.api.routes.runs.set_rls_org"),
        patch(
            "modulo.api.routes.runs.recover_node",
            new_callable=AsyncMock,
            side_effect=GuardrailOverrideRequiredError(_RUN_ID),
        ),
    ):
        resp = client.post(
            f"/api/v1/runs/{_RUN_ID}/nodes/manual-node-1/recover",
            json={"input_data": {"review": "approved"}},
        )

    assert resp.status_code == 409
    assert "guardrail-override" in resp.json()["detail"].lower()


# ---------------------------------------------------------------------------
# POST /api/v1/runs/{run_id}/guardrail-override — FAR-208 remediation
# ---------------------------------------------------------------------------


def test_guardrail_override_clean_input_dispatches_execute_run(client: TestClient) -> None:
    """A clean-input guardrail override flips the run to pending and
    re-dispatches it from run start (execute_run, no resume data)."""
    run = _make_run(status="pending")
    with (
        patch("modulo.api.routes.runs.set_rls_org"),
        patch("modulo.api.routes.runs.guardrail_override", new_callable=AsyncMock, return_value=run),
        patch(
            "modulo.api.routes.runs.dispatch_run",
            new_callable=AsyncMock,
            return_value=("enqueued", "job-id"),
        ) as mock_dispatch,
    ):
        resp = client.post(
            f"/api/v1/runs/{_RUN_ID}/guardrail-override",
            json={"input_data": {"body": "clean replacement text"}},
        )

    assert resp.status_code == 200
    body = resp.json()
    assert body["action"] == "override"
    assert body["status"] == "pending"
    mock_dispatch.assert_awaited_once()
    # execute_run from run start — NOT resume_run. job_type is left at the
    # dispatch_run default (execute_run) and no resume_data is passed.
    assert mock_dispatch.await_args.kwargs.get("job_type", "execute_run") == "execute_run"
    assert mock_dispatch.await_args.kwargs.get("resume_data") is None


def test_guardrail_override_still_violating_input_rejected_422(client: TestClient) -> None:
    """A still-violating supplied input is refused (422, re-block safe default)
    and never dispatched."""
    with (
        patch("modulo.api.routes.runs.set_rls_org"),
        patch(
            "modulo.api.routes.runs.guardrail_override",
            new_callable=AsyncMock,
            side_effect=GuardrailOverrideRejectedError(_RUN_ID, "no-secrets", "still has SECRET_ABC12345"),
        ),
        patch("modulo.api.routes.runs.dispatch_run", new_callable=AsyncMock) as mock_dispatch,
    ):
        resp = client.post(
            f"/api/v1/runs/{_RUN_ID}/guardrail-override",
            json={"input_data": {"body": "still has SECRET_ABC12345"}},
        )

    assert resp.status_code == 422
    assert "no-secrets" in resp.json()["detail"]
    mock_dispatch.assert_not_called()


def test_guardrail_override_non_guardrail_run_rejected_409(client: TestClient) -> None:
    """An override against a run that is NOT guardrail-blocked terminal 409s."""
    with (
        patch("modulo.api.routes.runs.set_rls_org"),
        patch(
            "modulo.api.routes.runs.guardrail_override",
            new_callable=AsyncMock,
            side_effect=GuardrailOverrideError(_RUN_ID, "status='failed' error_code='agent.failed'"),
        ),
        patch("modulo.api.routes.runs.dispatch_run", new_callable=AsyncMock) as mock_dispatch,
    ):
        resp = client.post(
            f"/api/v1/runs/{_RUN_ID}/guardrail-override",
            json={"input_data": {"body": "clean"}},
        )

    assert resp.status_code == 409
    mock_dispatch.assert_not_called()


def test_guardrail_override_dispatch_deferred_surfaces_500(client: TestClient) -> None:
    """A deferred/enqueue-failed dispatch after override surfaces as 500 so the
    recovery is never silently dropped."""
    run = _make_run(status="pending")
    with (
        patch("modulo.api.routes.runs.set_rls_org"),
        patch("modulo.api.routes.runs.guardrail_override", new_callable=AsyncMock, return_value=run),
        patch(
            "modulo.api.routes.runs.dispatch_run",
            new_callable=AsyncMock,
            return_value=("enqueue_failed", None),
        ),
    ):
        resp = client.post(
            f"/api/v1/runs/{_RUN_ID}/guardrail-override",
            json={"input_data": {"body": "clean"}},
        )

    assert resp.status_code == 500
    assert "enqueue" in resp.json()["detail"].lower()


# ---------------------------------------------------------------------------
# POST /api/v1/runs/{run_id}/guardrail-override — rate limit (FAR-223 PR C)
# ---------------------------------------------------------------------------


def _patch_override_rate_limiter(burst: int) -> patch:
    """Replace the endpoint's module-level limiter with a fresh one.

    The default burst is 10; each test overrides with a small deterministic
    burst so the N+1th-request-exceeds behaviour is provable without sharing
    token state across tests.
    """
    return patch.object(
        runs_module,
        "_guardrail_override_rate_limiter",
        TokenBucketRegistry(rate=burst / 60.0, burst=burst),
    )


def test_guardrail_override_within_rate_limit_succeeds(client: TestClient) -> None:
    """Overrides up to the per-window limit succeed (200)."""
    run = _make_run(status="pending")
    with (
        _patch_override_rate_limiter(burst=3),
        patch("modulo.api.routes.runs.set_rls_org"),
        patch("modulo.api.routes.runs.guardrail_override", new_callable=AsyncMock, return_value=run),
        patch(
            "modulo.api.routes.runs.dispatch_run",
            new_callable=AsyncMock,
            return_value=("enqueued", "job-id"),
        ),
    ):
        for _ in range(3):
            resp = client.post(
                f"/api/v1/runs/{_RUN_ID}/guardrail-override",
                json={"input_data": {"body": "clean replacement text"}},
            )
            assert resp.status_code == 200


def test_guardrail_override_rate_limit_fires_429(client: TestClient) -> None:
    """The (burst + 1)th override in the window is refused with 429."""
    run = _make_run(status="pending")
    with (
        _patch_override_rate_limiter(burst=2),
        patch("modulo.api.routes.runs.set_rls_org"),
        patch("modulo.api.routes.runs.guardrail_override", new_callable=AsyncMock, return_value=run),
        patch(
            "modulo.api.routes.runs.dispatch_run",
            new_callable=AsyncMock,
            return_value=("enqueued", "job-id"),
        ),
    ):
        assert (
            client.post(
                f"/api/v1/runs/{_RUN_ID}/guardrail-override",
                json={"input_data": {"body": "clean"}},
            ).status_code
            == 200
        )
        assert (
            client.post(
                f"/api/v1/runs/{_RUN_ID}/guardrail-override",
                json={"input_data": {"body": "clean"}},
            ).status_code
            == 200
        )
        resp = client.post(
            f"/api/v1/runs/{_RUN_ID}/guardrail-override",
            json={"input_data": {"body": "clean"}},
        )
        assert resp.status_code == 429
        assert "try again" in resp.json()["detail"].lower()


def test_guardrail_override_429_prevents_dispatch(client: TestClient) -> None:
    """An exceeding override is refused before dispatch_run is ever called."""
    run = _make_run(status="pending")
    with (
        _patch_override_rate_limiter(burst=1),
        patch("modulo.api.routes.runs.set_rls_org"),
        patch("modulo.api.routes.runs.guardrail_override", new_callable=AsyncMock, return_value=run),
        patch(
            "modulo.api.routes.runs.dispatch_run",
            new_callable=AsyncMock,
            return_value=("enqueued", "job-id"),
        ) as mock_dispatch,
    ):
        assert (
            client.post(
                f"/api/v1/runs/{_RUN_ID}/guardrail-override",
                json={"input_data": {"body": "clean"}},
            ).status_code
            == 200
        )
        assert (
            client.post(
                f"/api/v1/runs/{_RUN_ID}/guardrail-override",
                json={"input_data": {"body": "clean"}},
            ).status_code
            == 429
        )
        assert mock_dispatch.await_count == 1


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
            input_payload={"task": "fix bug", "api_key": "sk-secret"},
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
        assert body["input_payload"]["task"] == "fix bug"
        assert body["input_payload"]["api_key"] == SENSITIVE_VALUE_MASK
        assert body["outputs_json"]["planner"]["api_key"] == SENSITIVE_VALUE_MASK
        assert body["outputs_json"]["planner"]["result"] == "ok"
        telemetry = body["node_telemetry"]["planner"]
        assert telemetry["secrets"]["api_key"] == SENSITIVE_VALUE_MASK
        assert telemetry["agent_stdout"] == "plain log"

    def test_io_masks_input_payload_sensitive_keys(self, client: TestClient) -> None:
        run = _make_io_run(
            input_payload={"prompt": "Hello", "api_key": "sk-input-secret"},
            outputs_json={"planner": {"result": "ok"}},
            node_telemetry_json=None,
        )

        with (
            patch("modulo.api.routes.runs.get_run", return_value=run),
            patch("modulo.api.routes.runs.set_rls_org"),
        ):
            resp = client.get(f"/api/v1/runs/{_RUN_ID}/io")

        assert resp.status_code == 200
        body = resp.json()
        assert body["input_payload"]["prompt"] == "Hello"
        assert body["input_payload"]["api_key"] == SENSITIVE_VALUE_MASK

    def test_io_populates_node_labels_from_snapshot_graph(self, client: TestClient, mock_session: AsyncMock) -> None:
        run = _make_io_run(
            input_payload={"prompt": "Hello"},
            outputs_json={"planner": {"result": "ok"}},
            node_telemetry_json=None,
        )
        run.snapshot_id = _SNAPSHOT_ID

        snapshot = MagicMock()
        snapshot.graph_json = {
            "nodes": [
                {"id": "planner", "label": "Planner Agent"},
                {"id": "coder", "node_type": "sandbox_agent"},
                {"id": "unlabeled"},
            ],
            "edges": [],
        }
        exec_result = MagicMock()
        exec_result.scalar_one_or_none.return_value = snapshot
        mock_session.execute.return_value = exec_result

        with (
            patch("modulo.api.routes.runs.get_run", return_value=run),
            patch("modulo.api.routes.runs.set_rls_org"),
        ):
            resp = client.get(f"/api/v1/runs/{_RUN_ID}/io")

        assert resp.status_code == 200
        body = resp.json()
        assert body["node_labels"] == {
            "planner": "Planner Agent",
            "coder": "sandbox_agent",
            "unlabeled": "unlabeled",
        }

    def test_io_node_labels_empty_without_snapshot(self, client: TestClient, mock_session: AsyncMock) -> None:
        run = _make_io_run(
            input_payload={"prompt": "Hello"},
            outputs_json={"planner": {"result": "ok"}},
            node_telemetry_json=None,
        )

        with (
            patch("modulo.api.routes.runs.get_run", return_value=run),
            patch("modulo.api.routes.runs.set_rls_org"),
        ):
            resp = client.get(f"/api/v1/runs/{_RUN_ID}/io")

        assert resp.status_code == 200
        assert not resp.json()["node_labels"]


# ---------------------------------------------------------------------------
# Active-run observability (FAR-307)
# ---------------------------------------------------------------------------


def _make_run_event(seq: int, event_type: str, payload: dict[str, Any], ts: datetime) -> MagicMock:
    evt = MagicMock()
    evt.seq = seq
    evt.event_type = event_type
    evt.payload = payload
    evt.timestamp = ts
    return evt


class TestGetRunObservability:
    """GET /api/v1/runs/{run_id} — active-run observability fields."""

    def test_detail_exposes_observability_fields(self, client: TestClient) -> None:
        run = _make_run(status="running")
        run.heartbeat_at = datetime.now(UTC)
        run.work_item_refs = [{"kind": "pr", "ref": "https://github.com/x/y/pull/1", "source": "github"}]
        run.trigger_id = uuid.uuid4()
        capacity = {"active_runs": 2, "concurrency_limit": 5, "waiting": False}
        child_runs = [{"run_id": str(uuid.uuid4()), "run_number": 2, "status": "complete", "pipeline_name": "P"}]

        with (
            patch("modulo.api.routes.runs._do_get_run", return_value=run),
            patch("modulo.api.routes.runs._do_get_child_run_rollup", return_value=(Decimal(0), 0)),
            patch("modulo.api.routes.runs._do_get_otel_endpoint", return_value=""),
            patch(
                "modulo.api.routes.runs._do_get_run_observability",
                return_value=("duncan@modulo.run", capacity, child_runs),
            ),
            patch("modulo.api.routes.runs.set_rls_org"),
        ):
            resp = client.get(f"/api/v1/runs/{_RUN_ID}")

        assert resp.status_code == 200
        body = resp.json()
        assert body["trigger_actor"] == "duncan@modulo.run"
        assert body["trigger_type"] == "manual"
        assert body["trigger_id"] == str(run.trigger_id)
        assert body["heartbeat_at"] == run.heartbeat_at.astimezone(UTC).isoformat().replace("+00:00", "Z")
        assert body["work_item_refs"] == run.work_item_refs
        assert body["child_runs"] == child_runs
        assert body["capacity"] == capacity

    def test_detail_observability_none_when_absent(self, client: TestClient) -> None:
        run = _make_run(status="running")
        run.heartbeat_at = None
        run.work_item_refs = None
        run.trigger_id = None

        with (
            patch("modulo.api.routes.runs._do_get_run", return_value=run),
            patch("modulo.api.routes.runs._do_get_child_run_rollup", return_value=(Decimal(0), 0)),
            patch("modulo.api.routes.runs._do_get_otel_endpoint", return_value=""),
            patch("modulo.api.routes.runs._do_get_run_observability", return_value=(None, None, None)),
            patch("modulo.api.routes.runs.set_rls_org"),
        ):
            resp = client.get(f"/api/v1/runs/{_RUN_ID}")

        assert resp.status_code == 200
        body = resp.json()
        assert body["trigger_actor"] is None
        assert body["trigger_type"] == "manual"
        assert body["trigger_id"] is None
        assert body["heartbeat_at"] is None
        assert body["work_item_refs"] is None
        assert body["child_runs"] is None
        assert body["capacity"] is None

    def test_detail_exposes_snapshot_id(self, client: TestClient) -> None:
        """FAR-490: the run→snapshot link is on the detail body."""
        run = _make_run(status="complete")
        run.snapshot_id = uuid.uuid4()

        with (
            patch("modulo.api.routes.runs._do_get_run", return_value=run),
            patch("modulo.api.routes.runs._do_get_child_run_rollup", return_value=(Decimal(0), 0)),
            patch("modulo.api.routes.runs._do_get_otel_endpoint", return_value=""),
            patch("modulo.api.routes.runs._do_get_run_observability", return_value=(None, None, None)),
            patch("modulo.api.routes.runs.set_rls_org"),
        ):
            resp = client.get(f"/api/v1/runs/{_RUN_ID}")

        assert resp.status_code == 200
        assert resp.json()["snapshot_id"] == str(run.snapshot_id)


class TestResolveCapacityAdmissionGate:
    """``_resolve_capacity`` must mirror the dispatch admission-gate semantics.

    Regression for the review finding: the capacity count included pending
    runs (``ACTIVE_RUN_STATUSES`` contains ``pending``) and the viewed run
    itself, so a pending run with room available (limit=3, two running) was
    reported as ``waiting=True`` even though dispatch would admit it at once.
    """

    async def test_pending_run_not_waiting_when_room_is_available(self, mock_session: AsyncMock) -> None:
        run = _make_run(status="pending")
        count = AsyncMock(return_value=2)
        with (
            patch("modulo.api.routes.runs.count_active_runs_for_org", count),
            patch("modulo.api.routes.runs.get_org_run_concurrency_limit", AsyncMock(return_value=3)),
        ):
            capacity = await runs_module._resolve_capacity(mock_session, _ORG_ID, run)

        assert capacity["waiting"] is False
        count.assert_awaited_once_with(mock_session, _ORG_ID, include_pending=False, exclude_run_id=run.id)

    async def test_pending_run_waiting_when_at_limit(self, mock_session: AsyncMock) -> None:
        run = _make_run(status="pending")
        with (
            patch("modulo.api.routes.runs.count_active_runs_for_org", AsyncMock(return_value=3)),
            patch("modulo.api.routes.runs.get_org_run_concurrency_limit", AsyncMock(return_value=3)),
        ):
            capacity = await runs_module._resolve_capacity(mock_session, _ORG_ID, run)

        assert capacity["active_runs"] == 3
        assert capacity["waiting"] is True

    async def test_running_run_never_waiting(self, mock_session: AsyncMock) -> None:
        run = _make_run(status="running")
        with (
            patch("modulo.api.routes.runs.count_active_runs_for_org", AsyncMock(return_value=3)),
            patch("modulo.api.routes.runs.get_org_run_concurrency_limit", AsyncMock(return_value=3)),
        ):
            capacity = await runs_module._resolve_capacity(mock_session, _ORG_ID, run)

        assert capacity["waiting"] is False


class TestGetRunEventsLifecycle:
    """GET /api/v1/runs/{run_id}/events — node lifecycle events surfaced."""

    def test_events_include_lifecycle_events(self, client: TestClient) -> None:
        run = _make_run(status="running")
        ts = datetime.now(UTC)
        events = [
            _make_run_event(1, "node.stdout_chunk", {"node_id": "a", "text": "hi"}, ts),
            _make_run_event(2, "node_started", {"node_id": "a"}, ts),
            _make_run_event(3, "node_completed", {"node_id": "a"}, ts),
            _make_run_event(4, "node_failed", {"node_id": "b", "error": "boom"}, ts),
        ]
        broker = MagicMock()
        broker.replay_since.return_value = events
        registry = MagicMock()
        registry.get.return_value = broker

        with (
            patch("modulo.api.routes.runs._do_get_run", return_value=run),
            patch("modulo.api.routes.runs.get_registry", return_value=registry),
            patch("modulo.api.routes.runs.set_rls_org"),
        ):
            resp = client.get(f"/api/v1/runs/{_RUN_ID}/events")

        assert resp.status_code == 200
        body = resp.json()
        types = [e["event_type"] for e in body["events"]]
        assert "node_started" in types
        assert "node_completed" in types
        assert "node_failed" in types
        assert "node.stdout_chunk" in types
        assert len(body["events"]) == 4

    def test_events_filter_by_node_id(self, client: TestClient) -> None:
        run = _make_run(status="running")
        ts = datetime.now(UTC)
        events = [
            _make_run_event(1, "node_started", {"node_id": "a"}, ts),
            _make_run_event(2, "node_started", {"node_id": "b"}, ts),
        ]
        broker = MagicMock()
        broker.replay_since.return_value = events
        registry = MagicMock()
        registry.get.return_value = broker

        with (
            patch("modulo.api.routes.runs._do_get_run", return_value=run),
            patch("modulo.api.routes.runs.get_registry", return_value=registry),
            patch("modulo.api.routes.runs.set_rls_org"),
        ):
            resp = client.get(f"/api/v1/runs/{_RUN_ID}/events?node_id=a")

        assert resp.status_code == 200
        body = resp.json()
        assert [e["event_type"] for e in body["events"]] == ["node_started"]
        assert all(e["payload"]["node_id"] == "a" for e in body["events"])


class TestListRunsObservability:
    """GET /api/v1/runs — active-run observability fields on each item."""

    def test_list_items_include_observability_fields(self, client: TestClient, mock_session) -> None:
        run = _make_run(status="running")
        run.heartbeat_at = datetime.now(UTC)

        page = MagicMock()
        page.items = [run]
        page.total = 1
        page.page = 1
        page.page_size = 20
        page.next_cursor = None
        page.has_more = False

        with (
            patch("modulo.api.routes.runs.db_list_runs", return_value=page),
            patch("modulo.api.routes.runs.get_child_run_rollup", return_value={}),
            patch("modulo.api.routes.runs.count_active_runs_for_org", AsyncMock(return_value=0)),
            patch("modulo.api.routes.runs.get_org_run_concurrency_limit", AsyncMock(return_value=5)),
            patch("modulo.api.routes.runs.set_rls_org"),
        ):
            resp = client.get("/api/v1/runs")

        assert resp.status_code == 200
        body = resp.json()
        assert len(body["items"]) == 1
        item = body["items"][0]
        assert item["heartbeat_at"] == run.heartbeat_at.isoformat()
        assert "trigger_actor" in item
        assert item["capacity"] == {
            "active_runs": 0,
            "concurrency_limit": 5,
            "waiting": False,
        }

    def test_list_pending_run_not_waiting_when_room_is_available(self, client: TestClient, mock_session) -> None:
        run = _make_run(status="pending")
        page = MagicMock()
        page.items = [run]
        page.total = 1
        page.page = 1
        page.page_size = 20
        page.next_cursor = None
        page.has_more = False

        with (
            patch("modulo.api.routes.runs.db_list_runs", return_value=page),
            patch("modulo.api.routes.runs.get_child_run_rollup", return_value={}),
            patch("modulo.api.routes.runs.count_active_runs_for_org", AsyncMock(return_value=2)),
            patch("modulo.api.routes.runs.get_org_run_concurrency_limit", AsyncMock(return_value=3)),
            patch("modulo.api.routes.runs.set_rls_org"),
        ):
            resp = client.get("/api/v1/runs")

        assert resp.status_code == 200
        item = resp.json()["items"][0]
        assert item["status"] == "pending"
        assert item["capacity"] == {
            "active_runs": 2,
            "concurrency_limit": 3,
            "waiting": False,
        }

    def test_list_pending_run_waiting_when_at_limit(self, client: TestClient, mock_session) -> None:
        run = _make_run(status="pending")
        page = MagicMock()
        page.items = [run]
        page.total = 1
        page.page = 1
        page.page_size = 20
        page.next_cursor = None
        page.has_more = False

        with (
            patch("modulo.api.routes.runs.db_list_runs", return_value=page),
            patch("modulo.api.routes.runs.get_child_run_rollup", return_value={}),
            patch("modulo.api.routes.runs.count_active_runs_for_org", AsyncMock(return_value=3)),
            patch("modulo.api.routes.runs.get_org_run_concurrency_limit", AsyncMock(return_value=3)),
            patch("modulo.api.routes.runs.set_rls_org"),
        ):
            resp = client.get("/api/v1/runs")

        assert resp.status_code == 200
        item = resp.json()["items"][0]
        assert item["capacity"] == {
            "active_runs": 3,
            "concurrency_limit": 3,
            "waiting": True,
        }
