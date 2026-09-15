"""Journey dismissal (soft-delete) + operator restore tests (FAR-795 slice C).

These tests exercise the REAL paths against an in-memory SQLite database
(no mocks of the functions under test):

  * hydrate suppression: ``_hydrate_journeys`` never re-stamps the provenance
    of a dismissed row and never mints agent entries whose canonical row is a
    tombstone (the flag-ON agent probe counts them as
    ``refs_dismissal_suppressed`` instead of ``agent_minted``);
  * advancement suppression: ``advance_journeys`` never advances a dismissed
    row (newer evidence is refused, and the suppressed arm is NOT counted);
    refs whose row truly does not exist are still minted;
  * dismiss / restore helper idempotence: dismiss never overwrites an existing
    tombstone; restore is the ONLY un-dismissal path and is a no-op when
    nothing is dismissed;
  * read-path exclusion: a dismissed journey disappears from
    ``list_map_journeys`` / ``get_map_journey`` and returns after a restore;
  * restore reopens provenance upgrades (the upsert arm accepts writes again).
"""

import uuid
from collections.abc import AsyncGenerator
from datetime import datetime
from typing import Any, cast

import pytest
from sqlalchemy import Table, select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine

from modulo.core.lifecycle_map.advancement import advance_journeys
from modulo.core.lifecycle_map.journeys import (
    dismiss_journey,
    get_map_journey,
    list_map_journeys,
    restore_journey,
)
from modulo.db.crud.run import _hydrate_journeys
from modulo.db.lifecycle_refs import canonical_work_item_id, set_refs_counter_hook
from modulo.db.models.base import Base
from modulo.db.models.journey import Journey
from modulo.db.models.lifecycle_map import LifecycleMap
from modulo.db.models.lifecycle_map_stage import LifecycleMapStage
from modulo.db.models.organisation import Organisation
from modulo.db.models.run import Run

_ORG = uuid.UUID("00000000-0000-0000-0000-000000000001")
_ACCOUNT = uuid.UUID("00000000-0000-0000-0000-000000000002")
_MAP = uuid.UUID("00000000-0000-0000-0000-00000000000a")
_STAGE_PIPELINE = uuid.UUID("00000000-0000-0000-0000-0000000000a1")
_SNAPSHOT = uuid.UUID("00000000-0000-0000-0000-0000000000b1")

_T0 = datetime(2026, 1, 1, 0, 0, 0)
_T1 = datetime(2026, 1, 2, 0, 0, 0)
_T2 = datetime(2026, 1, 3, 0, 0, 0)

