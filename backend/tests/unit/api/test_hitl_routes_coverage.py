"""Route-level coverage tests for the HITL endpoints (FAR-618).

Complements ``test_hitl_resilience.py`` (SQLAlchemyError→503, NotTeamMember→403,
sandbox-capacity 409s, FAR-541 gate stamping) by covering the claim happy path
and missing-claim-data 500, the per-route manager exception matrices
(GateNotFound→404 / AlreadyClaimed|AlreadyDecided→409 / ClaimTokenInvalid→403 /
ClaimTokenExpired→410 / DecisionPayload→422), ProgrammingError→501, the
generic-Exception→500 backstops, the resume-failure→500 surfaces, the
deliver-manual empty-output 422, and the run/org pending-gate read paths.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncGenerator, Generator
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.exc import ProgrammingError, SQLAlchemyError

from modulo.api.dependencies import _get_engine, get_db_session, get_plan_context
from modulo.api.main import app
from modulo.api.models.problem import ProblemType
from modulo.auth.dependencies import get_current_user
from modulo.auth.jwt import AuthenticatedPrincipal
from modulo.core.hitl_manager import (
    AlreadyClaimedError,
    ClaimTokenExpiredError,
    ClaimTokenInvalidError,
    DecisionPayloadError,
    GateAlreadyDecidedError,
    GateNotFoundError,
    RunNotAwaitingError,
)
from modulo.settings import Settings, get_settings
from tests.unit.api.mock_session import configure_mock_session

_VALID_32 = "a" * 32
_ORG_ID = uuid.UUID("00000000-0000-0000-0000-000000000001")
_USER_ID = uuid.UUID("00000000-0000-0000-0000-000000000002")
_RUN_ID = uuid.UUID("00000000-0000-0000-0000-000000000100")
_SNAPSHOT_ID = uuid.UUID("00000000-0000-0000-0000-000000000200")

_PROG = ProgrammingError("s", {}, Exception())
_SQL = SQLAlchemyError("boom")


def _make_settings() -> Settings:
    return Settings(
        database_url="postgresql+asyncpg://localhost/test",
        secret_key=_VALID_32,
        fernet_key=_VALID_32,
        modulo_admin_password="testpass",
    )


def _make_session() -> AsyncMock:
    session = AsyncMock()
    configure_mock_session(session, allow_empty_execute=True)
    begin_cm = AsyncMock()
    begin_cm.__aenter__ = AsyncMock(return_value=None)
    begin_cm.__aexit__ = AsyncMock(return_value=False)
    session.begin = MagicMock(return_value=begin_cm)
    session.get = AsyncMock(return_value=None)
    return session


@pytest.fixture
def client() -> Generator[tuple[TestClient, AsyncMock], None, None]:
    session = _make_session()

    async def override_session() -> AsyncGenerator[AsyncMock, None]:
        yield session

    mock_plan = MagicMock()
    mock_plan.feature_enabled.return_value = True
    app.dependency_overrides[get_settings] = _make_settings
    app.dependency_overrides[get_plan_context] = lambda: mock_plan
    app.dependency_overrides[get_db_session] = override_session
    app.dependency_overrides[_get_engine] = lambda: MagicMock()
    app.dependency_overrides[get_current_user] = lambda: AuthenticatedPrincipal(
        username="admin@test",
        organisation_id=_ORG_ID,
        account_id=_USER_ID,
        org_role="admin",
    )
    yield TestClient(app), session
    app.dependency_overrides.clear()


def _gate_mock(*, claim_token: str | None = "tok-123") -> MagicMock:
    gate = MagicMock()
    gate.run_id = _RUN_ID
    gate.gate_id = "gate-1"
    gate.claim_token = claim_token
    gate.expires_at = datetime.now(UTC) + timedelta(minutes=15)
    return gate


def _claim_gate(http: TestClient) -> object:
    return http.post(f"/api/v1/runs/{_RUN_ID}/hitl/gate-1/claim", json={"expiry_minutes": 15})


# ---------------------------------------------------------------------------
# _build_resume_executor — notifier init failure isolation
# ---------------------------------------------------------------------------


def test_build_resume_executor_survives_notifier_init_failure() -> None:
    from modulo.api.routes.hitl import _build_resume_executor

    with (
        patch("modulo.api.routes.hitl.Notifier", side_effect=RuntimeError("no fernet")),
        patch("modulo.api.routes.hitl.PipelineExecutor", return_value="executor") as executor_cls,
    ):
        executor = _build_resume_executor(MagicMock())

    assert executor == "executor"
    assert executor_cls.call_args.kwargs["notifier"] is None


# ---------------------------------------------------------------------------
# POST .../claim — happy path, error matrix, missing claim data
# ---------------------------------------------------------------------------


def test_claim_gate_happy_path(client: tuple[TestClient, AsyncMock]) -> None:
    http, _session = client
    gate = _gate_mock()
    with (
        patch("modulo.api.routes.hitl.HITLManager.claim", new=AsyncMock(return_value=gate)),
        patch("modulo.api.routes.hitl.transition_run", new=AsyncMock(return_value=True)) as transition,
    ):
        resp = _claim_gate(http)

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["run_id"] == str(_RUN_ID)
    assert body["gate_id"] == "gate-1"
    assert body["claim_token"] == "tok-123"
    transition.assert_awaited_once()


@pytest.mark.parametrize(
    ("exc", "expected"),
    [
        (GateNotFoundError(_RUN_ID, "gate-1"), 404),
        (AlreadyClaimedError(_RUN_ID, "gate-1"), 409),
        (_PROG, 501),
        (_SQL, 503),
        (RuntimeError("kaboom"), 500),
    ],
    ids=["gate-404", "already-claimed-409", "programming-501", "sqlalchemy-503", "unexpected-500"],
)
def test_claim_gate_assert_error_matrix(client: tuple[TestClient, AsyncMock], exc: Exception, expected: int) -> None:
    http, _session = client
    with patch("modulo.api.routes.hitl.HITLManager.claim", new=AsyncMock(side_effect=exc)):
        resp = _claim_gate(http)

    assert resp.status_code == expected, resp.text


def test_claim_gate_missing_claim_data_returns_500(client: tuple[TestClient, AsyncMock]) -> None:
    http, _session = client
    gate = _gate_mock(claim_token=None)
    with (
        patch("modulo.api.routes.hitl.HITLManager.claim", new=AsyncMock(return_value=gate)),
        patch("modulo.api.routes.hitl.transition_run", new=AsyncMock(return_value=True)),
    ):
        resp = _claim_gate(http)

    assert resp.status_code == 500, resp.text
    assert "gate_missing_claim_data" in resp.json()["detail"]


@pytest.mark.parametrize(
    ("exc", "problem_type", "detail_fragment"),
    [
        (
            AlreadyClaimedError(_RUN_ID, "gate-1"),
            ProblemType.HITL_GATE_ALREADY_CLAIMED,
            "is already claimed",
        ),
        (
            GateAlreadyDecidedError(_RUN_ID, "gate-1"),
            ProblemType.HITL_GATE_ALREADY_DECIDED,
            "already has a decision",
        ),
        (
            RunNotAwaitingError(_RUN_ID, "complete"),
            ProblemType.HITL_RUN_NOT_AWAITING,
            "not awaiting a human decision (status: complete)",
        ),
    ],
    ids=["already-claimed", "already-decided", "run-not-awaiting"],
)
def test_claim_gate_maps_domain_errors_to_typed_problems(
    client: tuple[TestClient, AsyncMock],
    exc: Exception,
    problem_type: ProblemType,
    detail_fragment: str,
) -> None:
    """FAR-645: the three claim conflicts carry distinct RFC 9457 problem types
    (the frontend discriminates by ``type``, not by prose) with identical status
    (409) and human detail text as before."""
    http, _session = client
    with patch("modulo.api.routes.hitl.HITLManager.claim", new=AsyncMock(side_effect=exc)):
        resp = _claim_gate(http)

    assert resp.status_code == 409, resp.text
    body = resp.json()
    assert body["type"] == f"urn:problem:modulo:{problem_type.value}"
    assert body["title"] == "Conflict"
    assert detail_fragment in body["detail"]


# ---------------------------------------------------------------------------
# Manager-exception matrices on the four decision routes + manual submit
# ---------------------------------------------------------------------------


_DECISION_ROUTES = [
    ("approve", "hitl/gate-1/approve", {"claim_token": "tok", "notes": "n"}, "approve"),
    (
        "approve-with-modification",
        "hitl/gate-1/approve-with-modification",
        {"claim_token": "tok", "modified_output": {"k": "v"}},
        "approve_with_modification",
    ),
    ("reject", "hitl/gate-1/reject", {"claim_token": "tok", "reason": "bad output"}, "reject"),
    ("deliver-manual", "hitl/gate-1/deliver-manual", {"claim_token": "tok", "output": {"o": 1}}, "deliver_manual"),
    ("manual-submit", "manual/gate-1/submit", {"claim_token": "tok", "output": {"o": 1}}, "approve"),
]


def _decision_patches(hitl_method: str, side_effect: object, *, resume: object = AsyncMock()) -> list:
    executor = MagicMock()
    executor.resume = resume
    return [
        patch("modulo.api.routes.hitl.org_sandbox_capacity_free", new=AsyncMock(return_value=True)),
        patch(f"modulo.api.routes.hitl.HITLManager.{hitl_method}", new=AsyncMock(side_effect=side_effect)),
        patch("modulo.api.routes.hitl._build_resume_executor", return_value=executor),
    ]


@pytest.mark.parametrize(
    ("exc", "expected"),
    [
        (GateNotFoundError(_RUN_ID, "gate-1"), 404),
        (GateAlreadyDecidedError(_RUN_ID, "gate-1"), 409),
        (ClaimTokenInvalidError(), 403),
        (ClaimTokenExpiredError(), 410),
        (DecisionPayloadError("bad payload"), 422),
        (_PROG, 501),
        (_SQL, 503),
        (RuntimeError("kaboom"), 500),
    ],
    ids=[
        "gate-404",
        "decided-409",
        "token-invalid-403",
        "token-expired-410",
        "payload-422",
        "prog-501",
        "sql-503",
        "unexpected-500",
    ],
)
def test_approve_gate_assert_error_matrix(client: tuple[TestClient, AsyncMock], exc: Exception, expected: int) -> None:
    http, _session = client
    patches = _decision_patches("approve", exc)
    for p in patches:
        p.start()
    try:
        resp = http.post(f"/api/v1/runs/{_RUN_ID}/hitl/gate-1/approve", json={"claim_token": "tok"})
    finally:
        for p in patches:
            p.stop()

    assert resp.status_code == expected, resp.text


@pytest.mark.parametrize(
    ("label", "path", "payload", "hitl_method"),
    [(label, path, payload, method) for label, path, payload, method in _DECISION_ROUTES[1:]],
)
def test_decision_route_error_matrices(
    client: tuple[TestClient, AsyncMock],
    label: str,
    path: str,
    payload: dict,
    hitl_method: str,
) -> None:
    """The approve-with-modification / reject / deliver-manual / manual-submit
    routes repeat approve's manager-exception matrix — cover them all."""
    http, _session = client
    cases = [
        (GateNotFoundError(_RUN_ID, "gate-1"), 404),
        (GateAlreadyDecidedError(_RUN_ID, "gate-1"), 409),
        (ClaimTokenInvalidError(), 403),
        (ClaimTokenExpiredError(), 410),
        (DecisionPayloadError("bad payload"), 422),
        (_PROG, 501),
        (RuntimeError("kaboom"), 500),
    ]
    for exc, expected in cases:
        patches = _decision_patches(hitl_method, exc)
        for p in patches:
            p.start()
        try:
            resp = http.post(f"/api/v1/runs/{_RUN_ID}/{path}", json=payload)
        finally:
            for p in patches:
                p.stop()
        assert resp.status_code == expected, f"{label}: {exc!r} -> {resp.text}"


