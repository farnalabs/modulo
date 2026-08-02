"""Integration tests for the per-org sandbox concurrency cap.

Drives ``PipelineExecutor._check_capacity`` and the stale-run recovery sweep
DIRECTLY against a real Postgres (testcontainers) with runs seeded via raw
SQL — no GraphValidator, hubs, or checkpointer involved.
"""

import json
import uuid
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker

from modulo.core.pipeline_engine.executor import PipelineExecutor
from modulo.core.pipeline_execution import CAPACITY_TIMEOUT_TTL_MINUTES, stale_run_recovery_sweep
from modulo.db.rls import set_rls_org

pytestmark = pytest.mark.integration

_SANDBOX_GRAPH = {
    "nodes": [{"id": "sandbox-1", "node_type": "sandbox_agent", "agent_prompt": "do work"}],
    "edges": [],
}

_PIPELINE_CAP = 100  # pipeline max_concurrent_runs high so only the org cap binds


def _thread_id(org_id: uuid.UUID, run_id: uuid.UUID) -> str:
    return f"{org_id}:{run_id}"


def _hash(seq: int) -> str:
    return f"cap-{seq}-{uuid.uuid4().hex[:8]}"


# Globally increasing run_number — unique per (organisation_id, run_number).
_run_number_seq = 0


def _next_run_number() -> int:
    global _run_number_seq
    _run_number_seq += 1
    return _run_number_seq


# ---------------------------------------------------------------------------
# Seeding helpers (raw SQL, RLS-scoped)
# ---------------------------------------------------------------------------


async def _seed_org(db_engine: AsyncEngine, name: str, cap: int | None) -> uuid.UUID:
    org_id = uuid.uuid4()
    settings = {"sandbox_concurrency_limit": cap} if cap is not None else {}
    async with db_engine.connect() as conn, conn.begin():
        await conn.execute(
            text(
                "INSERT INTO organisations (id, name, slug, settings_json) "
                "VALUES (:id, :name, :slug, CAST(:settings AS json))"
            ),
            {
                "id": str(org_id),
                "name": name,
                "slug": f"{name}-{org_id.hex[:8]}",
                "settings": json.dumps(settings),
            },
        )
    return org_id


async def _seed_account(db_engine: AsyncEngine, org_id: uuid.UUID, email: str) -> uuid.UUID:
    account_id = uuid.uuid4()
    async with db_engine.connect() as conn, conn.begin():
        await conn.execute(
            text(
                "INSERT INTO accounts (id, email, display_name, auth_provider, active, password_hash) "
                "VALUES (:id, :email, :name, 'local', true, 'hash')"
            ),
            {"id": str(account_id), "email": email, "name": f"Admin {email}"},
        )
    return account_id


async def _seed_pipeline(
    db_engine: AsyncEngine,
    org_id: uuid.UUID,
    name: str,
    account_id: uuid.UUID,
    max_concurrent: int = _PIPELINE_CAP,
) -> uuid.UUID:
    pipeline_id = uuid.uuid4()
    async with db_engine.connect() as conn, conn.begin():
        await conn.execute(
            text(
                "INSERT INTO pipelines (id, organisation_id, name, account_id, "
                "max_concurrent_runs, lock_wait_timeout_seconds, node_timeout_seconds, "
                "run_context_defaults, graph_nodes_json, default_autonomy_level, visibility) "
                "VALUES (:id, :oid, :name, :uid, :mcr, 30, 300, "
                "'{}'::json, '[]'::json, 'manual_approval', 'org')"
            ),
            {
                "id": str(pipeline_id),
                "oid": str(org_id),
                "name": name,
                "uid": str(account_id),
                "mcr": max_concurrent,
            },
        )
    return pipeline_id


