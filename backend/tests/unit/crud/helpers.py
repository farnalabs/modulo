"""Shared SQLAlchemy session/execute test doubles for the CRUD tier tests."""

import contextlib
from typing import Any
from unittest.mock import AsyncMock, MagicMock

from sqlalchemy import Column, Select, TextClause


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


# ---------------------------------------------------------------------------
# Structural statement inspection
#
# Every discriminator below keys on the SQLAlchemy statement' actual STRUCTURE
# (what it selects, what its WHERE references) rather than on rendered-SQL
# substrings or call positions, so a refactor that changes compilation while
# preserving meaning keeps the assertions truthful.
# ---------------------------------------------------------------------------


def is_count_select(stmt: Any) -> bool:
    """True for ``SELECT count(...)`` statements (no ORM entity in the result).

    The entity must be EXPLICITLY ``None`` — a description entry without an
    ``entity`` key at all (SQLAlchemy omits it for bare literal selects like
    ``select(1)``) does NOT count as count-shaped; ``mock_execute``'s routing
    then fails those statements loudly instead of silently serving them.
    """
    if not isinstance(stmt, Select):
        return False
    descriptions = stmt.column_descriptions
    return bool(descriptions) and all("entity" in d and d["entity"] is None for d in descriptions)


def is_entity_select(stmt: Any, model: type[Any]) -> bool:
    """True for ``SELECT model`` statements returning whole ORM rows of *model*.

    Entries without an ``entity`` key (bare literal selects) are never
    entity-selects — use ``.get`` so such statements route on to
    ``is_count_select`` and the loud final assertion instead of a KeyError.
    """
    if not isinstance(stmt, Select):
        return False
    descriptions = stmt.column_descriptions
    return bool(descriptions) and all(d.get("entity") is model for d in descriptions)


def is_statement_timeout_plumbing(stmt: Any) -> bool:
    """True for the production ``SELECT set_config('statement_timeout', :ms, ...)`` text.

    Discriminated structurally — a ``TextClause`` whose bind params include
    ``ms`` — rather than by substring-matching ``set_config``, which would also
    match the RLS ``app.*`` set_config statements. Verified empirically: the
    timeout statement's only named bind is ``ms``; a bare ``SELECT 1`` has none.
    """
    return isinstance(stmt, TextClause) and "ms" in stmt._bindparams


def where_column_names(stmt: Any) -> frozenset[str]:
    """Column keys referenced by the statement's WHERE clause (structural).

    Walks the WHERE-criteria expression tree collecting ``Column`` keys — the
    dialect-free replacement for negative substring assertions like
    ``'tier NOT IN' not in sql``, which can also match inside a LIKE literal or
    a rendered comment and so under-enforce their own point.
    """
    out: set[str] = set()
    criteria = getattr(stmt, "_where_criteria", None)
    stack = list(criteria) if criteria else []
    while stack:
        node = stack.pop()
        if isinstance(node, Column):
            out.add(node.key)
        with contextlib.suppress(AttributeError, TypeError):
            stack.extend(node.get_children())
    return frozenset(out)


# ---------------------------------------------------------------------------
# Session doubles
# ---------------------------------------------------------------------------


def executed_statements(session: Any) -> list[Any]:
    """Every statement passed to ``session.execute``, in execution order."""
    return [call.args[0] for call in session.execute.await_args_list]


def find_count_statement(statements: list[Any]) -> Any:
    """The single ``SELECT count(...)`` in *statements* — semantic, not positional."""
    matches = [s for s in statements if is_count_select(s)]
    assert len(matches) == 1, f"expected exactly one count select, found {len(matches)}"
    return matches[0]


def find_entity_statement(statements: list[Any], model: type[Any]) -> Any:
    """The single ``SELECT model`` row-select in *statements* — semantic, not positional."""
    matches = [s for s in statements if is_entity_select(s, model)]
    assert len(matches) == 1, f"expected exactly one {model.__name__} row select, found {len(matches)}"
    return matches[0]


def mock_execute(*, model: type[Any], count: int) -> AsyncMock:
    """Structural ``session.execute`` replacement for the tier list flows.

    Routes each executed statement on its SHAPE instead of call order: the
    ``SELECT model`` items page returns *count* canned rows and the
    ``SELECT count(*)`` returns *count*. Any other statement fails loudly
    instead of being silently served by a positional side-effect list — so an
    unexpected extra query can never be mistaken for one of the expected
    results. The tier list flows these tests serve never execute the
    statement-timeout plumbing text, so that shape is rejected too; a flow
    that starts running it needs a fresh explicit double rather than shared
    silence.
    """
    scalar_result = MagicMock()
    scalar_result.scalar_one.return_value = count
    scalars_result = MagicMock()
    scalars_result.scalars.return_value = [MagicMock(spec=model) for _ in range(count)]

    async def _route(stmt: Any, *_args: Any, **_kwargs: Any) -> MagicMock:
        if is_entity_select(stmt, model):
            return scalars_result
        if is_count_select(stmt):
            return scalar_result
        raise AssertionError(
            f"statement is none of: {model.__name__} row select, count select "
            f"(statement-timeout plumbing text is intentionally rejected)\n{stmt}"
        )

    return AsyncMock(side_effect=_route)


def executed_sql(session: MagicMock) -> list[str]:
    """Compile every statement passed to session.execute with inlined binds."""
    return [compile_sql(call.args[0]) for call in session.execute.await_args_list]