@pytest.mark.parametrize(
    ("path", "payload", "detail_fragment"),
    [
        ("hitl/gate-1/approve", {"claim_token": "tok"}, "after approval"),
        (
            "hitl/gate-1/approve-with-modification",
            {"claim_token": "tok", "modified_output": {"k": "v"}},
            "with modification",
        ),
        ("hitl/gate-1/reject", {"claim_token": "tok", "reason": "bad"}, "after rejection"),
        ("hitl/gate-1/deliver-manual", {"claim_token": "tok", "output": {"o": 1}}, "manual delivery"),
        ("manual/gate-1/submit", {"claim_token": "tok", "output": {"o": 1}}, "manual output submission"),
    ],
    ids=["approve", "approve-mod", "reject", "deliver-manual", "manual-submit"],
)
def test_resume_failure_returns_500(
    client: tuple[TestClient, AsyncMock],
    path: str,
    payload: dict,
    detail_fragment: str,
) -> None:
    http, _session = client
    executor = MagicMock()
    executor.resume = AsyncMock(side_effect=RuntimeError("executor down"))
    route = next(r for r in _DECISION_ROUTES if r[1] == path)
    patches = [
        patch("modulo.api.routes.hitl.org_sandbox_capacity_free", new=AsyncMock(return_value=True)),
        patch(f"modulo.api.routes.hitl.HITLManager.{route[3]}", new=AsyncMock(return_value=MagicMock())),
        patch("modulo.api.routes.hitl._build_resume_executor", return_value=executor),
    ]
    for p in patches:
        p.start()
    try:
        resp = http.post(f"/api/v1/runs/{_RUN_ID}/{path}", json=payload)
    finally:
        for p in patches:
            p.stop()

    assert resp.status_code == 500, resp.text
    assert detail_fragment in resp.json()["detail"]


