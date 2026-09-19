"""FAR-1009: soft-deleted EvalDefinitions must be excluded from coverage/gap signals.

Covers:
  - ``variant_group.get_coverage_gaps`` (eval gap detection)
  - ``variant_group.has_pipeline_default_evals`` (pipeline has-eval signal)

Each test asserts the emitted WHERE clause contains ``deleted_at`` so a future
regression that drops the filter is caught. Tests use mocked sessions — no DB
required.

Without the FAR-1009 filter the WHERE clause omits ``deleted_at`` and the test
fails on the assertion.
"""

from __future__ import annotations

import uuid
from typing import Any
from unittest.mock import AsyncMock, MagicMock

from sqlalchemy.ext.asyncio import AsyncSession

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_session(execute_return: Any | None = None) -> AsyncMock:
    """Build a mock session whose execute() returns a scripted result."""
    session = AsyncMock(spec=AsyncSession)
    if execute_return is not None:
        session.execute = AsyncMock(return_value=execute_return)
    return session


def _scalars_result(rows: list[Any]) -> MagicMock:
    """Mock result that supports list(result.scalars()) and result.scalars().all()."""
    result = MagicMock()
    scalars_mock = MagicMock()
    scalars_mock.all.return_value = rows
    scalars_mock.__iter__ = MagicMock(return_value=iter(rows))
    result.scalars.return_value = scalars_mock
    # Also support list(result.scalars()) — list() calls __iter__ on the mock
    result.scalars = MagicMock(return_value=scalars_mock)
    return result


def _scalar_result(value: Any) -> MagicMock:
    result = MagicMock()
    result.scalar_one_or_none.return_value = value
    return result


# ---------------------------------------------------------------------------
# variant_group.get_coverage_gaps
# ---------------------------------------------------------------------------


async def test_get_coverage_gaps_includes_deleted_at_filter():
    """get_coverage_gaps must add deleted_at IS NULL to the WHERE."""
    from modulo.db.crud.variant_group import get_coverage_gaps

    pipeline_id = uuid.uuid4()

    # Build a mock VariantGroup with no eval_definition_ids on variants
    group = MagicMock()
    group.pipeline_id = pipeline_id
    group.variants = [{"id": "v1", "name": "A"}]

    session = _make_session(_scalars_result([]))
    await get_coverage_gaps(session, group)

    call_args = session.execute.call_args
    stmt = call_args[0][0]
    where_clause = str(stmt.whereclause)
    assert "deleted_at" in where_clause, f"Expected 'deleted_at' in WHERE clause but got: {where_clause}"


# ---------------------------------------------------------------------------
# variant_group.has_pipeline_default_evals
# ---------------------------------------------------------------------------


async def test_has_pipeline_default_evals_includes_deleted_at_filter():
    """has_pipeline_default_evals must add deleted_at IS NULL to the WHERE."""
    from modulo.db.crud.variant_group import has_pipeline_default_evals

    pipeline_id = uuid.uuid4()

    session = _make_session(_scalar_result(None))
    result = await has_pipeline_default_evals(session, pipeline_id)

    assert result is False

    call_args = session.execute.call_args
    stmt = call_args[0][0]
    where_clause = str(stmt.whereclause)
    assert "deleted_at" in where_clause, f"Expected 'deleted_at' in WHERE clause but got: {where_clause}"
