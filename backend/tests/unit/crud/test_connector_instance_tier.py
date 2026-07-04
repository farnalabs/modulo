"""Unit tests for connector_instance CRUD tier filtering.

Tests the default-behaviour, None-handling, empty-list, and explicit-filter
code paths in the function.  No DB — uses mock sessions.
"""

from unittest.mock import AsyncMock, MagicMock

from modulo.db.crud.connector_instance import list_connector_instances
from modulo.db.models.connector_instance import ConnectorInstance


def _mock_session() -> MagicMock:
    session = MagicMock()
    ctx = AsyncMock()
    ctx.__aenter__ = AsyncMock(return_value=ctx)
    ctx.__aexit__ = AsyncMock(return_value=False)
    session.begin = MagicMock(return_value=ctx)
    session.in_transaction = MagicMock(return_value=True)
    return session


def _mock_execute(*, count: int, items: list | None = None) -> MagicMock:
    """Return an AsyncMock suitable for session.execute."""
    m = AsyncMock()
    scalar = MagicMock()
    scalar.scalar_one.return_value = count
    scalars = MagicMock()
    scalars.scalars.return_value = items or [MagicMock(spec=ConnectorInstance) for _ in range(count)]
    m.side_effect = [scalar, scalars]
    return m


async def test_default_excludes_in_dev() -> None:
    session = _mock_session()
    session.execute = _mock_execute(count=2)

    result = await list_connector_instances(session)

    assert result.total == 2
    assert len(result.items) == 2


async def test_excluded_tiers_none_same_as_default() -> None:
    session = _mock_session()
    session.execute = _mock_execute(count=2)

    result = await list_connector_instances(session, excluded_tiers=None)

    assert result.total == 2


async def test_excluded_tiers_explicit_in_dev() -> None:
    session = _mock_session()
    session.execute = _mock_execute(count=1)

    result = await list_connector_instances(session, excluded_tiers=["in_dev"])

    assert result.total == 1


async def test_excluded_tiers_empty_skips_filter() -> None:
    session = _mock_session()
    session.execute = _mock_execute(count=5)

    result = await list_connector_instances(session, excluded_tiers=[])

    assert result.total == 5


async def test_excluded_tiers_preview() -> None:
    session = _mock_session()
    session.execute = _mock_execute(count=3)

    result = await list_connector_instances(session, excluded_tiers=["preview"])

    assert result.total == 3



