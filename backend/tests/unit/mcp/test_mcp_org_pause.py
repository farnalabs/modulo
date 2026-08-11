"""Unit tests for the set_org_triggers_paused MCP tool (org-wide trigger pause).

Covers the pause kill-switch exposed to MCP: pausing, resuming, idempotent
no-op, org-not-found, migration-required, and the fail-open audit path.
"""

from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from sqlalchemy.exc import ProgrammingError, SQLAlchemyError

from modulo.api.mcp_server import set_org_triggers_paused
from tests.unit.mcp.helpers import AuthContext, make_session_context


def _make_org(*, paused: bool, paused_at) -> MagicMock:
    org = MagicMock()
    org.triggers_paused = paused
    org.triggers_paused_at = paused_at
    return org


class TestSetOrgTriggersPausedScope:
    def test_scope_requires_admin(self) -> None:
        from modulo.core.mcp.scope_validator import MCPAuthorizationError, check_tool_scope

        assert check_tool_scope("admin", "set_org_triggers_paused") is None
        with pytest.raises(MCPAuthorizationError):
            check_tool_scope("operator", "set_org_triggers_paused")


class TestSetOrgTriggersPausedErrors(AuthContext):
    @patch("modulo.api.mcp_server.validate_current_auth", return_value=False)
    async def test_returns_auth_error_on_revoked_token(self, mock_validate_auth: AsyncMock) -> None:
        result = await set_org_triggers_paused(paused=True)
        assert result["error"] == "auth_expired"

    @patch("modulo.api.mcp_server.validate_current_auth", return_value=True)
    @patch("modulo.api.mcp_server._session")
    async def test_insufficient_scope(
        self,
        mock_session: AsyncMock,
        mock_validate_auth: AsyncMock,
    ) -> None:
        from modulo.core.mcp.scope_validator import MCPAuthorizationError

        mock_session.return_value = make_session_context(AsyncMock())
        with patch(
            "modulo.api.mcp_server.check_tool_scope",
            side_effect=MCPAuthorizationError("Insufficient scope"),
        ):
            result = await set_org_triggers_paused(paused=True)
        assert result["error"] == "insufficient_scope"

    @patch("modulo.api.mcp_server.validate_current_auth", return_value=True)
    @patch("modulo.api.mcp_server._session")
    @patch("modulo.api.mcp_server.check_tool_scope")
    @patch("modulo.db.crud.organisation.get_organisation")
    async def test_migration_required_on_programming_error(
        self,
        mock_get_org: AsyncMock,
        mock_check_scope: MagicMock,
        mock_session: AsyncMock,
        mock_validate_auth: AsyncMock,
    ) -> None:
        mock_get_org.return_value = _make_org(paused=False, paused_at=None)
        mock_sesh = AsyncMock()
        mock_sesh.flush = AsyncMock(side_effect=ProgrammingError("UPDATE", {}, Exception("no column")))
        mock_session.return_value = make_session_context(mock_sesh)

        result = await set_org_triggers_paused(paused=True)

        assert result["error"] == "migration_required"

    @patch("modulo.api.mcp_server.validate_current_auth", return_value=True)
    @patch("modulo.api.mcp_server._session")
    @patch("modulo.api.mcp_server.check_tool_scope")
    async def test_returns_not_found_when_org_missing(
        self,
        mock_check_scope: MagicMock,
        mock_session: AsyncMock,
        mock_validate_auth: AsyncMock,
    ) -> None:
        mock_session.return_value = make_session_context(AsyncMock())
        with patch("modulo.db.crud.organisation.get_organisation", AsyncMock(return_value=None)):
            result = await set_org_triggers_paused(paused=True)
        assert result["error"] == "not_found"


