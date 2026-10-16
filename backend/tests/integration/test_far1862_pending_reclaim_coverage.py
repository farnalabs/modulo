"""FAR-1862: the ``pending`` reclaim predicate must be TOTAL.

Observed prod incident (2026-10-16): runs sat ``pending`` with ``error_code=None``
and a heartbeat frozen for 22h while a sibling pending run was retried every
~60s. The reconciler's decision inputs were not exposed by any read surface, so
the runs could not be diagnosed (Deliverable A adds them).

Why the rows were stranded (Deliverable B): the composed recovery predicate is
an OR of branches, and a ``pending`` row with a SET ``dispatched_at`` whose
``dispatcher`` is neither NULL nor ``'saq'`` matched NO branch at all — the two
dispatcher-NULL branches require NULL, the FAR-1649 re-enqueue branch requires
``'saq'``, and the capacity-deferred branch requires a NULL ``dispatched_at``.
No age backstop owned it either (the never-dispatched sweep needs a NULL
``dispatched_at``; the capacity sweep needs a capacity marker).

These tests run against a REAL Postgres and drive the REAL code:

* the composed reclaim predicate the 60s reconcile selects with
  (``reconciler_recovery_predicate`` AND the capacity-marker exclusion);
* the real age backstops (``_sweep_org_stale_runs``'s never-dispatched /
  capacity-timeout branches and the new ``_terminalize_stranded_pending_runs``).

They ENUMERATE the ``pending`` cross-product of
``(dispatched_at, dispatcher, enqueue_failed_at, error_code, heartbeat)`` and
assert every combination is either reclaimed by a leg or terminalised by a
backstop — a test covering only the shape that was fixed would pass while a
sibling gap stayed open. The precise gap shape is additionally asserted to be
reclaimed by the new foreign-dispatcher leg, so adding the backstop alone (which
would terminalise it eventually) does not satisfy the test.
"""

from __future__ import annotations

import itertools
import uuid
from collections.abc import AsyncGenerator
from datetime import UTC, datetime, timedelta
from typing import NamedTuple

import pytest
import pytest_asyncio
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncEngine

from modulo.core.cron_helpers import (
    _reconcile_capacity_marker_exclusion,
    _terminalize_stranded_pending_runs,
    reconciler_recovery_predicate,
)
from modulo.core.pipeline_execution import _sweep_org_stale_runs
from modulo.db.models.run import Run

pytestmark = pytest.mark.integration

# Rows are seeded three hours old: comfortably past every window used below
# (never-dispatched 300s, capacity TTL 120 min, stranded-pending age bound
# 135 min, heartbeat/dispatch staleness 120s).
_OLD_MINUTES = 180
_RUNS_PER_ORG = 1000

_ND_WINDOW = 300
_WL_WINDOW = 600
_STALE_WINDOW = 120
_CAPACITY_REDISPATCH_SECONDS = 120
_REENQUEUE_WINDOW = 120
_BACKSTOP_AGE_MINUTES = 135

# The reconciler's scan selects these non-terminal statuses. This test seeds
# only ``pending`` (the status the FAR-1862 gap is in); the added leg and
# backstop are both ``pending``-scoped.
_SCAN_STATUSES = ("pending", "running", "awaiting_human", "claimed", "hitl_parked")


class _Env(NamedTuple):
    org_id: uuid.UUID
    pipeline_id: uuid.UUID
    snapshot_id: uuid.UUID
    account_id: uuid.UUID


