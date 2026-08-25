"""Security tests for MCP server — scope enforcement, human_only bypass, API-key roles.

Tests cover:
1. Scope enforcement: read-only tools blocked for insufficient roles
2. human_only gate bypass attempts via MCP
3. API-key role restrictions: runner-role keys cannot call operator/admin tools
"""

from __future__ import annotations

import uuid
from collections.abc import Generator
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from modulo.core.mcp.scope_validator import (
    MCPAuthorizationError,
    check_tool_scope,
)

_FAKE_ID = "00000000-0000-0000-0000-000000000001"

# ── Scope enforcement ─────────────────────────────────────────────────────


class TestScopeEnforcement:
    """Verify the dual-layer scope enforcement blocks insufficiently privileged access."""

    @pytest.mark.parametrize(
        ("role", "tool"),
        [
            ("viewer", "trigger_pipeline"),
            ("viewer", "cancel_run"),
            ("viewer", "create_pipeline"),
            ("viewer", "create_model_backend"),
            ("viewer", "update_pipeline_graph"),
            ("viewer", "review_hitl"),
            ("viewer", "copy_library_primitive"),
            ("viewer", "list_pending_hitl"),
            ("viewer", "get_run_output"),
            ("viewer", "list_trigger_events"),
            ("viewer", "update_trigger"),
            ("viewer", "delete_trigger"),
            ("viewer", "get_trigger"),
            ("runner", "create_pipeline"),
            ("runner", "create_model_backend"),
            ("runner", "update_pipeline_graph"),
        ],
        ids=[
            "viewer-trigger_pipeline",
            "viewer-cancel_run",
            "viewer-create_pipeline",
            "viewer-create_model_backend",
            "viewer-update_pipeline_graph",
            "viewer-review_hitl",
            "viewer-copy_library_primitive",
            "viewer-list_pending_hitl",
            "viewer-get_run_output",
            "viewer-list_trigger_events",
            "viewer-update_trigger",
            "viewer-delete_trigger",
            "viewer-get_trigger",
            "runner-create_pipeline",
            "runner-create_model_backend",
            "runner-update_pipeline_graph",
        ],
    )
    def test_read_tool_blocked_for_low_role(self, role: str, tool: str) -> None:
        with pytest.raises(MCPAuthorizationError) as excinfo:
            check_tool_scope(role, tool)
        assert "Insufficient scope" in str(excinfo.value)

    @pytest.mark.parametrize(
        ("role", "tool"),
        [
            ("viewer", "list_pipelines"),
            ("viewer", "get_run_status"),
            ("runner", "list_pipelines"),
            ("runner", "get_run_status"),
            ("operator", "list_pipelines"),
            ("operator", "get_run_status"),
            ("admin", "list_pipelines"),
            ("admin", "get_run_status"),
        ],
        ids=[
            "viewer-list_pipelines",
            "viewer-get_run_status",
            "runner-list_pipelines",
            "runner-get_run_status",
            "operator-list_pipelines",
            "operator-get_run_status",
            "admin-list_pipelines",
            "admin-get_run_status",
        ],
    )
    def test_unrestricted_tools_work_for_all_roles(self, role: str, tool: str) -> None:
        result = check_tool_scope(role, tool)
        assert result is None

    def test_viewer_cannot_claim_hitl(self) -> None:
        with pytest.raises(MCPAuthorizationError):
            check_tool_scope("viewer", "review_hitl", action="claim")

    def test_runner_can_claim_hitl(self) -> None:
        assert check_tool_scope("runner", "review_hitl", action="claim") is None

    def test_viewer_cannot_approve_hitl(self) -> None:
        with pytest.raises(MCPAuthorizationError):
            check_tool_scope("viewer", "review_hitl", action="approve")

    def test_runner_cannot_approve_hitl(self) -> None:
        with pytest.raises(MCPAuthorizationError):
            check_tool_scope("runner", "review_hitl", action="approve")

    def test_operator_can_approve_hitl(self) -> None:
        assert check_tool_scope("operator", "review_hitl", action="approve") is None

    def test_admin_can_approve_hitl(self) -> None:
        assert check_tool_scope("admin", "review_hitl", action="approve") is None

    def test_runner_cannot_create_pipeline(self) -> None:
        with pytest.raises(MCPAuthorizationError):
            check_tool_scope("runner", "create_pipeline")

    def test_operator_can_create_pipeline(self) -> None:
        assert check_tool_scope("operator", "create_pipeline") is None

    def test_runner_cannot_create_model_backend(self) -> None:
        with pytest.raises(MCPAuthorizationError):
            check_tool_scope("runner", "create_model_backend")