def test_deliver_manual_empty_output_returns_422(client: tuple[TestClient, AsyncMock]) -> None:
    http, _session = client

    resp = http.post(f"/api/v1/runs/{_RUN_ID}/hitl/gate-1/deliver-manual", json={"claim_token": "tok", "output": {}})

    assert resp.status_code == 422, resp.text
    assert "non-empty object" in resp.json()["detail"]


# ---------------------------------------------------------------------------
# GET /runs/{run_id}/hitl/pending — 404 / ProgrammingError / unexpected
# ---------------------------------------------------------------------------


def test_list_run_pending_gates_unknown_run_returns_404(client: tuple[TestClient, AsyncMock]) -> None:
    http, _session = client
    with patch("modulo.api.routes.hitl.get_run", new=AsyncMock(return_value=None)):
        resp = http.get(f"/api/v1/runs/{_RUN_ID}/hitl/pending")

    assert resp.status_code == 404, resp.text
    assert resp.json()["detail"] == "Run not found"


@pytest.mark.parametrize(("exc", "expected"), [(_PROG, 501), (RuntimeError("kaboom"), 500)])
def test_list_run_pending_gates_error_mapping(
    client: tuple[TestClient, AsyncMock], exc: Exception, expected: int
) -> None:
    http, _session = client
    with patch("modulo.api.routes.hitl.get_run", new=AsyncMock(side_effect=exc)):
        resp = http.get(f"/api/v1/runs/{_RUN_ID}/hitl/pending")

    assert resp.status_code == expected, resp.text


# ---------------------------------------------------------------------------
# GET /hitl/pending — org-level pipeline-map resolution + error mapping
# ---------------------------------------------------------------------------


def _org_gate() -> MagicMock:
    gate = MagicMock()
    gate.run_id = _RUN_ID
    gate.gate_id = "gate-1"
    gate.pipeline_id = uuid.uuid4()
    gate.account_id = _USER_ID
    gate.claimed_at = None
    gate.expires_at = None
    gate.decision = None
    gate.decision_at = None
    return gate


def test_list_org_pending_gates_resolves_pipeline_names(client: tuple[TestClient, AsyncMock]) -> None:
    http, session = client
    gate = _org_gate()
    pipeline_name = "Reviewer Pipeline"

    def _execute(stmt: object, *_args: object, **_kwargs: object) -> MagicMock:
        result = MagicMock()
        result.all = MagicMock(return_value=[(gate.pipeline_id, pipeline_name)])
        return result

    session.execute = AsyncMock(side_effect=_execute)
    with patch("modulo.api.routes.hitl.HITLManager.list_pending", new=AsyncMock(return_value=[gate])):
        resp = http.get("/api/v1/hitl/pending")

    assert resp.status_code == 200, resp.text
    gates = resp.json()["gates"]
    assert len(gates) == 1
    assert gates[0]["pipeline_name"] == pipeline_name


def test_list_org_pending_gates_no_gates_skips_pipeline_query(client: tuple[TestClient, AsyncMock]) -> None:
    http, _session = client
    with (
        patch("modulo.api.routes.hitl.HITLManager.list_pending", new=AsyncMock(return_value=[])),
    ):
        resp = http.get("/api/v1/hitl/pending")

    assert resp.status_code == 200, resp.text
    assert not resp.json()["gates"]


