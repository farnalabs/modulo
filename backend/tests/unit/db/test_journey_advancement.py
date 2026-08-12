"""Journey advancement service tests (FAR-143 part 2).

These tests exercise the REAL ``advance_journeys`` path against an in-memory
SQLite database (no mocks of the function under test), mirroring the session
setup in ``test_journey_create_run.py``:

  * first terminal creates the journey row with evidence + run_count=1
  * newer completed_at overwrites evidence; older completed_at keeps it but
    still increments run_count
  * cancelled / stalled / replay / variant never advance
  * failed is a terminal advancing status
  * awaiting_human sets latest_status without counting and without moving the
    stage (unless at a map-stage pipeline)
  * stage identity resolves only for map-stage pipelines
  * refs are canonicalised (``#123`` vs ``123`` land in the same row)
"""

import uuid
from collections.abc import AsyncGenerator
from datetime import datetime
from typing import Any, cast

import pytest
from sqlalchemy import Table, select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine

from modulo.core.lifecycle_map.advancement import advance_journeys
from modulo.db.lifecycle_refs import canonical_work_item_id
from modulo.db.models.base import Base
from modulo.db.models.journey import Journey
from modulo.db.models.lifecycle_map_stage import LifecycleMapStage

_ORG = uuid.UUID("00000000-0000-0000-0000-000000000001")
_PIPELINE = uuid.UUID("00000000-0000-0000-0000-0000000000a1")
_MAP_PIPELINE = uuid.UUID("00000000-0000-0000-0000-0000000000a2")
_MAP_ID = uuid.UUID("00000000-0000-0000-0000-0000000000c1")
_ACCOUNT = uuid.UUID("00000000-0000-0000-0000-0000000000d1")

_REFS = [{"kind": "pr", "ref": "#123", "source": "derived"}]

# Controlled evidence timestamps (UTC, ascending) — no dependence on wall clock.
# Naive UTC so equality against SQLite's returned datetimes holds.
_T0 = datetime(2026, 1, 1, 0, 0, 0)
_T1 = datetime(2026, 1, 2, 0, 0, 0)
_T2 = datetime(2026, 1, 3, 0, 0, 0)
_T3 = datetime(2026, 1, 4, 0, 0, 0)

_TABLES: list[Table] = cast(
    list[Table],
    [Journey.__table__, LifecycleMapStage.__table__],
)


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


async def _seed_stage(session: AsyncSession, *, pipeline_id: uuid.UUID = _MAP_PIPELINE) -> None:
    session.add(
        LifecycleMapStage(
            organisation_id=_ORG,
            account_id=_ACCOUNT,
            map_id=_MAP_ID,
            version=1,
            stage_id="build",
            stage_name="Build",
            position=1,
            stage_type="modulo",
            pipeline_id=pipeline_id,
        )
    )
    await session.flush()


async def _seed_journey(
    session: AsyncSession,
    *,
    kind: str = "pr",
    ref: str = "123",
    run_count: int = 1,
    latest_terminal_run_id: uuid.UUID | None = None,
    latest_status: str | None = "complete",
    latest_provenance: str | None = "derived",
    updated_at: datetime = _T1,
    stage_identity: bool = False,
) -> Journey:
    journey = Journey(
        organisation_id=_ORG,
        kind=kind,
        ref=ref,
        canonical_work_item_id=canonical_work_item_id(_ORG, kind, ref),
        run_count=run_count,
        latest_terminal_run_id=latest_terminal_run_id,
        latest_status=latest_status,
        latest_provenance=latest_provenance,
        created_at=_T0,
        updated_at=updated_at,
    )
    if stage_identity:
        journey.map_id = _MAP_ID
        journey.map_version = 1
        journey.stage_id = "review"
        journey.stage_name = "Review"
        journey.position = 2
    session.add(journey)
    await session.flush()
    return journey


