"""Unit tests for the cost subsystem's ``system_config`` KV helpers (spec §4.7).

These helpers are the shared global-table read/write + advisory-lock discipline
used by the probe and the ledger's duplicate-terminal flood recorder. The
probe/ledger tests mock these helpers away, so the helpers themselves get direct
coverage here: the sha256-derived advisory lock keys, the postgres/non-postgres
dialect branches, and the read/upsert/flush behaviour.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

from sqlalchemy.ext.asyncio import AsyncSession

from modulo.core.cost_controller.system_config import (
    _lock_keys,
    acquire_kv_lock,
    read_system_config,
    try_acquire_kv_lock,
    write_system_config,
)


def _make_session(dialect: str = "postgresql") -> AsyncMock:
    session = AsyncMock(spec=AsyncSession)
    bind = MagicMock()
    bind.dialect.name = dialect
    session.get_bind = MagicMock(return_value=bind)
    return session


# ---------------------------------------------------------------------------
# _lock_keys — stable sha256-derived advisory lock keys
# ---------------------------------------------------------------------------


def test_lock_keys_are_deterministic() -> None:
    k1, k2 = _lock_keys("probe_state:org-1")
    assert k1 == _lock_keys("probe_state:org-1")[0]
    assert k2 == _lock_keys("probe_state:org-1")[1]


def test_lock_keys_differ_per_key() -> None:
    assert _lock_keys("probe_state:org-1") != _lock_keys("probe_state:org-2")


def test_lock_keys_produce_signed_int4_pair() -> None:
    key1, key2 = _lock_keys("duplicate_terminal_events")
    assert -(2**31) <= key1 < 2**31
    assert -(2**31) <= key2 < 2**31


# ---------------------------------------------------------------------------
# acquire_kv_lock — advisory xact lock on postgres, no-op elsewhere
# ---------------------------------------------------------------------------


async def test_acquire_kv_lock_issues_pg_advisory_xact_lock() -> None:
    session = _make_session("postgresql")
    key1, key2 = _lock_keys("probe_state:org-1")
    await acquire_kv_lock(session, "probe_state:org-1")
    session.execute.assert_awaited_once()
    call = session.execute.await_args
    assert call is not None
    text_stmt, params = call.args
    assert "pg_advisory_xact_lock" in str(text_stmt)
    assert params == {"k1": key1, "k2": key2}


async def test_acquire_kv_lock_noop_on_sqlite() -> None:
    session = _make_session("sqlite")
    await acquire_kv_lock(session, "probe_state:org-1")
    session.execute.assert_not_awaited()


# ---------------------------------------------------------------------------
# try_acquire_kv_lock — non-blocking variant
# ---------------------------------------------------------------------------


async def test_try_acquire_kv_lock_returns_true_when_acquired() -> None:
    session = _make_session("postgresql")
    result = MagicMock()
    result.scalar_one.return_value = True
    session.execute = AsyncMock(return_value=result)
    assert await try_acquire_kv_lock(session, "probe_state:org-1") is True
    call = session.execute.await_args
    assert call is not None
    assert "pg_try_advisory_xact_lock" in str(call.args[0])


async def test_try_acquire_kv_lock_returns_false_when_held() -> None:
    session = _make_session("postgresql")
    result = MagicMock()
    result.scalar_one.return_value = False
    session.execute = AsyncMock(return_value=result)
    assert await try_acquire_kv_lock(session, "probe_state:org-1") is False


async def test_try_acquire_kv_lock_true_on_non_postgres() -> None:
    session = _make_session("sqlite")
    assert await try_acquire_kv_lock(session, "probe_state:org-1") is True
    session.execute.assert_not_awaited()


# ---------------------------------------------------------------------------
# read_system_config — single value read
# ---------------------------------------------------------------------------


async def test_read_system_config_returns_value() -> None:
    session = _make_session()
    result = MagicMock()
    result.scalar_one_or_none.return_value = {"last_cadence_at": "2026-08-10T00:00:00+00:00"}
    session.execute = AsyncMock(return_value=result)
    assert await read_system_config(session, "probe_state:org-1") == {"last_cadence_at": "2026-08-10T00:00:00+00:00"}


async def test_read_system_config_returns_none_when_absent() -> None:
    session = _make_session()
    result = MagicMock()
    result.scalar_one_or_none.return_value = None
    session.execute = AsyncMock(return_value=result)
    assert await read_system_config(session, "missing-key") is None


# ---------------------------------------------------------------------------
# write_system_config — upsert + flush
# ---------------------------------------------------------------------------


async def test_write_system_config_inserts_new_row() -> None:
    session = _make_session()
    result = MagicMock()
    result.scalar_one_or_none.return_value = None
    session.execute = AsyncMock(return_value=result)
    await write_system_config(session, "probe_state:org-1", {"fired": True})
    session.add.assert_called_once()
    added = session.add.call_args.args[0]
    assert added.key == "probe_state:org-1"
    assert added.value == {"fired": True}
    session.flush.assert_awaited_once()


async def test_write_system_config_updates_existing_row() -> None:
    session = _make_session()
    existing = MagicMock()
    existing.value = {"fired": False}
    result = MagicMock()
    result.scalar_one_or_none.return_value = existing
    session.execute = AsyncMock(return_value=result)
    await write_system_config(session, "probe_state:org-1", {"fired": True})
    session.add.assert_not_called()
    assert existing.value == {"fired": True}
    session.flush.assert_awaited_once()