def test_list_org_pending_gates_requests_include_claimed(
    client: tuple[TestClient, AsyncMock],
) -> None:
    """FAR-686: the org endpoint asks list_pending() for claimed-but-undecided
    gates too, so a claimed gate stays on the review page after a refresh
    instead of vanishing (the reported bug)."""
    http, _session = client
    claimed = _org_gate()
    unclaimed = _org_gate()
    unclaimed.account_id = None
    list_pending = AsyncMock(return_value=[claimed, unclaimed])
    with patch("modulo.api.routes.hitl.HITLManager.list_pending", new=list_pending):
        resp = http.get("/api/v1/hitl/pending")

    assert resp.status_code == 200, resp.text
    list_pending.assert_awaited_once()
    assert list_pending.await_args.args[1] == _ORG_ID
    assert list_pending.await_args.kwargs["include_claimed"] is True
    gates = resp.json()["gates"]
    assert len(gates) == 2
    claimed_rows = [g for g in gates if g["claimed_by"] is not None]
    assert len(claimed_rows) == 1
    assert claimed_rows[0]["claimed_by"] == str(_USER_ID)


def test_list_org_pending_gates_resolves_gate_labels(
    client: tuple[TestClient, AsyncMock],
) -> None:
    """FAR-686: the org endpoint batch-resolves each pending gate's human
    label via run → snapshot → hitl_gate_config.label. A gate whose run has
    no snapshot degrades to label=None while the rest of the list keeps its
    labels, and pipeline_name resolution keeps working."""
    http, session = client
    snap_id = uuid.uuid4()
    run_with_snapshot = uuid.uuid4()
    run_without_snapshot = uuid.uuid4()
    pipeline_id = uuid.uuid4()

    claimed = _org_gate()
    claimed.run_id = run_with_snapshot
    claimed.gate_id = "hitl_gate_planner_deploy"
    claimed.pipeline_id = pipeline_id
    unclaimed = _org_gate()
    unclaimed.run_id = run_without_snapshot
    unclaimed.gate_id = "hitl_gate_review_ship"
    unclaimed.pipeline_id = pipeline_id
    unclaimed.account_id = None

    graph_json = {
        "edges": [
            {
                "source": "planner",
                "target": "deploy",
                "hitl_gate_config": {"label": "Deploy gate"},
            }
        ]
    }

    def _execute(stmt: object, *_args: object, **_kwargs: object) -> MagicMock:
        result = MagicMock()
        text = str(stmt)
        if "pipeline_snapshots" in text:
            result.all.return_value = [(snap_id, graph_json)]
        elif "FROM runs" in text:
            result.all.return_value = [
                (run_with_snapshot, snap_id),
                (run_without_snapshot, None),
            ]
        elif "pipelines" in text:
            result.all.return_value = [(pipeline_id, "Reviewer Pipeline")]
        else:
            # set_config / authz plumbing — benign empty result
            result.scalar.return_value = None
            result.scalar_one_or_none.return_value = None
            result.all.return_value = []
        return result

    session.execute = AsyncMock(side_effect=_execute)
    with patch("modulo.api.routes.hitl.HITLManager.list_pending", new=AsyncMock(return_value=[claimed, unclaimed])):
        resp = http.get("/api/v1/hitl/pending")

    assert resp.status_code == 200, resp.text
    gates = resp.json()["gates"]
    assert len(gates) == 2
    assert gates[0]["gate_id"] == "hitl_gate_planner_deploy"
    assert gates[0]["label"] == "Deploy gate"
    assert gates[1]["gate_id"] == "hitl_gate_review_ship"
    assert gates[1]["label"] is None
    assert gates[0]["pipeline_name"] == "Reviewer Pipeline"
    assert gates[1]["pipeline_name"] == "Reviewer Pipeline"


def test_list_org_pending_gates_resolves_claimant_names(
    client: tuple[TestClient, AsyncMock],
) -> None:
    """FAR-691: the org endpoint batch-resolves claimant display names from
    the accounts table (one select for all gates) and stamps claimed_by_me
    from the principal. A gate whose account row is missing degrades to
    claimed_by_name=None (the frontend falls back to the raw UUID), and an
    empty display_name falls back to the account email."""
    http, session = client
    other_id = uuid.uuid4()
    ghost_id = uuid.uuid4()
    mine = _org_gate()
    mine.account_id = _USER_ID
    theirs = _org_gate()
    theirs.run_id = uuid.uuid4()
    theirs.gate_id = "gate-2"
    theirs.account_id = other_id
    ghost = _org_gate()
    ghost.run_id = uuid.uuid4()
    ghost.gate_id = "gate-3"
    ghost.account_id = ghost_id

    def _execute(stmt: object, *_args: object, **_kwargs: object) -> MagicMock:
        result = MagicMock()
        text = str(stmt)
        if "accounts" in text:
            result.all.return_value = [
                (_USER_ID, "Alice Reviewer", "alice@test"),
                (other_id, "", "bob@test"),
            ]
        elif "pipelines" in text:
            result.all.return_value = [(mine.pipeline_id, "Reviewer Pipeline")]
        else:
            # runs / snapshots / RLS plumbing — benign empty result
            result.all.return_value = []
        return result

    session.execute = AsyncMock(side_effect=_execute)
    with (
        patch(
            "modulo.api.routes.hitl.HITLManager.list_pending",
            new=AsyncMock(return_value=[mine, theirs, ghost]),
        ),
        patch("modulo.api.routes.hitl.resolve_gate_descriptions", new=AsyncMock(return_value={})),
    ):
        resp = http.get("/api/v1/hitl/pending")

    assert resp.status_code == 200, resp.text
    gates = resp.json()["gates"]
    assert len(gates) == 3
    own = next(g for g in gates if g["gate_id"] == "gate-1")
    foreign = next(g for g in gates if g["gate_id"] == "gate-2")
    ghost_gate = next(g for g in gates if g["gate_id"] == "gate-3")
    assert own["claimed_by_me"] is True
    assert own["claimed_by_name"] == "Alice Reviewer"
    assert foreign["claimed_by_me"] is False
    assert foreign["claimed_by_name"] == "bob@test"
    assert ghost_gate["claimed_by_name"] is None
    assert ghost_gate["claimed_by_me"] is False


