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
    continues;
  * a run selected by the raw-ref drift predicate whose refs canonicalise to
    current journeys is NOT re-advanced (would double-count);
  * a run whose refs are all malformed is skipped;
  * ``asyncio.CancelledError`` is re-raised (never swallowed by fail-open);
  * the ``_drift_predicate`` SQL is dialect-correct (``jsonb_array_elements``
    + ``->>`` on Postgres, ``json_each`` + ``json_extract`` elsewhere);
  * the ``record_*`` metric counters lazy-init against the OTel meter and
    silently no-op when no meter is available.
"""

import asyncio
import uuid
from collections.abc import AsyncGenerator
from datetime import datetime
from typing import Any, cast

import pytest
from sqlalchemy import Table, select
from sqlalchemy.dialects import postgresql, sqlite
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine

import modulo.core.lifecycle_map.reconcile as reconcile_mod
from modulo.core.lifecycle_map.advancement import advance_journeys
from modulo.core.lifecycle_map.reconcile import (
    _canonical_refs,
    _drift_predicate,
    _drift_refs,
    reconcile_journeys,
)
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


class TestReconcileEdgeCases:
    async def test_cancelled_error_is_reraised(self, session: AsyncSession, monkeypatch: pytest.MonkeyPatch) -> None:
        # The fail-open guard must NOT swallow task cancellation — a
        # CancelledError has to propagate so the caller can unwind cleanly.
        await _seed_run(session, refs=[{"kind": "github_pr", "ref": "1", "source": "derived"}])

        async def _cancel(*args: Any, **kwargs: Any) -> int:
            raise asyncio.CancelledError

        monkeypatch.setattr("modulo.core.lifecycle_map.reconcile.advance_journeys", _cancel)
        with pytest.raises(asyncio.CancelledError):
            await reconcile_journeys(session, batch_size=10)

    async def test_malformed_refs_run_is_skipped(self, session: AsyncSession) -> None:
        # Every ref entry fails canonicalisation → no canonical refs → the run
        # is skipped (no mint, no advance, no error).
        await _seed_run(
            session,
            refs=[{"kind": "", "ref": "x"}, "not-a-dict", None],
            completed_at=_T2,
        )
        assert await reconcile_journeys(session, batch_size=10) == 0
        assert await _journey_count(session) == 0

    async def test_raw_ref_matching_current_canonical_journey_is_not_drift(self, session: AsyncSession) -> None:
        # The journey is keyed canonically ("456"); the run stores the RAW ref
        # "#456". The sweep's raw-ref drift predicate sees a join miss and
        # selects the run, but _drift_refs canonicalises FIRST and finds the
        # journey current — so the sweep must NOT re-advance, or run_count
        # would be double-counted.
        current_run = uuid.uuid4()
        await _seed_journey(
            session, "github_pr", "456", updated_at=_T3, run_count=1, latest_terminal_run_id=current_run
        )
        await _seed_run(
            session,
            refs=[{"kind": "github_pr", "ref": "#456", "source": "derived"}],
            completed_at=_T2,
        )

        assert await reconcile_journeys(session, batch_size=10) == 0

        journey = await _read_journey(session, "github_pr", "456")
        assert journey is not None
        assert journey.run_count == 1  # NOT double-counted
        assert journey.latest_terminal_run_id == current_run
        assert journey.updated_at == _T3

    async def test_drift_refs_empty_canonical_returns_empty(self, session: AsyncSession) -> None:
        assert not await _drift_refs(session, _ORG, [], None, advancing=True)
        assert not await _drift_refs(session, _ORG, [], _T2, advancing=False)


class TestAdvanceJourneysDirect:
    """Direct ``advance_journeys`` fail-open + dedupe guard coverage.

    The reconcile sweep pre-canonicalises refs via ``_canonical_refs``, so the
    malformed-entry and duplicate-ref guards inside ``advance_journeys`` itself
    are only reachable by calling it directly (its other consumers, e.g. the
    run finalise hook, pass raw entries).
    """

    async def test_malformed_entry_is_dropped_with_warning(
        self,
        session: AsyncSession,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        run_id = uuid.uuid4()
        with caplog.at_level("WARNING", logger="modulo.core.lifecycle_map.advancement"):
            advanced = await advance_journeys(
                session,
                _ORG,
                run_id=run_id,
                pipeline_id=None,
                refs=[
                    {"kind": "", "ref": "x", "source": "derived"},
                    {"kind": "github_pr", "ref": "123", "source": "derived"},
                ],
                status="complete",
                completed_at=_T2,
                run_created_at=_T1,
            )

        assert advanced == 1
        assert any("dropping invalid work-item ref entry" in m for m in caplog.messages)
        journey = await _read_journey(session, "github_pr", "123")
        assert journey is not None
        assert journey.latest_terminal_run_id == run_id
        assert journey.run_count == 1

    async def test_duplicate_canonical_refs_advanced_once(self, session: AsyncSession) -> None:
        run_id = uuid.uuid4()
        advanced = await advance_journeys(
            session,
            _ORG,
            run_id=run_id,
            pipeline_id=None,
            refs=[
                {"kind": "github_pr", "ref": "123", "source": "derived"},
                {"kind": "github_pr", "ref": "#123", "source": "derived"},
            ],
            status="complete",
            completed_at=_T2,
            run_created_at=_T1,
        )

        assert advanced == 1
        journey = await _read_journey(session, "github_pr", "123")
        assert journey is not None
        assert journey.latest_terminal_run_id == run_id
        assert journey.run_count == 1


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


class TestDriftPredicateDialects:
    def test_postgresql_branch_uses_jsonb(self) -> None:
        stmt = _drift_predicate("postgresql")
        sql = str(stmt.compile(dialect=postgresql.dialect()))
        assert "jsonb_array_elements" in sql
        assert "->>" in sql
        assert "json_each" not in sql

    def test_sqlite_branch_uses_json_each(self) -> None:
        stmt = _drift_predicate("sqlite")
        sql = str(stmt.compile(dialect=sqlite.dialect()))
        assert "json_each" in sql
        assert "json_extract" in sql
        assert "jsonb_array_elements" not in sql

    def test_sqlite_datetime_normalisation(self) -> None:
        # SQLite stores datetimes as text — the predicate must normalise both
        # sides with datetime() so an equal-instant evidence anchor never
        # re-selects a reconciled run.
        stmt = _drift_predicate("sqlite")
        sql = str(stmt.compile(dialect=sqlite.dialect()))
        assert "datetime(" in sql

    def test_unknown_dialect_falls_back_to_generic_json(self) -> None:
        stmt = _drift_predicate("mariadb")
        sql = str(stmt.compile(dialect=sqlite.dialect()))
        assert "json_each" in sql


class TestCanonicalRefs:
    def test_non_list_returns_empty(self) -> None:
        assert not _canonical_refs(None)
        assert not _canonical_refs("not-a-list")
        assert not _canonical_refs({"kind": "github_pr", "ref": "1"})

    def test_malformed_entries_dropped_with_warning(self, caplog: pytest.LogCaptureFixture) -> None:
        with caplog.at_level("WARNING", logger="modulo.core.lifecycle_map.reconcile"):
            result = _canonical_refs(
                [
                    {"kind": "", "ref": "1"},
                    "not-a-dict",
                    {"kind": "github_pr", "ref": ""},
                    {"kind": "github_pr", "ref": "123", "source": "derived"},
                ]
            )
        # Blank kind, non-dict, blank ref all dropped; the valid entry survives.
        assert result == [{"kind": "github_pr", "ref": "123", "source": "derived"}]
        assert len(caplog.messages) == 3

    def test_duplicates_deduped_after_canonicalisation(self) -> None:
        # "#1" and "1" canonicalise to the same ref → deduped; "#2" survives.
        result = _canonical_refs(
            [
                {"kind": "github_pr", "ref": "#1"},
                {"kind": "github_pr", "ref": "1"},
                {"kind": "github_pr", "ref": "#2"},
            ]
        )
        assert result == [
            {"kind": "github_pr", "ref": "1", "source": "derived"},
            {"kind": "github_pr", "ref": "2", "source": "derived"},
        ]


class _FakeCounter:
    def __init__(self, name: str) -> None:
        self.name = name
        self.calls: list[dict] = []

    def add(self, value: int, attributes: dict | None = None) -> None:
        self.calls.append({"value": value, "attributes": attributes})


class _FakeMeter:
    def __init__(self) -> None:
        self.counters: list[_FakeCounter] = []

    def create_counter(self, *, name: str, description: str, unit: str) -> _FakeCounter:
        counter = _FakeCounter(name)
        self.counters.append(counter)
        return counter

    def counter(self, name: str) -> _FakeCounter | None:
        return next((c for c in self.counters if c.name == name), None)


@pytest.fixture
def fake_meter() -> _FakeMeter:
    return _FakeMeter()


_RECONCILE_HANDLE_NAMES = (
    "_journey_advance_total",
    "_journey_parse_failure_total",
    "_journey_finalise_attempt_total",
    "_self_report_refs_capped_total",
    "_unmatched_self_report_refs_total",
    "_journey_reconcile_drift_total",
)


@pytest.fixture(autouse=True)
def _reset_reconcile_handles(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in _RECONCILE_HANDLE_NAMES:
        monkeypatch.setattr(reconcile_mod, name, None)


class TestReconcileMetrics:
    def test_get_meter_missing_provider_returns_none(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr("opentelemetry.metrics.get_meter_provider", lambda: None)
        assert reconcile_mod._get_meter() is None

    def test_get_meter_provider_failure_returns_none(self, monkeypatch: pytest.MonkeyPatch) -> None:
        def _boom() -> Any:
            raise RuntimeError("meter provider down")

        monkeypatch.setattr("opentelemetry.metrics.get_meter_provider", _boom)
        assert reconcile_mod._get_meter() is None

    def test_record_functions_initialise_and_attribute(
        self, monkeypatch: pytest.MonkeyPatch, fake_meter: _FakeMeter
    ) -> None:
        monkeypatch.setattr(reconcile_mod, "_get_meter", lambda: fake_meter)
        reconcile_mod.record_journey_advance(2)
        assert fake_meter.counter("modulo_journey_advance_total").calls == [{"value": 2, "attributes": None}]

        reconcile_mod.record_journey_parse_failure("live", 1)
        assert fake_meter.counter("modulo_journey_parse_failure_total").calls == [
            {"value": 1, "attributes": {"writer": "live"}}
        ]

        reconcile_mod.record_journey_finalise_attempt("live", 3)
        assert fake_meter.counter("modulo_journey_finalise_attempt_total").calls == [
            {"value": 3, "attributes": {"writer": "live"}}
        ]

        reconcile_mod.record_self_report_refs_capped(4)
        assert fake_meter.counter("modulo_journey_self_report_refs_capped_total").calls == [
            {"value": 4, "attributes": None}
        ]

        reconcile_mod.record_unmatched_self_report_refs(1)
        assert fake_meter.counter("modulo_journey_unmatched_self_report_refs_total").calls == [
            {"value": 1, "attributes": None}
        ]

        reconcile_mod.record_journey_reconcile_drift(2, kind="stale")
        assert fake_meter.counter("modulo_journey_reconcile_drift_total").calls == [
            {"value": 2, "attributes": {"kind": "stale"}}
        ]

    def test_ensure_early_return_when_handles_initialised(
        self, monkeypatch: pytest.MonkeyPatch, fake_meter: _FakeMeter
    ) -> None:
        monkeypatch.setattr(reconcile_mod, "_get_meter", lambda: fake_meter)
        reconcile_mod._ensure()
        reconcile_mod._ensure()
        # Only the first call builds the six handles; the second returns early.
        assert len(fake_meter.counters) == 6

    def test_record_functions_noop_without_meter(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(reconcile_mod, "_get_meter", lambda: None)
        reconcile_mod.record_journey_advance(1)
        reconcile_mod.record_journey_parse_failure("live")
        reconcile_mod.record_journey_finalise_attempt("live")
        reconcile_mod.record_self_report_refs_capped(1)
        reconcile_mod.record_unmatched_self_report_refs(1)
        reconcile_mod.record_journey_reconcile_drift(1, kind="missing")
        for name in _RECONCILE_HANDLE_NAMES:
            assert getattr(reconcile_mod, name) is None
