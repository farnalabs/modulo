"""Unit tests: SQLAlchemyError→503 and NotTeamMemberError→403 on HITL API routes."""

import uuid
from collections.abc import AsyncGenerator, Generator
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.exc import SQLAlchemyError

from modulo.api.dependencies import _get_engine, get_db_session, get_plan_context
from modulo.api.main import app
from modulo.api.routes.hitl import HumanOnlyDenied, _emit_human_only_denial_audit
from modulo.auth.dependencies import get_current_user
from modulo.auth.jwt import AuthenticatedPrincipal
from modulo.core.hitl_manager import NotTeamMemberError, RunNotAwaitingError
from modulo.db.crud.hitl_gate_config import EVENT_HUMAN_ONLY_DENIED, MSG_HUMAN_ONLY_DENY, MSG_HUMAN_ONLY_UNRESOLVED
from modulo.settings import Settings, get_settings
from tests.unit.api.mock_session import configure_mock_session

_VALID_32 = "a" * 32
_ORG_ID = uuid.UUID("00000000-0000-0000-0000-000000000001")
_USER_ID = uuid.UUID("00000000-0000-0000-0000-000000000002")
_RUN_ID = uuid.UUID("00000000-0000-0000-0000-000000000100")


def _make_settings() -> Settings:
    return Settings(
        database_url="postgresql+asyncpg://localhost/test",
        secret_key=_VALID_32,
        fernet_key=_VALID_32,
        modulo_admin_password="testpass",
    )


@pytest.fixture
def client() -> Generator[TestClient, None, None]:
    mock_session = AsyncMock()
    configure_mock_session(mock_session)
    begin_cm = AsyncMock()
    begin_cm.__aenter__ = AsyncMock(return_value=None)
    begin_cm.__aexit__ = AsyncMock(return_value=False)
    mock_session.begin = MagicMock(return_value=begin_cm)

    async def override_session() -> AsyncGenerator[AsyncMock, None]:
        yield mock_session

    mock_plan = MagicMock()
    mock_plan.feature_enabled.return_value = True
    app.dependency_overrides[get_settings] = _make_settings
    app.dependency_overrides[get_plan_context] = lambda: mock_plan
    app.dependency_overrides[get_db_session] = override_session
    app.dependency_overrides[_get_engine] = lambda: MagicMock()
    app.dependency_overrides[get_current_user] = lambda: AuthenticatedPrincipal(
        username="user",
        organisation_id=_ORG_ID,
        account_id=_USER_ID,
        org_role="admin",
    )
    yield TestClient(app)
    app.dependency_overrides.clear()


class TestClaimGateSQLAlchemyError:
    @patch("modulo.api.routes.hitl.HITLManager.claim", new=AsyncMock(side_effect=SQLAlchemyError("mock", {}, "")))
    def test_claim_gate_returns_503(self, client: TestClient) -> None:
        resp = client.post(
            f"/api/v1/runs/{_RUN_ID}/hitl/gate-1/claim",
            json={"expiry_minutes": 15},
        )
        assert resp.status_code == 503


class TestClaimGateKeepsParkedRunParked:
    """FAR-604 D2/D3 + qa F7: claiming the gate of a ``hitl_parked`` run must
    NOT transition the run to ``claimed`` — a claim is not a decision, and
    the claim-expiry sweep would otherwise un-park the run via the
    claimed→awaiting_human reset. The un-park happens at decision time
    (``HITLManager._decide``). FAR-612 rebased the guard onto the fenced
    transition authority: the flip is guarded INSIDE the conditional UPDATE
    (``allowed_from={"awaiting_human"}``) — ``hitl_parked`` is not an
    admissible source state, so a parked run is never flipped and the park
    sweep's concurrent commit is fenced out (TOCTOU-safe, same property the
    old ``update_run_status(not_status=...)`` guard provided)."""

    @staticmethod
    def _claim_gate_response() -> MagicMock:
        gate = MagicMock()
        gate.run_id = _RUN_ID
        gate.gate_id = "gate-1"
        gate.claim_token = "tok-123"
        gate.expires_at = datetime.now(UTC)
        return gate

    def test_claim_write_is_guarded_against_parked(self, client: TestClient) -> None:
        transition = AsyncMock(return_value=True)
        with (
            patch(
                "modulo.api.routes.hitl.HITLManager",
                return_value=MagicMock(claim=AsyncMock(return_value=self._claim_gate_response())),
            ),
            patch("modulo.api.routes.hitl.transition_run", new=transition),
        ):
            resp = client.post(
                f"/api/v1/runs/{_RUN_ID}/hitl/gate-1/claim",
                json={"expiry_minutes": 15},
            )

        assert resp.status_code == 200
        assert resp.json()["claim_token"] == "tok-123"
        transition.assert_awaited_once()
        assert transition.await_args.args[1] == _RUN_ID
        assert transition.await_args.kwargs["target_status"] == "claimed"
        # The parked guard rides INSIDE the conditional UPDATE — the route
        # never pre-reads, and "hitl_parked" must never be an admissible
        # source state for the claim's status flip.
        assert "hitl_parked" not in transition.await_args.kwargs["allowed_from"]


class TestClaimGateNotTeamMemberError:
    @patch(
        "modulo.api.routes.hitl.HITLManager.claim",
        new=AsyncMock(side_effect=NotTeamMemberError(_RUN_ID, "gate-1", _ORG_ID, _USER_ID)),
    )
    def test_claim_gate_returns_403_for_non_team_member(self, client: TestClient) -> None:
        resp = client.post(
            f"/api/v1/runs/{_RUN_ID}/hitl/gate-1/claim",
            json={"expiry_minutes": 15},
        )
        assert resp.status_code == 403


class TestClaimGateRunNotAwaitingError:
    """FAR-612: a gate whose run is not awaiting_human must 409 with the run's
    actual status -- a terminal run is never flipped to "claimed" by a stale
    gate claim."""

    @patch(
        "modulo.api.routes.hitl.HITLManager.claim",
        new=AsyncMock(side_effect=RunNotAwaitingError(_RUN_ID, "complete")),
    )
    def test_claim_gate_returns_409_when_run_not_awaiting(self, client: TestClient) -> None:
        resp = client.post(
            f"/api/v1/runs/{_RUN_ID}/hitl/gate-1/claim",
            json={"expiry_minutes": 15},
        )
        assert resp.status_code == 409
        detail = resp.json()["detail"]
        assert "not awaiting a human decision" in detail
        assert "status: complete" in detail


class TestClaimGateRunStatusFence:
    """FAR-612: the claim route's run-status flip must go through the fenced
    transition authority guarded on ``allowed_from={"awaiting_human"}`` so a
    run that goes terminal between claim()'s status pre-check and the flip is
    never clobbered to "claimed"."""

    @staticmethod
    def _claimed_gate() -> MagicMock:
        gate = MagicMock()
        gate.claim_token = "tok-123"
        gate.run_id = _RUN_ID
        gate.gate_id = "gate-1"
        gate.expires_at = datetime(2026, 1, 1, tzinfo=UTC)
        return gate

    def test_claim_flip_is_fenced_to_awaiting_human(self, client: TestClient) -> None:
        transition = AsyncMock(return_value=True)
        with (
            patch(
                "modulo.api.routes.hitl.HITLManager",
                return_value=MagicMock(claim=AsyncMock(return_value=self._claimed_gate())),
            ),
            patch("modulo.api.routes.hitl.transition_run", new=transition),
        ):
            resp = client.post(
                f"/api/v1/runs/{_RUN_ID}/hitl/gate-1/claim",
                json={"expiry_minutes": 15},
            )

        assert resp.status_code == 200
        transition.assert_awaited_once()
        assert transition.await_args.kwargs["target_status"] == "claimed"
        assert transition.await_args.kwargs["allowed_from"] == frozenset({"awaiting_human"})

    def test_claim_still_succeeds_when_status_flip_fences_out(self, client: TestClient) -> None:
        """Fenced-miss degradation: the gate claim stands (200 + token); the
        run keeps its terminal status and the claim token expires unused."""
        transition = AsyncMock(return_value=False)
        with (
            patch(
                "modulo.api.routes.hitl.HITLManager",
                return_value=MagicMock(claim=AsyncMock(return_value=self._claimed_gate())),
            ),
            patch("modulo.api.routes.hitl.transition_run", new=transition),
        ):
            resp = client.post(
                f"/api/v1/runs/{_RUN_ID}/hitl/gate-1/claim",
                json={"expiry_minutes": 15},
            )

        assert resp.status_code == 200
        assert resp.json()["claim_token"] == "tok-123"