class TestSetOrgTriggersPausedSuccess(AuthContext):
    @patch("modulo.api.mcp_server.validate_current_auth", return_value=True)
    @patch("modulo.api.mcp_server._session")
    @patch("modulo.api.mcp_server.check_tool_scope")
    @patch("modulo.core.audit_logger.append_audit_event")
    @patch("modulo.db.crud.organisation.get_organisation")
    async def test_pause_true_toggles_org_and_appends_audit(
        self,
        mock_get_org: AsyncMock,
        mock_append_audit: AsyncMock,
        mock_check_scope: MagicMock,
        mock_session: AsyncMock,
        mock_validate_auth: AsyncMock,
    ) -> None:
        org = _make_org(paused=False, paused_at=None)
        mock_get_org.return_value = org
        mock_sesh = AsyncMock()
        mock_session.return_value = make_session_context(mock_sesh)

        result = await set_org_triggers_paused(paused=True)

        assert result["paused"] is True
        assert isinstance(result["paused_at"], str)
        assert org.triggers_paused is True
        assert org.triggers_paused_at is not None
        mock_append_audit.assert_awaited_once()
        call_kwargs = mock_append_audit.await_args.kwargs
        assert call_kwargs["event_type"] == "triggers_paused"
        assert call_kwargs["payload_json"] == {"paused": True}
        mock_sesh.flush.assert_awaited_once()

    @patch("modulo.api.mcp_server.validate_current_auth", return_value=True)
    @patch("modulo.api.mcp_server._session")
    @patch("modulo.api.mcp_server.check_tool_scope")
    @patch("modulo.core.audit_logger.append_audit_event")
    @patch("modulo.db.crud.organisation.get_organisation")
    async def test_resume_false_clears_paused_at(
        self,
        mock_get_org: AsyncMock,
        mock_append_audit: AsyncMock,
        mock_check_scope: MagicMock,
        mock_session: AsyncMock,
        mock_validate_auth: AsyncMock,
    ) -> None:
        org = _make_org(paused=True, paused_at=datetime(2026, 1, 1, 12, 0, tzinfo=UTC))
        mock_get_org.return_value = org
        mock_session.return_value = make_session_context(AsyncMock())

        result = await set_org_triggers_paused(paused=False)

        assert result["paused"] is False
        assert result["paused_at"] is None
        assert org.triggers_paused is False
        assert org.triggers_paused_at is None
        mock_append_audit.assert_awaited_once()
        assert mock_append_audit.await_args.kwargs["payload_json"] == {"paused": False}

    @patch("modulo.api.mcp_server.validate_current_auth", return_value=True)
    @patch("modulo.api.mcp_server._session")
    @patch("modulo.api.mcp_server.check_tool_scope")
    @patch("modulo.core.audit_logger.append_audit_event")
    @patch("modulo.db.crud.organisation.get_organisation")
    async def test_idempotent_same_state_returns_current_without_audit(
        self,
        mock_get_org: AsyncMock,
        mock_append_audit: AsyncMock,
        mock_check_scope: MagicMock,
        mock_session: AsyncMock,
        mock_validate_auth: AsyncMock,
    ) -> None:
        paused_at = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)
        org = _make_org(paused=True, paused_at=paused_at)
        mock_get_org.return_value = org
        mock_sesh = AsyncMock()
        mock_session.return_value = make_session_context(mock_sesh)

        result = await set_org_triggers_paused(paused=True)

        assert result == {"paused": True, "paused_at": paused_at.isoformat()}
        mock_append_audit.assert_not_awaited()
        mock_sesh.flush.assert_not_awaited()

    @patch("modulo.api.mcp_server.validate_current_auth", return_value=True)
    @patch("modulo.api.mcp_server._session")
    @patch("modulo.api.mcp_server.check_tool_scope")
    @patch("modulo.core.audit_logger.append_audit_event")
    @patch("modulo.db.crud.organisation.get_organisation")
    async def test_audit_failure_is_fail_open_toggle_commits(
        self,
        mock_get_org: AsyncMock,
        mock_append_audit: AsyncMock,
        mock_check_scope: MagicMock,
        mock_session: AsyncMock,
        mock_validate_auth: AsyncMock,
    ) -> None:
        org = _make_org(paused=False, paused_at=None)
        mock_get_org.return_value = org
        mock_append_audit.side_effect = SQLAlchemyError("audit write failed")
        mock_session.return_value = make_session_context(AsyncMock())

        result = await set_org_triggers_paused(paused=True)

        assert result["paused"] is True
        assert result["paused_at"] is not None
        assert "error" not in result
        assert org.triggers_paused is True