_TABLES: list[Table] = cast(
    list[Table],
    [
        Organisation.__table__,
        LifecycleMap.__table__,
        LifecycleMapStage.__table__,
        Journey.__table__,
        Run.__table__,
    ],
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


@pytest.fixture
def refs_events() -> list[tuple[str, dict[str, Any]]]:
    captured: list[tuple[str, dict[str, Any]]] = []
    set_refs_counter_hook(lambda event, attrs: captured.append((event, dict(attrs))))
    yield captured
    set_refs_counter_hook(None)


async def _seed_org(session: AsyncSession, *, settings: dict[str, Any] | None = None) -> None:
    session.add(Organisation(id=_ORG, name="test org", slug=f"test-{_ORG}", settings_json=settings))
    await session.flush()


async def _seed_map_fixtures(session: AsyncSession) -> None:
    session.add(
        LifecycleMap(
            id=_MAP,
            organisation_id=_ORG,
            name="SDLC",
            account_id=_ACCOUNT,
            version=1,
            content_json={"stages": []},
        )
    )
    session.add(
        LifecycleMapStage(
            organisation_id=_ORG,
            account_id=_ACCOUNT,
            map_id=_MAP,
            version=1,
            stage_id="review",
            stage_name="Review",
            position=1,
            stage_type="modulo",
            pipeline_id=_STAGE_PIPELINE,
        )
    )
    await session.flush()


async def _seed_journey(
    session: AsyncSession,
    *,
    kind: str = "github",
    ref: str = "a/b#5",
    provenance: str = "agent",
    updated_at: datetime = _T1,
) -> Journey:
    journey = Journey(
        organisation_id=_ORG,
        kind=kind,
        ref=ref,
        canonical_work_item_id=canonical_work_item_id(_ORG, kind, ref),
        provenance=provenance,
        first_seen_source=provenance,
        run_count=1,
        created_at=_T0,
        updated_at=updated_at,
    )
    session.add(journey)
    await session.flush()
    return journey


async def _read_journey(session: AsyncSession, *, kind: str = "github", ref: str = "a/b#5") -> Journey | None:
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


class TestHydrateDismissSuppression:
    async def test_hydrate_does_not_re_stamp_dismissed_provenance(self, session: AsyncSession) -> None:
        """Without the ``dismissed_at IS NULL`` hydration predicate, the
        rank-guarded arm would stamp the tombstone agent(0) -> derived(1)."""
        await _seed_org(session)
        tombstone = await _seed_journey(session, provenance="agent")
        assert await dismiss_journey(session, _ORG, kind="github", ref="a/b#5", dismissed_by=_ACCOUNT, reason="done")
        await session.flush()
        session.expire_all()
        row = await _read_journey(session)
        dismissed_at_before = row.dismissed_at
        tombstone_dismissed_at = tombstone.dismissed_at

        await _hydrate_journeys(session, _ORG, [{"kind": "github", "ref": "a/b#5", "source": "derived"}])
        journey = await _read_journey(session)
        assert journey is not None
        assert journey.provenance == "agent"
        assert journey.dismissed_at.replace(tzinfo=None) == tombstone_dismissed_at.replace(tzinfo=None)
        assert journey.dismissed_by == _ACCOUNT
        assert journey.reason == "done"
        assert dismissed_at_before is not None

    async def test_agent_probe_counts_dismissed_rows_not_as_mint(
        self, session: AsyncSession, refs_events: list[tuple[str, dict[str, Any]]]
    ) -> None:
        """Flag ON: agent entries whose canonical row is a tombstone are
        dismissal-suppressed (counted) and dropped from the mint batch."""
        import modulo.db.crud.run as run_mod

        await _seed_org(session, settings={"work_item_agent_minting_enabled": True})
        await _seed_journey(session, provenance="agent")
        assert await dismiss_journey(session, _ORG, kind="github", ref="a/b#5", dismissed_by=_ACCOUNT)
        await session.flush()
        run_mod.clear_agent_mint_flag_cache()
        try:
            await _hydrate_journeys(
                session,
                _ORG,
                [
                    {"kind": "github", "ref": "a/b#5", "source": "agent"},
                    {"kind": "linear", "ref": "FAR-1", "source": "agent"},
                ],
            )
        finally:
            run_mod.clear_agent_mint_flag_cache()
        session.expire_all()
        # The fresh ref is minted; the tombstoned one keeps its evidence.
        fresh = await _read_journey(session, kind="linear", ref="FAR-1")
        assert fresh is not None
        assert fresh.provenance == "agent"
        tombstone = await _read_journey(session)
        assert tombstone.dismissed_at is not None
        assert ("agent_minted", {"count": 1}) in refs_events
        assert ("refs_dismissal_suppressed", {"count": 1}) in refs_events

    async def test_restore_reopens_provenance_upgrades(self, session: AsyncSession) -> None:
        """After an operator restore the row is writable again: the next
        higher-rank intake stamp lands."""
        await _seed_org(session)
        tombstone = await _seed_journey(session, provenance="agent")
        assert await dismiss_journey(session, _ORG, kind="github", ref="a/b#5", dismissed_by=_ACCOUNT)
        await _hydrate_journeys(session, _ORG, [{"kind": "github", "ref": "a/b#5", "source": "derived"}])
        assert (await _read_journey(session)).provenance == "agent"

        assert await restore_journey(session, _ORG, kind="github", ref="a/b#5")
        assert tombstone.dismissed_by is None
        await session.flush()
        session.expire_all()
        await _hydrate_journeys(session, _ORG, [{"kind": "github", "ref": "a/b#5", "source": "derived"}])
        journey = await _read_journey(session)
        assert journey.provenance == "derived"
        assert journey.first_seen_source == "agent"


class TestDismissRestoreIdempotence:
    async def test_dismiss_missing_row_is_false(self, session: AsyncSession) -> None:
        await _seed_org(session)
        assert not await dismiss_journey(session, _ORG, kind="github", ref="gone", dismissed_by=_ACCOUNT)

    async def test_dismiss_never_overwrites_existing_tombstone(self, session: AsyncSession) -> None:
        await _seed_org(session)
        tombstone = await _seed_journey(session)
        assert await dismiss_journey(
            session, _ORG, kind="github", ref="a/b#5", dismissed_by=_ACCOUNT, reason="original"
        )
        await session.flush()
        other = uuid.uuid4()
        assert not await dismiss_journey(
            session, _ORG, kind="github", ref="a/b#5", dismissed_by=other, reason="over-write"
        )
        assert tombstone.dismissed_by == _ACCOUNT
        assert tombstone.reason == "original"

    async def test_restore_active_or_missing_row_is_false(self, session: AsyncSession) -> None:
        await _seed_org(session)
        await _seed_journey(session)  # active
        assert await _read_journey(session) is not None
        assert not await restore_journey(session, _ORG, kind="github", ref="a/b#5")
        assert not await restore_journey(session, _ORG, kind="github", ref="gone")

    async def test_restore_clears_all_tombstone_columns(self, session: AsyncSession) -> None:
        await _seed_org(session)
        await _seed_journey(session)
        assert await dismiss_journey(session, _ORG, kind="github", ref="a/b#5", dismissed_by=_ACCOUNT, reason="why")
        await session.flush()
        session.expire_all()
        assert await restore_journey(session, _ORG, kind="github", ref="a/b#5")
        await session.flush()
        row = await _read_journey(session)
        assert row is not None
        assert row.dismissed_at is None
        assert row.dismissed_by is None
        assert row.reason is None


class TestAdvanceDismissSuppression:
    async def test_advance_never_moves_dismissed_row(self, session: AsyncSession) -> None:
        await _seed_org(session)
        tombstone = await _seed_journey(session, updated_at=_T1)
        original_run_id = uuid.uuid4()
        tombstone.latest_terminal_run_id = original_run_id
        await session.flush()
        assert await dismiss_journey(session, _ORG, kind="github", ref="a/b#5", dismissed_by=_ACCOUNT)
        await session.flush()
        session.expire_all()
        tombstone_updated_at = (await _read_journey(session)).updated_at

        run_id = uuid.uuid4()
        advanced = await advance_journeys(
            session,
            _ORG,
            run_id=run_id,
            pipeline_id=uuid.uuid4(),
            refs=[{"kind": "github", "ref": "a/b#5", "source": "derived"}],
            status="complete",
            completed_at=_T2,
            run_created_at=_T1,
            is_replay=False,
            variant_group_id=None,
        )
        assert advanced == 0
        journey_row = await _read_journey(session)
        assert journey_row is not None
        assert journey_row.latest_terminal_run_id == original_run_id
        assert journey_row.run_count == 1
        assert journey_row.updated_at == tombstone_updated_at

    async def test_advance_mints_missing_refs_alongside_dismissed(self, session: AsyncSession) -> None:
        await _seed_org(session)
        await _seed_journey(session)
        assert await dismiss_journey(session, _ORG, kind="github", ref="a/b#5", dismissed_by=_ACCOUNT)
        await session.flush()
        session.expire_all()
        tombstone_updated_at = (await _read_journey(session)).updated_at

        run_id = uuid.uuid4()
        advanced = await advance_journeys(
            session,
            _ORG,
            run_id=run_id,
            pipeline_id=uuid.uuid4(),
            refs=[
                {"kind": "github", "ref": "a/b#5", "source": "derived"},
                {"kind": "github", "ref": "a/b#9", "source": "derived"},
            ],
            status="complete",
            completed_at=_T2,
            run_created_at=_T1,
            is_replay=False,
            variant_group_id=None,
        )
        assert advanced == 1  # only the genuinely-missing ref advanced
        dismissed_row = await _read_journey(session)
        assert dismissed_row.updated_at == tombstone_updated_at
        fresh = await _read_journey(session, ref="a/b#9")
        assert fresh is not None
        assert fresh.latest_terminal_run_id == run_id


class TestReadPathExclusion:
    async def test_dismissed_hidden_from_list_and_get_then_restored(self, session: AsyncSession) -> None:
        await _seed_org(session)
        await _seed_map_fixtures(session)
        journey = await _seed_journey(session, provenance="agent")
        # Attribute to the map with stage identity so list/get find it while active.
        journey.map_id = _MAP
        journey.map_version = 1
        journey.stage_id = "review"
        journey.stage_name = "Review"
        journey.position = 1
        await session.flush()
        journey_id = journey.id

        listed_before = await list_map_journeys(session, map_id=_MAP)
        assert [j.id for j, _unattributed in listed_before[0]] == [journey_id]
        assert await get_map_journey(session, map_id=_MAP, kind="github", ref="a/b#5") is not None

        assert await dismiss_journey(session, _ORG, kind="github", ref="a/b#5", dismissed_by=_ACCOUNT)
        await session.flush()
        listed_after = await list_map_journeys(session, map_id=_MAP)
        assert not listed_after[0]
        assert await get_map_journey(session, map_id=_MAP, kind="github", ref="a/b#5") is None

        assert await restore_journey(session, _ORG, kind="github", ref="a/b#5")
        await session.flush()
        session.expire_all()
        listed_restored = await list_map_journeys(session, map_id=_MAP)
        assert [j.id for j, _unattributed in listed_restored[0]] == [journey_id]
        assert await get_map_journey(session, map_id=_MAP, kind="github", ref="a/b#5") is not None
