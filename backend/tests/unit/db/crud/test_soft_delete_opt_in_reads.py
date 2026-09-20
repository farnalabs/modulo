"""Unit tests for the FAR-1025 ``include_deleted=True`` opt-in read paths.

Every CRUD read path that must see soft-deleted rows opts out of the global
soft-delete filter via ``include_soft_deleted``. The integration suite covers
the behaviour against a real database, but integration tests are excluded from
the coverage denominator, so these mocked-session tests cover the opt-in
branches that gate the changed-lines coverage check.
"""

from __future__ import annotations

import uuid
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

from modulo.db.crud.environment_profile import (
    get_environment_profile,
    list_environment_profiles,
)
from modulo.db.crud.node_category import list_node_categories
from modulo.db.crud.observability import update_otel_config
from modulo.db.crud.pipeline import list_pipelines
from modulo.db.crud.view import get_view, list_views


def _result(*, scalar: Any = None, scalars: list[Any] | None = None) -> MagicMock:
    """Build a mock ``Result`` for the read paths under test."""
    result = MagicMock()
    result.scalar_one = MagicMock(return_value=scalar)
    result.scalar_one_or_none = MagicMock(return_value=scalar)
    result.scalars = MagicMock(return_value=MagicMock(all=MagicMock(return_value=scalars or [])))
    return result


# ---------------------------------------------------------------------------
# environment_profile
# ---------------------------------------------------------------------------


async def test_get_environment_profile_include_deleted() -> None:
    profile = MagicMock()
    session = AsyncMock()
    session.execute = AsyncMock(return_value=_result(scalar=profile))

    result = await get_environment_profile(session, uuid.uuid4(), include_deleted=True)

    assert result is profile


async def test_list_environment_profiles_include_deleted() -> None:
    session = AsyncMock()
    session.execute = AsyncMock(side_effect=[_result(scalar=2), _result(scalars=[])])

    result = await list_environment_profiles(session, include_deleted=True)

    assert result.total == 2
    assert not result.items


async def test_list_environment_profiles_include_deleted_cursor() -> None:
    page = MagicMock(items=[], total=0, next_cursor=None, has_more=False)
    session = AsyncMock()
    with patch("modulo.db.crud.environment_profile.CursorPaginator") as paginator_cls:
        paginator_cls.return_value.paginate = AsyncMock(return_value=page)

        result = await list_environment_profiles(session, cursor="abc", include_deleted=True)

    assert result.total == 0
    paginator_cls.return_value.paginate.assert_awaited_once()


# ---------------------------------------------------------------------------
# node_category
# ---------------------------------------------------------------------------


async def test_list_node_categories_include_deleted() -> None:
    session = AsyncMock()
    session.execute = AsyncMock(side_effect=[_result(scalar=1), _result(scalars=[])])

    result = await list_node_categories(session, org_id=uuid.uuid4(), include_deleted=True)

    assert result.total == 1


# ---------------------------------------------------------------------------
# view
# ---------------------------------------------------------------------------


async def test_get_view_include_deleted() -> None:
    view = MagicMock()
    session = AsyncMock()
    session.execute = AsyncMock(return_value=_result(scalar=view))

    result = await get_view(session, uuid.uuid4(), include_deleted=True)

    assert result is view


async def test_list_views_include_deleted() -> None:
    session = AsyncMock()
    session.execute = AsyncMock(side_effect=[_result(scalar=4), _result(scalars=[])])

    result = await list_views(session, include_deleted=True)

    assert result.total == 4


# ---------------------------------------------------------------------------
# pipeline
# ---------------------------------------------------------------------------


async def test_list_pipelines_include_deleted() -> None:
    session = AsyncMock()
    session.execute = AsyncMock(side_effect=[_result(scalar=5), _result(scalars=[])])

    result = await list_pipelines(session, include_deleted=True)

    assert result.total == 5


# ---------------------------------------------------------------------------
# observability
# ---------------------------------------------------------------------------


async def test_update_otel_config_merges_into_opted_in_read() -> None:
    org = MagicMock()
    org.otel_config_json = {"endpoint": "http://otel-collector:4317"}
    session = AsyncMock()
    session.execute = AsyncMock(return_value=_result(scalar=org))
    session.flush = AsyncMock()

    merged = await update_otel_config(session, uuid.uuid4(), {"sampling": 0.5})

    assert merged == {"endpoint": "http://otel-collector:4317", "sampling": 0.5}
    assert org.otel_config_json == merged
    session.flush.assert_awaited_once()