class TestApproveGateSQLAlchemyError:
    @patch("modulo.api.routes.hitl.resolve_hitl_gate_config", new=AsyncMock(return_value=None))
    @patch("modulo.api.routes.hitl.HITLManager.approve", new=AsyncMock(side_effect=SQLAlchemyError("mock", {}, "")))
    def test_approve_gate_returns_503(self, client: TestClient) -> None:
        resp = client.post(
            f"/api/v1/runs/{_RUN_ID}/hitl/gate-1/approve",
            json={"claim_token": "test-token", "notes": "approved"},
        )
        assert resp.status_code == 503


class TestApproveGateAtSandboxCapacity:
    @patch("modulo.api.routes.hitl.resolve_hitl_gate_config", new=AsyncMock(return_value=None))
    @patch(
        "modulo.api.routes.hitl.org_sandbox_capacity_free",
        new=AsyncMock(return_value=False),
    )
    def test_approve_gate_at_capacity_returns_409(self, client: TestClient) -> None:
        """At org sandbox capacity the gate is left undecided — 409, not 202."""
        resp = client.post(
            f"/api/v1/runs/{_RUN_ID}/hitl/gate-1/approve",
            json={"claim_token": "test-token", "notes": "approved"},
        )
        assert resp.status_code == 409
        assert "gate left undecided" in resp.json()["detail"]


class TestResumeSandboxCapacityExceeded:
    """Executor-level (post pre-check) SandboxCapacityExceededError → 409 on each
    of the four resume routes. Regression for the reviewer finding: the route's
    fast-fail pre-check was mocked, but the executor's atomic gate (FAR-1306) was
    never covered at the route layer."""

    @staticmethod
    def _executor_raising() -> MagicMock:
        from modulo.core.pipeline_engine.executor import SandboxCapacityExceededError

        executor = MagicMock()
        executor.resume = AsyncMock(
            side_effect=SandboxCapacityExceededError(_ORG_ID),
        )
        return executor

    @pytest.mark.parametrize(
        ("path", "payload", "hitl_method"),
        [
            ("hitl/gate-1/approve", {"claim_token": "test-token", "notes": "approved"}, "approve"),
            (
                "hitl/gate-1/approve-with-modification",
                {"claim_token": "test-token", "modified_output": {"key": "value"}, "notes": "mod"},
                "approve_with_modification",
            ),
            (
                "hitl/gate-1/deliver-manual",
                {"claim_token": "test-token", "output": {"result": "ok"}},
                "deliver_manual",
            ),
            (
                "manual/gate-1/submit",
                {"claim_token": "test-token", "output": {"result": "ok"}},
                "approve",
            ),
        ],
    )
    def test_resume_capacity_exceeded_returns_409(
        self,
        client: TestClient,
        path: str,
        payload: dict[str, Any],
        hitl_method: str,
    ) -> None:
        executor = self._executor_raising()
        with (
            patch("modulo.api.routes.hitl.resolve_hitl_gate_config", new=AsyncMock(return_value=None)),
            patch("modulo.api.routes.hitl.org_sandbox_capacity_free", new=AsyncMock(return_value=True)),
            patch(
                f"modulo.api.routes.hitl.HITLManager.{hitl_method}",
                new=AsyncMock(),
            ),
            patch("modulo.api.routes.hitl._build_resume_executor", return_value=executor),
        ):
            resp = client.post(f"/api/v1/runs/{_RUN_ID}/{path}", json=payload)

        assert resp.status_code == 409
        assert executor.resume.await_count == 1


class TestResumeDataGateStamp:
    """FAR-541: every decision payload is STAMPED with the gate (or manual
    node) id it resolves — both on the persisted ``decision_payload`` (the
    reconcile reconstructs its resume from it) and on the ``executor.resume``
    injection (the per-gate consumer verifies the stamp)."""

    @pytest.mark.parametrize(
        ("path", "payload", "hitl_method"),
        [
            ("hitl/gate-1/approve", {"claim_token": "tok", "notes": "n"}, "approve"),
            (
                "hitl/gate-1/approve-with-modification",
                {"claim_token": "tok", "modified_output": {"k": "v"}},
                "approve_with_modification",
            ),
            ("hitl/gate-1/reject", {"claim_token": "tok", "reason": "not good"}, "reject"),
            ("hitl/gate-1/deliver-manual", {"claim_token": "tok", "output": {"o": 1}}, "deliver_manual"),
            ("manual/node-1/submit", {"claim_token": "tok", "output": {"o": 1}}, "approve"),
        ],
    )
    def test_route_stamps_decision_payload_and_resume_data(
        self,
        client: TestClient,
        path: str,
        payload: dict[str, Any],
        hitl_method: str,
    ) -> None:
        expected_stamp = "node-1" if path.startswith("manual/") else "gate-1"
        manager = MagicMock()
        method_mock = AsyncMock(return_value=MagicMock())
        setattr(manager, hitl_method, method_mock)
        executor = MagicMock()
        executor.resume = AsyncMock()
        with (
            patch("modulo.api.routes.hitl.resolve_hitl_gate_config", new=AsyncMock(return_value=None)),
            patch("modulo.api.routes.hitl.org_sandbox_capacity_free", new=AsyncMock(return_value=True)),
            patch("modulo.api.routes.hitl.HITLManager", return_value=manager),
            patch("modulo.api.routes.hitl._build_resume_executor", return_value=executor),
        ):
            resp = client.post(f"/api/v1/runs/{_RUN_ID}/{path}", json=payload)

        assert resp.status_code == 200
        method_mock.assert_awaited_once()
        persisted = method_mock.await_args.kwargs["decision_payload"]
        assert persisted["gate_id"] == expected_stamp
        injected = executor.resume.await_args.kwargs["resume_data"]
        assert injected == persisted
        assert injected["gate_id"] == expected_stamp


class TestApproveWithModificationSQLAlchemyError:
    @patch("modulo.api.routes.hitl.resolve_hitl_gate_config", new=AsyncMock(return_value=None))
    @patch(
        "modulo.api.routes.hitl.HITLManager.approve_with_modification",
        new=AsyncMock(side_effect=SQLAlchemyError("mock", {}, "")),
    )
    def test_approve_with_modification_returns_503(self, client: TestClient) -> None:
        resp = client.post(
            f"/api/v1/runs/{_RUN_ID}/hitl/gate-1/approve-with-modification",
            json={"claim_token": "test-token", "modified_output": {"key": "value"}, "notes": "modified"},
        )
        assert resp.status_code == 503


class TestRejectGateSQLAlchemyError:
    @patch("modulo.api.routes.hitl.HITLManager.reject", new=AsyncMock(side_effect=SQLAlchemyError("mock", {}, "")))
    def test_reject_gate_returns_503(self, client: TestClient) -> None:
        resp = client.post(
            f"/api/v1/runs/{_RUN_ID}/hitl/gate-1/reject",
            json={"claim_token": "test-token", "reason": "not needed"},
        )
        assert resp.status_code == 503


