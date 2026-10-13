"""FAR-1714: migration 0305 + the dispatch-phase timeline / drop counter round-trip.

Against REAL Postgres (testcontainers, private database per test — the
``test_migration_0276`` / ``test_migration_0302`` harness):

* NEGATIVE CONTROL — at ``0304_metrics_staging_recorded_at_index`` (the chain
  head before this change) NEITHER column exists, so the post-migration
  assertions below cannot pass without the real upgrade;
* the REAL alembic upgrade of ``0306_runs_dispatch_phase_history`` lands both
  columns with the exact contract (``dispatch_phase_history`` nullable
  ``jsonb``; ``dispatch_phase_write_drops`` ``integer NOT NULL DEFAULT 0``);
* the DOWNGRADE drops both columns, and the upgrade re-applies cleanly after
  a downgrade;
* fresh-database path: the whole chain straight to 0306 replays on a
  brand-new database.

Plus the composed ROUND-TRIP on the shared migrated database — the actual
statements and the actual writer executed against Postgres, not mocked:

* the REAL claim statement appends the ``{"phase": "claimed"}`` attempt-boundary
  entry and stamps the scalar floor in the same statement;
* the REAL ``DispatchPhaseWriter`` appends one timeline entry per durable
  phase, inherits the claim-token fence (a superseded token appends nothing)
  and the monotonic guard, and leaves ``dispatch_phase_write_drops`` at 0 on
  the happy path;
* an abandoned (timed-out) write bumps ``runs.dispatch_phase_write_drops``
  through the REAL ``_DROP_COUNTER_UPDATE_SQL`` — the durable FAR-1511
  saturation signal — without raising into the caller.
"""

from __future__ import annotations

import asyncio
import json
import uuid
from collections.abc import AsyncGenerator
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urlparse, urlunparse

import pytest
import pytest_asyncio
from alembic import command
from alembic.config import Config
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine
from sqlalchemy.pool import NullPool

import modulo.core.pipeline_execution as pe
from modulo.core.pipeline_execution import PHASE_LOADING_SETUP, PHASE_STREAMING, DispatchPhaseWriter

pytestmark = [pytest.mark.integration]

BACKEND_ROOT = Path(__file__).parents[2]  # backend/
MIGRATION_REV = "0306_runs_dispatch_phase_history"
PREV_REV = "0305_guardrail_escalate_action"

_HISTORY_COL = "dispatch_phase_history"
_DROPS_COL = "dispatch_phase_write_drops"

# Dedicated pipeline/snapshot constants for THIS module's round-trip seeds —
# distinct from other migration tests' constants so sessions sharing the
# migrated database cannot collide.
PIPELINE_ID = "c5c00000-0000-0000-0000-000000000001"
SNAPSHOT_ID = "c5c00000-0000-0000-0000-000000000003"

_CLAIM_TOKEN = "tok-far1714-roundtrip"


# ---------------------------------------------------------------------------
# Private-database harness (mirrors test_migration_0276_runs_autovacuum.py)
# ---------------------------------------------------------------------------


def _alembic_config(db_url: str) -> Config:
    config = Config(BACKEND_ROOT / "alembic.ini")
    config.set_main_option("sqlalchemy.url", db_url)
    config.set_main_option("script_location", str(BACKEND_ROOT / "src" / "modulo" / "db" / "migrations"))
    config.config_file_name = None
    return config


def _swap_db_name(db_url: str, new_db: str) -> str:
    parsed = urlparse(db_url)
    return urlunparse(parsed._replace(path=f"/{new_db}"))


def _alembic_cmd(iso_url: str, cmd: str, target: str) -> None:
    """Run an alembic command against the isolated DB via the Python API.

    Deliberately does NOT inject ``cmd_opts`` (the FAR-967 F2 shape): env.py
    infers direction from the active EnvironmentContext instead.
    """
    cfg = _alembic_config(iso_url)
    assert getattr(cfg, "cmd_opts", None) is None, "Python-API invocation must carry no cmd_opts"
    getattr(command, cmd)(cfg, target)


async def _fresh_db(db_url: str, prefix: str) -> tuple[str, str]:
    """Create an empty private database; return ``(name, async url)``."""
    admin_engine = create_async_engine(db_url, poolclass=NullPool, execution_options={"isolation_level": "AUTOCOMMIT"})
    db_name = f"{prefix}_{uuid.uuid4().hex[:10]}"
    async with admin_engine.connect() as conn:
        await conn.execute(text(f'CREATE DATABASE "{db_name}" WITH TEMPLATE template0'))
    await admin_engine.dispose()
    return db_name, _swap_db_name(db_url, db_name)


