"""Integration test for migration 0171_runs_list_performance_indexes.

Runs against the fully-migrated testcontainer Postgres (the ``migrated_db_url``
session fixture applies the whole Alembic chain, including 0171) and verifies
the runs-list index landscape on the live schema:

* ``ix_runs_org_created_pipeline (organisation_id, created_at) INCLUDE
  (pipeline_id)`` — serves the runs-page ``total`` COUNT index-only. The INCLUDE
  column must be NON-KEY: pg_index.indnkeyatts == 2 (key columns) while
  indnatts == 3 (key + included);
* ``ix_runs_organisation_status (organisation_id, status)`` from migration 0155
  is still present and untouched — it serves the active-run counts (runs page +
  every dispatch admission gate), so 0171 must not have dropped or shadowed it;
* 0171 did NOT create a redundant ``ix_runs_org_status`` — a second
  (organisation_id, status) index would double the write amplification of
  ``status`` (updated on every run transition).

No schema mutation — read-only against the shared migrated database.
"""

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import NullPool

pytestmark = [pytest.mark.integration]

_RUNS_INDEX_SQL = (
    "SELECT i.relname AS index_name, ix.indnatts AS total_attrs, ix.indnkeyatts AS key_attrs "
    "FROM pg_index ix "
    "JOIN pg_class i ON i.oid = ix.indexrelid "
    "JOIN pg_class t ON t.oid = ix.indrelid "
    "WHERE t.relname = 'runs'"
)


async def test_0171_runs_list_indexes_exist(migrated_db_url: str) -> None:
    engine = create_async_engine(migrated_db_url, poolclass=NullPool)
    try:
        async with engine.connect() as conn:
            rows = (await conn.execute(text(_RUNS_INDEX_SQL))).mappings().all()
    finally:
        await engine.dispose()

    by_name = {row["index_name"]: (row["total_attrs"], row["key_attrs"]) for row in rows}

    assert "ix_runs_org_created_pipeline" in by_name, f"ix_runs_org_created_pipeline missing; got {sorted(by_name)}"
    cover_total, cover_key = by_name["ix_runs_org_created_pipeline"]
    # (organisation_id, created_at) as KEY columns + pipeline_id INCLUDEd — the
    # non-key column is what lets the runs-page total COUNT scan index-only.
    assert (cover_total, cover_key) == (3, 2), (
        f"expected 3 total attrs / 2 key attrs (INCLUDE pipeline_id), got {(cover_total, cover_key)}"
    )

    # Migration 0155's org+status index must still be present and untouched.
    assert "ix_runs_organisation_status" in by_name, (
        f"0155's ix_runs_organisation_status missing; got {sorted(by_name)}"
    )
    org_status_total, org_status_key = by_name["ix_runs_organisation_status"]
    assert (org_status_total, org_status_key) == (2, 2), (
        "0155's ix_runs_organisation_status must remain a plain two-column composite"
    )

    # 0171 must NOT have created a duplicate (organisation_id, status) index.
    assert "ix_runs_org_status" not in by_name, f"redundant ix_runs_org_status present; got {sorted(by_name)}"