@pytest.mark.parametrize(("exc", "expected"), [(_PROG, 501), (RuntimeError("kaboom"), 500)])
def test_list_org_pending_gates_error_mapping(
    client: tuple[TestClient, AsyncMock], exc: Exception, expected: int
) -> None:
    http, _session = client
    with patch("modulo.api.routes.hitl.HITLManager.list_pending", new=AsyncMock(side_effect=exc)):
        resp = http.get("/api/v1/hitl/pending")

    assert resp.status_code == expected, resp.text


# ---------------------------------------------------------------------------
# FAR-613 — pending gates carry the decision briefing (description + context)
# ---------------------------------------------------------------------------


def _briefed_gate(graph_gate_id: str, *, context: dict | None) -> MagicMock:
    gate = MagicMock()
    gate.run_id = _RUN_ID
    gate.gate_id = graph_gate_id
    gate.pipeline_id = uuid.uuid4()
    gate.account_id = _USER_ID
    gate.claimed_at = None
    gate.expires_at = None
    gate.decision = None
    gate.decision_at = None
    gate.context_json = context
    return gate


def _graph_with_gate(gate_id: str, description: str | None = "Why this gate needs a human decision.") -> dict:
    from modulo.db.crud.hitl_gate_config import parse_hitl_gate_id

    source, target = parse_hitl_gate_id(gate_id) or ("src-1", "tgt-2")
    config: dict = {"label": "Review gate"}
    if description is not None:
        config["description"] = description
    return {
        "nodes": [{"id": source, "label": "Generator"}],
        "edges": [{"source": source, "target": target, "hitl_gate_config": config}],
    }


def test_list_run_pending_gates_carries_description_and_context(client: tuple[TestClient, AsyncMock]) -> None:
    http, session = client
    graph = _graph_with_gate("hitl_gate_src-1_tgt-2")
    context = {"trigger": "condition", "condition": "output.severity == 'high'", "pipeline_name": "PR Reviewer"}
    gate = _briefed_gate("hitl_gate_src-1_tgt-2", context=context)

    run = MagicMock()
    run.snapshot_id = _SNAPSHOT_ID
    claims_result = MagicMock()
    claims_result.scalars.return_value = [gate]
    snapshot = MagicMock()
    snapshot.graph_json = graph
    snapshot_result = MagicMock()
    snapshot_result.scalar_one_or_none.return_value = snapshot

    async def _execute(stmt: object, *_args: object, **_kwargs: object) -> MagicMock:
        text = str(stmt)
        if "hitl_claims" in text:
            return claims_result
        return snapshot_result

    session.execute = AsyncMock(side_effect=_execute)

    with (
        patch("modulo.api.routes.hitl.get_run", new=AsyncMock(return_value=run)),
    ):
        resp = http.get(f"/api/v1/runs/{_RUN_ID}/hitl/pending")

    assert resp.status_code == 200, resp.text
    gate_payload = resp.json()["gates"][0]
    assert gate_payload["description"] == "Why this gate needs a human decision."
    assert gate_payload["context"] == context


def test_list_run_pending_gates_prefers_the_captured_description(client: tuple[TestClient, AsyncMock]) -> None:
    """FAR-688: context-first precedence at the run-level endpoint — the
    fire-time captured description wins over the snapshot config's
    description (which may have been edited after the gate fired)."""
    http, session = client
    graph = _graph_with_gate("hitl_gate_src-1_tgt-2", description="Stale snapshot briefing.")
    context = {
        "trigger": "condition",
        "description": "Captured fire-time briefing.",
        "condition": "output.severity == 'high'",
    }
    gate = _briefed_gate("hitl_gate_src-1_tgt-2", context=context)

    run = MagicMock()
    run.snapshot_id = _SNAPSHOT_ID
    claims_result = MagicMock()
    claims_result.scalars.return_value = [gate]
    snapshot = MagicMock()
    snapshot.graph_json = graph
    snapshot_result = MagicMock()
    snapshot_result.scalar_one_or_none.return_value = snapshot

    async def _execute(stmt: object, *_args: object, **_kwargs: object) -> MagicMock:
        text = str(stmt)
        if "hitl_claims" in text:
            return claims_result
        return snapshot_result

    session.execute = AsyncMock(side_effect=_execute)

    with (
        patch("modulo.api.routes.hitl.get_run", new=AsyncMock(return_value=run)),
    ):
        resp = http.get(f"/api/v1/runs/{_RUN_ID}/hitl/pending")

    assert resp.status_code == 200, resp.text
    gate_payload = resp.json()["gates"][0]
    assert gate_payload["description"] == "Captured fire-time briefing."


def test_list_org_pending_gates_carries_description_and_context(client: tuple[TestClient, AsyncMock]) -> None:
    http, session = client
    gate = _briefed_gate("hitl_gate_src-1_tgt-2", context={"trigger": "node", "reason": "two failures"})
    graph = _graph_with_gate("hitl_gate_src-1_tgt-2", description="Org-level briefing.")

    pipeline_rows = MagicMock()
    pipeline_rows.all.return_value = [(gate.pipeline_id, "Reviewer Pipeline")]
    run_rows = MagicMock()
    run_rows.all.return_value = [(_RUN_ID, _SNAPSHOT_ID)]
    snapshot_rows = MagicMock()
    snapshot_rows.all.return_value = [(_SNAPSHOT_ID, graph)]

    async def _execute(stmt: object, *_args: object, **_kwargs: object) -> MagicMock:
        text = str(stmt)
        if "pipeline_snapshots" in text:
            return snapshot_rows
        if "runs" in text:
            return run_rows
        return pipeline_rows

    session.execute = AsyncMock(side_effect=_execute)

    with patch("modulo.api.routes.hitl.HITLManager.list_pending", new=AsyncMock(return_value=[gate])):
        resp = http.get("/api/v1/hitl/pending")

    assert resp.status_code == 200, resp.text
    gate_payload = resp.json()["gates"][0]
    assert gate_payload["pipeline_name"] == "Reviewer Pipeline"
    assert gate_payload["description"] == "Org-level briefing."
    assert gate_payload["context"] == {"trigger": "node", "reason": "two failures"}


