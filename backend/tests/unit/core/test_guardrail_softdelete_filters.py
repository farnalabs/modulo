"""FAR-1009: soft-deleted EvalDefinitions must be excluded from guardrail runtime loaders.

Covers:
  - ``pre_guardrail._load_guardrail_definitions`` (pipeline-guardrail load)
  - ``conformance.load_node_guardrails`` (per-node guardrail load)
  - ``conformance.load_claimed_guardrails`` (run-start claim discovery)

Each test asserts the emitted WHERE clause contains ``deleted_at`` so a future
regression that drops the filter is caught by the test-style scanner and by the
WHERE-clause assertion. Tests use mocked sessions — no DB required.

Without the FAR-1009 filter the WHERE clause omits ``deleted_at`` and the test
fails on the assertion.
"""

from __future__ import annotations

import uuid
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
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
    result = MagicMock()
    scalars_mock = MagicMock()
    scalars_mock.all.return_value = rows
    result.scalars.return_value = scalars_mock
    return result


# ---------------------------------------------------------------------------
# pre_guardrail._load_guardrail_definitions
# ---------------------------------------------------------------------------


async def test_pre_guardrail_load_includes_deleted_at_filter():
    """_load_guardrail_definitions must add deleted_at IS NULL to the WHERE."""
    from modulo.core.trigger_engine.pre_guardrail import _load_guardrail_definitions

    pipeline_id = uuid.uuid4()
    org_id = uuid.uuid4()

    session = _make_session(_scalars_result([]))
    await _load_guardrail_definitions(session, org_id=org_id, pipeline_id=pipeline_id)

    call_args = session.execute.call_args
    stmt = call_args[0][0]
    where_clause = str(stmt.whereclause)
    assert "deleted_at" in where_clause, f"Expected 'deleted_at' in WHERE clause but got: {where_clause}"


# ---------------------------------------------------------------------------
# conformance.load_node_guardrails
# ---------------------------------------------------------------------------


async def test_load_node_guardrails_includes_deleted_at_filter():
    """load_node_guardrails must add deleted_at IS NULL to the WHERE."""
    from modulo.core.guardrails.conformance import load_node_guardrails

    pipeline_id = uuid.uuid4()
    org_id = uuid.uuid4()

    session = _make_session(_scalars_result([]))
    await load_node_guardrails(session, org_id=org_id, pipeline_id=pipeline_id, node_id="node-1")

    call_args = session.execute.call_args
    stmt = call_args[0][0]
    where_clause = str(stmt.whereclause)
    assert "deleted_at" in where_clause, f"Expected 'deleted_at' in WHERE clause but got: {where_clause}"


# ---------------------------------------------------------------------------
# conformance.load_claimed_guardrails
# ---------------------------------------------------------------------------


async def test_load_claimed_guardrails_includes_deleted_at_filter():
    """load_claimed_guardrails must add deleted_at IS NULL to the WHERE."""
    import modulo.core.guardrails.conformance as mod

    pipeline_id = uuid.uuid4()
    org_id = uuid.uuid4()

    async def _noop_rls(session: Any, org_id: uuid.UUID) -> None:
        return None

    session = _make_session(_scalars_result([]))
    session.begin = MagicMock(return_value=session)

    factory = MagicMock()
    factory.return_value = session
    factory.return_value.__aenter__ = AsyncMock(return_value=session)
    factory.return_value.__aexit__ = AsyncMock(return_value=None)

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(mod, "_set_rls", _noop_rls)
        _claimed, load_failed = await mod.load_claimed_guardrails(factory, org_id=org_id, pipeline_id=pipeline_id)

    assert load_failed is False

    call_args = session.execute.call_args
    stmt = call_args[0][0]
    where_clause = str(stmt.whereclause)
    assert "deleted_at" in where_clause, f"Expected 'deleted_at' in WHERE clause but got: {where_clause}"
