"""FAR-1862: the ``pending`` reclaim predicate must be TOTAL — and SAFE.

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

Coverage is only HALF the contract. The first cut of the age backstop anchored
its bound on ``created_at`` and matched every aged ``pending`` row with a set
``dispatched_at`` — but ``dispatch_run`` stamps ``dispatched_at`` at ENQUEUE time
while the status stays ``pending`` until a worker's claim, so that is the NORMAL
queued state, and SAQ gives a queued job no TTL. A backstop that collected it
would mass-fail runs that are still going to execute (silently losing the work
when their job later no-ops on a ``failed`` row). The backstop is therefore the
EXACT complement of the foreign-dispatcher reclaim leg: it collects the foreign
shape and NOTHING else.

These tests run against a REAL Postgres and drive the REAL code:

* the composed reclaim predicate the 60s reconcile selects with
  (``reconciler_recovery_predicate`` AND the capacity-marker exclusion);
* the real age backstops (``_sweep_org_stale_runs``'s never-dispatched /
  capacity-timeout branches and ``_terminalize_stranded_pending_runs``).

They ENUMERATE the ``pending`` cross-product of
``(dispatched_at, dispatcher, enqueue_failed_at, error_code, heartbeat)`` and
assert every combination is either reclaimed by a leg, terminalised by a
backstop, or deliberately owned by the enqueue-failed handler — a test covering
only the shape that was fixed would pass while a sibling gap stayed open. They
additionally assert the backstop LEAVES ALONE every still-executable shape, so
the over-reach is caught, not just the gap.
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


async def _pending_run_ids(db_engine: AsyncEngine, org_id: uuid.UUID) -> set[uuid.UUID]:
    """The ids of the org's rows still ``pending`` after a backstop pass."""
    async with db_engine.connect() as conn:
        result = await conn.execute(
            text("SELECT id FROM runs WHERE organisation_id = :oid AND status = 'pending'"),
            {"oid": str(org_id)},
        )
        return {row[0] for row in result.all()}


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

    # A ``dispatcher='saq'`` row with a SET ``dispatched_at`` is the NORMAL
    # enqueued state (``dispatch_run`` stamps ``dispatched_at`` at ENQUEUE time;
    # the status stays ``pending`` until a claim). The FAR-1649 re-enqueue leg
    # SELECTS it, and the row loop's per-row ``q.job()`` gate protects it while
    # its SAQ job is live — a job that may still be sitting in an unbounded
    # queue and execute later. The age backstop must therefore NOT terminalise
    # it (asserted below).
    saq_queued_id = next(
        run_id for run_id, combo in seeded.items() if combo == (dispatched_old, "saq", None, None, stale)
    )
    assert saq_queued_id in reclaimed, "the scan must still select the normal queued saq shape"

    # A ``dispatcher IS NULL`` row carrying ``enqueue_failed_at`` is owned by
    # ``_reconcile_enqueue_failed``, which deliberately keeps the row ``pending``
    # while Redis is unverifiable (``dispatch_failed`` fires only after a Redis
    # re-check succeeds). The backstop must never override that fail-safe.
    marker_owned = {
        run_id
        for run_id, combo in seeded.items()
        # combo = (dispatched_at, dispatcher, enqueue_failed_at, error_code, heartbeat)
        if combo[1] is None and combo[2] is not None
    }

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

    # Totality: every seeded pending shape is either reclaimed by a reclaim leg,
    # terminalised by a sweep/backstop, or owned by the enqueue-failed handler
    # (which keeps its row ``pending`` BY DESIGN while Redis is unverifiable —
    # those marker-owned shapes are exactly the ones the backstop must not
    # collect, so they are the one deliberate exception to "reclaimed-or-
    # terminalised").
    uncovered = [
        seeded[run_id]
        for run_id in seeded
        if run_id not in reclaimed and run_id not in backstopped and run_id not in marker_owned
    ]
    assert not uncovered, f"pending shapes left with no reclaim leg and no age backstop: {uncovered}"

    # The SAFETY half: the stranded-pending backstop is the EXACT complement of
    # the foreign-dispatcher reclaim leg. It must not terminalise the normal
    # queued ``dispatcher='saq'`` shape nor any marker-owned shape — either
    # would silently lose still-executable work.
    assert saq_queued_id not in set(stranded), (
        "the stranded-pending backstop must NOT terminalise the normal queued saq shape"
    )
    assert not (set(stranded) & marker_owned), (
        "the stranded-pending backstop must NOT terminalise enqueue-failed (marker-owned) runs"
    )