class TestDeliverManualSQLAlchemyError:
    @patch("modulo.api.routes.hitl.resolve_hitl_gate_config", new=AsyncMock(return_value=None))
    @patch(
        "modulo.api.routes.hitl.HITLManager.deliver_manual",
        new=AsyncMock(side_effect=SQLAlchemyError("mock", {}, "")),
    )
    def test_deliver_manual_returns_503(self, client: TestClient) -> None:
        resp = client.post(
            f"/api/v1/runs/{_RUN_ID}/hitl/gate-1/deliver-manual",
            json={"claim_token": "test-token", "output": {"result": "ok"}},
        )
        assert resp.status_code == 503


class TestSubmitManualSQLAlchemyError:
    @patch("modulo.api.routes.hitl.resolve_hitl_gate_config", new=AsyncMock(return_value=None))
    @patch("modulo.api.routes.hitl.HITLManager.approve", new=AsyncMock(side_effect=SQLAlchemyError("mock", {}, "")))
    def test_submit_manual_returns_503(self, client: TestClient) -> None:
        resp = client.post(
            f"/api/v1/runs/{_RUN_ID}/manual/gate-1/submit",
            json={"claim_token": "test-token", "output": {"result": "ok"}},
        )
        assert resp.status_code == 503


class TestSubmitManualNotTeamMemberError:
    @patch("modulo.api.routes.hitl.resolve_hitl_gate_config", new=AsyncMock(return_value=None))
    @patch(
        "modulo.api.routes.hitl.HITLManager.approve",
        new=AsyncMock(side_effect=NotTeamMemberError(_RUN_ID, "gate-1", _ORG_ID, _USER_ID)),
    )
    def test_submit_manual_returns_403_for_non_team_member(self, client: TestClient) -> None:
        resp = client.post(
            f"/api/v1/runs/{_RUN_ID}/manual/gate-1/submit",
            json={"claim_token": "test-token", "output": {"result": "ok"}},
        )
        assert resp.status_code == 403


class TestListRunPendingGatesSQLAlchemyError:
    @patch("modulo.api.routes.hitl.get_run", new=AsyncMock(side_effect=SQLAlchemyError("mock", {}, "")))
    def test_list_run_pending_gates_returns_503(self, client: TestClient) -> None:
        resp = client.get(f"/api/v1/runs/{_RUN_ID}/hitl/pending")
        assert resp.status_code == 503


class TestListOrgPendingGatesSQLAlchemyError:
    @patch(
        "modulo.api.routes.hitl.HITLManager.list_pending",
        new=AsyncMock(side_effect=SQLAlchemyError("mock", "", "")),
    )
    def test_list_org_pending_gates_returns_503(self, client: TestClient) -> None:
        resp = client.get("/api/v1/hitl/pending")
        assert resp.status_code == 503


class TestGateResponseLabel:
    def test_gate_to_response_passes_label_through(self) -> None:
        from modulo.api.routes.hitl import _gate_to_response

        claim = MagicMock()
        claim.run_id = _RUN_ID
        claim.gate_id = "hitl_gate_planner_deploy"
        claim.pipeline_id = uuid.uuid4()
        claim.account_id = _USER_ID
        claim.claimed_at = None
        claim.expires_at = None
        claim.decision = None
        claim.decision_at = None

        resp = _gate_to_response(claim, pipeline_name="My Pipeline", label="Deploy gate")

        assert resp.gate_id == "hitl_gate_planner_deploy"
        assert resp.label == "Deploy gate"

    def test_gate_to_response_label_defaults_to_none(self) -> None:
        from modulo.api.routes.hitl import _gate_to_response

        claim = MagicMock()
        claim.run_id = _RUN_ID
        claim.gate_id = "hitl_gate_planner_deploy"
        claim.pipeline_id = uuid.uuid4()
        claim.account_id = _USER_ID
        claim.claimed_at = None
        claim.expires_at = None
        claim.decision = None
        claim.decision_at = None

        resp = _gate_to_response(claim)

        assert resp.label is None

    def test_build_gate_label_map_from_snapshot_edges(self) -> None:
        from modulo.api.routes.hitl import _build_gate_label_map

        graph = {
            "edges": [
                {"source": "planner", "target": "deploy", "hitl_gate_config": {"label": "Deploy gate"}},
                {"source_node_id": "a", "target_node_id": "b", "hitl_gate_config": {"label": "Review gate"}},
                {"source": "e", "target": "f", "hitl_gate_config": {"label": ""}},
                {"source": "g", "target": "h"},
                {"hitl_gate_config": {"label": "no-edge-keys"}},
                "not-a-dict",
            ]
        }

        assert _build_gate_label_map(graph) == {
            "hitl_gate_planner_deploy": "Deploy gate",
            "hitl_gate_a_b": "Review gate",
        }


class TestListRunPendingGatesLabelResolution:
    def test_list_run_pending_gates_resolves_gate_label(self, client: TestClient) -> None:
        from collections.abc import AsyncGenerator

        mock_session = AsyncMock()
        configure_mock_session(mock_session, allow_empty_execute=True)
        begin_cm = AsyncMock()
        begin_cm.__aenter__ = AsyncMock(return_value=None)
        begin_cm.__aexit__ = AsyncMock(return_value=False)
        mock_session.begin = MagicMock(return_value=begin_cm)
        mock_session.get = AsyncMock(return_value=None)

        run = MagicMock()
        run.id = _RUN_ID
        run.snapshot_id = uuid.uuid4()

        snapshot = MagicMock()
        snapshot.graph_json = {
            "nodes": [],
            "edges": [{"source": "planner", "target": "deploy", "hitl_gate_config": {"label": "Deploy gate"}}],
        }

        claim = MagicMock()
        claim.run_id = _RUN_ID
        claim.gate_id = "hitl_gate_planner_deploy"
        claim.pipeline_id = uuid.uuid4()
        claim.account_id = _USER_ID
        claim.claimed_at = None
        claim.expires_at = None
        claim.decision = None
        claim.decision_at = None

        def _execute(stmt: object, *args: object, **kwargs: object) -> MagicMock:
            result = MagicMock()
            if "pipeline_snapshots" in str(stmt):
                result.scalar_one_or_none.return_value = snapshot
            else:
                result.scalars.return_value = [claim]
            return result

        mock_session.execute = AsyncMock(side_effect=_execute)

        async def override_session() -> AsyncGenerator[AsyncMock, None]:
            yield mock_session

        app.dependency_overrides[get_db_session] = override_session
        try:
            with patch("modulo.api.routes.hitl.get_run", new=AsyncMock(return_value=run)):
                resp = client.get(f"/api/v1/runs/{_RUN_ID}/hitl/pending")
        finally:
            app.dependency_overrides.clear()

        assert resp.status_code == 200
        gates = resp.json()["gates"]
        assert gates[0]["gate_id"] == "hitl_gate_planner_deploy"
        assert gates[0]["label"] == "Deploy gate"

    def test_list_run_pending_gates_label_none_without_snapshot(self, client: TestClient) -> None:
        from collections.abc import AsyncGenerator

        mock_session = AsyncMock()
        configure_mock_session(mock_session, allow_empty_execute=True)
        begin_cm = AsyncMock()
        begin_cm.__aenter__ = AsyncMock(return_value=None)
        begin_cm.__aexit__ = AsyncMock(return_value=False)
        mock_session.begin = MagicMock(return_value=begin_cm)
        mock_session.get = AsyncMock(return_value=None)

        run = MagicMock()
        run.id = _RUN_ID
        run.snapshot_id = None

        claim = MagicMock()
        claim.run_id = _RUN_ID
        claim.gate_id = "hitl_gate_planner_deploy"
        claim.pipeline_id = uuid.uuid4()
        claim.account_id = _USER_ID
        claim.claimed_at = None
        claim.expires_at = None
        claim.decision = None
        claim.decision_at = None

        def _execute(stmt: object, *args: object, **kwargs: object) -> MagicMock:
            result = MagicMock()
            result.scalars.return_value = [claim]
            return result

        mock_session.execute = AsyncMock(side_effect=_execute)

        async def override_session() -> AsyncGenerator[AsyncMock, None]:
            yield mock_session

        app.dependency_overrides[get_db_session] = override_session
        try:
            with patch("modulo.api.routes.hitl.get_run", new=AsyncMock(return_value=run)):
                resp = client.get(f"/api/v1/runs/{_RUN_ID}/hitl/pending")
        finally:
            app.dependency_overrides.clear()

        assert resp.status_code == 200
        assert resp.json()["gates"][0]["label"] is None


