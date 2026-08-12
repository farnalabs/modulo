"""Journey reconciliation sweep tests (FAR-143 part 4).

These tests exercise the REAL ``reconcile_journeys`` path against an in-memory
SQLite database (no mocks of the function under test), mirroring the session
setup in ``test_journey_advancement.py``:

  * a terminal run with a MISSING journey row → the sweep mints + advances it;
  * a STALE journey (older evidence than the run's completed_at) → the sweep
    re-advances it (evidence + run_count);
  * a CURRENT journey → no-op;
  * idempotent on re-run — a reconciled run is not advanced twice;
  * batch-bounded — ``batch_size`` candidates per pass, the remainder drains
    on the next pass;
  * only DRIFT refs are re-advanced — a current ref's ``run_count`` is never
    double-counted;
  * ``cancelled`` / ``stalled`` runs are mint-only: a stale row is NOT
    perpetual drift for them;
  * fail-open per run — a per-run advance failure is logged and the sweep
    continues.
"""

import uuid
from collections.abc import AsyncGenerator
from datetime import datetime
from typing import Any, cast

import pytest
from sqlalchemy import Table, select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine

from modulo.core.lifecycle_map.advancement import advance_journeys
from modulo.core.lifecycle_map.reconcile import reconcile_journeys
from modulo.db.lifecycle_refs import canonical_work_item_id
from modulo.db.models.base import Base
from modulo.db.models.journey import Journey
from modulo.db.models.lifecycle_map_stage import LifecycleMapStage
from modulo.db.models.run import Run
from modulo.db.models.run_daily_facts import JourneyFact

_ORG = uuid.UUID("00000000-0000-0000-0000-000000000001")
_PIPELINE = uuid.UUID("00000000-0000-0000-0000-0000000000a1")
_SNAPSHOT = uuid.UUID("00000000-0000-0000-0000-0000000000b1")

# Controlled evidence timestamps (naive UTC so equality against SQLite holds).
_T0 = datetime(2026, 1, 1, 0, 0, 0)
_T1 = datetime(2026, 1, 2, 0, 0, 0)
_T2 = datetime(2026, 1, 3, 0, 0, 0)
_T3 = datetime(2026, 1, 4, 0, 0, 0)
_T4 = datetime(2026, 1, 5, 0, 0, 0)

