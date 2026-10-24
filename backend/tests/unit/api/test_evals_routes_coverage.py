"""Route-level coverage tests for the eval management endpoints (FAR-574).

Complements the existing eval test modules (endpoint contract, compare,
leaderboard, coverage-gap, dashboard) by covering the role-gate 403s (operator floor for create/update/from-run,
admin-only delete — FAR-735), the
guardrail-request validation 422s, the run-evals listing, the compare
fallback helpers, the from-run creation, and the route error convention
(ProgrammingError→501 / IntegrityError→409 / SQLAlchemyError→503 /
Exception→500) across all eval endpoints.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncGenerator, Generator
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.exc import IntegrityError, ProgrammingError, SQLAlchemyError

from modulo.api.dependencies import deny_break_glass_mint, get_db_session, get_plan_context
from modulo.api.main import app
from modulo.api.routes.evals import MutePolicyConfigRequest, update_mute_policy
from modulo.auth.dependencies import get_current_tenant_user, get_current_user
from modulo.auth.jwt import AuthenticatedPrincipal, TenantPrincipal
from modulo.settings import Settings, get_settings
from tests.unit.api.mock_session import configure_rls_preamble

_ORG_ID = uuid.UUID("00000000-0000-0000-0000-000000000001")
_USER_ID = uuid.UUID("00000000-0000-0000-0000-000000000002")
# FIXED UUIDs (not uuid.uuid4()): these constants are interpolated into
# @pytest.mark.parametrize values, and xdist workers import this module in
# separate processes — a random UUID yields DIFFERENT collected test IDs per
# worker and pytest-xdist aborts with "Different tests were collected between
# gw0 and gw1" (breaks every `-n auto` run, i.e. CI's Test Backend and the
# pre-push gate). Mocked IDs need stability, not randomness.
_PIPELINE_ID = uuid.UUID("00000000-0000-0000-0000-000000000003")
_RUN_ID = uuid.UUID("00000000-0000-0000-0000-000000000004")
_EVAL_ID = uuid.UUID("00000000-0000-0000-0000-000000000005")
_SUITE_ID = uuid.UUID("00000000-0000-0000-0000-000000000006")
_NODE_ID = uuid.UUID("00000000-0000-0000-0000-000000000007")
_RUN_ID_B = uuid.UUID("00000000-0000-0000-0000-000000000008")


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
    # Contract-correct double: the RLS preamble reads ``in_transaction()`` /
    # ``get_bind()`` / ``add()`` synchronously, so those must be sync mocks —
    # a bare AsyncMock turns each call into a never-awaited coroutine. The
    # ``execute`` contract is stubbed explicitly below.
    session = configure_rls_preamble(AsyncMock())
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

    # Make execute return a MagicMock (not a coroutine) so the team scope
    # resolver's synchronous `row = result.first(); row[0]` works.
    default_result = MagicMock()
    default_result.first.return_value = None
    default_result.scalar_one_or_none.return_value = None
    default_result.scalar.return_value = None
    default_result.all.return_value = []

    async def _execute(stmt: object, *_args: object, **_kwargs: object) -> MagicMock:
        return default_result

    session.execute = AsyncMock(side_effect=_execute)
    return session


def _result(
    scalar_one_or_none: object = None,
    scalar: object = None,
    rows: list | None = None,
    first: object | None = None,
) -> MagicMock:
    r = MagicMock()
    r.scalar_one_or_none = MagicMock(return_value=scalar_one_or_none)
    r.scalar = MagicMock(return_value=scalar)
    r.scalars.return_value.all = MagicMock(return_value=rows if rows is not None else [])
    r.all = MagicMock(return_value=rows if rows is not None else [])
    if first is not None:
        r.first = MagicMock(return_value=first)
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


def _typed_judge_config() -> dict:
    """A structurally valid typed judge config (ADR 066 D8)."""
    return {
        "field": "output",
        "answer_contract": "typed_questions",
        "backing": {"kind": "model_backend", "model_backend_id": str(uuid.uuid4())},
        "questions": [
            {
                "id": "verdict",
                "type": "noul",
                "instructions": "Is the output acceptable?",
                "criteria": [],
                "weight": 1.0,
                "required": True,
                "min_pass_confidence": 0.5,
            }
        ],
        "pass_rule": {"kind": "weighted_score_at_least", "threshold": 0.5},
        "verdict_kind": "assessment",
    }


def _create_payload(**overrides: object) -> dict:
    payload = {
        "pipeline_id": str(_PIPELINE_ID),
        "name": "my-eval",
        "eval_type": "model_judge",
        "config_json": _typed_judge_config(),
    }
    payload.update(overrides)
    return payload


def test_create_eval_operator_succeeds() -> None:
    """FAR-735: operators may create eval definitions (admin no longer required)."""
    session = _make_session()
    _install_overrides(session, org_role="operator")
    try:
        http = TestClient(app)
        session.execute = AsyncMock(return_value=_result(scalar_one_or_none=MagicMock()))
        resp = http.post("/api/v1/evals", json=_create_payload())
        assert resp.status_code == 201, resp.text
        assert resp.json()["name"] == "my-eval"
    finally:
        app.dependency_overrides.clear()


@pytest.mark.parametrize("org_role", ["runner", "viewer"])
def test_create_eval_below_operator_returns_403(org_role: str) -> None:
    """Roles below the registry floor are denied by require_permission."""
    session = _make_session()
    _install_overrides(session, org_role=org_role)
    try:
        http = TestClient(app)
        resp = http.post("/api/v1/evals", json=_create_payload())
        assert resp.status_code == 403
        assert "requires 'operator' role" in resp.json()["detail"]
    finally:
        app.dependency_overrides.clear()


@pytest.mark.parametrize(
    ("payload_overrides", "detail_fragment"),
    [
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
    assert body["eval_type"] == "model_judge"
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
            {"run_id": str(_RUN_ID), "node_id": str(_NODE_ID), "eval_type": "regex", "name": "n"},
        ),
        ("POST", "/api/v1/evals/compare", {"run_id_a": str(_RUN_ID), "run_id_b": str(_RUN_ID_B)}),
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
# Role gates: POST/PUT/from-run are operator-floor, DELETE stays admin-only
# ---------------------------------------------------------------------------


def test_delete_eval_stays_admin_for_operator() -> None:
    """DELETE is stricter than its registry floor: operators are still 403."""
    session = _make_session()
    _install_overrides(session, org_role="operator")
    try:
        http = TestClient(app)
        resp = http.delete(f"/api/v1/evals/{_EVAL_ID}")
        assert resp.status_code == 403
        assert "Only admins" in resp.json()["detail"]
    finally:
        app.dependency_overrides.clear()


def test_update_eval_operator_succeeds() -> None:
    """FAR-735: operators may update eval definitions."""
    session = _make_session()
    _install_overrides(session, org_role="operator")
    try:
        http = TestClient(app)
        eval_def = _eval_def_row(_EVAL_ID)
        _queue_execute(
            session,
            [
                _result(scalar_one_or_none=eval_def),  # eval lookup
                _result(scalar_one_or_none=None),  # PolicyGate lookup for current failure_behaviour
                _result(scalar_one_or_none=eval_def),  # create_or_update_eval: existing Eval lookup
                _result(scalar_one_or_none=None),  # create_or_update_eval: PolicyGate lookup
                _result(scalar_one_or_none=None),  # create_or_update_eval: flush
                _result(scalar_one_or_none=None),  # PolicyGate reload for response mapping
            ],
        )
        resp = http.put(f"/api/v1/evals/{_EVAL_ID}", json={"name": "renamed"})
        assert resp.status_code == 200, resp.text
        assert resp.json()["name"] == "renamed"
    finally:
        app.dependency_overrides.clear()


@pytest.mark.parametrize("org_role", ["runner", "viewer"])
def test_update_eval_below_operator_returns_403(org_role: str) -> None:
    session = _make_session()
    _install_overrides(session, org_role=org_role)
    try:
        http = TestClient(app)
        resp = http.put(f"/api/v1/evals/{_EVAL_ID}", json={"name": "renamed"})
        assert resp.status_code == 403
        assert "requires 'operator' role" in resp.json()["detail"]
    finally:
        app.dependency_overrides.clear()


def test_from_run_operator_succeeds() -> None:
    """FAR-735: operators may promote a run output into an eval definition."""
    session = _make_session()
    _install_overrides(session, org_role="operator")
    try:
        http = TestClient(app)
        node_id = uuid.uuid4()
        run = MagicMock()
        run.pipeline_id = _PIPELINE_ID
        run.outputs_json = {node_id.hex: "sample output"}
        run.node_telemetry_json = None
        pipeline = MagicMock()
        # FAR-583 read-switch: `session.info` binds the RLS org so read_rls_org
        # short-circuits without consuming a queue slot; the empty
        # run_node_outputs row set falls back to the legacy runs row.
        session.info = {"org_id": _ORG_ID}
        row = SimpleNamespace(
            run_id=str(_RUN_ID),
            node_id=node_id.hex,
            attempt_key="__final__",
            organisation_id=_ORG_ID,
            outputs_json={node_id.hex: "sample output"},
            node_telemetry_json=None,
            raw_output_markers=None,
            outputs_absent=False,
            telemetry_absent=True,
            markers_absent=True,
        )
        _queue_execute(
            session,
            [
                _result(scalar_one_or_none=run),  # run lookup
                _result(scalar_one_or_none=pipeline),  # pipeline lookup
                _result(rows=[row]),  # B2b: the run's __final__ rows page
                _result(scalar_one_or_none=None),  # PolicyGate upsert lookup (none yet)
            ],
        )
        resp = http.post("/api/v1/evals/from-run", json=_from_run_payload(node_id))
        assert resp.status_code == 201, resp.text
        assert resp.json()["eval_type"] == "regex"
    finally:
        app.dependency_overrides.clear()


@pytest.mark.parametrize("org_role", ["runner", "viewer"])
def test_from_run_below_operator_returns_403(org_role: str) -> None:
    session = _make_session()
    _install_overrides(session, org_role=org_role)
    try:
        http = TestClient(app)
        resp = http.post("/api/v1/evals/from-run", json=_from_run_payload())
        assert resp.status_code == 403
        assert "requires 'operator' role" in resp.json()["detail"]
    finally:
        app.dependency_overrides.clear()


def test_update_suite_alerting_rejects_operators() -> None:
    session = _make_session()
    _install_overrides(session, org_role="operator")
    try:
        http = TestClient(app)
        # The team scope resolver queries EvalSuite before the admin check in
        # the handler fires.  Return a valid org-visible suite so the resolver
        # doesn't 404 and the admin 403 can execute.
        suite_mock = MagicMock()
        suite_mock.id = _SUITE_ID
        suite_mock.visibility = "org"
        suite_mock.owner_team_id = None
        suite_result = MagicMock()
        suite_result.first.return_value = (None, "org")
        suite_result.scalar_one_or_none.return_value = suite_mock

        authz_result = MagicMock()
        authz_result.scalar_one_or_none.return_value = None

        async def _execute(stmt: object, *_args: object, **_kwargs: object) -> MagicMock:
            stmt_str = str(stmt)
            if "authz_enforce" in stmt_str:
                return authz_result
            if "eval_suites" in stmt_str:
                return suite_result
            return authz_result

        session.execute = AsyncMock(side_effect=_execute)
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


def test_list_run_evals_applies_node_and_eval_filters(client: tuple[TestClient, AsyncMock]) -> None:
    """Both optional filters (node_id, eval_id) add their predicate to the query."""
    http, session = client
    run = MagicMock()
    filter_node_id = uuid.uuid4()
    _queue_execute(
        session,
        [
            _result(scalar_one_or_none=run),  # run lookup
            _result(scalar=1),  # total count
            _result(rows=[_eval_result_row(_EVAL_ID)]),  # page rows
        ],
    )

    resp = http.get(
        f"/api/v1/runs/{_RUN_ID}/evals",
        params={"node_id": str(filter_node_id), "eval_id": str(_EVAL_ID)},
    )

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["total"] == 1
    assert len(body["items"]) == 1
    assert body["items"][0]["eval_id"] == str(_EVAL_ID)


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


@pytest.mark.asyncio
async def test_update_mute_policy_preserves_omitted_field() -> None:
    session = _make_session()
    eval_def = _eval_def_row(_EVAL_ID)
    eval_def.organisation_id = _ORG_ID
    eval_def.mute_threshold = 0.2
    eval_def.mute_min_outcomes = 10
    eval_def.is_muted = True
    eval_def.muted_at = None
    eval_def.muted_by = None
    eval_def.acceptance_rate = 0.8
    session.get = AsyncMock(return_value=eval_def)
    principal = TenantPrincipal(
        username="admin@test",
        organisation_id=_ORG_ID,
        account_id=_USER_ID,
        org_role="admin",
    )

    with (
        patch("modulo.api.routes.evals.set_rls_org", new_callable=AsyncMock),
        patch("modulo.api.routes.evals.set_rls_user_context", new_callable=AsyncMock),
        patch("modulo.core.eval.finding_resolver.apply_mute_policy", new_callable=AsyncMock),
        patch("modulo.core.eval.mute_policy.record_mute_audit_event", new_callable=AsyncMock),
    ):
        response = await update_mute_policy(
            _EVAL_ID,
            MutePolicyConfigRequest(mute_threshold=0.3),
            session,
            principal,
        )

    assert response.mute_threshold == pytest.approx(0.3)
    assert response.mute_min_outcomes == 10
    assert eval_def.mute_min_outcomes == 10


@pytest.mark.asyncio
async def test_update_mute_policy_explicit_null_clears_field() -> None:
    session = _make_session()
    eval_def = _eval_def_row(_EVAL_ID)
    eval_def.organisation_id = _ORG_ID
    eval_def.mute_threshold = 0.2
    eval_def.mute_min_outcomes = 10
    eval_def.is_muted = True
    eval_def.muted_at = None
    eval_def.muted_by = None
    eval_def.acceptance_rate = 0.8
    session.get = AsyncMock(return_value=eval_def)
    principal = TenantPrincipal(
        username="admin@test",
        organisation_id=_ORG_ID,
        account_id=_USER_ID,
        org_role="admin",
    )

    with (
        patch("modulo.api.routes.evals.set_rls_org", new_callable=AsyncMock),
        patch("modulo.api.routes.evals.set_rls_user_context", new_callable=AsyncMock),
        patch("modulo.core.eval.finding_resolver.apply_mute_policy", new_callable=AsyncMock),
        patch("modulo.core.eval.mute_policy.record_mute_audit_event", new_callable=AsyncMock),
    ):
        response = await update_mute_policy(
            _EVAL_ID,
            MutePolicyConfigRequest(mute_threshold=None),
            session,
            principal,
        )

    assert response.mute_threshold is None
    assert response.mute_min_outcomes == 10
    assert eval_def.is_muted is False


def _finding_row() -> MagicMock:
    finding = _eval_result_row(_EVAL_ID)
    finding.id = uuid.UUID("00000000-0000-0000-0000-000000000009")
    finding.resolution = "accepted"
    finding.resolution_reason = "verified"
    finding.resolved_at = datetime(2026, 1, 1, tzinfo=UTC)
    finding.resolved_by = _USER_ID
    finding.eval_id = _EVAL_ID
    return finding


def _configured_eval_row(*, muted: bool = False) -> MagicMock:
    eval_def = _eval_def_row(_EVAL_ID)
    eval_def.organisation_id = _ORG_ID
    eval_def.is_muted = muted
    eval_def.mute_threshold = 0.5
    eval_def.mute_min_outcomes = 3
    eval_def.acceptance_rate = 0.25
    eval_def.muted_at = datetime(2026, 1, 1, tzinfo=UTC) if muted else None
    eval_def.muted_by = _USER_ID if muted else None
    return eval_def


def test_resolve_finding_endpoint_uses_wire_contract_and_mute_wiring(client: tuple[TestClient, AsyncMock]) -> None:
    http, session = client
    finding = _finding_row()
    session.begin = MagicMock(return_value=_make_session().begin.return_value)
    with (
        patch(
            "modulo.core.eval.finding_resolver.resolve_finding",
            new_callable=AsyncMock,
            return_value=finding,
        ) as resolve,
        patch("modulo.core.eval.finding_resolver.apply_mute_policy", new_callable=AsyncMock) as apply_mute,
    ):
        response = http.post(
            f"/api/v1/evals/findings/{finding.id}/resolve",
            json={"resolution": "accepted", "reason": "verified"},
        )

    assert response.status_code == 200, response.text
    assert response.json() == {
        "id": str(finding.id),
        "resolution": "accepted",
        "resolution_reason": "verified",
        "resolved_at": finding.resolved_at.isoformat(),
        "resolved_by": str(_USER_ID),
    }
    resolve.assert_awaited_once_with(
        session,
        str(finding.id),
        "accepted",
        "verified",
        str(_USER_ID),
        str(_ORG_ID),
    )
    apply_mute.assert_awaited_once_with(
        session,
        str(_EVAL_ID),
        str(_ORG_ID),
        muted_by=None,
        automatic=True,
    )


def test_resolve_finding_endpoint_rejects_invalid_payload(client: tuple[TestClient, AsyncMock]) -> None:
    http, _session = client
    response = http.post(
        f"/api/v1/evals/findings/{uuid.uuid4()}/resolve",
        json={"resolution": "not-a-resolution", "unexpected": True},
    )
    assert response.status_code == 422
    assert response.json()["detail"]


@pytest.mark.parametrize("org_role", ["runner", "viewer"])
def test_finding_resolution_endpoint_requires_permission(org_role: str) -> None:
    session = _make_session()
    _install_overrides(session, org_role=org_role)
    try:
        response = TestClient(app).post(
            f"/api/v1/evals/findings/{uuid.uuid4()}/resolve",
            json={"resolution": "accepted"},
        )
        assert response.status_code == 403
        assert "requires 'operator' role" in response.json()["detail"]
    finally:
        app.dependency_overrides.clear()


def test_get_mute_policy_endpoint_returns_evaluation_contract(client: tuple[TestClient, AsyncMock]) -> None:
    http, session = client
    eval_def = _configured_eval_row()
    session.get = AsyncMock(return_value=eval_def)
    with patch(
        "modulo.core.eval.finding_resolver.evaluate_mute_policy",
        new_callable=AsyncMock,
        return_value={
            "should_mute": True,
            "acceptance_rate": 0.25,
            "total_outcomes": 4,
            "threshold": 0.5,
            "min_outcomes": 3,
            "reason": "below threshold",
        },
    ):
        response = http.get(f"/api/v1/evals/{_EVAL_ID}/mute-policy", params={"window_days": 30})

    assert response.status_code == 200, response.text
    assert response.json() == {
        "eval_id": str(_EVAL_ID),
        "should_mute": True,
        "acceptance_rate": 0.25,
        "total_outcomes": 4,
        "threshold": 0.5,
        "min_outcomes": 3,
        "reason": "below threshold",
        "is_muted": False,
    }


def test_mute_policy_endpoint_rejects_invalid_window(client: tuple[TestClient, AsyncMock]) -> None:
    http, _session = client
    response = http.get(f"/api/v1/evals/{_EVAL_ID}/mute-policy", params={"window_days": 0})
    assert response.status_code == 422


def test_update_mute_policy_endpoint_round_trips_patch_and_audit_wiring(
    client: tuple[TestClient, AsyncMock],
) -> None:
    http, session = client
    eval_def = _configured_eval_row(muted=True)
    session.get = AsyncMock(return_value=eval_def)
    with (
        patch("modulo.core.eval.finding_resolver.apply_mute_policy", new_callable=AsyncMock) as apply_mute,
        patch("modulo.core.eval.mute_policy.record_mute_audit_event", new_callable=AsyncMock) as audit,
    ):
        response = http.patch(
            f"/api/v1/evals/{_EVAL_ID}/mute-policy",
            json={"mute_threshold": 0.7},
        )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["eval_id"] == str(_EVAL_ID)
    assert body["mute_threshold"] == pytest.approx(0.7)
    assert body["mute_min_outcomes"] == 3
    apply_mute.assert_awaited_once_with(
        session,
        str(_EVAL_ID),
        str(_ORG_ID),
        muted_by=None,
        automatic=True,
    )
    audit.assert_awaited_once()


def test_unmute_endpoint_returns_updated_state_and_audits(client: tuple[TestClient, AsyncMock]) -> None:
    http, session = client
    eval_def = _configured_eval_row(muted=True)
    session.get = AsyncMock(return_value=eval_def)
    with (
        patch("modulo.core.eval.finding_resolver.unmute_definition", new_callable=AsyncMock) as unmute,
        patch("modulo.core.eval.mute_policy.record_mute_audit_event", new_callable=AsyncMock) as audit,
    ):
        response = http.post(f"/api/v1/evals/{_EVAL_ID}/mute-policy/unmute")

    assert response.status_code == 200, response.text
    assert response.json()["eval_id"] == str(_EVAL_ID)
    unmute.assert_awaited_once_with(session, str(_EVAL_ID), str(_ORG_ID))
    audit.assert_awaited_once()


def test_update_mute_policy_endpoint_rejects_out_of_range_threshold(client: tuple[TestClient, AsyncMock]) -> None:
    http, _session = client
    response = http.patch(
        f"/api/v1/evals/{_EVAL_ID}/mute-policy",
        json={"mute_threshold": 1.1},
    )
    assert response.status_code == 422


def test_create_eval_from_run_returns_definition_with_sample(client: tuple[TestClient, AsyncMock]) -> None:
    http, session = client
    node_id = uuid.uuid4()
    run = MagicMock()
    run.pipeline_id = _PIPELINE_ID
    run.outputs_json = {node_id.hex: "sample output"}
    run.node_telemetry_json = None
    pipeline = MagicMock()
    # FAR-583 read-switch: the blobs reassemble through the repo reader, which
    # issues its own SELECTs against this session. `session.info` binds the
    # RLS org so read_rls_org short-circuits without consuming a queue slot;
    # the empty run_node_outputs row set falls back to the legacy runs row.
    session.info = {"org_id": _ORG_ID}
    row = SimpleNamespace(
        run_id=str(run.id),
        node_id=node_id.hex,
        attempt_key="__final__",
        organisation_id=_ORG_ID,
        outputs_json={node_id.hex: "sample output"},
        node_telemetry_json=None,
        raw_output_markers=None,
        outputs_absent=False,
        telemetry_absent=True,
        markers_absent=True,
    )
    _queue_execute(
        session,
        [
            _result(scalar_one_or_none=run),  # run lookup
            _result(scalar_one_or_none=pipeline),  # pipeline lookup
            _result(rows=[row]),  # B2b: the run's __final__ rows page
            _result(scalar_one_or_none=None),  # PolicyGate upsert lookup (none yet)
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
