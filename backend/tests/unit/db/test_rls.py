"""Unit tests for db/rls.py — set_rls_org, set_rls_user_context, register_rls_reset_hook."""

import asyncio
import logging
import uuid
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from modulo.db.rls import register_rls_reset_hook, set_rls_org, set_rls_user_context

_ORG_ID = uuid.UUID("00000000-0000-0000-0000-000000000001")
_USER_ID = uuid.UUID("00000000-0000-0000-0000-000000000002")
_ORG_ROLE = "admin"


def _make_session(*, in_tx: bool = True, dialect: str = "postgresql") -> AsyncMock:
    session = AsyncMock(spec=AsyncSession)
    session.in_transaction.return_value = in_tx
    session.execute = AsyncMock()

    bind = MagicMock()
    bind.dialect.name = dialect

    async def _get_bind() -> MagicMock:
        return bind

    session.get_bind = _get_bind
    return session


class TestSetRlsOrg:
    async def test_executes_set_config_with_correct_params(self) -> None:
        session = _make_session()

        await set_rls_org(session, _ORG_ID)

        session.execute.assert_awaited_once()
        call_args = session.execute.await_args
        assert call_args is not None
        compiled = call_args[0][0].compile()
        assert "set_config" in str(compiled)
        assert call_args[0][1]["oid"] == str(_ORG_ID)

    async def test_raises_without_active_transaction(self) -> None:
        session = _make_session(in_tx=False)

        with pytest.raises(RuntimeError, match="requires an active transaction"):
            await set_rls_org(session, _ORG_ID)

    async def test_none_org_id_returns_early_without_transaction(self) -> None:
        """System-admin no-org path must be a no-op even outside a transaction."""
        session = _make_session(in_tx=False)

        await set_rls_org(session, None)

        session.execute.assert_not_called()


class TestSetRlsUserContext:
    async def test_sets_user_id_and_org_role(self) -> None:
        session = _make_session()

        await set_rls_user_context(session, _USER_ID, _ORG_ROLE)

        assert session.execute.await_count == 2

        first_call = session.execute.await_args_list[0]
        compiled_1 = first_call[0][0].compile()
        assert "set_config" in str(compiled_1)
        assert "app.user_id" in str(compiled_1)
        assert first_call[0][1]["uid"] == str(_USER_ID)

        second_call = session.execute.await_args_list[1]
        compiled_2 = second_call[0][0].compile()
        assert "set_config" in str(compiled_2)
        assert "app.org_role" in str(compiled_2)
        assert second_call[0][1]["role"] == _ORG_ROLE

    async def test_raises_without_active_transaction(self) -> None:
        session = _make_session(in_tx=False)

        with pytest.raises(RuntimeError, match="requires an active transaction"):
            await set_rls_user_context(session, _USER_ID, _ORG_ROLE)


class TestRegisterRlsResetHook:
    def test_registers_checkout_listener(self) -> None:
        engine = MagicMock()
        engine.dialect.name = "postgresql"
        engine.sync_engine = MagicMock()

        with patch("modulo.db.rls.event.listens_for") as mock_listens:
            register_rls_reset_hook(engine)

        mock_listens.assert_called_once_with(engine.sync_engine, "checkout")


def _capture_checkout_listener(engine: MagicMock) -> Any:
    """Register the reset hook and return the actual checkout listener function."""

    captured: dict[str, Any] = {}

    def _fake_listens_for(target: object, ident: str) -> Any:
        def _decorator(fn: Any) -> Any:
            captured["fn"] = fn
            return fn

        return _decorator

    with patch("modulo.db.rls.event.listens_for", side_effect=_fake_listens_for):
        register_rls_reset_hook(engine)

    assert "fn" in captured, "register_rls_reset_hook did not register a listener"
    return captured["fn"]


class TestCheckoutResetListener:
    """Exercise the actual _reset_org_on_checkout body for postgres checkouts."""

    def _listener(self) -> Any:
        engine = MagicMock()
        engine.dialect.name = "postgresql"
        engine.sync_engine = MagicMock()
        return _capture_checkout_listener(engine)

    def test_clears_all_gucs_on_checkout(self) -> None:
        cursor = MagicMock()
        connection = MagicMock()
        connection.cursor.return_value = cursor

        self._listener()(connection, MagicMock(), MagicMock())

        assert cursor.execute.call_count == 4
        executed_sql = [str(call) for call in cursor.execute.call_args_list]
        for config_name in (
            "app.organisation_id",
            "app.user_id",
            "app.org_role",
            "app.execution_context",
        ):
            assert any(f"set_config('{config_name}'" in sql for sql in executed_sql)
        cursor.close.assert_called_once()

    def test_logs_warning_when_cursor_api_unavailable(self, caplog: pytest.LogCaptureFixture) -> None:
        connection = object()

        with caplog.at_level(logging.WARNING, logger="modulo.db.rls"):
            self._listener()(connection, MagicMock(), MagicMock())

        assert any("sync cursor API not available" in message for message in caplog.messages)

    def test_re_raises_cancelled_error(self) -> None:
        cursor = MagicMock()
        cursor.execute.side_effect = asyncio.CancelledError()
        connection = MagicMock()
        connection.cursor.return_value = cursor

        with pytest.raises(asyncio.CancelledError):
            self._listener()(connection, MagicMock(), MagicMock())

        cursor.close.assert_called_once()

    def test_logs_warning_when_set_config_fails(self, caplog: pytest.LogCaptureFixture) -> None:
        cursor = MagicMock()
        cursor.execute.side_effect = RuntimeError("connection gone")
        connection = MagicMock()
        connection.cursor.return_value = cursor

        with caplog.at_level(logging.WARNING, logger="modulo.db.rls"):
            self._listener()(connection, MagicMock(), MagicMock())

        assert any("failed to clear RLS session context" in message for message in caplog.messages)
        cursor.close.assert_called_once()
