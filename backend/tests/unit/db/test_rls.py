"""Unit tests for db/rls.py — set_rls_org, set_rls_user_context, register_rls_reset_hook."""

import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from modulo.db.rls import register_rls_reset_hook, set_rls_org, set_rls_user_context

_ORG_ID = uuid.UUID("00000000-0000-0000-0000-000000000001")
_USER_ID = uuid.UUID("00000000-0000-0000-0000-000000000002")
_ORG_ROLE = "admin"


def _make_session(*, in_tx: bool = True) -> AsyncMock:
    session = AsyncMock(spec=AsyncSession)
    session.in_transaction.return_value = in_tx
    session.execute = AsyncMock()
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
        engine.sync_engine = MagicMock()

        with patch("modulo.db.rls.event.listens_for") as mock_listens:
            register_rls_reset_hook(engine)

        mock_listens.assert_called_once_with(engine.sync_engine, "checkout")
