"""FAR-795: ``list_journey_runs`` JSONB containment against REAL Postgres.

The unit suite exercises this path on SQLite via the portable Python scan,
which cannot prove the Postgres jsonb-containment predicate actually matches
the STORED entry shape — a predicate/shape mismatch would silently return an
empty history while unit tests stay green. These tests drive the real
Postgres path (dialect-detected containment, the same operator class the
``ix_runs_work_item_refs_gin`` partial GIN index serves) and assert the
mixing run IS surfaced — never asserting only the empty side.

Sessions run as the production ``modulo_app`` role with RLS org context set
to a dedicated ``journey_org`` (not the shared ``test_org``), mirroring the
REST route posture while keeping seeded ``run_number`` values collision-free.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncGenerator
from datetime import UTC, datetime
from typing import Any

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from modulo.core.lifecycle_map.journeys import list_journey_runs
from modulo.db.lifecycle_refs import canonical_work_item_id
from modulo.db.models.journey import Journey
from modulo.db.models.run import Run
from modulo.db.rls import set_rls_org

pytestmark = pytest.mark.integration


@pytest_asyncio.fixture(scope="module")
async def journey_org(db_engine: AsyncEngine) -> uuid.UUID:
    """Dedicated committed org so seeded ``run_number`` values never collide
    with runs other integration tests commit under the shared ``test_org``
    (the ``uq_runs_org_run_number`` unique constraint rejects duplicates)."""
    org_id = uuid.uuid4()
    async with db_engine.connect() as conn, conn.begin():
        await conn.execute(
            text(
                "INSERT INTO organisations (id, name, slug, settings_json) VALUES (:id, :name, :slug, '{}'::json)",
            ),
            {"id": str(org_id), "name": "Journey Containment Org", "slug": f"journey-{org_id.hex[:8]}"},
        )
    return org_id


@pytest_asyncio.fixture(scope="module")
async def journey_pipeline(
    db_engine: AsyncEngine,
    journey_org: uuid.UUID,
    test_user: uuid.UUID,
) -> uuid.UUID:
    """Committed pipeline in ``journey_org`` for run foreign keys."""
    pipeline_id = uuid.uuid4()
    async with db_engine.connect() as conn, conn.begin():
        await conn.execute(
            text(
                "INSERT INTO pipelines (id, organisation_id, name, account_id, "
                "max_concurrent_runs, lock_wait_timeout_seconds, node_timeout_seconds, "
                "run_context_defaults, graph_nodes_json) "
                "VALUES (:id, :oid, :name, :uid, 10, 30, 300, '{}'::json, '[]'::json)",
            ),
            {
                "id": str(pipeline_id),
                "oid": str(journey_org),
                "name": "Journey Containment Pipeline",
                "uid": str(test_user),
            },
        )
    return pipeline_id


@pytest_asyncio.fixture(scope="module")
async def journey_snapshot(
    db_engine: AsyncEngine,
    journey_org: uuid.UUID,
    journey_pipeline: uuid.UUID,
) -> uuid.UUID:
    """Committed pipeline_snapshot in ``journey_org`` for run foreign keys."""
    snapshot_id = uuid.uuid4()
    async with db_engine.connect() as conn, conn.begin():
        await conn.execute(
            text(
                "INSERT INTO pipeline_snapshots (id, pipeline_id, organisation_id, "
                "snapshot_version, graph_json, connector_bindings_json, "
                "schema_pins_json, prompt_pins_json, model_backend_pins_json, "
                "run_context_defaults, config_json) "
                "VALUES (:id, :pid, :oid, 1, '{}'::json, '[]'::json, "
                "'[]'::json, '[]'::json, '[]'::json, '{}'::json, '{}'::json)",
            ),
            {"id": str(snapshot_id), "pid": str(journey_pipeline), "oid": str(journey_org)},
        )
    return snapshot_id


@pytest_asyncio.fixture
async def app_role_rls_session(
    modulo_app_engine: AsyncEngine,
    journey_org: uuid.UUID,
) -> AsyncGenerator[AsyncSession, None]:
    """AsyncSession as the production ``modulo_app`` role (RLS enforced)."""
    factory = async_sessionmaker(modulo_app_engine, expire_on_commit=False)
    async with factory() as session:
        await session.begin()
        await set_rls_org(session, journey_org)
        yield session
        await session.rollback()


async def _seed_run(
    session: AsyncSession,
    *,
    org_id: uuid.UUID,
    pipeline_id: uuid.UUID,
    snapshot_id: uuid.UUID,
    work_item_refs: list[dict[str, Any]],
    completed_at: datetime,
    run_number: int,
) -> Run:
    run = Run(
        id=uuid.uuid4(),
        organisation_id=org_id,
        pipeline_id=pipeline_id,
        snapshot_id=snapshot_id,
        trigger_type="manual",
        status="complete",
        run_number=run_number,
        input_hash="a" * 64,
        langgraph_thread_id=f"{org_id}:{uuid.uuid4()}",
        work_item_refs=work_item_refs,
        completed_at=completed_at,
    )
    session.add(run)
    await session.flush()
    return run


def _refs(*, kind: str, ref: str, source: str = "derived", **extra: Any) -> list[dict[str, Any]]:
    return [{"kind": kind, "ref": ref, "source": source, **extra}]


async def test_containment_predicate_surfaces_the_mixing_run(
    app_role_rls_session: AsyncSession,
    journey_org: uuid.UUID,
    journey_pipeline: uuid.UUID,
    journey_snapshot: uuid.UUID,
) -> None:
    """The containment predicate MUST match the stored entry shape: entries
    with the validate_ref_entry keys (kind/ref/source + optional extras) and
    mid-array position are still matched; other journeys' runs do not leak."""
    journey = Journey(
        organisation_id=journey_org,
        kind="jira",
        ref="PAY-77",
        canonical_work_item_id=canonical_work_item_id(journey_org, "jira", "PAY-77"),
        latest_status="complete",
        latest_provenance="derived",
    )
    app_role_rls_session.add(journey)
    await app_role_rls_session.flush()

    older = await _seed_run(
        app_role_rls_session,
        org_id=journey_org,
        pipeline_id=journey_pipeline,
        snapshot_id=journey_snapshot,
        work_item_refs=_refs(kind="jira", ref="PAY-77"),
        completed_at=datetime(2026, 6, 1, tzinfo=UTC),
        run_number=1,
    )
    newer_rich = await _seed_run(
        app_role_rls_session,
        org_id=journey_org,
        pipeline_id=journey_pipeline,
        snapshot_id=journey_snapshot,
        work_item_refs=[
            _refs(kind="linear", ref="FAR-1", source="caller")[0],
            _refs(kind="jira", ref="PAY-77", source="agent", status="done")[0],
        ],
        completed_at=datetime(2026, 6, 2, tzinfo=UTC),
        run_number=2,
    )
    # Unrelated refs — enough to prove the predicate never widens.
    await _seed_run(
        app_role_rls_session,
        org_id=journey_org,
        pipeline_id=journey_pipeline,
        snapshot_id=journey_snapshot,
        work_item_refs=_refs(kind="jira", ref="PAY-40"),
        completed_at=datetime(2026, 6, 3, tzinfo=UTC),
        run_number=3,
    )
    await _seed_run(
        app_role_rls_session,
        org_id=journey_org,
        pipeline_id=journey_pipeline,
        snapshot_id=journey_snapshot,
        work_item_refs=_refs(kind="github_issue", ref="a/b#5"),
        completed_at=datetime(2026, 6, 4, tzinfo=UTC),
        run_number=4,
    )

    runs = await list_journey_runs(app_role_rls_session, journey=journey)
    assert [r.id for r in runs] == [newer_rich.id, older.id]


