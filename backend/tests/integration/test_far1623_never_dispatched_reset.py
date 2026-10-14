"""FAR-1623: the never-dispatched sweep must not terminal-fail a run the
heartbeat-stale slot reconciliation just reset for retry.

Two periodic sweeps used to fight over the same row:

* ``run_admission.reconcile_pipeline_slots`` (the FAR-779/812 heartbeat-stale
  slot reconciliation) resets a stale ``running`` run to ``pending`` and NULLs
  ``dispatched_at``/``dispatcher``/``heartbeat_at``, stamping
  ``error_code='heartbeat_stale'`` so ``dispatcher_reconcile`` re-dispatches it
  on its next 60s tick.
* ``pipeline_execution._sweep_org_stale_runs`` (the "never dispatched" branch,
  driven by ``stale_run_recovery_sweep``) terminal-failed ANY ``pending`` run
  with ``dispatched_at IS NULL``, ``dispatcher IS NULL`` and ``error_code``
  outside the capacity-timeout codes. The just-reset run matched immediately,
  so the FAR-779 auto-retry was dead code in practice (FAR-1603 RCA: 34/34
  ``harness.dispatch_failed`` deaths).

These tests run against a REAL Postgres and drive the REAL code the two sweeps
execute: the reset through ``reconcile_pipeline_slots`` (scoped to this test's
own org) and the never-dispatched branch through ``_sweep_org_stale_runs`` — the
exact per-org helper ``stale_run_recovery_sweep`` calls. A mocked or SQLite
double cannot catch this class of defect: it is a predicate collision in real
SQL against the real ``runs`` row shape.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncGenerator
from datetime import UTC, datetime, timedelta
from typing import NamedTuple
from unittest.mock import AsyncMock, patch

import pytest
import pytest_asyncio
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncEngine

from modulo.core.cron_helpers import _build_re_dispatch_predicate
from modulo.core.pipeline_execution import _sweep_org_stale_runs
from modulo.core.run_admission import reconcile_pipeline_slots
from modulo.db.models.run import Run

pytestmark = pytest.mark.integration

# Window the reset uses to decide a running run's heartbeat is stale. The
# seeded stale heartbeat is an hour old, comfortably beyond it.
_STALE_SECONDS = 60
# Windows the never-dispatched branch uses. Seeded rows are an hour old, so
# they fall inside both.
_ND_WINDOW = 300
_WL_WINDOW = 600

_OLD_MINUTES = 60


class _Env(NamedTuple):
    """A dedicated org + pipeline + snapshot + account for one test."""

    org_id: uuid.UUID
    pipeline_id: uuid.UUID
    snapshot_id: uuid.UUID
    account_id: uuid.UUID


async def _seed_env(db_engine: AsyncEngine) -> _Env:
    org_id = uuid.uuid4()
    account_id = uuid.uuid4()
    pipeline_id = uuid.uuid4()
    snapshot_id = uuid.uuid4()
    async with db_engine.begin() as conn:
        await conn.execute(
            text(
                "INSERT INTO organisations (id, name, slug, settings_json) VALUES (:id, :name, :slug, '{}'::json)",
            ),
            {"id": str(org_id), "name": "FAR-1623 Org", "slug": f"far1623-{org_id.hex[:8]}"},
        )
        await conn.execute(
            text(
                "INSERT INTO accounts (id, email, display_name, password_hash, auth_provider, active) "
                "VALUES (:id, :email, :name, 'hash', 'local', true)",
            ),
            {"id": str(account_id), "email": f"far1623-{account_id.hex[:8]}@test.local", "name": "FAR-1623 User"},
        )
        await conn.execute(
            text(
                "INSERT INTO pipelines (id, organisation_id, name, account_id, max_concurrent_runs, "
                "lock_wait_timeout_seconds, node_timeout_seconds, run_context_defaults, graph_nodes_json, "
                "default_autonomy_level, visibility) "
                "VALUES (:id, :oid, :name, :aid, 10, 30, 300, '{}'::json, '[]'::json, 'manual_approval', 'org')",
            ),
            {
                "id": str(pipeline_id),
                "oid": str(org_id),
                "name": f"far1623-{pipeline_id.hex[:8]}",
                "aid": str(account_id),
            },
        )
        await conn.execute(
            text(
                "INSERT INTO pipeline_snapshots (id, pipeline_id, organisation_id, snapshot_version, "
                "graph_json, connector_bindings_json, schema_pins_json, prompt_pins_json, "
                "model_backend_pins_json, run_context_defaults, config_json) "
                "VALUES (:id, :pid, :oid, 1, '{}'::json, '[]'::json, '[]'::json, '[]'::json, "
                "'[]'::json, '{}'::json, '{}'::json)",
            ),
            {"id": str(snapshot_id), "pid": str(pipeline_id), "oid": str(org_id)},
        )
    return _Env(org_id=org_id, pipeline_id=pipeline_id, snapshot_id=snapshot_id, account_id=account_id)


async def _seed_run(
    db_engine: AsyncEngine,
    env: _Env,
    *,
    status: str,
    claim_count: int,
    run_number: int,
    heartbeat_at: datetime | None,
    dispatched_at: datetime | None,
    dispatcher: str | None,
    error_code: str | None,
) -> uuid.UUID:
    run_id = uuid.uuid4()
    created_at = datetime.now(UTC) - timedelta(minutes=_OLD_MINUTES)
    async with db_engine.begin() as conn:
        await conn.execute(
            text(
                "INSERT INTO runs (id, organisation_id, pipeline_id, snapshot_id, account_id, "
                "trigger_type, status, input_hash, langgraph_thread_id, run_number, claim_count, "
                "dispatcher, heartbeat_at, dispatched_at, error_code, created_at, started_at) "
                "VALUES (:id, :oid, :pid, :sid, :uid, 'manual', :status, 'hash', :thread, :rn, "
                ":claims, :dispatcher, :heartbeat_at, :dispatched_at, :error_code, :created_at, :created_at)",
            ),
            {
                "id": str(run_id),
                "oid": str(env.org_id),
                "pid": str(env.pipeline_id),
                "sid": str(env.snapshot_id),
                "uid": str(env.account_id),
                "status": status,
                "thread": f"{env.org_id}:{run_id}",
                "rn": run_number,
                "claims": claim_count,
                "dispatcher": dispatcher,
                "heartbeat_at": heartbeat_at,
                "dispatched_at": dispatched_at,
                "error_code": error_code,
                "created_at": created_at,
            },
        )
    return run_id


async def _fetch_run(db_engine: AsyncEngine, run_id: uuid.UUID) -> dict[str, object]:
    async with db_engine.connect() as conn:
        result = await conn.execute(
            text(
                "SELECT status, error_code, dispatched_at, dispatcher, heartbeat_at, claim_count "
                "FROM runs WHERE id = :id",
            ),
            {"id": str(run_id)},
        )
        row = result.one()
    return {
        "status": row.status,
        "error_code": row.error_code,
        "dispatched_at": row.dispatched_at,
        "dispatcher": row.dispatcher,
        "heartbeat_at": row.heartbeat_at,
        "claim_count": row.claim_count,
    }


async def _sweep_never_dispatched(db_engine: AsyncEngine, org_id: uuid.UUID) -> tuple[int, int, int]:
    async with db_engine.connect() as conn, conn.begin():
        return await _sweep_org_stale_runs(
            conn,
            org_id=org_id,
            nd_window=_ND_WINDOW,
            wl_window=_WL_WINDOW,
            stranded_rows=[],
            terminalised_run_ids=[],
        )


async def _run_matches_re_dispatch_predicate(db_engine: AsyncEngine, run_id: uuid.UUID) -> bool:
    """True when the run is admitted by the REAL ``dispatcher_reconcile``
    re-dispatch predicate (F3c/F6a) — the exact admission criterion the 60s
    reconcile scans with. Evaluated as a real SQL query against the seeded row,
    so it proves the run's actual status/dispatched_at values match, not merely
    the predicate's structure."""
    predicate = _build_re_dispatch_predicate(
        reenqueue_window=60,
        stale_window=60,
        capacity_redispatch_seconds=60,
    )
    async with db_engine.connect() as conn:
        matched = (await conn.execute(select(Run.id).where(predicate, Run.id == run_id))).scalar_one_or_none()
    return matched is not None


