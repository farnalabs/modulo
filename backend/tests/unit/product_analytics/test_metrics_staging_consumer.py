"""FAR-1717: metrics_staging consumption + purge, end to end on a real DB.

The ingest endpoint stages curated frontend events into ``metrics_staging``;
before FAR-1717 nothing ever read or deleted those rows. These tests exercise
the REAL consumer and the REAL purge against in-memory SQLite (the SQL is
dialect-independent: a GROUP BY count and a range DELETE — a mocked session
cannot demonstrate either), and each one FAILS against the pre-change code:

* ``_aggregate_staged_events`` counts staged events by type for one (org set,
  UTC day) — the function does not exist before the change.
* ``_build_payload`` carries the aggregation under ``event_counts`` — before
  the change the payload has no such key (KeyError).
* ``_purge_staging`` deletes consumed rows (watermark past their date) and rows
  past the retention bound — the function does not exist before the change.
* ``metrics_dump`` runs the purge on EVERY tick, before the jitter gate —
  without the wiring the stale row survives a jitter-skip tick.

RLS is a migration-level concern (absent on SQLite); the system-factory
requirement is pinned by ``TestSystemSessionFactory`` in
``test_metrics_dump.py``.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncGenerator
from datetime import UTC, date, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from modulo.core.product_analytics.metrics_dump import (
    _STAGING_RETENTION_DAYS,
    _aggregate_staged_events,
    _build_payload,
    _purge_staging,
    metrics_dump,
)
from modulo.db.models.base import Base
from modulo.db.models.metrics_staging import MetricsStaging
from modulo.db.models.system_config import SystemConfig

_ORG_A = uuid.uuid4()
_ORG_B = uuid.uuid4()

_DAY = date(2026, 10, 10)


def _at(day: date, hour: int = 12) -> datetime:
    return datetime(day.year, day.month, day.day, hour, tzinfo=UTC)


def _event(
    *,
    org_id: uuid.UUID,
    event_type: str,
    recorded_at: datetime,
    event_id: str | None = None,
) -> MetricsStaging:
    return MetricsStaging(
        id=uuid.uuid4(),
        organisation_id=org_id,
        event_id=event_id or f"{event_type}-{recorded_at.isoformat()}-{uuid.uuid4().hex[:8]}",
        event_type=event_type,
        payload={},
        recorded_at=recorded_at,
    )


@pytest.fixture
async def factory() -> AsyncGenerator[async_sessionmaker[AsyncSession], None]:
    """In-memory SQLite with exactly the two tables these tests touch."""
    engine = create_async_engine("sqlite+aiosqlite://", echo=False)
    async with engine.begin() as conn:
        await conn.run_sync(
            lambda sync_conn: Base.metadata.create_all(
                sync_conn,
                tables=[MetricsStaging.__table__, SystemConfig.__table__],
            )
        )
    maker = async_sessionmaker(engine, expire_on_commit=False)
    try:
        yield maker
    finally:
        await engine.dispose()


async def _seed(maker: async_sessionmaker[AsyncSession], rows: list[MetricsStaging]) -> None:
    async with maker() as session, session.begin():
        session.add_all(rows)


async def _remaining_event_ids(maker: async_sessionmaker[AsyncSession]) -> list[str]:
    async with maker() as session, session.begin():
        result = await session.execute(select(MetricsStaging.event_id).order_by(MetricsStaging.event_id))
        return list(result.scalars())


async def _count(maker: async_sessionmaker[AsyncSession]) -> int:
    async with maker() as session, session.begin():
        return int((await session.execute(select(func.count()).select_from(MetricsStaging))).scalar_one())


async def _set_watermark(maker: async_sessionmaker[AsyncSession], value: str) -> None:
    from modulo.core.product_analytics.constants import DUMP_WATERMARK_KEY

    async with maker() as session, session.begin():
        session.add(SystemConfig(id=uuid.uuid4(), key=DUMP_WATERMARK_KEY, value=value))


# ---------------------------------------------------------------------------
# Consumer: staged events aggregated into the daily payload
# ---------------------------------------------------------------------------


class TestAggregateStagedEvents:
    async def test_counts_by_type_for_target_org_and_day(
        self,
        factory: async_sessionmaker[AsyncSession],
    ) -> None:
        """(org A, 2026-10-10) sees its own two pipeline_created + one api_error —
        NOT org B's rows and NOT rows dated the next day."""
        await _seed(
            factory,
            [
                _event(org_id=_ORG_A, event_type="pipeline_created", recorded_at=_at(_DAY, 9)),
                _event(org_id=_ORG_A, event_type="pipeline_created", recorded_at=_at(_DAY, 10)),
                _event(org_id=_ORG_A, event_type="api_error", recorded_at=_at(_DAY, 23)),
                # Same type, wrong day -> excluded from (org A, _DAY).
                _event(org_id=_ORG_A, event_type="pipeline_created", recorded_at=_at(_DAY + timedelta(days=1))),
                # Same type, same day, wrong org -> excluded.
                _event(org_id=_ORG_B, event_type="pipeline_created", recorded_at=_at(_DAY)),
            ],
        )

        async with factory() as session, session.begin():
            counts = await _aggregate_staged_events(session, [_ORG_A], _DAY)

        assert counts == {"pipeline_created": 2, "api_error": 1}

    async def test_day_window_is_half_open_utc(
        self,
        factory: async_sessionmaker[AsyncSession],
    ) -> None:
        """00:00 belongs to its day; the next day's 00:00 does not."""
        await _seed(
            factory,
            [
                _event(org_id=_ORG_A, event_type="schema_created", recorded_at=_at(_DAY, 0)),
                _event(org_id=_ORG_A, event_type="schema_created", recorded_at=_at(_DAY + timedelta(days=1), 0)),
            ],
        )

        async with factory() as session, session.begin():
            counts = await _aggregate_staged_events(session, [_ORG_A], _DAY)

        assert counts == {"schema_created": 1}

    async def test_no_staged_events_returns_empty(
        self,
        factory: async_sessionmaker[AsyncSession],
    ) -> None:
        async with factory() as session, session.begin():
            counts = await _aggregate_staged_events(session, [_ORG_A], _DAY)

        assert not counts