def test_list_org_pending_gates_prefers_the_captured_description(client: tuple[TestClient, AsyncMock]) -> None:
    """FAR-688: context-first precedence at the org-level endpoint — the
    fire-time captured description wins over the snapshot config's
    description, via the same ``resolve_gate_descriptions`` helper the
    MCP ``list_pending_hitl`` surface applies."""
    http, session = client
    context = {
        "trigger": "node",
        "reason": "two failures",
        "description": "Captured org briefing.",
    }
    gate = _briefed_gate("hitl_gate_src-1_tgt-2", context=context)
    graph = _graph_with_gate("hitl_gate_src-1_tgt-2", description="Stale snapshot briefing.")

    pipeline_rows = MagicMock()
    pipeline_rows.all.return_value = [(gate.pipeline_id, "Reviewer Pipeline")]
    run_rows = MagicMock()
    run_rows.all.return_value = [(_RUN_ID, _SNAPSHOT_ID)]
    snapshot_rows = MagicMock()
    snapshot_rows.all.return_value = [(_SNAPSHOT_ID, graph)]

    async def _execute(stmt: object, *_args: object, **_kwargs: object) -> MagicMock:
        text = str(stmt)
        if "pipeline_snapshots" in text:
            return snapshot_rows
        if "runs" in text:
            return run_rows
        return pipeline_rows

    session.execute = AsyncMock(side_effect=_execute)

    with patch("modulo.api.routes.hitl.HITLManager.list_pending", new=AsyncMock(return_value=[gate])):
        resp = http.get("/api/v1/hitl/pending")

    assert resp.status_code == 200, resp.text
    gate_payload = resp.json()["gates"][0]
    assert gate_payload["description"] == "Captured org briefing."


def test_list_run_pending_gates_resolves_claimant_names(
    client: tuple[TestClient, AsyncMock],
) -> None:
    """FAR-691: the run-level endpoint batch-resolves claimant display names
    from the accounts table and stamps claimed_by_me from the principal —
    same contract as the org endpoint."""
    http, session = client
    other_id = uuid.uuid4()
    mine = _briefed_gate("hitl_gate_src-1_tgt-2", context=None)
    mine.account_id = _USER_ID
    theirs = _briefed_gate("hitl_gate_src-3_tgt-4", context=None)
    theirs.account_id = other_id
    run = MagicMock()
    run.snapshot_id = _SNAPSHOT_ID
    claims_result = MagicMock()
    claims_result.scalars.return_value = [mine, theirs]
    accounts_result = MagicMock()
    accounts_result.all.return_value = [
        (_USER_ID, "Alice Reviewer", "alice@test"),
        (other_id, "", "bob@test"),
    ]
    snapshot = MagicMock()
    snapshot.graph_json = {}
    snapshot_result = MagicMock()
    snapshot_result.scalar_one_or_none.return_value = snapshot

    async def _execute(stmt: object, *_args: object, **_kwargs: object) -> MagicMock:
        text = str(stmt)
        if "hitl_claims" in text:
            return claims_result
        if "accounts" in text:
            return accounts_result
        return snapshot_result

    session.execute = AsyncMock(side_effect=_execute)

    with patch("modulo.api.routes.hitl.get_run", new=AsyncMock(return_value=run)):
        resp = http.get(f"/api/v1/runs/{_RUN_ID}/hitl/pending")

    assert resp.status_code == 200, resp.text
    gates = resp.json()["gates"]
    assert len(gates) == 2
    own = next(g for g in gates if g["gate_id"] == "hitl_gate_src-1_tgt-2")
    foreign = next(g for g in gates if g["gate_id"] == "hitl_gate_src-3_tgt-4")
    assert own["claimed_by_me"] is True
    assert own["claimed_by_name"] == "Alice Reviewer"
    assert foreign["claimed_by_me"] is False
    assert foreign["claimed_by_name"] == "bob@test"


def test_pending_gate_without_context_or_description_renders_null_fields(client: tuple[TestClient, AsyncMock]) -> None:
    http, session = client
    graph = _graph_with_gate("hitl_gate_src-1_tgt-2", description=None)
    gate = _briefed_gate("hitl_gate_src-1_tgt-2", context=None)

    run = MagicMock()
    run.snapshot_id = _SNAPSHOT_ID
    claims_result = MagicMock()
    claims_result.scalars.return_value = [gate]
    snapshot = MagicMock()
    snapshot.graph_json = graph
    snapshot_result = MagicMock()
    snapshot_result.scalar_one_or_none.return_value = snapshot

    async def _execute(stmt: object, *_args: object, **_kwargs: object) -> MagicMock:
        text = str(stmt)
        if "hitl_claims" in text:
            return claims_result
        return snapshot_result

    session.execute = AsyncMock(side_effect=_execute)

    with patch("modulo.api.routes.hitl.get_run", new=AsyncMock(return_value=run)):
        resp = http.get(f"/api/v1/runs/{_RUN_ID}/hitl/pending")

    assert resp.status_code == 200, resp.text
    gate_payload = resp.json()["gates"][0]
    assert gate_payload["description"] is None
    assert gate_payload["context"] is None


# ---------------------------------------------------------------------------
# GET /hitl/gates — paginated org gate listing incl. decided gates (FAR-692)
# ---------------------------------------------------------------------------


def _decided_gate(*, decision: str = "approved", account_id: uuid.UUID | None = _USER_ID) -> MagicMock:
    gate = _org_gate()
    gate.decision = decision
    gate.decision_at = datetime.now(UTC) - timedelta(hours=1)
    gate.account_id = account_id
    gate.claimed_at = datetime.now(UTC) - timedelta(hours=2)
    return gate