async def _teardown_env(db_engine: AsyncEngine, env: _Env) -> None:
    async with db_engine.begin() as conn:
        await conn.execute(text("DELETE FROM runs WHERE organisation_id = :oid"), {"oid": str(env.org_id)})
        await conn.execute(
            text("DELETE FROM pipeline_snapshots WHERE organisation_id = :oid"),
            {"oid": str(env.org_id)},
        )
        await conn.execute(text("DELETE FROM pipelines WHERE organisation_id = :oid"), {"oid": str(env.org_id)})
        await conn.execute(text("DELETE FROM accounts WHERE id = :id"), {"id": str(env.account_id)})
        await conn.execute(text("DELETE FROM organisations WHERE id = :oid"), {"oid": str(env.org_id)})


@pytest_asyncio.fixture
async def far1623_env(db_engine: AsyncEngine) -> AsyncGenerator[_Env, None]:
    """A dedicated org seeded fresh per test, torn down afterwards.

    A dedicated org keeps the sweep assertions isolated: the sweep helpers are
    called for THIS org only, so a sibling integration test's runs can never
    change the counts.
    """
    env = await _seed_env(db_engine)
    try:
        yield env
    finally:
        await _teardown_env(db_engine, env)


async def test_heartbeat_stale_reset_run_is_not_never_dispatched(
    db_engine: AsyncEngine,
    far1623_env: _Env,
) -> None:
    """A run reset for retry survives the never-dispatched sweep.

    Reproduces the FAR-1603 fight: a deploy-orphaned ``running`` run with a
    stale heartbeat is reset to ``pending`` by the slot reconciliation (FAR-779
    retry), then the never-dispatched branch must leave it alone so
    ``dispatcher_reconcile`` can re-dispatch it instead of killing it.
    """
    stale = datetime.now(UTC) - timedelta(minutes=_OLD_MINUTES)
    run_id = await _seed_run(
        db_engine,
        far1623_env,
        status="running",
        claim_count=1,
        run_number=1,
        heartbeat_at=stale,
        dispatched_at=stale,
        dispatcher="saq",
        error_code=None,
    )

    # Step 1 — the heartbeat-stale slot reconciliation resets the run to
    # pending. ``_sweep_org_ids`` is scoped to this test's org so the real
    # sweep never touches another test's rows.
    with patch(
        "modulo.core.run_admission._sweep_org_ids",
        new=AsyncMock(return_value=[far1623_env.org_id]),
    ):
        reset = await reconcile_pipeline_slots(db_engine, stale_seconds=_STALE_SECONDS)

    assert reset["released"] == 0
    assert reset["retried"] == 1
    after_reset = await _fetch_run(db_engine, run_id)
    assert after_reset["status"] == "pending"
    assert after_reset["error_code"] == "heartbeat_stale"
    assert after_reset["dispatched_at"] is None
    assert after_reset["dispatcher"] is None
    assert after_reset["claim_count"] == 1

    # Step 2 — the never-dispatched branch must NOT match the just-reset run
    # (claim_count >= 1) even though status/dispatched_at/created_at all fit.
    never, capacity, lost = await _sweep_never_dispatched(db_engine, far1623_env.org_id)

    assert never == 0
    assert capacity == 0
    assert lost == 0
    after_sweep = await _fetch_run(db_engine, run_id)
    assert after_sweep["status"] == "pending"
    assert after_sweep["error_code"] == "heartbeat_stale"

    # Step 3 — the spared run is now ADMITTED by the re-dispatch predicate, so
    # the reconcile path actually re-dispatches it. This is the positive
    # recovery outcome the "never == 0" assertion alone does not prove: being
    # spared is only useful if the run is then picked back up. Exercised as a
    # real SQL match against the run's post-reset row (predicate-match, not a
    # predicate-structure check; driving ``dispatcher_reconcile`` end-to-end
    # would need the SAQ harness and an org-wide scan with unrelated side
    # effects).
    assert after_sweep["dispatched_at"] is None
    assert await _run_matches_re_dispatch_predicate(db_engine, run_id)


