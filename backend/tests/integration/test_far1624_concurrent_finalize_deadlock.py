"""FAR-1624: concurrent run finalisations must not deadlock on the org row.

Reproduced on production 2026-10-09 (20:05Z): 5 of 8 simultaneous trivial runs
failed after ~12s with asyncpg ``deadlock detected`` (SQLSTATE 40P01). Every
finalising transaction had already inserted rows that FK-reference the
organisation (taking ``FOR KEY SHARE`` on the org tuple), and then the
spend-ceiling gate (``_apply_spend_ceiling_gate``) requested ``FOR UPDATE`` on
that same tuple. Several backends each held ``FOR KEY SHARE`` and waited for
the others' ``FOR UPDATE`` upgrade — a classic foreign-key ``FOR KEY SHARE`` ->
``FOR UPDATE`` lock-upgrade cycle.

The gate now requests ``FOR NO KEY UPDATE`` (SQLAlchemy
``with_for_update(key_share=True)``), which does NOT conflict with
``FOR KEY SHARE`` and still conflicts with itself, so concurrent spend accrual
remains serialised while the upgrade deadlock is removed. The gate acquires the
lock and returns the accrual it decided on; the lifetime
``org_cumulative_spend_cents`` increment itself rides the ledger savepoint in
``_record_ledger_with_retry`` (FAR-391/#1499 money-correctness), which is
invoked by ``_ledger_block`` while the org lock is still held.

Why this is an integration test
-------------------------------
The defect is a real Postgres row-lock interaction: no mock produces a 40P01.
The test drives the REAL terminal ledger block (``_ledger_block``, which
invokes ``_apply_spend_ceiling_gate`` and the ledger-savepoint accrual) from N
independent sessions/transactions, each holding a real FK ``FOR KEY SHARE`` on
the org (via an ``audit_events`` insert, exactly as a finalising run records
facts). A barrier synchronises every session so ALL N hold their share lock
before ANY requests the org row lock, which makes the old ``FOR UPDATE`` code
deadlock deterministically.
"""

from __future__ import annotations

import asyncio
import uuid
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker

from modulo.core.cost_controller.finalize import _ledger_block
from modulo.db.models.run import Run

pytestmark = pytest.mark.integration

_N_RUNS = 8
_RUN_COST_USD = Decimal("1.00")
_RUN_COST_CENTS = 100
_CONCURRENCY_TIMEOUT_SECONDS = 120.0


async def _seed(engine: AsyncEngine) -> dict[str, Any]:
    """Commit one org plus N terminal runs (and the pipeline/snapshot they need)."""
    org_id = uuid.uuid4()
    account_id = uuid.uuid4()
    pipeline_id = uuid.uuid4()
    snapshot_id = uuid.uuid4()
    run_ids = [uuid.uuid4() for _ in range(_N_RUNS)]
    async with engine.begin() as conn:
        await conn.execute(
            text(
                "INSERT INTO organisations (id, name, slug, settings_json, otel_config_json, "
                "org_cumulative_spend_cents) "
                "VALUES (:id, :name, :slug, '{}'::json, '{}'::json, 0)"
            ),
            {"id": str(org_id), "name": "FAR-1624 deadlock org", "slug": f"far1624-{org_id.hex[:8]}"},
        )
        await conn.execute(
            text(
                "INSERT INTO accounts (id, email, display_name, password_hash, auth_provider, active) "
                "VALUES (:id, :email, :name, 'hash', 'local', true)"
            ),
            {
                "id": str(account_id),
                "email": f"far1624-{account_id.hex[:8]}@example.com",
                "name": "FAR-1624",
            },
        )
        await conn.execute(
            text(
                "INSERT INTO pipelines (id, organisation_id, account_id, name, graph_nodes_json, "
                "run_context_defaults, visibility, max_concurrent_runs) "
                "VALUES (:id, :oid, :aid, :name, '[]'::json, '{}'::json, 'org', 50)"
            ),
            {
                "id": str(pipeline_id),
                "oid": str(org_id),
                "aid": str(account_id),
                "name": f"far1624-{pipeline_id.hex[:8]}",
            },
        )
        await conn.execute(
            text(
                "INSERT INTO pipeline_snapshots (id, organisation_id, pipeline_id, snapshot_version, "
                "account_id, graph_json, connector_bindings_json, schema_pins_json, prompt_pins_json, "
                "model_backend_pins_json, composite_bindings_json, run_context_defaults) "
                "VALUES (:id, :oid, :pid, 1, :aid, '{}'::json, '[]'::json, '[]'::json, '[]'::json, "
                "'[]'::json, '[]'::json, '{}'::json)"
            ),
            {"id": str(snapshot_id), "oid": str(org_id), "pid": str(pipeline_id), "aid": str(account_id)},
        )
        for index, run_id in enumerate(run_ids):
            await conn.execute(
                text(
                    "INSERT INTO runs (id, organisation_id, pipeline_id, snapshot_id, account_id, "
                    "trigger_type, status, input_hash, langgraph_thread_id, run_number) "
                    "VALUES (:id, :oid, :pid, :sid, :aid, 'manual', 'complete', 'hash', :thread, :rn)"
                ),
                {
                    "id": str(run_id),
                    "oid": str(org_id),
                    "pid": str(pipeline_id),
                    "sid": str(snapshot_id),
                    "aid": str(account_id),
                    "thread": f"far1624:{org_id}:{run_id}",
                    "rn": index + 1,
                },
            )
    return {"org_id": org_id, "account_id": account_id, "run_ids": run_ids}


