"""Unit tests for MCP dual-layer scope validation.

Tests the ViewModel-level scope checks independently of the middleware,
and verifies integration through the MCP tool handlers.
"""

import uuid
from collections.abc import Generator
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from modulo.core.mcp.scope_validator import (
    TOOL_SCOPE_REQUIREMENTS,
    MCPAuthorizationError,
    MCPConfigurationError,
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
            ("runner", "list_housekeeping"),
            ("admin", "perform_housekeeping"),
            ("operator", "create_connector"),
            ("admin", "create_connector"),
            ("operator", "create_trigger"),
            ("admin", "create_trigger"),
            ("operator", "delete_pipeline"),
            ("admin", "delete_pipeline"),
            ("operator", "create_agent"),
            ("admin", "create_agent"),
            ("operator", "infer_schema"),
            ("admin", "infer_schema"),
            ("viewer", "query_analytics"),
            ("runner", "query_analytics"),
            ("operator", "query_analytics"),
            ("admin", "query_analytics"),
            ("operator", "review_hitl"),
            ("admin", "review_hitl"),
        ],
        ids=[
            "runner-trigger_pipeline",
            "operator-trigger_pipeline",
            "admin-trigger_pipeline",
            "runner-cancel_run",
            "runner-list_pending_hitl",
            "runner-copy_library_primitive",
            "runner-list_housekeeping",
            "admin-perform_housekeeping",
            "operator-create_connector",
            "admin-create_connector",
            "operator-create_trigger",
            "admin-create_trigger",
            "operator-delete_pipeline",
            "admin-delete_pipeline",
            "operator-create_agent",
            "admin-create_agent",
            "operator-infer_schema",
            "admin-infer_schema",
            "viewer-query_analytics",
            "runner-query_analytics",
            "operator-query_analytics",
            "admin-query_analytics",
            "operator-review_hitl",
            "admin-review_hitl",
        ],
    )
    def test_authorized_role_passes(self, role: str, tool: str) -> None:
        assert check_tool_scope(role, tool) is None

    def test_query_analytics_resolves_to_viewer(self) -> None:
        from modulo.auth.permissions import resolve_required
        from modulo.core.mcp.scope_validator import TOOL_SCOPE_REQUIREMENTS

        assert TOOL_SCOPE_REQUIREMENTS["query_analytics"] == "analytics.query"
        assert resolve_required("analytics.query") == "viewer"
        # all four roles pass at the viewer boundary
        for role in ("viewer", "runner", "operator", "admin"):
            assert check_tool_scope(role, "query_analytics") is None

    @pytest.mark.parametrize(
        ("role", "tool"),
        [
            ("viewer", "trigger_pipeline"),
            ("viewer", "cancel_run"),
            ("viewer", "list_pending_hitl"),
            ("viewer", "copy_library_primitive"),
            ("viewer", "review_hitl"),
            ("viewer", "list_housekeeping"),
            ("runner", "review_hitl"),
            ("runner", "perform_housekeeping"),
            ("viewer", "create_connector"),
            ("runner", "create_connector"),
            ("viewer", "create_trigger"),
            ("runner", "create_trigger"),
            ("viewer", "delete_pipeline"),
            ("runner", "delete_pipeline"),
            ("viewer", "create_agent"),
            ("runner", "create_agent"),
            ("viewer", "infer_schema"),
            ("runner", "infer_schema"),
        ],
        ids=[
            "viewer-trigger_pipeline",
            "viewer-cancel_run",
            "viewer-list_pending_hitl",
            "viewer-copy_library_primitive",
            "viewer-review_hitl",
            "viewer-list_housekeeping",
            "runner-review_hitl",
            "runner-perform_housekeeping",
            "viewer-create_connector",
            "runner-create_connector",
            "viewer-create_trigger",
            "runner-create_trigger",
            "viewer-delete_pipeline",
            "runner-delete_pipeline",
            "viewer-create_agent",
            "runner-create_agent",
            "viewer-infer_schema",
            "runner-infer_schema",
        ],
    )
    def test_unauthorized_role_raises(self, role: str, tool: str) -> None:
        with pytest.raises(MCPAuthorizationError) as excinfo:
            check_tool_scope(role, tool)
        assert "Insufficient scope" in str(excinfo.value)
        assert tool in str(excinfo.value)

    @pytest.mark.parametrize("role", ["viewer", "runner", "operator", "admin"])
    def test_tools_without_scope_req_always_pass(self, role: str) -> None:
        assert check_tool_scope(role, "list_pipelines") is None
        assert check_tool_scope(role, "get_run_status") is None

    def test_none_role_raises(self) -> None:
        with pytest.raises(MCPAuthorizationError) as excinfo:
            check_tool_scope(None, "trigger_pipeline")
        assert "No authentication context" in str(excinfo.value)

    def test_unknown_role_raises(self) -> None:
        with pytest.raises(MCPAuthorizationError) as excinfo:
            check_tool_scope("superadmin", "trigger_pipeline")
        assert "Unknown role" in str(excinfo.value)

    def test_empty_tool_name_raises(self) -> None:
        with pytest.raises(MCPAuthorizationError) as excinfo:
            check_tool_scope("admin", "")
        assert "empty or whitespace-only" in str(excinfo.value)

    def test_whitespace_tool_name_raises(self) -> None:
        with pytest.raises(MCPAuthorizationError) as excinfo:
            check_tool_scope("admin", "   ")
        assert "empty or whitespace-only" in str(excinfo.value)

    def test_empty_action_raises(self) -> None:
        with pytest.raises(MCPAuthorizationError) as excinfo:
            check_tool_scope("admin", "review_hitl", action="")
        assert "empty or whitespace-only" in str(excinfo.value)

    def test_whitespace_action_raises(self) -> None:
        with pytest.raises(MCPAuthorizationError) as excinfo:
            check_tool_scope("admin", "review_hitl", action="   ")
        assert "empty or whitespace-only" in str(excinfo.value)

    def test_unknown_action_for_tool_raises(self) -> None:
        with pytest.raises(MCPAuthorizationError) as excinfo:
            check_tool_scope("admin", "trigger_pipeline", action="approve")
        assert "Unknown action" in str(excinfo.value)
        assert "trigger_pipeline" in str(excinfo.value)

    def test_case_insensitive_tool_name(self) -> None:
        with pytest.raises(MCPAuthorizationError):
            check_tool_scope("viewer", "TRIGGER_PIPELINE")
        with pytest.raises(MCPAuthorizationError):
            check_tool_scope("viewer", "Trigger_Pipeline")

    def test_case_insensitive_action(self) -> None:
        with pytest.raises(MCPAuthorizationError):
            check_tool_scope("viewer", "review_hitl", action="CLAIM")
        with pytest.raises(MCPAuthorizationError):
            check_tool_scope("viewer", "review_hitl", action="Approve")

    def test_non_string_tool_name_raises(self) -> None:
        with pytest.raises(MCPAuthorizationError) as excinfo:
            check_tool_scope("admin", 123)  # type: ignore[arg-type]
        assert "must be a string" in str(excinfo.value)

    def test_non_string_action_raises(self) -> None:
        with pytest.raises(MCPAuthorizationError) as excinfo:
            check_tool_scope("admin", "review_hitl", action=456)  # type: ignore[arg-type]
        assert "must be a string" in str(excinfo.value)

    def test_empty_string_role_passes_lookup_then_raises_unknown(self) -> None:
        with pytest.raises(MCPAuthorizationError) as excinfo:
            check_tool_scope("", "trigger_pipeline")
        assert "Unknown role" in str(excinfo.value)

    def test_none_type_tool_name_raises(self) -> None:
        with pytest.raises(MCPAuthorizationError) as excinfo:
            check_tool_scope("admin", None)  # type: ignore[arg-type]
        assert "must be a string" in str(excinfo.value)


