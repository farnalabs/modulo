"""Integration: System Maps improvement-loop admission over real Postgres (FAR-2349).

The unit suite proves the gate predicates against SQLite. This module proves the
properties that only a real database can: the admission transaction's row locks
serialise two simultaneous admissions (exactly one wins a single slot), the
attempt/reservation/run triple commits atomically, a terminal write settles the
reservation, and the cooldown boundary survives a release.
"""

from __future__ import annotations

import asyncio
import uuid
from datetime import UTC, datetime
from decimal import Decimal
from unittest.mock import MagicMock

import pytest
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from modulo.core import system_map_loop_admission as loop_runtime
from modulo.db.crud.run import update_run_status
from modulo.db.models.account import Account
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
from modulo.db.repositories.system_map import SystemMapRepository, settle_loop_reservation_for_run

pytestmark = pytest.mark.integration

_NODE_KEY = "improve_architecture"
_REPO = SystemMapRepository(session_factory=MagicMock())


async def _seed_loop(
    engine: AsyncEngine,
    *,
    autonomy: str = "notify_on_complete",
    published: bool = True,
    max_concurrent_runs: int = 1,
    budget_limit_usd: Decimal = Decimal(10),
    cooldown_seconds: int = 0,
    with_binding: bool = True,
) -> dict[str, uuid.UUID]:
    """Commit one org + published map + loop config + attribution binding."""
    org_id = uuid.uuid4()
    account_id = uuid.uuid4()
    pipeline_id = uuid.uuid4()
    map_id = uuid.uuid4()
    revision_id = uuid.uuid4()
    loop_config_id = uuid.uuid4()
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as session, session.begin():
        session.add(Organisation(id=org_id, name=f"Loop Org {org_id.hex[:8]}", slug=f"loop-{org_id.hex[:8]}"))
        session.add(
            Account(
                id=account_id,
                email=f"loop-{org_id.hex[:8]}@example.com",
                display_name="Loop Owner",
            )
        )
        await session.flush()
        session.add(
            Pipeline(
                id=pipeline_id,
                organisation_id=org_id,
                name="Improve Architecture",
                account_id=account_id,
                max_concurrent_runs=5,
                default_autonomy_level=autonomy,
            )
        )
        system_map = SystemMap(
            id=map_id,
            organisation_id=org_id,
            name=f"Loop map {map_id.hex[:8]}",
            account_id=account_id,
        )
        session.add(system_map)
        await session.flush()
        session.add(
            SystemMapRevision(
                id=revision_id,
                organisation_id=org_id,
                system_map_id=map_id,
                revision_number=1,
                status="published" if published else "draft",
                content_json={"nodes": []},
                published_at=datetime.now(UTC) if published else None,
            )
        )
        await session.flush()
        if published:
            system_map.published_revision_id = revision_id
            await session.flush()
        session.add(
            SystemMapLoopConfig(
                id=loop_config_id,
                organisation_id=org_id,
                revision_id=revision_id,
                node_key=_NODE_KEY,
                target_signal="uncovered_architecture_gaps",
                observation_window_seconds=60,
                action_pipeline_id=pipeline_id,
                budget_limit_usd=budget_limit_usd,
                max_concurrent_runs=max_concurrent_runs,
                cooldown_seconds=cooldown_seconds,
                outcome_metric="delta_test_failures",
            )
        )
        if with_binding:
            session.add(
                SystemMapAttributionBinding(
                    id=uuid.uuid4(),
                    organisation_id=org_id,
                    pipeline_id=pipeline_id,
                    system_map_id=map_id,
                    node_key=_NODE_KEY,
                    binding_kind="action_pipeline",
                )
            )
    return {
        "org_id": org_id,
        "account_id": account_id,
        "pipeline_id": pipeline_id,
        "map_id": map_id,
        "revision_id": revision_id,
        "loop_config_id": loop_config_id,
    }


async def _admit(
    factory: async_sessionmaker[AsyncSession],
    seed: dict[str, uuid.UUID],
    *,
    key: str,
    reserved_budget_usd: Decimal | None = None,
    now: datetime | None = None,
) -> loop_runtime.LoopAdmissionDecision:
    async with factory() as session, session.begin():
        return await loop_runtime.admit_loop_run(
            session,
            organisation_id=seed["org_id"],
            loop_config_id=seed["loop_config_id"],
            admission_key=key,
            reserved_budget_usd=reserved_budget_usd,
            now=now,
        )


async def _reservations(factory: async_sessionmaker[AsyncSession], org_id: uuid.UUID) -> list[SystemMapLoopReservation]:
    async with factory() as session:
        return list(
            (
                await session.execute(
                    select(SystemMapLoopReservation).where(SystemMapLoopReservation.organisation_id == org_id)
                )
            )
            .scalars()
            .all()
        )


