"""Helpers for contract-correct SQLAlchemy async session test doubles."""

from unittest.mock import AsyncMock, MagicMock


def configure_mock_session(session: AsyncMock) -> AsyncMock:
    """Configure AsyncSession methods that are synchronous in SQLAlchemy."""
    bind = MagicMock()
    bind.dialect.name = "sqlite"
    session.in_transaction = MagicMock(return_value=True)
    session.get_bind = MagicMock(return_value=bind)
    session.add = MagicMock()
    session.add_all = MagicMock()
    session.expunge = MagicMock()
    session.info = {}
    nested = MagicMock()
    nested.__aenter__ = AsyncMock(return_value=None)
    nested.__aexit__ = AsyncMock(return_value=False)
    session.begin_nested = MagicMock(return_value=nested)
    result = MagicMock()
    result.scalar.return_value = 0
    result.scalar_one.return_value = 0
    result.scalar_one_or_none.return_value = None
    result.first.return_value = None
    result.all.return_value = []
    result.scalars.return_value.all.return_value = []
    session.execute = AsyncMock(return_value=result)
    return session
