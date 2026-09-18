"""Unit tests for model_backend CRUD tier filtering.

Tests the default-behaviour, None-handling, empty-list, explicit-filter,
org-scoping, pagination, and error-path code paths in the function.
No DB — uses mock sessions.
"""

import uuid
from unittest.mock import AsyncMock, MagicMock

from sqlalchemy.exc import ProgrammingError

from modulo.db.crud.model_backend import list_model_backends
from modulo.db.models.model_backend import ModelBackend
from tests.unit.crud.helpers import executed_sql, mock_execute, mock_session

_ORG_ID = uuid.UUID("00000000-0000-0000-0000-000000000001")


async def test_default_excludes_in_dev() -> None:
    session = mock_session()
    session.execute = mock_execute(model=ModelBackend, count=2)

    result = await list_model_backends(session, org_id=_ORG_ID)

    assert result.total == 2
    assert len(result.items) == 2
    for sql in executed_sql(session):
        assert "tier NOT IN ('in_dev')" in sql
        assert f"organisation_id = '{_ORG_ID.hex}'" in sql


async def test_excluded_tiers_none_same_as_default() -> None:
    session = mock_session()
    session.execute = mock_execute(model=ModelBackend, count=2)

    result = await list_model_backends(session, org_id=_ORG_ID, excluded_tiers=None)

    assert result.total == 2
    for sql in executed_sql(session):
        assert "tier NOT IN ('in_dev')" in sql


async def test_excluded_tiers_explicit_in_dev() -> None:
    session = mock_session()
    session.execute = mock_execute(model=ModelBackend, count=1)

    result = await list_model_backends(session, org_id=_ORG_ID, excluded_tiers=["in_dev"])

    assert result.total == 1
    for sql in executed_sql(session):
        assert "tier NOT IN ('in_dev')" in sql


async def test_excluded_tiers_empty_list_in_dev() -> None:
    session = mock_session()
    session.execute = mock_execute(model=ModelBackend, count=2)

    result = await list_model_backends(session, org_id=_ORG_ID, excluded_tiers=[])

    assert result.total == 2
    for sql in executed_sql(session):
        assert "tier NOT IN" not in sql
        assert f"organisation_id = '{_ORG_ID.hex}'" in sql


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
    items_sql = executed_sql(session)[1]
    assert "LIMIT 10" in items_sql
    assert "OFFSET 10" in items_sql
    assert "tier NOT IN ('preview')" in items_sql
    assert f"organisation_id = '{_ORG_ID.hex}'" in items_sql


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
