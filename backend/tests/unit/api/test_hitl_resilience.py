"""Unit tests: SQLAlchemyError→503 and NotTeamMemberError→403 on HITL API routes."""

import uuid
from collections.abc import AsyncGenerator, Generator
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.exc import SQLAlchemyError

from modulo.api.dependencies import _get_engine, get_db_session, get_plan_context
from modulo.api.main import app
from modulo.auth.dependencies import get_current_user
from modulo.auth.jwt import AuthenticatedPrincipal
from modulo.core.hitl_manager import NotTeamMemberError
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


class TestApproveGateSQLAlchemyError:
    @patch("modulo.api.routes.hitl.HITLManager.approve", new=AsyncMock(side_effect=SQLAlchemyError("mock", {}, "")))
    def test_approve_gate_returns_503(self, client: TestClient) -> None:
        resp = client.post(
            f"/api/v1/runs/{_RUN_ID}/hitl/gate-1/approve",
            json={"claim_token": "test-token", "notes": "approved"},
        )
        assert resp.status_code == 503


class TestApproveGateAtSandboxCapacity:
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


class TestApproveWithModificationSQLAlchemyError:
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
    @patch("modulo.api.routes.hitl.HITLManager.approve", new=AsyncMock(side_effect=SQLAlchemyError("mock", {}, "")))
    def test_submit_manual_returns_503(self, client: TestClient) -> None:
        resp = client.post(
            f"/api/v1/runs/{_RUN_ID}/manual/gate-1/submit",
            json={"claim_token": "test-token", "output": {"result": "ok"}},
        )
        assert resp.status_code == 503


class TestSubmitManualNotTeamMemberError:
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
