"""FAR-1530: pause / resume / unarchive-implies-paused CRUD (real SQLite rows).

Drives the REAL CRUD functions against an in-memory SQLite database (no mocks
of the functions under test), so the state-machine rules are proven against
the actual ``ck_pipelines_run_enabled`` CHECK as well:

  * pause writes ``run_enabled=false, reason='operator', at=now``,
  * pause on an already-disabled pipeline is an IDEMPOTENT NO-OP — FIRST CAUSE
    OWNS THE REASON (a circuit-breaker pause keeps ``'circuit_breaker'``);
    without the guard this test FAILS (the operator cause overwrites it),
  * resume clears the unified state,
  * resume on a running pipeline is an idempotent no-op,
  * unarchive restores to PAUSED, not Active (the ratified FAR-1530 taxonomy:
    a dormant pipeline must not surprise-fire); without the fold this test
    FAILS (the pipeline would come back ``run_enabled=true``),
  * unarchive keeps an already-present cause (first cause owns the reason),
  * missing pipeline -> ``None`` on every function (the routes map it to 404).

The resume-while-tripped 409 and the pause/resume ROUTE wiring live in
``tests/unit/api/test_pipelines_routes_coverage.py``; the circuit-breaker
fold (trip/reset) lives in ``tests/unit/core/cost_controller/test_circuit_breaker.py``.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncGenerator
from datetime import UTC, datetime

import pytest
from sqlalchemy import Table, select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine

from modulo.db.crud.pipeline import archive_pipeline, pause_pipeline, resume_pipeline, unarchive_pipeline
from modulo.db.models.base import Base
from modulo.db.models.organisation import Organisation
from modulo.db.models.pipeline import Pipeline

_ORG = uuid.UUID("00000000-0000-0000-0000-000000000001")
_PIPELINE = uuid.UUID("00000000-0000-0000-0000-0000000000a1")

_TABLES: list[Table] = [Organisation.__table__, Pipeline.__table__]


@pytest.fixture
async def engine() -> AsyncGenerator[AsyncEngine, None]:
    eng = create_async_engine("sqlite+aiosqlite://", echo=False)
    async with eng.begin() as conn:
        await conn.run_sync(lambda sync_conn: Base.metadata.create_all(sync_conn, tables=_TABLES))
        await conn.exec_driver_sql("PRAGMA foreign_keys = OFF")
    yield eng
    await eng.dispose()


@pytest.fixture
async def session(engine: AsyncEngine) -> AsyncGenerator[AsyncSession, None]:
    maker = async_sessionmaker(engine, expire_on_commit=False)
    async with maker() as s:
        yield s


async def _seed(
    session: AsyncSession,
    *,
    archived_at: datetime | None = None,
    run_enabled: bool = True,
    run_disabled_reason: str | None = None,
    run_disabled_at: datetime | None = None,
) -> None:
    if (await session.execute(select(Organisation).where(Organisation.id == _ORG))).scalar_one_or_none() is None:
        session.add(Organisation(id=_ORG, name="test org", slug=f"test-{_ORG}"))
        await session.flush()
    session.add(
        Pipeline(
            id=_PIPELINE,
            organisation_id=_ORG,
            name="pipeline",
            account_id=_ORG,
            visibility="org",
            archived_at=archived_at,
            run_enabled=run_enabled,
            run_disabled_reason=run_disabled_reason,
            run_disabled_at=run_disabled_at,
        )
    )
    await session.flush()


def _aware(value: datetime | None) -> datetime | None:
    """SQLite returns naive datetimes (the UTC offset is not round-tripped);
    normalise so cause-timestamp equality is comparable."""
    if value is None or value.tzinfo is not None:
        return value
    return value.replace(tzinfo=UTC)


async def _row(session: AsyncSession) -> Pipeline:
    result = await session.execute(select(Pipeline).where(Pipeline.id == _PIPELINE))
    return result.scalar_one()


class TestPausePipeline:
    async def test_pause_disables_with_operator_cause(self, session: AsyncSession) -> None:
        await _seed(session)

        result = await pause_pipeline(session, _PIPELINE)

        assert result is not None
        assert result.run_enabled is False
        assert result.run_disabled_reason == "operator"
        assert result.run_disabled_at is not None

    async def test_pause_on_already_disabled_is_idempotent_first_cause_owns_reason(self, session: AsyncSession) -> None:
        """REGRESSION: an operator pause must NOT overwrite the breaker's cause.

        Without the first-cause guard in ``pause_pipeline`` this test FAILS:
        the reason flips to 'operator' and a later admin reset would then
        clear a pause the operator never set (or, conversely here, the
        breaker's audit trail is destroyed by a no-op pause).
        """
        tripped_at = datetime(2026, 10, 1, tzinfo=UTC)
        await _seed(
            session,
            run_enabled=False,
            run_disabled_reason="circuit_breaker",
            run_disabled_at=tripped_at,
        )

        result = await pause_pipeline(session, _PIPELINE)

        assert result is not None
        assert result.run_enabled is False
        assert result.run_disabled_reason == "circuit_breaker"
        assert _aware(result.run_disabled_at) == tripped_at

    async def test_pause_missing_pipeline_returns_none(self, session: AsyncSession) -> None:
        assert await pause_pipeline(session, uuid.uuid4()) is None

    async def test_pause_leaves_a_disabled_row_writable_under_the_check(self, session: AsyncSession) -> None:
        """The pause write satisfies ``ck_pipelines_run_enabled`` (cause +
        timestamp land in the SAME flush as the disable)."""
        await _seed(session)
        await pause_pipeline(session, _PIPELINE)
        # A second flush (e.g. a later write in the same transaction) must not
        # trip the CHECK either.
        row = await _row(session)
        row.name = "renamed"
        await session.flush()
        assert row.run_disabled_reason == "operator"


class TestResumePipeline:
    async def test_resume_clears_the_unified_state(self, session: AsyncSession) -> None:
        await _seed(
            session,
            run_enabled=False,
            run_disabled_reason="operator",
            run_disabled_at=datetime.now(UTC),
        )

        result = await resume_pipeline(session, _PIPELINE)

        assert result is not None
        assert result.run_enabled is True
        assert result.run_disabled_reason is None
        assert result.run_disabled_at is None

    async def test_resume_when_enabled_is_an_idempotent_noop(self, session: AsyncSession) -> None:
        await _seed(session)

        result = await resume_pipeline(session, _PIPELINE)

        assert result is not None
        assert result.run_enabled is True
        assert result.run_disabled_reason is None

    async def test_resume_missing_pipeline_returns_none(self, session: AsyncSession) -> None:
        assert await resume_pipeline(session, uuid.uuid4()) is None


class TestUnarchiveRestoresPaused:
    async def test_unarchive_restores_paused_not_active(self, session: AsyncSession) -> None:
        """REGRESSION (FAR-1530 taxonomy): unarchiving must NOT reactivate.

        Without the pause fold in ``unarchive_pipeline`` this test FAILS: the
        row comes back ``run_enabled=true`` and a dormant pipeline
        surprise-fires the moment it is unarchived.
        """
        await _seed(session, archived_at=datetime.now(UTC))

        result = await unarchive_pipeline(session, _PIPELINE)

        assert result is not None
        assert result.archived_at is None
        assert result.run_enabled is False
        assert result.run_disabled_reason == "operator"
        assert result.run_disabled_at is not None

    async def test_unarchive_keeps_an_already_present_cause(self, session: AsyncSession) -> None:
        """First cause owns the reason across the archive boundary too: a
        pipeline paused by the breaker BEFORE it was archived keeps
        ``'circuit_breaker'`` through unarchive (so the admin reset can still
        clear it)."""
        tripped_at = datetime(2026, 10, 1, tzinfo=UTC)
        await _seed(
            session,
            archived_at=datetime.now(UTC),
            run_enabled=False,
            run_disabled_reason="circuit_breaker",
            run_disabled_at=tripped_at,
        )

        result = await unarchive_pipeline(session, _PIPELINE)

        assert result is not None
        assert result.archived_at is None
        assert result.run_enabled is False
        assert result.run_disabled_reason == "circuit_breaker"
        assert _aware(result.run_disabled_at) == tripped_at

    async def test_archive_does_not_touch_the_run_state(self, session: AsyncSession) -> None:
        """Archiving a paused pipeline keeps the pause (archive is hidden-state
        only; the pause columns are the execution state)."""
        paused_at = datetime(2026, 10, 2, tzinfo=UTC)
        await _seed(
            session,
            run_enabled=False,
            run_disabled_reason="operator",
            run_disabled_at=paused_at,
        )

        result = await archive_pipeline(session, _PIPELINE)

        assert result is not None
        assert result.archived_at is not None
        assert result.run_enabled is False
        assert result.run_disabled_reason == "operator"
        assert _aware(result.run_disabled_at) == paused_at

    async def test_unarchive_missing_pipeline_returns_none(self, session: AsyncSession) -> None:
        assert await unarchive_pipeline(session, uuid.uuid4()) is None
