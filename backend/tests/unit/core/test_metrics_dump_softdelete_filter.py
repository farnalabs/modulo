"""FAR-1009: soft-deleted EvalDefinitions must not appear in analytics counts.

Covers:
  - ``product_analytics.metrics_dump._aggregate_entity_counts`` (per-org
    eval_definitions count)

The test asserts the emitted WHERE clause contains ``deleted_at`` so a future
regression that drops the filter is caught. Uses a mocked session — no DB
required.

Without the FAR-1009 filter the WHERE clause omits ``deleted_at`` and the test
fails on the assertion.
"""

from __future__ import annotations

import uuid
from typing import Any
from unittest.mock import AsyncMock, MagicMock

from sqlalchemy.ext.asyncio import AsyncSession


async def test_eval_definitions_count_where_clause_contains_deleted_at():
    """_aggregate_entity_counts must add deleted_at IS NULL to the eval_definitions query.

    This is the direct regression guard: if the filter is dropped, soft-deleted
    definitions inflate the analytics count.
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

    # The eval_definitions query is one of the captured statements.
    # Find it by checking which statement references EvalDefinition.
    eval_def_stmts = [s for s in captured_stmts if "eval_definition" in str(s).lower()]
    assert len(eval_def_stmts) >= 1, f"Expected at least one EvalDefinition query, got {len(captured_stmts)} statements"

    for stmt in eval_def_stmts:
        where_clause = str(stmt.whereclause)
        assert "deleted_at" in where_clause, (
            f"Expected 'deleted_at' in eval_definitions WHERE clause but got: {where_clause}"
        )