async def _runs(factory: async_sessionmaker[AsyncSession], org_id: uuid.UUID) -> list[Run]:
    async with factory() as session:
        return list((await session.execute(select(Run).where(Run.organisation_id == org_id))).scalars().all())


async def test_admission_creates_run_attempt_and_reservation(db_engine: AsyncEngine) -> None:
    seed = await _seed_loop(db_engine)
    factory = async_sessionmaker(db_engine, expire_on_commit=False)

    decision = await _admit(factory, seed, key="window:1")

    assert decision.admitted
    assert decision.rejection_reason is None
    runs = await _runs(factory, seed["org_id"])
    assert [run.id for run in runs] == [decision.run_id]
    assert runs[0].status == "pending"
    reservations = await _reservations(factory, seed["org_id"])
    assert len(reservations) == 1
    assert reservations[0].status == "active"
    assert reservations[0].reserved_budget_usd == Decimal("10.000000")
    assert reservations[0].month_start == datetime.now(UTC).date().replace(day=1)
    async with factory() as session:
        attempt = await session.get(SystemMapLoopAdmissionAttempt, decision.attempt_id)
    assert attempt is not None
    assert attempt.status == "admitted"
    assert attempt.run_id == decision.run_id


async def test_two_simultaneous_admissions_cannot_both_win_single_slot(
    db_engine: AsyncEngine,
) -> None:
    seed = await _seed_loop(db_engine, max_concurrent_runs=1, cooldown_seconds=0)
    factory = async_sessionmaker(db_engine, expire_on_commit=False)

    results = await asyncio.gather(
        _admit(factory, seed, key="concurrent:a"),
        _admit(factory, seed, key="concurrent:b"),
    )

    statuses = sorted(decision.status for decision in results)
    assert statuses == ["admitted", "rejected"]
    rejected = next(decision for decision in results if decision.status == "rejected")
    assert rejected.rejection_reason == "concurrency"
    assert len(await _runs(factory, seed["org_id"])) == 1
    assert len(await _reservations(factory, seed["org_id"])) == 1


async def test_idempotent_replay_does_not_double_run(db_engine: AsyncEngine) -> None:
    seed = await _seed_loop(db_engine)
    factory = async_sessionmaker(db_engine, expire_on_commit=False)

    first = await _admit(factory, seed, key="window:7")
    replay = await _admit(factory, seed, key="window:7")

    assert first.admitted
    assert replay.replay is True
    assert replay.run_id == first.run_id
    assert replay.reservation_id == first.reservation_id
    assert len(await _runs(factory, seed["org_id"])) == 1
    assert len(await _reservations(factory, seed["org_id"])) == 1


async def test_budget_rejection_is_deterministic_and_auditable(db_engine: AsyncEngine) -> None:
    seed = await _seed_loop(db_engine, max_concurrent_runs=2, budget_limit_usd=Decimal(1), cooldown_seconds=0)
    factory = async_sessionmaker(db_engine, expire_on_commit=False)

    first = await _admit(factory, seed, key="budget:a", reserved_budget_usd=Decimal("0.600000"))
    second = await _admit(factory, seed, key="budget:b", reserved_budget_usd=Decimal("0.600000"))

    assert first.admitted
    assert second.status == "rejected"
    assert second.rejection_reason == "budget"
    async with factory() as session:
        attempt = await session.get(SystemMapLoopAdmissionAttempt, second.attempt_id)
    assert attempt is not None
    assert attempt.rejection_reason == "budget"
    assert attempt.run_id is None
    assert len(await _runs(factory, seed["org_id"])) == 1


async def test_autonomy_refused_loop_is_never_dispatched(db_engine: AsyncEngine) -> None:
    seed = await _seed_loop(db_engine, autonomy="manual_approval")
    factory = async_sessionmaker(db_engine, expire_on_commit=False)

    decision = await _admit(factory, seed, key="autonomy:1")

    assert decision.status == "rejected"
    assert decision.rejection_reason == "autonomy"
    assert not await _runs(factory, seed["org_id"])


async def test_missing_attribution_binding_is_refused(db_engine: AsyncEngine) -> None:
    seed = await _seed_loop(db_engine, with_binding=False)
    factory = async_sessionmaker(db_engine, expire_on_commit=False)

    decision = await _admit(factory, seed, key="attribution:1")

    assert decision.rejection_reason == "attribution"
    assert not await _runs(factory, seed["org_id"])


async def test_unpublished_revision_is_refused(db_engine: AsyncEngine) -> None:
    seed = await _seed_loop(db_engine, published=False)
    factory = async_sessionmaker(db_engine, expire_on_commit=False)

    decision = await _admit(factory, seed, key="unpublished:1")

    assert decision.rejection_reason == "unpublished_revision"
    assert not await _runs(factory, seed["org_id"])