# ---------------------------------------------------------------------------
# FAR-610: human_only enforcement on the REST decision routes
# ---------------------------------------------------------------------------

# FAR-634 review: the denial detail is the shared MSG_HUMAN_ONLY_DENY constant
# (single-sourced in db.crud.hitl_gate_config) — REST and MCP must deny with
# the SAME wording.
_HUMAN_ONLY_DETAIL = MSG_HUMAN_ONLY_DENY
_UNRESOLVABLE_DETAIL = MSG_HUMAN_ONLY_UNRESOLVED
_SRC_ID = uuid.UUID("00000000-0000-0000-0000-00000000000a")
_TGT_ID = uuid.UUID("00000000-0000-0000-0000-00000000000b")
_SNAPSHOT_ID = uuid.UUID("00000000-0000-0000-0000-000000000004")
_GATE_ID = f"hitl_gate_{_SRC_ID}_{_TGT_ID}"


def _resume_executor() -> MagicMock:
    executor = MagicMock()
    executor.resume = AsyncMock()
    return executor


def _hitl_session(
    run: MagicMock,
    snapshot: object = None,
    edge: object = None,
    pipeline_nodes: object = None,
    claim_row: object = None,
) -> AsyncMock:
    """Session double stubbing the resolver's queries (runs/snapshots/edges/
    pipelines) plus the fail-closed claim lookup and the authz-kill-switch and
    RLS set_config reads that fire on every request."""
    mock_session = AsyncMock()
    configure_mock_session(mock_session)
    begin_cm = AsyncMock()
    begin_cm.__aenter__ = AsyncMock(return_value=None)
    begin_cm.__aexit__ = AsyncMock(return_value=False)
    mock_session.begin = MagicMock(return_value=begin_cm)

    def _execute(stmt: object, *args: object, **kwargs: object) -> MagicMock:
        result = MagicMock()
        text = str(stmt)
        if "set_config" in text:
            result.scalar.return_value = None
        elif "authz_enforce" in text:
            result.scalar_one_or_none.return_value = None
        elif "FROM runs" in text:
            result.scalar_one_or_none.return_value = run
        elif "pipeline_snapshots" in text:
            result.scalar_one_or_none.return_value = snapshot
        elif "pipeline_edges" in text:
            result.scalar_one_or_none.return_value = edge
        elif "pipelines" in text:
            result.scalar_one_or_none.return_value = pipeline_nodes
        elif "hitl_claims" in text:
            result.scalar_one_or_none.return_value = claim_row
        else:
            raise AssertionError(f"Unexpected query in HITL route flow: {text}")
        return result

    mock_session.execute = AsyncMock(side_effect=_execute)
    return mock_session


def _make_hitl_run(*, snapshot_id: uuid.UUID | None = _SNAPSHOT_ID) -> MagicMock:
    run = MagicMock()
    run.id = _RUN_ID
    run.pipeline_id = uuid.uuid4()
    run.snapshot_id = snapshot_id
    return run


def _human_only_snapshot(*, human_only: bool = True) -> MagicMock:
    snapshot = MagicMock()
    snapshot.graph_json = {
        "nodes": [],
        "edges": [
            {
                "source": str(_SRC_ID),
                "target": str(_TGT_ID),
                "hitl_gate_config": {"human_only": human_only},
            }
        ],
    }
    return snapshot


def _override_principal(via_api_key: bool, client_kind: str = "browser") -> None:
    app.dependency_overrides[get_current_user] = lambda: AuthenticatedPrincipal(
        username="user",
        organisation_id=_ORG_ID,
        account_id=_USER_ID,
        org_role="admin",
        via_api_key=via_api_key,
        client_kind=client_kind,
    )