async def _seed_env(db_engine: AsyncEngine) -> _Env:
    org_id, account_id, pipeline_id, snapshot_id = uuid.uuid4(), uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    async with db_engine.begin() as conn:
        await conn.execute(
            text(
                "INSERT INTO organisations (id, name, slug, settings_json) VALUES (:id, :name, :slug, '{}'::json)",
            ),
            {"id": str(org_id), "name": "FAR-1862 Org", "slug": f"far1862-{org_id.hex[:8]}"},
        )
        await conn.execute(
            text(
                "INSERT INTO accounts (id, email, display_name, password_hash, auth_provider, active) "
                "VALUES (:id, :email, :name, 'hash', 'local', true)",
            ),
            {"id": str(account_id), "email": f"far1862-{account_id.hex[:8]}@test.local", "name": "FAR-1862 User"},
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
                "name": f"far1862-{pipeline_id.hex[:8]}",
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


async def _seed_pending_row(
    db_engine: AsyncEngine,
    env: _Env,
    *,
    run_number: int,
    dispatched_at: datetime | None,
    dispatcher: str | None,
    enqueue_failed_at: datetime | None,
    error_code: str | None,
    heartbeat_at: datetime | None,
) -> uuid.UUID:
    """Insert one ``pending`` run with the exact reconciler-input shape."""
    run_id = uuid.uuid4()
    created_at = datetime.now(UTC) - timedelta(minutes=_OLD_MINUTES)
    async with db_engine.begin() as conn:
        await conn.execute(
            text(
                "INSERT INTO runs (id, organisation_id, pipeline_id, snapshot_id, account_id, "
                "trigger_type, status, input_hash, langgraph_thread_id, run_number, claim_count, "
                "dispatcher, dispatched_at, enqueue_failed_at, error_code, heartbeat_at, created_at, started_at) "
                "VALUES (:id, :oid, :pid, :sid, :uid, 'manual', 'pending', 'hash', :thread, :rn, "
                "0, :dispatcher, :dispatched_at, :enqueue_failed_at, :error_code, :heartbeat_at, :created_at, NULL)",
            ),
            {
                "id": str(run_id),
                "oid": str(env.org_id),
                "pid": str(env.pipeline_id),
                "sid": str(env.snapshot_id),
                "uid": str(env.account_id),
                "thread": f"{env.org_id}:{run_id}",
                "rn": run_number,
                "dispatcher": dispatcher,
                "dispatched_at": dispatched_at,
                "enqueue_failed_at": enqueue_failed_at,
                "error_code": error_code,
                "heartbeat_at": heartbeat_at,
                "created_at": created_at,
            },
        )
    return run_id


async def _reclaimed_by_scan(db_engine: AsyncEngine, org_id: uuid.UUID) -> set[uuid.UUID]:
    """Run the EXACT scan WHERE the 60s reconcile selects with."""
    recon = reconciler_recovery_predicate(
        reenqueue_window=_REENQUEUE_WINDOW,
        stale_window=_STALE_WINDOW,
        capacity_redispatch_seconds=_CAPACITY_REDISPATCH_SECONDS,
        nodeless_window=35,
        early_detect_minutes=None,
    )
    exclusion = _reconcile_capacity_marker_exclusion(_CAPACITY_REDISPATCH_SECONDS)
    stmt = select(Run.id).where(
        Run.organisation_id == org_id,
        Run.status.in_(_SCAN_STATUSES),
        recon,
        exclusion,
    )
    async with db_engine.connect() as conn:
        return {row[0] for row in (await conn.execute(stmt)).all()}


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
async def far1862_env(db_engine: AsyncEngine) -> AsyncGenerator[_Env, None]:
    env = await _seed_env(db_engine)
    try:
        yield env
    finally:
        await _teardown_env(db_engine, env)


async def test_every_pending_shape_is_reclaimed_or_backstopped(
    db_engine: AsyncEngine,
    far1862_env: _Env,
) -> None:
    now = datetime.now(UTC)
    stale = now - timedelta(minutes=_OLD_MINUTES)
    fresh = now - timedelta(seconds=5)
    dispatched_old = now - timedelta(minutes=_OLD_MINUTES)

    # (dispatched_at, dispatcher, enqueue_failed_at, error_code, heartbeat)
    axes: dict[str, list[object]] = {
        "dispatched_at": [None, dispatched_old],
        "dispatcher": [None, "saq", "legacy"],
        "enqueue_failed_at": [None, stale],
        "error_code": [None, "org_capacity_limited", "pipeline_capacity", "heartbeat_stale", "other"],
        "heartbeat_at": [None, stale, fresh],
    }
    combos = list(itertools.product(*axes.values()))
    keys = list(axes.keys())
    assert len(combos) == 180
    assert len(combos) < _RUNS_PER_ORG

    seeded: dict[uuid.UUID, tuple[object, ...]] = {}
    for run_number, combo in enumerate(combos, start=1):
        values = dict(zip(keys, combo, strict=True))
        run_id = await _seed_pending_row(db_engine, far1862_env, run_number=run_number, **values)  # type: ignore[arg-type]
        seeded[run_id] = combo

    reclaimed = await _reclaimed_by_scan(db_engine, far1862_env.org_id)

    # The FAR-1862 gap shape: dispatched + a foreign dispatcher. It MUST be
    # reclaimed by a leg (not merely terminalisable) — that is the fix.
    gap = (dispatched_old, "legacy", None, None, stale)
    gap_id = next(run_id for run_id, combo in seeded.items() if combo == gap)
    assert gap_id in reclaimed, "the foreign-dispatcher pending shape must be reclaimed by the new leg"

    # The FAR-1649 stale-job shape: ``pending`` + ``dispatcher='saq'`` + an old
    # ``dispatched_at``. The FAR-1649 leg SELECTS it, but the row loop's per-row
    # ``q.job()`` gate skips it every tick while its SAQ job exists — so the
    # stranded-pending age backstop is the thing that bounds it. (Asserted below
    # by running the real backstop.)
    saq_stale_job_id = next(
        run_id for run_id, combo in seeded.items() if combo == (dispatched_old, "saq", None, None, stale)
    )

    # Now run the real backstops over the remaining rows.
    backstopped: set[uuid.UUID] = set()
    async with db_engine.connect() as conn, conn.begin():
        sweep_ids: list[tuple[uuid.UUID, uuid.UUID]] = []
        await _sweep_org_stale_runs(
            conn,
            org_id=far1862_env.org_id,
            nd_window=_ND_WINDOW,
            wl_window=_WL_WINDOW,
            stranded_rows=[],
            terminalised_run_ids=sweep_ids,
        )
    backstopped.update(run_id for run_id, _ in sweep_ids)
    async with db_engine.connect() as conn, conn.begin():
        stranded = await _terminalize_stranded_pending_runs(
            conn,  # type: ignore[arg-type]
            far1862_env.org_id,
            max_age_minutes=_BACKSTOP_AGE_MINUTES,
            max_rows=_RUNS_PER_ORG,
        )
    backstopped.update(stranded)

    uncovered = [seeded[run_id] for run_id in seeded if run_id not in reclaimed and run_id not in backstopped]
    assert not uncovered, f"pending shapes left with no reclaim leg and no age backstop: {uncovered}"

    # The backstop terminalised the FAR-1649 stale-job shape (the sweep cannot:
    # never-dispatched needs a NULL dispatched_at, capacity-timeout needs a
    # capacity marker, worker-lost needs ``running``).
    assert saq_stale_job_id in set(stranded), (
        "the stranded-pending backstop must terminalise the FAR-1649 stale-job shape"
    )