class TestBuildPayloadEventCounts:
    async def test_payload_carries_staged_event_counts(
        self,
        factory: async_sessionmaker[AsyncSession],
    ) -> None:
        """``_build_payload`` must include ``event_counts`` — the section does
        not exist before FAR-1717, so this FAILS with KeyError on the old code."""
        await _seed(
            factory,
            [
                _event(org_id=_ORG_A, event_type="pipeline_created", recorded_at=_at(_DAY, 9)),
                _event(org_id=_ORG_A, event_type="api_error", recorded_at=_at(_DAY, 10)),
                # Out of window: different day must not leak into the payload.
                _event(org_id=_ORG_A, event_type="api_error", recorded_at=_at(_DAY + timedelta(days=1))),
            ],
        )
        orgs = [{"id": _ORG_A, "level_changed_at": None}]

        with (
            # The other aggregates read tables this fixture does not create;
            # only the staged-event consumer runs for real here.
            patch(
                "modulo.core.product_analytics.metrics_dump._count_entities",
                new_callable=AsyncMock,
                return_value={"pipelines": 0},
            ),
            patch(
                "modulo.core.product_analytics.metrics_dump._aggregate_run_stats",
                new_callable=AsyncMock,
                return_value={"total_runs": 0},
            ),
            patch(
                "modulo.core.product_analytics.metrics_dump._aggregate_error_stats",
                new_callable=AsyncMock,
                return_value={"total": 0},
            ),
            patch(
                "modulo.core.product_analytics.metrics_dump._build_integration_inventory",
                new_callable=AsyncMock,
                return_value={},
            ),
        ):
            payload = await _build_payload(factory, orgs, _DAY)

        assert payload["event_counts"] == {"pipeline_created": 1, "api_error": 1}


# ---------------------------------------------------------------------------
# Purge: consumed rows + retention bound
# ---------------------------------------------------------------------------


