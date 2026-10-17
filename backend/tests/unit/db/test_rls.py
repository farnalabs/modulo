"""Unit tests for db/rls.py — set_rls_org, set_rls_user_context, register_rls_reset_hook."""

import asyncio
import logging
import uuid
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from sqlalchemy.exc import UnboundExecutionError
from sqlalchemy.ext.asyncio import AsyncSession

from modulo.db.rls import (
    _RLS_CONFIG_NAMES,
    _read_guc_org,
    _set_config_statement,
    read_rls_org,
    register_rls_reset_hook,
    set_rls_org,
    set_rls_user_context,
)

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


class TestSetConfigStatement:
    """FAR-915 / GitHub #126 hygiene: the checkout hook interpolates the GUC
    name into the DBAPI2 statement (no portable parameter binding), so the
    builder must fail closed on any name outside the known constant."""

    def test_known_names_round_trip(self) -> None:
        for config_name in _RLS_CONFIG_NAMES:
            assert _set_config_statement(config_name) == f"SELECT set_config('{config_name}', '', false)"

    def test_rejects_name_outside_known_set(self) -> None:
        with pytest.raises(ValueError, match="Unknown RLS config name"):
            _set_config_statement("app.execution_context') ; SELECT 1 --")

    def test_checkout_hook_uses_known_config_names(self) -> None:
        """The listener body must interpolate only validated member names."""
        engine = MagicMock()
        engine.dialect.name = "postgresql"
        engine.sync_engine = MagicMock()
        listener = _capture_checkout_listener(engine)

        cursor = MagicMock()
        connection = MagicMock()
        connection.cursor.return_value = cursor
        listener(connection, MagicMock(), MagicMock())

        assert cursor.execute.call_count == len(_RLS_CONFIG_NAMES)
        for call in cursor.execute.call_args_list:
            sql = call[0][0]
            assert sql.startswith("SELECT set_config('")
            assert sql.endswith("', '', false)")


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


class TestReadRlsOrgDialectOrder:
    """FAR-1884: the session's dialect selects the authoritative RLS source.

    On Postgres the ``current_setting`` GUC is authoritative — ``set_rls_org``
    never writes ``session.info`` there — so a populated ``session.info['org_id']``
    must NOT shadow it (the pre-FAR-1884 order consulted ``session.info`` first).
    """

    async def test_postgres_ignores_populated_session_info(self) -> None:
        """Prove-the-fix: the GUC wins over a populated ``session.info`` value."""
        guc_org = uuid.UUID("00000000-0000-0000-0000-0000000000ff")
        session = _make_session(dialect="postgresql")
        session.info = {"org_id": str(_ORG_ID)}
        result = MagicMock()
        result.scalar.return_value = str(guc_org)
        session.execute = AsyncMock(return_value=result)

        assert await read_rls_org(session) == guc_org

    async def test_postgres_empty_guc_does_not_fall_back_to_info(self) -> None:
        """A populated ``session.info`` must not leak when the GUC is unset."""
        session = _make_session(dialect="postgresql")
        session.info = {"org_id": str(_ORG_ID)}
        result = MagicMock()
        result.scalar.return_value = ""
        session.execute = AsyncMock(return_value=result)

        assert await read_rls_org(session) is None

    async def test_generic_backend_reads_session_info(self) -> None:
        """Generic backends (no GUC) keep the ``session.info`` path."""
        session = _make_session(dialect="sqlite")
        session.info = {"org_id": str(_ORG_ID)}

        assert await read_rls_org(session) == _ORG_ID

    async def test_no_bind_session_double_keeps_info_first_order(self) -> None:
        """Finding 4(a): a session double exposing only ``execute``/``info`` — and
        no readable dialect (no ``get_bind`` at all) — resolves via
        ``session.info`` exactly as it did before the dialect split.

        Non-vacuous: the info org and the GUC probe value are DIFFERENT UUIDs, so
        a regression that treated this shape as Postgres (GUC-first) or dropped
        the generic fallback would return the GUC value and fail here. ``execute``
        is never reached because the info lookup short-circuits.
        """
        guc_org = uuid.UUID("00000000-0000-0000-0000-0000000000ee")
        session = _NoBindSessionDouble(info={"org_id": str(_ORG_ID)}, guc_value=str(guc_org))

        result = await read_rls_org(session)

        assert result == _ORG_ID
        assert result != guc_org
        assert session.execute_count == 0

    async def test_unbound_session_keeps_info_first_order(self) -> None:
        """The narrowed catch still keeps the generic path for an unbound session.

        ``AsyncSession.get_bind()`` raises ``UnboundExecutionError`` when nothing
        is bound; that must NOT be mistaken for an unreadable dialect (which
        fails closed). The differing-UUID assertions make the distinction real.
        """
        guc_org = uuid.UUID("00000000-0000-0000-0000-0000000000ee")
        session = _UnboundSessionDouble(info={"org_id": str(_ORG_ID)}, guc_value=str(guc_org))

        result = await read_rls_org(session)

        assert result == _ORG_ID
        assert result != guc_org

    async def test_unreadable_dialect_fails_closed_to_guc(self, caplog: pytest.LogCaptureFixture) -> None:
        """Finding 1: a bind that EXISTS but exposes no readable dialect must NOT
        silently fall back to the info-first order — it fails closed to the GUC
        probe (and logs loudly).

        Non-vacuous: ``session.info`` holds ``_ORG_ID`` while the GUC probe
        returns a DIFFERENT org, so the silent-degradation regression (treating
        an unreadable bind as a generic backend) would resolve via info and fail.
        """
        guc_org = uuid.UUID("00000000-0000-0000-0000-0000000000ee")
        session = _UnreadableDialectSessionDouble(info={"org_id": str(_ORG_ID)}, guc_value=str(guc_org))

        with caplog.at_level(logging.WARNING, logger="modulo.db.rls"):
            result = await read_rls_org(session)

        assert result == guc_org
        assert result != _ORG_ID
        assert any("no readable dialect" in message for message in caplog.messages)