async def _drop_db(db_url: str, db_name: str) -> None:
    admin_engine = create_async_engine(db_url, poolclass=NullPool, execution_options={"isolation_level": "AUTOCOMMIT"})
    async with admin_engine.connect() as conn:
        await conn.execute(text(f'DROP DATABASE IF EXISTS "{db_name}"'))
    await admin_engine.dispose()


def _migrate(iso_url: str, target: str) -> None:
    """Run an alembic upgrade with env.py's URL resolution pinned to ``iso_url``."""
    with pytest.MonkeyPatch().context() as mp:
        mp.setenv("DATABASE_URL", iso_url)
        mp.setenv("DATABASE_ADMIN_URL", iso_url)
        _alembic_cmd(iso_url, "upgrade", target)


def _downgrade(iso_url: str, target: str) -> None:
    with pytest.MonkeyPatch().context() as mp:
        mp.setenv("DATABASE_URL", iso_url)
        mp.setenv("DATABASE_ADMIN_URL", iso_url)
        _alembic_cmd(iso_url, "downgrade", target)


async def _ensure_alembic_table(iso_url: str) -> None:
    engine = create_async_engine(iso_url, poolclass=NullPool)
    async with engine.connect() as conn:
        await conn.execute(
            text("CREATE TABLE IF NOT EXISTS alembic_version (version_num VARCHAR(255) NOT NULL PRIMARY KEY)")
        )
    await engine.dispose()


@pytest_asyncio.fixture
async def pre_upgrade_db_url(db_url: str) -> AsyncGenerator[str, None]:
    """A fresh, private Postgres database migrated only up to ``PREV_REV`` —
    the deployed shape BEFORE FAR-1714 (neither column exists)."""
    db_name, iso_url = await _fresh_db(db_url, "m0305_pre")
    try:
        await _ensure_alembic_table(iso_url)
        _migrate(iso_url, PREV_REV)
        yield iso_url
    finally:
        await _drop_db(db_url, db_name)


@pytest_asyncio.fixture
async def fresh_chain_db_url(db_url: str) -> AsyncGenerator[str, None]:
    """A fresh, private Postgres database migrated ALL the way to ``MIGRATION_REV``."""
    db_name, iso_url = await _fresh_db(db_url, "m0305_fresh")
    try:
        await _ensure_alembic_table(iso_url)
        _migrate(iso_url, MIGRATION_REV)
        yield iso_url
    finally:
        await _drop_db(db_url, db_name)


async def _run_column_shapes(engine: AsyncEngine) -> dict[str, dict[str, str | None]]:
    """The two FAR-1714 columns' information_schema shapes (missing -> absent)."""
    async with engine.connect() as conn:
        rows = (
            await conn.execute(
                text(
                    "SELECT column_name, is_nullable, column_default, data_type "
                    "FROM information_schema.columns "
                    "WHERE table_schema = 'public' AND table_name = 'runs' "
                    "AND column_name IN (:hist, :drops)"
                ),
                {"hist": _HISTORY_COL, "drops": _DROPS_COL},
            )
        ).all()
    return {str(r[0]): {"nullable": str(r[1]), "default": r[2], "type": str(r[3])} for r in rows}


async def _alembic_version(engine: AsyncEngine) -> str:
    async with engine.connect() as conn:
        result = await conn.execute(text("SELECT version_num FROM alembic_version"))
        return str(result.scalar_one())


class TestMigrationShape:
    async def test_columns_absent_then_land_with_the_exact_contract(self, pre_upgrade_db_url: str) -> None:
        """NEGATIVE CONTROL first: at 0304 neither column exists — without
        the real 0305 upgrade every assertion below fails."""
        engine = create_async_engine(pre_upgrade_db_url, poolclass=NullPool)
        try:
            before = await _run_column_shapes(engine)
            assert _HISTORY_COL not in before, f"pre-0305 must lack {_HISTORY_COL}, got {before}"
            assert _DROPS_COL not in before, f"pre-0305 must lack {_DROPS_COL}, got {before}"

            _migrate(pre_upgrade_db_url, MIGRATION_REV)
            assert await _alembic_version(engine) == MIGRATION_REV

            after = await _run_column_shapes(engine)
            history = after[_HISTORY_COL]
            assert history["nullable"] == "YES", f"{_HISTORY_COL} must be nullable (pre-0305 rows), got {history}"
            assert history["type"] == "jsonb", f"{_HISTORY_COL} must be jsonb on Postgres, got {history}"
            assert history["default"] is None, f"{_HISTORY_COL} carries no default, got {history}"

            drops = after[_DROPS_COL]
            assert drops["nullable"] == "NO", f"{_DROPS_COL} must be NOT NULL, got {drops}"
            assert drops["type"] == "integer", f"{_DROPS_COL} must be integer, got {drops}"
            assert str(drops["default"]) == "0", f"{_DROPS_COL} must default 0, got {drops}"
        finally:
            await engine.dispose()

    async def test_downgrade_removes_both_and_upgrade_reapplies(self, pre_upgrade_db_url: str) -> None:
        engine = create_async_engine(pre_upgrade_db_url, poolclass=NullPool)
        try:
            _migrate(pre_upgrade_db_url, MIGRATION_REV)
            _downgrade(pre_upgrade_db_url, PREV_REV)
            assert await _alembic_version(engine) == PREV_REV
            shapes = await _run_column_shapes(engine)
            assert _HISTORY_COL not in shapes
            assert _DROPS_COL not in shapes

            # Round-trips: the upgrade applies cleanly again after a downgrade.
            _migrate(pre_upgrade_db_url, MIGRATION_REV)
            assert await _alembic_version(engine) == MIGRATION_REV
            shapes = await _run_column_shapes(engine)
            assert _HISTORY_COL in shapes
            assert _DROPS_COL in shapes
        finally:
            await engine.dispose()


