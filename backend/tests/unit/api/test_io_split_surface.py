"""Unit tests for the /io normalized split surfaces (FAR-126 P2a).

The endpoint must expose ONE shape to the frontend: ``outputs_json`` holds
each node's PURE return and ``node_telemetry`` holds its exhaustive
telemetry. These tests assert the round-trip invariants:

- A new-shape run's ``outputs_json[node]`` contains ZERO telemetry keys
  while ``node_telemetry[node]`` carries them.
- A legacy run's ``outputs_json[node]`` stays the mixed envelope verbatim.
- A telemetry-only node (skipped / no outputs entry) still appears under
  ``node_telemetry``.
- Seeded secrets never surface on the pure-return surface, and
  sensitive-keyed values are masked on both surfaces.
"""

import json
import uuid
from collections.abc import AsyncGenerator, Generator
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import AsyncSession

from modulo.api.dependencies import _get_engine, get_db_session, get_plan_context
from modulo.api.main import app
from modulo.api.middleware.sensitive_mask import SENSITIVE_VALUE_MASK
from modulo.auth.dependencies import get_current_tenant_user, get_current_user
from modulo.auth.jwt import AuthenticatedPrincipal, TenantPrincipal
from modulo.settings import Settings, get_settings

_VALID_32 = "a" * 32
_ORG_ID = uuid.UUID("00000000-0000-0000-0000-000000000001")
_USER_ID = uuid.UUID("00000000-0000-0000-0000-000000000002")
_RUN_ID = uuid.uuid4()

# Representative telemetry vocabulary (mirrors TELEMETRY_FIELDS in
# node_output_split) — none of these may appear in a new-shape pure return.
_TELEMETRY_KEYS = (
    "status",
    "summary",
    "exit_code",
    "wall_clock_time_ms",
    "cost_estimate_usd",
    "agent_stdout",
    "agent_stderr",
    "sandbox_log_tail",
    "stall_reason",
    "recovered",
    "skipped",
)


def _make_settings() -> Settings:
    return Settings(
        database_url="postgresql+asyncpg://localhost/test",
        secret_key=_VALID_32,
        fernet_key=_VALID_32,
        modulo_admin_password="testpass",
    )