async def test_containment_query_respects_order_and_limit_on_postgres(
    app_role_rls_session: AsyncSession,
    journey_org: uuid.UUID,
    journey_pipeline: uuid.UUID,
    journey_snapshot: uuid.UUID,
) -> None:
    """Newest-first ordering plus the SQL-side LIMIT clamp survive the
    migrated-to-Postgres path."""
    journey = Journey(
        organisation_id=journey_org,
        kind="linear",
        ref="FAR-9",
        canonical_work_item_id=canonical_work_item_id(journey_org, "linear", "FAR-9"),
        latest_status="complete",
        latest_provenance="derived",
    )
    app_role_rls_session.add(journey)
    await app_role_rls_session.flush()

    for index, ts in enumerate(
        [
            datetime(2026, 6, 1, tzinfo=UTC),
            datetime(2026, 6, 2, tzinfo=UTC),
            datetime(2026, 6, 3, tzinfo=UTC),
        ]
    ):
        await _seed_run(
            app_role_rls_session,
            org_id=journey_org,
            pipeline_id=journey_pipeline,
            snapshot_id=journey_snapshot,
            work_item_refs=_refs(kind="linear", ref="FAR-9"),
            completed_at=ts,
            run_number=index + 10,
        )

    newest_first = await list_journey_runs(app_role_rls_session, journey=journey)
    assert len(newest_first) == 3
    completed_at_values = [r.completed_at for r in newest_first]
    assert completed_at_values[0] > completed_at_values[-1]

    clamped = await list_journey_runs(app_role_rls_session, journey=journey, limit=1)
    assert len(clamped) == 1
    assert clamped[0].id == newest_first[0].id
