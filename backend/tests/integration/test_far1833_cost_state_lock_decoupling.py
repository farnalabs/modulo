"""FAR-1833 — finalize cost path no longer serialises on the ``organisations`` row.

Two real-Postgres integration assertions (testcontainers):

1. **No serialisation** — a finalize accrual proceeds while an admin cost-config
   patch holds an uncommitted ``UPDATE organisations`` (a FOR NO KEY UPDATE on
   the org row). Under the retired A22 org-row lock the gate queued on that
   write; the gate now takes the per-org cost-state advisory lock and never
   locks the org row, so the two no longer block each other.
2. **No race / bounded degradation** — a contended cost-state advisory lock
   (ceiling set/clear vs accrual) surfaces as a BOUNDED SQLSTATE 55P03 wait, NOT
   a 40P01 abort.

Both need a live Postgres: no mock produces the lock interaction or the SQLSTATE.
"""

from __future__ import annotations

import asyncio
import time
import uuid
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from modulo.core.cost_controller.finalize import _ledger_block
from modulo.core.runner_capacity import lock_org_cost_state
from modulo.db.sqlstates import sqlstate_of

pytestmark = pytest.mark.integration

_RUN_COST_USD = Decimal("1.00")
_RUN_COST_CENTS = 100
_RUN_DATE = datetime(2026, 6, 24, tzinfo=UTC).date()
_BOUNDED_WAIT_TIMEOUT_SECONDS = 10.0
_SHORT_LOCK_TIMEOUT_MS = 300