async def _seed_snapshot(db_engine: AsyncEngine, org_id: uuid.UUID, pipeline_id: uuid.UUID, graph: dict) -> uuid.UUID:
    snapshot_id = uuid.uuid4()
    async with db_engine.connect() as conn, conn.begin():
        await conn.execute(
            text(
                "INSERT INTO pipeline_snapshots (id, pipeline_id, organisation_id, "
                "snapshot_version, graph_json, connector_bindings_json, "
                "schema_pins_json, prompt_pins_json, model_backend_pins_json, "
                "run_context_defaults, config_json) "
                "VALUES (:id, :pid, :oid, 1, CAST(:graph AS json), '[]'::json, "
                "'[]'::json, '[]'::json, '[]'::json, '{}'::json, '{}'::json)"
            ),
            {
                "id": str(snapshot_id),
                "pid": str(pipeline_id),
                "oid": str(org_id),
                "graph": json.dumps(graph),
            },
        )
    return snapshot_id


async def _seed_run(
    db_engine: AsyncEngine,
    org_id: uuid.UUID,
    pipeline_id: uuid.UUID,
    snapshot_id: uuid.UUID,
    *,
    status: str = "pending",
    error_code: str | None = None,
    created_at: datetime | None = None,
    heartbeat_at: datetime | None = None,
    dispatched_at: datetime | None = None,
) -> uuid.UUID:
    from sqlalchemy import insert

    from modulo.db.models.run import Run

    run_id = uuid.uuid4()
    factory = async_sessionmaker(db_engine, expire_on_commit=False)
    values: dict = {
        "id": run_id,
        "organisation_id": org_id,
        "pipeline_id": pipeline_id,
        "snapshot_id": snapshot_id,
        "trigger_type": "manual",
        "status": status,
        "input_hash": _hash(_next_run_number()),
        "langgraph_thread_id": _thread_id(org_id, run_id),
        "error_code": error_code,
        "run_number": _next_run_number(),
    }
    for col, value in (
        ("created_at", created_at),
        ("heartbeat_at", heartbeat_at),
        ("dispatched_at", dispatched_at),
    ):
        if value is not None:
            values[col] = value
    async with factory() as session, session.begin():
        await set_rls_org(session, org_id)
        await session.execute(insert(Run).values(**values))
    return run_id


async def _run_state(
    db_engine: AsyncEngine,
    org_id: uuid.UUID,
    run_id: uuid.UUID,
) -> tuple[str, str | None]:
    factory = async_sessionmaker(db_engine, expire_on_commit=False)
    async with factory() as session, session.begin():
        await set_rls_org(session, org_id)
        result = await session.execute(
            text("SELECT status, error_code FROM runs WHERE id = :rid"),
            {"rid": str(run_id)},
        )
        row = result.first()
        return (row.status, row.error_code) if row else ("missing", None)


async def _run_heartbeat(
    db_engine: AsyncEngine,
    org_id: uuid.UUID,
    run_id: uuid.UUID,
) -> datetime | None:
    factory = async_sessionmaker(db_engine, expire_on_commit=False)
    async with factory() as session, session.begin():
        await set_rls_org(session, org_id)
        result = await session.execute(
            text("SELECT heartbeat_at FROM runs WHERE id = :rid"),
            {"rid": str(run_id)},
        )
        row = result.first()
        return row[0] if row else None


@pytest.fixture(autouse=True)
def patched_redispatch(monkeypatch: pytest.MonkeyPatch) -> AsyncMock:
    """Fence the real SAQ/Celery dispatch out of sweep tests.

    The stranded re-dispatch branch calls ``_re_dispatch_capacity_blocked``
    which routes through ``dispatch_run`` (SAQ/Celery) — neither exists in the
    integration container. Every sweep test gets a deterministic AsyncMock so
    the branch's firing (or not) is observable without a broker.
    """
    from modulo.core import pipeline_execution as pe

    mock = AsyncMock(return_value="enqueued")
    monkeypatch.setattr(pe, "_re_dispatch_capacity_blocked", mock)
    return mock


def _make_executor(db_engine: AsyncEngine) -> PipelineExecutor:
    return PipelineExecutor(db_engine)