class TestReviewHitlActionScopes:
    """Action-level scoping for the ``review_hitl`` tool."""

    def test_claim_requires_runner(self) -> None:
        assert check_tool_scope("runner", "review_hitl", action="claim") is None

    def test_claim_rejects_viewer(self) -> None:
        with pytest.raises(MCPAuthorizationError):
            check_tool_scope("viewer", "review_hitl", action="claim")

    def test_approve_requires_operator(self) -> None:
        assert check_tool_scope("operator", "review_hitl", action="approve") is None

    def test_approve_rejects_runner(self) -> None:
        with pytest.raises(MCPAuthorizationError):
            check_tool_scope("runner", "review_hitl", action="approve")

    def test_reject_requires_operator(self) -> None:
        assert check_tool_scope("operator", "review_hitl", action="reject") is None

    def test_reject_rejects_runner(self) -> None:
        with pytest.raises(MCPAuthorizationError):
            check_tool_scope("runner", "review_hitl", action="reject")

    def test_no_action_requires_operator(self) -> None:
        with pytest.raises(MCPAuthorizationError):
            check_tool_scope("runner", "review_hitl")

    def test_deliver_manual_requires_operator(self) -> None:
        assert check_tool_scope("operator", "review_hitl", action="deliver_manual") is None
        assert check_tool_scope("admin", "review_hitl", action="deliver_manual") is None

    def test_deliver_manual_rejects_runner(self) -> None:
        with pytest.raises(MCPAuthorizationError):
            check_tool_scope("runner", "review_hitl", action="deliver_manual")

    def test_deliver_manual_rejects_viewer(self) -> None:
        with pytest.raises(MCPAuthorizationError):
            check_tool_scope("viewer", "review_hitl", action="deliver_manual")


