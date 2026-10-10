"""Shared session-dispatch helpers for the team-deletion BDD step modules.

``test_team_deletion.py`` and ``test_team_deletion_blocked.py`` both drive the
REAL ``delete_team_endpoint`` through the shared ``mock_session`` with only the
DB/RLS seams patched. These helpers live here — one copy — so the dispatch
wiring cannot drift between the two step files.
"""

from typing import Any
from unittest.mock import MagicMock


def _stmt_from_tables(stmt: object) -> list[str]:
    """Lower-cased table names of the FROM clauses *stmt* selects from.

    Reads the targeted ORM entity off the SQLAlchemy statement object itself
    (``Select.get_final_froms()``) instead of substring-matching its compiled
    SQL text, so a model rename or dialect quoting can never desynchronise the
    mock's answer from what the statement actually targets. Statements without
    FROM clauses (INSERT / UPDATE / raw text) yield no tables and fall through
    to the default result.
    """
    get_froms = getattr(stmt, "get_final_froms", None)
    if get_froms is None:
        return []
    try:
        froms = get_froms()
    except Exception:
        return []
    tables: list[str] = []
    for selectable in froms:
        name = getattr(selectable, "name", None)
        if isinstance(name, str):
            tables.append(name.lower())
    return tables


def _result(*, scalar: int = 0, row: Any = None) -> MagicMock:
    """Build a session-result mock the delete endpoint's reads can consume."""
    result = MagicMock()
    result.scalar = MagicMock(return_value=scalar)
    result.scalar_one_or_none = MagicMock(return_value=row)
    return result


def _configure_delete_session(session: Any, *, table_counts: dict[str, int], team_row: Any) -> None:
    """Wire the shared mock session so the REAL delete endpoint runs end to end.

    ``delete_team_endpoint`` issues four ``select(func.count())`` queries, one
    per resource model (pipeline / connector / model backend / library
    primitive), plus the real ``get_team`` read (``select(Team)``) on the
    soft-delete path. Queries are dispatched by the ORM entity they select
    FROM — the statement's own FROM table, never its SQL text — so a scenario
    blocks on ITS OWN resource type (every other type reports zero) and the
    ``teams`` read returns *team_row*: a truthy row for an existing team, or
    ``None`` so the real soft-delete path reports "not found" (404).

    Audit inserts and every other statement carry no FROM tables and fall
    through to the default zero-count/none-row result, preserving the
    endpoint's fail-closed defaults.
    """

    async def _execute(stmt: object, *_args: object, **_kwargs: object) -> MagicMock:
        from_tables = _stmt_from_tables(stmt)
        if "teams" in from_tables:
            return _result(row=team_row)
        for table, count in table_counts.items():
            if table in from_tables:
                return _result(scalar=count)
        return _result()

    session.execute.side_effect = _execute
