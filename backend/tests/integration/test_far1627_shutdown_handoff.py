"""FAR-1627: worker shutdown hands every in-flight run back for re-dispatch.

Before this ticket, ``fly deploy`` SIGTERMed the single prod SAQ runs worker
while agent runs were mid-execution and NOTHING noticed at shutdown: the run
row stayed ``running`` with its last heartbeat, detection waited out the
heartbeat TTL (``SLOT_RECONCILE_STALE_SECONDS`` = 30 min), and the sweep
force-released the row 30-35 min later with a misleading dispatch-path code
(the FAR-1603 RCA: 10 of 19 releases over 3 days killed in-flight runs).

These tests run against a REAL Postgres and drive the REAL shutdown sequence
at the component level: the runs are registered in the process in-flight
registry exactly as ``execute_run``/``resume_run`` do after a claim AND
abandoned exactly as SAQ's shutdown cancellation abandons them (a real task
running the real ``track_inflight_run`` scope, then cancelled — see
:func:`_register_abandoned`), then a REAL ``saq.Worker`` carrying the
production ``shutdown`` hook (``saq_worker._shutdown_handoff_hook`` — the
object ``_base_worker_settings`` installs) is ``stop()``-ed. That is precisely
what SAQ does after SIGTERM: ``Worker.start`` sets the stop event, ``stop()``
drains/cancels the job tasks, then awaits the ``shutdown`` lifecycle callbacks.

The runs are seeded with a FRESH heartbeat (seconds old): every TTL-based
sweep (slot reconciliation, reconcile stale legs, the legacy worker_lost
branch) requires a heartbeat older than minutes, so those paths structurally
CANNOT release these rows. Only the shutdown handback can — which is what
makes these tests prove PROMPTNESS (seconds, inside the worker's own shutdown)
rather than merely eventual recovery.

The requeue GATE (M1) is proven here end to end as well: a node-started run
whose graph is non-idempotent — or whose pipeline ``retry_policy`` forbids the
re-run — must end up FAILED IN PLACE with the truthful worker-loss code and
must NOT be admitted by the real ``dispatcher_reconcile`` re-dispatch
predicate (that leg re-dispatches as ``execute_run``, a re-EXECUTION with no
idempotency gate of its own). The M3 gate is proven in the other direction: an
entry whose task never observed the cancellation is never touched at all.

A mocked or SQLite double cannot catch this class of defect: the handback is
a fenced UPDATE against the real ``runs`` row shape, and the re-dispatch
admission is the real ``dispatcher_reconcile`` predicate.
"""

from __future__ import annotations

import asyncio
import json
import uuid
from collections.abc import AsyncGenerator, Generator
from datetime import UTC, datetime
from typing import NamedTuple
from unittest.mock import MagicMock, patch

import pytest
import pytest_asyncio
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncEngine

from modulo.core.cron_helpers import _build_re_dispatch_predicate
from modulo.core.pipeline_engine.error_codes import map_legacy_code
from modulo.core.pipeline_execution import claim_run_async
from modulo.core.worker_handoff import (
    WORKER_LOST_DETAIL,
    WORKER_LOST_ERROR_CODE,
    InFlightRunRegistry,
    get_inflight_registry,
    handback_inflight_runs,
    track_inflight_run,
)
from modulo.db.models.run import Run

