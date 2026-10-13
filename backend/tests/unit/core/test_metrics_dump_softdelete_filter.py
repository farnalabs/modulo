"""FAR-1009 / FAR-1729: analytics eval-definition counts — soft-delete filter + live source table.

Covers:
  - ``product_analytics.metrics_dump._count_entities`` (per-org eval-definition
    count): the query must filter ``deleted_at`` (FAR-1009) AND must read the
    live ``evals`` table, not the frozen ``eval_definitions`` snapshot (the
    0254 cutover froze that table — no production write path post-cutover, so
    counting it under-reports every post-cutover definition).

The tests assert the emitted WHERE clause contains ``deleted_at`` and the
statement source is ``evals``. Uses a mocked session — no DB required.

Without the FAR-1009 filter the WHERE clause omits ``deleted_at`` and the test
fails on the assertion. Reverting the source to ``eval_definitions``
(FAR-1729) fails the source-table pin.
"""

from __future__ import annotations

import uuid
from typing import Any
from unittest.mock import AsyncMock, MagicMock

from sqlalchemy.ext.asyncio import AsyncSession


async def test_eval_definitions_count_where_clause_contains_deleted_at():
    """_count_entities must add deleted_at IS NULL to the eval-definition query.

    This is the direct regression guard: if the filter is dropped, soft-deleted
    definitions inflate the analytics count. FAR-1729: the query must also
    source the count from the live ``evals`` table.
    """
    from modulo.core.product_analytics.metrics_dump import _count_entities

    org_id = uuid.uuid4()

    # Scripted results: each query returns a count of 0
    count_result = MagicMock()
    count_result.scalar_one.return_value = 0

    captured_stmts: list[Any] = []

    async def _scripted_execute(stmt: Any, *_args: Any, **_kwargs: Any) -> Any:
        captured_stmts.append(stmt)
        return count_result

    session = AsyncMock(spec=AsyncSession)
    session.execute = _scripted_execute

    await _count_entities(session, [org_id])

    # The eval-definition count is one of the captured statements. Since the
    # FAR-1729 repoint it references the live ``evals`` table.
    eval_def_stmts = [s for s in captured_stmts if "FROM evals" in str(s)]
    assert len(eval_def_stmts) >= 1, f"Expected at least one evals-table query, got {len(captured_stmts)} statements"

    for stmt in eval_def_stmts:
        where_clause = str(stmt.whereclause)
        assert "deleted_at" in where_clause, (
            f"Expected 'deleted_at' in the eval-definition WHERE clause but got: {where_clause}"
        )
        # FAR-1729 source-table pin: the count must never read the frozen table.
        assert "eval_definitions" not in str(stmt), (
            f"eval-definition count must read evals, not the frozen table, got: {stmt}"
        )
