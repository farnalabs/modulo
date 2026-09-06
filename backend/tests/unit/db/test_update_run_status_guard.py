"""Unit tests for the ``update_run_status`` guarded write (qa F7).

``not_status`` turns the status write into a guarded transition: when the
row is CURRENTLY in *not_status*, the write is skipped (logged) instead of
applied. The claim-gate route uses it so a run parked by the park sweep
between a claimer's pre-read and the write can never be flipped to
``claimed`` — the guard is evaluated against the authoritative row state
(the FOR UPDATE-locked read), not a stale pre-read.

Real in-memory SQLite (same fixture pattern as
``test_executor_stalled_status.py``) for the ORM path; the fenced variant's
guard clause is pinned by a string assertion on the SQL text.
"""

from __future__ import annotations

import logging
import uuid

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from modulo.db.crud.run import _UPDATE_STATUS_FENCED_SQL, update_run_status
from modulo.db.models.base import Base, OrgScoped
from modulo.db.models.run import Run


@pytest.fixture
async def sqlite_runs_engine():
    eng = create_async_engine("sqlite+aiosqlite://", echo=False)
    async with eng.begin() as conn:
        await conn.run_sync(lambda sync_conn: Base.metadata.create_all(sync_conn, tables=[Run.__table__]))
    yield eng
    await eng.dispose()


def _run_row(
    run_id: uuid.UUID,
    org_id: uuid.UUID,
    *,
    status: str,
    claim_token: str | None = None,
) -> Run:
    return Run(
        id=run_id,
        organisation_id=org_id,
        pipeline_id=uuid.uuid4(),
        snapshot_id=uuid.uuid4(),
        trigger_type="manual",
        status=status,
        run_number=1,
        input_hash="a" * 64,
        langgraph_thread_id=f"thread-{run_id}",
        claim_token=claim_token,
    )


class TestGuardedStatusWrite:
    async def test_parked_run_is_not_flipped_to_claimed(
        self, sqlite_runs_engine, caplog: pytest.LogCaptureFixture
    ) -> None:
        org_id = uuid.uuid4()
        run_id = uuid.uuid4()
        factory = async_sessionmaker(sqlite_runs_engine, expire_on_commit=False)
        async with factory() as session, session.begin():
            session.add(_run_row(run_id, org_id, status="hitl_parked"))
            await session.flush()

        # The guard skip is LOUD (visible in logs) — caplog.at_level is a
        # sync context manager and must wrap the async block from outside.
        with caplog.at_level(logging.INFO):
            async with factory() as session, session.begin():
                result = await update_run_status(session, run_id, "claimed", not_status="hitl_parked")

        assert result is not None
        assert result.status == "hitl_parked"
        assert any("run_status.guard_skipped" in r.message for r in caplog.records)

    async def test_awaiting_human_run_still_transitions(self, sqlite_runs_engine) -> None:
        org_id = uuid.uuid4()
        run_id = uuid.uuid4()
        factory = async_sessionmaker(sqlite_runs_engine, expire_on_commit=False)
        async with factory() as session, session.begin():
            session.add(_run_row(run_id, org_id, status="awaiting_human"))
            await session.flush()

        async with factory() as session, session.begin():
            result = await update_run_status(session, run_id, "claimed", not_status="hitl_parked")

        assert result is not None
        assert result.status == "claimed"

    async def test_no_guard_keeps_unconditional_write(self, sqlite_runs_engine) -> None:
        """The param is optional — a plain write (the overwhelming majority of
        callers) is unchanged."""
        org_id = uuid.uuid4()
        run_id = uuid.uuid4()
        factory = async_sessionmaker(sqlite_runs_engine, expire_on_commit=False)
        async with factory() as session, session.begin():
            session.add(_run_row(run_id, org_id, status="pending"))
            await session.flush()

        async with factory() as session, session.begin():
            result = await update_run_status(session, run_id, "running")

        assert result is not None
        assert result.status == "running"
        assert result.started_at is not None


def test_fenced_path_carries_the_not_status_guard():
    """The fenced variant (claim-token writes) carries the SAME guard clause
    in its WHERE — a fenced claim write can never flip a parked run either."""
    sql = str(_UPDATE_STATUS_FENCED_SQL)
    assert "CAST(:not_status AS text) IS NULL OR status <> CAST(:not_status AS text)" in sql


def test_org_scoped_base_import_alive():
    """Guard against accidental module-level breakage of the models import
    graph the fixture above depends on (OrgScoped declares organisation_id)."""
    assert issubclass(Run, OrgScoped)