class TestHumanOnlyRestEnforcement:
    """FAR-610: human_only gates deny API-key principals on the resume routes
    (approve / approve-with-modification / deliver-manual / submit-manual);
    browser JWTs pass. reject stays allowed for every client."""

    @pytest.fixture(autouse=True)
    def _no_real_audit_db(self) -> Generator[None, None, None]:
        """FAR-634: a denial now emits the ``hitl.human_only_denied`` audit
        event through a FRESH engine/session. In unit tests there is no
        database — fail the engine lookup immediately so the emit's
        failure-isolation path runs deterministically (the 403 outcome is
        never affected; that isolation is itself part of the contract)."""
        with patch(
            "modulo.api.routes.hitl.get_or_create_engine",
            side_effect=RuntimeError("no db in unit tests"),
        ):
            yield

    @staticmethod
    def _install_session(
        run: MagicMock,
        snapshot: object = None,
        edge: object = None,
        pipeline_nodes: object = None,
        claim_row: object = None,
    ) -> None:
        mock_session = _hitl_session(
            run, snapshot=snapshot, edge=edge, pipeline_nodes=pipeline_nodes, claim_row=claim_row
        )

        async def override_session() -> AsyncGenerator[AsyncMock, None]:
            yield mock_session

        app.dependency_overrides[get_db_session] = override_session

    def test_approve_human_only_api_key_returns_403(self, client: TestClient) -> None:
        approve = AsyncMock()
        with (
            patch("modulo.api.routes.hitl.org_sandbox_capacity_free", new=AsyncMock(return_value=True)),
            patch("modulo.api.routes.hitl.HITLManager") as mgr_cls,
            patch("modulo.api.routes.hitl._build_resume_executor", return_value=_resume_executor()),
        ):
            mgr_cls.return_value.approve = approve
            _override_principal(via_api_key=True)
            self._install_session(_make_hitl_run(), snapshot=_human_only_snapshot())
            resp = client.post(
                f"/api/v1/runs/{_RUN_ID}/hitl/{_GATE_ID}/approve",
                json={"claim_token": "tok"},
            )

        assert resp.status_code == 403
        assert resp.json()["detail"] == _HUMAN_ONLY_DETAIL
        approve.assert_not_called()

    def test_approve_human_only_api_key_403_via_live_edge_fallback(self, client: TestClient) -> None:
        """Legacy run without a snapshot: the live-edge fallback still resolves
        the gate config by topology and blocks the API-key principal."""
        live_edge = MagicMock()
        live_edge.hitl_gate_config = {"human_only": True}
        with patch("modulo.api.routes.hitl.HITLManager"):
            _override_principal(via_api_key=True)
            self._install_session(_make_hitl_run(snapshot_id=None), snapshot=None, edge=live_edge)
            resp = client.post(
                f"/api/v1/runs/{_RUN_ID}/hitl/{_GATE_ID}/approve",
                json={"claim_token": "tok"},
            )

        assert resp.status_code == 403
        assert resp.json()["detail"] == _HUMAN_ONLY_DETAIL

    def test_approve_human_only_browser_jwt_passes_check(self, client: TestClient) -> None:
        approve = AsyncMock()
        with (
            patch("modulo.api.routes.hitl.org_sandbox_capacity_free", new=AsyncMock(return_value=True)),
            patch("modulo.api.routes.hitl.HITLManager") as mgr_cls,
            patch("modulo.api.routes.hitl._build_resume_executor", return_value=_resume_executor()),
        ):
            mgr_cls.return_value.approve = approve
            _override_principal(via_api_key=False)
            self._install_session(_make_hitl_run(), snapshot=_human_only_snapshot())
            resp = client.post(
                f"/api/v1/runs/{_RUN_ID}/hitl/{_GATE_ID}/approve",
                json={"claim_token": "tok"},
            )

        assert resp.status_code == 200
        approve.assert_awaited_once()

    def test_approve_non_human_only_api_key_allowed(self, client: TestClient) -> None:
        """No over-blocking: API-key principals may approve non-human_only gates."""
        approve = AsyncMock()
        with (
            patch("modulo.api.routes.hitl.org_sandbox_capacity_free", new=AsyncMock(return_value=True)),
            patch("modulo.api.routes.hitl.HITLManager") as mgr_cls,
            patch("modulo.api.routes.hitl._build_resume_executor", return_value=_resume_executor()),
        ):
            mgr_cls.return_value.approve = approve
            _override_principal(via_api_key=True)
            self._install_session(_make_hitl_run(), snapshot=_human_only_snapshot(human_only=False))
            resp = client.post(
                f"/api/v1/runs/{_RUN_ID}/hitl/{_GATE_ID}/approve",
                json={"claim_token": "tok"},
            )

        assert resp.status_code == 200
        approve.assert_awaited_once()

    def test_approve_unresolvable_fired_gate_api_key_returns_403(self, client: TestClient) -> None:
        """Fail closed (FAR-610 review): the gate FIRED (claim row exists) but
        its config is unresolvable — the policy cannot be verified, so the
        API-key principal is denied instead of silently allowed."""
        approve = AsyncMock()
        with patch("modulo.api.routes.hitl.HITLManager") as mgr_cls:
            mgr_cls.return_value.approve = approve
            _override_principal(via_api_key=True)
            self._install_session(_make_hitl_run(snapshot_id=None), snapshot=None, edge=None, claim_row=MagicMock())
            resp = client.post(
                f"/api/v1/runs/{_RUN_ID}/hitl/{_GATE_ID}/approve",
                json={"claim_token": "tok"},
            )

        assert resp.status_code == 403
        assert resp.json()["detail"] == _UNRESOLVABLE_DETAIL
        approve.assert_not_called()

    def test_approve_unresolvable_gate_without_claim_api_key_allowed(self, client: TestClient) -> None:
        """A parseable gate id with NO claim row never fired — the fail-closed
        check passes it through (the manager 404s it later as before)."""
        approve = AsyncMock()
        with (
            patch("modulo.api.routes.hitl.org_sandbox_capacity_free", new=AsyncMock(return_value=True)),
            patch("modulo.api.routes.hitl.HITLManager") as mgr_cls,
            patch("modulo.api.routes.hitl._build_resume_executor", return_value=_resume_executor()),
        ):
            mgr_cls.return_value.approve = approve
            _override_principal(via_api_key=True)
            self._install_session(_make_hitl_run(snapshot_id=None), snapshot=None, edge=None, claim_row=None)
            resp = client.post(
                f"/api/v1/runs/{_RUN_ID}/hitl/{_GATE_ID}/approve",
                json={"claim_token": "tok"},
            )

        assert resp.status_code == 200
        approve.assert_awaited_once()

    def test_approve_unresolvable_fired_gate_browser_jwt_allowed(self, client: TestClient) -> None:
        """Browser JWTs pass the unresolvable case — the UI is their
        enforcement surface, and the claim table is not even consulted."""
        approve = AsyncMock()
        with (
            patch("modulo.api.routes.hitl.org_sandbox_capacity_free", new=AsyncMock(return_value=True)),
            patch("modulo.api.routes.hitl.HITLManager") as mgr_cls,
            patch("modulo.api.routes.hitl._build_resume_executor", return_value=_resume_executor()),
        ):
            mgr_cls.return_value.approve = approve
            _override_principal(via_api_key=False)
            self._install_session(_make_hitl_run(snapshot_id=None), snapshot=None, edge=None, claim_row=MagicMock())
            resp = client.post(
                f"/api/v1/runs/{_RUN_ID}/hitl/{_GATE_ID}/approve",
                json={"claim_token": "tok"},
            )

        assert resp.status_code == 200
        approve.assert_awaited_once()

    def test_approve_with_modification_human_only_api_key_returns_403(self, client: TestClient) -> None:
        modify = AsyncMock()
        with patch("modulo.api.routes.hitl.HITLManager") as mgr_cls:
            mgr_cls.return_value.approve_with_modification = modify
            _override_principal(via_api_key=True)
            self._install_session(_make_hitl_run(), snapshot=_human_only_snapshot())
            resp = client.post(
                f"/api/v1/runs/{_RUN_ID}/hitl/{_GATE_ID}/approve-with-modification",
                json={"claim_token": "tok", "modified_output": {"k": "v"}},
            )

        assert resp.status_code == 403
        assert resp.json()["detail"] == _HUMAN_ONLY_DETAIL
        modify.assert_not_called()

    def test_deliver_manual_human_only_api_key_returns_403(self, client: TestClient) -> None:
        deliver = AsyncMock()
        with patch("modulo.api.routes.hitl.HITLManager") as mgr_cls:
            mgr_cls.return_value.deliver_manual = deliver
            _override_principal(via_api_key=True)
            self._install_session(_make_hitl_run(), snapshot=_human_only_snapshot())
            resp = client.post(
                f"/api/v1/runs/{_RUN_ID}/hitl/{_GATE_ID}/deliver-manual",
                json={"claim_token": "tok", "output": {"result": "ok"}},
            )

        assert resp.status_code == 403
        assert resp.json()["detail"] == _HUMAN_ONLY_DETAIL
        deliver.assert_not_called()

    def test_submit_manual_node_id_api_key_allowed(self, client: TestClient) -> None:
        """submit-manual's gate_id is a manual-NODE id (not hitl_gate_*); the
        human_only check resolves None and never over-blocks."""
        approve = AsyncMock()
        with (
            patch("modulo.api.routes.hitl.org_sandbox_capacity_free", new=AsyncMock(return_value=True)),
            patch("modulo.api.routes.hitl.HITLManager") as mgr_cls,
            patch("modulo.api.routes.hitl._build_resume_executor", return_value=_resume_executor()),
        ):
            mgr_cls.return_value.approve = approve
            _override_principal(via_api_key=True)
            self._install_session(_make_hitl_run(), snapshot=_human_only_snapshot())
            resp = client.post(
                f"/api/v1/runs/{_RUN_ID}/manual/node-1/submit",
                json={"claim_token": "tok", "output": {"o": 1}},
            )

        assert resp.status_code == 200
        approve.assert_awaited_once()

    def test_reject_human_only_api_key_still_allowed(self, client: TestClient) -> None:
        """reject is the safe direction — allowed for every client, even with
        a human_only gate."""
        reject = AsyncMock()
        with (
            patch("modulo.api.routes.hitl.HITLManager") as mgr_cls,
            patch("modulo.api.routes.hitl._build_resume_executor", return_value=_resume_executor()),
        ):
            mgr_cls.return_value.reject = reject
            _override_principal(via_api_key=True)
            self._install_session(_make_hitl_run(), snapshot=_human_only_snapshot())
            resp = client.post(
                f"/api/v1/runs/{_RUN_ID}/hitl/{_GATE_ID}/reject",
                json={"claim_token": "tok", "reason": "not good"},
            )

        assert resp.status_code == 200
        reject.assert_awaited_once()