def _make_run(
    *,
    status: str = "complete",
    run_number: int = 3,
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


def _make_mock_session() -> AsyncMock:
    session = AsyncMock(spec=AsyncSession)
    begin_cm = AsyncMock()
    begin_cm.__aenter__ = AsyncMock(return_value=None)
    begin_cm.__aexit__ = AsyncMock(return_value=False)
    session.begin = MagicMock(return_value=begin_cm)
    # The /io endpoint resolves the snapshot for node_labels; default to no
    # snapshot so node_labels is empty unless a test stubs one explicitly.
    exec_result = MagicMock()
    exec_result.scalar_one_or_none.return_value = None
    session.execute.return_value = exec_result
    return session


@pytest.fixture
def client(mock_session: AsyncMock) -> Generator[TestClient, None, None]:
    mock_engine = MagicMock()

    async def override_session() -> AsyncGenerator[AsyncMock, None]:
        yield mock_session

    app.dependency_overrides[get_settings] = _make_settings
    app.dependency_overrides[get_db_session] = override_session
    app.dependency_overrides[_get_engine] = lambda: mock_engine
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

    mock_plan = MagicMock()
    mock_plan.feature_enabled.return_value = True
    app.dependency_overrides[get_plan_context] = lambda: mock_plan
    yield TestClient(app)

    app.dependency_overrides.clear()


@pytest.fixture
def mock_session() -> AsyncMock:
    return _make_mock_session()


class TestIoRoundTrip:
    """GET /api/v1/runs/{run_id}/io — split-surface invariants."""

    def _get_io(self, client: TestClient, run: MagicMock) -> dict[str, Any]:
        with (
            patch("modulo.api.routes.runs.get_run", return_value=run),
            patch("modulo.api.routes.runs.set_rls_org"),
        ):
            resp = client.get(f"/api/v1/runs/{_RUN_ID}/io")
        assert resp.status_code == 200
        return resp.json()

    def test_new_shape_outputs_have_zero_telemetry_keys(self, client: TestClient, mock_session: AsyncMock) -> None:
        pure_outputs = {
            "planner": {"plan": "Step 1: analyse", "confidence": 0.9},
            "coder": {"code": "print('hello')"},
        }
        telemetry = {
            "planner": {
                "status": "completed",
                "summary": "planned",
                "agent_stdout": "thinking",
                "wall_clock_time_ms": 1200,
            },
            "coder": {"status": "completed", "summary": "coded", "agent_stdout": "log"},
        }
        run = _make_run(input_payload={"prompt": "Hello"}, outputs_json=pure_outputs, node_telemetry_json=telemetry)

        body = self._get_io(client, run)

        for node_id in pure_outputs:
            node_output = body["outputs_json"][node_id]
            assert isinstance(node_output, dict)
            assert not any(key in node_output for key in _TELEMETRY_KEYS), f"{node_id} leaked telemetry keys"
            assert body["node_telemetry"][node_id] == telemetry[node_id]

    def test_legacy_run_output_unchanged(self, client: TestClient, mock_session: AsyncMock) -> None:
        envelope = {
            "artifacts": [
                {
                    "node_id": "planner",
                    "status": "completed",
                    "output": {
                        "status": "completed",
                        "summary": "planned",
                        "output_json": {"plan": "Step 1"},
                        "agent_stdout": "log",
                    },
                }
            ],
            "output": {"status": "completed", "summary": "planned"},
        }
        run = _make_run(
            input_payload={"prompt": "Hello"},
            outputs_json={"planner": envelope},
            node_telemetry_json=None,
        )

        body = self._get_io(client, run)

        assert body["outputs_json"]["planner"] == envelope
        assert body["node_telemetry"]["planner"] == {"status": "completed", "summary": "planned"}

    def test_telemetry_only_node_appears_in_node_telemetry(self, client: TestClient, mock_session: AsyncMock) -> None:
        run = _make_run(
            input_payload={"prompt": "Hello"},
            outputs_json={"planner": {"plan": "Step 1"}},
            node_telemetry_json={
                "planner": {"status": "completed", "summary": "planned"},
                # Telemetry-only node: a skipped node with no outputs entry.
                "skipped-node": {"status": "skipped", "summary": "Skipped: missing input fields"},
            },
        )

        body = self._get_io(client, run)

        assert "skipped-node" not in body["outputs_json"]
        assert body["node_telemetry"]["skipped-node"] == {
            "status": "skipped",
            "summary": "Skipped: missing input fields",
        }

    def test_seeded_secret_in_agent_stdout_never_reaches_outputs_surface(
        self, client: TestClient, mock_session: AsyncMock
    ) -> None:
        secret = "sk-seeded-ultra-secret-42"
        run = _make_run(
            input_payload={"prompt": "Hello"},
            outputs_json={"planner": {"plan": "Step 1"}},
            node_telemetry_json={
                "planner": {
                    "status": "completed",
                    "summary": "planned",
                    "agent_stdout": f"issued credential {secret}",
                }
            },
        )

        body = self._get_io(client, run)

        # The pure-return surface never carries telemetry content.
        assert secret not in json.dumps(body["outputs_json"])
        # The telemetry surface is where it lives (unmasked — it is not under
        # a sensitive key).
        assert secret in body["node_telemetry"]["planner"]["agent_stdout"]

    def test_sensitive_keys_masked_on_both_surfaces(self, client: TestClient, mock_session: AsyncMock) -> None:
        run = _make_run(
            input_payload={"prompt": "Hello"},
            outputs_json={"planner": {"api_key": "sk-out", "result": "ok"}},
            node_telemetry_json={
                "planner": {
                    "status": "completed",
                    "summary": "planned",
                    "credentials": {"api_key": "sk-in", "public": "visible"},
                }
            },
        )

        body = self._get_io(client, run)

        assert body["outputs_json"]["planner"]["api_key"] == SENSITIVE_VALUE_MASK
        telemetry = body["node_telemetry"]["planner"]
        assert telemetry["credentials"]["api_key"] == SENSITIVE_VALUE_MASK
        assert telemetry["credentials"]["public"] == "visible"
