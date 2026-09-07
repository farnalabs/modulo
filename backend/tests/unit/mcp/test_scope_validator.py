"""Unit tests for MCP dual-layer scope validation.

Tests the ViewModel-level scope checks independently of the middleware,
and verifies integration through the MCP tool handlers.

FAR-620: the pure resolver ``resolve_tool_access`` (decision + permission
key) is exercised table-driven over the caller-scope dimension; the
``check_tool_scope`` delegation is pinned by a seam test.
"""

import uuid
from collections.abc import Generator
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from modulo.core.mcp.scope_validator import (
    CALLER_SCOPE_REQUIREMENTS,
    TOOL_SCOPE_REQUIREMENTS,
    VALID_CALLER_SCOPE_CLASSIFICATIONS,
    MCPAuthorizationError,
    MCPConfigurationError,
    check_tool_scope,
    classify_caller_scope,
    resolve_tool_access,
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
            "list_hitl_gates",
            "get_hitl_gate",
            "get_pipeline_gates",
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
            "get_hitl_email_alerts",
            "set_hitl_email_alerts",
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


# ---------------------------------------------------------------------------
# FAR-620: the pure resolver + caller-scope dimension
# ---------------------------------------------------------------------------

# A synthetic caller-scoped tool for the matrix. The real ``.self`` MCP tools
# (get/set_hitl_email_alerts) now exist, but the matrix stays table-driven on a
# test-only registry patch keyed on the EXISTING ``notification.self``
# permission key (a real ``.self`` entry with no MCP tool mapped to it) - the
# machinery under test is exactly what the real ``.self`` tools flow through.
_CALLER_SCOPED_TEST_TOOL = "notification_self"


def _patch_caller_scoped_tool(monkeypatch: pytest.MonkeyPatch) -> None:
    """Register a synthetic caller-scoped tool in the scope maps for one test."""
    import types

    import modulo.core.mcp.scope_validator as sv

    patched = {
        **sv._TOOL_SCOPE_REQUIREMENTS,
        _CALLER_SCOPED_TEST_TOOL: "notification.self",
    }
    # TOOL_SCOPE_REQUIREMENTS is a read-only proxy built from the private dict;
    # point BOTH names at the patched mapping so the resolver's lookups see
    # the synthetic tool.
    monkeypatch.setattr(sv, "_TOOL_SCOPE_REQUIREMENTS", patched)
    monkeypatch.setattr(sv, "TOOL_SCOPE_REQUIREMENTS", types.MappingProxyType(patched))


class TestResolveToolAccessMatrix:
    """Table-driven matrix over the pure resolver (FAR-620 3g).

    Axes: key_scope {None, org, user} x stored role {runner, operator} x
    kill_switch {on, off} x classification {org-only, caller-scoped, any}.
    The live-membership axis is exercised via the clamp tests
    (``tests/unit/auth/test_api_key_cap.py``); the resolver receives the
    ALREADY-clamped role.
    """

    @pytest.mark.parametrize(
        ("key_scope", "auth_type", "tool", "role", "kill_switch", "expected"),
        [
            pytest.param("org", "api_key", "create_pipeline", "operator", True, True, id="orgkey-op-ks-on"),
            pytest.param("org", "api_key", "create_pipeline", "operator", False, True, id="orgkey-op-ks-off"),
            # pipeline.create pins at operator — the role leg still denies a
            # runner (the caller-scope leg is orthogonal).
            pytest.param("org", "api_key", "create_pipeline", "runner", True, False, id="orgkey-runner-denied"),
            pytest.param("user", "api_key", "create_pipeline", "operator", True, False, id="userkey-orgtool"),
            # combined-legs row: kill_switch OFF bypasses the role leg only —
            # the caller-scope leg still denies a user-scoped KEY on an
            # org-only tool.
            pytest.param("user", "api_key", "create_pipeline", "admin", False, False, id="userkey-ks-off-combined"),
            # Identity-bound JWT/OAuth sessions keep today's org-only access.
            pytest.param("user", "jwt", "create_pipeline", "operator", True, True, id="jwt-op-orgtool"),
            pytest.param("user", "oauth", "create_pipeline", "operator", True, True, id="oauth-op-orgtool"),
            pytest.param("user", "jwt", "create_pipeline", "admin", False, True, id="jwt-admin-ks-off"),
            # Unset context fails closed on the caller-scope leg only for
            # caller-scoped tools; org-only tools keep legacy behaviour.
            pytest.param(None, None, "create_pipeline", "operator", True, True, id="unset-orgtool-legacy"),
            # ── explicitly org-only: create_api_key (FAR-620 mint surface) ──
            pytest.param("org", "api_key", "create_api_key", "operator", True, True, id="orgkey-mint-ok"),
            pytest.param("user", "api_key", "create_api_key", "admin", True, False, id="userkey-mint-denied"),
            pytest.param("user", "api_key", "create_api_key", "admin", False, False, id="userkey-mint-ks-elig"),
            pytest.param("user", "jwt", "create_api_key", "admin", True, True, id="jwt-mint-ok"),
            pytest.param("user", "oauth", "create_api_key", "admin", True, True, id="oauth-mint-ok"),
            # ── read-only tools classify 'any' (today's default preserved) ──
            pytest.param("org", "api_key", "list_pipelines", "viewer", True, True, id="orgkey-readonly"),
            pytest.param("user", "api_key", "list_pipelines", "viewer", True, True, id="userkey-readonly"),
            pytest.param(None, None, "list_pipelines", "viewer", True, True, id="unset-readonly"),
            # ── caller-scoped tools (synthetic ``.self`` permission key) ────
            pytest.param("user", "api_key", _CALLER_SCOPED_TEST_TOOL, "viewer", True, True, id="userkey-self-ok"),
            pytest.param("user", "jwt", _CALLER_SCOPED_TEST_TOOL, "viewer", True, True, id="jwt-self-ok"),
            pytest.param("user", "oauth", _CALLER_SCOPED_TEST_TOOL, "viewer", True, True, id="oauth-self-ok"),
            # Org keys — org-wide, team-scoped AND run-scoped (all 'org') —
            # are DENIED caller-scoped tools, kill switch ON or OFF.
            pytest.param("org", "api_key", _CALLER_SCOPED_TEST_TOOL, "admin", True, False, id="orgkey-self-denied"),
            pytest.param(
                "org", "api_key", _CALLER_SCOPED_TEST_TOOL, "admin", False, False, id="orgkey-self-denied-ks-elig"
            ),
            # Unset key scope fails closed.
            pytest.param(None, None, _CALLER_SCOPED_TEST_TOOL, "admin", True, False, id="unset-self-failclosed"),
            pytest.param(
                None, None, _CALLER_SCOPED_TEST_TOOL, "admin", False, False, id="unset-self-failclosed-ks-elig"
            ),
            # ── role leg still applies to caller-scoped callers ─────────────
            # hitl_email.self will pin at viewer; an unknown role fails closed
            # regardless of key_scope.
        ],
    )
    def test_matrix(
        self,
        monkeypatch: pytest.MonkeyPatch,
        key_scope: str | None,
        auth_type: str | None,
        tool: str,
        role: str,
        kill_switch: bool,
        expected: bool,
    ) -> None:
        _patch_caller_scoped_tool(monkeypatch)
        allowed, permission_key = resolve_tool_access(
            tool=tool,
            action=None,
            role=role,
            key_scope=key_scope,
            auth_type=auth_type,
            allowed_tools=None,
            kill_switch=kill_switch,
        )
        assert allowed is expected, f"{tool} key_scope={key_scope} role={role} kill_switch={kill_switch}"
        if expected:
            assert permission_key != ""

    def test_unknown_role_fails_closed_regardless_of_kill_switch(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _patch_caller_scoped_tool(monkeypatch)
        for kill_switch in (True, False):
            allowed, _ = resolve_tool_access(
                tool="create_pipeline",
                action=None,
                role="superadmin",
                key_scope="org",
                auth_type="api_key",
                allowed_tools=None,
                kill_switch=kill_switch,
            )
            assert allowed is False

    def test_none_role_fails_closed(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _patch_caller_scoped_tool(monkeypatch)
        allowed, _ = resolve_tool_access(
            tool="create_pipeline",
            action=None,
            role=None,
            key_scope="org",
            auth_type="api_key",
            allowed_tools=None,
            kill_switch=False,
        )
        assert allowed is False

    def test_kill_switch_off_bypasses_role_leg_only(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """kill_switch=False (enforcement OFF) fail-opens ONLY the hierarchy
        comparison: a viewer caller passes an operator tool, but the
        caller-scope and resolution legs still deny."""
        _patch_caller_scoped_tool(monkeypatch)
        # Role leg bypassed: viewer + operator tool → allowed.
        allowed, _ = resolve_tool_access(
            tool="create_pipeline",
            action=None,
            role="viewer",
            key_scope="org",
            auth_type="api_key",
            allowed_tools=None,
            kill_switch=False,
        )
        assert allowed is True
        # Caller-scope leg NOT bypassed: user key + org-only tool → denied
        # (the combined-legs row).
        allowed, _ = resolve_tool_access(
            tool="create_pipeline",
            action=None,
            role="viewer",
            key_scope="user",
            auth_type="api_key",
            allowed_tools=None,
            kill_switch=False,
        )
        assert allowed is False

    def test_allowed_tools_narrowing_in_resolver(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """FAR-418/436 narrowing semantics preserved exactly inside the
        resolver: absent = unrestricted; out-of-list = deny; empty = deny."""
        _patch_caller_scoped_tool(monkeypatch)
        # Absent allow-list → role check only.
        allowed, _ = resolve_tool_access("create_pipeline", None, "operator", "org", "api_key", None, True)
        assert allowed is True
        # Tool on the list → allowed.
        allowed, _ = resolve_tool_access(
            "create_pipeline", None, "operator", "org", "api_key", {"create_pipeline"}, True
        )
        assert allowed is True
        # Tool NOT on the list → denied despite a valid role.
        allowed, _key = resolve_tool_access("create_pipeline", None, "admin", "org", "api_key", {"create_agent"}, True)
        assert allowed is False
        # An explicit EMPTY allow-list is deny-by-default.
        allowed, _ = resolve_tool_access("create_pipeline", None, "admin", "org", "api_key", set(), True)
        assert allowed is False
        # Narrowing never widens: the caller-scope leg still denies a
        # user-scoped key even when the tool IS on the node's list.
        allowed, _ = resolve_tool_access("create_api_key", None, "admin", "user", "api_key", {"create_api_key"}, True)
        assert allowed is False

    def test_return_shape_is_decision_plus_permission_key(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _patch_caller_scoped_tool(monkeypatch)
        allowed, permission_key = resolve_tool_access("create_pipeline", None, "operator", "org", "api_key", None, True)
        assert allowed is True
        assert permission_key == TOOL_SCOPE_REQUIREMENTS["create_pipeline"]
        # Unresolvable tool — (False, "").
        allowed, permission_key = resolve_tool_access("unknown_tool", None, "admin", "org", "api_key", None, True)
        assert allowed is False
        assert permission_key == ""


class TestCallerScopeClassificationFailFast:
    """FAR-620: an out-of-vocabulary caller-scope classification must never be
    silently treated as 'any' (unrestricted) - it raises ``MCPConfigurationError``.
    The same vocabulary check runs at import time over the pinned map, so a
    typo'd pin fails the process at boot rather than widening access at run."""

    @staticmethod
    def _patch_misspelled_classification(monkeypatch: pytest.MonkeyPatch) -> None:
        """Pin ``create_pipeline`` with a typo'd classification for one test."""
        import types

        import modulo.core.mcp.scope_validator as sv

        patched = {**sv._CALLER_SCOPE_REQUIREMENTS, "create_pipeline": "orgonly"}
        monkeypatch.setattr(sv, "_CALLER_SCOPE_REQUIREMENTS", patched)
        monkeypatch.setattr(sv, "CALLER_SCOPE_REQUIREMENTS", types.MappingProxyType(patched))

    def test_import_time_pinned_classifications_are_in_vocabulary(self) -> None:
        """The import-time loop guarantees every pinned value is in vocabulary;
        this pins the invariant against silent regressions of the loop itself."""
        assert CALLER_SCOPE_REQUIREMENTS, "the pinned caller-scope map must not be empty"
        for tool, classification in CALLER_SCOPE_REQUIREMENTS.items():
            assert classification in VALID_CALLER_SCOPE_CLASSIFICATIONS, (
                f"caller-scope pin '{tool}' = '{classification}' is out of vocabulary"
            )

    def test_classify_caller_scope_raises_on_misspelled_classification(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self._patch_misspelled_classification(monkeypatch)
        with pytest.raises(MCPConfigurationError, match="orgonly"):
            classify_caller_scope("create_pipeline", TOOL_SCOPE_REQUIREMENTS["create_pipeline"])

    def test_resolver_propagates_configuration_error_on_misspelled_classification(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """``resolve_tool_access`` must RAISE, not fall through to 'any' (a
        misspelled classification silently widening access is the failure mode
        this fail-fast exists to prevent)."""
        self._patch_misspelled_classification(monkeypatch)
        with pytest.raises(MCPConfigurationError, match="orgonly"):
            resolve_tool_access(
                tool="create_pipeline",
                action=None,
                role="operator",
                key_scope="org",
                auth_type="api_key",
                allowed_tools=None,
                kill_switch=True,
            )


class TestCheckToolScopeDelegation:
    """``check_tool_scope`` stays the single entry point and delegates the
    decision to the pure resolver — the seam test patches
    ``resolve_tool_access`` and asserts handlers reach it transitively."""

    def test_seam_check_tool_scope_calls_resolver(self, monkeypatch: pytest.MonkeyPatch) -> None:
        import modulo.core.mcp.scope_validator as sv

        calls: list[dict[str, object]] = []

        def _fake_resolve(**kwargs: object) -> tuple[bool, str]:
            calls.append(kwargs)
            return True, "run.trigger"

        monkeypatch.setattr(sv, "resolve_tool_access", _fake_resolve)
        sv.check_tool_scope("runner", "trigger_pipeline", key_scope="org", auth_type="api_key")
        assert len(calls) == 1
        call = calls[0]
        assert call["tool"] == "trigger_pipeline"
        assert call["role"] == "runner"
        assert call["key_scope"] == "org"
        assert call["auth_type"] == "api_key"

    @pytest.mark.asyncio
    async def test_seam_handler_reaches_resolver_transitively(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """The production wiring: an MCP tool handler → _check_agent_tool_scope
        → check_tool_scope → resolve_tool_access, with key_scope/auth_type
        threaded from the request ContextVars."""
        import modulo.api.mcp_server as ms
        import modulo.core.mcp.scope_validator as sv

        calls: list[dict[str, object]] = []

        def _fake_resolve(**kwargs: object) -> tuple[bool, str]:
            calls.append(kwargs)
            return True, "hitl.list"

        monkeypatch.setattr(sv, "resolve_tool_access", _fake_resolve)
        token_role = ms._ctx_role.set("runner")
        token_org = ms._ctx_org_id.set(uuid.UUID(_FAKE_ID))
        token_scope = ms._ctx_key_scope.set("org")
        token_type = ms._ctx_auth_type.set("api_key")
        try:
            with (
                patch("modulo.api.mcp_server.validate_current_auth", return_value=True),
                patch("modulo.api.mcp_server._session"),
                patch(
                    "modulo.api.mcp_server._load_pending_hitl_gates",
                    new=AsyncMock(return_value=([], 0)),
                ),
            ):
                result = await ms.list_pending_hitl()
        finally:
            ms._ctx_role.reset(token_role)
            ms._ctx_org_id.reset(token_org)
            ms._ctx_key_scope.reset(token_scope)
            ms._ctx_auth_type.reset(token_type)
        assert "error" not in result
        assert len(calls) == 1
        assert calls[0]["tool"] == "list_pending_hitl"
        assert calls[0]["key_scope"] == "org"
        assert calls[0]["auth_type"] == "api_key"

    def test_user_key_denied_create_api_key_via_chokepoint(self) -> None:
        """A user-scoped KEY calling the org-only ``create_api_key`` tool is
        denied at the chokepoint with the pinned caller-scope message."""
        with pytest.raises(MCPAuthorizationError) as excinfo:
            check_tool_scope(
                "admin",
                "create_api_key",
                key_scope="user",
                auth_type="api_key",
            )
        assert "org-scoped" in str(excinfo.value)
        assert "user-scoped API key" in str(excinfo.value)

    def test_caller_scoped_deny_message_via_chokepoint(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _patch_caller_scoped_tool(monkeypatch)
        with pytest.raises(MCPAuthorizationError) as excinfo:
            check_tool_scope("admin", _CALLER_SCOPED_TEST_TOOL, key_scope="org", auth_type="api_key")
        assert "caller-scoped" in str(excinfo.value)
        assert "user-scoped" in str(excinfo.value)
        # None key scope fails closed with the 'unset' display.
        with pytest.raises(MCPAuthorizationError) as excinfo:
            check_tool_scope("admin", _CALLER_SCOPED_TEST_TOOL, key_scope=None, auth_type=None)
        assert "unset" in str(excinfo.value)

    def test_legacy_messages_preserved_through_delegation(self) -> None:
        """The pre-existing per-leg denial messages are byte-identical after
        the delegation rewrite (resolution, narrowing, role)."""
        with pytest.raises(MCPAuthorizationError, match="Unknown action 'bogus' for tool 'trigger_pipeline'"):
            check_tool_scope("admin", "trigger_pipeline", action="bogus")
        with pytest.raises(MCPAuthorizationError, match=r"Tool 'unknown_tool' is not registered in the scope policy"):
            check_tool_scope("admin", "unknown_tool")
        with pytest.raises(
            MCPAuthorizationError, match=r"Tool 'create_pipeline' is outside the node's allowed_tools scope"
        ):
            check_tool_scope("admin", "create_pipeline", allowed_tools=["create_agent"])
        with pytest.raises(MCPAuthorizationError, match="Insufficient scope for 'MCP tool") as excinfo:
            check_tool_scope("viewer", "create_pipeline")
        assert "requires 'operator' role, got 'viewer'" in str(excinfo.value)


class TestCallerScopeClassification:
    """The 3-value classification (pure) + its structural invariants."""

    def test_explicit_org_only_pin_create_api_key(self) -> None:
        assert CALLER_SCOPE_REQUIREMENTS["create_api_key"] == "org-only"

    def test_self_suffix_derives_caller_scoped(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _patch_caller_scoped_tool(monkeypatch)
        from modulo.core.mcp.scope_validator import TOOL_SCOPE_REQUIREMENTS

        assert classify_caller_scope(_CALLER_SCOPED_TEST_TOOL, TOOL_SCOPE_REQUIREMENTS[_CALLER_SCOPED_TEST_TOOL]) == (
            "caller-scoped"
        )

    def test_unmapped_mutating_is_org_only(self) -> None:
        for tool in ("create_pipeline", "delete_connector", "perform_housekeeping"):
            assert classify_caller_scope(tool, TOOL_SCOPE_REQUIREMENTS[tool]) == "org-only"

    def test_read_only_is_any(self) -> None:
        for tool in ("list_pipelines", "get_run_status", "search_library"):
            assert classify_caller_scope(tool, "resource.read_only") == "any"