async def _seed_org_account(db_engine: AsyncEngine, name: str, cap: int | None) -> tuple[uuid.UUID, uuid.UUID]:
    org_id = await _seed_org(db_engine, name, cap)
    account_id = await _seed_account(db_engine, org_id, f"{name}-{org_id.hex[:8]}@test.local")
    return org_id, account_id


# ---------------------------------------------------------------------------
# Sequential admission at the org cap
# ---------------------------------------------------------------------------


async def test_sequential_runs_across_pipelines_respect_org_cap(
    db_engine: AsyncEngine,
):
    org_id, user_id = await _seed_org_account(db_engine, "CapOrg-A", cap=2)
    pipe_a = await _seed_pipeline(db_engine, org_id, "PipeA", user_id)
    pipe_b = await _seed_pipeline(db_engine, org_id, "PipeB", user_id)
    snap_a = await _seed_snapshot(db_engine, org_id, pipe_a, _SANDBOX_GRAPH)
    snap_b = await _seed_snapshot(db_engine, org_id, pipe_b, _SANDBOX_GRAPH)

    runs = [
        (await _seed_run(db_engine, org_id, pipe_a, snap_a), pipe_a, snap_a),
        (await _seed_run(db_engine, org_id, pipe_a, snap_a), pipe_a, snap_a),
        (await _seed_run(db_engine, org_id, pipe_b, snap_b), pipe_b, snap_b),
        (await _seed_run(db_engine, org_id, pipe_b, snap_b), pipe_b, snap_b),
        (await _seed_run(db_engine, org_id, pipe_a, snap_a), pipe_a, snap_a),
    ]

    executor = _make_executor(db_engine)
    for run_id, pipe_id, snap_id in runs:
        await executor._check_capacity(
            run_id=run_id,
            org_id=org_id,
            pipeline_id=pipe_id,
            max_concurrent=_PIPELINE_CAP,
            graph_json=_SANDBOX_GRAPH,
            snapshot_id=snap_id,
        )

    running = 0
    pending_marked = 0
    for run_id, _pipe_id, _snap_id in runs:
        status, code = await _run_state(db_engine, org_id, run_id)
        if status == "running":
            running += 1
            assert code is None, "admitted runs must have no marker"
        else:
            assert status == "pending"
            assert code == "org_capacity_limited"
            pending_marked += 1

    assert running == 2
    assert pending_marked == 3


async def test_freed_slot_is_claimed_on_next_check(
    db_engine: AsyncEngine,
):
    org_id, user_id = await _seed_org_account(db_engine, "CapOrg-B", cap=1)
    pipe = await _seed_pipeline(db_engine, org_id, "PipeB", user_id)
    snap = await _seed_snapshot(db_engine, org_id, pipe, _SANDBOX_GRAPH)

    first = await _seed_run(db_engine, org_id, pipe, snap)
    second = await _seed_run(db_engine, org_id, pipe, snap)

    executor = _make_executor(db_engine)
    await executor._check_capacity(
        run_id=first,
        org_id=org_id,
        pipeline_id=pipe,
        max_concurrent=_PIPELINE_CAP,
        graph_json=_SANDBOX_GRAPH,
        snapshot_id=snap,
    )
    await executor._check_capacity(
        run_id=second,
        org_id=org_id,
        pipeline_id=pipe,
        max_concurrent=_PIPELINE_CAP,
        graph_json=_SANDBOX_GRAPH,
        snapshot_id=snap,
    )

    status, code = await _run_state(db_engine, org_id, second)
    assert status == "pending"
    assert code == "org_capacity_limited"

    # Free the slot: mark the admitted run complete, then the pending run is claimed.
    factory = async_sessionmaker(db_engine, expire_on_commit=False)
    async with factory() as session, session.begin():
        await set_rls_org(session, org_id)
        await session.execute(text("UPDATE runs SET status = 'complete' WHERE id = :rid"), {"rid": str(first)})

    await executor._check_capacity(
        run_id=second,
        org_id=org_id,
        pipeline_id=pipe,
        max_concurrent=_PIPELINE_CAP,
        graph_json=_SANDBOX_GRAPH,
        snapshot_id=snap,
    )

    status, code = await _run_state(db_engine, org_id, second)
    assert status == "running"
    assert code is None, "marker must be cleared on admission"