async def _advance(
    session: AsyncSession,
    *,
    refs: list[dict[str, Any]] | None = None,
    run_id: uuid.UUID | None = None,
    pipeline_id: uuid.UUID = _PIPELINE,
    status: str = "complete",
    completed_at: datetime | None = _T2,
    run_created_at: datetime = _T1,
    is_replay: bool = False,
    variant_group_id: uuid.UUID | None = None,
) -> int:
    return await advance_journeys(
        session,
        _ORG,
        run_id=run_id or uuid.uuid4(),
        pipeline_id=pipeline_id,
        refs=_REFS if refs is None else refs,
        status=status,
        completed_at=completed_at,
        run_created_at=run_created_at,
        is_replay=is_replay,
        variant_group_id=variant_group_id,
    )


async def _read_journey(session: AsyncSession, kind: str = "pr", ref: str = "123") -> Journey | None:
    # The raw-SQL advance updates the DB directly; expire the ORM identity map
    # so the reload reflects the advanced values, not the seeded object.
    session.expire_all()
    return (
        await session.execute(
            select(Journey).where(
                Journey.organisation_id == _ORG,
                Journey.kind == kind,
                Journey.ref == ref,
            )
        )
    ).scalar_one_or_none()


class TestFirstTerminal:
    async def test_first_terminal_creates_journey_with_evidence(self, session: AsyncSession) -> None:
        run_id = uuid.uuid4()
        advanced = await _advance(session, run_id=run_id, status="complete", completed_at=_T1)
        assert advanced == 1

        journey = await _read_journey(session)
        assert journey is not None
        assert journey.latest_terminal_run_id == run_id
        assert journey.latest_status == "complete"
        assert journey.latest_provenance == "derived"
        assert journey.run_count == 1
        assert journey.updated_at == _T1


class TestCompareAndSet:
    async def test_newer_terminal_overwrites_evidence_and_counts(self, session: AsyncSession) -> None:
        first = uuid.uuid4()
        await _seed_journey(session, latest_terminal_run_id=first, run_count=1, updated_at=_T1)

        second = uuid.uuid4()
        advanced = await _advance(session, run_id=second, status="complete", completed_at=_T2)
        assert advanced == 1

        journey = await _read_journey(session)
        assert journey is not None
        assert journey.latest_terminal_run_id == second
        assert journey.latest_status == "complete"
        assert journey.run_count == 2
        assert journey.updated_at == _T2

    async def test_older_terminal_increments_count_but_keeps_evidence(self, session: AsyncSession) -> None:
        first = uuid.uuid4()
        await _seed_journey(session, latest_terminal_run_id=first, run_count=1, updated_at=_T2)

        older_run = uuid.uuid4()
        advanced = await _advance(session, run_id=older_run, status="complete", completed_at=_T1)
        assert advanced == 1

        journey = await _read_journey(session)
        assert journey is not None
        assert journey.latest_terminal_run_id == first
        assert journey.latest_status == "complete"
        assert journey.run_count == 2
        assert journey.updated_at == _T2


