"""Route-level coverage tests for the eval management endpoints (FAR-574).

Complements the existing eval test modules (endpoint contract, compare,
leaderboard, coverage-gap, dashboard) by covering the admin-gate 403s, the
guardrail-request validation 422s, the run-evals listing, the compare
fallback helpers, the from-run creation, and the route error convention
(ProgrammingError→501 / IntegrityError→409 / SQLAlchemyError→503 /
Exception→500) across all eval endpoints.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncGenerator, Generator
from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.exc import IntegrityError, ProgrammingError, SQLAlchemyError

from modulo.api.dependencies import deny_break_glass_mint, get_db_session, get_plan_context
from modulo.api.main import app
from modulo.auth.dependencies import get_current_tenant_user, get_current_user
from modulo.auth.jwt import AuthenticatedPrincipal, TenantPrincipal
from modulo.settings import Settings, get_settings

_ORG_ID = uuid.UUID("00000000-0000-0000-0000-000000000001")
_USER_ID = uuid.UUID("00000000-0000-0000-0000-000000000002")
_PIPELINE_ID = uuid.uuid4()
_RUN_ID = uuid.uuid4()
_EVAL_ID = uuid.uuid4()
_SUITE_ID = uuid.uuid4()

_PROG = ProgrammingError("s", {}, Exception())
_SQL = SQLAlchemyError("boom")
_RUNTIME = RuntimeError("kaboom")
_INTEGRITY = IntegrityError("s", {}, Exception())


def _make_settings() -> Settings:
    return Settings(
        database_url="postgresql+asyncpg://localhost/test",
        secret_key="a" * 32,
        fernet_key="a" * 32,
        modulo_admin_password="testpass",
    )


class _BeginRaiser:
    """Async CM whose ``__aenter__`` raises the given exception.

    Since every eval endpoint wraps its DB work in
    ``try: async with session.begin(): ...``, a begin-time raise exercises the
    endpoint's error-mapping blocks without touching real query plumbing.
    """

    def __init__(self, exc: Exception) -> None:
        self._exc = exc

    async def __aenter__(self) -> None:
        raise self._exc

    async def __aexit__(self, *args: object) -> None:
        return None


def _make_session(begin_exc: Exception | None = None) -> AsyncMock:
    session = AsyncMock()
    if begin_exc is not None:
        session.begin = MagicMock(return_value=_BeginRaiser(begin_exc))
        return session
    begin_cm = AsyncMock()
    begin_cm.__aenter__ = AsyncMock(return_value=None)
    begin_cm.__aexit__ = AsyncMock(return_value=False)
    session.begin = MagicMock(return_value=begin_cm)
    session.add = MagicMock(return_value=None)
    session.flush = AsyncMock(return_value=None)
    session.delete = AsyncMock(return_value=None)
    session.refresh = AsyncMock(return_value=None)
    return session


def _result(scalar_one_or_none: object = None, scalar: object = None, rows: list | None = None) -> MagicMock:
    r = MagicMock()
    r.scalar_one_or_none = MagicMock(return_value=scalar_one_or_none)
    r.scalar = MagicMock(return_value=scalar)
    r.scalars.return_value.all = MagicMock(return_value=rows if rows is not None else [])
    r.all = MagicMock(return_value=rows if rows is not None else [])
    return r


def _queue_execute(session: AsyncMock, results: list[MagicMock]) -> None:
    """Route ``session.execute`` through a result queue, minus auth noise.

    ``require_permission`` issues a per-request kill-switch read on
    ``organisations.authz_enforce`` through the same session; that read is
    answered with an empty result and never consumes the queued results.
    """
    authz_result = _result()

    async def _execute(stmt: object, *_args: object, **_kwargs: object) -> MagicMock:
        if "authz_enforce" in str(stmt):
            return authz_result
        if not results:
            raise AssertionError("Unexpected session.execute(): the result queue is exhausted")
        return results.pop(0)

    session.execute = AsyncMock(side_effect=_execute)


def _eval_result_row(
    eval_id: uuid.UUID,
    *,
    score: float | None = 0.8,
    node_id: uuid.UUID | None = None,
) -> MagicMock:
    r = MagicMock()
    r.id = uuid.uuid4()
    r.run_id = _RUN_ID
    r.eval_id = eval_id
    r.node_id = node_id
    r.passed = True
    r.score = score
    r.detail = "ok"
    r.evaluated_at = datetime.now(UTC)
    return r


def _eval_def_row(eval_id: uuid.UUID = _EVAL_ID) -> MagicMock:
    d = MagicMock()
    d.id = eval_id
    d.name = "judge"
    return d


def _install_overrides(session: AsyncMock, *, org_role: str = "admin") -> None:
    async def override_session() -> AsyncGenerator[AsyncMock, None]:
        yield session

    app.dependency_overrides[get_settings] = _make_settings
    app.dependency_overrides[get_db_session] = override_session
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
    # Plan gating + break-glass mint checks must not consume the mocked
    # session's execute queue (each would issue its own DB read).
    mock_plan = MagicMock()
    mock_plan.feature_enabled.return_value = True
    app.dependency_overrides[get_plan_context] = lambda: mock_plan
    app.dependency_overrides[deny_break_glass_mint] = lambda: None


@pytest.fixture(autouse=True)
def _patch_route_rls():
    """Stub the RLS session-setup helpers the eval routes call directly.

    The real ``set_rls_org``/``set_rls_user_context`` issue ``set_config``
    statements through ``session.execute``, which would consume the tests'
    side_effect result queues; unit tier stubs them at the route boundary.
    """
    with (
        patch("modulo.api.routes.evals.set_rls_org", new_callable=AsyncMock),
        patch("modulo.api.routes.evals.set_rls_user_context", new_callable=AsyncMock),
    ):
        yield


@pytest.fixture
def client() -> Generator[tuple[TestClient, AsyncMock], None, None]:
    session = _make_session()
    _install_overrides(session)
    yield TestClient(app), session
    app.dependency_overrides.clear()


# ---------------------------------------------------------------------------
# POST /evals — admin gate, guardrail validation, pipeline 404, error mapping
# ---------------------------------------------------------------------------


def _create_payload(**overrides: object) -> dict:
    payload = {
        "pipeline_id": str(_PIPELINE_ID),
        "name": "my-eval",
        "eval_type": "llm_judge",
        "config_json": {},
    }
    payload.update(overrides)
    return payload


def test_create_eval_non_admin_returns_403() -> None:
    session = _make_session()
    _install_overrides(session, org_role="operator")
    try:
        http = TestClient(app)
        resp = http.post("/api/v1/evals", json=_create_payload())
        assert resp.status_code == 403
        assert "Only admins" in resp.json()["detail"]
    finally:
        app.dependency_overrides.clear()


@pytest.mark.parametrize(
    ("payload_overrides", "detail_fragment"),
    [
        ({"eval_type": "guardrail", "failure_behaviour": "retry"}, "never use failure_behaviour='retry'"),
        ({"eval_type": "guardrail", "failure_behaviour": "explode"}, "must be 'warn' or 'block'"),
        ({"eval_type": "guardrail", "config_json": {"action": "delete"}}, "action must be one of"),
        ({"eval_type": "guardrail", "config_json": {"type": "llm"}}, "detection must be regex|json_schema"),
        (
            {"eval_type": "guardrail", "config_json": {"detection": {"type": "llm"}}},
            "detection envelope type must be regex|json_schema",
        ),
    ],
)
def test_create_guardrail_validation_rejections(
    client: tuple[TestClient, AsyncMock],
    payload_overrides: dict,
    detail_fragment: str,
) -> None:
    http, _session = client

    resp = http.post("/api/v1/evals", json=_create_payload(**payload_overrides))

    assert resp.status_code == 422, resp.text
    assert detail_fragment in resp.json()["detail"]


def test_create_eval_pipeline_not_found_returns_404(client: tuple[TestClient, AsyncMock]) -> None:
    http, session = client
    session.execute = AsyncMock(return_value=_result(scalar_one_or_none=None))

    resp = http.post("/api/v1/evals", json=_create_payload())

    assert resp.status_code == 404
    assert resp.json()["detail"] == "Pipeline not found"


def test_create_eval_returns_definition_dict(client: tuple[TestClient, AsyncMock]) -> None:
    http, session = client
    pipeline = MagicMock()
    session.execute = AsyncMock(return_value=_result(scalar_one_or_none=pipeline))

    resp = http.post("/api/v1/evals", json=_create_payload())

    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["name"] == "my-eval"
    assert body["eval_type"] == "llm_judge"
    assert body["version"] == 1


@pytest.mark.parametrize(
    ("exc", "expected"),
    [(_PROG, 501), (_INTEGRITY, 409), (_SQL, 503), (_RUNTIME, 500)],
)
def test_create_eval_error_mapping(
    client: tuple[TestClient, AsyncMock],
    exc: Exception,
    expected: int,
) -> None:
    session = _make_session(begin_exc=exc)
    _install_overrides(session)
    try:
        http2 = TestClient(app)
        resp = http2.post("/api/v1/evals", json=_create_payload())
        assert resp.status_code == expected, resp.text
    finally:
        app.dependency_overrides.clear()


# ---------------------------------------------------------------------------
# Error mapping for the read/update/delete endpoints (begin-time raise)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("method", "url", "json_body"),
    [
        ("GET", "/api/v1/evals", None),
        ("GET", f"/api/v1/evals/coverage?pipeline_id={_PIPELINE_ID}", None),
        ("GET", "/api/v1/evals/leaderboard", None),
        ("GET", f"/api/v1/evals/{_EVAL_ID}/timeseries", None),
        ("GET", f"/api/v1/evals/{_EVAL_ID}", None),
        ("GET", f"/api/v1/runs/{_RUN_ID}/evals", None),
        ("PUT", f"/api/v1/evals/suites/{_SUITE_ID}/alerting", {"cooldown": 30}),
        ("PUT", f"/api/v1/evals/{_EVAL_ID}", {"name": "renamed"}),
        ("DELETE", f"/api/v1/evals/{_EVAL_ID}", None),
        (
            "POST",
            "/api/v1/evals/from-run",
            {"run_id": str(_RUN_ID), "node_id": str(uuid.uuid4()), "eval_type": "regex", "name": "n"},
        ),
        ("POST", "/api/v1/evals/compare", {"run_id_a": str(_RUN_ID), "run_id_b": str(uuid.uuid4())}),
    ],
    ids=[
        "list-evals",
        "coverage",
        "leaderboard",
        "timeseries",
        "get-eval",
        "run-evals",
        "suite-alerting",
        "update-eval",
        "delete-eval",
        "from-run",
        "compare",
    ],
)
@pytest.mark.parametrize(
    ("exc", "expected"),
    [(_PROG, 501), (_INTEGRITY, 409), (_SQL, 503), (_RUNTIME, 500)],
)
def test_evals_error_mapping_matrix(
    client: tuple[TestClient, AsyncMock],
    method: str,
    url: str,
    json_body: dict | None,
    exc: Exception,
    expected: int,
) -> None:
    session = _make_session(begin_exc=exc)
    _install_overrides(session)
    try:
        http2 = TestClient(app)
        resp = http2.request(method, url, json=json_body)
        assert resp.status_code == expected, f"{method} {url}: {resp.text}"
    finally:
        app.dependency_overrides.clear()


# ---------------------------------------------------------------------------
# PUT/DELETE admin gates
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("method", "url", "json_body"),
    [
        ("PUT", f"/api/v1/evals/{_EVAL_ID}", {"name": "renamed"}),
        ("DELETE", f"/api/v1/evals/{_EVAL_ID}", None),
        (
            "POST",
            "/api/v1/evals/from-run",
            {"run_id": str(_RUN_ID), "node_id": str(uuid.uuid4()), "eval_type": "regex", "name": "n"},
        ),
    ],
)
def test_admin_gates_reject_operators(method: str, url: str, json_body: dict | None) -> None:
    session = _make_session()
    _install_overrides(session, org_role="operator")
    try:
        http = TestClient(app)
        resp = http.request(method, url, json=json_body)
        assert resp.status_code == 403
        assert "Only admins" in resp.json()["detail"]
    finally:
        app.dependency_overrides.clear()


def test_update_suite_alerting_rejects_operators() -> None:
    session = _make_session()
    _install_overrides(session, org_role="operator")
    try:
        http = TestClient(app)
        resp = http.put(f"/api/v1/evals/suites/{_SUITE_ID}/alerting", json={"cooldown": 30})
        assert resp.status_code == 403
        assert "Only admins" in resp.json()["detail"]
    finally:
        app.dependency_overrides.clear()


# ---------------------------------------------------------------------------
# GET /runs/{run_id}/evals — happy path + 404
# ---------------------------------------------------------------------------


def test_list_run_evals_returns_paginated_results(client: tuple[TestClient, AsyncMock]) -> None:
    http, session = client
    run = MagicMock()
    _queue_execute(
        session,
        [
            _result(scalar_one_or_none=run),  # run lookup
            _result(scalar=1),  # total count
            _result(rows=[_eval_result_row(_EVAL_ID)]),  # page rows
        ],
    )

    resp = http.get(f"/api/v1/runs/{_RUN_ID}/evals", params={"page": 1, "page_size": 10})

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["total"] == 1
    assert body["page"] == 1
    assert body["page_size"] == 10
    assert len(body["items"]) == 1
    item = body["items"][0]
    assert item["eval_id"] == str(_EVAL_ID)
    assert item["passed"] is True
    assert item["score"] == pytest.approx(0.8)


def test_list_run_evals_unknown_run_returns_404(client: tuple[TestClient, AsyncMock]) -> None:
    http, session = client
    session.execute = AsyncMock(return_value=_result(scalar_one_or_none=None))

    resp = http.get(f"/api/v1/runs/{_RUN_ID}/evals")

    assert resp.status_code == 404
    assert resp.json()["detail"] == "Run not found"


# ---------------------------------------------------------------------------
# POST /evals/compare — run-B 404 + mixed-result helper paths
# ---------------------------------------------------------------------------


def test_compare_evals_run_b_missing_returns_404(client: tuple[TestClient, AsyncMock]) -> None:
    http, session = client
    run_a = MagicMock()
    _queue_execute(
        session,
        [
            _result(scalar_one_or_none=run_a),  # run A
            _result(scalar_one_or_none=None),  # run B missing
        ],
    )

    resp = http.post(
        "/api/v1/evals/compare",
        json={"run_id_a": str(_RUN_ID), "run_id_b": str(uuid.uuid4())},
    )

    assert resp.status_code == 404
    assert resp.json()["detail"] == "Run B not found"


def test_compare_evals_handles_missing_sides_and_null_scores(client: tuple[TestClient, AsyncMock]) -> None:
    http, session = client
    run_a, run_b = MagicMock(), MagicMock()
    run_a.id, run_b.id = _RUN_ID, uuid.uuid4()
    run_a.created_at, run_b.created_at = datetime.now(UTC), None
    eval_other = uuid.uuid4()
    result_a = _eval_result_row(_EVAL_ID, score=0.8, node_id=uuid.uuid4())
    result_b = _eval_result_row(eval_other, score=None, node_id=None)
    _queue_execute(
        session,
        [
            _result(scalar_one_or_none=run_a),
            _result(scalar_one_or_none=run_b),
            _result(rows=[result_a]),  # results for A
            _result(rows=[result_b]),  # results for B
            _result(rows=[_eval_def_row(), _eval_def_row(eval_other)]),  # definitions
        ],
    )

    resp = http.post(
        "/api/v1/evals/compare",
        json={"run_id_a": str(_RUN_ID), "run_id_b": str(run_b.id)},
    )

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["run_b"]["created_at"] is None  # _iso_or_none(None)
    results = {r["eval_id"]: r for r in body["results"]}
    # eval present only in A: result_b is None, delta = 0.8 - 0.0.
    mine = results[str(_EVAL_ID)]
    assert mine["result_b"] is None
    assert mine["delta"] == pytest.approx(0.8)
    assert mine["node_id"] == str(result_a.node_id)
    # eval present only in B with score None + node_id None: node_id falls back to None.
    other = results[str(eval_other)]
    assert other["result_a"] is None
    assert other["node_id"] is None
    assert other["delta"] == 0.0


# ---------------------------------------------------------------------------
# POST /evals/from-run — happy path + pipeline 404
# ---------------------------------------------------------------------------


def _from_run_payload(node_id: uuid.UUID | None = None) -> dict:
    return {
        "run_id": str(_RUN_ID),
        "node_id": str(node_id or uuid.uuid4()),
        "eval_type": "regex",
        "name": "from-run-eval",
    }


def test_create_eval_from_run_returns_definition_with_sample(client: tuple[TestClient, AsyncMock]) -> None:
    http, session = client
    node_id = uuid.uuid4()
    run = MagicMock()
    run.pipeline_id = _PIPELINE_ID
    run.outputs_json = {node_id.hex: "sample output"}
    run.node_telemetry_json = None
    pipeline = MagicMock()
    _queue_execute(
        session,
        [
            _result(scalar_one_or_none=run),  # run lookup
            _result(scalar_one_or_none=pipeline),  # pipeline lookup
        ],
    )

    resp = http.post("/api/v1/evals/from-run", json=_from_run_payload(node_id))

    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["name"] == "from-run-eval"
    assert body["eval_type"] == "regex"
    assert body["sample_output"]


def test_create_eval_from_run_pipeline_missing_returns_404(client: tuple[TestClient, AsyncMock]) -> None:
    http, session = client
    run = MagicMock()
    run.pipeline_id = _PIPELINE_ID
    run.outputs_json = {}
    run.node_telemetry_json = None
    _queue_execute(
        session,
        [
            _result(scalar_one_or_none=run),
            _result(scalar_one_or_none=None),  # pipeline missing
        ],
    )

    resp = http.post("/api/v1/evals/from-run", json=_from_run_payload())

    assert resp.status_code == 404
    assert resp.json()["detail"] == "Pipeline not found"