def _capture_gates_execute(session: AsyncMock, *, gates: list[MagicMock], total: int | None = None) -> list[object]:
    """Wire session.execute to a statement-shape dispatch for /hitl/gates.

    The endpoint issues a count() over hitl_claims, then (when total > 0) the
    page query, plus pipeline / run / snapshot / account enrichment selects
    and the RLS set_config plumbing. Returns the captured statements so tests
    can assert on the compiled WHERE / LIMIT / OFFSET.
    """
    captured: list[object] = []

    async def _execute(stmt: object, *_args: object, **_kwargs: object) -> MagicMock:
        captured.append(stmt)
        text = str(stmt)
        # NB: dispatch on "count(*)", never bare "count" — "account_id"
        # contains "count" as a substring.
        if "count(*)" in text:
            result = MagicMock()
            result.scalar_one.return_value = total if total is not None else len(gates)
            return result
        if "hitl_claims" in text:
            result = MagicMock()
            result.scalars.return_value = list(gates)
            return result
        # runs / pipeline_snapshots / accounts / RLS set_config — benign empty
        result = MagicMock()
        result.scalar.return_value = None
        result.scalar_one_or_none.return_value = None
        result.all.return_value = []
        return result

    session.execute = AsyncMock(side_effect=_execute)
    return captured


def _where_sql(stmt: object) -> str:
    """The statement's WHERE clause compiled with inlined literals."""
    whereclause = getattr(stmt, "whereclause", None)
    if whereclause is None:
        return ""
    return str(whereclause.compile(compile_kwargs={"literal_binds": True}))


def _page_statements(captured: list[object]) -> list[object]:
    """The non-count hitl_claims page statements captured by the mock."""
    return [s for s in captured if "hitl_claims" in str(s) and "count(*)" not in str(s)]


def _compiled_sql(stmts: list[object]) -> str:
    """Statements compiled with inlined literals, joined for substring asserts."""
    return " ".join(str(s.compile(compile_kwargs={"literal_binds": True})) for s in stmts)


def test_list_org_gates_default_status_is_undecided(client: tuple[TestClient, AsyncMock]) -> None:
    """No status param → the undecided queue (decision IS NULL), the same
    view the pending endpoint serves — the review page's default."""
    http, session = client
    captured = _capture_gates_execute(session, gates=[_org_gate()])

    resp = http.get("/api/v1/hitl/gates")

    assert resp.status_code == 200, resp.text
    where = " ".join(_where_sql(s) for s in captured)
    assert "decision IS NULL" in where
    # Default view is pending work — fenced to actionable runs (FAR-612/FAR-604).
    assert "runs.status IN" in where
    assert "JOIN runs ON hitl_claims.run_id = runs.id" in _compiled_sql(_page_statements(captured))
    assert "approved" not in where
    body = resp.json()
    assert body["page"] == 1
    assert body["page_size"] == 25
    assert body["total"] == 1
    assert len(body["items"]) == 1


@pytest.mark.parametrize(
    ("status_param", "expected_fragments"),
    [
        ("undecided", ["decision IS NULL", "runs.status IN"]),
        ("pending", ["decision IS NULL", "account_id IS NULL", "runs.status IN"]),
        ("claimed", ["decision IS NULL", "account_id IS NOT NULL", "runs.status IN"]),
        ("approved", ["decision = 'approved'"]),
        ("rejected", ["decision = 'rejected'"]),
    ],
    ids=["undecided", "pending", "claimed", "approved", "rejected"],
)
def test_list_org_gates_status_filter_compiles_the_right_where(
    client: tuple[TestClient, AsyncMock],
    status_param: str,
    expected_fragments: list[str],
) -> None:
    """Each status param narrows to exactly the intended gate subset — asserted
    on the compiled SQL the endpoint actually issues (the filter is
    server-side; a mocked session cannot re-implement it)."""
    http, session = client
    # total > 0 so the endpoint actually issues the page query to inspect.
    captured = _capture_gates_execute(session, gates=[], total=1)

    resp = http.get(f"/api/v1/hitl/gates?status={status_param}")

    assert resp.status_code == 200, resp.text
    page_stmts = _page_statements(captured)
    assert page_stmts
    where = " ".join(_where_sql(s) for s in page_stmts)
    for fragment in expected_fragments:
        assert fragment in where, f"{status_param}: {fragment!r} not in {where!r}"


@pytest.mark.parametrize("status_param", ["undecided", "pending", "claimed"])
def test_list_org_gates_pending_work_statuses_join_and_fence_to_actionable_runs(
    client: tuple[TestClient, AsyncMock], status_param: str
) -> None:
    """Pending-work statuses are fenced like HITLManager.list_pending
    (FAR-612/FAR-604): joined to runs and restricted to actionable run
    statuses, so orphaned undecided gates on terminal runs never surface —
    on BOTH the page query and the count (the count must match the page)."""
    http, session = client
    # total > 0 so the endpoint actually issues the page query to inspect.
    captured = _capture_gates_execute(session, gates=[], total=1)

    resp = http.get(f"/api/v1/hitl/gates?status={status_param}")

    assert resp.status_code == 200, resp.text
    page_stmts = _page_statements(captured)
    assert page_stmts
    page_sql = _compiled_sql(page_stmts)
    assert "JOIN runs ON hitl_claims.run_id = runs.id" in page_sql
    assert "runs.status IN" in _where_sql(page_stmts[0])
    count_stmts = [s for s in captured if "count(*)" in str(s)]
    assert len(count_stmts) == 1
    count_sql = str(count_stmts[0].compile(compile_kwargs={"literal_binds": True}))
    assert "JOIN runs ON hitl_claims.run_id = runs.id" in count_sql
    assert "runs.status IN" in count_sql


