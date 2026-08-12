"""Journey advancement service tests (FAR-143 part 2 + part 3).

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

Part 3 wiring (FAR-143 part 3):

  * the ``finalize_cost`` hook (``_advance_journeys_on_terminal``) advances
    from create-stamped refs AND from confirmed self-report refs (never mints
    from self-report), and is fail-open in its own savepoint
  * ``finalize_cost`` fires the hook on the zero-cost early-return, the main
    terminal, and the legacy-fallback terminal paths
  * the raw writers (``mark_complete`` / ``fail_run_terminal``) advance
    journeys from stored refs via ``_advance_journeys_from_stored_refs``
"""

import uuid
from collections.abc import AsyncGenerator
from datetime import datetime
from decimal import Decimal
from typing import Any, Self, cast
from unittest.mock import AsyncMock, MagicMock

import pytest
from sqlalchemy import Table, select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine

from modulo.core.cost_controller.finalize import _advance_journeys_on_terminal, finalize_cost
from modulo.core.lifecycle_map.advancement import advance_journeys
from modulo.core.pipeline_execution import (
    _advance_journeys_from_stored_refs,
    fail_run_terminal,
    mark_complete,
)
from modulo.db.lifecycle_refs import canonical_work_item_id
from modulo.db.models.base import Base
from modulo.db.models.journey import Journey
from modulo.db.models.lifecycle_map_stage import LifecycleMapStage
from modulo.db.models.run import Run

_ORG = uuid.UUID("00000000-0000-0000-0000-000000000001")
_PIPELINE = uuid.UUID("00000000-0000-0000-0000-0000000000a1")
_MAP_PIPELINE = uuid.UUID("00000000-0000-0000-0000-0000000000a2")
_MAP_ID = uuid.UUID("00000000-0000-0000-0000-0000000000c1")
_ACCOUNT = uuid.UUID("00000000-0000-0000-0000-0000000000d1")
_SNAPSHOT = uuid.UUID("00000000-0000-0000-0000-0000000000b1")

_REFS = [{"kind": "pr", "ref": "#123", "source": "derived"}]

# Controlled evidence timestamps (UTC, ascending) — no dependence on wall clock.
# Naive UTC so equality against SQLite's returned datetimes holds.
_T0 = datetime(2026, 1, 1, 0, 0, 0)
_T1 = datetime(2026, 1, 2, 0, 0, 0)
_T2 = datetime(2026, 1, 3, 0, 0, 0)
_T3 = datetime(2026, 1, 4, 0, 0, 0)