class TestHumanOnlyClaimEnforcement:
    """FAR-609: claim is human_only — a non-browser credential can neither
    CLAIM nor decide a human_only gate. The REST claim route runs the same
    fail-closed policy as the decision routes; the denial carries the
    ``hitl.human_only_denied`` audit event (action="claim")."""

    @pytest.fixture(autouse=True)
    def _no_real_audit_db(self) -> Generator[None, None, None]:
        with patch(
            "modulo.api.routes.hitl.get_or_create_engine",
            side_effect=RuntimeError("no db in unit tests"),
        ):
            yield

    @staticmethod
    def _install_session(
        run: MagicMock,
        snapshot: object = None,
        edge: object = None,
        pipeline_nodes: object = None,
        claim_row: object = None,
    ) -> None:
        mock_session = _hitl_session(
            run, snapshot=snapshot, edge=edge, pipeline_nodes=pipeline_nodes, claim_row=claim_row
        )

        async def override_session() -> AsyncGenerator[AsyncMock, None]:
            yield mock_session

        app.dependency_overrides[get_db_session] = override_session

    @staticmethod
    def _gate_claim_mock() -> MagicMock:
        gate = MagicMock()
        gate.run_id = _RUN_ID
        gate.gate_id = _GATE_ID
        gate.claim_token = "tok-claim"
        gate.expires_at = datetime.now(UTC) + timedelta(minutes=15)
        return gate

    @staticmethod
    def _untagged_config_snapshot() -> MagicMock:
        """A snapshot whose gate config has NO explicit ``human_only`` key."""
        snapshot = MagicMock()
        snapshot.graph_json = {
            "nodes": [],
            "edges": [
                {
                    "source": str(_SRC_ID),
                    "target": str(_TGT_ID),
                    "hitl_gate_config": {"label": "Legacy gate"},
                }
            ],
        }
        return snapshot

    def test_claim_human_only_api_key_returns_403(self, client: TestClient) -> None:
        claim = AsyncMock()
        with (
            patch("modulo.api.routes.hitl.HITLManager") as mgr_cls,
            patch("modulo.api.routes.hitl.transition_run", new=AsyncMock()),
        ):
            mgr_cls.return_value.claim = claim
            _override_principal(via_api_key=True)
            self._install_session(_make_hitl_run(), snapshot=_human_only_snapshot())
            resp = client.post(
                f"/api/v1/runs/{_RUN_ID}/hitl/{_GATE_ID}/claim",
                json={"expiry_minutes": 15},
            )

        assert resp.status_code == 403
        assert resp.json()["detail"] == _HUMAN_ONLY_DETAIL
        claim.assert_not_called()

    def test_claim_missing_human_only_key_defaults_true_api_key_returns_403(self, client: TestClient) -> None:
        """FAR-609 default flip: a gate config WITHOUT ``human_only`` is
        human-only, so an API-key claim is denied."""
        claim = AsyncMock()
        with (
            patch("modulo.api.routes.hitl.HITLManager") as mgr_cls,
            patch("modulo.api.routes.hitl.transition_run", new=AsyncMock()),
        ):
            mgr_cls.return_value.claim = claim
            _override_principal(via_api_key=True)
            self._install_session(_make_hitl_run(), snapshot=self._untagged_config_snapshot())
            resp = client.post(
                f"/api/v1/runs/{_RUN_ID}/hitl/{_GATE_ID}/claim",
                json={"expiry_minutes": 15},
            )

        assert resp.status_code == 403
        assert resp.json()["detail"] == _HUMAN_ONLY_DETAIL
        claim.assert_not_called()

    def test_claim_human_only_api_key_emits_audit_event(self, client: TestClient) -> None:
        emit = AsyncMock()
        claim = AsyncMock()
        with (
            patch("modulo.api.routes.hitl._emit_human_only_denial_audit", new=emit),
            patch("modulo.api.routes.hitl.HITLManager") as mgr_cls,
            patch("modulo.api.routes.hitl.transition_run", new=AsyncMock()),
        ):
            mgr_cls.return_value.claim = claim
            _override_principal(via_api_key=True)
            self._install_session(_make_hitl_run(), snapshot=_human_only_snapshot())
            resp = client.post(
                f"/api/v1/runs/{_RUN_ID}/hitl/{_GATE_ID}/claim",
                json={"expiry_minutes": 15},
            )

        assert resp.status_code == 403
        emit.assert_awaited_once()
        denied = emit.await_args.args[0]
        assert denied.action == "claim"
        assert denied.gate_id == _GATE_ID
        claim.assert_not_called()

    def test_claim_human_only_browser_jwt_allowed(self, client: TestClient) -> None:
        """Browser JWTs claim human_only gates — the UI is their surface."""
        claim = AsyncMock(return_value=self._gate_claim_mock())
        with (
            patch("modulo.api.routes.hitl.HITLManager") as mgr_cls,
            patch("modulo.api.routes.hitl.transition_run", new=AsyncMock(return_value=True)),
        ):
            mgr_cls.return_value.claim = claim
            _override_principal(via_api_key=False)
            self._install_session(_make_hitl_run(), snapshot=_human_only_snapshot())
            resp = client.post(
                f"/api/v1/runs/{_RUN_ID}/hitl/{_GATE_ID}/claim",
                json={"expiry_minutes": 15},
            )

        assert resp.status_code == 200
        assert resp.json()["claim_token"] == "tok-claim"
        claim.assert_awaited_once()

    def test_claim_opt_out_api_key_allowed(self, client: TestClient) -> None:
        """``human_only: false`` opts the gate out — API-key claims work."""
        claim = AsyncMock(return_value=self._gate_claim_mock())
        with (
            patch("modulo.api.routes.hitl.HITLManager") as mgr_cls,
            patch("modulo.api.routes.hitl.transition_run", new=AsyncMock(return_value=True)),
        ):
            mgr_cls.return_value.claim = claim
            _override_principal(via_api_key=True)
            self._install_session(_make_hitl_run(), snapshot=_human_only_snapshot(human_only=False))
            resp = client.post(
                f"/api/v1/runs/{_RUN_ID}/hitl/{_GATE_ID}/claim",
                json={"expiry_minutes": 15},
            )

        assert resp.status_code == 200
        claim.assert_awaited_once()

    def test_claim_unresolvable_fired_gate_api_key_returns_403(self, client: TestClient) -> None:
        """Fail closed (FAR-610 review, applied to claim): the gate FIRED but
        its config is unresolvable — the API-key claim is denied."""
        claim = AsyncMock()
        with (
            patch("modulo.api.routes.hitl.HITLManager") as mgr_cls,
            patch("modulo.api.routes.hitl.transition_run", new=AsyncMock()),
        ):
            mgr_cls.return_value.claim = claim
            _override_principal(via_api_key=True)
            self._install_session(_make_hitl_run(snapshot_id=None), snapshot=None, edge=None, claim_row=MagicMock())
            resp = client.post(
                f"/api/v1/runs/{_RUN_ID}/hitl/{_GATE_ID}/claim",
                json={"expiry_minutes": 15},
            )

        assert resp.status_code == 403
        assert resp.json()["detail"] == _UNRESOLVABLE_DETAIL
        claim.assert_not_called()