async def test_lowering_cap_does_not_kill_in_flight_runs(
    db_engine: AsyncEngine,
):
    org_id, user_id = await _seed_org_account(db_engine, "CapOrg-C", cap=1)
    pipe = await _seed_pipeline(db_engine, org_id, "PipeC", user_id)
    snap = await _seed_snapshot(db_engine, org_id, pipe, _SANDBOX_GRAPH)

    # Two in-flight runs admitted before the cap was lowered.
    running_a = await _seed_run(db_engine, org_id, pipe, snap, status="running")
    running_b = await _seed_run(db_engine, org_id, pipe, snap, status="running")
    new_run = await _seed_run(db_engine, org_id, pipe, snap)

    executor = _make_executor(db_engine)
    await executor._check_capacity(
        run_id=new_run,
        org_id=org_id,
        pipeline_id=pipe,
        max_concurrent=_PIPELINE_CAP,
        graph_json=_SANDBOX_GRAPH,
        snapshot_id=snap,
    )

    # Admission-only semantics: existing running runs are untouched.
    for run_id in (running_a, running_b):
        status, _code = await _run_state(db_engine, org_id, run_id)
        assert status == "running"
    status, code = await _run_state(db_engine, org_id, new_run)
    assert status == "pending"
    assert code == "org_capacity_limited"


async def test_org_b_running_runs_do_not_block_org_a(
    db_engine: AsyncEngine,
):
    org_a, user_a = await _seed_org_account(db_engine, "Isolated-A", cap=1)
    org_b, user_b = await _seed_org_account(db_engine, "Isolated-B", cap=1)
    pipe_a = await _seed_pipeline(db_engine, org_a, "PipeA", user_a)
    pipe_b = await _seed_pipeline(db_engine, org_b, "PipeB", user_b)
    snap_a = await _seed_snapshot(db_engine, org_a, pipe_a, _SANDBOX_GRAPH)
    snap_b = await _seed_snapshot(db_engine, org_b, pipe_b, _SANDBOX_GRAPH)

    # Org B saturates its own cap.
    await _seed_run(db_engine, org_b, pipe_b, snap_b, status="running")
    await _seed_run(db_engine, org_b, pipe_b, snap_b, status="running")

    new_run = await _seed_run(db_engine, org_a, pipe_a, snap_a)
    executor = _make_executor(db_engine)
    await executor._check_capacity(
        run_id=new_run,
        org_id=org_a,
        pipeline_id=pipe_a,
        max_concurrent=_PIPELINE_CAP,
        graph_json=_SANDBOX_GRAPH,
        snapshot_id=snap_a,
    )

    status, code = await _run_state(db_engine, org_a, new_run)
    assert status == "running", "Org B's load must not block Org A"
    assert code is None


async def test_non_sandbox_graph_bypasses_org_cap(
    db_engine: AsyncEngine,
):
    org_id, user_id = await _seed_org_account(db_engine, "NoSandbox", cap=1)
    pipe = await _seed_pipeline(db_engine, org_id, "PipePlain", user_id)
    snap = await _seed_snapshot(db_engine, org_id, pipe, {"nodes": [{"id": "a", "node_type": "agent"}], "edges": []})

    run_a = await _seed_run(db_engine, org_id, pipe, snap)
    run_b = await _seed_run(db_engine, org_id, pipe, snap)

    executor = _make_executor(db_engine)
    plain_graph = {"nodes": [{"id": "a", "node_type": "agent"}], "edges": []}
    for run_id in (run_a, run_b):
        await executor._check_capacity(
            run_id=run_id,
            org_id=org_id,
            pipeline_id=pipe,
            max_concurrent=1,
            graph_json=plain_graph,
            snapshot_id=snap,
        )

    # Org cap is ignored (no sandbox node) — only the per-pipeline cap binds
    # here, so the second run is blocked by pipeline capacity, not org cap.
    status_a, _ = await _run_state(db_engine, org_id, run_a)
    status_b, code_b = await _run_state(db_engine, org_id, run_b)
    assert status_a == "running"
    assert status_b == "pending"
    assert code_b == "pipeline_capacity"


