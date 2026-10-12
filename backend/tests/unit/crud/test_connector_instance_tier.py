"""Unit tests for connector_instance CRUD tier filtering.

Tests the default-behaviour, None-handling, empty-list, explicit-filter,
org-scoping, pagination, error-path, and cursor-pagination code paths in the
function. No DB — uses mock sessions.
"""

import uuid
from unittest.mock import AsyncMock, MagicMock, patch

from sqlalchemy.exc import ProgrammingError

from modulo.db.crud.connector_instance import list_connector_instances
from modulo.db.models.connector_instance import ConnectorInstance
from tests.unit.crud.helpers import compile_sql, executed_sql, mock_execute, mock_session

_ORG = uuid.UUID("33333333-3333-3333-3333-333333333333")
# Statement compilation with literal binds under the DEFAULT dialect renders
# :bindparam tokens as 32-char hex — the same rendering every org predicate
# assertion in this tier relies on.
_ORG_SQL = f"organisation_id = '{_ORG.hex}'"


async def test_default_excludes_in_dev() -> None:
    session = mock_session()
    session.execute = mock_execute(model=ConnectorInstance, count=2)

    result = await list_connector_instances(session)

    assert result.total == 2
    assert len(result.items) == 2
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


async def test_org_scoping_applies_to_count_and_items() -> None:
    """tenant-scope: BOTH executed statements — the count and the items page —
    must carry the ``organisation_id`` predicate, not just the items query."""
    session = mock_session()
    session.execute = mock_execute(model=ConnectorInstance, count=2)

    result = await list_connector_instances(session, organisation_id=_ORG)

    assert result.total == 2
    for sql in executed_sql(session):
        assert _ORG_SQL in sql


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
    assert not result.items


async def test_programming_error_on_items_query_returns_empty_result() -> None:
    """A ProgrammingError on the items query (after a successful count) must also degrade."""
    session = mock_session()
    count_result = MagicMock()
    count_result.scalar_one.return_value = 7
    session.execute = AsyncMock(side_effect=[count_result, ProgrammingError("stmt", {}, RuntimeError("boom"))])

    result = await list_connector_instances(session)

    assert result.total == 0
    assert not result.items


async def test_cursor_pagination_applies_filter() -> None:
    """The cursor path must forward the tier + org filters into the paginated stmt."""
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
        result = await list_connector_instances(session, organisation_id=_ORG, cursor="abc", excluded_tiers=["preview"])

    paginator_cls.assert_called_once()
    assert result.total == 1
    assert len(result.items) == 1
    assert result.next_cursor == "next"
    assert result.has_more is False
    stmt = paginator.paginate.await_args.args[1]
    sql = compile_sql(stmt)
    assert "tier NOT IN ('preview')" in sql
    assert _ORG_SQL in sql