class TestNonAdvancing:
    @pytest.mark.parametrize("status", ["cancelled", "stalled"])
    async def test_cancelled_or_stalled_does_not_advance(self, session: AsyncSession, status: str) -> None:
        first = uuid.uuid4()
        await _seed_journey(session, latest_terminal_run_id=first, run_count=1, updated_at=_T1)

        advanced = await _advance(session, status=status, completed_at=_T2)
        assert advanced == 0

        journey = await _read_journey(session)
        assert journey is not None
        assert journey.latest_terminal_run_id == first
        assert journey.latest_status == "complete"
        assert journey.run_count == 1
        assert journey.updated_at == _T1

    @pytest.mark.parametrize("status", ["cancelled", "stalled"])
    async def test_cancelled_or_stalled_mints_missing_journey_without_evidence(
        self, session: AsyncSession, status: str
    ) -> None:
        advanced = await _advance(session, status=status, completed_at=_T2)
        assert advanced == 0

        journey = await _read_journey(session)
        assert journey is not None
        assert journey.latest_terminal_run_id is None
        assert journey.latest_status is None
        assert journey.run_count == 0

    async def test_replay_does_not_advance(self, session: AsyncSession) -> None:
        first = uuid.uuid4()
        await _seed_journey(session, latest_terminal_run_id=first, run_count=1, updated_at=_T1)

        advanced = await _advance(session, run_id=uuid.uuid4(), is_replay=True, completed_at=_T2)
        assert advanced == 0

        journey = await _read_journey(session)
        assert journey is not None
        assert journey.latest_terminal_run_id == first
        assert journey.run_count == 1

    async def test_variant_does_not_advance(self, session: AsyncSession) -> None:
        first = uuid.uuid4()
        await _seed_journey(session, latest_terminal_run_id=first, run_count=1, updated_at=_T1)

        advanced = await _advance(session, run_id=uuid.uuid4(), variant_group_id=uuid.uuid4(), completed_at=_T2)
        assert advanced == 0

        journey = await _read_journey(session)
        assert journey is not None
        assert journey.latest_terminal_run_id == first
        assert journey.run_count == 1


class TestTerminalAdvancingStatuses:
    async def test_failed_advances(self, session: AsyncSession) -> None:
        first = uuid.uuid4()
        await _seed_journey(session, latest_terminal_run_id=first, run_count=1, updated_at=_T1)

        failed_run = uuid.uuid4()
        advanced = await _advance(session, run_id=failed_run, status="failed", completed_at=_T2)
        assert advanced == 1

        journey = await _read_journey(session)
        assert journey is not None
        assert journey.latest_terminal_run_id == failed_run
        assert journey.latest_status == "failed"
        assert journey.run_count == 2

    async def test_eval_failed_advances(self, session: AsyncSession) -> None:
        await _seed_journey(session, latest_terminal_run_id=uuid.uuid4(), run_count=1, updated_at=_T1)

        eval_failed_run = uuid.uuid4()
        advanced = await _advance(session, run_id=eval_failed_run, status="eval_failed", completed_at=_T2)
        assert advanced == 1

        journey = await _read_journey(session)
        assert journey is not None
        assert journey.latest_status == "eval_failed"
        assert journey.run_count == 2


class TestAwaitingHuman:
    async def test_awaiting_human_sets_status_without_count_or_stage(self, session: AsyncSession) -> None:
        first = uuid.uuid4()
        await _seed_journey(
            session,
            latest_terminal_run_id=first,
            run_count=1,
            latest_status="complete",
            updated_at=_T1,
            stage_identity=True,
        )

        awaiting_run = uuid.uuid4()
        advanced = await _advance(
            session,
            run_id=awaiting_run,
            status="awaiting_human",
            completed_at=None,
            run_created_at=_T2,
        )
        assert advanced == 1

        journey = await _read_journey(session)
        assert journey is not None
        assert journey.latest_terminal_run_id == awaiting_run
        assert journey.latest_status == "awaiting_human"
        assert journey.run_count == 1
        # Non-map-stage pipeline: stage identity is NOT advanced.
        assert journey.stage_id == "review"
        assert journey.map_id == _MAP_ID

    async def test_awaiting_human_at_map_stage_advances_stage(self, session: AsyncSession) -> None:
        await _seed_stage(session)
        await _seed_journey(session, latest_terminal_run_id=uuid.uuid4(), run_count=1, updated_at=_T1)

        awaiting_run = uuid.uuid4()
        advanced = await _advance(
            session,
            run_id=awaiting_run,
            pipeline_id=_MAP_PIPELINE,
            status="awaiting_human",
            completed_at=None,
            run_created_at=_T2,
        )
        assert advanced == 1

        journey = await _read_journey(session)
        assert journey is not None
        assert journey.latest_status == "awaiting_human"
        assert journey.map_id == _MAP_ID
        assert journey.map_version == 1
        assert journey.stage_id == "build"
        assert journey.stage_name == "Build"
        assert journey.position == 1


