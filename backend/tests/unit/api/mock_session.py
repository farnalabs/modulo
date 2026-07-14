"""Helpers for contract-correct SQLAlchemy async session test doubles."""

from typing import Any
from unittest.mock import DEFAULT, AsyncMock, MagicMock


def configure_mock_session(session: AsyncMock, *, allow_empty_execute: bool = False) -> AsyncMock:
    """Configure AsyncSession contracts, requiring explicit query results by default."""
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
    if allow_empty_execute:
        result = MagicMock()
        result.scalar.return_value = 0
        result.scalar_one.return_value = 0
        result.scalar_one_or_none.return_value = None
        result.first.return_value = None
        result.all.return_value = []
        result.scalars.return_value.all.return_value = []
        session.execute = AsyncMock(return_value=result)
    else:
        execute = AsyncMock()

        def require_explicit_result(*args: Any, **kwargs: Any) -> Any:
            if execute._mock_return_value is not DEFAULT:
                return execute._mock_return_value
            raise AssertionError(
                "Unexpected session.execute(); stub the expected result or opt in with allow_empty_execute=True"
            )

        execute.side_effect = require_explicit_result
        session.execute = execute
    return session
