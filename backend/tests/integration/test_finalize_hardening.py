"""Integration tests for run-finalisation hardening (real Postgres).

Two of the three finalisation-path fixes need a real database to observe:

  1. the transaction-scoped row-lock bound (``SET LOCAL lock_timeout``) is set
     at the finalisation entry point BEFORE any lock, so a contended
     finalisation surfaces as a bounded 55P03 rather than queuing on
     wall-clock;
  1b. the reduced escape's FRESH transaction sets the same bound BEFORE its
     row-locking ``update_run_status`` (FAR-1642 item 1);
  3. the org accrual is committed IFF the ledger write succeeds — a contained
     ledger-write failure followed by a re-finalisation must NOT double-count
     ``org_cumulative_spend_cents``.

(Fix 2, the bounded whole-transaction 40P01/55P03 retry, is owned by the
executor's transaction layer and is unit-tested in
``tests/unit/core/test_executor_spend_ceiling_gate.py``.)

Requires a running Postgres via testcontainers (``pytest.mark.integration``).
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from decimal import Decimal
from unittest.mock import AsyncMock, patch

import pytest
import pytest_asyncio
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from modulo.core.cost_controller.finalize import (
    _ledger_block,
    _LedgerEscapeContext,
    _reduced_escape,
    finalize_cost,
)
from modulo.db.models.run import Run

pytestmark = pytest.mark.integration

_SPEND_CEILING_CENTS = 10_000  # $100 — the test runs stay well under it
_RUN_COST = Decimal("3.00")  # -> 300 cents


# ---------------------------------------------------------------------------
# Seed helpers
# ---------------------------------------------------------------------------


async def _seed_org(db_engine: AsyncEngine, name: str) -> uuid.UUID:
    org_id = uuid.uuid4()
    async with db_engine.connect() as conn, conn.begin():
        await conn.execute(
            text(
                "INSERT INTO organisations (id, name, slug, settings_json, "
                "spend_ceiling_cents, org_cumulative_spend_cents) "
                "VALUES (:id, :name, :slug, '{}'::json, :ceiling, 0)",
            ),
            {
                "id": str(org_id),
                "name": name,
                "slug": f"{name.lower()}-{org_id.hex[:8]}",
                "ceiling": _SPEND_CEILING_CENTS,
            },
        )
    return org_id


async def _seed_account(db_engine: AsyncEngine, org_id: uuid.UUID, email: str) -> uuid.UUID:
    account_id = uuid.uuid4()
    async with db_engine.connect() as conn, conn.begin():
        await conn.execute(
            text(
                "INSERT INTO accounts (id, email, display_name, password_hash, "
                "auth_provider, active) "
                "VALUES (:id, :email, :name, 'hash', 'local', true)",
            ),
            {"id": str(account_id), "email": email, "name": email.split("@", maxsplit=1)[0]},
        )
        await conn.execute(
            text(
                "INSERT INTO org_memberships (id, account_id, organisation_id, role) "
                "VALUES (:mid, :aid, :oid, 'admin')",
            ),
            {"mid": str(uuid.uuid4()), "aid": str(account_id), "oid": str(org_id)},
        )
    return account_id


async def _seed_pipeline(db_engine: AsyncEngine, org_id: uuid.UUID, account_id: uuid.UUID) -> uuid.UUID:
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
                "oid": str(org_id),
                "name": f"Finalize Hardening {pipeline_id.hex[:8]}",
                "uid": str(account_id),
            },
        )
    return pipeline_id


async def _seed_snapshot(db_engine: AsyncEngine, org_id: uuid.UUID, pipeline_id: uuid.UUID) -> uuid.UUID:
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
            {"id": str(snapshot_id), "pid": str(pipeline_id), "oid": str(org_id)},
        )
    return snapshot_id


async def _insert_run(
    session: AsyncSession,
    *,
    org_id: uuid.UUID,
    pipeline_id: uuid.UUID,
    snapshot_id: uuid.UUID,
) -> uuid.UUID:
    run_id = uuid.uuid4()
    await session.execute(
        Run.__table__.insert().values(
            id=run_id,
            organisation_id=org_id,
            pipeline_id=pipeline_id,
            snapshot_id=snapshot_id,
            trigger_type="manual",
            status="running",
            input_hash=uuid.uuid4().hex,
            langgraph_thread_id=f"thread-{run_id.hex[:16]}",
            run_number=int(run_id.int % 10**9) + 1,
            started_at=datetime(2026, 6, 24, 10, 0, tzinfo=UTC),
        )
    )
    return run_id


async def _set_org_rls(session: AsyncSession, org_id: uuid.UUID) -> None:
    await session.execute(
        text("SELECT set_config('app.organisation_id', :oid, true)"),
        {"oid": str(org_id)},
    )


async def _org_cumulative_cents(session: AsyncSession, org_id: uuid.UUID) -> int:
    await _set_org_rls(session, org_id)
    return int(
        (
            await session.execute(
                text("SELECT org_cumulative_spend_cents FROM organisations WHERE id = :oid"),
                {"oid": str(org_id)},
            )
        ).scalar_one()
    )


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def finalize_env(db_engine: AsyncEngine) -> tuple[uuid.UUID, uuid.UUID, uuid.UUID]:
    org_id = await _seed_org(db_engine, f"FinalizeHardening {uuid.uuid4().hex[:8]}")
    account_id = await _seed_account(db_engine, org_id, f"fin-{uuid.uuid4().hex[:8]}@test.local")
    pipeline_id = await _seed_pipeline(db_engine, org_id, account_id)
    snapshot_id = await _seed_snapshot(db_engine, org_id, pipeline_id)
    return org_id, pipeline_id, snapshot_id


# ---------------------------------------------------------------------------
# Fix 1 — transaction-scoped lock bound
# ---------------------------------------------------------------------------


async def test_finalize_sets_transaction_scoped_lock_timeout(
    db_session: AsyncSession,
    finalize_env: tuple[uuid.UUID, uuid.UUID, uuid.UUID],
) -> None:
    """``finalize_cost`` bounds every row-lock wait for the rest of the caller's
    transaction (SET LOCAL lock_timeout), and the bound reverts on commit."""
    org_id, pipeline_id, snapshot_id = finalize_env
    run_id = await _insert_run(db_session, org_id=org_id, pipeline_id=pipeline_id, snapshot_id=snapshot_id)
    await db_session.commit()

    async with db_session.begin():
        await _set_org_rls(db_session, org_id)
        before = (await db_session.execute(text("SHOW lock_timeout"))).scalar_one()
        assert before == "0", f"expected the server default (unbounded) before finalisation, got {before!r}"

        await finalize_cost(
            db_session,
            run_id=run_id,
            org_id=org_id,
            status="complete",
            segment_node_token_usage=None,
            segment_completed_node_outputs=None,
            node_type_map={},
            is_terminal=True,
        )

        after = (await db_session.execute(text("SHOW lock_timeout"))).scalar_one()
        assert after != "0", "finalize_cost must bound the finalisation transaction's row-lock waits"

    # SET LOCAL: the bound must NOT leak past the transaction (never pooled).
    async with db_session.begin():
        reverted = (await db_session.execute(text("SHOW lock_timeout"))).scalar_one()
        assert reverted == "0", "the lock bound is transaction-scoped and must revert on commit"


async def test_reduced_escape_bounds_lock_timeout_before_status_write(
    db_engine: AsyncEngine,
    db_session: AsyncSession,
    finalize_env: tuple[uuid.UUID, uuid.UUID, uuid.UUID],
) -> None:
    """FAR-1642 item 1: the reduced escape's FRESH transaction must bound its
    row-lock waits BEFORE the row-locking ``update_run_status``.

    The reduced escape runs while the (aborted/rolled-back) outer transaction
    may still hold the ``runs`` ``FOR UPDATE``, so an unbounded wait on the
    status write is exactly the wall-clock hang the finalisation lock bound
    exists to prevent. The observation is taken INSIDE the fresh transaction at
    the moment the status write would take its lock: ``SHOW lock_timeout`` must
    already report the bound (non-zero) rather than the unbounded default.
    """
    org_id, pipeline_id, snapshot_id = finalize_env
    run_id = await _insert_run(db_session, org_id=org_id, pipeline_id=pipeline_id, snapshot_id=snapshot_id)
    await db_session.commit()

    observed: dict[str, str] = {}

    async def _capturing_update_run_status(
        session: AsyncSession,
        _run_id: uuid.UUID,
        _status: str,
        **_kwargs: object,
    ) -> None:
        # This runs where the real, row-locking ``update_run_status`` would —
        # the fresh transaction must already carry the bounded lock_timeout.
        observed["lock_timeout"] = (await session.execute(text("SHOW lock_timeout"))).scalar_one()

    ctx = _LedgerEscapeContext(
        run_id=run_id,
        org_id=org_id,
        status="complete",
        finalize_fields={},
        session_factory=async_sessionmaker(bind=db_engine, expire_on_commit=False),
        claim_token=None,
    )

    with patch(
        "modulo.core.cost_controller.finalize.update_run_status",
        new=_capturing_update_run_status,
    ):
        await _reduced_escape(db_session, ctx)

    assert "lock_timeout" in observed, "the reduced escape must reach its status write"
    assert observed["lock_timeout"] != "0", (
        "the reduced escape must SET LOCAL lock_timeout before its row-locking status write"
    )


# ---------------------------------------------------------------------------
# Fix 3 — accrual double-count on a contained ledger-write failure
# ---------------------------------------------------------------------------


async def test_ledger_write_failure_then_refinalize_does_not_double_accrue(
    db_session: AsyncSession,
    finalize_env: tuple[uuid.UUID, uuid.UUID, uuid.UUID],
) -> None:
    """A non-abort ledger-write failure must roll the org accrual back WITH the
    ledger; a later re-finalisation then accrues exactly once.

    Regression for the pre-existing exposure where the org accrual was flushed
    in the OUTER transaction (outside the ledger savepoint), so a contained
    ledger-write failure committed the accrual without the ledger and a later
    re-finalisation (``ledger_written`` still false) re-accrued — double count.
    """
    org_id, pipeline_id, snapshot_id = finalize_env
    run_id = await _insert_run(db_session, org_id=org_id, pipeline_id=pipeline_id, snapshot_id=snapshot_id)
    await db_session.commit()
    run_date = datetime(2026, 6, 24, 10, 0, tzinfo=UTC).date()

    # --- First finalisation: the ledger write fails (non-abort). ---
    async with db_session.begin():
        await _set_org_rls(db_session, org_id)
        run = (await db_session.execute(select(Run).where(Run.id == run_id))).scalar_one()
        with patch(
            "modulo.core.cost_controller.finalize.check_and_record_spend",
            new=AsyncMock(side_effect=RuntimeError("ledger write failed")),
        ):
            await _ledger_block(
                db_session,
                run_id=run_id,
                org_id=org_id,
                status="complete",
                total=_RUN_COST,
                owner_team_id=None,
                run_date=run_date,
                finalize_fields={},
                session_factory=None,
                claim_token=None,
            )

    # No accrual committed, and the run is left re-finalisable.
    async with db_session.begin():
        await _set_org_rls(db_session, org_id)
        assert await _org_cumulative_cents(db_session, org_id) == 0
        assert run.ledger_written is False

    # --- Re-finalisation: the ledger write now succeeds. ---
    async with db_session.begin():
        await _set_org_rls(db_session, org_id)
        run = (await db_session.execute(select(Run).where(Run.id == run_id))).scalar_one()
        with (
            patch(
                "modulo.core.cost_controller.finalize.check_and_record_spend",
                new=AsyncMock(return_value=(True, None)),
            ),
            patch("modulo.core.cost_controller.finalize._check_circuit_breaker", new=AsyncMock()),
        ):
            await _ledger_block(
                db_session,
                run_id=run_id,
                org_id=org_id,
                status="complete",
                total=_RUN_COST,
                owner_team_id=None,
                run_date=run_date,
                finalize_fields={},
                session_factory=None,
                claim_token=None,
            )

    async with db_session.begin():
        await _set_org_rls(db_session, org_id)
        cumulative = await _org_cumulative_cents(db_session, org_id)
        assert cumulative == 300, f"expected exactly one accrual (300), got {cumulative}"
        assert run.ledger_written is True