class TestFreshDatabase:
    async def test_fresh_chain_carries_both_columns(self, fresh_chain_db_url: str) -> None:
        """Safe on a fresh database: the whole chain to 0305 replays on a
        brand-new database (``runs`` created, then both columns added)."""
        engine = create_async_engine(fresh_chain_db_url, poolclass=NullPool)
        try:
            assert await _alembic_version(engine) == MIGRATION_REV
            shapes = await _run_column_shapes(engine)
            assert _HISTORY_COL in shapes
            assert _DROPS_COL in shapes
            assert str(shapes[_DROPS_COL]["default"]) == "0"
        finally:
            await engine.dispose()


# ---------------------------------------------------------------------------
# Composed round-trip on the shared migrated database: real claim SQL +
# real DispatchPhaseWriter against real Postgres.
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def round_trip_run(
    test_org: uuid.UUID,
    test_user: uuid.UUID,
    db_engine: AsyncEngine,
) -> AsyncGenerator[tuple[uuid.UUID, uuid.UUID], None]:
    """Seed a claimable run (plus its pipeline/snapshot FKs) for one test.

    Yields ``(org_id, run_id)``; tears every row down afterwards so the
    shared migrated database stays clean for other modules.
    """
    run_id = uuid.uuid4()
    async with db_engine.connect() as conn, conn.begin():
        await conn.execute(
            text(
                "INSERT INTO pipelines (id, organisation_id, name, account_id, "
                "max_concurrent_runs, lock_wait_timeout_seconds, node_timeout_seconds, "
                "run_context_defaults, graph_nodes_json) "
                "VALUES (:pid, :oid, 'far1714 pipeline', :uid, 10, 30, 300, '{}'::json, '[]'::json) "
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
        await conn.execute(
            text(
                "INSERT INTO runs (id, organisation_id, pipeline_id, snapshot_id, trigger_type, status, "
                "run_number, input_hash, langgraph_thread_id, created_at, claim_token) "
                "VALUES (:id, :oid, :pid, :sid, 'manual', 'pending', :run_number, 'ih', :thread, "
                ":created_at, :tok)"
            ),
            {
                "id": str(run_id),
                "oid": str(test_org),
                "pid": PIPELINE_ID,
                "sid": SNAPSHOT_ID,
                "run_number": 910000 + run_id.int % 10000,
                "thread": f"far1714-{run_id.hex[:12]}",
                "created_at": datetime.now(UTC),
                "tok": _CLAIM_TOKEN,
            },
        )
    try:
        yield test_org, run_id
    finally:
        async with db_engine.connect() as conn, conn.begin():
            await conn.execute(text("DELETE FROM runs WHERE id = :rid"), {"rid": str(run_id)})
            await conn.execute(
                text("DELETE FROM pipeline_snapshots WHERE id = :sid AND pipeline_id = :pid"),
                {"sid": SNAPSHOT_ID, "pid": PIPELINE_ID},
            )
            await conn.execute(text("DELETE FROM pipelines WHERE id = :pid"), {"pid": PIPELINE_ID})


async def _claim(db_engine: AsyncEngine, org_id: uuid.UUID, run_id: uuid.UUID, token: str) -> bool:
    """Execute the REAL claim statement (with its history append) on ``run_id``."""
    stmt = pe.build_claim_update(_stale_seconds=450, claim_token=token)
    async with db_engine.connect() as conn, conn.begin():
        await conn.execute(text(f"SET LOCAL app.organisation_id = '{org_id}'"))
        result = await conn.execute(
            stmt,
            {
                "rid": str(run_id),
                "oid": str(org_id),
                "stale_seconds": 450,
                "claim_cap": 20,
                "tok": token,
            },
        )
        return result.fetchone() is not None


async def _read_state(db_engine: AsyncEngine, org_id: uuid.UUID, run_id: uuid.UUID) -> dict[str, object]:
    """Read back the three FAR-1714 surface columns for one run."""
    async with db_engine.connect() as conn, conn.begin():
        await conn.execute(text(f"SET LOCAL app.organisation_id = '{org_id}'"))
        row = (
            await conn.execute(
                text(
                    "SELECT dispatch_phase, dispatch_phase_history, dispatch_phase_write_drops "
                    "FROM runs WHERE id = :rid"
                ),
                {"rid": str(run_id)},
            )
        ).one()
    history = row[1]
    if isinstance(history, str):
        history = json.loads(history)
    return {"phase": row[0], "history": history, "drops": int(row[2])}


def _writer(db_engine: AsyncEngine, org_id: uuid.UUID, run_id: uuid.UUID, token: str) -> DispatchPhaseWriter:
    return DispatchPhaseWriter(
        db_engine,
        run_id=str(run_id),
        org_id=str(org_id),
        claim_token=token,
    )


class TestRoundTrip:
    async def test_claim_appends_the_attempt_boundary_entry(
        self, round_trip_run: tuple[uuid.UUID, uuid.UUID], db_engine: AsyncEngine
    ) -> None:
        org_id, run_id = round_trip_run
        assert await _claim(db_engine, org_id, run_id, _CLAIM_TOKEN)

        state = await _read_state(db_engine, org_id, run_id)
        # Scalar floor stamped in the SAME statement (FAR-1088 contract intact)...
        assert state["phase"] == "claimed"
        # ...and the history timeline starts with the attempt boundary.
        history = state["history"]
        assert isinstance(history, list)
        assert len(history) == 1
        assert history[0]["phase"] == "claimed"
        assert history[0]["entered_at"] is not None
        assert state["drops"] == 0

    async def test_writer_appends_timeline_and_enforces_fence(
        self, round_trip_run: tuple[uuid.UUID, uuid.UUID], db_engine: AsyncEngine
    ) -> None:
        org_id, run_id = round_trip_run
        assert await _claim(db_engine, org_id, run_id, _CLAIM_TOKEN)

        # The REAL writer appends one entry per durable phase.
        writer = _writer(db_engine, org_id, run_id, _CLAIM_TOKEN)
        writer.record(PHASE_LOADING_SETUP)
        await writer.flush()
        writer.record(PHASE_STREAMING)
        await writer.flush()

        state = await _read_state(db_engine, org_id, run_id)
        history = state["history"]
        assert isinstance(history, list)
        assert [entry["phase"] for entry in history] == ["claimed", PHASE_LOADING_SETUP, PHASE_STREAMING]
        assert state["phase"] == PHASE_STREAMING
        assert state["drops"] == 0  # happy path records no drop
        # History timestamps are non-decreasing (the monotonic guard pairs
        # every append with dispatch_phase_entered_at=now()).
        stamps = [datetime.fromisoformat(entry["entered_at"]) for entry in history]
        assert stamps == sorted(stamps)

        # A SUPERSEDED attempt (rotated token) must append nothing.
        stale_writer = _writer(db_engine, org_id, run_id, "tok-rotated-successor")
        stale_writer.record(PHASE_STREAMING)
        await stale_writer.flush()
        after_fence = await _read_state(db_engine, org_id, run_id)
        assert after_fence["history"] == history
        assert after_fence["phase"] == PHASE_STREAMING

    async def test_abandoned_write_increments_the_drop_counter(
        self, round_trip_run: tuple[uuid.UUID, uuid.UUID], db_engine: AsyncEngine
    ) -> None:
        org_id, run_id = round_trip_run
        assert await _claim(db_engine, org_id, run_id, _CLAIM_TOKEN)

        writer = DispatchPhaseWriter(
            db_engine,
            run_id=str(run_id),
            org_id=str(org_id),
            claim_token=_CLAIM_TOKEN,
            timeout_seconds=0.05,
        )

        async def _hang(_phase: str) -> None:
            # Simulates DB saturation: the write exceeds PHASE_WRITE_TIMEOUT_SECONDS.
            await asyncio.Event().wait()

        writer._write = _hang  # type: ignore[method-assign]
        writer.record(PHASE_STREAMING)
        await writer.flush()  # bounded + fail-soft: must return, never raise

        state = await _read_state(db_engine, org_id, run_id)
        # The REAL _DROP_COUNTER_UPDATE_SQL landed (RLS org context included).
        assert state["drops"] == 1
        # The abandoned phase write itself did NOT land.
        assert state["phase"] == "claimed"
        history = state["history"]
        assert isinstance(history, list)
        assert [entry["phase"] for entry in history] == ["claimed"]