# ── human_only gate bypass tests ──────────────────────────────────────────


class TestHumanOnlyGateBypass:
    """Verify human_only gates reject MCP-based approval.

    The ``review_hitl`` MCP tool must refuse to approve gates where
    the pipeline edge config has ``human_only=true``.
    """

    @pytest.fixture(autouse=True)
    def _patch_auth(self) -> Generator[None, None, None]:
        """Mock validate_current_auth and set tenant context so the tool handler runs."""
        import modulo.api.mcp_server as _ms

        org_token = _ms._ctx_org_id.set(uuid.UUID(_FAKE_ID))
        user_token = _ms._ctx_user_id.set(uuid.UUID(_FAKE_ID))
        try:
            with patch("modulo.api.mcp_server.validate_current_auth", return_value=True):
                yield
        finally:
            _ms._ctx_user_id.reset(user_token)
            _ms._ctx_org_id.reset(org_token)

    async def test_human_only_approve_rejected_via_mcp(self) -> None:
        from modulo.api.mcp_server import _ctx_role as _role
        from modulo.api.mcp_server import review_hitl as _rh

        _role.set("operator")
        with patch("modulo.api.mcp_server._session") as mock_session_factory:
            mock_session = AsyncMock(name="session")
            mock_session_factory.return_value.__aenter__.return_value = mock_session

            gate_row = MagicMock()
            gate_row.run_id = _FAKE_ID
            gate_row.gate_id = "gate-human-only"
            gate_row.organisation_id = _FAKE_ID
            gate_row.pipeline_id = _FAKE_ID

            edge_row = MagicMock()
            edge_row.hitl_gate_config = {"human_only": True}

            hitl_result = MagicMock()
            hitl_result.scalar_one_or_none.return_value = gate_row
            edge_result = MagicMock()
            edge_result.scalars.return_value.first.return_value = edge_row
            run_result = MagicMock()
            run_result.scalar_one_or_none.return_value = MagicMock()  # the run

            mock_session.execute.side_effect = [run_result, hitl_result, edge_result]

            result = await _rh(
                run_id=_FAKE_ID,
                gate_id="gate-human-only",
                action="approve",
                claim_token="test-token",
            )

        assert result.get("error") == "human_only_gate", f"Expected human_only_gate error, got {result}"

    async def test_non_human_only_approve_proceeds(self) -> None:
        """A gate without human_only=true should not be blocked."""
        from modulo.api.mcp_server import _ctx_role as _role
        from modulo.api.mcp_server import review_hitl as _rh

        _role.set("operator")
        with patch("modulo.api.mcp_server._session") as mock_session_factory:
            mock_session = AsyncMock(name="session")
            mock_session_factory.return_value.__aenter__.return_value = mock_session

            gate_row = MagicMock()
            gate_row.run_id = _FAKE_ID
            gate_row.gate_id = "gate-normal"
            gate_row.organisation_id = _FAKE_ID
            gate_row.pipeline_id = _FAKE_ID

            edge_row = MagicMock()
            edge_row.hitl_gate_config = {"human_only": False}

            hitl_result = MagicMock()
            hitl_result.scalar_one_or_none.return_value = gate_row
            edge_result = MagicMock()
            edge_result.scalars.return_value.first.return_value = edge_row
            run_result = MagicMock()
            run_result.scalar_one_or_none.return_value = MagicMock()  # the run

            mock_session.execute.side_effect = [run_result, hitl_result, edge_result]

            with patch("modulo.api.mcp_server.HITLManager") as mock_mgr:
                mock_mgr_instance = AsyncMock()
                mock_mgr.return_value = mock_mgr_instance
                mock_mgr_instance.approve = AsyncMock(return_value=MagicMock(status="approved"))

                result = await _rh(
                    run_id=_FAKE_ID,
                    gate_id="gate-normal",
                    action="approve",
                    claim_token="test-token",
                )

        assert result.get("error") != "human_only_gate", f"Non-human_only gate should not be blocked: {result}"

    async def test_human_only_bypass_fails_for_runner_role(self) -> None:
        """Runner role claiming a human_only gate should work, but approve blocked by scope."""
        from modulo.api.mcp_server import _ctx_role as _role
        from modulo.api.mcp_server import review_hitl as _rh

        _role.set("runner")
        with patch("modulo.api.mcp_server._session") as mock_session_factory:
            mock_session = AsyncMock(name="session")
            mock_session_factory.return_value.__aenter__.return_value = mock_session
            mock_session.execute.side_effect = [MagicMock(), MagicMock()]

            result = await _rh(
                run_id=_FAKE_ID,
                gate_id="gate-human-only",
                action="approve",
                claim_token="test-token",
            )

        assert result.get("error") == "insufficient_scope", (
            f"Runner should get insufficient_scope for approve, got {result}"
        )


