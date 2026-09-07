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
from modulo.auth.dependencies import get_current_user
from modulo.auth.jwt import AuthenticatedPrincipal
from modulo.core.hitl_manager import (
    AlreadyClaimedError,
    ClaimTokenExpiredError,
    ClaimTokenInvalidError,
    DecisionPayloadError,
    GateAlreadyDecidedError,
    GateNotFoundError,
)
from modulo.settings import Settings, get_settings
from tests.unit.api.mock_session import configure_mock_session

_VALID_32 = "a" * 32
_ORG_ID = uuid.UUID("00000000-0000-0000-0000-000000000001")
_USER_ID = uuid.UUID("00000000-0000-0000-0000-000000000002")
_RUN_ID = uuid.UUID("00000000-0000-0000-0000-000000000100")

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


@pytest.mark.parametrize(("exc", "expected"), [(_PROG, 501), (RuntimeError("kaboom"), 500)])
def test_list_org_pending_gates_error_mapping(
    client: tuple[TestClient, AsyncMock], exc: Exception, expected: int
) -> None:
    http, _session = client
    with patch("modulo.api.routes.hitl.HITLManager.list_pending", new=AsyncMock(side_effect=exc)):
        resp = http.get("/api/v1/hitl/pending")

    assert resp.status_code == expected, resp.text
