"""Shared SQLAlchemy session/execute test doubles for the CRUD tier tests."""

from typing import Any
from unittest.mock import AsyncMock, MagicMock


def compile_sql(stmt: Any, dialect: Any = None, *, literal_binds: bool = True) -> str:
    """Render a SQLAlchemy statement as SQL, optionally baking literals in.

    The single shared compile helper for the CRUD tier tests: assertions
    operate on the real rendered SQL string rather than brittle AST
    introspection. Pass a real dialect (``sqlite.dialect()``,
    ``postgresql.dialect()``) when the rendered dialect matters — a statement
    rendered with the default dialect is NOT byte-identical to the
    dialect-rendered form.
    """
    compile_kwargs = {"literal_binds": True} if literal_binds else {}
    return str(stmt.compile(dialect=dialect, compile_kwargs=compile_kwargs))


def fake_nested_cm(*, enter_exc: Exception | None = None, return_value: Any = None) -> MagicMock:
    """Async context-manager double standing in for ``session.begin_nested()``.

    Raises *enter_exc* on ``__aenter__`` when given (simulating a failed
    savepoint); otherwise yields *return_value*. ``__aexit__`` always reports
    no exception suppression.
    """
    cm = MagicMock()
    if enter_exc is not None:
        cm.__aenter__ = AsyncMock(side_effect=enter_exc)
    else:
        cm.__aenter__ = AsyncMock(return_value=return_value)
    cm.__aexit__ = AsyncMock(return_value=False)
    return cm


def mock_session() -> MagicMock:
    """Build a MagicMock session whose transaction context is fully mocked."""
    session = MagicMock()
    session.begin = MagicMock(return_value=fake_nested_cm())
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
    return [compile_sql(call.args[0]) for call in session.execute.await_args_list]
