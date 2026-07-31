"""Unit tests for connector_instance CRUD tier filtering.

Tests the default-behaviour, None-handling, empty-list, explicit-filter,
pagination, error-path, and cursor-pagination code paths in the function.
No DB — uses mock sessions.
"""

from unittest.mock import AsyncMock, MagicMock, patch

from sqlalchemy.exc import ProgrammingError

from modulo.db.crud.connector_instance import list_connector_instances
from modulo.db.models.connector_instance import ConnectorInstance
from tests.unit.crud.helpers import executed_sql, mock_execute, mock_session


async def test_default_excludes_in_dev() -> None:
    session = mock_session()
    session.execute = mock_execute(model=ConnectorInstance, count=2)

    result = await list_connector_instances(session)

    assert result.total == 2
    assert len(result.items) == 2
    for sql in executed_sql(session):
        assert "tier NOT IN ('in_dev')" in sql


async def test_excluded_tiers_none_same_as_default() -> None:
    session = mock_session()
    session.execute = mock_execute(model=ConnectorInstance, count=2)

    result = await list_connector_instances(session, excluded_tiers=None)

    assert result.total == 2
    for sql in executed_sql(session):
        assert "tier NOT IN ('in_dev')" in sql


async def test_excluded_tiers_explicit_in_dev() -> None:
    session = mock_session()
    session.execute = mock_execute(model=ConnectorInstance, count=1)

    result = await list_connector_instances(session, excluded_tiers=["in_dev"])

    assert result.total == 1
    for sql in executed_sql(session):
        assert "tier NOT IN ('in_dev')" in sql


async def test_excluded_tiers_empty_skips_filter() -> None:
    session = mock_session()
    session.execute = mock_execute(model=ConnectorInstance, count=5)

    result = await list_connector_instances(session, excluded_tiers=[])

    assert result.total == 5
    assert all("tier NOT IN" not in sql for sql in executed_sql(session))


async def test_excluded_tiers_preview() -> None:
    session = mock_session()
    session.execute = mock_execute(model=ConnectorInstance, count=3)

    result = await list_connector_instances(session, excluded_tiers=["preview"])

    assert result.total == 3
    for sql in executed_sql(session):
        assert "tier NOT IN ('preview')" in sql


async def test_page_offset_applied() -> None:
    """page=2 must offset the items query while keeping the tier filter."""
    session = mock_session()
    session.execute = mock_execute(model=ConnectorInstance, count=7)

    result = await list_connector_instances(session, page=2, page_size=10, excluded_tiers=["preview"])

    assert result.total == 7
    items_sql = executed_sql(session)[1]
    assert "LIMIT 10" in items_sql
    assert "OFFSET 10" in items_sql
    assert "tier NOT IN ('preview')" in items_sql


async def test_programming_error_returns_empty_result() -> None:
    """A ProgrammingError on the count query must degrade to an empty page."""
    session = mock_session()
    session.execute = AsyncMock(side_effect=ProgrammingError("stmt", {}, RuntimeError("boom")))

    result = await list_connector_instances(session)

    assert result.total == 0
    assert result.items == []


async def test_programming_error_on_items_query_returns_empty_result() -> None:
    """A ProgrammingError on the items query (after a successful count) must also degrade."""
    session = mock_session()
    count_result = MagicMock()
    count_result.scalar_one.return_value = 7
    session.execute = AsyncMock(side_effect=[count_result, ProgrammingError("stmt", {}, RuntimeError("boom"))])

    result = await list_connector_instances(session)

    assert result.total == 0
    assert result.items == []


async def test_cursor_pagination_applies_filter() -> None:
    """The cursor path must forward the tier filter into the paginated stmt."""
    session = mock_session()
    paginator = MagicMock()
    paginator.paginate = AsyncMock(
        return_value=MagicMock(
            items=[MagicMock(spec=ConnectorInstance)],
            total=1,
            next_cursor="next",
            has_more=False,
        )
    )

    with patch("modulo.db.crud.connector_instance.CursorPaginator", return_value=paginator) as paginator_cls:
        result = await list_connector_instances(session, cursor="abc", excluded_tiers=["preview"])

    paginator_cls.assert_called_once()
    assert result.total == 1
    assert len(result.items) == 1
    assert result.next_cursor == "next"
    stmt = paginator.paginate.await_args.args[1]
    assert "tier NOT IN ('preview')" in str(stmt.compile(compile_kwargs={"literal_binds": True}))