async def test_non_sandbox_running_runs_do_not_consume_sandbox_slots(
    db_engine: AsyncEngine,
):
    """A non-sandbox pipeline running in the org must not consume a sandbox slot.

    Regression for the reviewer finding: ``count_active_sandbox_runs_for_org``
    used to count ALL ``running`` runs, so org cap=1 + one non-sandbox run
    running would wrongly block a sandbox run with ``org_capacity_limited``.
    """
    org_id, user_id = await _seed_org_account(db_engine, "MixedCap", cap=1)
    plain_pipe = await _seed_pipeline(db_engine, org_id, "PlainPipe", user_id)
    sandbox_pipe = await _seed_pipeline(db_engine, org_id, "SandboxPipe", user_id)
    plain_snap = await _seed_snapshot(
        db_engine, org_id, plain_pipe, {"nodes": [{"id": "a", "node_type": "agent"}], "edges": []}
    )
    sandbox_snap = await _seed_snapshot(db_engine, org_id, sandbox_pipe, _SANDBOX_GRAPH)

    # One non-sandbox run is already executing.
    plain_running = await _seed_run(db_engine, org_id, plain_pipe, plain_snap, status="running")

    sandbox_run = await _seed_run(db_engine, org_id, sandbox_pipe, sandbox_snap)
    executor = _make_executor(db_engine)
    await executor._check_capacity(
        run_id=sandbox_run,
        org_id=org_id,
        pipeline_id=sandbox_pipe,
        max_concurrent=_PIPELINE_CAP,
        graph_json=_SANDBOX_GRAPH,
        snapshot_id=sandbox_snap,
    )

    status, code = await _run_state(db_engine, org_id, sandbox_run)
    assert status == "running", "a non-sandbox run must not consume a sandbox slot"
    assert code is None

    # The non-sandbox run is untouched.
    status_plain, _ = await _run_state(db_engine, org_id, plain_running)
    assert status_plain == "running"


async def test_running_non_sandbox_run_leaves_sandbox_cap_to_sandbox_runs(
    db_engine: AsyncEngine,
):
    """Cap=1: a running non-sandbox run + a running sandbox run saturate the
    sandbox slot — a second sandbox run must be demoted."""
    org_id, user_id = await _seed_org_account(db_engine, "MixedCap2", cap=1)
    sandbox_pipe = await _seed_pipeline(db_engine, org_id, "SandboxPipe2", user_id)
    sandbox_snap = await _seed_snapshot(db_engine, org_id, sandbox_pipe, _SANDBOX_GRAPH)

    running_sandbox = await _seed_run(db_engine, org_id, sandbox_pipe, sandbox_snap, status="running")
    blocked = await _seed_run(db_engine, org_id, sandbox_pipe, sandbox_snap)

    executor = _make_executor(db_engine)
    await executor._check_capacity(
        run_id=blocked,
        org_id=org_id,
        pipeline_id=sandbox_pipe,
        max_concurrent=_PIPELINE_CAP,
        graph_json=_SANDBOX_GRAPH,
        snapshot_id=sandbox_snap,
    )

    status, code = await _run_state(db_engine, org_id, blocked)
    assert status == "pending"
    assert code == "org_capacity_limited"
    status_running, _ = await _run_state(db_engine, org_id, running_sandbox)
    assert status_running == "running"


# ---------------------------------------------------------------------------
# Stale-run sweep: durable capacity-timeout backstop
# ---------------------------------------------------------------------------


