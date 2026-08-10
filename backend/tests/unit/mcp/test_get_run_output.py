"""Unit tests for the get_run_output MCP tool."""

import uuid
from unittest.mock import AsyncMock, MagicMock, patch

from modulo.api.mcp_server import get_run_output

_PLACEHOLDER_ORG_ID = uuid.UUID("00000000-0000-0000-0000-000000000001")
_API_KEY = "mk_testprefix_testsecretkey1234567890abc"


def _make_mock_run(*, outputs_json: dict | None = None, node_telemetry_json: dict | None = None) -> MagicMock:
    r = MagicMock()
    r.outputs_json = outputs_json
    r.node_telemetry_json = node_telemetry_json
    return r


def _make_mock_session() -> AsyncMock:
    session = AsyncMock()
    begin_cm = AsyncMock()
    begin_cm.__aenter__ = AsyncMock(return_value=None)
    begin_cm.__aexit__ = AsyncMock(return_value=False)
    session.begin = MagicMock(return_value=begin_cm)
    return session


# ---------------------------------------------------------------------------
# Auth error cases
# ---------------------------------------------------------------------------


class TestGetRunOutputAuth:
    def setup_method(self) -> None:
        from modulo.api.mcp_server import _ctx_auth_token, _ctx_auth_type, _ctx_org_id, _ctx_role

        _ctx_org_id.set(_PLACEHOLDER_ORG_ID)
        _ctx_role.set("runner")
        _ctx_auth_token.set(_API_KEY)
        _ctx_auth_type.set("api_key")

    def teardown_method(self) -> None:
        from modulo.api.mcp_server import _ctx_auth_token, _ctx_auth_type, _ctx_org_id, _ctx_role

        _ctx_org_id.set(None)
        _ctx_role.set(None)
        _ctx_auth_token.set(None)
        _ctx_auth_type.set(None)

    @patch("modulo.api.mcp_server.validate_current_auth", return_value=False)
    async def test_returns_auth_error_on_revoked_token(self, mock_validate_auth: AsyncMock) -> None:
        result = await get_run_output(run_id=str(uuid.uuid4()), node_id="node1")
        assert result["error"] == "auth_expired"
        assert "revoked" in result.get("detail", "").lower() or "expired" in result.get("detail", "").lower()

    @patch("modulo.api.mcp_server.validate_current_auth", return_value=True)
    @patch("modulo.api.mcp_server._session")
    @patch("modulo.api.mcp_server.check_tool_scope")
    async def test_insufficient_scope(
        self,
        mock_check_scope: MagicMock,
        mock_session: AsyncMock,
        mock_validate_auth: AsyncMock,
    ) -> None:
        from modulo.core.mcp.scope_validator import MCPAuthorizationError

        mock_check_scope.side_effect = MCPAuthorizationError("insufficient_scope")
        mock_cm = AsyncMock()
        mock_cm.__aenter__ = AsyncMock(return_value=AsyncMock())
        mock_cm.__aexit__ = AsyncMock(return_value=False)
        mock_session.return_value = mock_cm

        result = await get_run_output(run_id=str(uuid.uuid4()), node_id="node1")
        assert result["error"] == "insufficient_scope"


# ---------------------------------------------------------------------------
# Successful responses
# ---------------------------------------------------------------------------