class TestStageResolution:
    async def test_map_stage_pipeline_sets_stage_identity(self, session: AsyncSession) -> None:
        await _seed_stage(session)
        await _seed_journey(session, latest_terminal_run_id=uuid.uuid4(), run_count=1, updated_at=_T1)

        run_id = uuid.uuid4()
        advanced = await _advance(
            session, run_id=run_id, pipeline_id=_MAP_PIPELINE, status="complete", completed_at=_T2
        )
        assert advanced == 1

        journey = await _read_journey(session)
        assert journey is not None
        assert journey.latest_terminal_run_id == run_id
        assert journey.map_id == _MAP_ID
        assert journey.map_version == 1
        assert journey.stage_id == "build"
        assert journey.stage_name == "Build"
        assert journey.position == 1

    async def test_non_map_pipeline_keeps_stage_columns(self, session: AsyncSession) -> None:
        first = uuid.uuid4()
        await _seed_journey(
            session,
            latest_terminal_run_id=first,
            run_count=1,
            updated_at=_T1,
            stage_identity=True,
        )

        run_id = uuid.uuid4()
        advanced = await _advance(session, run_id=run_id, pipeline_id=_PIPELINE, status="complete", completed_at=_T2)
        assert advanced == 1

        journey = await _read_journey(session)
        assert journey is not None
        assert journey.latest_terminal_run_id == run_id
        assert journey.latest_status == "complete"
        # Stage identity is preserved for a non-map-stage pipeline.
        assert journey.map_id == _MAP_ID
        assert journey.stage_id == "review"
        assert journey.stage_name == "Review"
        assert journey.position == 2


class TestRefCanonicalisation:
    async def test_hash_prefixed_and_bare_refs_land_in_same_journey(self, session: AsyncSession) -> None:
        run_a = uuid.uuid4()
        await _advance(
            session,
            run_id=run_a,
            refs=[{"kind": "pr", "ref": "#123", "source": "derived"}],
            status="complete",
            completed_at=_T1,
        )
        run_b = uuid.uuid4()
        await _advance(
            session,
            run_id=run_b,
            refs=[{"kind": "pr", "ref": "123", "source": "derived"}],
            status="complete",
            completed_at=_T2,
        )

        journey = await _read_journey(session)
        assert journey is not None
        assert journey.latest_terminal_run_id == run_b
        assert journey.run_count == 2

        rows = (
            (
                await session.execute(
                    select(Journey).where(
                        Journey.organisation_id == _ORG,
                        Journey.kind == "pr",
                    )
                )
            )
            .scalars()
            .all()
        )
        assert len(rows) == 1

    async def test_duplicate_canonical_refs_within_one_call_count_once(self, session: AsyncSession) -> None:
        run_id = uuid.uuid4()
        advanced = await _advance(
            session,
            run_id=run_id,
            refs=[
                {"kind": "pr", "ref": "#123", "source": "derived"},
                {"kind": "pr", "ref": "123", "source": "derived"},
            ],
            status="complete",
            completed_at=_T1,
        )
        assert advanced == 1

        journey = await _read_journey(session)
        assert journey is not None
        assert journey.run_count == 1

    async def test_malformed_ref_is_dropped_fail_open(self, session: AsyncSession) -> None:
        run_id = uuid.uuid4()
        advanced = await _advance(
            session,
            run_id=run_id,
            refs=[
                {"kind": "pr", "ref": "#123", "source": "derived"},
                {"kind": "", "ref": "broken"},
                "not-a-dict",
            ],
            status="complete",
            completed_at=_T1,
        )
        assert advanced == 1

        journey = await _read_journey(session)
        assert journey is not None
        assert journey.latest_terminal_run_id == run_id
        assert journey.run_count == 1

    async def test_empty_refs_advances_nothing(self, session: AsyncSession) -> None:
        advanced = await _advance(session, refs=[])
        assert advanced == 0
        rows = (await session.execute(select(Journey))).scalars().all()
        assert rows == []