class _NoBindSessionDouble:
    """A session double exposing only ``execute``/``info`` — NO ``get_bind``.

    Mirrors the many lightweight unit fakes that satisfy ``read_rls_org`` without
    a real ``AsyncSession`` bind (a real unbound session raises
    ``UnboundExecutionError``). ``info['org_id']`` is the pre-FAR-1884 result and
    must keep winning on this shape.
    """

    def __init__(self, *, info: dict[str, object] | None = None, guc_value: str = "") -> None:
        self.info = dict(info or {})
        self._guc_value = guc_value
        self.execute_count = 0

    async def execute(self, _stmt: object, _params: object = None) -> MagicMock:
        self.execute_count += 1
        result = MagicMock()
        result.scalar.return_value = self._guc_value
        return result


class _UnboundSessionDouble(_NoBindSessionDouble):
    """Same no-bind shape, but ``get_bind`` raises ``UnboundExecutionError``."""

    def get_bind(self) -> object:
        raise UnboundExecutionError("This session is not bound to a single Engine or Connection.")


class _UnreadableDialectSessionDouble(_NoBindSessionDouble):
    """A bind EXISTS but exposes no ``dialect`` — its name is unreadable."""

    def get_bind(self) -> object:
        return SimpleNamespace()


class TestReadGucOrgDegradation:
    """Finding 2: ``_read_guc_org`` degrades BOTH the execute AND the fetch to None."""

    async def test_execute_failure_degrades_to_none(self) -> None:
        session = AsyncMock()
        session.execute = AsyncMock(side_effect=RuntimeError("connection closed"))

        assert await _read_guc_org(session) is None

    async def test_scalar_fetch_failure_degrades_to_none(self) -> None:
        """Prove-the-fix: a fetch-time failure (``result.scalar()``) is now INSIDE
        the guarded region.

        Non-vacuous: the pre-fix structure left ``result.scalar()`` outside the
        ``try``, so this exact input raised ``RuntimeError`` out of
        ``_read_guc_org`` instead of returning ``None``.
        """
        session = AsyncMock()
        result = MagicMock()
        result.scalar.side_effect = RuntimeError("server-side cursor closed mid-fetch")
        session.execute = AsyncMock(return_value=result)

        assert await _read_guc_org(session) is None
