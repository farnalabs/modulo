"""FAR-1502: every MCP tool failure carries a specific, branchable error code.

Pins the shared ``_tool_exception_error`` classifier — the ladder every
generic ``except Exception`` arm routes through — branch by branch, and proves
end-to-end that a real tool's generic arm classifies instead of answering the
disallowed generic ``internal_error``.

Unit tier: no DB, no Docker — direct classifier calls plus mocked tool bodies.
"""

from unittest.mock import AsyncMock, patch

from sqlalchemy.exc import (
    IntegrityError,
    InvalidRequestError,
    MissingGreenlet,
    OperationalError,
    PendingRollbackError,
    ProgrammingError,
    SQLAlchemyError,
)
from starlette.exceptions import HTTPException as StarletteHTTPException

import modulo.api.mcp_server as ms
from modulo.api.db_error_handling import MSG_SESSION_CONTRACT
from modulo.api.mcp_server import _tool_exception_error, search_documentation
from modulo.core.mcp.scope_validator import MCPAuthorizationError

_MSG = "Failed to do the thing"
_LOG_KEY = "mcp.test_tool"


class TestToolExceptionErrorCodes:
    """Branch-by-branch pins for the shared exception -> specific-code ladder."""

    def test_mcp_authorization_error_is_insufficient_scope(self) -> None:
        result = _tool_exception_error(_MSG, MCPAuthorizationError("denied"), _LOG_KEY)
        assert result == {"error": "insufficient_scope", "detail": "denied"}

    def test_client_http_error_is_validation_failed(self) -> None:
        """4xx keeps the surface's existing HTTPException rendering (parity with
        the ``handle_http_exception`` arm), with the operation context as detail."""
        result = _tool_exception_error(_MSG, StarletteHTTPException(status_code=404, detail="missing"), _LOG_KEY)
        assert result["error"] == "validation_failed"
        assert result["detail"] == _MSG

    def test_server_http_error_is_server_error(self) -> None:
        result = _tool_exception_error(_MSG, StarletteHTTPException(status_code=503, detail="upstream"), _LOG_KEY)
        assert result["error"] == "server_error"
        assert result["detail"] == _MSG

    def test_invalid_request_error_is_session_contract_error(self) -> None:
        result = _tool_exception_error(_MSG, InvalidRequestError("Autobegin is disabled on this Session"), _LOG_KEY)
        assert result["error"] == "session_contract_error"
        assert result["detail"] == MSG_SESSION_CONTRACT

    def test_missing_greenlet_is_session_contract_error(self) -> None:
        result = _tool_exception_error(_MSG, MissingGreenlet(), _LOG_KEY)
        assert result["error"] == "session_contract_error"
        assert result["detail"] == MSG_SESSION_CONTRACT

    def test_pending_rollback_is_database_unavailable(self) -> None:
        """PendingRollbackError IS an InvalidRequestError but signals a transient fault."""
        result = _tool_exception_error(_MSG, PendingRollbackError("session is poisoned"), _LOG_KEY)
        assert result["error"] == "database_unavailable"
        assert result["detail"] == _MSG

    def test_integrity_error_is_conflict(self) -> None:
        result = _tool_exception_error(_MSG, IntegrityError("stmt", {}, Exception("unique")), _LOG_KEY)
        assert result["error"] == "conflict"
        assert result["detail"] == _MSG

    def test_programming_error_is_migration_required(self) -> None:
        result = _tool_exception_error(_MSG, ProgrammingError("stmt", {}, Exception("missing")), _LOG_KEY)
        assert result["error"] == "migration_required"
        assert result["detail"] == _MSG

    def test_operational_error_is_database_unavailable(self) -> None:
        result = _tool_exception_error(_MSG, OperationalError("down", {}, Exception("conn")), _LOG_KEY)
        assert result["error"] == "database_unavailable"
        assert result["detail"] == _MSG

    def test_generic_sqlalchemy_error_is_database_unavailable(self) -> None:
        result = _tool_exception_error(_MSG, SQLAlchemyError("down"), _LOG_KEY)
        assert result["error"] == "database_unavailable"
        assert result["detail"] == _MSG

    def test_unexpected_exception_is_server_error(self) -> None:
        """``server_error`` is the RESERVED catch-all — the only generic code left."""
        result = _tool_exception_error(_MSG, RuntimeError("boom"), _LOG_KEY)
        assert result == {"error": "server_error", "detail": _MSG}


class TestGenericArmClassifiesEndToEnd:
    """A real tool's generic ``except Exception`` arm classifies the exception.

    ``search_documentation`` peels nothing but auth, so every failure mode
    below reaches the shared classifier exactly as a production call would.
    """

    async def test_session_contract_error_on_generic_arm(self) -> None:
        with (
            patch.object(ms, "validate_current_auth", new=AsyncMock(return_value=True)),
            patch.object(
                ms,
                "_get_doc_index",
                side_effect=InvalidRequestError("Autobegin is disabled on this Session"),
            ),
        ):
            result = await search_documentation(query="pipelines")
        assert result["error"] == "session_contract_error"
        assert result["detail"] == MSG_SESSION_CONTRACT

    async def test_database_unavailable_on_generic_arm(self) -> None:
        with (
            patch.object(ms, "validate_current_auth", new=AsyncMock(return_value=True)),
            patch.object(ms, "_get_doc_index", side_effect=OperationalError("down", {}, Exception("conn"))),
        ):
            result = await search_documentation(query="pipelines")
        assert result["error"] == "database_unavailable"

    async def test_server_error_on_generic_arm(self) -> None:
        with (
            patch.object(ms, "validate_current_auth", new=AsyncMock(return_value=True)),
            patch.object(ms, "_get_doc_index", side_effect=RuntimeError("boom")),
        ):
            result = await search_documentation(query="pipelines")
        assert result["error"] == "server_error"
        assert result["detail"] == "Failed to search documentation"