async def test_genuinely_never_dispatched_run_still_fails(
    db_engine: AsyncEngine,
    far1623_env: _Env,
) -> None:
    """The fix must not disable the branch: a genuinely never-dispatched run
    (``claim_count = 0``, no heartbeat, no dispatcher) is still failed."""
    run_id = await _seed_run(
        db_engine,
        far1623_env,
        status="pending",
        claim_count=0,
        run_number=1,
        heartbeat_at=None,
        dispatched_at=None,
        dispatcher=None,
        error_code=None,
    )

    never, capacity, lost = await _sweep_never_dispatched(db_engine, far1623_env.org_id)

    assert never == 1
    assert capacity == 0
    assert lost == 0
    after_sweep = await _fetch_run(db_engine, run_id)
    assert after_sweep["status"] == "failed"
    assert after_sweep["error_code"] == "never_dispatched"


async def test_sweep_is_org_scoped(
    db_engine: AsyncEngine,
    far1623_env: _Env,
) -> None:
    """The never-dispatched branch never touches another org's runs.

    The sweep runs on the superuser engine (RLS bypassed), so a dropped
    ``organisation_id`` predicate would not be caught by RLS. Seed a SECOND
    org with its own equally-aged never-dispatched ``pending`` run, sweep only
    the first org, and assert the second org's run is untouched.
    """
    org_a = far1623_env
    org_b = await _seed_env(db_engine)
    try:
        run_a = await _seed_run(
            db_engine,
            org_a,
            status="pending",
            claim_count=0,
            run_number=1,
            heartbeat_at=None,
            dispatched_at=None,
            dispatcher=None,
            error_code=None,
        )
        run_b = await _seed_run(
            db_engine,
            org_b,
            status="pending",
            claim_count=0,
            run_number=1,
            heartbeat_at=None,
            dispatched_at=None,
            dispatcher=None,
            error_code=None,
        )

        never, capacity, lost = await _sweep_never_dispatched(db_engine, org_a.org_id)

        # Only org A's run was swept.
        assert never == 1
        assert capacity == 0
        assert lost == 0
        after_a = await _fetch_run(db_engine, run_a)
        assert after_a["status"] == "failed"
        assert after_a["error_code"] == "never_dispatched"
        # Org B's run is untouched — still pending, no error code.
        after_b = await _fetch_run(db_engine, run_b)
        assert after_b["status"] == "pending"
        assert after_b["error_code"] is None
    finally:
        await _teardown_env(db_engine, org_b)