class TestNewlyGuardedTools:
    """The 5 previously-unguarded tools now enforce their roles."""

    @pytest.mark.parametrize(
        ("role", "tool"),
        [
            ("viewer", "delete_connector"),
            ("runner", "delete_connector"),
            ("viewer", "create_secret"),
            ("runner", "create_secret"),
            ("viewer", "delete_secret"),
            ("runner", "delete_secret"),
            ("viewer", "list_secrets"),
            ("runner", "list_secrets"),
        ],
        ids=[
            "viewer-delete_connector",
            "runner-delete_connector",
            "viewer-create_secret",
            "runner-create_secret",
            "viewer-delete_secret",
            "runner-delete_secret",
            "viewer-list_secrets",
            "runner-list_secrets",
        ],
    )
    def test_low_role_denied(self, role: str, tool: str) -> None:
        with pytest.raises(MCPAuthorizationError):
            check_tool_scope(role, tool)

    @pytest.mark.parametrize(
        ("role", "tool"),
        [
            ("operator", "delete_connector"),
            ("admin", "delete_connector"),
            ("operator", "create_secret"),
            ("admin", "create_secret"),
            ("operator", "delete_secret"),
            ("admin", "delete_secret"),
            ("operator", "list_secrets"),
            ("admin", "list_secrets"),
            ("runner", "list_trigger_events"),
            ("operator", "list_trigger_events"),
            ("admin", "list_trigger_events"),
        ],
        ids=[
            "operator-delete_connector",
            "admin-delete_connector",
            "operator-create_secret",
            "admin-create_secret",
            "operator-delete_secret",
            "admin-delete_secret",
            "operator-list_secrets",
            "admin-list_secrets",
            "runner-list_trigger_events",
            "operator-list_trigger_events",
            "admin-list_trigger_events",
        ],
    )
    def test_authorized_role_passes(self, role: str, tool: str) -> None:
        assert check_tool_scope(role, tool) is None

    def test_viewer_denied_list_trigger_events(self) -> None:
        with pytest.raises(MCPAuthorizationError):
            check_tool_scope("viewer", "list_trigger_events")


