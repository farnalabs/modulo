"""Unit tests for MCP dual-layer scope validation.

Tests the ViewModel-level scope checks independently of the middleware,
and verifies integration through the MCP tool handlers.
"""

from unittest.mock import AsyncMock, patch

import pytest

from modulo.core.mcp.scope_validator import (
    REVIEW_HITL_ACTION_REQUIREMENTS,
    TOOL_SCOPE_REQUIREMENTS,
    MCPAuthorizationError,
    check_tool_scope,
)


class TestCheckToolScope:
    """Direct unit tests for the ``check_tool_scope`` function."""

    @pytest.mark.parametrize(
        ("role", "tool"),
        [
            ("runner", "trigger_pipeline"),
            ("operator", "trigger_pipeline"),
            ("admin", "trigger_pipeline"),
            ("runner", "cancel_run"),
            ("runner", "list_pending_hitl"),
            ("runner", "copy_library_primitive"),
            ("operator", "review_hitl"),
            ("admin", "review_hitl"),
        ],
    )
    def test_authorized_role_passes(self, role: str, tool: str) -> None:
        check_tool_scope(role, tool)

    @pytest.mark.parametrize(
        ("role", "tool"),
        [
            ("viewer", "trigger_pipeline"),
            ("viewer", "cancel_run"),
            ("viewer", "list_pending_hitl"),
            ("viewer", "copy_library_primitive"),
            ("viewer", "review_hitl"),
            ("runner", "review_hitl"),
        ],
    )
    def test_unauthorized_role_raises(self, role: str, tool: str) -> None:
        with pytest.raises(MCPAuthorizationError) as excinfo:
            check_tool_scope(role, tool)
        assert "Insufficient scope" in str(excinfo.value)
        assert tool in str(excinfo.value)

    @pytest.mark.parametrize("role", ["viewer", "runner", "operator", "admin"])
    def test_tools_without_scope_req_always_pass(self, role: str) -> None:
        check_tool_scope(role, "list_pipelines_tool")
        check_tool_scope(role, "get_run_status")

    def test_none_role_raises(self) -> None:
        with pytest.raises(MCPAuthorizationError) as excinfo:
            check_tool_scope(None, "trigger_pipeline")
        assert "No authentication context" in str(excinfo.value)

    def test_unknown_role_raises(self) -> None:
        with pytest.raises(MCPAuthorizationError) as excinfo:
            check_tool_scope("superadmin", "trigger_pipeline")
        assert "Unknown role" in str(excinfo.value)


class TestReviewHitlActionScopes:
    """Action-level scoping for the ``review_hitl`` tool."""

    def test_claim_requires_runner(self) -> None:
        check_tool_scope("runner", "review_hitl", action="claim")

    def test_claim_rejects_viewer(self) -> None:
        with pytest.raises(MCPAuthorizationError):
            check_tool_scope("viewer", "review_hitl", action="claim")

    def test_approve_requires_operator(self) -> None:
        check_tool_scope("operator", "review_hitl", action="approve")

    def test_approve_rejects_runner(self) -> None:
        with pytest.raises(MCPAuthorizationError):
            check_tool_scope("runner", "review_hitl", action="approve")

    def test_reject_requires_operator(self) -> None:
        check_tool_scope("operator", "review_hitl", action="reject")

    def test_reject_rejects_runner(self) -> None:
        with pytest.raises(MCPAuthorizationError):
            check_tool_scope("runner", "review_hitl", action="reject")

    def test_no_action_requires_operator(self) -> None:
        with pytest.raises(MCPAuthorizationError):
            check_tool_scope("runner", "review_hitl")


class TestMCPAuthorizationError:
    """MCPAuthorizationError behaviour."""

    def test_message_attribute(self) -> None:
        exc = MCPAuthorizationError("test message")
        assert exc.message == "test message"
        assert str(exc) == "test message"

    def test_is_exception(self) -> None:
        assert issubclass(MCPAuthorizationError, Exception)


class TestConstants:
    """Sanity checks on the scope requirement constants."""

    def test_tool_scope_requirements_keys(self) -> None:
        expected_tools = {
            "trigger_pipeline",
            "cancel_run",
            "review_hitl",
            "copy_library_primitive",
            "list_pending_hitl",
            "get_run_output",
            "get_trigger_events",
        }
        assert set(TOOL_SCOPE_REQUIREMENTS) == expected_tools

    def test_tool_requirements_use_valid_roles(self) -> None:
        valid_roles = {"viewer", "runner", "operator", "admin"}
        for tool, role in TOOL_SCOPE_REQUIREMENTS.items():
            assert role in valid_roles, f"{tool} has invalid role '{role}'"

    def test_review_hitl_action_requirements_keys(self) -> None:
        assert set(REVIEW_HITL_ACTION_REQUIREMENTS) == {"claim", "approve", "reject"}

    def test_review_hitl_action_use_valid_roles(self) -> None:
        for action, role in REVIEW_HITL_ACTION_REQUIREMENTS.items():
            assert role in {"runner", "operator"}, f"{action} has invalid role '{role}'"