class TestHumanOnlyClientKindEnforcement:
    """FAR-634: the human_only credential gate reads the JWT's ``client_kind``
    claim, not just the API-key marker. A programmatic-class JWT is denied;
    a legacy token (no claim -> decoded browser) still passes."""

    @pytest.fixture(autouse=True)
    def _no_real_audit_db(self) -> Generator[None, None, None]:
        with patch(
            "modulo.api.routes.hitl.get_or_create_engine",
            side_effect=RuntimeError("no db in unit tests"),
        ):
            yield

    @staticmethod
    def _install_session(
        run: MagicMock,
        snapshot: object = None,
        edge: object = None,
        pipeline_nodes: object = None,
        claim_row: object = None,
    ) -> None:
        mock_session = _hitl_session(
            run, snapshot=snapshot, edge=edge, pipeline_nodes=pipeline_nodes, claim_row=claim_row
        )

        async def override_session() -> AsyncGenerator[AsyncMock, None]:
            yield mock_session

        app.dependency_overrides[get_db_session] = override_session

    def test_approve_programmatic_client_kind_jwt_returns_403(self, client: TestClient) -> None:
        """A programmatic-class JWT (the future API-key/automation mint path)
        is denied on a human_only gate — the credential class subsumes the
        API-key marker."""
        approve = AsyncMock()
        with (
            patch("modulo.api.routes.hitl.HITLManager") as mgr_cls,
            patch("modulo.api.routes.hitl._build_resume_executor", return_value=_resume_executor()),
        ):
            mgr_cls.return_value.approve = approve
            _override_principal(via_api_key=False, client_kind="programmatic")
            self._install_session(_make_hitl_run(), snapshot=_human_only_snapshot())
            resp = client.post(
                f"/api/v1/runs/{_RUN_ID}/hitl/{_GATE_ID}/approve",
                json={"claim_token": "tok"},
            )

        assert resp.status_code == 403
        assert resp.json()["detail"] == _HUMAN_ONLY_DETAIL
        approve.assert_not_called()

    def test_approve_with_modification_programmatic_client_kind_returns_403(self, client: TestClient) -> None:
        """Route-matrix completeness: the approve-with-modification decision
        route denies a programmatic-class JWT exactly like approve."""
        modify = AsyncMock()
        with patch("modulo.api.routes.hitl.HITLManager") as mgr_cls:
            mgr_cls.return_value.approve_with_modification = modify
            _override_principal(via_api_key=False, client_kind="programmatic")
            self._install_session(_make_hitl_run(), snapshot=_human_only_snapshot())
            resp = client.post(
                f"/api/v1/runs/{_RUN_ID}/hitl/{_GATE_ID}/approve-with-modification",
                json={"claim_token": "tok", "modified_output": {"k": "v"}},
            )

        assert resp.status_code == 403
        assert resp.json()["detail"] == _HUMAN_ONLY_DETAIL
        modify.assert_not_called()

    def test_approve_programmatic_jwt_denied_on_claim_stamped_config(self, client: TestClient) -> None:
        """FAR-634: the resolver returns the claim row's stamped config (O(1))
        — a programmatic JWT is denied on the stamped human_only config even
        with no snapshot to walk. (The session double returns the routed
        scalar directly — the config dict IS the stamp value.)"""
        approve = AsyncMock()
        with patch("modulo.api.routes.hitl.HITLManager") as mgr_cls:
            mgr_cls.return_value.approve = approve
            _override_principal(via_api_key=False, client_kind="programmatic")
            self._install_session(_make_hitl_run(snapshot_id=None), claim_row={"human_only": True})
            resp = client.post(
                f"/api/v1/runs/{_RUN_ID}/hitl/{_GATE_ID}/approve",
                json={"claim_token": "tok"},
            )

        assert resp.status_code == 403
        assert resp.json()["detail"] == _HUMAN_ONLY_DETAIL
        approve.assert_not_called()

    def test_approve_unknown_client_kind_fails_closed(self, client: TestClient) -> None:
        """An unknown client_kind value is NOT 'browser' — fail closed."""
        approve = AsyncMock()
        with (
            patch("modulo.api.routes.hitl.HITLManager") as mgr_cls,
            patch("modulo.api.routes.hitl._build_resume_executor", return_value=_resume_executor()),
        ):
            mgr_cls.return_value.approve = approve
            _override_principal(via_api_key=False, client_kind="hologram")
            self._install_session(_make_hitl_run(), snapshot=_human_only_snapshot())
            resp = client.post(
                f"/api/v1/runs/{_RUN_ID}/hitl/{_GATE_ID}/approve",
                json={"claim_token": "tok"},
            )

        assert resp.status_code == 403
        approve.assert_not_called()

    def test_approve_legacy_browser_jwt_still_passes(self, client: TestClient) -> None:
        """Backward compat: a legacy token (no claim) decodes client_kind=
        browser and the browser hot path is unchanged."""
        approve = AsyncMock()
        with (
            patch("modulo.api.routes.hitl.org_sandbox_capacity_free", new=AsyncMock(return_value=True)),
            patch("modulo.api.routes.hitl.HITLManager") as mgr_cls,
            patch("modulo.api.routes.hitl._build_resume_executor", return_value=_resume_executor()),
        ):
            mgr_cls.return_value.approve = approve
            _override_principal(via_api_key=False, client_kind="browser")
            self._install_session(_make_hitl_run(), snapshot=_human_only_snapshot())
            resp = client.post(
                f"/api/v1/runs/{_RUN_ID}/hitl/{_GATE_ID}/approve",
                json={"claim_token": "tok"},
            )

        assert resp.status_code == 200
        approve.assert_awaited_once()

    def test_api_key_principal_still_denied_under_client_kind_rule(self, client: TestClient) -> None:
        """The via_api_key marker remains part of the denial rule (an API-key
        principal carries client_kind=programmatic from the dependency, but
        the OR subsumes doubles that set only the marker)."""
        approve = AsyncMock()
        with patch("modulo.api.routes.hitl.HITLManager") as mgr_cls:
            mgr_cls.return_value.approve = approve
            _override_principal(via_api_key=True, client_kind="browser")
            self._install_session(_make_hitl_run(), snapshot=_human_only_snapshot())
            resp = client.post(
                f"/api/v1/runs/{_RUN_ID}/hitl/{_GATE_ID}/approve",
                json={"claim_token": "tok"},
            )

        assert resp.status_code == 403
        approve.assert_not_called()

    def test_deliver_manual_programmatic_client_kind_returns_403(self, client: TestClient) -> None:
        deliver = AsyncMock()
        with patch("modulo.api.routes.hitl.HITLManager") as mgr_cls:
            mgr_cls.return_value.deliver_manual = deliver
            _override_principal(via_api_key=False, client_kind="programmatic")
            self._install_session(_make_hitl_run(), snapshot=_human_only_snapshot())
            resp = client.post(
                f"/api/v1/runs/{_RUN_ID}/hitl/{_GATE_ID}/deliver-manual",
                json={"claim_token": "tok", "output": {"result": "ok"}},
            )

        assert resp.status_code == 403
        deliver.assert_not_called()

    def test_reject_programmatic_client_kind_still_allowed(self, client: TestClient) -> None:
        """reject is the safe direction for every credential class."""
        reject = AsyncMock()
        with (
            patch("modulo.api.routes.hitl.HITLManager") as mgr_cls,
            patch("modulo.api.routes.hitl._build_resume_executor", return_value=_resume_executor()),
        ):
            mgr_cls.return_value.reject = reject
            _override_principal(via_api_key=False, client_kind="programmatic")
            self._install_session(_make_hitl_run(), snapshot=_human_only_snapshot())
            resp = client.post(
                f"/api/v1/runs/{_RUN_ID}/hitl/{_GATE_ID}/reject",
                json={"claim_token": "tok", "reason": "not good"},
            )

        assert resp.status_code == 200
        reject.assert_awaited_once()


