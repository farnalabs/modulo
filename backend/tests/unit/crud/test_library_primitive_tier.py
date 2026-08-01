"""Unit tests for library_primitive CRUD tier filtering.

Tests the default-behaviour, None-handling, empty-list, explicit-filter,
search/type composition, org-scoping, pagination, error-path, and
cursor-pagination code paths in the function.  No DB — uses mock sessions.
"""

import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from sqlalchemy.exc import SQLAlchemyError

from modulo.db.crud.library_primitive import list_library_primitives
from modulo.db.models.library_primitive import LibraryPrimitive
from tests.unit.crud.helpers import executed_sql, mock_execute, mock_session

ORG_ID = uuid.uuid4()
ORG_ID_SQL = f"organisation_id = '{ORG_ID.hex}'"


async def test_default_excludes_in_dev() -> None:
    session = mock_session()
    session.execute = mock_execute(model=LibraryPrimitive, count=2)

    result = await list_library_primitives(session, org_id=ORG_ID)

    assert result.total == 2
    assert len(result.items) == 2
    for sql in executed_sql(session):
        assert "tier NOT IN ('in_dev')" in sql
        assert ORG_ID_SQL in sql
        assert "deleted_at IS NULL" in sql


async def test_excluded_tiers_none_same_as_default() -> None:
    session = mock_session()
    session.execute = mock_execute(model=LibraryPrimitive, count=2)

    result = await list_library_primitives(session, org_id=ORG_ID, excluded_tiers=None)

    assert result.total == 2
    for sql in executed_sql(session):
        assert "tier NOT IN ('in_dev')" in sql


async def test_excluded_tiers_explicit_in_dev() -> None:
    session = mock_session()
    session.execute = mock_execute(model=LibraryPrimitive, count=1)

    result = await list_library_primitives(session, org_id=ORG_ID, excluded_tiers=["in_dev"])

    assert result.total == 1
    for sql in executed_sql(session):
        assert "tier NOT IN ('in_dev')" in sql


async def test_excluded_tiers_empty_skips_filter() -> None:
    session = mock_session()
    session.execute = mock_execute(model=LibraryPrimitive, count=5)

    result = await list_library_primitives(session, org_id=ORG_ID, excluded_tiers=[])

    assert result.total == 5
    for sql in executed_sql(session):
        assert "tier NOT IN" not in sql
        assert ORG_ID_SQL in sql
        assert "deleted_at IS NULL" in sql


async def test_excluded_tiers_preview() -> None:
    session = mock_session()
    session.execute = mock_execute(model=LibraryPrimitive, count=3)

    result = await list_library_primitives(session, org_id=ORG_ID, excluded_tiers=["preview"])

    assert result.total == 3
    for sql in executed_sql(session):
        assert "tier NOT IN ('preview')" in sql


async def test_org_id_none_omits_org_condition() -> None:
    """org_id=None must drop the org scoping condition but keep the tier filter."""
    session = mock_session()
    session.execute = mock_execute(model=LibraryPrimitive, count=1)

    result = await list_library_primitives(session, org_id=None)

    assert result.total == 1
    for sql in executed_sql(session):
        assert "organisation_id =" not in sql
        assert "deleted_at IS NULL" in sql
        assert "tier NOT IN ('in_dev')" in sql


async def test_excluded_tiers_with_search_and_type() -> None:
    """Verify the tier filter composes correctly with other conditions."""
    session = mock_session()
    session.execute = mock_execute(model=LibraryPrimitive, count=1)

    result = await list_library_primitives(
        session,
        org_id=ORG_ID,
        primitive_type="schema",
        search="test",
        excluded_tiers=["preview"],
    )

    assert result.total == 1
    for sql in executed_sql(session):
        assert "primitive_type = 'schema'" in sql
        assert "LIKE lower('%test%')" in sql
        assert "tier NOT IN ('preview')" in sql
        assert ORG_ID_SQL in sql


async def test_page_offset_applied() -> None:
    """page=2 must offset the items query while keeping all filters."""
    session = mock_session()
    session.execute = mock_execute(model=LibraryPrimitive, count=7)

    result = await list_library_primitives(session, org_id=ORG_ID, page=2, page_size=10, excluded_tiers=["preview"])

    assert result.total == 7
    items_sql = executed_sql(session)[1]
    assert "LIMIT 10" in items_sql
    assert "OFFSET 10" in items_sql
    assert "tier NOT IN ('preview')" in items_sql
    assert ORG_ID_SQL in items_sql


async def test_sqlalchemy_error_is_re_raised() -> None:
    """A SQLAlchemyError on the count query must propagate (unlike the empty-page fallback)."""
    session = mock_session()
    session.execute = AsyncMock(side_effect=SQLAlchemyError("boom"))

    with pytest.raises(SQLAlchemyError):
        await list_library_primitives(session, org_id=ORG_ID)


async def test_cursor_pagination_applies_filters() -> None:
    """The cursor path must forward all built conditions into the paginated stmt."""
    session = mock_session()
    paginator = MagicMock()
    paginator.paginate = AsyncMock(
        return_value=MagicMock(
            items=[MagicMock(spec=LibraryPrimitive)],
            total=1,
            next_cursor="next",
            has_more=False,
        )
    )

    with patch("modulo.db.crud.library_primitive.CursorPaginator", return_value=paginator):
        result = await list_library_primitives(
            session,
            org_id=ORG_ID,
            cursor="abc",
            primitive_type="schema",
            excluded_tiers=["preview"],
        )

    assert result.total == 1
    assert len(result.items) == 1
    assert result.next_cursor == "next"
    stmt = paginator.paginate.await_args.args[1]
    sql = str(stmt.compile(compile_kwargs={"literal_binds": True}))
    assert "tier NOT IN ('preview')" in sql
    assert ORG_ID_SQL in sql
    assert "primitive_type = 'schema'" in sql
