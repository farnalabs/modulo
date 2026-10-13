"""FAR-1715: the sweep's SAQ zero-progress branch and the reconcile age-bound
path must never double-terminalise the same run, and the sweep is org-scoped.

The new ``stale_run_recovery_sweep`` zero-progress branch
(``_sweep_org_saq_zero_progress_runs``) and dispatcher_reconcile's nodeless
router (``_terminalize_aged_nodeless_zombies`` -> ``_fail_nodeless_run``)
select the SAME rows: ``running`` + ``dispatcher='saq'`` + ZERO node progress +
``started_at`` past the shared age bound. The ``status='running'`` guard makes
double-firing a no-op — whichever path reaches the row first terminalises it
and the other then matches nothing. These tests prove that against a REAL
Postgres, driving the REAL per-org helpers both production paths execute
(see also FAR-1623's twin for the never-dispatched branch): a mocked or
SQLite double cannot catch a predicate collision in real SQL against the real
``runs`` row shape.

Also proves the pre-PR QA M2 contract: when the sweep wins the race (the
degraded-fleet scenario it exists to cover) it emits the FAR-714 class signal
itself — the SHARED stable error message via ``_ingest_saq_error`` — so the
Error Dashboard group fed by the reconcile chokepoint is still fed.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncGenerator
from datetime import UTC, datetime, timedelta
from typing import NamedTuple
from unittest.mock import AsyncMock, patch

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker

from modulo.core import cron_helpers as ch
from modulo.core.cron_helpers import _terminalize_aged_nodeless_zombies
from modulo.core.pipeline_execution import _sweep_org_saq_zero_progress_runs

pytestmark = pytest.mark.integration

# Both the sweep helper and the reconcile router are driven with the SHARED
# default age bound (the pre-FAR-1715 derivation, unchanged at the default
# settings). Seeded runs are 3h old, comfortably past it.
_AGE_BOUND_MINUTES = 135
_OLD = timedelta(hours=3)


class _Env(NamedTuple):
    """A dedicated org + pipeline + snapshot + account for one test."""

    org_id: uuid.UUID
    pipeline_id: uuid.UUID
    snapshot_id: uuid.UUID
    account_id: uuid.UUID


async def _seed_env(db_engine: AsyncEngine, label: str) -> _Env:
    org_id = uuid.uuid4()
    account_id = uuid.uuid4()
    pipeline_id = uuid.uuid4()
    snapshot_id = uuid.uuid4()
    async with db_engine.begin() as conn:
        await conn.execute(
            text(
                "INSERT INTO organisations (id, name, slug, settings_json) VALUES (:id, :name, :slug, '{}'::json)",
            ),
            {"id": str(org_id), "name": f"FAR-1715 {label} Org", "slug": f"far1715-{label}-{org_id.hex[:8]}"},
        )
        await conn.execute(
            text(
                "INSERT INTO accounts (id, email, display_name, password_hash, auth_provider, active) "
                "VALUES (:id, :email, :name, 'hash', 'local', true)",
            ),
            {
                "id": str(account_id),
                "email": f"far1715-{label}-{account_id.hex[:8]}@test.local",
                "name": f"FAR-1715 {label} User",
            },
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
                "name": f"far1715-{label}-{pipeline_id.hex[:8]}",
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


async def _seed_zero_progress_run(db_engine: AsyncEngine, env: _Env) -> uuid.UUID:
    """A zero-progress ``dispatcher='saq'`` run aged past the age bound.

    ``node_attempt_count`` defaults to 0, no ``run_node_outputs`` /
    checkpoints / token usage exist, ``dispatch_phase`` stays NULL — the exact
    shape both terminalisers select.
    """
    run_id = uuid.uuid4()
    started = datetime.now(UTC) - _OLD
    async with db_engine.begin() as conn:
        await conn.execute(
            text(
                "INSERT INTO runs (id, organisation_id, pipeline_id, snapshot_id, account_id, "
                "trigger_type, status, input_hash, langgraph_thread_id, run_number, claim_count, "
                "dispatcher, heartbeat_at, dispatched_at, created_at, started_at) "
                "VALUES (:id, :oid, :pid, :sid, :uid, 'manual', 'running', 'hash', :thread, 1, 1, "
                "'saq', :heartbeat, :dispatched, :started, :started)",
            ),
            {
                "id": str(run_id),
                "oid": str(env.org_id),
                "pid": str(env.pipeline_id),
                "sid": str(env.snapshot_id),
                "uid": str(env.account_id),
                "thread": f"{env.org_id}:{run_id}",
                # asyncpg binds timestamptz params as datetime objects, never
                # ISO strings (the FAR-1623 twin passes datetimes the same way).
                "heartbeat": started,
                "dispatched": started,
                "started": started,
            },
        )
    return run_id


async def _sweep_org(db_engine: AsyncEngine, org_id: uuid.UUID) -> int:
    """Drive the REAL sweep helper the per-org loop calls."""
    async with db_engine.connect() as conn, conn.begin():
        return await _sweep_org_saq_zero_progress_runs(
            conn,
            org_id=org_id,
            max_age_minutes=_AGE_BOUND_MINUTES,
            max_rows=25,
            terminalised_run_ids=[],
        )


async def _reconcile_router(db_engine: AsyncEngine, org_id: uuid.UUID) -> tuple[list[uuid.UUID], dict[str, object]]:
    """Drive the REAL reconcile nodeless router (the age-bound zero-progress
    path) with its own summary, like ``_reconcile_org`` does."""
    factory = async_sessionmaker(db_engine, expire_on_commit=False)
    summary = ch._dispatcher_summary()
    async with factory() as session, session.begin():
        routed = await _terminalize_aged_nodeless_zombies(
            session,
            org_id,
            max_age_minutes=_AGE_BOUND_MINUTES,
            summary=summary,
            max_rows=None,
        )
    return routed, summary


async def _fetch_run(db_engine: AsyncEngine, run_id: uuid.UUID) -> dict[str, object]:
    async with db_engine.connect() as conn:
        result = await conn.execute(
            text("SELECT status, error_code, error_detail, completed_at FROM runs WHERE id = :id"),
            {"id": str(run_id)},
        )
        row = result.one()
    return {
        "status": row.status,
        "error_code": row.error_code,
        "error_detail": row.error_detail,
        "completed_at": row.completed_at,
    }


async def _teardown_env(db_engine: AsyncEngine, env: _Env) -> None:
    async with db_engine.begin() as conn:
        await conn.execute(text("DELETE FROM error_events WHERE organisation_id = :oid"), {"oid": str(env.org_id)})
        await conn.execute(text("DELETE FROM error_groups WHERE organisation_id = :oid"), {"oid": str(env.org_id)})
        await conn.execute(text("DELETE FROM runs WHERE organisation_id = :oid"), {"oid": str(env.org_id)})
        await conn.execute(
            text("DELETE FROM pipeline_snapshots WHERE organisation_id = :oid"),
            {"oid": str(env.org_id)},
        )
        await conn.execute(text("DELETE FROM pipelines WHERE organisation_id = :oid"), {"oid": str(env.org_id)})
        await conn.execute(text("DELETE FROM accounts WHERE id = :id"), {"id": str(env.account_id)})
        await conn.execute(text("DELETE FROM organisations WHERE id = :oid"), {"oid": str(env.org_id)})


@pytest_asyncio.fixture
async def far1715_env(db_engine: AsyncEngine) -> AsyncGenerator[_Env, None]:
    """A dedicated org seeded fresh per test, torn down afterwards.

    A dedicated org keeps the terminaliser assertions isolated: both helpers
    are called for THIS org only, so a sibling integration test's runs can
    never change the counts.
    """
    env = await _seed_env(db_engine, "a")
    try:
        yield env
    finally:
        await _teardown_env(db_engine, env)


async def test_sweep_then_reconcile_does_not_double_terminalise(
    db_engine: AsyncEngine,
    far1715_env: _Env,
) -> None:
    """The sweep terminalises first; the reconcile path then matches nothing.

    The run keeps the sweep's own error_detail (naming the sweep as writer)
    and its first ``completed_at`` — a second terminalisation would overwrite
    both.
    """
    run_id = await _seed_zero_progress_run(db_engine, far1715_env)

    count = await _sweep_org(db_engine, far1715_env.org_id)
    assert count == 1
    after_sweep = await _fetch_run(db_engine, run_id)
    assert after_sweep["status"] == "failed"
    assert after_sweep["error_code"] == "executor_stalled"
    assert after_sweep["completed_at"] is not None
    assert after_sweep["error_detail"] is not None
    assert "stale_run_recovery zero-progress sweep" in str(after_sweep["error_detail"])

    routed, summary = await _reconcile_router(db_engine, far1715_env.org_id)

    assert routed == []
    assert summary["claimed_but_never_dispatched"] == 0
    after_reconcile = await _fetch_run(db_engine, run_id)
    assert after_reconcile["status"] == "failed"
    assert after_reconcile["error_code"] == "executor_stalled"
    # The sweep's writer attribution survives — the chokepoint never re-wrote
    # the row.
    assert after_reconcile["error_detail"] == after_sweep["error_detail"]
    assert after_reconcile["completed_at"] == after_sweep["completed_at"]


async def test_reconcile_then_sweep_does_not_double_terminalise(
    db_engine: AsyncEngine,
    far1715_env: _Env,
) -> None:
    """The reconcile chokepoint terminalises first (the routine order — it
    runs every 60s); the 5-min sweep then matches nothing and never overwrites
    the chokepoint's writer attribution."""
    run_id = await _seed_zero_progress_run(db_engine, far1715_env)

    routed, summary = await _reconcile_router(db_engine, far1715_env.org_id)
    assert routed == [run_id]
    assert summary["claimed_but_never_dispatched"] == 1
    after_reconcile = await _fetch_run(db_engine, run_id)
    assert after_reconcile["status"] == "failed"
    assert after_reconcile["error_code"] == "executor_stalled"
    assert after_reconcile["error_detail"] is not None
    assert "dispatcher_reconcile zombie repair" in str(after_reconcile["error_detail"])
    assert after_reconcile["completed_at"] is not None

    count = await _sweep_org(db_engine, far1715_env.org_id)

    assert count == 0
    after_sweep = await _fetch_run(db_engine, run_id)
    assert after_sweep["status"] == "failed"
    assert after_sweep["error_code"] == "executor_stalled"
    assert after_sweep["error_detail"] == after_reconcile["error_detail"]
    assert after_sweep["completed_at"] == after_reconcile["completed_at"]


