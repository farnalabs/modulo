"""``PageResult.has_more`` is correct-by-construction when omitted (FAR-1877).

``PageResult`` is a plain dataclass. Its ``has_more`` field used to default to
``False``, so ANY producer that omitted the argument silently reported "no more
pages" even when rows remained — the root cause behind a whole sweep of
offset-paginated list endpoints. The field now defaults to ``None`` and
``__post_init__`` computes the offset-pagination identity, so an omitted value
is correct by construction while an explicitly-passed value always wins.

These are pure unit tests of that contract — no database.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest
from sqlalchemy import Select, column
from sqlalchemy import select as sa_select
from sqlalchemy.ext.asyncio import AsyncSession

from modulo.db.crud.base import PageResult
from modulo.db.repositories.base import BaseRepository


def test_omitted_has_more_full_non_final_page_is_true() -> None:
    # page 1 of 45 rows at 20/page: 0 + 20 < 45 -> more remain.
    page: PageResult[int] = PageResult(items=list(range(20)), total=45, page=1, page_size=20)

    assert page.has_more is True


def test_omitted_has_more_final_partial_page_is_false() -> None:
    # page 2 of 25 rows at 20/page: 20 + 5 < 25 is False -> final page.
    page: PageResult[int] = PageResult(items=list(range(5)), total=25, page=2, page_size=20)

    assert page.has_more is False


def test_omitted_has_more_exact_last_full_page_is_false() -> None:
    # page 2 of exactly 40 rows at 20/page: 20 + 20 < 40 is False -> no more.
    page: PageResult[int] = PageResult(items=list(range(20)), total=40, page=2, page_size=20)

    assert page.has_more is False


def test_omitted_has_more_empty_fallback_is_false() -> None:
    page: PageResult[int] = PageResult(items=[], total=0, page=1, page_size=20)

    assert page.has_more is False


def test_explicit_has_more_false_overrides_would_be_true() -> None:
    # 20 + 20 < 100 would compute True, but the explicit value must win.
    page: PageResult[int] = PageResult(items=list(range(20)), total=100, page=1, page_size=20, has_more=False)

    assert page.has_more is False


def test_explicit_has_more_true_overrides_would_be_false() -> None:
    # 20 + 5 < 25 would compute False, but the explicit value must win.
    page: PageResult[int] = PageResult(items=list(range(5)), total=25, page=2, page_size=20, has_more=True)

    assert page.has_more is True


class _ConcreteRepo(BaseRepository):
    def apply_tenant_filter(self, stmt: Select, org_id: object) -> Select:
        return stmt


@pytest.fixture
def repo() -> _ConcreteRepo:
    return _ConcreteRepo(session_factory=MagicMock())


def _mock_paginate_session(items: list[str], total: int) -> AsyncMock:
    session = AsyncMock(spec=AsyncSession)

    count_result = MagicMock()
    count_result.scalar_one_or_none.return_value = total

    scalars_result = MagicMock()
    scalars_result.all.return_value = items
    items_result = MagicMock()
    items_result.scalars.return_value = scalars_result

    session.execute = AsyncMock(side_effect=[count_result, items_result])
    return session


def _mock_stmt() -> MagicMock:
    stmt = MagicMock(spec=Select)
    stmt.order_by.return_value.subquery.return_value = sa_select(column("x")).subquery()
    return stmt


async def test_base_repository_paginate_reports_has_more_on_full_non_final_page(repo: _ConcreteRepo) -> None:
    session = _mock_paginate_session(["a", "b"], total=5)

    page = await repo.paginate(session, _mock_stmt(), page=1, page_size=2)

    assert page.total == 5
    assert page.has_more is True


async def test_base_repository_paginate_reports_no_more_on_final_page(repo: _ConcreteRepo) -> None:
    session = _mock_paginate_session(["a"], total=5)

    page = await repo.paginate(session, _mock_stmt(), page=3, page_size=2)

    assert page.total == 5
    assert page.has_more is False