_TABLES: list[Table] = cast(
    list[Table],
    [Journey.__table__, LifecycleMapStage.__table__, Run.__table__],
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


# ---------------------------------------------------------------------------
# FAR-143 part 3 — finalize_cost hook + raw-writer wiring
# ---------------------------------------------------------------------------


async def _seed_run(
    session: AsyncSession,
    *,
    refs: list[dict[str, Any]] | None = None,
    status: str = "running",
    is_replay: bool = False,
    variant_group_id: uuid.UUID | None = None,
    completed_at: datetime | None = None,
) -> Run:
    """Seed a Run row directly (create-time stamping via ``create_run`` is
    covered in ``test_journey_create_run.py``)."""
    run = Run(
        id=uuid.uuid4(),
        organisation_id=_ORG,
        pipeline_id=_PIPELINE,
        snapshot_id=_SNAPSHOT,
        trigger_type="manual",
        run_number=1,
        input_hash="x",
        langgraph_thread_id=f"{_ORG}:{uuid.uuid4()}",
        status=status,
        work_item_refs=refs,
        is_replay=is_replay,
        variant_group_id=variant_group_id,
        created_at=_T1,
        completed_at=completed_at,
        # Explicit — SQLite renders the model's ``server_default="false"`` as
        # ``DEFAULT 'false'`` (a truthy string), which would trip finalize_cost's
        # B6 cancel-wins rewrite on this backend.
        cancellation_requested=False,
        ledger_written=False,
    )
    session.add(run)
    await session.flush()
    return run


async def _seed_journey_by_kind(
    session: AsyncSession,
    kind: str,
    ref: str,
) -> Journey:
    journey = Journey(
        organisation_id=_ORG,
        kind=kind,
        ref=ref,
        canonical_work_item_id=canonical_work_item_id(_ORG, kind, ref),
        run_count=0,
        created_at=_T0,
        updated_at=_T0,
    )
    session.add(journey)
    await session.flush()
    return journey


async def _read_journey_by_kind(session: AsyncSession, kind: str, ref: str) -> Journey | None:
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


async def _read_run_refs(session: AsyncSession, run_id: uuid.UUID) -> list[dict[str, Any]] | None:
    session.expire_all()
    return (await session.execute(select(Run.work_item_refs).where(Run.id == run_id))).scalar_one()


def _patch_finalize_machinery(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make ``finalize_cost``'s cost/ledger/analytics machinery cheap no-ops so
    the journey-hook wiring can be asserted without a full cost stack."""

    async def _noop(*_a: Any, **_k: Any) -> None:
        return None

    async def _fake_components(*_a: Any, **_k: Any) -> list[Any]:
        return []

    monkeypatch.setattr("modulo.core.cost_controller.finalize.record_run_facts", _noop)
    monkeypatch.setattr("modulo.core.cost_controller.finalize._ledger_block", _noop)
    monkeypatch.setattr("modulo.core.cost_controller.finalize.load_live_components", _fake_components)
    # ``finalize_cost`` calls ``get_settings()`` at argument-evaluation time for
    # ``build_cost_breakdown(..., settings=...)`` — stub it so no real Settings
    # (which requires env vars) is constructed.
    monkeypatch.setattr("modulo.settings.get_settings", lambda: MagicMock())
    monkeypatch.setattr(
        "modulo.core.cost_controller.finalize.build_telemetry",
        lambda _enriched, _components: ({}, {}),
    )
    monkeypatch.setattr(
        "modulo.core.cost_controller.finalize.build_cost_breakdown",
        lambda _telemetry, _components, settings=None: ([], Decimal(0)),
    )


class TestFinalizeJourneyHook:
    """The finalise hook itself — self-report confirm + create-stamped advance."""

    async def test_create_stamped_refs_advance(self, session: AsyncSession) -> None:
        run = await _seed_run(session, refs=[{"kind": "github_pr", "ref": "123", "source": "derived"}])
        run_id = run.id
        await _advance_journeys_on_terminal(session, run, "complete", {})
        journey = await _read_journey_by_kind(session, "github_pr", "123")
        assert journey is not None
        assert journey.latest_terminal_run_id == run_id
        assert journey.latest_status == "complete"
        assert journey.latest_provenance == "derived"
        assert journey.run_count == 1

    async def test_self_report_confirmed_ref_is_appended_and_advances(self, session: AsyncSession) -> None:
        run = await _seed_run(session, refs=[{"kind": "github_pr", "ref": "123", "source": "derived"}])
        run_id = run.id
        await _seed_journey_by_kind(session, "github_pr", "456")
        merged = {"node1": {"output": {"work_item_refs": [{"kind": "github_pr", "ref": "#456", "status": "done"}]}}}
        await _advance_journeys_on_terminal(session, run, "complete", merged)

        refs = await _read_run_refs(session, run_id)
        assert refs is not None
        assert {"kind": "github_pr", "ref": "456", "source": "reported", "status": "done"} in refs
        assert {"kind": "github_pr", "ref": "123", "source": "derived"} in refs
        journey = await _read_journey_by_kind(session, "github_pr", "456")
        assert journey is not None
        assert journey.latest_terminal_run_id == run_id
        assert journey.latest_provenance == "reported"
        assert journey.run_count == 1

    async def test_self_report_without_journey_is_dropped_not_minted(self, session: AsyncSession) -> None:
        run = await _seed_run(session, refs=[{"kind": "github_pr", "ref": "123", "source": "derived"}])
        run_id = run.id
        merged = {"node1": {"work_item_refs": [{"kind": "github_pr", "ref": "#789", "status": "done"}]}}
        await _advance_journeys_on_terminal(session, run, "complete", merged)

        refs = await _read_run_refs(session, run_id)
        assert refs == [{"kind": "github_pr", "ref": "123", "source": "derived"}]
        assert await _read_journey_by_kind(session, "github_pr", "789") is None

    async def test_fail_open_logs_and_does_not_raise(
        self, session: AsyncSession, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        run = await _seed_run(session, refs=[{"kind": "github_pr", "ref": "123", "source": "derived"}])
        run_id = run.id

        async def _boom(*_a: Any, **_k: Any) -> int:
            raise RuntimeError("journey explosion")

        monkeypatch.setattr("modulo.core.cost_controller.finalize.advance_journeys", _boom)
        with caplog.at_level("ERROR", logger="modulo.core.cost_controller.finalize"):
            await _advance_journeys_on_terminal(session, run, "complete", {})
        assert any("journey_advance_failed" in m for m in caplog.messages)
        # Nothing escaped — and no journey was written.
        assert await _read_journey_by_kind(session, "github_pr", "123") is None
        # The run row is untouched by the hook failure.
        assert await _read_run_refs(session, run_id) == [{"kind": "github_pr", "ref": "123", "source": "derived"}]


class TestFinalizeCostWiring:
    """finalize_cost fires the hook on the terminal paths."""

    async def test_early_return_terminal_advances_from_stamped_refs(
        self, session: AsyncSession, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _patch_finalize_machinery(monkeypatch)
        run = await _seed_run(session, refs=[{"kind": "github_pr", "ref": "123", "source": "derived"}])
        run_id = run.id
        await finalize_cost(
            session,
            run_id=run_id,
            org_id=_ORG,
            status="complete",
            segment_node_token_usage=None,
            segment_completed_node_outputs=None,
            node_type_map={},
            is_terminal=True,
        )
        journey = await _read_journey_by_kind(session, "github_pr", "123")
        assert journey is not None
        assert journey.latest_terminal_run_id == run_id
        assert journey.run_count == 1

    async def test_main_path_confirms_self_report_and_advances(
        self, session: AsyncSession, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _patch_finalize_machinery(monkeypatch)
        run = await _seed_run(session, refs=[{"kind": "github_pr", "ref": "123", "source": "derived"}])
        run_id = run.id
        await _seed_journey_by_kind(session, "github_pr", "456")
        await finalize_cost(
            session,
            run_id=run_id,
            org_id=_ORG,
            status="complete",
            segment_node_token_usage={"node1": {"input_tokens": 1, "output_tokens": 1, "total_tokens": 2}},
            segment_completed_node_outputs={
                "node1": {"output": {"work_item_refs": [{"kind": "github_pr", "ref": "#456", "status": "done"}]}}
            },
            node_type_map={},
            is_terminal=True,
        )
        refs = await _read_run_refs(session, run_id)
        assert refs is not None
        assert {"kind": "github_pr", "ref": "456", "source": "reported", "status": "done"} in refs
        journey = await _read_journey_by_kind(session, "github_pr", "456")
        assert journey is not None
        assert journey.latest_terminal_run_id == run_id

    async def test_hook_failure_never_blocks_terminal_write(
        self, session: AsyncSession, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        _patch_finalize_machinery(monkeypatch)

        async def _boom(*_a: Any, **_k: Any) -> int:
            raise RuntimeError("journey explosion")

        monkeypatch.setattr("modulo.core.cost_controller.finalize.advance_journeys", _boom)
        run = await _seed_run(session, refs=[{"kind": "github_pr", "ref": "123", "source": "derived"}])
        run_id = run.id
        with caplog.at_level("ERROR", logger="modulo.core.cost_controller.finalize"):
            await finalize_cost(
                session,
                run_id=run_id,
                org_id=_ORG,
                status="complete",
                segment_node_token_usage=None,
                segment_completed_node_outputs=None,
                node_type_map={},
                is_terminal=True,
            )
        session.expire_all()
        refreshed = (await session.execute(select(Run).where(Run.id == run_id))).scalar_one()
        assert refreshed.status == "complete"
        assert refreshed.completed_at is not None
        assert any("journey_advance_failed" in m for m in caplog.messages)


class TestJourneyFactsDenominators:
    """FAR-143 part 4 — the finalise hook records per-writer denominators + metrics."""

    async def test_hook_records_per_writer_fact(self, session: AsyncSession, monkeypatch: pytest.MonkeyPatch) -> None:
        run = await _seed_run(session, refs=[{"kind": "github_pr", "ref": "123", "source": "derived"}])
        await _seed_journey_by_kind(session, "github_pr", "456")
        captured: dict[str, Any] = {}

        async def _fake_fact(
            session_: Any, run_: Any, writer: str, parse_failures: int, finalise_attempts: int
        ) -> None:
            captured.update(writer=writer, parse_failures=parse_failures, finalise_attempts=finalise_attempts)

        monkeypatch.setattr(
            "modulo.core.cost_controller.finalize._record_journey_fact",
            _fake_fact,
        )
        merged = {
            "node1": {
                "work_item_refs": [
                    {"kind": "github_pr", "ref": "#456", "status": "done"},
                    {"kind": "", "ref": "broken"},
                    "not-a-dict",
                ]
            }
        }
        await _advance_journeys_on_terminal(session, run, "complete", merged, writer="fallback")

        # 3 raw self-report entries attempted; 2 malformed (bad kind + non-dict).
        assert captured["writer"] == "fallback"
        assert captured["parse_failures"] == 2
        assert captured["finalise_attempts"] == 3

    async def test_metric_counters_wired(self, session: AsyncSession, monkeypatch: pytest.MonkeyPatch) -> None:
        run = await _seed_run(session, refs=[{"kind": "github_pr", "ref": "123", "source": "derived"}])
        await _seed_journey_by_kind(session, "github_pr", "456")
        advance = MagicMock()
        parse_failure = MagicMock()
        finalise_attempt = MagicMock()
        capped = MagicMock()
        unmatched = MagicMock()
        for name, mock in (
            ("record_journey_advance", advance),
            ("record_journey_parse_failure", parse_failure),
            ("record_journey_finalise_attempt", finalise_attempt),
            ("record_self_report_refs_capped", capped),
            ("record_unmatched_self_report_refs", unmatched),
        ):
            monkeypatch.setattr(f"modulo.core.cost_controller.finalize.{name}", mock)
        merged = {
            "node1": {
                "work_item_refs": [
                    {"kind": "github_pr", "ref": "#456", "status": "done"},  # confirmed (journey exists)
                    {"kind": "github_pr", "ref": "#789", "status": "done"},  # unmatched (no journey)
                    {"kind": "", "ref": "broken"},  # malformed
                ]
            }
        }
        await _advance_journeys_on_terminal(session, run, "complete", merged, writer="live")

        advance.assert_called_once_with(2)  # create-stamped #123 + confirmed #456
        parse_failure.assert_called_once_with("live", 1)
        finalise_attempt.assert_called_once_with("live", 3)
        capped.assert_not_called()
        unmatched.assert_called_once_with(1)


class _FakeAsyncResult:
    def __init__(self, row: object | None = None) -> None:
        self._row = row

    def fetchone(self) -> object | None:
        return self._row


class _FakeAsyncConn:
    """Async fake connection returning a fixed row (mirrors test_pipeline_execution)."""

    def __init__(self, row: object | None = None) -> None:
        self._row = row

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, *args: object) -> bool:
        return False

    def begin(self) -> Self:
        return self

    async def execute(self, stmt: object, params: dict[str, object] | None = None) -> _FakeAsyncResult:
        return _FakeAsyncResult(self._row)


class TestRawWriterJourneyAdvance:
    """mark_complete / fail_run_terminal advance journeys from stored refs."""

    async def test_advance_from_stored_refs_real(self, tmp_path: Any) -> None:
        # File-backed SQLite so the helper's own session sees the seeded run.
        engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path}/journey_raw.db", echo=False)
        try:
            async with engine.begin() as conn:
                await conn.run_sync(lambda sync_conn: Base.metadata.create_all(sync_conn, tables=_TABLES))
                await conn.exec_driver_sql("PRAGMA foreign_keys = OFF")
            maker = async_sessionmaker(engine, expire_on_commit=False)
            async with maker() as s, s.begin():
                run = await _seed_run(
                    s,
                    refs=[{"kind": "github_pr", "ref": "123", "source": "derived"}],
                    status="complete",
                    completed_at=_T2,
                )
                run_id = run.id
            await _advance_journeys_from_stored_refs(engine, str(run_id), str(_ORG), "complete")
            async with maker() as s:
                journey = await _read_journey_by_kind(s, "github_pr", "123")
                assert journey is not None
                assert journey.latest_terminal_run_id == run_id
                assert journey.latest_status == "complete"
                assert journey.run_count == 1
        finally:
            await engine.dispose()

    async def test_mark_complete_wires_advance_on_success(self, monkeypatch: pytest.MonkeyPatch) -> None:
        engine = MagicMock()
        engine.connect.return_value = _FakeAsyncConn(("id",))
        advanced = AsyncMock()
        monkeypatch.setattr("modulo.core.pipeline_execution._advance_journeys_from_stored_refs", advanced)
        await mark_complete(engine, "run-1", "org-1")  # type: ignore[arg-type]
        advanced.assert_awaited_once_with(engine, "run-1", "org-1", "complete")

    async def test_mark_complete_skips_advance_when_noop(self, monkeypatch: pytest.MonkeyPatch) -> None:
        engine = MagicMock()
        engine.connect.return_value = _FakeAsyncConn(None)
        advanced = AsyncMock()
        monkeypatch.setattr("modulo.core.pipeline_execution._advance_journeys_from_stored_refs", advanced)
        await mark_complete(engine, "run-1", "org-1")  # type: ignore[arg-type]
        advanced.assert_not_awaited()

    async def test_fail_run_terminal_wires_advance_on_success(self, monkeypatch: pytest.MonkeyPatch) -> None:
        engine = MagicMock()
        engine.connect.return_value = _FakeAsyncConn(("id",))
        advanced = AsyncMock()
        monkeypatch.setattr("modulo.core.pipeline_execution._advance_journeys_from_stored_refs", advanced)
        ok = await fail_run_terminal(  # type: ignore[arg-type]
            engine,
            "run-1",
            "org-1",
            error_code="executor_stalled",
            error_detail="boom",
        )
        assert ok is True
        advanced.assert_awaited_once_with(engine, "run-1", "org-1", "failed")

    async def test_fail_run_terminal_skips_advance_when_noop(self, monkeypatch: pytest.MonkeyPatch) -> None:
        engine = MagicMock()
        engine.connect.return_value = _FakeAsyncConn(None)
        advanced = AsyncMock()
        monkeypatch.setattr("modulo.core.pipeline_execution._advance_journeys_from_stored_refs", advanced)
        ok = await fail_run_terminal(  # type: ignore[arg-type]
            engine,
            "run-1",
            "org-1",
            error_code="executor_stalled",
            error_detail="boom",
        )
        assert ok is False
        advanced.assert_not_awaited()