_TABLES: list[Table] = cast(
    list[Table],
    [Journey.__table__, JourneyFact.__table__, LifecycleMapStage.__table__, Run.__table__],
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


async def _seed_run(
    session: AsyncSession,
    *,
    refs: list[dict[str, Any]],
    status: str = "complete",
    completed_at: datetime = _T2,
    created_at: datetime = _T1,
) -> Run:
    run = Run(
        id=uuid.uuid4(),
        organisation_id=_ORG,
        pipeline_id=_PIPELINE,
        snapshot_id=_SNAPSHOT,
        trigger_type="manual",
        # Unique per run — runs.organisation_id + runs.run_number is UNIQUE.
        run_number=int(uuid.uuid4().int % 1_000_000_000),
        input_hash="x",
        langgraph_thread_id=f"{_ORG}:{uuid.uuid4()}",
        status=status,
        work_item_refs=refs,
        created_at=created_at,
        completed_at=completed_at,
        cancellation_requested=False,
        ledger_written=False,
    )
    session.add(run)
    await session.flush()
    return run


async def _seed_journey(
    session: AsyncSession,
    kind: str,
    ref: str,
    *,
    updated_at: datetime = _T1,
    run_count: int = 1,
    latest_terminal_run_id: uuid.UUID | None = None,
    latest_status: str | None = "complete",
) -> Journey:
    journey = Journey(
        organisation_id=_ORG,
        kind=kind,
        ref=ref,
        canonical_work_item_id=canonical_work_item_id(_ORG, kind, ref),
        run_count=run_count,
        latest_terminal_run_id=latest_terminal_run_id,
        latest_status=latest_status,
        latest_provenance="derived",
        created_at=_T0,
        updated_at=updated_at,
    )
    session.add(journey)
    await session.flush()
    return journey


async def _read_journey(session: AsyncSession, kind: str, ref: str) -> Journey | None:
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


async def _journey_count(session: AsyncSession) -> int:
    return len((await session.execute(select(Journey))).scalars().all())


class TestReconcileCreatesMissingJourney:
    async def test_missing_journey_is_minted_and_advanced(self, session: AsyncSession) -> None:
        run = await _seed_run(session, refs=[{"kind": "github_pr", "ref": "123", "source": "derived"}])
        run_id = run.id
        completed_at = run.completed_at
        assert await _read_journey(session, "github_pr", "123") is None

        advanced = await reconcile_journeys(session, batch_size=10)
        assert advanced == 1

        journey = await _read_journey(session, "github_pr", "123")
        assert journey is not None
        assert journey.latest_terminal_run_id == run_id
        assert journey.latest_status == "complete"
        assert journey.run_count == 1
        assert journey.updated_at == completed_at

    async def test_ref_is_canonicalised(self, session: AsyncSession) -> None:
        run = await _seed_run(
            session,
            refs=[{"kind": "github_pr", "ref": "#456", "source": "derived"}],
            completed_at=_T3,
        )
        run_id = run.id
        advanced = await reconcile_journeys(session, batch_size=10)
        assert advanced == 1

        # #456 canonicalises to 456 — the journey row is keyed canonically.
        journey = await _read_journey(session, "github_pr", "456")
        assert journey is not None
        assert journey.latest_terminal_run_id == run_id


class TestReconcileUpdatesStaleJourney:
    async def test_stale_journey_is_re_advanced(self, session: AsyncSession) -> None:
        older = uuid.uuid4()
        await _seed_journey(session, "github_pr", "123", updated_at=_T1, latest_terminal_run_id=older)
        run = await _seed_run(
            session,
            refs=[{"kind": "github_pr", "ref": "123", "source": "derived"}],
            completed_at=_T3,
        )
        run_id = run.id

        advanced = await reconcile_journeys(session, batch_size=10)
        assert advanced == 1

        journey = await _read_journey(session, "github_pr", "123")
        assert journey is not None
        assert journey.latest_terminal_run_id == run_id
        assert journey.latest_status == "complete"
        assert journey.run_count == 2
        assert journey.updated_at == _T3

    async def test_current_journey_is_noop(self, session: AsyncSession) -> None:
        newer = uuid.uuid4()
        await _seed_journey(session, "github_pr", "123", updated_at=_T4, latest_terminal_run_id=newer)
        await _seed_run(
            session,
            refs=[{"kind": "github_pr", "ref": "123", "source": "derived"}],
            completed_at=_T3,
        )

        advanced = await reconcile_journeys(session, batch_size=10)
        assert advanced == 0

        journey = await _read_journey(session, "github_pr", "123")
        assert journey is not None
        assert journey.latest_terminal_run_id == newer
        assert journey.run_count == 1
        assert journey.updated_at == _T4

    async def test_idempotent_on_rerun(self, session: AsyncSession) -> None:
        await _seed_run(session, refs=[{"kind": "github_pr", "ref": "123", "source": "derived"}])
        first = await reconcile_journeys(session, batch_size=10)
        assert first == 1

        second = await reconcile_journeys(session, batch_size=10)
        assert second == 0

        journey = await _read_journey(session, "github_pr", "123")
        assert journey is not None
        assert journey.run_count == 1
        assert journey.updated_at == _T2


class TestBatchBound:
    async def test_batch_limit_respected_across_passes(self, session: AsyncSession) -> None:
        # Distinct completed_at so the oldest-first ordering is deterministic
        # and the batch boundary drains oldest-first across passes.
        runs = [
            await _seed_run(
                session,
                refs=[{"kind": "github_pr", "ref": str(i), "source": "derived"}],
                created_at=_T0,
                completed_at=_T1,
            )
            for i in range(3)
        ]
        run_ids = {r.id for r in runs}
        refs = [str(i) for i in range(3)]
        # batch_size=2 with 3 drifted runs — the third drains on the next pass.
        first = await reconcile_journeys(session, batch_size=2)
        assert first == 2

        remaining = await reconcile_journeys(session, batch_size=2)
        assert remaining == 1

        assert await _journey_count(session) == 3
        for ref in refs:
            journey = await _read_journey(session, "github_pr", ref)
            assert journey is not None
            assert journey.latest_terminal_run_id in run_ids

        final = await reconcile_journeys(session, batch_size=2)
        assert final == 0


class TestAdvanceOnlyDriftRefs:
    async def test_current_ref_run_count_not_double_counted(self, session: AsyncSession) -> None:
        # #123 is current (evidence newer than the run); #456 is missing.
        current_run = uuid.uuid4()
        await _seed_journey(session, "github_pr", "123", updated_at=_T4, latest_terminal_run_id=current_run)
        run = await _seed_run(
            session,
            refs=[
                {"kind": "github_pr", "ref": "123", "source": "derived"},
                {"kind": "github_pr", "ref": "456", "source": "derived"},
            ],
            completed_at=_T3,
        )
        run_id = run.id

        advanced = await reconcile_journeys(session, batch_size=10)
        assert advanced == 1

        current = await _read_journey(session, "github_pr", "123")
        assert current is not None
        assert current.latest_terminal_run_id == current_run
        assert current.run_count == 1  # NOT double-counted by the sweep
        assert current.updated_at == _T4

        minted = await _read_journey(session, "github_pr", "456")
        assert minted is not None
        assert minted.latest_terminal_run_id == run_id
        assert minted.run_count == 1

    async def test_run_with_mixed_refs_converges_in_one_pass(self, session: AsyncSession) -> None:
        # #123 is current (evidence newer than the run); #456 is missing.
        await _seed_journey(session, "github_pr", "123", updated_at=_T2)
        run = await _seed_run(
            session,
            refs=[
                {"kind": "github_pr", "ref": "123", "source": "derived"},
                {"kind": "github_pr", "ref": "456", "source": "derived"},
            ],
            created_at=_T0,
            completed_at=_T1,
        )
        run_id = run.id
        assert await reconcile_journeys(session, batch_size=10) == 1
        # Both refs now current — a re-run is a complete no-op.
        assert await reconcile_journeys(session, batch_size=10) == 0
        assert (await _read_journey(session, "github_pr", "456")).latest_terminal_run_id == run_id


class TestNonAdvancingRuns:
    @pytest.mark.parametrize("status", ["cancelled", "stalled"])
    async def test_stale_journey_is_not_perpetual_drift_for_mint_only_runs(
        self, session: AsyncSession, status: str
    ) -> None:
        older = uuid.uuid4()
        await _seed_journey(session, "github_pr", "123", updated_at=_T1, latest_terminal_run_id=older)
        await _seed_run(
            session,
            refs=[{"kind": "github_pr", "ref": "123", "source": "derived"}],
            status=status,
            completed_at=_T3,
        )

        # The cancelled/stalled run can never move evidence, so a stale row is
        # NOT drift — the sweep must not loop forever on it.
        assert await reconcile_journeys(session, batch_size=10) == 0

        journey = await _read_journey(session, "github_pr", "123")
        assert journey is not None
        assert journey.latest_terminal_run_id == older
        assert journey.updated_at == _T1

    @pytest.mark.parametrize("status", ["cancelled", "stalled"])
    async def test_missing_journey_is_minted_for_mint_only_runs(self, session: AsyncSession, status: str) -> None:
        await _seed_run(
            session,
            refs=[{"kind": "github_pr", "ref": "123", "source": "derived"}],
            status=status,
            completed_at=_T3,
        )

        # The sweep mints the row (so it exists) without evidence or a count.
        assert await reconcile_journeys(session, batch_size=10) == 0

        journey = await _read_journey(session, "github_pr", "123")
        assert journey is not None
        assert journey.latest_terminal_run_id is None
        assert journey.latest_status is None
        assert journey.run_count == 0


class TestFailOpen:
    async def test_per_run_failure_does_not_abort_sweep(
        self, session: AsyncSession, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        await _seed_run(
            session,
            refs=[{"kind": "github_pr", "ref": "1", "source": "derived"}],
            completed_at=_T2,
        )
        failing_run = await _seed_run(
            session,
            refs=[{"kind": "github_pr", "ref": "2", "source": "derived"}],
            completed_at=_T2,
        )
        real_advance = advance_journeys

        async def _boom_for_one(*args: Any, **kwargs: Any) -> int:
            if kwargs.get("run_id") == failing_run.id:
                raise RuntimeError("journey advance exploded")
            return await real_advance(*args, **kwargs)

        monkeypatch.setattr("modulo.core.lifecycle_map.reconcile.advance_journeys", _boom_for_one)
        with caplog.at_level("ERROR", logger="modulo.core.lifecycle_map.reconcile"):
            advanced = await reconcile_journeys(session, batch_size=10)

        # One run advanced, one failed open — no exception escaped.
        assert advanced == 1
        assert any("journey_reconcile.advance_failed" in m for m in caplog.messages)
        assert await _read_journey(session, "github_pr", "1") is not None
        assert await _read_journey(session, "github_pr", "2") is None


class TestJourneyFactModel:
    """The per-writer denominator model is registered, org-scoped and queryable."""

    async def test_fact_row_round_trips(self, session: AsyncSession) -> None:
        run = await _seed_run(
            session,
            refs=[{"kind": "github_pr", "ref": "123", "source": "derived"}],
        )
        fact = JourneyFact(
            organisation_id=_ORG,
            run_id=run.id,
            writer="live",
            parse_failures=2,
            finalise_attempts=3,
        )
        session.add(fact)
        await session.flush()

        rows = (
            (
                await session.execute(
                    select(JourneyFact).where(
                        JourneyFact.organisation_id == _ORG,
                        JourneyFact.run_id == run.id,
                    )
                )
            )
            .scalars()
            .all()
        )
        assert len(rows) == 1
        assert rows[0].writer == "live"
        assert rows[0].parse_failures == 2
        assert rows[0].finalise_attempts == 3
        assert rows[0].created_at is not None

    async def test_fact_survives_run_purge(self, session: AsyncSession) -> None:
        run = await _seed_run(
            session,
            refs=[{"kind": "github_pr", "ref": "123", "source": "derived"}],
        )
        session.add(
            JourneyFact(
                organisation_id=_ORG,
                run_id=run.id,
                writer="early_return",
                parse_failures=0,
                finalise_attempts=1,
            )
        )
        await session.flush()
        await session.delete(run)
        await session.flush()

        # run_id is deliberately NOT a FK — the fact survives the run purge.
        rows = (
            (
                await session.execute(
                    select(JourneyFact).where(
                        JourneyFact.organisation_id == _ORG,
                        JourneyFact.writer == "early_return",
                    )
                )
            )
            .scalars()
            .all()
        )
        assert len(rows) == 1