async def _seed(db_engine: AsyncEngine) -> dict[str, Any]:
    """One org (with a cost-state row) plus one terminal run."""
    org_id = uuid.uuid4()
    account_id = uuid.uuid4()
    pipeline_id = uuid.uuid4()
    snapshot_id = uuid.uuid4()
    run_id = uuid.uuid4()
    async with db_engine.begin() as conn:
        await conn.execute(
            text(
                "INSERT INTO organisations (id, name, slug, settings_json, otel_config_json) "
                "VALUES (:id, :name, :slug, '{}'::json, '{}'::json)"
            ),
            {"id": str(org_id), "name": "FAR-1833 cost-state org", "slug": f"far1833-{org_id.hex[:8]}"},
        )
        await conn.execute(
            text(
                "INSERT INTO organisation_cost_state (org_id, org_cumulative_spend_cents) VALUES (:id, 0)",
            ),
            {"id": str(org_id)},
        )
        await conn.execute(
            text(
                "INSERT INTO accounts (id, email, display_name, password_hash, auth_provider, active) "
                "VALUES (:id, :email, :name, 'hash', 'local', true)"
            ),
            {"id": str(account_id), "email": f"far1833-{account_id.hex[:8]}@example.com", "name": "FAR-1833"},
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
                "name": f"far1833-{pipeline_id.hex[:8]}",
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
        await conn.execute(
            text(
                "INSERT INTO runs (id, organisation_id, pipeline_id, snapshot_id, account_id, "
                "trigger_type, status, input_hash, langgraph_thread_id, run_number) "
                "VALUES (:id, :oid, :pid, :sid, :aid, 'manual', 'complete', 'hash', :thread, 1)"
            ),
            {
                "id": str(run_id),
                "oid": str(org_id),
                "pid": str(pipeline_id),
                "sid": str(snapshot_id),
                "aid": str(account_id),
                "thread": f"far1833:{org_id}:{run_id}",
            },
        )
    return {"org_id": org_id, "account_id": account_id, "run_id": run_id}


async def _cleanup(engine: AsyncEngine, seed: dict[str, Any]) -> None:
    org_id = seed["org_id"]
    async with engine.begin() as conn:
        await conn.execute(text("DELETE FROM runs WHERE organisation_id = :oid"), {"oid": str(org_id)})
        await conn.execute(text("DELETE FROM pipeline_snapshots WHERE organisation_id = :oid"), {"oid": str(org_id)})
        await conn.execute(text("DELETE FROM pipelines WHERE organisation_id = :oid"), {"oid": str(org_id)})
        await conn.execute(text("DELETE FROM organisations WHERE id = :oid"), {"oid": str(org_id)})
        await conn.execute(text("DELETE FROM accounts WHERE id = :aid"), {"aid": str(seed["account_id"])})


async def _finalize(
    factory: async_sessionmaker[AsyncSession],
    org_id: uuid.UUID,
    run_id: uuid.UUID,
    *,
    fk_share_probe: bool = True,
) -> None:
    """Run the REAL terminal ledger block for one run in its own transaction."""
    async with factory() as session, session.begin():
        if fk_share_probe:
            # Mirror production: the finalising transaction inserts FK-referencing
            # rows (taking FOR KEY SHARE on the org tuple) before the gate runs.
            await session.execute(
                text(
                    "INSERT INTO audit_events "
                    "(id, organisation_id, event_type, payload_json, created_at, updated_at) "
                    "VALUES (:id, :oid, 'far1833.finalize_probe', '{}'::jsonb, now(), now())"
                ),
                {"id": str(uuid.uuid4()), "oid": str(org_id)},
            )
        await _ledger_block(
            session,
            run_id=run_id,
            org_id=org_id,
            status="complete",
            total=_RUN_COST_USD,
            owner_team_id=None,
            run_date=_RUN_DATE,
            finalize_fields={},
            session_factory=None,
        )


async def _accrued_cents(engine: AsyncEngine, org_id: uuid.UUID) -> int:
    async with engine.connect() as conn:
        value = await conn.scalar(
            text("SELECT org_cumulative_spend_cents FROM organisation_cost_state WHERE org_id = :oid"),
            {"oid": str(org_id)},
        )
    return int(value)


async def test_finalize_accrual_proceeds_while_org_row_write_is_held(db_engine: AsyncEngine) -> None:
    """No serialisation: finalize accrues while an admin holds an uncommitted
    ``UPDATE organisations`` on the SAME org row.

    The admin's UPDATE takes FOR NO KEY UPDATE on the org row. The old gate
    requested FOR NO KEY UPDATE on that same row too, so it queued behind the
    admin (and the D9 deadlock shape lived here). The FAR-1833 gate takes no org
    row lock, so the finalisation completes without waiting.
    """
    seed = await _seed(db_engine)
    org_id: uuid.UUID = seed["org_id"]
    run_id: uuid.UUID = seed["run_id"]
    factory = async_sessionmaker(db_engine, expire_on_commit=False)

    admin = factory()
    await admin.begin()
    try:
        # Admin cost-config patch: an uncommitted row write on the organisations row.
        await admin.execute(
            text("UPDATE organisations SET daily_spend_limit = 5 WHERE id = :oid"),
            {"oid": str(org_id)},
        )
        # Must NOT block behind the held org-row write.
        await asyncio.wait_for(
            _finalize(factory, org_id, run_id),
            timeout=_BOUNDED_WAIT_TIMEOUT_SECONDS,
        )
        accrued = await _accrued_cents(db_engine, org_id)
        assert accrued == _RUN_COST_CENTS
    finally:
        await admin.rollback()
        await admin.close()
        await _cleanup(db_engine, seed)


async def test_contended_cost_state_lock_degrades_to_bounded_55p03_not_40p01(
    db_engine: AsyncEngine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No race: a contended cost-state advisory lock is a BOUNDED 55P03 wait.

    A concurrent holder takes the per-org cost-state advisory lock. The finalize
    gate blocks on the SAME lock, but the transaction-scoped ``lock_timeout``
    bound (set by ``lock_org_cost_state``) turns the wait into a SQLSTATE 55P03
    ``lock_not_available`` — a retryable, bounded degradation — never an
    unbounded wait and never a 40P01 deadlock abort.
    """
    monkeypatch.setenv("MUTATION_ROW_LOCK_TIMEOUT_MS", str(_SHORT_LOCK_TIMEOUT_MS))
    from modulo.settings import get_settings

    get_settings.cache_clear()

    seed = await _seed(db_engine)
    org_id: uuid.UUID = seed["org_id"]
    run_id: uuid.UUID = seed["run_id"]
    factory = async_sessionmaker(db_engine, expire_on_commit=False)

    holder = factory()
    await holder.begin()
    try:
        await lock_org_cost_state(holder, org_id)  # holds the advisory lock
        started = time.monotonic()
        with pytest.raises(DBAPIError) as excinfo:
            await _finalize(factory, org_id, run_id, fk_share_probe=False)
        elapsed = time.monotonic() - started
    finally:
        await holder.rollback()
        await holder.close()
        await _cleanup(db_engine, seed)

    assert sqlstate_of(excinfo.value) == "55P03", (
        f"expected a bounded 55P03 lock_timeout, got {sqlstate_of(excinfo.value)!r}"
    )
    # Bounded: the wait was capped by the (short) lock_timeout, not unbounded.
    assert elapsed < _BOUNDED_WAIT_TIMEOUT_SECONDS, f"lock wait not bounded (elapsed {elapsed:.2f}s)"