async def test_sweep_is_org_scoped(
    db_engine: AsyncEngine,
    far1715_env: _Env,
) -> None:
    """The sweep never touches another org's runs.

    The sweep runs on the superuser engine (RLS bypassed), so a dropped
    ``organisation_id`` predicate would not be caught by RLS. Seed a SECOND
    org with its own equally-aged zero-progress SAQ run, sweep only the first
    org, and assert the second org's run is untouched.
    """
    org_a = far1715_env
    org_b = await _seed_env(db_engine, "b")
    try:
        run_a = await _seed_zero_progress_run(db_engine, org_a)
        run_b = await _seed_zero_progress_run(db_engine, org_b)

        count = await _sweep_org(db_engine, org_a.org_id)

        assert count == 1
        after_a = await _fetch_run(db_engine, run_a)
        assert after_a["status"] == "failed"
        assert after_a["error_code"] == "executor_stalled"
        # Org B's run is untouched — still running, no error code.
        after_b = await _fetch_run(db_engine, run_b)
        assert after_b["status"] == "running"
        assert after_b["error_code"] is None
    finally:
        await _teardown_env(db_engine, org_b)


async def test_sweep_emits_the_far714_class_signal_when_it_wins(
    db_engine: AsyncEngine,
    far1715_env: _Env,
) -> None:
    """Pre-PR QA M2: when the sweep wins the race, the row's terminalisation
    still feeds the FAR-714 class signal — the SHARED stable message via
    ``_ingest_saq_error`` (the same group the reconcile chokepoint feeds).

    Driven through the REAL sweep helper against real Postgres; the ingest
    target is mocked (same convention as the saq recovery integration tests)
    so the assertion is about the REAL sweep emitting, not about
    ErrorIngestionService's internals.
    """
    run_id = await _seed_zero_progress_run(db_engine, far1715_env)
    ingest = AsyncMock()

    with patch.object(ch, "_ingest_saq_error", new=ingest):
        count = await _sweep_org(db_engine, far1715_env.org_id)

    assert count == 1
    assert ingest.await_count == 1
    call = ingest.await_args
    assert call.args[1] == far1715_env.org_id
    assert call.kwargs["function"] == "stale_run_recovery"
    assert call.kwargs["message"] == ch._NODELESS_ZOMBIE_STABLE_MESSAGE
    context = call.kwargs["context"]
    assert context["run_id"] == str(run_id)
