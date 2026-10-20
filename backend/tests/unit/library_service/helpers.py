"""Shared helpers for library_service unit test modules.

``mock_session`` was re-implemented in four test modules with subtly different
behaviour; it lives here so a single definition serves the package.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock


def mock_session(*, in_transaction: bool = True, with_execute: bool = False) -> MagicMock:
    """Return a mock ``AsyncSession`` supporting ``async with session.begin():``.

    ``in_transaction`` controls ``session.in_transaction()``; ``with_execute``
    installs an ``AsyncMock`` for ``session.execute``.
    """
    session = MagicMock()
    ctx = AsyncMock()
    ctx.__aenter__ = AsyncMock(return_value=ctx)
    ctx.__aexit__ = AsyncMock(return_value=False)
    session.begin = MagicMock(return_value=ctx)
    session.in_transaction = MagicMock(return_value=in_transaction)
    if with_execute:
        session.execute = AsyncMock()
    return session
