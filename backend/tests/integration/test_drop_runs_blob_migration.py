"""Integration tests for migration 0215 (FAR-583 — the DROP migration) — Testcontainers Postgres.

* the migrated HEAD database has NO legacy runs blob columns;
* a run seeded through the repo writers reads back through the reassembly
  reader (the round-trip the legacy two-store reads used to hedge);
* the 0192 quarantine side table survives;
* the drain assertion's live count query flags a pre-B1 in-flight run,
  including the post-B2b-addition statuses (pending / hitl_parked).
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker

from modulo.db.crud.run_node_outputs import (
    read_run_blobs,
    replace_run_node_outputs,
    write_run_markers,
)

pytestmark = pytest.mark.integration

# The B1 deploy cutoff embedded in the migration (B1 merged 2026-09-10T11:25:42Z per PR #298);
# bound as a REAL datetime - asyncpg needs typed datetime params (a str DataErrors).
_B1_CUTOFF = datetime(2026, 9, 10, 11, 25, 42, tzinfo=UTC)

# A dedicated pipeline/snapshot pair for the org the session fixtures provide.
PIPELINE_ID = "b2b0000b-0000-0000-0000-000000000001"
SNAPSHOT_ID = "b2b0000b-0000-0000-0000-000000000003"


@pytest.fixture
async def org_ready(test_org: uuid.UUID, test_user: uuid.UUID, db_engine: AsyncEngine) -> uuid.UUID:
    """Seed the b2b pipeline + snapshot under the session-scoped test org."""
    async with db_engine.connect() as conn, conn.begin():
        await conn.execute(
            text(
                "INSERT INTO pipelines (id, organisation_id, name, account_id, "
                "max_concurrent_runs, lock_wait_timeout_seconds, node_timeout_seconds, "
                "run_context_defaults, graph_nodes_json) "
                "VALUES (:pid, :oid, 'b2b pipeline', :uid, 10, 30, 300, '{}'::json, '[]'::json) "
                "ON CONFLICT (id) DO NOTHING"
            ),
            {"pid": PIPELINE_ID, "oid": str(test_org), "uid": str(test_user)},
        )
        await conn.execute(
            text(
                "INSERT INTO pipeline_snapshots (id, pipeline_id, organisation_id, "
                "snapshot_version, graph_json, connector_bindings_json, "
                "schema_pins_json, prompt_pins_json, model_backend_pins_json, "
                "run_context_defaults, config_json) "
                "VALUES (:sid, :pid, :oid, 1, '{}'::json, '[]'::json, '[]'::json, "
                "'[]'::json, '[]'::json, '{}'::json, '{}'::json) "
                "ON CONFLICT (id) DO NOTHING"
            ),
            {"sid": SNAPSHOT_ID, "pid": PIPELINE_ID, "oid": str(test_org)},
        )
        # Reset any RUNS left behind by a previous crashed session sharing
        # this fixture org (the shared migrated DB persists across pytest
        # runs; stale rows collide on uq (organisation_id, run_number) and
        # the b2b pipeline-id constants keep the seeds unambiguous).
        await conn.execute(
            text("DELETE FROM run_node_outputs WHERE run_id IN (SELECT id FROM runs WHERE pipeline_id = :pid)"),
            {"pid": PIPELINE_ID},
        )
        await conn.execute(text("DELETE FROM runs WHERE pipeline_id = :pid"), {"pid": PIPELINE_ID})
    return test_org


async def _seed_run(
    db_engine: AsyncEngine,
    org_id: uuid.UUID,
    run_id: uuid.UUID,
    *,
    status: str = "complete",
    started: bool = False,
) -> None:
    async with db_engine.begin() as conn:
        await conn.execute(
            text(
                "INSERT INTO runs (id, organisation_id, pipeline_id, snapshot_id, trigger_type, status, "
                "run_number, input_hash, langgraph_thread_id, created_at, started_at, completed_at) "
                "VALUES (:id, :oid, :pid, :sid, 'manual', :status, :run_number, 'ih', :thread, "
                ":created_at, :started_at, :completed_at)"
            ),
            {
                "id": str(run_id),
                "oid": str(org_id),
                "pid": PIPELINE_ID,
                "sid": SNAPSHOT_ID,
                "status": status,
                "thread": f"b2b-{run_id.hex[:12]}",
                "run_number": run_id.int % 100000,  # int: avoids (org, run_number) unique collisions
                # asyncpg needs typed datetime params (a str would DataError).
                "created_at": datetime(2026, 9, 1, tzinfo=UTC),
                "started_at": datetime(2026, 9, 2, tzinfo=UTC) if started else None,
                "completed_at": datetime(2026, 9, 3, tzinfo=UTC) if status == "complete" else None,
            },
        )


async def test_dropped_legacy_columns(db_engine: AsyncEngine) -> None:
    async with db_engine.connect() as conn:
        cols = {
            row[0]
            for row in (
                await conn.execute(
                    text(
                        "SELECT column_name FROM information_schema.columns "
                        "WHERE table_schema = 'public' AND table_name = 'runs'"
                    )
                )
            ).all()
        }
    assert "outputs_json" not in cols
    assert "node_telemetry_json" not in cols
    assert "raw_output_markers" not in cols


async def test_blobs_serve_from_the_new_table(db_engine: AsyncEngine, org_ready: uuid.UUID) -> None:
    run_id = uuid.uuid4()
    await _seed_run(db_engine, org_ready, run_id)
    sessions = async_sessionmaker(db_engine, expire_on_commit=False)
    async with sessions() as session, session.begin():
        await session.execute(text(f"SET LOCAL app.organisation_id = '{org_ready}'"))
        # These repo helpers are SESSION-level APIs (resolve_dialect
        # reads session.get_bind().dialect); raw connections lack it.
        await replace_run_node_outputs(
            session,
            run_id=run_id,
            organisation_id=org_ready,
            outputs={"n1": {"answer": 42}},
            telemetry=None,
        )
        await write_run_markers(
            session, run_id=run_id, organisation_id=org_ready, markers={f"run:{run_id}:node:n1:0": {"raw": "x"}}
        )

    async with sessions() as session:
        async with session.begin():
            await session.execute(text(f"SET LOCAL app.organisation_id = '{org_ready}'"))
            blobs = await read_run_blobs(session, run_id=run_id, organisation_id=org_ready)
        assert blobs.outputs == {"n1": {"answer": 42}}
        assert blobs.telemetry is None
        assert blobs.markers == {f"run:{run_id}:node:n1:0": {"raw": "x"}}


async def test_quarantine_table_survives(db_engine: AsyncEngine) -> None:
    async with db_engine.connect() as conn:
        names = {
            row[0]
            for row in (
                await conn.execute(
                    text(
                        "SELECT table_name FROM information_schema.tables "
                        "WHERE table_schema = 'public' AND table_name = 'run_node_outputs_quarantine'"
                    )
                )
            ).all()
        }
    assert names == {"run_node_outputs_quarantine"}


async def test_drain_count_matches_the_migration_gate(db_engine: AsyncEngine, org_ready: uuid.UUID) -> None:
    """The live drain count query flags a pre-B1 in-flight run (the gate the
    migration RAISES on; release.sh's bounded retry re-checks on each attempt).

    The status set is pinned to the migration's drain set INCLUDING the
    fail-safe additions pending + hitl_parked (post-B2b F2a/F2b statuses).
    """
    run_id = uuid.uuid4()
    await _seed_run(db_engine, org_ready, run_id, status="running", started=True)
    async with db_engine.connect() as conn:
        rows = (
            await conn.execute(
                text(
                    "SELECT count(*) FROM runs r "
                    "WHERE r.status IN ('running', 'claimed', 'awaiting_human', 'pending', 'hitl_parked') "
                    "AND r.created_at < :cutoff"
                ),
                {"cutoff": _B1_CUTOFF},
            )
        ).scalar_one()
    assert rows >= 1, "a seeded pre-B1 in-flight run MUST be visible to the drain-gate count"