async def test_sweep_fails_old_marked_pending_but_not_young(
    db_engine: AsyncEngine,
    migrated_db_url: str,
    monkeypatch: pytest.MonkeyPatch,
):
    monkeypatch.setenv("DATABASE_URL", migrated_db_url)

    org_id, user_id = await _seed_org_account(db_engine, "SweepOrg", cap=1)
    pipe = await _seed_pipeline(db_engine, org_id, "PipeSweep", user_id)
    snap = await _seed_snapshot(db_engine, org_id, pipe, _SANDBOX_GRAPH)

    now = datetime.now(UTC)
    old_marked = await _seed_run(
        db_engine,
        org_id,
        pipe,
        snap,
        status="pending",
        error_code="org_capacity_limited",
        created_at=now - timedelta(minutes=CAPACITY_TIMEOUT_TTL_MINUTES + 30),
    )
    young_marked = await _seed_run(
        db_engine,
        org_id,
        pipe,
        snap,
        status="pending",
        error_code="pipeline_capacity",
        created_at=now - timedelta(minutes=10),
    )

    result = await stale_run_recovery_sweep(db_engine)
    assert result.get("error") is None, f"sweep failed: {result}"

    status, code = await _run_state(db_engine, org_id, old_marked)
    assert status == "failed"
    assert code == "capacity_timeout"

    status, code = await _run_state(db_engine, org_id, young_marked)
    assert status == "pending", "young reason-marked pending run must survive the sweep"
    assert code == "pipeline_capacity"


async def test_sweep_never_dispatched_skips_reason_marked_runs(
    db_engine: AsyncEngine,
    migrated_db_url: str,
    monkeypatch: pytest.MonkeyPatch,
):
    monkeypatch.setenv("DATABASE_URL", migrated_db_url)

    org_id, user_id = await _seed_org_account(db_engine, "SweepOrg2", cap=1)
    pipe = await _seed_pipeline(db_engine, org_id, "PipeSweep2", user_id)
    snap = await _seed_snapshot(db_engine, org_id, pipe, _SANDBOX_GRAPH)

    now = datetime.now(UTC)
    # Old (>5 min never-dispatched window), reason-marked, never dispatched.
    marked = await _seed_run(
        db_engine,
        org_id,
        pipe,
        snap,
        status="pending",
        error_code="org_capacity_limited",
        created_at=now - timedelta(minutes=30),
    )
    # Control: plain pending run with no marker and no dispatch — must be swept.
    plain = await _seed_run(
        db_engine,
        org_id,
        pipe,
        snap,
        status="pending",
        created_at=now - timedelta(minutes=30),
    )

    result = await stale_run_recovery_sweep(db_engine)
    assert result.get("error") is None, f"sweep failed: {result}"

    status, code = await _run_state(db_engine, org_id, marked)
    assert status == "pending", "never_dispatched must not kill reason-marked runs"
    assert code == "org_capacity_limited"

    status, code = await _run_state(db_engine, org_id, plain)
    assert status == "failed"
    assert code == "never_dispatched"


# ---------------------------------------------------------------------------
# Stranded capacity-blocked re-dispatch (worker-restart durability)
# ---------------------------------------------------------------------------


async def test_sweep_redispatches_stranded_capacity_blocked_run(
    db_engine: AsyncEngine,
    migrated_db_url: str,
    monkeypatch: pytest.MonkeyPatch,
    patched_redispatch: AsyncMock,
):
    """A capacity-marked run whose in-process retry loop died (stale heartbeat)
    is RE-DISPATCHED, not failed — the durable restart-durability guarantee."""
    from modulo.core.pipeline_executor_task import reset_engines

    reset_engines()
    monkeypatch.setenv("DATABASE_URL", migrated_db_url)

    org_id, user_id = await _seed_org_account(db_engine, "RedispatchOrg", cap=1)
    pipe = await _seed_pipeline(db_engine, org_id, "PipeRedispatch", user_id)
    snap = await _seed_snapshot(db_engine, org_id, pipe, _SANDBOX_GRAPH)

    now = datetime.now(UTC)
    stranded = await _seed_run(
        db_engine,
        org_id,
        pipe,
        snap,
        status="pending",
        error_code="org_capacity_limited",
        created_at=now - timedelta(minutes=30),
        heartbeat_at=now - timedelta(minutes=30),
        dispatched_at=now - timedelta(minutes=30),
    )

    result = await stale_run_recovery_sweep(db_engine)
    assert result.get("error") is None, f"sweep failed: {result}"
    assert result["stranded_capacity_redispatched"] == 1, result
    patched_redispatch.assert_awaited_once_with(str(stranded), str(org_id))

    status, code = await _run_state(db_engine, org_id, stranded)
    assert status == "pending", "re-dispatched run must not be terminal-failed"
    assert code == "org_capacity_limited"

    heartbeat = await _run_heartbeat(db_engine, org_id, stranded)
    assert heartbeat is not None and heartbeat > now - timedelta(seconds=30), (
        "the sweep must stamp a fresh heartbeat so it does not hot-loop"
    )