class TestToolHandlerScopeErrorFormat:
    """Tool handlers return ``insufficient_scope`` error when scope check fails."""

    pytestmark = pytest.mark.asyncio

    @pytest.fixture(autouse=True)
    def _patch_auth(self) -> None:
        """Mock ``validate_current_auth`` to return True so scope checks are reached."""
        with patch("modulo.api.mcp_server.validate_current_auth", return_value=True):
            yield

    async def test_trigger_pipeline_insufficient_scope(self) -> None:
        from modulo.api.mcp_server import _ctx_role as _role
        from modulo.api.mcp_server import trigger_pipeline as _tp

        _role.set(None)
        result = await _tp(pipeline_id="00000000-0000-0000-0000-000000000001")
        assert result == {
            "error": "insufficient_scope",
            "detail": "No authentication context — role not set",
        }

    async def test_cancel_run_insufficient_scope(self) -> None:
        from modulo.api.mcp_server import _ctx_role as _role
        from modulo.api.mcp_server import cancel_run as _cr

        _role.set(None)
        result = await _cr(run_id="00000000-0000-0000-0000-000000000001")
        assert result == {
            "error": "insufficient_scope",
            "detail": "No authentication context — role not set",
        }

    async def test_list_pending_hitl_insufficient_scope(self) -> None:
        from modulo.api.mcp_server import _ctx_role as _role
        from modulo.api.mcp_server import list_pending_hitl as _lph

        _role.set(None)
        result = await _lph()
        assert result == {
            "error": "insufficient_scope",
            "detail": "No authentication context — role not set",
        }

    async def test_copy_library_primitive_insufficient_scope(self) -> None:
        from modulo.api.mcp_server import _ctx_role as _role
        from modulo.api.mcp_server import copy_library_primitive as _clp

        _role.set(None)
        result = await _clp(primitive_id="00000000-0000-0000-0000-000000000001")
        assert result == {
            "error": "insufficient_scope",
            "detail": "No authentication context — role not set",
        }

    async def test_review_hitl_insufficient_scope(self) -> None:
        from modulo.api.mcp_server import _ctx_role as _role
        from modulo.api.mcp_server import review_hitl as _rh

        _role.set(None)
        result = await _rh(
            run_id="00000000-0000-0000-0000-000000000001",
            gate_id="gate-1",
            action="claim",
        )
        assert result == {
            "error": "insufficient_scope",
            "detail": "No authentication context — role not set",
        }

    async def test_review_hitl_approve_requires_operator(self) -> None:
        from modulo.api.mcp_server import _ctx_role as _role
        from modulo.api.mcp_server import review_hitl as _rh

        _role.set("viewer")
        result = await _rh(
            run_id="00000000-0000-0000-0000-000000000001",
            gate_id="gate-1",
            action="approve",
            claim_token="tok",
        )
        assert result["error"] == "insufficient_scope"
        assert "requires 'operator' role, got 'viewer'" in result["detail"]

    async def test_review_hitl_approve_runner_rejected(self) -> None:
        from modulo.api.mcp_server import _ctx_role as _role
        from modulo.api.mcp_server import review_hitl as _rh

        _role.set("runner")
        result = await _rh(
            run_id="00000000-0000-0000-0000-000000000001",
            gate_id="gate-1",
            action="approve",
            claim_token="tok",
        )
        assert result["error"] == "insufficient_scope"
        assert "requires 'operator' role, got 'runner'" in result["detail"]

    async def test_review_hitl_claim_runner_passes_check(self) -> None:
        from modulo.api.mcp_server import _ctx_role as _role
        from modulo.api.mcp_server import review_hitl as _rh

        _role.set("runner")
        with patch("modulo.api.mcp_server._session") as mock_session:
            mock_session.return_value.__aenter__.return_value = AsyncMock()
            result = await _rh(
                run_id="00000000-0000-0000-0000-000000000001",
                gate_id="gate-1",
                action="claim",
            )
            assert result["error"] != "insufficient_scope"

    async def test_list_pipelines_no_scope_check(self) -> None:
        from modulo.api.mcp_server import _ctx_role as _role
        from modulo.api.mcp_server import list_pipelines_tool as _lpt

        _role.set(None)
        with patch("modulo.api.mcp_server._session"):
            result = await _lpt()
        assert "insufficient_scope" not in result

    async def test_get_run_status_no_scope_check(self) -> None:
        from modulo.api.mcp_server import _ctx_role as _role
        from modulo.api.mcp_server import get_run_status as _grs

        _role.set(None)
        with patch("modulo.api.mcp_server._session"):
            result = await _grs(run_id="00000000-0000-0000-0000-000000000001")
        assert "insufficient_scope" not in result