async def test_cooldown_survives_cancellation(db_engine: AsyncEngine) -> None:
    seed = await _seed_loop(db_engine, cooldown_seconds=3600)
    factory = async_sessionmaker(db_engine, expire_on_commit=False)

    admitted = await _admit(factory, seed, key="cooldown:1")
    assert admitted.admitted
    run_id = admitted.run_id
    assert run_id is not None

    # Cancelling the run releases capacity but must not clear the quiet period.
    async with factory() as session, session.begin():
        released = await settle_loop_reservation_for_run(
            session,
            organisation_id=seed["org_id"],
            run_id=run_id,
            run_status="cancelled",
        )
    assert released

    reservations = await _reservations(factory, seed["org_id"])
    assert reservations[0].status == "released"
    assert reservations[0].cooldown_until > datetime.now(UTC)

    second = await _admit(factory, seed, key="cooldown:2")
    assert second.status == "rejected"
    assert second.rejection_reason == "cooldown"


async def test_complete_run_consumes_reservation_via_terminal_write(
    db_engine: AsyncEngine,
) -> None:
    seed = await _seed_loop(db_engine)
    factory = async_sessionmaker(db_engine, expire_on_commit=False)

    decision = await _admit(factory, seed, key="settle:complete")
    run_id = decision.run_id
    assert run_id is not None

    async with factory() as session, session.begin():
        updated = await update_run_status(session, run_id, "complete", total_cost_usd=Decimal("1.250000"))
    assert updated is not None
    assert updated.status == "complete"

    reservations = await _reservations(factory, seed["org_id"])
    assert reservations[0].status == "consumed"
    assert reservations[0].actual_spend_usd == Decimal("1.250000")


async def test_failed_run_releases_reservation_via_terminal_write(
    db_engine: AsyncEngine,
) -> None:
    seed = await _seed_loop(db_engine, cooldown_seconds=600)
    factory = async_sessionmaker(db_engine, expire_on_commit=False)

    decision = await _admit(factory, seed, key="settle:failed")
    run_id = decision.run_id
    assert run_id is not None
    cooldown_before = (await _reservations(factory, seed["org_id"]))[0].cooldown_until

    async with factory() as session, session.begin():
        updated = await update_run_status(session, run_id, "failed", error_code="node.failed")
    assert updated is not None

    reservations = await _reservations(factory, seed["org_id"])
    assert reservations[0].status == "released"
    assert reservations[0].released_at is not None
    assert reservations[0].cooldown_until == cooldown_before


async def test_cross_tenant_loop_is_isolated(db_engine: AsyncEngine) -> None:
    seed = await _seed_loop(db_engine)
    other_org = uuid.uuid4()
    factory = async_sessionmaker(db_engine, expire_on_commit=False)

    async with factory() as session, session.begin():
        session.add(Organisation(id=other_org, name=f"Other {other_org.hex[:8]}", slug=f"other-{other_org.hex[:8]}"))

    with pytest.raises(loop_runtime.LoopConfigNotFoundError):
        async with factory() as session, session.begin():
            await loop_runtime.admit_loop_run(
                session,
                organisation_id=other_org,
                loop_config_id=seed["loop_config_id"],
                admission_key="cross-tenant",
            )

    async with factory() as session:
        usage = await _REPO.get_loop_month_usage(
            session,
            organisation_id=other_org,
            loop_config_id=seed["loop_config_id"],
            month_start=datetime.now(UTC).date().replace(day=1),
        )
    assert usage == Decimal(0)
    assert not await _runs(factory, other_org)


async def test_reconcile_settles_terminal_runs_after_admission(db_engine: AsyncEngine) -> None:
    seed = await _seed_loop(db_engine)
    factory = async_sessionmaker(db_engine, expire_on_commit=False)

    decision = await _admit(factory, seed, key="reconcile:1")
    assert decision.admitted
    run_id = decision.run_id
    assert run_id is not None

    # A direct terminal write (bypassing update_run_status, so bypassing the
    # inline settlement hook) is exactly the out-of-band case the sweep exists
    # for.
    async with factory() as session, session.begin():
        await session.execute(
            update(Run).where(Run.id == run_id).values(status="complete", total_cost_usd=Decimal("2.000000"))
        )

    async with factory() as session, session.begin():
        settled = await loop_runtime.reconcile_loop_reservations(session, organisation_id=seed["org_id"])
    assert settled == 1

    reservations = await _reservations(factory, seed["org_id"])
    assert reservations[0].status == "consumed"
    assert reservations[0].actual_spend_usd == Decimal("2.000000")