class TestHumanOnlyDenialAudit:
    """FAR-634: every human_only denial emits a warning log + the
    ``hitl.human_only_denied`` audit event (fresh session, failure-isolated:
    an audit failure must never change the denial outcome)."""

    @pytest.fixture(autouse=True)
    def _no_real_audit_db(self) -> Generator[None, None, None]:
        """Default: fail the audit engine lookup (no DB in unit tests). Tests
        that assert the audit WRITE override this with a mock chain."""
        with patch(
            "modulo.api.routes.hitl.get_or_create_engine",
            side_effect=RuntimeError("no db in unit tests"),
        ):
            yield

    @staticmethod
    def _install_session(
        run: MagicMock,
        snapshot: object = None,
        claim_row: object = None,
    ) -> None:
        mock_session = _hitl_session(run, snapshot=snapshot, claim_row=claim_row)

        async def override_session() -> AsyncGenerator[AsyncMock, None]:
            yield mock_session

        app.dependency_overrides[get_db_session] = override_session

    def test_denial_emits_audit_event_with_denial_fields(self, client: TestClient) -> None:
        """The REST denial routes emit the audit through ``_run_hitl_manager``
        after the decision transaction rolled back, carrying run_id / gate_id /
        action / principal kind / client kind."""
        emit = AsyncMock()
        with (
            patch("modulo.api.routes.hitl.HITLManager"),
            patch("modulo.api.routes.hitl._emit_human_only_denial_audit", new=emit),
        ):
            _override_principal(via_api_key=False, client_kind="programmatic")
            self._install_session(_make_hitl_run(), snapshot=_human_only_snapshot())
            resp = client.post(
                f"/api/v1/runs/{_RUN_ID}/hitl/{_GATE_ID}/approve",
                json={"claim_token": "tok"},
            )

        assert resp.status_code == 403
        emit.assert_awaited_once()
        exc = emit.await_args.args[0]
        assert exc.run_id == _RUN_ID
        assert exc.gate_id == _GATE_ID
        assert exc.action == "approve"
        assert exc.org_id == _ORG_ID
        assert exc.principal_kind == "jwt"
        assert exc.client_kind == "programmatic"
        assert exc.reason == _HUMAN_ONLY_DETAIL

    def test_submit_manual_denial_carries_submit_manual_action(self, client: TestClient) -> None:
        """The submit-manual route labels its denial audit with the route's
        action, not the manager method it shares with approve."""
        emit = AsyncMock()
        with (
            patch("modulo.api.routes.hitl.HITLManager"),
            patch("modulo.api.routes.hitl._emit_human_only_denial_audit", new=emit),
        ):
            _override_principal(via_api_key=False, client_kind="programmatic")
            # run with NO snapshot: the claim stamp is the only config source.
            self._install_session(_make_hitl_run(snapshot_id=None), claim_row={"human_only": True})
            resp = client.post(
                f"/api/v1/runs/{_RUN_ID}/manual/{_GATE_ID}/submit",
                json={"claim_token": "tok", "output": {"o": 1}},
            )

        assert resp.status_code == 403
        emit.assert_awaited_once()
        exc = emit.await_args.args[0]
        assert exc.action == "submit_manual"
        assert exc.reason == _HUMAN_ONLY_DETAIL

    def test_audit_failure_never_changes_the_denial_outcome(self, client: TestClient) -> None:
        """Failure isolation: the audit write raising leaves the byte-identical
        403 (the denial is already emitted — the write is best-effort)."""
        engine = MagicMock()
        session = _audit_session_double()
        factory = MagicMock(return_value=_audit_session_ctx(session))
        with (
            patch("modulo.api.routes.hitl.HITLManager"),
            patch("modulo.api.routes.hitl.get_or_create_engine", return_value=engine),
            patch("modulo.api.routes.hitl.get_or_create_session_factory", return_value=factory),
            patch(
                "modulo.api.routes.hitl.append_audit_event",
                new=AsyncMock(side_effect=RuntimeError("audit write boom")),
            ),
        ):
            _override_principal(via_api_key=False, client_kind="programmatic")
            self._install_session(_make_hitl_run(), snapshot=_human_only_snapshot())
            resp = client.post(
                f"/api/v1/runs/{_RUN_ID}/hitl/{_GATE_ID}/approve",
                json={"claim_token": "tok"},
            )

        assert resp.status_code == 403
        assert resp.json()["detail"] == _HUMAN_ONLY_DETAIL

    async def test_emit_writes_audit_event_with_right_fields(self) -> None:
        """The emit helper appends ``hitl.human_only_denied`` in a fresh RLS
        org-scoped session with the full denial payload."""
        engine = MagicMock()
        session = _audit_session_double()
        factory = MagicMock(return_value=_audit_session_ctx(session))
        append = AsyncMock()
        emit_exc = HumanOnlyDenied(
            _HUMAN_ONLY_DETAIL,
            run_id=_RUN_ID,
            gate_id=_GATE_ID,
            action="approve",
            org_id=_ORG_ID,
            account_id=_USER_ID,
            org_role="admin",
            principal_kind="jwt",
            client_kind="programmatic",
        )
        with (
            patch("modulo.api.routes.hitl.get_or_create_engine", return_value=engine),
            patch("modulo.api.routes.hitl.get_or_create_session_factory", return_value=factory),
            patch("modulo.api.routes.hitl.append_audit_event", new=append),
        ):
            await _emit_human_only_denial_audit(emit_exc)

        append.assert_awaited_once()
        kwargs = append.await_args.kwargs
        assert kwargs["event_type"] == EVENT_HUMAN_ONLY_DENIED
        assert kwargs["org_id"] == _ORG_ID
        assert kwargs["actor_user_id"] == _USER_ID
        assert kwargs["resource_type"] == "run"
        assert kwargs["resource_id"] == _RUN_ID
        payload = kwargs["payload_json"]
        assert payload["run_id"] == str(_RUN_ID)
        assert payload["gate_id"] == _GATE_ID
        assert payload["action"] == "approve"
        assert payload["surface"] == "rest"
        assert payload["principal_kind"] == "jwt"
        assert payload["client_kind"] == "programmatic"
        assert payload["reason"] == _HUMAN_ONLY_DETAIL

    async def test_emit_is_failure_isolated_when_audit_raises(self) -> None:
        """The emit helper swallows an audit failure (logged) — the caller's
        re-raise of the original 403 is never disturbed."""
        emit_exc = HumanOnlyDenied(
            _HUMAN_ONLY_DETAIL,
            run_id=_RUN_ID,
            gate_id=_GATE_ID,
            action="approve",
            org_id=_ORG_ID,
            account_id=_USER_ID,
            org_role="admin",
            principal_kind="jwt",
            client_kind="programmatic",
        )
        # The autouse fixture fails the engine lookup with RuntimeError —
        # the helper must swallow it and return None (the caller then re-raises
        # the untouched HumanOnlyDenied).
        outcome = await _emit_human_only_denial_audit(emit_exc)
        assert outcome is None


def _audit_session_ctx(session: AsyncMock | MagicMock) -> Any:
    @asynccontextmanager
    async def _ctx() -> AsyncGenerator[AsyncMock | MagicMock, None]:
        yield session

    return _ctx()


def _audit_session_double() -> MagicMock:
    """An audit-session double faithful to the real AsyncSession surface.

    The emit's RLS preamble (``_ensure_active_transaction``) calls
    ``in_transaction()`` and ``get_bind()`` SYNCHRONOUSLY — a bare AsyncMock
    returns un-awaited coroutines for both and leaks a "coroutine never
    awaited" warning per call (two per denial). ``execute`` stays async
    because the set_config queries are genuinely awaited.
    """
    bind = MagicMock()
    bind.dialect.name = "postgresql"
    session = MagicMock()
    session.in_transaction = MagicMock(return_value=True)
    session.get_bind = MagicMock(return_value=bind)
    session.execute = AsyncMock()
    begin_cm = MagicMock(__aenter__=AsyncMock(return_value=None), __aexit__=AsyncMock(return_value=False))
    session.begin = MagicMock(return_value=begin_cm)
    return session
