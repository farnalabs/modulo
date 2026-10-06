"""FAR-1482: an ``InvalidRequestError`` on an MCP tool path is an internal error.

REST classifies a client-side session-contract violation with the shared
``raise_session_contract_error`` guard (FAR-1464) and answers ``500
MSG_SESSION_CONTRACT``. MCP tool results carry no status code — the payload IS
the response — so the same verdict has to arrive as this surface's
``internal_error`` payload. One classifier, two renderings.

Every ``internal_error`` assertion in this file FAILED before the fix: a
session-contract violation was reported to the MCP caller as ``database_unavailable``
/ ``Database temporarily unavailable`` — a retry-inviting outage reply for a
non-retryable server bug. The transient controls assert the other half of Done
means #2: a genuine ``SQLAlchemyError`` / ``PendingRollbackError`` must keep its
old behaviour.

Unit tier: no DB, no Docker — auth, the session factory and the CRUD layer are
all mocked (``tests/unit/mcp/`` has no settings-providing conftest, so the real
``_session`` factory must not be reached).
"""

import contextlib
import logging
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy.exc import InvalidRequestError, MissingGreenlet, PendingRollbackError, SQLAlchemyError

import modulo.api.mcp_server as ms
from modulo.api.db_error_handling import MSG_SESSION_CONTRACT
from modulo.api.mcp_server import _tool_db_shell, create_schema, list_api_keys
from tests.unit.mcp.helpers import USER_ID, AuthContext, make_session_context

#: The exception message a session-contract violation is built from.
_SESSION_CONTRACT_MSG = "Autobegin is disabled on this Session"


class _McpContext(AuthContext):
    """AuthContext plus the contextvars ``create_schema`` / ``list_api_keys`` read.

    ``AuthContext`` covers org/role/user/token/auth_type; the two tools under
    test also reach ``_ctx_key_id`` / ``_ctx_team_id`` / ``_ctx_node_allowed_tools``
    through the shared scope check, so set and clear those too.
    """

    def setup_method(self) -> None:
        super().setup_method()
        ms._ctx_key_id.set(USER_ID)
        ms._ctx_team_id.set(None)
        ms._ctx_node_allowed_tools.set(None)

    def teardown_method(self) -> None:
        ms._ctx_key_id.set(None)
        ms._ctx_team_id.set(None)
        ms._ctx_node_allowed_tools.set(None)
        super().teardown_method()


@contextlib.contextmanager
def _tool_env(**crud: Any):
    """Auth-valid + mocked session factory; each kwarg patches one CRUD callable."""
    with contextlib.ExitStack() as stack:
        stack.enter_context(patch.object(ms, "validate_current_auth", new=AsyncMock(return_value=True)))
        session_factory = stack.enter_context(patch.object(ms, "_session"))
        session_factory.return_value = make_session_context(AsyncMock())
        for name, side_effect in crud.items():
            stack.enter_context(patch.object(ms, name, new=AsyncMock(side_effect=side_effect)))
        yield


def _assert_internal_session_contract(result: dict[str, Any]) -> None:
    """The programming-error payload: ``internal_error`` + the SHARED REST message.

    Equality with ``MSG_SESSION_CONTRACT`` is what rules out the old reply
    (``database_unavailable`` / ``Database temporarily unavailable``) — a
    retry-inviting outage text for a bug that will fail identically until the
    code is fixed.
    """
    assert result["error"] == "internal_error", result
    assert result["detail"] == MSG_SESSION_CONTRACT, result


class TestToolDbShellSessionContract:
    """The shared ``_tool_db_shell`` exception ladder (eval-defs + get_trigger).

    A bare decorator over a local handler — no auth/contextvars involved.
    """

    async def test_invalid_request_error_is_internal_error(self) -> None:
        @_tool_db_shell(log_constant="test", integrity_detail=None, fallback="fail")
        async def handler() -> dict[str, Any]:
            raise InvalidRequestError(_SESSION_CONTRACT_MSG)

        result = await handler()
        _assert_internal_session_contract(result)

    async def test_missing_greenlet_is_internal_error(self) -> None:
        @_tool_db_shell(log_constant="test", integrity_detail=None, fallback="fail")
        async def handler() -> dict[str, Any]:
            raise MissingGreenlet

        result = await handler()
        _assert_internal_session_contract(result)

    async def test_invalid_request_error_beats_generic_fallback(self) -> None:
        """``db_errors_to_fallback`` must not swallow the shared classification."""

        @_tool_db_shell(
            log_constant="test",
            integrity_detail=None,
            fallback="Failed to get trigger",
            db_errors_to_fallback=True,
        )
        async def handler() -> dict[str, Any]:
            raise InvalidRequestError(_SESSION_CONTRACT_MSG)

        result = await handler()
        _assert_internal_session_contract(result)
        assert "Failed to get trigger" not in result["detail"], result

    async def test_transient_sqlalchemy_error_stays_database_unavailable(self) -> None:
        @_tool_db_shell(log_constant="test", integrity_detail=None, fallback="fail")
        async def handler() -> dict[str, Any]:
            raise SQLAlchemyError("down")

        result = await handler()
        assert result["error"] == "database_unavailable", result

    async def test_pending_rollback_stays_database_unavailable(self) -> None:
        """PendingRollbackError IS an InvalidRequestError but is transient."""

        @_tool_db_shell(log_constant="test", integrity_detail=None, fallback="fail")
        async def handler() -> dict[str, Any]:
            raise PendingRollbackError("session is poisoned")

        result = await handler()
        assert result["error"] == "database_unavailable", result


class TestToolPayloadSessionContract(_McpContext):
    """Hand-rolled per-tool ``except SQLAlchemyError`` ladders."""

    async def test_create_schema_reports_internal_error(self) -> None:
        with _tool_env(db_create_schema=InvalidRequestError(_SESSION_CONTRACT_MSG)):
            result = await create_schema(name="s")
        _assert_internal_session_contract(result)

    async def test_create_schema_transient_stays_database_unavailable(self) -> None:
        with _tool_env(db_create_schema=SQLAlchemyError("down")):
            result = await create_schema(name="s")
        assert result["error"] == "database_unavailable", result

    async def test_create_schema_pending_rollback_stays_database_unavailable(self) -> None:
        with _tool_env(db_create_schema=PendingRollbackError("poisoned")):
            result = await create_schema(name="s")
        assert result["error"] == "database_unavailable", result

    async def test_list_api_keys_reports_internal_error(self) -> None:
        """Covers the ``_tool_error(_MSG_DB_TEMPORARILY_UNAVAILABLE)`` arm shape."""
        with _tool_env(auth_list_api_keys=InvalidRequestError(_SESSION_CONTRACT_MSG)):
            result = await list_api_keys()
        _assert_internal_session_contract(result)

    async def test_list_api_keys_transient_keeps_old_detail(self) -> None:
        with _tool_env(auth_list_api_keys=SQLAlchemyError("down")):
            result = await list_api_keys()
        assert result["error"] == "internal_error", result
        assert result["detail"] == ms._MSG_DB_TEMPORARILY_UNAVAILABLE, result

    async def test_shared_classifier_writes_its_own_log_record(self, caplog: pytest.LogCaptureFixture) -> None:
        """Proof of reuse: the log key comes from db_error_handling, not a fork."""
        with (
            caplog.at_level(logging.ERROR, logger="modulo.api.db_error_handling"),
            _tool_env(db_create_schema=InvalidRequestError(_SESSION_CONTRACT_MSG)),
        ):
            await create_schema(name="s")

        assert "mcp.create_schema.session_contract_error" in caplog.text, caplog.text
