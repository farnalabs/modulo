"""Unit tests for the System Maps loop admission runtime (FAR-2349).

The admission *gate order* and the reservation lifecycle are exercised against a
real in-memory SQLite database with the real models, so the predicates and the
status transitions actually run. The full admit path (run + snapshot creation) is
covered by ``tests/integration/test_system_map_loop_admission.py`` against real
Postgres — here the run/snapshot seam is stubbed so a test can isolate a gate.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncGenerator
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace
from typing import Any

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine

from modulo.core import system_map_loop_admission as loop_runtime
from modulo.db.models.base import Base
from modulo.db.models.organisation import Organisation
from modulo.db.models.pipeline import Pipeline
from modulo.db.models.run import Run
from modulo.db.models.system_map import (
    SystemMap,
    SystemMapAttributionBinding,
    SystemMapLoopAdmissionAttempt,
    SystemMapLoopConfig,
    SystemMapLoopReservation,
    SystemMapRevision,
)
from modulo.db.repositories.system_map import settle_loop_reservation_for_run

_ORG = uuid.UUID("11111111-1111-1111-1111-111111111111")
_OTHER_ORG = uuid.UUID("22222222-2222-2222-2222-222222222222")
_MAP = uuid.UUID("33333333-3333-3333-3333-333333333333")
_REV = uuid.UUID("44444444-4444-4444-4444-444444444444")
_LOOP = uuid.UUID("55555555-5555-5555-5555-555555555555")
_PIPELINE = uuid.UUID("66666666-6666-6666-6666-666666666666")
_ACCOUNT = uuid.UUID("77777777-7777-7777-7777-777777777777")
_NODE_KEY = "improve_architecture"

_TABLES: list[Any] = [
    Organisation.__table__,
    Pipeline.__table__,
    Run.__table__,
    SystemMap.__table__,
    SystemMapRevision.__table__,
    SystemMapLoopConfig.__table__,
    SystemMapAttributionBinding.__table__,
    SystemMapLoopAdmissionAttempt.__table__,
    SystemMapLoopReservation.__table__,
]

_NOW = datetime(2026, 11, 3, 12, 0, tzinfo=UTC)
_MONTH_START = date(2026, 11, 1)


@pytest.fixture
def audit_events(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    """Capture audit appends instead of writing the audit chain."""
    recorded: list[dict[str, Any]] = []

    async def _append(session: AsyncSession, **kwargs: Any) -> SimpleNamespace:
        recorded.append(kwargs)
        return SimpleNamespace(id=uuid.uuid4())

    monkeypatch.setattr("modulo.core.audit_logger.append_audit_event", _append)
    return recorded


@pytest_asyncio.fixture
async def session() -> AsyncGenerator[AsyncSession, None]:
    engine: AsyncEngine = create_async_engine("sqlite+aiosqlite://", echo=False)
    async with engine.begin() as conn:
        await conn.run_sync(lambda sync_conn: Base.metadata.create_all(sync_conn, tables=_TABLES))
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as db:
        db.info["org_id"] = _ORG
        yield db
    await engine.dispose()


def _seed_loop(
    db: AsyncSession,
    *,
    published: bool = True,
    revision_status: str = "published",
    pipeline_archived: bool = False,
    run_enabled: bool = True,
    autonomy: str = "notify_on_complete",
    max_concurrent_runs: int = 1,
    budget_limit_usd: Decimal = Decimal(10),
    cooldown_seconds: int = 0,
    observation_window_seconds: int = 60,
    binding: bool = True,
) -> None:
    db.add(Organisation(id=_ORG, name="Admission Org", slug="admission-org"))
    db.add(
        Pipeline(
            id=_PIPELINE,
            organisation_id=_ORG,
            name="Improve Architecture",
            account_id=_ACCOUNT,
            default_autonomy_level=autonomy,
            run_enabled=run_enabled,
            archived_at=_NOW if pipeline_archived else None,
            max_concurrent_runs=5,
        )
    )
    db.add(
        SystemMap(
            id=_MAP,
            organisation_id=_ORG,
            name="Loop map",
            account_id=_ACCOUNT,
            published_revision_id=_REV if published else None,
        )
    )
    db.add(
        SystemMapRevision(
            id=_REV,
            organisation_id=_ORG,
            system_map_id=_MAP,
            revision_number=1,
            status=revision_status,
            content_json={},
        )
    )
    db.add(
        SystemMapLoopConfig(
            id=_LOOP,
            organisation_id=_ORG,
            revision_id=_REV,
            node_key=_NODE_KEY,
            target_signal="uncovered_architecture_gaps",
            observation_window_seconds=observation_window_seconds,
            action_pipeline_id=_PIPELINE,
            budget_limit_usd=budget_limit_usd,
            max_concurrent_runs=max_concurrent_runs,
            cooldown_seconds=cooldown_seconds,
            outcome_metric="delta_test_failures",
        )
    )
    if binding:
        db.add(
            SystemMapAttributionBinding(
                id=uuid.uuid4(),
                organisation_id=_ORG,
                pipeline_id=_PIPELINE,
                system_map_id=_MAP,
                node_key=_NODE_KEY,
                binding_kind="action_pipeline",
            )
        )


def _seed_run(db: AsyncSession, *, status: str = "pending", cost: Decimal | None = None) -> uuid.UUID:
    run_id = uuid.uuid4()
    db.add(
        Run(
            id=run_id,
            organisation_id=_ORG,
            pipeline_id=_PIPELINE,
            snapshot_id=uuid.uuid4(),
            trigger_type="cron",
            status=status,
            input_hash="a" * 64,
            langgraph_thread_id=f"{_ORG}:{run_id}",
            run_number=int(run_id.int % 10**9) + 1,
            total_cost_usd=cost,
        )
    )
    return run_id


def _seed_reservation(
    db: AsyncSession,
    *,
    run_id: uuid.UUID | None = None,
    status: str = "active",
    cooldown_until: datetime | None = None,
    reserved_budget_usd: Decimal = Decimal(1),
) -> SystemMapLoopReservation:
    reservation = SystemMapLoopReservation(
        id=uuid.uuid4(),
        organisation_id=_ORG,
        loop_config_id=_LOOP,
        run_id=run_id or uuid.uuid4(),
        idempotency_key=f"seed-{uuid.uuid4()}",
        cooldown_until=cooldown_until or _NOW + timedelta(minutes=5),
        month_start=_MONTH_START,
        reserved_budget_usd=reserved_budget_usd,
        status=status,
    )
    db.add(reservation)
    return reservation


class TestPureHelpers:
    def test_utc_month_start_is_the_calendar_month(self) -> None:
        assert loop_runtime.utc_month_start(datetime(2026, 11, 3, 23, 59, tzinfo=UTC)) == date(2026, 11, 1)

    def test_default_reserved_budget_splits_the_cap_across_slots(self) -> None:
        loop = SystemMapLoopConfig(
            organisation_id=_ORG,
            revision_id=_REV,
            node_key=_NODE_KEY,
            target_signal="signal",
            observation_window_seconds=60,
            action_pipeline_id=_PIPELINE,
            budget_limit_usd=Decimal(10),
            max_concurrent_runs=4,
            cooldown_seconds=0,
            outcome_metric="metric",
        )
        assert loop_runtime.default_reserved_budget(loop) == Decimal("2.500000")

    def test_window_key_is_stable_within_one_window(self) -> None:
        loop = SystemMapLoopConfig(
            id=_LOOP,
            organisation_id=_ORG,
            revision_id=_REV,
            node_key=_NODE_KEY,
            target_signal="signal",
            observation_window_seconds=300,
            action_pipeline_id=_PIPELINE,
            budget_limit_usd=Decimal(10),
            max_concurrent_runs=1,
            cooldown_seconds=0,
            outcome_metric="metric",
        )
        first = loop_runtime._window_key(loop, _NOW)
        same_window = loop_runtime._window_key(loop, _NOW + timedelta(seconds=10))
        next_window = loop_runtime._window_key(loop, _NOW + timedelta(seconds=301))
        assert first == same_window
        assert first != next_window


class TestAdmissionRejections:
    async def test_unpublished_revision_is_refused_and_audited(
        self, session: AsyncSession, audit_events: list[dict[str, Any]]
    ) -> None:
        _seed_loop(session, published=False)
        await session.flush()

        decision = await loop_runtime.admit_loop_run(
            session,
            organisation_id=_ORG,
            loop_config_id=_LOOP,
            admission_key="k1",
            now=_NOW,
        )

        assert decision.status == "rejected"
        assert decision.rejection_reason == "unpublished_revision"
        assert decision.run_id is None
        attempt = await session.get(SystemMapLoopAdmissionAttempt, decision.attempt_id)
        assert attempt is not None
        assert attempt.rejection_reason == "unpublished_revision"
        assert audit_events[0]["payload_json"]["decision"] == "rejected"
        assert audit_events[0]["payload_json"]["rejection_reason"] == "unpublished_revision"

    async def test_missing_attribution_binding_is_refused(self, session: AsyncSession) -> None:
        _seed_loop(session, binding=False)
        await session.flush()

        decision = await loop_runtime.admit_loop_run(
            session, organisation_id=_ORG, loop_config_id=_LOOP, admission_key="k1", now=_NOW
        )

        assert decision.rejection_reason == "attribution"

    async def test_manual_approval_autonomy_is_refused_without_a_run(self, session: AsyncSession) -> None:
        _seed_loop(session, autonomy="manual_approval")
        await session.flush()

        decision = await loop_runtime.admit_loop_run(
            session, organisation_id=_ORG, loop_config_id=_LOOP, admission_key="k1", now=_NOW
        )

        assert decision.rejection_reason == "autonomy"
        assert not (await session.execute(select(Run))).scalars().all()

    async def test_archived_action_pipeline_is_refused(self, session: AsyncSession) -> None:
        _seed_loop(session, pipeline_archived=True)
        await session.flush()

        decision = await loop_runtime.admit_loop_run(
            session, organisation_id=_ORG, loop_config_id=_LOOP, admission_key="k1", now=_NOW
        )

        assert decision.rejection_reason == "inactive_pipeline"

    async def test_cooldown_includes_released_reservations(self, session: AsyncSession) -> None:
        _seed_loop(session)
        await session.flush()
        _seed_reservation(session, status="released", cooldown_until=_NOW + timedelta(minutes=30))
        await session.flush()

        decision = await loop_runtime.admit_loop_run(
            session, organisation_id=_ORG, loop_config_id=_LOOP, admission_key="k1", now=_NOW
        )

        assert decision.rejection_reason == "cooldown"

    async def test_concurrency_limit_counts_live_reservations(self, session: AsyncSession) -> None:
        _seed_loop(session, max_concurrent_runs=1)
        await session.flush()
        run_id = _seed_run(session, status="running")
        _seed_reservation(session, run_id=run_id, status="active", cooldown_until=_NOW - timedelta(minutes=1))
        await session.flush()

        decision = await loop_runtime.admit_loop_run(
            session, organisation_id=_ORG, loop_config_id=_LOOP, admission_key="k1", now=_NOW
        )

        assert decision.rejection_reason == "concurrency"

    async def test_budget_rejection_is_deterministic_and_audited(
        self, session: AsyncSession, audit_events: list[dict[str, Any]]
    ) -> None:
        _seed_loop(session, max_concurrent_runs=2, budget_limit_usd=Decimal(1))
        await session.flush()
        run_id = _seed_run(session, status="complete", cost=Decimal("0.400000"))
        _seed_reservation(
            session,
            run_id=run_id,
            status="consumed",
            cooldown_until=_NOW - timedelta(minutes=1),
            reserved_budget_usd=Decimal("0.400000"),
        )
        await session.flush()

        decision = await loop_runtime.admit_loop_run(
            session,
            organisation_id=_ORG,
            loop_config_id=_LOOP,
            admission_key="k1",
            reserved_budget_usd=Decimal("0.700000"),
            now=_NOW,
        )

        assert decision.rejection_reason == "budget"
        assert audit_events[-1]["payload_json"]["rejection_reason"] == "budget"

    async def test_unknown_loop_config_raises(self, session: AsyncSession) -> None:
        _seed_loop(session)
        await session.flush()

        with pytest.raises(loop_runtime.LoopConfigNotFoundError):
            await loop_runtime.admit_loop_run(
                session,
                organisation_id=_ORG,
                loop_config_id=uuid.uuid4(),
                admission_key="k1",
                now=_NOW,
            )

    async def test_cross_tenant_loop_is_not_visible(self, session: AsyncSession) -> None:
        _seed_loop(session)
        await session.flush()

        with pytest.raises(loop_runtime.LoopConfigNotFoundError):
            await loop_runtime.admit_loop_run(
                session,
                organisation_id=_OTHER_ORG,
                loop_config_id=_LOOP,
                admission_key="k1",
                now=_NOW,
            )


class TestIdempotentReplay:
    async def test_same_key_replays_the_recorded_decision(self, session: AsyncSession) -> None:
        _seed_loop(session)
        await session.flush()
        attempt = SystemMapLoopAdmissionAttempt(
            organisation_id=_ORG,
            loop_config_id=_LOOP,
            idempotency_key="replay",
            status="rejected",
            rejection_reason="cooldown",
            month_start=_MONTH_START,
        )
        session.add(attempt)
        await session.flush()

        decision = await loop_runtime.admit_loop_run(
            session, organisation_id=_ORG, loop_config_id=_LOOP, admission_key="replay", now=_NOW
        )

        assert decision.replay is True
        assert decision.attempt_id == attempt.id
        assert decision.rejection_reason == "cooldown"
        attempts = (await session.execute(select(SystemMapLoopAdmissionAttempt))).scalars().all()
        assert len(attempts) == 1


class TestReservationSettlement:
    async def test_complete_run_consumes_with_observed_spend(self, session: AsyncSession) -> None:
        _seed_loop(session)
        await session.flush()
        run_id = _seed_run(session, status="running")
        reservation = _seed_reservation(session, run_id=run_id)
        await session.flush()

        assert await settle_loop_reservation_for_run(
            session,
            organisation_id=_ORG,
            run_id=run_id,
            run_status="complete",
            actual_spend_usd=Decimal("1.250000"),
        )

        await session.refresh(reservation)
        assert reservation.status == "consumed"
        assert reservation.actual_spend_usd == Decimal("1.250000")
        assert reservation.released_at is None

    async def test_failed_run_releases_and_keeps_the_cooldown(self, session: AsyncSession) -> None:
        _seed_loop(session)
        await session.flush()
        run_id = _seed_run(session, status="running")
        cooldown_until = _NOW + timedelta(minutes=15)
        reservation = _seed_reservation(session, run_id=run_id, cooldown_until=cooldown_until)
        await session.flush()

        assert await settle_loop_reservation_for_run(
            session,
            organisation_id=_ORG,
            run_id=run_id,
            run_status="failed",
            actual_spend_usd=Decimal("0.500000"),
        )

        await session.refresh(reservation)
        assert reservation.status == "released"
        assert reservation.released_at is not None
        assert reservation.cooldown_until.replace(tzinfo=UTC) == cooldown_until
        # Still suppressing admission: the quiet period outlives the release.
        assert (
            await loop_runtime._REPO.get_loop_cooldown_deadline(
                session, organisation_id=_ORG, loop_config_id=_LOOP, now=_NOW
            )
            is not None
        )

    async def test_settlement_is_idempotent(self, session: AsyncSession) -> None:
        _seed_loop(session)
        await session.flush()
        run_id = _seed_run(session, status="running")
        _seed_reservation(session, run_id=run_id)
        await session.flush()

        assert await settle_loop_reservation_for_run(
            session, organisation_id=_ORG, run_id=run_id, run_status="complete", actual_spend_usd=None
        )
        assert not await settle_loop_reservation_for_run(
            session, organisation_id=_ORG, run_id=run_id, run_status="complete", actual_spend_usd=None
        )

    async def test_non_terminal_status_is_a_noop(self, session: AsyncSession) -> None:
        _seed_loop(session)
        await session.flush()
        run_id = _seed_run(session, status="running")
        _seed_reservation(session, run_id=run_id)
        await session.flush()

        assert not await settle_loop_reservation_for_run(
            session, organisation_id=_ORG, run_id=run_id, run_status="running", actual_spend_usd=None
        )

    async def test_reconcile_settles_terminal_runs(self, session: AsyncSession) -> None:
        _seed_loop(session)
        await session.flush()
        complete_run = _seed_run(session, status="complete", cost=Decimal(2))
        failed_run = _seed_run(session, status="failed")
        active_run = _seed_run(session, status="running")
        _seed_reservation(session, run_id=complete_run)
        _seed_reservation(session, run_id=failed_run)
        _seed_reservation(session, run_id=active_run)
        await session.flush()

        assert await loop_runtime.reconcile_loop_reservations(session, organisation_id=_ORG) == 2

        statuses = {
            row.run_id: row.status for row in (await session.execute(select(SystemMapLoopReservation))).scalars().all()
        }
        assert statuses[complete_run] == "consumed"
        assert statuses[failed_run] == "released"
        assert statuses[active_run] == "active"


class TestDueLoopSelection:
    async def test_not_due_inside_the_observation_window(self, session: AsyncSession) -> None:
        _seed_loop(session, observation_window_seconds=3600)
        await session.flush()
        session.add(
            SystemMapLoopAdmissionAttempt(
                organisation_id=_ORG,
                loop_config_id=_LOOP,
                idempotency_key="previous",
                status="rejected",
                rejection_reason="cooldown",
                month_start=_MONTH_START,
                created_at=_NOW - timedelta(minutes=5),
            )
        )
        await session.flush()
        loop = (await session.execute(select(SystemMapLoopConfig))).scalar_one()

        assert not await loop_runtime._loop_is_due(session, organisation_id=_ORG, loop=loop, moment=_NOW)

    async def test_due_once_the_window_elapsed_with_no_live_capacity(self, session: AsyncSession) -> None:
        _seed_loop(session, observation_window_seconds=60)
        await session.flush()
        loop = (await session.execute(select(SystemMapLoopConfig))).scalar_one()

        assert await loop_runtime._loop_is_due(session, organisation_id=_ORG, loop=loop, moment=_NOW)

    async def test_dispatch_due_loops_admits_and_dispatches(
        self, session: AsyncSession, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _seed_loop(session, observation_window_seconds=60, cooldown_seconds=0)
        await session.commit()
        factory = async_sessionmaker(session.bind, expire_on_commit=False)
        run_id = uuid.uuid4()

        async def _fake_admit(db: AsyncSession, **kwargs: Any) -> loop_runtime.LoopAdmissionDecision:
            return loop_runtime.LoopAdmissionDecision(
                loop_config_id=_LOOP,
                attempt_id=uuid.uuid4(),
                status="admitted",
                run_id=run_id,
                admission_key=kwargs["admission_key"],
            )

        dispatched: list[uuid.UUID] = []

        async def _fake_dispatch(rid: uuid.UUID, organisation_id: uuid.UUID) -> str:
            dispatched.append(rid)
            return "enqueued"

        async def _enabled(_org: uuid.UUID) -> bool:
            return True

        monkeypatch.setattr(loop_runtime, "admit_loop_run", _fake_admit)
        monkeypatch.setattr(loop_runtime, "_dispatch_run", _fake_dispatch)
        monkeypatch.setattr(loop_runtime, "_system_maps_enabled", _enabled)

        summary = await loop_runtime.dispatch_due_loops(session_factory=factory, now=_NOW)

        assert summary["admitted"] == 1
        assert summary["dispatched"] == 1
        assert dispatched == [run_id]
