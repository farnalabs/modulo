"""Unit tests for model_backend CRUD tier filtering.

Tests the default-behaviour, None-handling, empty-list, explicit-filter,
org-scoping, cursor-pagination, pagination, and error-path code paths in the
function. No DB — uses mock sessions.
"""

import uuid
from unittest.mock import AsyncMock, MagicMock, patch

from sqlalchemy.exc import ProgrammingError

from modulo.db.crud.model_backend import list_model_backends
from modulo.db.models.model_backend import ModelBackend
from tests.unit.crud.helpers import (
    compile_sql,
    executed_sql,
    executed_statements,
    find_entity_statement,
    mock_execute,
    mock_session,
    where_column_names,
)

_ORG_ID = uuid.UUID("00000000-0000-0000-0000-000000000001")
# Literal-binds compilation under the DEFAULT dialect renders the org
# Uuid bind as 32-char hex — the same rendering every org predicate
# assertion in this file relies on.
_ORG_SQL = f"organisation_id = '{_ORG_ID.hex}'"


async def test_default_excludes_in_dev() -> None:
    session = mock_session()
    session.execute = mock_execute(model=ModelBackend, count=2)

    result = await list_model_backends(session, org_id=_ORG_ID)

    assert result.total == 2
    assert len(result.items) == 2
    for sql in executed_sql(session):
        assert "tier NOT IN ('in_dev')" in sql
        assert _ORG_SQL in sql


async def test_excluded_tiers_empty_list_in_dev() -> None:
    session = mock_session()
    session.execute = mock_execute(model=ModelBackend, count=2)

    result = await list_model_backends(session, org_id=_ORG_ID, excluded_tiers=[])

    assert result.total == 2
    # The negative is structural: NO tier column may appear in either where
    # clause — not the excluded-tiers predicate, not any other tier term.
    for stmt in executed_statements(session):
        assert "tier" not in where_column_names(stmt)
        assert _ORG_SQL in compile_sql(stmt)


async def test_excluded_tiers_preview_still_filters() -> None:
    session = mock_session()
    session.execute = mock_execute(model=ModelBackend, count=1)

    result = await list_model_backends(session, org_id=_ORG_ID, excluded_tiers=["preview"])

    assert result.total == 1
    for sql in executed_sql(session):
        assert "tier NOT IN ('preview')" in sql


async def test_page_offset_applied() -> None:
    """page=2 must offset the items query while keeping the tier and org filters."""
    session = mock_session()
    session.execute = mock_execute(model=ModelBackend, count=7)

    result = await list_model_backends(session, org_id=_ORG_ID, page=2, page_size=10, excluded_tiers=["preview"])

    assert result.total == 7
    # Selected semantically as the row-select (shape-routed), not by position.
    page_sql = compile_sql(find_entity_statement(executed_statements(session), ModelBackend))
    assert "LIMIT 10" in page_sql
    assert "OFFSET 10" in page_sql
    assert "tier NOT IN ('preview')" in page_sql
    assert _ORG_SQL in page_sql


async def test_cursor_pagination_applies_filters() -> None:
    """The cursor path must forward the tier + org filters into the paginated
    stmt — and map the paginator result through verbatim (the non-cursor
    tests prove the SELECT shape; this one proves the forwarded shape)."""
    session = mock_session()
    paginator = MagicMock()
    paginator.paginate = AsyncMock(
        return_value=MagicMock(
            items=[MagicMock(spec=ModelBackend)],
            total=7,
            next_cursor="zz",
            has_more=True,
        )
    )

    with patch("modulo.db.crud.model_backend.CursorPaginator", return_value=paginator):
        result = await list_model_backends(session, org_id=_ORG_ID, cursor="abc", excluded_tiers=["preview"])

    assert result.total == 7
    assert result.next_cursor == "zz"
    assert result.has_more is True
    stmt = paginator.paginate.await_args.args[1]
    sql = compile_sql(stmt)
    assert "tier NOT IN ('preview')" in sql
    assert _ORG_SQL in sql


async def test_programming_error_returns_empty_result() -> None:
    """A ProgrammingError on the count query must degrade to an empty page."""
    session = mock_session()
    session.execute = AsyncMock(side_effect=ProgrammingError("stmt", {}, RuntimeError("boom")))

    result = await list_model_backends(session, org_id=_ORG_ID)

    assert result.total == 0
    assert not result.items


async def test_programming_error_on_items_query_returns_empty_result() -> None:
    """A ProgrammingError on the items query (after a successful count) must also degrade."""
    session = mock_session()
    count_result = MagicMock()
    count_result.scalar_one.return_value = 7
    session.execute = AsyncMock(side_effect=[count_result, ProgrammingError("stmt", {}, RuntimeError("boom"))])

    result = await list_model_backends(session, org_id=_ORG_ID)

    assert result.total == 0
    assert not result.items
