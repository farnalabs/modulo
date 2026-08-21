"""FAR-126 P2b contract tests: pure returns on MCP surfaces.

- ``get_run_output`` returns the PURE return (never raw telemetry) by default.
- ``get_run_status`` derives node status from ``node_telemetry_json``.
- A seeded secret in ``agent_stdout`` (telemetry) never surfaces in any MCP
  response.
"""

import json
import uuid
from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock, patch

from modulo.api.mcp_server import get_run_output, get_run_status

_PLACEHOLDER_ORG_ID = uuid.UUID("00000000-0000-0000-0000-000000000001")
_PLACEHOLDER_USER_ID = uuid.UUID("00000000-0000-0000-0000-000000000003")
_API_KEY = "mk_testprefix_testsecretkey1234567890abc"

_SEEDED_SECRET = "SECRET_SEEDED_TOKEN_9f8a7b6c"

# Every exhaustive telemetry field that must NEVER leak into a default
# get_run_output response (the pure-return contract). ``summary`` is excluded
# on purpose: it is a legitimate pure-return key too.
_TELEMETRY_KEYS = (
    "status",
    "exit_code",
    "wall_clock_time_ms",
    "cost_estimate_usd",
    "model_cost_usd",
    "model_cost_raw_usd",
    "model_cost_clamped",
    "model_cost_out_of_band_high",
    "model_cost_display_usd",
    "agent_stdout",
    "agent_stderr",
    "stdout_length",
    "stderr_length",
    "stall_reason",
    "sandbox_id",
    "sandbox_log_tail",
    "error_type",
    "error_message",
)


def _make_session_context(session: AsyncMock) -> AsyncMock:
    cm = AsyncMock()
    cm.__aenter__ = AsyncMock(return_value=session)
    cm.__aexit__ = AsyncMock(return_value=False)
    return cm


def _make_mock_run(
    *,
    outputs_json: dict | None = None,
    node_telemetry_json: dict | None = None,
    node_token_usage: dict | None = None,
) -> MagicMock:
    run = MagicMock()
    run.id = uuid.uuid4()
    run.pipeline_id = uuid.uuid4()
    run.status = "complete"
    run.trigger_type = "manual"
    run.created_at = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)
    run.started_at = None
    run.completed_at = None
    run.error_code = None
    run.outputs_json = outputs_json
    run.node_telemetry_json = node_telemetry_json
    run.node_token_usage = node_token_usage
    return run


class _AuthContext:
    """Set/teardown the MCP ContextVars so tool handlers reach the DB layer."""

    def setup_method(self) -> None:
        from modulo.api.mcp_server import _ctx_auth_token, _ctx_auth_type, _ctx_org_id, _ctx_role, _ctx_user_id

        _ctx_org_id.set(_PLACEHOLDER_ORG_ID)
        _ctx_role.set("runner")
        _ctx_user_id.set(_PLACEHOLDER_USER_ID)
        _ctx_auth_token.set(_API_KEY)
        _ctx_auth_type.set("api_key")

    def teardown_method(self) -> None:
        from modulo.api.mcp_server import _ctx_auth_token, _ctx_auth_type, _ctx_org_id, _ctx_role, _ctx_user_id

        _ctx_org_id.set(None)
        _ctx_role.set(None)
        _ctx_user_id.set(None)
        _ctx_auth_token.set(None)
        _ctx_auth_type.set(None)


class TestGetRunOutputPureReturnContract(_AuthContext):
    @patch("modulo.api.mcp_server.validate_current_auth", return_value=True)
    @patch("modulo.api.mcp_server.get_run")
    @patch("modulo.api.mcp_server._session")
    async def test_default_response_contains_no_telemetry_keys(
        self,
        mock_session: AsyncMock,
        mock_get_run: AsyncMock,
        mock_validate_auth: AsyncMock,
    ) -> None:
        mock_sesh = AsyncMock()
        mock_session.return_value = _make_session_context(mock_sesh)
        pure_return = {"summary": "agent summary", "changed_files": ["a.py"]}
        mock_get_run.return_value = _make_mock_run(
            outputs_json={"node1": pure_return},
            node_telemetry_json={
                "node1": {
                    "status": "completed",
                    "agent_stdout": _SEEDED_SECRET,
                    "agent_stderr": "traceback",
                    "error_message": "boom",
                    "wall_clock_time_ms": 12345,
                    "cost_estimate_usd": 0.5,
                    "sandbox_log_tail": "tail",
                }
            },
        )

        result = await get_run_output(run_id=str(uuid.uuid4()), node_id="node1")

        assert result["output"] == pure_return
        for key in _TELEMETRY_KEYS:
            assert key not in result["output"]
        assert _SEEDED_SECRET not in json.dumps(result)


class TestGetRunStatusDerivesFromTelemetry(_AuthContext):
    @patch("modulo.api.mcp_server.validate_current_auth", return_value=True)
    @patch("modulo.api.mcp_server.get_run")
    @patch("modulo.api.mcp_server._session")
    async def test_derives_failed_status_from_telemetry(
        self,
        mock_session: AsyncMock,
        mock_get_run: AsyncMock,
        mock_validate_auth: AsyncMock,
    ) -> None:
        mock_sesh = AsyncMock()
        mock_session.return_value = _make_session_context(mock_sesh)
        mock_get_run.return_value = _make_mock_run(
            outputs_json={"node_a": {"summary": "ok"}},
            node_telemetry_json={
                "node_a": {"status": "completed"},
                "node_b": {"status": "failed", "summary": "boom", "agent_stderr": _SEEDED_SECRET},
            },
        )

        result = await get_run_status(run_id=str(uuid.uuid4()), detail=True)

        by_id = {n["node_id"]: n for n in result["nodes"]}
        assert by_id["node_a"]["status"] == "completed"
        assert by_id["node_a"]["has_output"] is True
        assert by_id["node_b"]["status"] == "failed"
        assert by_id["node_b"]["has_output"] is True
        assert _SEEDED_SECRET not in json.dumps(result)


class TestSeededSecretNeverLeaks(_AuthContext):
    @patch("modulo.api.mcp_server.validate_current_auth", return_value=True)
    @patch("modulo.api.mcp_server.get_run")
    @patch("modulo.api.mcp_server._session")
    async def test_seeded_secret_in_agent_stdout_never_in_any_mcp_response(
        self,
        mock_session: AsyncMock,
        mock_get_run: AsyncMock,
        mock_validate_auth: AsyncMock,
    ) -> None:
        mock_sesh = AsyncMock()
        mock_session.return_value = _make_session_context(mock_sesh)
        mock_get_run.return_value = _make_mock_run(
            outputs_json={"node1": {"summary": "all good"}},
            node_telemetry_json={
                "node1": {
                    "status": "completed",
                    "agent_stdout": _SEEDED_SECRET,
                    "agent_stderr": _SEEDED_SECRET,
                    "sandbox_log_tail": _SEEDED_SECRET,
                }
            },
        )

        output_result = await get_run_output(run_id=str(uuid.uuid4()), node_id="node1")
        assert _SEEDED_SECRET not in json.dumps(output_result)
        assert output_result["output"] == {"summary": "all good"}

        status_result = await get_run_status(run_id=str(uuid.uuid4()), detail=True)
        assert _SEEDED_SECRET not in json.dumps(status_result)