class TestAllowedToolsNarrowing:
    """FAR-436: node-level ``allowed_tools`` narrows (never widens) the chokepoint.

    The role check must STILL pass for the tool (narrowing is an additional
    filter, never a bypass), and when a node declares ``allowed_tools`` an
    out-of-scope tool is rejected. Only an ABSENT allow-list (None) is
    unrestricted; an explicit EMPTY allow-list is deny-by-default.
    """

    def test_no_allowed_tools_is_unrestricted(self) -> None:
        # Absent allow-list (None) -> role check only (legacy behaviour).
        assert check_tool_scope("runner", "trigger_pipeline") is None

    def test_in_scope_tool_passes(self) -> None:
        assert check_tool_scope("admin", "create_pipeline", allowed_tools=["create_pipeline"]) is None

    def test_out_of_scope_tool_rejected_despite_valid_role(self) -> None:
        with pytest.raises(MCPAuthorizationError, match="allowed_tools scope"):
            check_tool_scope("admin", "create_pipeline", allowed_tools=["create_agent"])

    def test_empty_allow_list_is_deny_by_default(self) -> None:
        # A node granted no tools may call none — even with a valid role.
        with pytest.raises(MCPAuthorizationError, match="allowed_tools scope"):
            check_tool_scope("admin", "create_pipeline", allowed_tools=[])

    def test_matching_is_case_insensitive(self) -> None:
        assert check_tool_scope("admin", "CREATE_PIPELINE", allowed_tools=["create_pipeline"]) is None

    def test_scope_does_not_bypass_role_check(self) -> None:
        # Narrowing is additive, never a grant: an in-scope tool still needs
        # the role (viewer lacks pipeline.create -> denied by the role check).
        with pytest.raises(MCPAuthorizationError):
            check_tool_scope("viewer", "create_pipeline", allowed_tools=["create_pipeline"])


class TestMCPAuthorizationError:
    """MCPAuthorizationError behaviour."""

    def test_message_attribute(self) -> None:
        exc = MCPAuthorizationError("test message")
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
            "review_hitl:claim",
            "review_hitl:approve",
            "review_hitl:reject",
            "review_hitl:deliver_manual",
            "copy_library_primitive",
            "list_pending_hitl",
            "get_run_output",
            "create_pipeline",
            "update_pipeline_graph",
            "create_model_backend",
            "list_runs",
            "get_run_evals",
            "list_eval_definitions",
            "create_eval_definition",
            "update_eval_definition",
            "delete_eval_definition",
            "bind_connector_to_node",
            "list_triggers",
            "get_trigger",
            "update_trigger",
            "delete_trigger",
            "set_org_triggers_paused",
            "list_housekeeping",
            "perform_housekeeping",
            "create_connector",
            "delete_connector",
            "create_trigger",
            "delete_pipeline",
            "create_agent",
            "create_schema",
            "infer_schema",
            "create_secret",
            "delete_secret",
            "list_secrets",
            "create_api_key",
            "list_api_keys",
            "revoke_api_key",
            "list_trigger_events",
            "query_analytics",
            "query_analytics_concurrency",
        }
        assert set(TOOL_SCOPE_REQUIREMENTS) == expected_tools

    def test_tool_requirements_resolve_to_valid_roles(self) -> None:
        valid_roles = {"viewer", "runner", "operator", "admin"}
        from modulo.auth.permissions import resolve_required

        for tool, permission_key in TOOL_SCOPE_REQUIREMENTS.items():
            role = resolve_required(permission_key)
            assert role in valid_roles, f"{tool} resolves to invalid role '{role}'"

    def test_unregistered_tool_denied_by_default(self) -> None:
        with pytest.raises(MCPAuthorizationError) as excinfo:
            check_tool_scope("viewer", "some_unknown_tool")
        assert "not registered in the scope policy" in str(excinfo.value)

    def test_read_only_tools_pinned_at_viewer(self) -> None:
        for tool in ("list_pipelines", "get_run_status", "search_library", "get_pipeline_graph"):
            assert check_tool_scope("viewer", tool) is None
        with pytest.raises(MCPAuthorizationError):
            check_tool_scope(None, "list_pipelines")

    def test_mcp_configuration_error_is_exception(self) -> None:
        assert issubclass(MCPConfigurationError, Exception)

    def test_tool_scope_requirements_immutable_keys(self) -> None:
        # Ensure TOOL_SCOPE_REQUIREMENTS contains all expected tool keys
        assert "trigger_pipeline" in TOOL_SCOPE_REQUIREMENTS
        assert "create_model_backend" in TOOL_SCOPE_REQUIREMENTS


