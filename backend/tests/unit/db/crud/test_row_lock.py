"""Unit tests for ``modulo.db.crud.row_lock`` (FAR-1592 connection seam).

``set_mutation_row_lock_timeout`` historically accepted only an
``AsyncSession``. FAR-1592's periodic sweeps in ``core.run_admission`` drive
their per-org transaction off ``engine.connect()`` — an ``AsyncConnection``,
not a session — so the dialect gate must resolve the connection's own
``dialect`` attribute (``AsyncConnection`` does not proxy ``get_bind()``; only
the listed ATTRIBUTES are proxied, and ``get_bind`` is a method).

These tests take the REAL connection shape (SQLite in-memory, where the bound
is a documented no-op) so the ``isinstance(..., AsyncConnection)`` branch is
exercised rather than only the session shape every other test drives.
"""

from __future__ import annotations

from collections.abc import AsyncIterator

import pytest
from sqlalchemy.ext.asyncio import AsyncConnection, create_async_engine

from modulo.db.crud import row_lock
from modulo.db.crud.row_lock import _dialect_name, set_mutation_row_lock_timeout


@pytest.fixture
async def conn() -> AsyncIterator[AsyncConnection]:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    try:
        async with engine.connect() as connection:
            yield connection
    finally:
        await engine.dispose()


async def test_dialect_name_reads_connection_dialect_directly(conn: AsyncConnection) -> None:
    """The ``AsyncConnection`` shape resolves via ``conn.dialect.name``."""
    assert isinstance(conn, AsyncConnection)
    assert await _dialect_name(conn) == "sqlite"


async def test_bound_gates_on_connection_dialect_without_get_bind(
    conn: AsyncConnection,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The bound must not route an ``AsyncConnection`` through the session
    helper (which would call the non-existent ``get_bind()``)."""

    async def _never(_session: object) -> str:
        raise AssertionError("get_dialect_name must not be used for an AsyncConnection")

    monkeypatch.setattr(row_lock, "get_dialect_name", _never)
    # SQLite has no ``set_config``: the bound is a documented safe no-op, so
    # this must neither raise nor call ``get_dialect_name``.
    await set_mutation_row_lock_timeout(conn)
