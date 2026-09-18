"""Shared SQLAlchemy session/execute test doubles for the CRUD tier tests."""

from typing import Any
from unittest.mock import AsyncMock, MagicMock


def mock_session() -> MagicMock:
    """Build a MagicMock session whose transaction context is fully mocked."""
    session = MagicMock()
    ctx = AsyncMock()
    ctx.__aenter__ = AsyncMock(return_value=ctx)
    ctx.__aexit__ = AsyncMock(return_value=False)
    session.begin = MagicMock(return_value=ctx)
    session.in_transaction = MagicMock(return_value=True)
    return session


def mock_execute(*, model: type[Any], count: int) -> AsyncMock:
    """Return an AsyncMock for session.execute yielding a count result then an items result."""
    m = AsyncMock()
    scalar = MagicMock()
    scalar.scalar_one.return_value = count
    scalars = MagicMock()
    scalars.scalars.return_value = [MagicMock(spec=model) for _ in range(count)]
    m.side_effect = [scalar, scalars]
    return m


def executed_sql(session: MagicMock) -> list[str]:
    """Compile every statement passed to session.execute with inlined binds."""
    return [
        str(call.args[0].compile(compile_kwargs={"literal_binds": True})) for call in session.execute.await_args_list
    ]