async def test_sweep_skips_fresh_heartbeat_capacity_blocked_run(
    db_engine: AsyncEngine,
    migrated_db_url: str,
    monkeypatch: pytest.MonkeyPatch,
    patched_redispatch: AsyncMock,
):
    """A live in-process retry loop refreshes heartbeat_at — the sweep's fence
    must skip it (no re-dispatch, no double-execution)."""
    from modulo.core.pipeline_executor_task import reset_engines

    reset_engines()
    monkeypatch.setenv("DATABASE_URL", migrated_db_url)

    org_id, user_id = await _seed_org_account(db_engine, "LiveLoopOrg", cap=1)
    pipe = await _seed_pipeline(db_engine, org_id, "PipeLiveLoop", user_id)
    snap = await _seed_snapshot(db_engine, org_id, pipe, _SANDBOX_GRAPH)

    now = datetime.now(UTC)
    live = await _seed_run(
        db_engine,
        org_id,
        pipe,
        snap,
        status="pending",
        error_code="pipeline_capacity",
        created_at=now - timedelta(minutes=30),
        heartbeat_at=now - timedelta(seconds=5),
        dispatched_at=now - timedelta(minutes=30),
    )

    result = await stale_run_recovery_sweep(db_engine)
    assert result.get("error") is None, f"sweep failed: {result}"
    assert result["stranded_capacity_redispatched"] == 0, result
    patched_redispatch.assert_not_awaited()

    status, code = await _run_state(db_engine, org_id, live)
    assert status == "pending"
    assert code == "pipeline_capacity"


async def test_sweep_still_fails_capacity_timeout_eligible_run(
    db_engine: AsyncEngine,
    migrated_db_url: str,
    monkeypatch: pytest.MonkeyPatch,
    patched_redispatch: AsyncMock,
):
    """A run past the 120-min capacity_timeout TTL must FAIL, never be
    resurrected by the re-dispatch branch (existing behaviour preserved)."""
    from modulo.core.pipeline_executor_task import reset_engines

    reset_engines()
    monkeypatch.setenv("DATABASE_URL", migrated_db_url)

    org_id, user_id = await _seed_org_account(db_engine, "TimeoutOrg", cap=1)
    pipe = await _seed_pipeline(db_engine, org_id, "PipeTimeout", user_id)
    snap = await _seed_snapshot(db_engine, org_id, pipe, _SANDBOX_GRAPH)

    now = datetime.now(UTC)
    old = await _seed_run(
        db_engine,
        org_id,
        pipe,
        snap,
        status="pending",
        error_code="org_capacity_limited",
        created_at=now - timedelta(minutes=CAPACITY_TIMEOUT_TTL_MINUTES + 30),
        heartbeat_at=now - timedelta(minutes=CAPACITY_TIMEOUT_TTL_MINUTES + 30),
        dispatched_at=now - timedelta(minutes=CAPACITY_TIMEOUT_TTL_MINUTES + 30),
    )

    result = await stale_run_recovery_sweep(db_engine)
    assert result.get("error") is None, f"sweep failed: {result}"
    assert result["stranded_capacity_redispatched"] == 0, result
    assert result["capacity_timeout_swept"] == 1, result
    patched_redispatch.assert_not_awaited()

    status, code = await _run_state(db_engine, org_id, old)
    assert status == "failed"
    assert code == "capacity_timeout"
