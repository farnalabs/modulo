"""Integration tests for the runtime-hardening schema surface (final state).

The migration chain was squashed into three idempotent reconciliation
migrations (``0108_schema_org_identity`` / ``0109_schema_teams_library`` /
``0110_schema_pipeline_runtime``). The revisions that used to add the
runtime-hardening columns (``0073`` / ``0074``) no longer exist, and the
reconciliation migrations' downgrades are no-ops, so a downgrade/upgrade
round-trip is no longer possible. Instead these tests assert the FINAL
schema state that the reconciliation chain produces against a real Postgres
(testcontainers) — the state the old round-trip asserted at its head:

* ``runs.enqueue_failed_at`` / ``sandbox_dispatch_state`` / ``sandbox_id``
  columns exist,
* ``runs.claim_token`` is NOT NULL with a ``gen_random_uuid()`` default,
* ``hitl_claims.decision_payload`` exists as ``jsonb``,
* the recreated ``ck_runs_status`` CHECK rejects ``waiting_for_lock`` status.
"""

import uuid

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncEngine

pytestmark = pytest.mark.integration


async def test_runtime_hardening_columns_exist(db_engine: AsyncEngine) -> None:
    async with db_engine.connect() as conn:
        runs_cols = {
            row[0]
            for row in (
                await conn.execute(
                    text(
                        "SELECT column_name FROM information_schema.columns "
                        "WHERE table_name = 'runs' AND table_schema = 'public'"
                    )
                )
            ).fetchall()
        }
    assert {"enqueue_failed_at", "sandbox_dispatch_state", "sandbox_id"} <= runs_cols


async def test_runs_claim_token_not_null_with_default(db_engine: AsyncEngine) -> None:
    async with db_engine.connect() as conn:
        nullability = (
            await conn.execute(
                text(
                    "SELECT is_nullable FROM information_schema.columns "
                    "WHERE table_schema = 'public' AND table_name = 'runs' "
                    "AND column_name = 'claim_token'"
                )
            )
        ).scalar_one()
        assert nullability == "NO"

        default_expr = (
            await conn.execute(
                text(
                    "SELECT column_default FROM information_schema.columns "
                    "WHERE table_schema = 'public' AND table_name = 'runs' "
                    "AND column_name = 'claim_token'"
                )
            )
        ).scalar_one()
        assert default_expr is not None
        assert "gen_random_uuid" in default_expr


async def test_hitl_claims_decision_payload_exists(db_engine: AsyncEngine) -> None:
    async with db_engine.connect() as conn:
        hitl_cols = {
            row[0]
            for row in (
                await conn.execute(
                    text(
                        "SELECT column_name FROM information_schema.columns "
                        "WHERE table_name = 'hitl_claims' AND table_schema = 'public'"
                    )
                )
            ).fetchall()
        }
        assert "decision_payload" in hitl_cols
        payload_type = (
            await conn.execute(
                text(
                    "SELECT data_type FROM information_schema.columns "
                    "WHERE table_schema = 'public' AND table_name = 'hitl_claims' "
                    "AND column_name = 'decision_payload'"
                )
            )
        ).scalar_one()
        assert payload_type == "jsonb"


async def test_ck_runs_status_rejects_waiting_for_lock(
    db_engine: AsyncEngine,
    test_org: uuid.UUID,
    test_pipeline: uuid.UUID,
    test_snapshot: uuid.UUID,
) -> None:
    """The reconciliation chain's ``ck_runs_status`` CHECK excludes
    ``waiting_for_lock`` — a legacy status the old chain backfilled to
    ``pending``. Inserting it must fail."""
    async with db_engine.connect() as conn:
        with pytest.raises(DBAPIError):
            await conn.execute(
                text(
                    "INSERT INTO runs (id, organisation_id, pipeline_id, snapshot_id, status, "
                    "trigger_type, run_number, input_hash, langgraph_thread_id) "
                    "VALUES (:id, :oid, :pid, :sid, 'waiting_for_lock', 'manual', 1, :hash, :thread)"
                ),
                {
                    "id": str(uuid.uuid4()),
                    "oid": str(test_org),
                    "pid": str(test_pipeline),
                    "sid": str(test_snapshot),
                    "hash": "0" * 64,
                    "thread": f"{test_org}:rejected-{uuid.uuid4()}",
                },
            )