@pytest.mark.parametrize("status_param", ["approved", "rejected", "all"])
def test_list_org_gates_history_statuses_are_not_run_fenced(
    client: tuple[TestClient, AsyncMock], status_param: str
) -> None:
    """Decided history (approved/rejected) and the `all` audit view are
    deliberately unfenced: a decided gate's run has legitimately moved past
    awaiting_human, and the audit view must surface data-rot rows."""
    http, session = client
    captured = _capture_gates_execute(session, gates=[], total=1)

    resp = http.get(f"/api/v1/hitl/gates?status={status_param}")

    assert resp.status_code == 200, resp.text
    page_stmts = _page_statements(captured)
    assert page_stmts
    page_sql = _compiled_sql(page_stmts)
    assert "JOIN runs" not in page_sql
    assert "runs.status IN" not in page_sql
    count_stmts = [s for s in captured if "count(*)" in str(s)]
    assert len(count_stmts) == 1
    count_sql = str(count_stmts[0].compile(compile_kwargs={"literal_binds": True}))
    assert "JOIN runs" not in count_sql
    assert "runs.status IN" not in count_sql


def test_list_org_gates_status_all_skips_the_decision_filter(client: tuple[TestClient, AsyncMock]) -> None:
    http, session = client
    captured = _capture_gates_execute(session, gates=[_org_gate()])

    resp = http.get("/api/v1/hitl/gates?status=all")

    assert resp.status_code == 200, resp.text
    page_stmts = _page_statements(captured)
    assert page_stmts
    assert not _where_sql(page_stmts[0])
    assert resp.json()["total"] == 1


def test_list_org_gates_invalid_status_returns_422(client: tuple[TestClient, AsyncMock]) -> None:
    """The Literal-typed param gets 422 validation for free — no hand-rolled
    check in the handler."""
    http, _session = client

    resp = http.get("/api/v1/hitl/gates?status=bogus")

    assert resp.status_code == 422, resp.text


def test_list_org_gates_pagination_respected(client: tuple[TestClient, AsyncMock]) -> None:
    http, session = client
    captured = _capture_gates_execute(session, gates=[_org_gate()], total=42)

    resp = http.get("/api/v1/hitl/gates?page=3&page_size=10")

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["total"] == 42
    assert body["page"] == 3
    assert body["page_size"] == 10
    page_sql = _compiled_sql(_page_statements(captured))
    assert "LIMIT 10" in page_sql
    assert "OFFSET 20" in page_sql
    # Newest activity first, stable id tiebreaker.
    assert "decision_at DESC NULLS LAST" in page_sql
    assert "claimed_at DESC NULLS LAST" in page_sql
    assert "id DESC" in page_sql


def test_list_org_gates_page_size_clamped_at_100(client: tuple[TestClient, AsyncMock]) -> None:
    """Oversized page_size clamps to the ceiling (200 here) instead of 422 —
    the response echoes the effective page size and the LIMIT matches."""
    http, session = client
    captured = _capture_gates_execute(session, gates=[_org_gate()])

    resp = http.get("/api/v1/hitl/gates?page_size=500")

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["page_size"] == 100
    page_sql = _compiled_sql(_page_statements(captured))
    assert "LIMIT 100" in page_sql


def test_list_org_gates_zero_total_skips_the_page_query(client: tuple[TestClient, AsyncMock]) -> None:
    http, session = client
    captured = _capture_gates_execute(session, gates=[], total=0)

    resp = http.get("/api/v1/hitl/gates")

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["total"] == 0
    assert not body["items"]
    assert not _page_statements(captured)


def test_list_org_gates_decided_gates_carry_label_claimant_and_me(client: tuple[TestClient, AsyncMock]) -> None:
    """A decided gate renders through the same GateResponse contract as the
    pending queue: snapshot label (FAR-686), claimant display name + the
    caller-owns stamp (FAR-691), decision + decision_at."""
    http, session = client
    snap_id = uuid.uuid4()
    decided = _decided_gate()
    decided.gate_id = "hitl_gate_planner_deploy"  # matches the graph edge's derived gate id
    graph_json = {
        "edges": [
            {
                "source": "planner",
                "target": "deploy",
                "hitl_gate_config": {"label": "Deploy gate"},
            }
        ]
    }

    async def _execute(stmt: object, *_args: object, **_kwargs: object) -> MagicMock:
        text = str(stmt)
        result = MagicMock()
        if "count(*)" in text:
            result.scalar_one.return_value = 1
            return result
        if "hitl_claims" in text:
            result.scalars.return_value = [decided]
            return result
        if "pipeline_snapshots" in text:
            result.all.return_value = [(snap_id, graph_json)]
            return result
        if "FROM runs" in text:
            result.all.return_value = [(decided.run_id, snap_id)]
            return result
        if "pipelines" in text:
            result.all.return_value = [(decided.pipeline_id, "Reviewer Pipeline")]
            return result
        if "accounts" in text:
            result.all.return_value = [(_USER_ID, "Alice Reviewer", "alice@test")]
            return result
        result.scalar.return_value = None
        result.scalar_one_or_none.return_value = None
        result.all.return_value = []
        return result

    session.execute = AsyncMock(side_effect=_execute)

    with patch("modulo.api.routes.hitl.resolve_gate_descriptions", new=AsyncMock(return_value={})):
        resp = http.get("/api/v1/hitl/gates?status=approved")

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["total"] == 1
    item = body["items"][0]
    assert item["decision"] == "approved"
    assert item["decision_at"] is not None
    assert item["label"] == "Deploy gate"
    assert item["pipeline_name"] == "Reviewer Pipeline"
    assert item["claimed_by_name"] == "Alice Reviewer"
    assert item["claimed_by_me"] is True


@pytest.mark.parametrize(("exc", "expected"), [(_PROG, 501), (_SQL, 503), (RuntimeError("kaboom"), 500)])
def test_list_org_gates_error_mapping(client: tuple[TestClient, AsyncMock], exc: Exception, expected: int) -> None:
    http, session = client
    session.execute = AsyncMock(side_effect=exc)
    resp = http.get("/api/v1/hitl/gates")

    assert resp.status_code == expected, resp.text
