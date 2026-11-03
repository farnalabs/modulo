"""Integration: the System Maps overview aggregations over real Postgres (FAR-2263).

The unit suite runs these two repository methods against in-memory SQLite, which
proves the predicates but not the production dialect. This module round-trips
the aggregation SQL — the JSON-free column set, the half-open ``created_at``
window, the ``CASE``-based counting and the reservation/fact join — against
Postgres with seeded rows, including the cross-organisation boundary.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from unittest.mock import MagicMock

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from modulo.db.models.account import Account
from modulo.db.models.organisation import Organisation
from modulo.db.models.pipeline import Pipeline
from modulo.db.models.pipeline_snapshot import PipelineSnapshot
from modulo.db.models.run import Run
from modulo.db.models.run_daily_facts import RunDailyFact
from modulo.db.models.system_map import (
    SystemMap,
    SystemMapLoopAdmissionAttempt,
    SystemMapLoopConfig,
    SystemMapLoopReservation,
    SystemMapRevision,
)
from modulo.db.repositories.system_map import SystemMapRepository
from modulo.db.rls import set_rls_org, set_rls_user_context

pytestmark = pytest.mark.integration

_REPO = SystemMapRepository(session_factory=MagicMock())
_WINDOW_END = datetime.now(UTC).replace(microsecond=0)
_WINDOW_START = _WINDOW_END - timedelta(days=7)


async def _seed(session: AsyncSession) -> dict[str, uuid.UUID]:
    org_id = uuid.uuid4()
    account_id = uuid.uuid4()
    pipeline_id = uuid.uuid4()
    map_id = uuid.uuid4()
    revision_id = uuid.uuid4()
    loop_config_id = uuid.uuid4()
    snapshot_id = uuid.uuid4()

    session.add(Organisation(id=org_id, name=f"Overview {org_id.hex[:8]}", slug=f"overview-{org_id.hex[:8]}"))
    session.add(Account(id=account_id, email=f"overview-{org_id.hex[:8]}@example.com", display_name="Overview"))
    await session.flush()

    session.add(
        Pipeline(
            id=pipeline_id,
            organisation_id=org_id,
            name="overview pipeline",
            account_id=account_id,
            max_concurrent_runs=4,
        )
    )
    session.add(
        PipelineSnapshot(
            id=snapshot_id,
            organisation_id=org_id,
            pipeline_id=pipeline_id,
            snapshot_version=1,
            graph_json={"nodes": []},
            connector_bindings_json=[],
            schema_pins_json=[],
            prompt_pins_json=[],
            model_backend_pins_json=[],
            config_json={},
            run_context_defaults={},
        )
    )
    await session.flush()

    session.add(
        SystemMap(
            id=map_id,
            organisation_id=org_id,
            name="overview map",
            account_id=account_id,
            default_map=True,
            visibility="org",
        )
    )
    await session.flush()
    session.add(
        SystemMapRevision(
            id=revision_id,
            organisation_id=org_id,
            system_map_id=map_id,
            revision_number=1,
            status="published",
            content_json={"nodes": []},
            published_at=_WINDOW_START,
        )
    )
    await session.flush()
    session.add(
        SystemMapLoopConfig(
            id=loop_config_id,
            organisation_id=org_id,
            revision_id=revision_id,
            node_key="loop-1",
            target_signal="error_rate",
            observation_window_seconds=300,
            action_pipeline_id=pipeline_id,
            budget_limit_usd=Decimal("10.000000"),
            cooldown_seconds=3600,
            outcome_metric="error_rate",
        )
    )
    await session.flush()

    return {
        "org_id": org_id,
        "account_id": account_id,
        "pipeline_id": pipeline_id,
        "map_id": map_id,
        "revision_id": revision_id,
        "loop_config_id": loop_config_id,
        "snapshot_id": snapshot_id,
    }


def _add_fact(
    session: AsyncSession,
    *,
    org_id: uuid.UUID,
    pipeline_id: uuid.UUID,
    status: str,
    created_at: datetime,
    benign: bool | None = None,
    telemetry_bytes: int | None = None,
    total_cost_usd: Decimal | None = None,
    run_id: uuid.UUID | None = None,
) -> uuid.UUID:
    fact_run_id = run_id or uuid.uuid4()
    session.add(
        RunDailyFact(
            run_id=fact_run_id,
            organisation_id=org_id,
            pipeline_id=pipeline_id,
            run_date=created_at.date(),
            trigger_type="manual",
            status=status,
            benign=benign,
            telemetry_bytes=telemetry_bytes,
            total_cost_usd=total_cost_usd,
            completed_at=created_at,
            created_at=created_at,
        )
    )
    return fact_run_id


def _add_run(
    session: AsyncSession,
    *,
    org_id: uuid.UUID,
    pipeline_id: uuid.UUID,
    snapshot_id: uuid.UUID,
    status: str = "pending",
    cancellation_requested: bool = False,
) -> uuid.UUID:
    run_id = uuid.uuid4()
    session.add(
        Run(
            id=run_id,
            organisation_id=org_id,
            pipeline_id=pipeline_id,
            snapshot_id=snapshot_id,
            trigger_type="manual",
            status=status,
            run_number=uuid.uuid4().int % 2_000_000_000,
            input_hash="a" * 64,
            langgraph_thread_id=f"thread-{run_id.hex}",
            cancellation_requested=cancellation_requested,
        )
    )
    return run_id


async def test_pipeline_metrics_over_postgres(db_session: AsyncSession) -> None:
    seeded = await _seed(db_session)
    org_id = seeded["org_id"]
    pipeline_id = seeded["pipeline_id"]
    await set_rls_org(db_session, org_id)
    await set_rls_user_context(db_session, seeded["account_id"], "admin")

    # Inside the half-open window: the boundary row is INCLUDED.
    _add_fact(
        db_session,
        org_id=org_id,
        pipeline_id=pipeline_id,
        status="complete",
        created_at=_WINDOW_START,
        telemetry_bytes=0,
        total_cost_usd=Decimal("1.000000"),
    )
    _add_fact(
        db_session,
        org_id=org_id,
        pipeline_id=pipeline_id,
        status="failed",
        created_at=_WINDOW_START + timedelta(hours=1),
        telemetry_bytes=128,
    )
    _add_fact(
        db_session,
        org_id=org_id,
        pipeline_id=pipeline_id,
        status="stalled",
        created_at=_WINDOW_START + timedelta(hours=2),
    )
    # Deliberate outcomes are not failures.
    _add_fact(
        db_session,
        org_id=org_id,
        pipeline_id=pipeline_id,
        status="cancelled",
        created_at=_WINDOW_START + timedelta(hours=3),
    )
    _add_fact(
        db_session,
        org_id=org_id,
        pipeline_id=pipeline_id,
        status="rejected",
        created_at=_WINDOW_START + timedelta(hours=4),
    )
    # A benign failure never counts against the pipeline.
    _add_fact(
        db_session,
        org_id=org_id,
        pipeline_id=pipeline_id,
        status="failed",
        benign=True,
        created_at=_WINDOW_START + timedelta(hours=5),
    )
    # The exclusive boundary row is EXCLUDED.
    _add_fact(
        db_session,
        org_id=org_id,
        pipeline_id=pipeline_id,
        status="complete",
        created_at=_WINDOW_END,
    )

    _add_run(db_session, org_id=org_id, pipeline_id=pipeline_id, snapshot_id=seeded["snapshot_id"])
    _add_run(db_session, org_id=org_id, pipeline_id=pipeline_id, snapshot_id=seeded["snapshot_id"])
    _add_run(
        db_session,
        org_id=org_id,
        pipeline_id=pipeline_id,
        snapshot_id=seeded["snapshot_id"],
        cancellation_requested=True,
    )
    await db_session.flush()

    metrics = await _REPO.get_overview_pipeline_metrics(
        db_session,
        organisation_id=org_id,
        pipeline_ids={pipeline_id},
        window_start=_WINDOW_START,
        window_end=_WINDOW_END,
    )

    item = metrics[pipeline_id]
    assert item["run_count"] == 6
    # 5 non-benign runs, 2 canonical failures (failed + stalled).
    assert item["failure_rate"] == pytest.approx(0.4)
    # 2 of 6 rows carried an observed telemetry byte count (0 is observed).
    assert item["telemetry_coverage"] == pytest.approx(2 / 6)
    assert item["queue_depth"] == 2
    assert item["capacity_pressure"] == pytest.approx(0.5)


async def test_pipeline_metrics_are_organisation_scoped(db_session: AsyncSession) -> None:
    seeded = await _seed(db_session)
    other = await _seed(db_session)
    await set_rls_org(db_session, seeded["org_id"])
    await set_rls_user_context(db_session, seeded["account_id"], "admin")

    _add_fact(
        db_session,
        org_id=seeded["org_id"],
        pipeline_id=seeded["pipeline_id"],
        status="failed",
        created_at=_WINDOW_START,
    )
    await db_session.flush()

    # The other organisation's pipeline id is never returned for this caller.
    metrics = await _REPO.get_overview_pipeline_metrics(
        db_session,
        organisation_id=seeded["org_id"],
        pipeline_ids={seeded["pipeline_id"], other["pipeline_id"]},
        window_start=_WINDOW_START,
        window_end=_WINDOW_END,
    )

    assert set(metrics) == {seeded["pipeline_id"]}


async def test_loop_metrics_over_postgres(db_session: AsyncSession) -> None:
    seeded = await _seed(db_session)
    org_id = seeded["org_id"]
    loop_config_id = seeded["loop_config_id"]
    await set_rls_org(db_session, org_id)
    await set_rls_user_context(db_session, seeded["account_id"], "admin")

    # A reservation's run_id is a real FK to runs (same organisation), so the
    # reservation runs are created first and the facts reference them.
    consumed_run = _add_run(
        db_session, org_id=org_id, pipeline_id=seeded["pipeline_id"], snapshot_id=seeded["snapshot_id"]
    )
    unobserved_run = _add_run(
        db_session, org_id=org_id, pipeline_id=seeded["pipeline_id"], snapshot_id=seeded["snapshot_id"]
    )
    released_run = _add_run(
        db_session, org_id=org_id, pipeline_id=seeded["pipeline_id"], snapshot_id=seeded["snapshot_id"]
    )
    _add_fact(
        db_session,
        org_id=org_id,
        pipeline_id=seeded["pipeline_id"],
        status="complete",
        created_at=_WINDOW_START,
        total_cost_usd=Decimal("1.500000"),
        run_id=consumed_run,
    )
    _add_fact(
        db_session,
        org_id=org_id,
        pipeline_id=seeded["pipeline_id"],
        status="complete",
        created_at=_WINDOW_START,
        total_cost_usd=None,
        run_id=unobserved_run,
    )
    _add_fact(
        db_session,
        org_id=org_id,
        pipeline_id=seeded["pipeline_id"],
        status="complete",
        created_at=_WINDOW_START,
        total_cost_usd=Decimal("9.000000"),
        run_id=released_run,
    )
    cooldown_until = _WINDOW_END + timedelta(hours=2)
    db_session.add(
        SystemMapLoopReservation(
            organisation_id=org_id,
            loop_config_id=loop_config_id,
            run_id=consumed_run,
            idempotency_key=f"consumed-{uuid.uuid4().hex}",
            reserved_at=_WINDOW_START + timedelta(hours=1),
            cooldown_until=_WINDOW_END - timedelta(minutes=1),
            status="consumed",
        )
    )
    db_session.add(
        SystemMapLoopReservation(
            organisation_id=org_id,
            loop_config_id=loop_config_id,
            run_id=unobserved_run,
            idempotency_key=f"active-{uuid.uuid4().hex}",
            reserved_at=_WINDOW_START + timedelta(hours=2),
            cooldown_until=cooldown_until,
            status="active",
        )
    )
    db_session.add(
        SystemMapLoopReservation(
            organisation_id=org_id,
            loop_config_id=loop_config_id,
            run_id=released_run,
            idempotency_key=f"released-{uuid.uuid4().hex}",
            reserved_at=_WINDOW_START + timedelta(hours=3),
            cooldown_until=_WINDOW_END + timedelta(hours=4),
            status="released",
        )
    )
    db_session.add(
        SystemMapLoopAdmissionAttempt(
            organisation_id=org_id,
            loop_config_id=loop_config_id,
            idempotency_key=f"admitted-{uuid.uuid4().hex}",
            status="admitted",
            created_at=_WINDOW_END - timedelta(hours=1),
        )
    )
    await db_session.flush()

    metrics = await _REPO.get_overview_loop_metrics(
        db_session,
        organisation_id=org_id,
        loop_config_ids={loop_config_id},
        window_start=_WINDOW_START,
        window_end=_WINDOW_END,
    )

    item = metrics[loop_config_id]
    assert item["last_admission_status"] == "admitted"
    # One contributing reservation has no observed cost, so the total is unknown.
    assert item["budget_used_usd"] is None
    # The released reservation does not contribute its 9.00.
    assert item["cooldown_active"] is True
    assert item["cooldown_until"] == cooldown_until
    # No budget period is recorded, so the window is not reported as one.
    assert "budget_period_start" not in item