async def _cleanup(engine: AsyncEngine, seed: dict[str, Any]) -> None:
    org_id = seed["org_id"]
    async with engine.begin() as conn:
        # Child-first, so no RESTRICT FK blocks a parent delete. audit_events is
        # append-only at trigger depth 1, but the org cascade (migration 0285)
        # runs at depth >= 2 and is permitted.
        await conn.execute(text("DELETE FROM runs WHERE organisation_id = :oid"), {"oid": str(org_id)})
        await conn.execute(text("DELETE FROM pipeline_snapshots WHERE organisation_id = :oid"), {"oid": str(org_id)})
        await conn.execute(text("DELETE FROM pipelines WHERE organisation_id = :oid"), {"oid": str(org_id)})
        await conn.execute(text("DELETE FROM organisations WHERE id = :oid"), {"oid": str(org_id)})
        await conn.execute(text("DELETE FROM accounts WHERE id = :aid"), {"aid": str(seed["account_id"])})


async def _finalise_one(
    factory: Any,
    org_id: uuid.UUID,
    run_id: uuid.UUID,
    barrier: asyncio.Barrier,
) -> None:
    """One run's finalisation: its own session + transaction."""
    async with factory() as session, session.begin():
        # Real FK insert: references the organisation, taking FOR KEY SHARE on
        # the org tuple — exactly what a finalising transaction does when it
        # writes audit/cost rows before the spend-ceiling gate runs.
        await session.execute(
            text(
                "INSERT INTO audit_events "
                "(id, organisation_id, event_type, payload_json, created_at, updated_at) "
                "VALUES (:id, :oid, 'far1624.finalize_probe', '{}'::jsonb, now(), now())"
            ),
            {"id": str(uuid.uuid4()), "oid": str(org_id)},
        )
        # Deterministic reproduction: hold every FOR KEY SHARE before ANY
        # transaction requests the org row lock (the old FOR UPDATE upgrade).
        await barrier.wait()
        run = await session.get(Run, run_id)
        assert run is not None
        # The REAL terminal ledger block: the ceiling gate acquires the org
        # FOR NO KEY UPDATE lock, then the lifetime accrual is applied inside
        # the ledger savepoint — all within this transaction, exactly as
        # production finalisation does.
        await _ledger_block(
            session,
            run_id=run_id,
            org_id=org_id,
            status="complete",
            total=_RUN_COST_USD,
            owner_team_id=None,
            run_date=datetime.now(UTC).date(),
            finalize_fields={},
            session_factory=None,
        )


async def test_concurrent_finalisations_do_not_deadlock(db_engine: AsyncEngine) -> None:
    seed = await _seed(db_engine)
    org_id: uuid.UUID = seed["org_id"]
    run_ids: list[uuid.UUID] = seed["run_ids"]
    factory = async_sessionmaker(db_engine, expire_on_commit=False)
    barrier = asyncio.Barrier(_N_RUNS)

    try:
        results = await asyncio.wait_for(
            asyncio.gather(
                *(_finalise_one(factory, org_id, run_id, barrier) for run_id in run_ids),
                return_exceptions=True,
            ),
            timeout=_CONCURRENCY_TIMEOUT_SECONDS,
        )

        failures = [r for r in results if isinstance(r, BaseException)]
        assert not failures, "concurrent finalisations failed: " + "; ".join(repr(f) for f in failures)
        assert "deadlock detected" not in " ".join(repr(r) for r in results).lower()

        async with db_engine.connect() as conn:
            accrued = await conn.scalar(
                text("SELECT org_cumulative_spend_cents FROM organisations WHERE id = :oid"),
                {"oid": str(org_id)},
            )
        # Every run accrued exactly once — the NU lock serialised the RMW.
        assert accrued == _RUN_COST_CENTS * _N_RUNS
    finally:
        await _cleanup(db_engine, seed)