_FAKE_ID = "00000000-0000-0000-0000-000000000001"


class TestToolHandlerScopeErrorFormat:
    """Tool handlers return ``insufficient_scope`` error when scope check fails."""

    pytestmark = pytest.mark.asyncio

    @pytest.fixture(autouse=True)
    def _patch_auth(self) -> Generator[None, None, None]:
        """Mock ``validate_current_auth`` and set auth context so scope checks are reached."""
        from modulo.api.mcp_server import _ctx_org_id

        token = _ctx_org_id.set(uuid.UUID(_FAKE_ID))
        with patch("modulo.api.mcp_server.validate_current_auth", return_value=True):
            yield
        _ctx_org_id.reset(token)

    @pytest.mark.parametrize(
        ("handler_name", "kwargs"),
        [
            ("trigger_pipeline", {"pipeline_id": _FAKE_ID}),
            ("cancel_run", {"run_id": _FAKE_ID}),
            ("list_pending_hitl", {}),
            ("copy_library_primitive", {"primitive_id": _FAKE_ID}),
            ("review_hitl", {"run_id": _FAKE_ID, "gate_id": "gate-1", "action": "claim"}),
        ],
    )
    async def test_insufficient_scope_when_role_none(
        self,
        handler_name: str,
        kwargs: dict[str, str],
    ) -> None:
        import importlib

        mcp = importlib.import_module("modulo.api.mcp_server")
        handler = getattr(mcp, handler_name)
        mcp._ctx_role.set(None)
        result = await handler(**kwargs)
        assert result == {
            "error": "insufficient_scope",
            "detail": "No authentication context: role not set",
        }

    async def test_review_hitl_approve_requires_operator(self) -> None:
        from modulo.api.mcp_server import _ctx_role as _role
        from modulo.api.mcp_server import review_hitl as _rh

        _role.set("viewer")
        result = await _rh(
            run_id=_FAKE_ID,
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
            run_id=_FAKE_ID,
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
        gate = MagicMock(claim_token="claim-token", expires_at=None)
        with (
            patch("modulo.api.mcp_server._session") as mock_session,
            patch("modulo.api.mcp_server.HITLManager") as manager_class,
        ):
            mock_session.return_value.__aenter__.return_value = AsyncMock()
            manager_class.return_value.claim = AsyncMock(return_value=gate)
            result = await _rh(
                run_id=_FAKE_ID,
                gate_id="gate-1",
                action="claim",
            )
            assert result.get("error") != "insufficient_scope"

    async def test_list_pipelines_no_scope_check(self) -> None:
        from modulo.api.mcp_server import _ctx_role as _role
        from modulo.api.mcp_server import list_pipelines_tool as _lpt

        _role.set(None)
        page = MagicMock(items=[], total=0, next_cursor=None, has_more=False)
        with (
            patch("modulo.api.mcp_server._session"),
            patch("modulo.db.crud.pipeline.list_pipelines", new=AsyncMock(return_value=page)),
        ):
            result = await _lpt()
        assert "insufficient_scope" not in result

    async def test_get_run_status_no_scope_check(self) -> None:
        from modulo.api.mcp_server import _ctx_role as _role
        from modulo.api.mcp_server import get_run_status as _grs

        _role.set(None)
        with (
            patch("modulo.api.mcp_server._session"),
            patch("modulo.api.mcp_server.get_run", new=AsyncMock(return_value=None)),
        ):
            result = await _grs(run_id=_FAKE_ID)
        assert "insufficient_scope" not in result