class TestPurgeStaging:
    async def test_purges_consumed_rows_and_keeps_today(
        self,
        factory: async_sessionmaker[AsyncSession],
    ) -> None:
        """Watermark = yesterday -> every date through yesterday is dumped and
        purgeable; rows dated today are held back (they may post-date today's
        dump moment and must not be deleted by their own dump day)."""
        today = datetime.now(UTC).date()
        await _set_watermark(factory, today.isoformat())
        await _seed(
            factory,
            [
                _event(org_id=_ORG_A, event_type="api_error", recorded_at=_at(today - timedelta(days=5))),
                _event(org_id=_ORG_A, event_type="api_error", recorded_at=_at(today - timedelta(days=1), 23)),
                _event(org_id=_ORG_A, event_type="api_error", recorded_at=datetime.now(UTC)),
            ],
        )

        deleted = await _purge_staging(factory)

        assert deleted == 2
        assert await _count(factory) == 1

    async def test_watermark_behind_today_purges_only_through_watermark(
        self,
        factory: async_sessionmaker[AsyncSession],
    ) -> None:
        """Watermark = 5 days ago: dates <= watermark purge; the undumped days
        between watermark and today are RETAINED for the next successful dump."""
        today = datetime.now(UTC).date()
        watermark = today - timedelta(days=5)
        await _set_watermark(factory, watermark.isoformat())
        await _seed(
            factory,
            [
                _event(org_id=_ORG_A, event_type="api_error", recorded_at=_at(watermark - timedelta(days=2))),
                _event(org_id=_ORG_A, event_type="api_error", recorded_at=_at(watermark)),
                _event(org_id=_ORG_A, event_type="api_error", recorded_at=_at(watermark + timedelta(days=1))),
            ],
        )

        deleted = await _purge_staging(factory)

        # cutoff = start_of_day(watermark + 1) -> the watermark day itself is
        # consumed (purged), the day after is not.
        assert deleted == 2
        assert await _count(factory) == 1

    async def test_retention_bounds_rows_when_dump_never_ran(
        self,
        factory: async_sessionmaker[AsyncSession],
    ) -> None:
        """No watermark (instance switch off / dumps failing) -> rows older
        than the retention bound still purge, so growth is bounded."""
        now = datetime.now(UTC)
        await _seed(
            factory,
            [
                _event(
                    org_id=_ORG_A,
                    event_type="api_error",
                    recorded_at=now - timedelta(days=_STAGING_RETENTION_DAYS + 10),
                ),
                _event(org_id=_ORG_A, event_type="api_error", recorded_at=now - timedelta(days=1)),
            ],
        )

        deleted = await _purge_staging(factory, now=now)

        assert deleted == 1
        assert await _count(factory) == 1

    async def test_far_future_dated_rows_are_purged(
        self,
        factory: async_sessionmaker[AsyncSession],
    ) -> None:
        """A future-dated row is never consumed and must still expire.

        ``recorded_at`` is client-supplied; no dump looks past today, so a
        far-future row is undeliverable, and a past-only cutoff would keep it
        forever even with no watermark (the instance switch off). The purge
        owns the retention bound, so it deletes beyond the future horizon too.
        This FAILS against the pre-change code, which only deleted rows older
        than its cutoff and left the future row in place.
        """
        now = datetime.now(UTC)
        await _seed(
            factory,
            [
                _event(
                    org_id=_ORG_A,
                    event_type="pipeline_created",
                    recorded_at=now + timedelta(days=_STAGING_RETENTION_DAYS + 10),
                ),
                _event(org_id=_ORG_A, event_type="api_error", recorded_at=now),
            ],
        )

        deleted = await _purge_staging(factory, now=now)

        assert deleted == 1
        assert await _count(factory) == 1

    async def test_near_future_dated_rows_are_retained(
        self,
        factory: async_sessionmaker[AsyncSession],
    ) -> None:
        """A row dated a day ahead is consumable by tomorrow's dump and survives."""
        now = datetime.now(UTC)
        await _seed(
            factory,
            [_event(org_id=_ORG_A, event_type="api_error", recorded_at=now + timedelta(days=1))],
        )

        deleted = await _purge_staging(factory, now=now)

        assert deleted == 0
        assert await _count(factory) == 1

    async def test_future_watermark_never_purges_todays_rows(
        self,
        factory: async_sessionmaker[AsyncSession],
    ) -> None:
        """Clock skew (watermark ahead of now) must cap at today, not delete
        rows that have not been delivered."""
        today = datetime.now(UTC).date()
        await _set_watermark(factory, (today + timedelta(days=1)).isoformat())
        await _seed(
            factory,
            [
                _event(org_id=_ORG_A, event_type="api_error", recorded_at=datetime.now(UTC)),
                _event(org_id=_ORG_A, event_type="api_error", recorded_at=_at(today, 0)),
            ],
        )

        deleted = await _purge_staging(factory)

        assert deleted == 0
        assert await _count(factory) == 2

    async def test_returns_zero_when_nothing_matches(
        self,
        factory: async_sessionmaker[AsyncSession],
    ) -> None:
        await _seed(
            factory,
            [_event(org_id=_ORG_A, event_type="api_error", recorded_at=datetime.now(UTC))],
        )

        deleted = await _purge_staging(factory)

        assert deleted == 0


# ---------------------------------------------------------------------------
# Wiring: metrics_dump purges on EVERY tick, before the jitter gate
# ---------------------------------------------------------------------------


class TestMetricsDumpPurgeWiring:
    async def test_jitter_skip_tick_still_purges(
        self,
        factory: async_sessionmaker[AsyncSession],
    ) -> None:
        """The jitter gate skips 359 of every 360 ticks — if the purge sat
        behind the gate, retention would be unbounded in practice. A stale row
        must be gone even though the tick reports jitter_skip."""
        now = datetime.now(UTC)
        stale = _event(
            org_id=_ORG_A,
            event_type="api_error",
            recorded_at=now - timedelta(days=_STAGING_RETENTION_DAYS + 5),
        )
        fresh = _event(org_id=_ORG_A, event_type="api_error", recorded_at=now)
        await _seed(factory, [stale, fresh])

        with (
            patch(
                "modulo.core.saq_worker._make_system_session_factory",
                return_value=factory,
            ),
            patch(
                "modulo.core.product_analytics.metrics_dump._should_dump_now",
                new_callable=AsyncMock,
                return_value=False,
            ),
            patch("modulo.settings.get_settings", return_value=MagicMock()),
        ):
            result = await metrics_dump({})

        assert result["skipped"] == "jitter_skip"
        assert result["staging_purged"] == 1
        assert await _remaining_event_ids(factory) == [fresh.event_id]
