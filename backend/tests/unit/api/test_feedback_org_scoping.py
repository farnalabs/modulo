"""Unit tests for explicit org scoping in feedback route queries (FAR-897 / #330).

Each helper query must carry an explicit ``organisation_id`` predicate in its
SQL — relying on RLS alone is not acceptable — so the tests compile the emitted
statements and inspect the WHERE clauses, not just the returned rows.
"""

import uuid
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from modulo.api.routes.feedback import (
    _build_node_name_map,
    _load_eval_suite,
    _resolve_pipeline_name,
    _resolve_producing_node_uuid,
    _resolve_publish_context,
)
from modulo.core.feedback_manager import FeedbackManager

_ORG_ID = uuid.UUID("00000000-0000-0000-0000-000000000001")
_RUN_ID = uuid.uuid4()
_SNAPSHOT_ID = uuid.uuid4()
_RECORD_ID = uuid.uuid4()


class _CapturingSession:
    """Session stand-in that records every executed statement."""

    def __init__(self) -> None:
        self.statements: list[Any] = []


def _compiled_sql(stmt: Any) -> str:
    return str(stmt.compile())


def _scalar_result(value: Any) -> MagicMock:
    result = MagicMock()
    result.scalar_one_or_none.return_value = value
    return result


@pytest.mark.asyncio
async def test_build_node_name_map_queries_carry_org_predicates() -> None:
    session = _CapturingSession()
    run_row = MagicMock()
    run_row.snapshot_id = _SNAPSHOT_ID
    run_rows = MagicMock()
    run_rows.all.return_value = [run_row]
    snap_rows = MagicMock()
    snap_rows.all.return_value = []

    async def side_effect(stmt: Any, *args: Any, **kwargs: Any) -> MagicMock:
        session.statements.append(stmt)
        return run_rows if len(session.statements) == 1 else snap_rows

    session.execute = AsyncMock(side_effect=side_effect)  # type: ignore[method-assign]
    item = MagicMock()
    item.run_id = _RUN_ID

    names = await _build_node_name_map(session, [item], _ORG_ID)

    assert not names  # no snapshot rows -> empty map
    run_sql = _compiled_sql(session.statements[0])
    assert "runs.organisation_id" in run_sql
    assert "runs.id" in run_sql
    snap_sql = _compiled_sql(session.statements[1])
    assert "pipeline_snapshots.organisation_id" in snap_sql
    assert "pipeline_snapshots.id" in snap_sql


@pytest.mark.asyncio
async def test_resolve_producing_node_uuid_snapshot_query_carries_org_predicate() -> None:
    session = _CapturingSession()
    record = MagicMock()
    record.producing_node_id = "node-not-a-uuid"  # forces the snapshot lookup
    run = MagicMock()
    run.snapshot_id = _SNAPSHOT_ID
    snap_result = _scalar_result(None)
    session.execute = AsyncMock(side_effect=lambda stmt, *a, **k: _record_and_return(session, stmt, snap_result))  # type: ignore[method-assign]

    outcome = await _resolve_producing_node_uuid(session, record, run)

    assert outcome is None
    assert "pipeline_snapshots.organisation_id" in _compiled_sql(session.statements[0])


def _record_and_return(session: _CapturingSession, stmt: Any, result: MagicMock) -> MagicMock:
    session.statements.append(stmt)
    return result


@pytest.mark.asyncio
async def test_resolve_publish_context_run_query_carries_org_predicate() -> None:
    session = _CapturingSession()
    mgr = MagicMock(spec=FeedbackManager)
    record = MagicMock()
    record.eval_gap = True
    record.feedback_status = "pending"
    record.run_id = _RUN_ID
    mgr.get_feedback_record = AsyncMock(return_value=record)

    run = MagicMock()
    run.snapshot_id = None
    run_result = _scalar_result(run)
    session.execute = AsyncMock(side_effect=lambda stmt, *a, **k: _record_and_return(session, stmt, run_result))  # type: ignore[method-assign]

    result_run, node_id = await _resolve_publish_context(mgr, session, _RECORD_ID, uuid.uuid4(), _ORG_ID)

    assert result_run is run
    assert node_id is not None
    run_sql = _compiled_sql(session.statements[-1])
    assert "runs.organisation_id" in run_sql
    assert "runs.id" in run_sql


@pytest.mark.asyncio
async def test_load_eval_suite_run_query_carries_org_predicate() -> None:
    session = _CapturingSession()
    record = MagicMock()
    record.run_id = _RUN_ID

    run = MagicMock()
    run.pipeline_id = uuid.uuid4()
    empty_scalars = MagicMock()
    empty_scalars.all.return_value = []
    eval_result = MagicMock()
    eval_result.scalars.return_value = empty_scalars

    async def side_effect(stmt: Any, *args: Any, **kwargs: Any) -> MagicMock:
        session.statements.append(stmt)
        return _scalar_result(run) if len(session.statements) == 1 else eval_result

    session.execute = AsyncMock(side_effect=side_effect)  # type: ignore[method-assign]

    suite = await _load_eval_suite(session, record, _ORG_ID)

    assert not suite
    run_sql = _compiled_sql(session.statements[0])
    assert "runs.organisation_id" in run_sql
    assert "runs.id" in run_sql


@pytest.mark.asyncio
async def test_resolve_pipeline_name_run_query_carries_org_predicate() -> None:
    session = _CapturingSession()
    record = MagicMock()
    record.run_id = _RUN_ID

    run = MagicMock()
    run.pipeline_id = uuid.uuid4()
    pipeline = MagicMock()
    pipeline.name = "demo pipeline"
    pipeline_get = AsyncMock(return_value=pipeline)
    session.get = pipeline_get  # type: ignore[method-assign]

    async def side_effect(stmt: Any, *args: Any, **kwargs: Any) -> MagicMock:
        session.statements.append(stmt)
        return _scalar_result(run)

    session.execute = AsyncMock(side_effect=side_effect)  # type: ignore[method-assign]

    name = await _resolve_pipeline_name(session, record, _ORG_ID)

    assert name == "demo pipeline"
    run_sql = _compiled_sql(session.statements[0])
    assert "runs.organisation_id" in run_sql
    assert "runs.id" in run_sql
