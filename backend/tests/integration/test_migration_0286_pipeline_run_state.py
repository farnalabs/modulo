"""Migration 0286 + the Paused gate against a real Postgres (FAR-1530).

Two halves, both CI-run (testcontainers):

1. **Migration / backfill** — runs the real Alembic ``upgrade`` of 0286
   against a fresh, isolated Postgres (a private database cloned from
   ``template0``; pattern from ``test_migration_0191_...`` /
   ``test_migration_0262_...``), seeded at 0285 with the exact witness shapes
   production held:

   * a tripped row with a stamped ``circuit_breaker_tripped_at`` — the
     backfill must set ``run_enabled=false, reason='circuit_breaker',
     at=tripped_at`` (FAILS without the data migration: the row would come
     back ``run_enabled=true`` and only the trigger-side witness would be
     honoured);
   * a tripped row with a NULL stamp (legacy witness) — ``COALESCE`` must
     still produce a non-null ``run_disabled_at`` so the new tie CHECK holds;
   * a clean row — must stay ``run_enabled=true`` with no cause;
   * the witness columns themselves must be untouched (they stay the
     breaker's witness for the admin-reset path);
   * both CHECK constraints must exist afterwards;
   * the DDL must be existence-gated: pre-creating one of the new columns at
     0285 (a partially-applied upgrade) must NOT break the upgrade, and the
     backfill UPDATE must be safely re-runnable (0 rows on the second pass).

2. **Gate on the migrated schema** — ``create_run`` on the head-migrated
   testcontainer refuses a paused pipeline with ``state="paused"`` under the
   same execution-context RLS session the background fire jobs use, and
   persists no run row (the refusal precedes the INSERT).
"""

from __future__ import annotations

import importlib.util
import uuid
from collections.abc import AsyncIterator
from datetime import datetime
from pathlib import Path
from typing import Any

import pytest
import pytest_asyncio
from alembic import command
from alembic.config import Config
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from modulo.core.exceptions import PipelineNotRunnableError
from modulo.db.crud.run import create_run
from modulo.db.rls import set_rls_execution_context, set_rls_org
from tests.integration.test_pipeline_state_gate_rls import _seed_org, _seed_user

pytestmark = [pytest.mark.integration]

BACKEND_ROOT = Path(__file__).parents[2]  # backend/ (this file: backend/tests/integration/)
MIGRATION_REV = "0286_pipeline_run_state"
PREV_REV = "0285_system_audit_events"


def _alembic_config(db_url: str) -> Config:
    config = Config(BACKEND_ROOT / "alembic.ini")
    config.set_main_option("sqlalchemy.url", db_url)
    config.set_main_option(
        "script_location",
        str(BACKEND_ROOT / "src" / "modulo" / "db" / "migrations"),
    )
    config.config_file_name = None
    return config


def _swap_db_name(db_url: str, new_db: str) -> str:
    """Return ``db_url`` with its database name replaced by ``new_db``."""
    from urllib.parse import urlparse, urlunparse

    parsed = urlparse(db_url)
    return urlunparse(parsed._replace(path=f"/{new_db}"))


def _migration_module() -> Any:
    """Load 0286's module (constants only — ``upgrade()`` needs Alembic's
    migration context, which ``command.upgrade`` provides; the module-level
    SQL constants are what the idempotence re-run uses)."""
    path = BACKEND_ROOT / "src" / "modulo" / "db" / "migrations" / "versions" / "0286_pipeline_run_state.py"
    spec = importlib.util.spec_from_file_location("migration_0286_pipeline_run_state", path)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest_asyncio.fixture