pytestmark = pytest.mark.integration


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
            {"id": str(org_id), "name": "FAR-1627 Org", "slug": f"far1627-{org_id.hex[:8]}"},
        )
        await conn.execute(
            text(
                "INSERT INTO accounts (id, email, display_name, password_hash, auth_provider, active) "
                "VALUES (:id, :email, :name, 'hash', 'local', true)",
            ),
            {"id": str(account_id), "email": f"far1627-{account_id.hex[:8]}@test.local", "name": "FAR-1627 User"},
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
                "name": f"far1627-{pipeline_id.hex[:8]}",
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


async def _seed_running_run(
    db_engine: AsyncEngine,
    env: _Env,
    *,
    run_number: int,
    claim_token: str,
    dispatch_phase: str | None = None,
    cancellation_requested: bool = False,
    status: str = "running",
) -> uuid.UUID:
    """Seed a claimed, EXECUTING run with a FRESH heartbeat (seconds old).

    The fresh heartbeat is the promptness discriminator: every TTL-based sweep
    needs a heartbeat older than minutes, so these rows are structurally
    untouchable by the 30-minute backstop paths — only the shutdown handback
    can release them.
    """
    run_id = uuid.uuid4()
    now = datetime.now(UTC)
    async with db_engine.begin() as conn:
        await conn.execute(
            text(
                "INSERT INTO runs (id, organisation_id, pipeline_id, snapshot_id, account_id, "
                "trigger_type, status, input_hash, langgraph_thread_id, run_number, claim_count, "
                "claim_token, dispatcher, saq_job_id, heartbeat_at, dispatched_at, started_at, "
                "dispatch_phase, cancellation_requested, created_at) "
                "VALUES (:id, :oid, :pid, :sid, :uid, 'manual', :status, 'hash', :thread, :rn, "
                "1, :tok, 'saq', :jid, :now, :now, :now, :phase, :cancel, :now)"
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
                "tok": claim_token,
                "jid": f"saq:job:runs:run:{run_id}",
                "now": now,
                "phase": dispatch_phase,
                "cancel": cancellation_requested,
            },
        )
    return run_id


async def _fetch_run(db_engine: AsyncEngine, run_id: uuid.UUID) -> dict[str, object]:
    async with db_engine.connect() as conn:
        result = await conn.execute(
            text(
                "SELECT status, error_code, error_detail, dispatched_at, dispatcher, heartbeat_at, "
                "claim_token, completed_at "
                "FROM runs WHERE id = :id"
            ),
            {"id": str(run_id)},
        )
        row = result.one()
    return {
        "status": row.status,
        "error_code": row.error_code,
        "error_detail": row.error_detail,
        "dispatched_at": row.dispatched_at,
        "dispatcher": row.dispatcher,
        "heartbeat_at": row.heartbeat_at,
        "claim_token": row.claim_token,
        "completed_at": row.completed_at,
    }


async def _run_matches_re_dispatch_predicate(db_engine: AsyncEngine, run_id: uuid.UUID) -> bool:
    """True when the REAL ``dispatcher_reconcile`` predicate admits the run."""
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
async def far1627_env(db_engine: AsyncEngine) -> AsyncGenerator[_Env, None]:
    """A dedicated org seeded fresh per test, torn down afterwards."""
    env = await _seed_env(db_engine)
    try:
        yield env
    finally:
        await _teardown_env(db_engine, env)


@pytest.fixture
def clean_registry() -> Generator[InFlightRunRegistry, None, None]:
    """Empty the process in-flight registry before and after each test."""
    reg = get_inflight_registry()
    reg.clear()
    yield reg
    reg.clear()


async def _stop_worker_with_shutdown_hook(db_engine: AsyncEngine) -> None:
    """Run the REAL SAQ shutdown sequence with the production hook.

    ``Worker.stop()`` is what SAQ invokes after SIGTERM (event set -> drain ->
    cancel -> shutdown callbacks). A stub queue is enough: ``stop()`` never
    touches it. The hook's engine is patched to the testcontainer engine so
    the handback writes hit the real database.
    """
    from saq import Worker

    import modulo.core.saq_worker as sw

    worker = Worker(
        queue=MagicMock(),  # stop() never uses the queue
        functions=[],
        shutdown=sw._shutdown_handoff_hook,
    )
    with patch.object(sw, "_get_async_engine", new=lambda: db_engine):
        await worker.stop()


async def _register_abandoned(
    registry: InFlightRunRegistry,
    run_id: uuid.UUID,
    org_id: uuid.UUID,
    claim_token: str,
) -> None:
    """Register a run the way ``execute_run`` does, then abandon it like SAQ.

    Drives the REAL ``track_inflight_run`` contract instead of poking the
    registry: a task enters the claim scope, then is CANCELLED (what
    ``Worker.stop()`` does after the shutdown grace period), so the entry
    survives AND carries the cancellation observation the handback's M3 gate
    requires. A bare ``registry.register()`` would leave the entry
    un-observed — a still-running predecessor the handback must NOT release
    (see ``test_handoff_leaves_unobserved_run_to_the_backstop``).
    """
    entered = asyncio.Event()

    async def _body() -> None:
        async with track_inflight_run(run_id=run_id, org_id=org_id, claim_token=claim_token, registry=registry):
            entered.set()
            await asyncio.Event().wait()  # run until SAQ's shutdown cancellation

    task = asyncio.create_task(_body())
    await entered.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


async def test_shutdown_releases_inflight_runs_promptly_with_truthful_code(
    db_engine: AsyncEngine,
    far1627_env: _Env,
    clean_registry: InFlightRunRegistry,
) -> None:
    """The SIGTERM sequence releases every in-flight run — in seconds, not 30 min.

    Seeds TWO executing runs with FRESH heartbeats (a pre-node run and a
    durably node-started run — the class the stale sweeps refuse to reset),
    registers them as execute_run does, and stops a REAL saq.Worker carrying
    the production shutdown hook. Asserts the reset state + truthful code land
    immediately after stop() returns, that the rows are admitted by the real
    re-dispatch predicate, and that the claim path can re-claim them at once.
    """
    zero_node = await _seed_running_run(
        db_engine, far1627_env, run_number=1, claim_token="tok-zero-node", dispatch_phase="claimed"
    )
    node_started = await _seed_running_run(
        db_engine,
        far1627_env,
        run_number=2,
        claim_token="tok-node-started",
        dispatch_phase="first_node_dispatched",
    )

    registry = clean_registry
    await _register_abandoned(registry, zero_node, far1627_env.org_id, "tok-zero-node")
    await _register_abandoned(registry, node_started, far1627_env.org_id, "tok-node-started")

    await _stop_worker_with_shutdown_hook(db_engine)

    for run_id in (zero_node, node_started):
        after = await _fetch_run(db_engine, run_id)
        # Reset for re-dispatch — NOT terminal-failed, NOT left running.
        assert after["status"] == "pending"
        assert after["error_code"] == WORKER_LOST_ERROR_CODE
        # The truthfulness contract: the surfaced code names worker loss, and
        # is NOT the misleading dispatch-path code the RCA complained about.
        assert map_legacy_code(str(after["error_code"])) == "harness.worker_lost"
        assert map_legacy_code(str(after["error_code"])) != "harness.dispatch_failed"
        assert after["error_detail"] == WORKER_LOST_DETAIL
        # Claim released: the dispatch fields are cleared so the reconcile
        # pending legs admit the row.
        assert after["dispatched_at"] is None
        assert after["dispatcher"] is None
        assert after["heartbeat_at"] is None
        assert after["completed_at"] is None
        # Admitted by the REAL dispatcher_reconcile re-dispatch predicate.
        assert await _run_matches_re_dispatch_predicate(db_engine, run_id)
        # And immediately re-claimable — the run can be picked back up now,
        # not after a TTL.
        reclaimed = await claim_run_async(db_engine, str(run_id), str(far1627_env.org_id))
        assert reclaimed is not None

    # The registry is drained: a second shutdown handback is a no-op.
    assert len(registry) == 0
    second = await handback_inflight_runs(db_engine)
    assert second == {"handed_back": 0, "terminalised": 0, "skipped": 0}


async def test_handoff_fences_rotated_claim_terminal_and_cancelled_runs(
    db_engine: AsyncEngine,
    far1627_env: _Env,
    clean_registry: InFlightRunRegistry,
) -> None:
    """The handback never clobbers a run it no longer owns.

    Three fenced-out classes, each registered then protected by the UPDATE's
    guards: a successor rotated the claim token, the run's fate already landed
    (complete), and a cancellation is in flight (cancel wins).
    """
    rotated = await _seed_running_run(
        db_engine, far1627_env, run_number=1, claim_token="tok-old", dispatch_phase="claimed"
    )
    completed = await _seed_running_run(
        db_engine, far1627_env, run_number=2, claim_token="tok-complete", dispatch_phase="claimed"
    )
    cancelling = await _seed_running_run(
        db_engine,
        far1627_env,
        run_number=3,
        claim_token="tok-cancel",
        dispatch_phase="claimed",
        cancellation_requested=True,
    )
    # A successor re-claimed the run: the DB token no longer matches ours.
    async with db_engine.begin() as conn:
        await conn.execute(
            text("UPDATE runs SET claim_token = 'tok-rotated' WHERE id = :id"),
            {"id": str(rotated)},
        )
        await conn.execute(
            text("UPDATE runs SET status = 'complete', completed_at = now() WHERE id = :id"),
            {"id": str(completed)},
        )

    registry = clean_registry
    # Registered with the PRE-rotation tokens — the fence must hold.
    await _register_abandoned(registry, rotated, far1627_env.org_id, "tok-old")
    await _register_abandoned(registry, completed, far1627_env.org_id, "tok-complete")
    await _register_abandoned(registry, cancelling, far1627_env.org_id, "tok-cancel")

    await _stop_worker_with_shutdown_hook(db_engine)

    after_rotated = await _fetch_run(db_engine, rotated)
    assert after_rotated["status"] == "running"
    assert after_rotated["error_code"] is None
    assert after_rotated["claim_token"] == "tok-rotated"

    after_completed = await _fetch_run(db_engine, completed)
    assert after_completed["status"] == "complete"
    assert after_completed["error_code"] is None

    after_cancelling = await _fetch_run(db_engine, cancelling)
    assert after_cancelling["status"] == "running"
    assert after_cancelling["error_code"] is None


async def test_handoff_is_org_scoped(
    db_engine: AsyncEngine,
    far1627_env: _Env,
    clean_registry: InFlightRunRegistry,
) -> None:
    """The handback only touches runs registered by THIS worker process.

    A second org's equally-executing run (never registered) must survive the
    shutdown of the first org's worker untouched — the handback iterates the
    process registry, never an org-wide scan.
    """
    org_b = await _seed_env(db_engine)
    try:
        run_a = await _seed_running_run(
            db_engine, far1627_env, run_number=1, claim_token="tok-a", dispatch_phase="claimed"
        )
        run_b = await _seed_running_run(db_engine, org_b, run_number=1, claim_token="tok-b", dispatch_phase="claimed")

        registry = clean_registry
        await _register_abandoned(registry, run_a, far1627_env.org_id, "tok-a")

        await _stop_worker_with_shutdown_hook(db_engine)

        after_a = await _fetch_run(db_engine, run_a)
        assert after_a["status"] == "pending"
        assert after_a["error_code"] == WORKER_LOST_ERROR_CODE

        after_b = await _fetch_run(db_engine, run_b)
        assert after_b["status"] == "running"
        assert after_b["error_code"] is None
        assert after_b["heartbeat_at"] is not None
    finally:
        await _teardown_env(db_engine, org_b)


async def test_non_idempotent_node_started_run_is_failed_in_place_not_requeued(
    db_engine: AsyncEngine,
    far1627_env: _Env,
    clean_registry: InFlightRunRegistry,
) -> None:
    """M1: a non-idempotent NODE-STARTED run must never reach the re-dispatch leg.

    The handback's reset feeds ``dispatcher_reconcile``'s capacity-deferred leg
    (``pending`` + ``dispatched_at IS NULL``), which re-dispatches as
    ``execute_run`` — a RE-EXECUTION with no idempotency gate of its own (the
    ``executor._fenced_pending_reset`` contract: a new producer MUST keep the
    ``_graph_is_idempotent`` gate). So the gate lives in the handback: this run
    is terminal-failed in place with the truthful worker-loss code and is NOT
    admitted by the real re-dispatch predicate.
    """
    run_id = await _seed_running_run(
        db_engine,
        far1627_env,
        run_number=1,
        claim_token="tok-non-idempotent",
        dispatch_phase="first_node_dispatched",
    )
    async with db_engine.begin() as conn:
        await conn.execute(
            text("UPDATE pipeline_snapshots SET graph_json = CAST(:g AS json) WHERE id = :sid"),
            {
                "g": json.dumps({"nodes": [{"id": "n1", "node_type": "sandbox_agent", "idempotent": False}]}),
                "sid": str(far1627_env.snapshot_id),
            },
        )
    await _register_abandoned(clean_registry, run_id, far1627_env.org_id, "tok-non-idempotent")

    await _stop_worker_with_shutdown_hook(db_engine)

    after = await _fetch_run(db_engine, run_id)
    # Failed IN PLACE with the truthful code — not reset to pending, not left
    # running, not carrying a dispatch-path misattribution.
    assert after["status"] == "failed"
    assert after["error_code"] == WORKER_LOST_ERROR_CODE
    assert map_legacy_code(str(after["error_code"])) == "harness.worker_lost"
    assert after["completed_at"] is not None
    assert "non-idempotent" in str(after["error_detail"])
    # THE M1 assertion: the real re-dispatch predicate must NOT admit it.
    assert not await _run_matches_re_dispatch_predicate(db_engine, run_id)
    # And it cannot simply be re-claimed — the claim path only takes
    # pending/running rows.
    reclaimed = await claim_run_async(db_engine, str(run_id), str(far1627_env.org_id))
    assert reclaimed is None


async def test_handoff_terminalises_node_started_run_when_retry_policy_forbids_requeue(
    db_engine: AsyncEngine,
    far1627_env: _Env,
    clean_registry: InFlightRunRegistry,
) -> None:
    """M1: a never-retry ``retry_policy`` forbids the requeue, as in sibling legs.

    A policy whose ``on`` covers neither the failure nor the stall class is an
    explicit "never re-run this automatically" decision; the handback honours
    it instead of requeueing anyway (unit-level coverage of the same gate is in
    ``tests/unit/core/test_worker_handoff.py``).
    """
    run_id = await _seed_running_run(
        db_engine,
        far1627_env,
        run_number=1,
        claim_token="tok-policy",
        dispatch_phase="first_node_dispatched",
    )
    async with db_engine.begin() as conn:
        await conn.execute(
            text("UPDATE pipelines SET retry_policy = CAST(:p AS json) WHERE id = :pid"),
            {"p": json.dumps({"on": ["eval_failed"], "max_retries": 3}), "pid": str(far1627_env.pipeline_id)},
        )
    await _register_abandoned(clean_registry, run_id, far1627_env.org_id, "tok-policy")

    await _stop_worker_with_shutdown_hook(db_engine)

    after = await _fetch_run(db_engine, run_id)
    assert after["status"] == "failed"
    assert after["error_code"] == WORKER_LOST_ERROR_CODE
    assert "retry policy" in str(after["error_detail"])
    assert not await _run_matches_re_dispatch_predicate(db_engine, run_id)


async def test_handoff_leaves_unobserved_run_to_the_backstop(
    db_engine: AsyncEngine,
    far1627_env: _Env,
    clean_registry: InFlightRunRegistry,
) -> None:
    """M3: an entry whose task never observed the cancellation is NOT handed back.

    SAQ runs the ``shutdown`` callbacks even when ``cancel_tasks`` timed out,
    so a stuck task can still be alive at hook time. Releasing its run would
    re-dispatch against a LIVE predecessor whose external side effects (E2B
    sandbox, connector writes) are not DB-token-fenced — exactly the guarantee
    the old docstring asserted but never established. The run must stay
    untouched and fall back to the heartbeat-TTL sweeps instead.
    """
    run_id = await _seed_running_run(
        db_engine,
        far1627_env,
        run_number=1,
        claim_token="tok-stuck",
        dispatch_phase="first_node_dispatched",
    )
    # A bare register(): no task entered/cancelled through track_inflight_run,
    # so nothing ever observed a cancellation for this entry.
    clean_registry.register(run_id=run_id, org_id=far1627_env.org_id, claim_token="tok-stuck")

    await _stop_worker_with_shutdown_hook(db_engine)

    after = await _fetch_run(db_engine, run_id)
    assert after["status"] == "running"
    assert after["error_code"] is None
    assert after["heartbeat_at"] is not None
    assert after["claim_token"] == "tok-stuck"
    # Not on the re-dispatch leg either — the backstop owns it now.
    assert not await _run_matches_re_dispatch_predicate(db_engine, run_id)
    # The registry is still drained (the process is shutting down).
    assert len(clean_registry) == 0


async def test_empty_registry_handback_is_a_silent_noop(db_engine: AsyncEngine) -> None:
    """A worker with no in-flight runs (e.g. the system worker) hands back nothing."""
    result = await handback_inflight_runs(db_engine)
    assert result == {"handed_back": 0, "terminalised": 0, "skipped": 0}
