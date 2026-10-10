"""Unit tests for the decision-record reconciliation scanner (FAR-1108 chunk 8b).

Runs against an in-memory SQLite database (the SQL is dialect-independent), with
stub ``eval_results`` / ``runs`` tables so orphan detection can be exercised
without materialising the full referenced schema.  Proves each anomaly class is
detected, that a clean table reports zero anomalies, that the scan is read-only,
and that the per-org scope filters.
"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import Column, MetaData, Table, Uuid, func, select, text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, create_async_engine

import modulo.db.models  # noqa: F401  (register metadata)
from modulo.core.eval_engine.decision_reconcile import (
    ANOMALY_DUPLICATE_EVENT,
    ANOMALY_DUPLICATE_UNBOUNDED_EVENT,
    ANOMALY_MISSING_EVAL_RESULT_ID,
    ANOMALY_ORPHANED_EVAL_RESULT,
    ANOMALY_ORPHANED_RUN,
    reconcile_decision_records,
)
from modulo.db.models.base import Base
from modulo.db.models.policy_gate_decision import PolicyGateDecision

_PERMANENT_INDEX = "uq_policy_gate_decisions_eval_result_id"


_STUB_META = MetaData()
_EVAL_RESULTS_STUB = Table("eval_results", _STUB_META, Column("id", Uuid, primary_key=True))
_RUNS_STUB = Table("runs", _STUB_META, Column("id", Uuid, primary_key=True))


async def _make_session() -> tuple[AsyncEngine, AsyncSession]:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(lambda c: Base.metadata.create_all(c, tables=[PolicyGateDecision.__table__]))
        await conn.run_sync(_STUB_META.create_all)
    session = AsyncSession(engine)
    return engine, session


async def _add_decision(
    session: AsyncSession,
    *,
    org_id: uuid.UUID | None = None,
    gate_id: uuid.UUID | None = None,
    eval_id: uuid.UUID | None = None,
    run_id: uuid.UUID | None = None,
    eval_result_id: uuid.UUID | None = None,
    resolved_action: str = "continue",
) -> uuid.UUID:
    row = PolicyGateDecision(
        id=uuid.uuid4(),
        organisation_id=org_id or uuid.uuid4(),
        policy_gate_id=gate_id or uuid.uuid4(),
        eval_id=eval_id or uuid.uuid4(),
        resolved_action=resolved_action,
        run_id=run_id,
        eval_result_id=eval_result_id,
    )
    session.add(row)
    await session.flush()
    return row.id


async def _add_stub(session: AsyncSession, table_name: str, row_id: uuid.UUID) -> None:
    """Insert a row into a stub ``eval_results`` / ``runs`` table.

    ``table_name`` is one of two module-local literals (never user input). The
    ``Uuid`` column's bind processor is applied so the stored representation
    matches the one the scanner compares against.
    """
    table = _EVAL_RESULTS_STUB if table_name == "eval_results" else _RUNS_STUB
    await session.execute(table.insert().values(id=row_id))


@pytest.mark.asyncio
async def test_clean_table_reports_no_anomalies() -> None:
    engine, session = await _make_session()
    try:
        er = uuid.uuid4()
        run = uuid.uuid4()
        await _add_stub(session, "eval_results", er)
        await _add_stub(session, "runs", run)
        await _add_decision(session, eval_result_id=er, run_id=run)
        await session.flush()

        report = await reconcile_decision_records(session)
        assert report.scanned == 1
        assert report.total == 0
        assert report.counts_by_kind() == {}
    finally:
        await session.close()
        await engine.dispose()


@pytest.mark.asyncio
async def test_duplicate_event_rows_detected_when_index_dropped() -> None:
    """A hit here is the tripwire for a dropped/disabled unique index."""
    engine, session = await _make_session()
    try:
        # Drop the permanent partial unique index to simulate index loss.
        # Index name is a module constant, not user input.
        await session.execute(text(f'DROP INDEX "{_PERMANENT_INDEX}"'))
        er = uuid.uuid4()
        await _add_stub(session, "eval_results", er)
        await _add_decision(session, eval_result_id=er)
        await _add_decision(session, eval_result_id=er)
        await session.flush()

        report = await reconcile_decision_records(session)
        dups = [a for a in report.anomalies if a.kind == ANOMALY_DUPLICATE_EVENT]
        assert len(dups) == 1
        assert dups[0].detail["eval_result_id"] == str(er)
        assert dups[0].detail["row_count"] == 2
    finally:
        await session.close()
        await engine.dispose()


@pytest.mark.asyncio
async def test_duplicate_unbounded_event_rows_detected() -> None:
    engine, session = await _make_session()
    try:
        run = uuid.uuid4()
        gate = uuid.uuid4()
        await _add_decision(session, run_id=run, gate_id=gate, eval_result_id=None)
        await _add_decision(session, run_id=run, gate_id=gate, eval_result_id=None)
        await session.flush()

        report = await reconcile_decision_records(session)
        dups = [a for a in report.anomalies if a.kind == ANOMALY_DUPLICATE_UNBOUNDED_EVENT]
        assert len(dups) == 1
        assert dups[0].detail["policy_gate_id"] == str(gate)
        assert dups[0].detail["row_count"] == 2
    finally:
        await session.close()
        await engine.dispose()


@pytest.mark.asyncio
async def test_missing_eval_result_id_detected() -> None:
    engine, session = await _make_session()
    try:
        await _add_decision(session, eval_result_id=None, resolved_action="warn")
        await session.flush()

        report = await reconcile_decision_records(session)
        missing = [a for a in report.anomalies if a.kind == ANOMALY_MISSING_EVAL_RESULT_ID]
        assert len(missing) == 1
        assert missing[0].detail["resolved_action"] == "warn"
    finally:
        await session.close()
        await engine.dispose()


@pytest.mark.asyncio
async def test_orphaned_eval_result_detected() -> None:
    engine, session = await _make_session()
    try:
        missing_er = uuid.uuid4()  # not inserted into eval_results
        await _add_decision(session, eval_result_id=missing_er)
        await session.flush()

        report = await reconcile_decision_records(session)
        orphans = [a for a in report.anomalies if a.kind == ANOMALY_ORPHANED_EVAL_RESULT]
        assert len(orphans) == 1
        assert orphans[0].detail["eval_result_id"] == str(missing_er)
    finally:
        await session.close()
        await engine.dispose()


@pytest.mark.asyncio
async def test_orphaned_run_detected() -> None:
    engine, session = await _make_session()
    try:
        er = uuid.uuid4()
        missing_run = uuid.uuid4()  # not inserted into runs
        await _add_stub(session, "eval_results", er)
        await _add_decision(session, eval_result_id=er, run_id=missing_run)
        await session.flush()

        report = await reconcile_decision_records(session)
        orphans = [a for a in report.anomalies if a.kind == ANOMALY_ORPHANED_RUN]
        assert len(orphans) == 1
        assert orphans[0].detail["run_id"] == str(missing_run)
    finally:
        await session.close()
        await engine.dispose()


@pytest.mark.asyncio
async def test_scan_is_read_only() -> None:
    engine, session = await _make_session()
    try:
        er = uuid.uuid4()
        await _add_stub(session, "eval_results", er)
        await _add_decision(session, eval_result_id=er)
        await _add_decision(session, eval_result_id=None)
        await session.flush()

        before = (await session.execute(select(func.count()).select_from(PolicyGateDecision))).scalar()
        await reconcile_decision_records(session)
        after = (await session.execute(select(func.count()).select_from(PolicyGateDecision))).scalar()
        assert before == after
        assert before == 2
    finally:
        await session.close()
        await engine.dispose()


@pytest.mark.asyncio
async def test_org_scope_filters_anomalies() -> None:
    engine, session = await _make_session()
    try:
        org_a = uuid.uuid4()
        org_b = uuid.uuid4()
        await _add_decision(session, org_id=org_a, eval_result_id=None)
        await _add_decision(session, org_id=org_b, eval_result_id=None)
        await session.flush()

        report_a = await reconcile_decision_records(session, org_id=org_a)
        assert report_a.scanned == 1
        assert report_a.total == 1
        assert report_a.anomalies[0].kind == ANOMALY_MISSING_EVAL_RESULT_ID

        report_all = await reconcile_decision_records(session)
        assert report_all.scanned == 2
    finally:
        await session.close()
        await engine.dispose()