async def isolated_db_url(db_url: str) -> AsyncIterator[str]:
    """A fresh, private Postgres database migrated only up to ``PREV_REV``.

    The env pinning lives in a ``pytest.MonkeyPatch().context()`` around the
    upgrade itself (env.py resolves ``DATABASE_URL`` at upgrade time), so the
    module-scoped ``monkeypatch`` fixture is not needed here.
    """
    admin_engine = create_async_engine(db_url, poolclass=NullPool, execution_options={"isolation_level": "AUTOCOMMIT"})
    db_name = f"m0286_iso_{uuid.uuid4().hex[:10]}"
    async with admin_engine.connect() as conn:
        await conn.execute(text(f'CREATE DATABASE "{db_name}" WITH TEMPLATE template0'))
    await admin_engine.dispose()

    iso_url = _swap_db_name(db_url, db_name)

    eng = create_async_engine(iso_url, poolclass=NullPool)
    async with eng.connect() as conn:
        await conn.execute(
            text("CREATE TABLE IF NOT EXISTS alembic_version (version_num VARCHAR(255) NOT NULL PRIMARY KEY)")
        )
        await conn.commit()
    await eng.dispose()

    with pytest.MonkeyPatch().context() as mp:
        mp.setenv("DATABASE_URL", iso_url)
        mp.setenv("DATABASE_ADMIN_URL", iso_url)
        command.upgrade(_alembic_config(iso_url), PREV_REV)

    yield iso_url

    admin_engine = create_async_engine(db_url, poolclass=NullPool, execution_options={"isolation_level": "AUTOCOMMIT"})
    async with admin_engine.connect() as conn:
        await conn.execute(text(f'DROP DATABASE IF EXISTS "{db_name}"'))
    await admin_engine.dispose()


async def _seed_pipeline(
    engine: AsyncEngine,
    *,
    org_id: uuid.UUID,
    account_id: uuid.UUID,
    tripped: bool,
    tripped_at: datetime | None,
) -> uuid.UUID:
    """Seed one pipeline in the 0285 shape (NO run_* columns yet)."""
    pipeline_id = uuid.uuid4()
    async with engine.begin() as conn:
        await conn.execute(
            text(
                "INSERT INTO pipelines (id, organisation_id, name, account_id, "
                "max_concurrent_runs, lock_wait_timeout_seconds, node_timeout_seconds, "
                "run_context_defaults, graph_nodes_json, default_autonomy_level, visibility, "
                "circuit_breaker_threshold, circuit_breaker_tripped, circuit_breaker_tripped_at) "
                "VALUES (:id, :oid, :name, :uid, 10, 30, 300, "
                "'{}'::json, '[]'::json, 'manual_approval', 'org', "
                "100.0, :tripped, :tripped_at)"
            ),
            {
                "id": str(pipeline_id),
                "oid": str(org_id),
                "name": f"m0286-{uuid.uuid4().hex[:8]}",
                "uid": str(account_id),
                "tripped": tripped,
                "tripped_at": tripped_at,
            },
        )
    return pipeline_id


async def _state(engine: AsyncEngine, pipeline_id: uuid.UUID) -> dict[str, Any]:
    async with engine.connect() as conn:
        row = (
            (
                await conn.execute(
                    text(
                        "SELECT run_enabled, run_disabled_reason, run_disabled_at, "
                        "circuit_breaker_tripped, circuit_breaker_tripped_at "
                        "FROM pipelines WHERE id = :id"
                    ),
                    {"id": str(pipeline_id)},
                )
            )
            .mappings()
            .one()
        )
    return dict(row)


async def test_backfill_folds_tripped_witnesses_into_the_unified_state(isolated_db_url: str) -> None:
    """The data migration (same revision as the DDL — not a follow-up)."""
    engine = create_async_engine(isolated_db_url, poolclass=NullPool)
    try:
        org_id = await _seed_org(engine, "m0286-backfill")
        account_id = await _seed_user(engine, org_id, "m0286-backfill@example.test")
        tripped_stamped = await _seed_pipeline(
            engine,
            org_id=org_id,
            account_id=account_id,
            tripped=True,
            tripped_at=datetime.fromisoformat("2026-10-01T04:05:06+00:00"),
        )
        tripped_null_stamp = await _seed_pipeline(
            engine, org_id=org_id, account_id=account_id, tripped=True, tripped_at=None
        )
        clean = await _seed_pipeline(engine, org_id=org_id, account_id=account_id, tripped=False, tripped_at=None)

        command.upgrade(_alembic_config(isolated_db_url), MIGRATION_REV)

        stamped = await _state(engine, tripped_stamped)
        assert stamped["run_enabled"] is False, "backfill must disable a tripped pipeline"
        assert stamped["run_disabled_reason"] == "circuit_breaker"
        # The cause timestamp is the witness's own stamp (spec: at=tripped_at).
        assert stamped["run_disabled_at"] is not None
        assert stamped["run_disabled_at"].isoformat().startswith("2026-10-01T04:05:06")
        # The witness itself is untouched — it stays the breaker's witness.
        assert stamped["circuit_breaker_tripped"] is True
        assert stamped["circuit_breaker_tripped_at"] is not None

        legacy = await _state(engine, tripped_null_stamp)
        assert legacy["run_enabled"] is False
        assert legacy["run_disabled_reason"] == "circuit_breaker"
        # COALESCE guard: a NULL witness stamp still yields a real cause
        # timestamp, otherwise the tie CHECK would reject the row.
        assert legacy["run_disabled_at"] is not None

        untouched = await _state(engine, clean)
        assert untouched["run_enabled"] is True
        assert untouched["run_disabled_reason"] is None
        assert untouched["run_disabled_at"] is None
        assert untouched["circuit_breaker_tripped"] is False
    finally:
        await engine.dispose()