async def test_backstop_leaves_executable_and_marker_runs_alone(
    db_engine: AsyncEngine,
    far1862_env: _Env,
) -> None:
    """The age backstop must never collect a run that is still executable.

    Regression guard for the FAR-1862 over-reach. The first cut anchored the age
    on ``created_at`` and matched every aged ``pending`` row with a set
    ``dispatched_at``, so it terminal-failed runs that were merely QUEUED
    (``dispatch_run`` stamps ``dispatched_at`` at enqueue time and SAQ gives a
    queued job no TTL), and it overrode the enqueue-failed fail-safe. This test
    pins the three still-executable shapes the backstop must leave pending, plus
    the foreign shape it must still collect as a positive control.
    """
    now = datetime.now(UTC)
    fresh = now - timedelta(seconds=5)
    old = now - timedelta(minutes=_OLD_MINUTES)

    # Created 180 min ago but dispatched only seconds ago: the exact shape the
    # ``created_at``-anchored backstop killed on its first tick, which must
    # survive under the ``dispatched_at`` anchor.
    fresh_saq = await _seed_pending_row(
        db_engine,
        far1862_env,
        run_number=1,
        dispatched_at=fresh,
        dispatcher="saq",
        enqueue_failed_at=None,
        error_code=None,
        heartbeat_at=fresh,
    )
    # An aged, still-queued ``dispatcher='saq'`` run: a live SAQ job may still be
    # waiting in an unbounded queue, so it is NOT the backstop's to collect.
    aged_saq = await _seed_pending_row(
        db_engine,
        far1862_env,
        run_number=2,
        dispatched_at=old,
        dispatcher="saq",
        enqueue_failed_at=None,
        error_code=None,
        heartbeat_at=old,
    )
    # An aged enqueue-failed row: owned by ``_reconcile_enqueue_failed``, which
    # keeps it pending while Redis is unverifiable.
    aged_marker = await _seed_pending_row(
        db_engine,
        far1862_env,
        run_number=3,
        dispatched_at=old,
        dispatcher=None,
        enqueue_failed_at=old,
        error_code=None,
        heartbeat_at=fresh,
    )
    # Positive control: an aged FOREIGN-dispatcher run — the backstop's one
    # legitimate target — must still be terminalised.
    aged_foreign = await _seed_pending_row(
        db_engine,
        far1862_env,
        run_number=4,
        dispatched_at=old,
        dispatcher="legacy",
        enqueue_failed_at=None,
        error_code=None,
        heartbeat_at=old,
    )

    async with db_engine.connect() as conn, conn.begin():
        stranded = await _terminalize_stranded_pending_runs(
            conn,  # type: ignore[arg-type]
            far1862_env.org_id,
            max_age_minutes=_BACKSTOP_AGE_MINUTES,
            max_rows=_RUNS_PER_ORG,
        )
    stranded_set = set(stranded)

    assert aged_foreign in stranded_set, "the backstop must still terminalise the aged foreign-dispatcher run"
    assert fresh_saq not in stranded_set, "the backstop must not terminalise a freshly-dispatched queued run"
    assert aged_saq not in stranded_set, "the backstop must not terminalise an aged but still-queued saq run"
    assert aged_marker not in stranded_set, (
        "the backstop must not terminalise an enqueue-failed (Redis-unverifiable) run"
    )

    remaining = await _pending_run_ids(db_engine, far1862_env.org_id)
    assert fresh_saq in remaining
    assert aged_saq in remaining
    assert aged_marker in remaining
    assert aged_foreign not in remaining