class TestGetRunOutputSuccess:
    def setup_method(self) -> None:
        from modulo.api.mcp_server import _ctx_auth_token, _ctx_auth_type, _ctx_org_id, _ctx_role

        _ctx_org_id.set(_PLACEHOLDER_ORG_ID)
        _ctx_role.set("runner")
        _ctx_auth_token.set(_API_KEY)
        _ctx_auth_type.set("api_key")

    def teardown_method(self) -> None:
        from modulo.api.mcp_server import _ctx_auth_token, _ctx_auth_type, _ctx_org_id, _ctx_role

        _ctx_org_id.set(None)
        _ctx_role.set(None)
        _ctx_auth_token.set(None)
        _ctx_auth_type.set(None)

    @patch("modulo.api.mcp_server.validate_current_auth", return_value=True)
    @patch("modulo.api.mcp_server.get_run")
    @patch("modulo.api.mcp_server._session")
    async def test_returns_masked_output(
        self,
        mock_session: AsyncMock,
        mock_get_run: AsyncMock,
        mock_validate_auth: AsyncMock,
    ) -> None:
        mock_cm = AsyncMock()
        mock_cm.__aenter__ = AsyncMock(return_value=AsyncMock())
        mock_cm.__aexit__ = AsyncMock(return_value=False)
        mock_session.return_value = mock_cm

        run_id = uuid.uuid4()
        mock_get_run.return_value = _make_mock_run(
            outputs_json={
                "node1": {
                    "result": "hello",
                    "api_key": "sk-1234567890abcdef",
                    "nested": {"secret": "super-secret-value"},
                },
            },
        )

        result = await get_run_output(run_id=str(run_id), node_id="node1")

        assert result["node_id"] == "node1"
        assert result["output"]["result"] == "hello"
        assert result["output"]["api_key"] == "••••••"
        assert result["output"]["nested"]["secret"] == "••••••"
        assert "api_key" in result["masked_fields"]
        mock_get_run.assert_awaited_once()

    @patch("modulo.api.mcp_server.validate_current_auth", return_value=True)
    @patch("modulo.api.mcp_server.get_run")
    @patch("modulo.api.mcp_server._session")
    async def test_no_masked_fields(
        self,
        mock_session: AsyncMock,
        mock_get_run: AsyncMock,
        mock_validate_auth: AsyncMock,
    ) -> None:
        mock_cm = AsyncMock()
        mock_cm.__aenter__ = AsyncMock(return_value=AsyncMock())
        mock_cm.__aexit__ = AsyncMock(return_value=False)
        mock_session.return_value = mock_cm

        run_id = uuid.uuid4()
        mock_get_run.return_value = _make_mock_run(
            outputs_json={
                "node1": {"result": "hello", "summary": "all good"},
            },
        )

        result = await get_run_output(run_id=str(run_id), node_id="node1")

        assert result["node_id"] == "node1"
        assert result["output"]["result"] == "hello"
        assert result["masked_fields"] == []

    @patch("modulo.api.mcp_server.validate_current_auth", return_value=True)
    @patch("modulo.api.mcp_server.get_run")
    @patch("modulo.api.mcp_server._session")
    async def test_run_not_found(
        self,
        mock_session: AsyncMock,
        mock_get_run: AsyncMock,
        mock_validate_auth: AsyncMock,
    ) -> None:
        mock_cm = AsyncMock()
        mock_cm.__aenter__ = AsyncMock(return_value=AsyncMock())
        mock_cm.__aexit__ = AsyncMock(return_value=False)
        mock_session.return_value = mock_cm

        run_id = uuid.uuid4()
        mock_get_run.return_value = None

        result = await get_run_output(run_id=str(run_id), node_id="node1")
        assert result["error"] == "run_not_found"

    @patch("modulo.api.mcp_server.validate_current_auth", return_value=True)
    @patch("modulo.api.mcp_server.get_run")
    @patch("modulo.api.mcp_server._session")
    async def test_node_not_found(
        self,
        mock_session: AsyncMock,
        mock_get_run: AsyncMock,
        mock_validate_auth: AsyncMock,
    ) -> None:
        mock_cm = AsyncMock()
        mock_cm.__aenter__ = AsyncMock(return_value=AsyncMock())
        mock_cm.__aexit__ = AsyncMock(return_value=False)
        mock_session.return_value = mock_cm

        run_id = uuid.uuid4()
        mock_get_run.return_value = _make_mock_run(
            outputs_json={"node1": {"result": "hello"}},
        )

        result = await get_run_output(run_id=str(run_id), node_id="nonexistent")
        assert result["error"] == "node_output_not_found"

    @patch("modulo.api.mcp_server.validate_current_auth", return_value=True)
    @patch("modulo.api.mcp_server.get_run")
    @patch("modulo.api.mcp_server._session")
    async def test_new_shape_returns_pure_return_with_no_telemetry_keys(
        self,
        mock_session: AsyncMock,
        mock_get_run: AsyncMock,
        mock_validate_auth: AsyncMock,
    ) -> None:
        mock_cm = AsyncMock()
        mock_cm.__aenter__ = AsyncMock(return_value=AsyncMock())
        mock_cm.__aexit__ = AsyncMock(return_value=False)
        mock_session.return_value = mock_cm

        run_id = uuid.uuid4()
        pure_return = {"summary": "agent summary", "changed_files": ["a.py"]}
        mock_get_run.return_value = _make_mock_run(
            outputs_json={"node1": pure_return},
            node_telemetry_json={
                "node1": {
                    "status": "completed",
                    "agent_stdout": "console output",
                    "agent_stderr": "traceback",
                    "error_message": "boom",
                    "wall_clock_time_ms": 12345,
                    "cost_estimate_usd": 0.5,
                    "sandbox_log_tail": "tail",
                }
            },
        )

        result = await get_run_output(run_id=str(run_id), node_id="node1")

        assert result["node_id"] == "node1"
        assert result["output"] == pure_return
        for key in (
            "status",
            "agent_stdout",
            "agent_stderr",
            "error_message",
            "wall_clock_time_ms",
            "cost_estimate_usd",
            "sandbox_log_tail",
        ):
            assert key not in result["output"]
        assert result["masked_fields"] == []

    @patch("modulo.api.mcp_server.validate_current_auth", return_value=True)
    @patch("modulo.api.mcp_server.get_run")
    @patch("modulo.api.mcp_server._session")
    async def test_derived_status_fallback_when_return_none(
        self,
        mock_session: AsyncMock,
        mock_get_run: AsyncMock,
        mock_validate_auth: AsyncMock,
    ) -> None:
        mock_cm = AsyncMock()
        mock_cm.__aenter__ = AsyncMock(return_value=AsyncMock())
        mock_cm.__aexit__ = AsyncMock(return_value=False)
        mock_session.return_value = mock_cm

        run_id = uuid.uuid4()
        mock_get_run.return_value = _make_mock_run(
            outputs_json={},
            node_telemetry_json={"node1": {"status": "failed", "summary": "command failed"}},
        )

        result = await get_run_output(run_id=str(run_id), node_id="node1")

        assert result["node_id"] == "node1"
        assert result["output"] == {"status": "failed", "summary": "command failed"}
        assert result["masked_fields"] == []

    @patch("modulo.api.mcp_server.validate_current_auth", return_value=True)
    @patch("modulo.api.mcp_server.get_run")
    @patch("modulo.api.mcp_server._session")
    async def test_node_not_found_when_absent_from_both_columns(
        self,
        mock_session: AsyncMock,
        mock_get_run: AsyncMock,
        mock_validate_auth: AsyncMock,
    ) -> None:
        mock_cm = AsyncMock()
        mock_cm.__aenter__ = AsyncMock(return_value=AsyncMock())
        mock_cm.__aexit__ = AsyncMock(return_value=False)
        mock_session.return_value = mock_cm

        run_id = uuid.uuid4()
        mock_get_run.return_value = _make_mock_run(
            outputs_json={"node1": {"result": "hello"}},
            node_telemetry_json={"node2": {"status": "completed", "summary": "ok"}},
        )

        result = await get_run_output(run_id=str(run_id), node_id="nonexistent")
        assert result["error"] == "node_output_not_found"

    @patch("modulo.api.mcp_server.validate_current_auth", return_value=True)
    @patch("modulo.api.mcp_server._session")
    async def test_invalid_run_id_returns_invalid_id(
        self,
        mock_session: AsyncMock,
        mock_validate_auth: AsyncMock,
    ) -> None:
        result = await get_run_output(run_id="not-a-uuid", node_id="node1")

        assert result["error"] == "invalid_id"
        assert result["field"] == "run_id"
        # Validation fails before any session is opened.
        mock_session.assert_not_called()