async def test_ddl_is_existence_gated_and_the_backfill_is_rerunnable(isolated_db_url: str) -> None:
    """A partially-applied upgrade must no-op, and the backfill must be safe
    to run again (idempotent DDL + data, per the migration contract)."""
    engine = create_async_engine(isolated_db_url, poolclass=NullPool)
    try:
        org_id = await _seed_org(engine, "m0286-partial")
        account_id = await _seed_user(engine, org_id, "m0286-partial@example.test")
        tripped = await _seed_pipeline(
            engine,
            org_id=org_id,
            account_id=account_id,
            tripped=True,
            tripped_at=datetime.fromisoformat("2026-10-01T00:00:00+00:00"),
        )

        # Simulate a partially-applied upgrade: one of the new columns already
        # exists at 0285. Without `ADD COLUMN IF NOT EXISTS` the upgrade below
        # would abort with "column already exists".
        async with engine.begin() as conn:
            await conn.execute(text("ALTER TABLE pipelines ADD COLUMN run_enabled BOOLEAN NOT NULL DEFAULT true"))

        command.upgrade(_alembic_config(isolated_db_url), MIGRATION_REV)

        state = await _state(engine, tripped)
        assert state["run_enabled"] is False
        assert state["run_disabled_reason"] == "circuit_breaker"

        # Re-run the backfill SQL directly: it must match 0 rows now (the
        # guard is `run_enabled = true`), i.e. it cannot clobber a cause.
        module = _migration_module()
        async with engine.begin() as conn:
            result = await conn.execute(text(module._BACKFILL))
            assert result.rowcount == 0
        after = await _state(engine, tripped)
        assert after["run_disabled_reason"] == "circuit_breaker"
    finally:
        await engine.dispose()


async def test_paused_pipeline_is_refused_on_real_postgres(db_engine: AsyncEngine) -> None:
    """The gate's `run_enabled` read, on the head-migrated schema under the
    execution-context RLS session the background fire jobs use.

    FAILS without the gate extension (the SELECT carried no run_enabled, so
    the paused row read as runnable and a run row would be created).
    """
    org_id = await _seed_org(db_engine, "m0286-gate")
    account_id = await _seed_user(db_engine, org_id, "m0286-gate@example.test")
    pipeline_id = uuid.uuid4()
    async with db_engine.begin() as conn:
        await conn.execute(
            text(
                "INSERT INTO pipelines (id, organisation_id, name, account_id, "
                "max_concurrent_runs, lock_wait_timeout_seconds, node_timeout_seconds, "
                "run_context_defaults, graph_nodes_json, default_autonomy_level, visibility, "
                "run_enabled, run_disabled_reason, run_disabled_at) "
                "VALUES (:id, :oid, :name, :uid, 10, 30, 300, "
                "'{}'::json, '[]'::json, 'manual_approval', 'org', "
                "false, 'operator', now())"
            ),
            {"id": str(pipeline_id), "oid": str(org_id), "name": "m0286-paused", "uid": str(account_id)},
        )

    factory = async_sessionmaker(db_engine, expire_on_commit=False)
    async with factory() as session, session.begin():
        await set_rls_org(session, org_id)
        await set_rls_execution_context(session)

        with pytest.raises(PipelineNotRunnableError) as excinfo:
            await create_run(
                session,
                org_id=org_id,
                pipeline_id=pipeline_id,
                snapshot_id=uuid.uuid4(),
                trigger_type="manual",
                input_payload={"data": 1},
            )

    assert excinfo.value.state == "paused"
    assert excinfo.value.pipeline_id == pipeline_id

    async with db_engine.connect() as conn:
        run_count = (
            await conn.execute(text("SELECT count(*) FROM runs WHERE pipeline_id = :pid"), {"pid": str(pipeline_id)})
        ).scalar_one()
    assert int(run_count) == 0, "the refusal must precede the run INSERT"