# ── API-key role restrictions ─────────────────────────────────────────────


class TestApiKeyRoleRestrictions:
    """Verify that API-key role (runner/operator) is enforced on MCP tools.

    A runner-role API key must not be able to call operator-scoped tools.
    """

    def test_runner_key_cannot_create_pipeline(self) -> None:
        with pytest.raises(MCPAuthorizationError) as excinfo:
            check_tool_scope("runner", "create_pipeline")
        assert "requires 'operator' role" in str(excinfo.value)

    def test_runner_key_cannot_create_model_backend(self) -> None:
        with pytest.raises(MCPAuthorizationError) as excinfo:
            check_tool_scope("runner", "create_model_backend")
        assert "requires 'operator' role" in str(excinfo.value)

    def test_runner_key_cannot_approve_hitl(self) -> None:
        with pytest.raises(MCPAuthorizationError) as excinfo:
            check_tool_scope("runner", "review_hitl", action="approve")
        assert "requires 'operator' role" in str(excinfo.value)

    def test_runner_key_can_trigger_pipeline(self) -> None:
        assert check_tool_scope("runner", "trigger_pipeline") is None

    def test_runner_key_can_cancel_run(self) -> None:
        assert check_tool_scope("runner", "cancel_run") is None

    def test_runner_key_can_claim_hitl(self) -> None:
        assert check_tool_scope("runner", "review_hitl", action="claim") is None

    def test_operator_key_can_create_pipeline(self) -> None:
        assert check_tool_scope("operator", "create_pipeline") is None

    def test_operator_key_can_create_model_backend(self) -> None:
        assert check_tool_scope("operator", "create_model_backend") is None

    @pytest.mark.parametrize(
        ("role", "tool"),
        [
            ("runner", "trigger_pipeline"),
            ("operator", "trigger_pipeline"),
            ("admin", "trigger_pipeline"),
            ("operator", "create_pipeline"),
            ("admin", "create_pipeline"),
            ("runner", "cancel_run"),
            ("runner", "get_trigger"),
            ("operator", "update_trigger"),
            ("operator", "delete_trigger"),
            ("viewer", "list_pipelines"),
        ],
        ids=[
            "runner-trigger_pipeline",
            "operator-trigger_pipeline",
            "admin-trigger_pipeline",
            "operator-create_pipeline",
            "admin-create_pipeline",
            "runner-cancel_run",
            "runner-get_trigger",
            "operator-update_trigger",
            "operator-delete_trigger",
            "viewer-list_pipelines",
        ],
    )
    def test_role_hierarchy_permits_upward(self, role: str, tool: str) -> None:
        """Higher roles inherit all permissions of lower roles."""
        assert check_tool_scope(role, tool) is None


# ── Null session / invalid token edge cases ────────────────────────────────


class TestNullSessionEdgeCases:
    """Edge cases: null roles, invalid tokens, missing auth context."""

    def test_null_role_raises(self) -> None:
        with pytest.raises(MCPAuthorizationError) as excinfo:
            check_tool_scope(None, "trigger_pipeline")
        assert "No authentication context" in str(excinfo.value)

    def test_non_string_tool_name_raises(self) -> None:
        with pytest.raises(MCPAuthorizationError) as excinfo:
            check_tool_scope("admin", 123)  # type: ignore[arg-type]
        assert "must be a string" in str(excinfo.value)

    def test_empty_tool_name_raises(self) -> None:
        with pytest.raises(MCPAuthorizationError) as excinfo:
            check_tool_scope("admin", "")
        assert "empty or whitespace-only" in str(excinfo.value)

    def test_whitespace_tool_name_raises(self) -> None:
        with pytest.raises(MCPAuthorizationError) as excinfo:
            check_tool_scope("admin", "   ")
        assert "empty or whitespace-only" in str(excinfo.value)

    def test_unknown_role_fails_gracefully(self) -> None:
        with pytest.raises(MCPAuthorizationError) as excinfo:
            check_tool_scope("superadmin", "trigger_pipeline")
        assert "Unknown role" in str(excinfo.value)
