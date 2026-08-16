"""Cost-component surface assertions (final reconciliation-chain state) — the
FIVE pinned columns, the NULLS NOT DISTINCT unique index, the dropped
constraint, RLS, and the MIGRATE-role owner.

Runs against the migrated testcontainer (test_initial_migration harness). The
full alembic upgrade round-trip with NOLOGIN-role provisioning is covered by
the CI harness: conftest provisions ``modulo_migrate`` before
``alembic upgrade heads`` and the reconciliation chain (0110_schema_pipeline_runtime)
re-owns the table to ``modulo_migrate``.
"""

from __future__ import annotations

from sqlalchemy import inspect, text
from sqlalchemy.ext.asyncio import AsyncEngine


async def _columns(db_engine: AsyncEngine, table: str) -> set[str]:
    async with db_engine.connect() as connection:
        return await connection.run_sync(
            lambda sync_connection: {col["name"] for col in inspect(sync_connection).get_columns(table)},
        )


async def test_runs_gains_cost_columns(db_engine: AsyncEngine) -> None:
    cols = await _columns(db_engine, "runs")
    assert "cost_breakdown" in cols
    assert "ledger_written" in cols
    assert "ledger_refused_at" in cols


async def test_org_daily_run_counts_gains_clamp_columns(db_engine: AsyncEngine) -> None:
    cols = await _columns(db_engine, "org_daily_run_counts")
    assert "clamped" in cols
    assert "refused_spend_usd" in cols


async def test_cost_components_table_exists(db_engine: AsyncEngine) -> None:
    async with db_engine.connect() as connection:
        table_names = await connection.run_sync(lambda sync_connection: inspect(sync_connection).get_table_names())
    assert "cost_components" in table_names
    cols = await _columns(db_engine, "cost_components")
    for expected in (
        "id",
        "organisation_id",
        "name",
        "display_name",
        "kind",
        "rate_usd",
        "rate_fallback",
        "formula",
        "report_key",
        "enabled",
        "sort_order",
        "deleted_at",
    ):
        assert expected in cols, f"cost_components missing column {expected}"


async def test_null_distinct_unique_index(db_engine: AsyncEngine) -> None:
    async with db_engine.connect() as connection:
        result = await connection.execute(
            text(
                "SELECT indexrelid::regclass::text, indisunique, "
                "indnullsnotdistinct FROM pg_index "
                "WHERE indexrelid = 'uq_org_daily_run_counts'::regclass"
            )
        )
        row = result.first()
    assert row is not None, "uq_org_daily_run_counts index not found"
    assert row[1] is True
    assert row[2] is True, "uq_org_daily_run_counts must be NULLS NOT DISTINCT"


async def test_old_unique_constraint_dropped(db_engine: AsyncEngine) -> None:
    async with db_engine.connect() as connection:
        result = await connection.execute(
            text(
                "SELECT conname FROM pg_constraint "
                "WHERE conrelid = 'public.org_daily_run_counts'::regclass "
                "AND conname = 'uq_org_daily_run_counts_org_team_date'"
            )
        )
        row = result.first()
    assert row is None, "old unique constraint must be dropped"


async def test_cost_components_owned_by_migrate_role(db_engine: AsyncEngine) -> None:
    async with db_engine.connect() as connection:
        result = await connection.execute(
            text("SELECT relowner::regrole::text FROM pg_class WHERE oid = to_regclass('public.cost_components')")
        )
        owner = result.scalar_one()
    assert owner == "modulo_migrate", f"cost_components owner is {owner}, expected modulo_migrate"


async def test_cost_components_rls_enabled(db_engine: AsyncEngine) -> None:
    async with db_engine.connect() as connection:
        result = await connection.execute(
            text("SELECT relrowsecurity FROM pg_class WHERE oid = to_regclass('public.cost_components')")
        )
        relrowsecurity = result.scalar_one()
    assert relrowsecurity is True


async def test_probe_and_refusal_indexes_exist(db_engine: AsyncEngine) -> None:
    async with db_engine.connect() as connection:
        indexes = await connection.run_sync(
            lambda sync_connection: {
                row[0]
                for row in sync_connection.execute(text("SELECT indexname FROM pg_indexes WHERE tablename = 'runs'"))
            }
        )
    assert "ix_runs_probe" in indexes
    assert "ix_runs_refusal" in indexes
