"""Integration tests for dispatcher_reconcile (plan F3c) - real Redis + Postgres.

Positive path: a staled dispatched run whose SAQ job hash was evicted is
re-dispatched by reconcile WITHOUT SAQ-internal eviction (B2) — re-dispatch
uses a fresh key suffix so SAQ key dedupe never suppresses the recovery enqueue.
F6a review: awaiting_human/claimed runs are NEVER auto-redispatched
(re-dispatch would resume with an empty decision and auto-approve the HITL
gate). F4: capacity-deferred runs (pending, dispatched_at NULL, dispatcher
NULL) are re-dispatched when capacity frees.
"""

from __future__ import annotations

import asyncio
import uuid
from typing import Any

import pytest
from sqlalchemy import text

from modulo.core import cron_helpers as ch

pytestmark = [
    pytest.mark.integration,
    pytest.mark.asyncio(loop_scope="session"),
]


async def _seed_saq_run(
    db_engine: Any,
    org_id: uuid.UUID,
    account_id: uuid.UUID,
    *,
    status: str = "running",
    heartbeat_stale: bool = True,
    dispatched: bool = True,
    dispatcher: str | None = "saq",
    claim_token: str | None = "token-a",
) -> tuple[uuid.UUID, uuid.UUID]:
    pipeline_id = uuid.uuid4()
    snapshot_id = uuid.uuid4()
    run_id = uuid.uuid4()
    run_number = int(run_id.int % 10**9) + 1
    # Unique per seed: the shared session org + uq_pipelines_org_name make a
    # fixed name collide with every later seed in the same pytest session.
    pipeline_name = f"saq-reconcile-test-{uuid.uuid4().hex[:10]}"
    from sqlalchemy import NullPool
    from sqlalchemy.ext.asyncio import create_async_engine

    eng = create_async_engine(db_engine.url.render_as_string(hide_password=False), poolclass=NullPool)
    try:
        async with eng.connect() as conn, conn.begin():
            # enqueue_failed_at + hitl_claims.decision_payload ship in parallel
            # runtime migrations (not yet on this branch) but
            # dispatcher_reconcile/dispatch_run reference them — self-provision
            # idempotently; no-ops once the migrations land.
            await conn.execute(text("ALTER TABLE runs ADD COLUMN IF NOT EXISTS enqueue_failed_at timestamptz"))
            await conn.execute(text("ALTER TABLE hitl_claims ADD COLUMN IF NOT EXISTS decision_payload jsonb"))
            await conn.execute(
                text(
                    "INSERT INTO pipelines (id, organisation_id, account_id, name, graph_nodes_json, "
                    "run_context_defaults, visibility, max_concurrent_runs) "
                    "VALUES (:id, :oid, :uid, :pname, '[]'::json, '{}'::json, 'org', 5)"
                ),
                {"id": str(pipeline_id), "oid": str(org_id), "uid": str(account_id), "pname": pipeline_name},
            )
            await conn.execute(
                text(
                    "INSERT INTO pipeline_snapshots (id, organisation_id, pipeline_id, snapshot_version, "
                    "account_id, graph_json, connector_bindings_json, schema_pins_json, prompt_pins_json, "
                    "model_backend_pins_json, composite_bindings_json, run_context_defaults) "
                    "VALUES (:id, :oid, :pid, 1, :uid, '{}'::json, '[]'::json, '[]'::json, '[]'::json, "
                    "'[]'::json, '[]'::json, '{}'::json)"
                ),
                {"id": str(snapshot_id), "oid": str(org_id), "pid": str(pipeline_id), "uid": str(account_id)},
            )
            await conn.execute(
                text(
                    "INSERT INTO runs (id, organisation_id, pipeline_id, snapshot_id, account_id, trigger_type, "
                    "status, input_hash, langgraph_thread_id, run_number, dispatcher, claim_count, "
                    "heartbeat_at, dispatched_at, saq_job_id, claim_token) "
                    "VALUES (:id, :oid, :pid, :sid, :uid, 'manual', :status, 'hash', :thread, :rn, :disp, 1, "
                    "CASE WHEN :stale THEN now() - interval '30 minutes' ELSE now() END, "
                    "CASE WHEN :dispatched THEN now() - interval '30 minutes' ELSE NULL END, "
                    ":job_id, :tok)"
                ),
                {
                    "id": str(run_id),
                    "oid": str(org_id),
                    "pid": str(pipeline_id),
                    "sid": str(snapshot_id),
                    "uid": str(account_id),
                    "status": status,
                    "thread": f"{org_id}:{run_id}",
                    "rn": run_number,
                    "stale": heartbeat_stale,
                    "dispatched": dispatched,
                    "disp": dispatcher,
                    "job_id": f"saq:job:runs:run:{run_id}",
                    "tok": claim_token,
                },
            )
        return run_id, pipeline_id
    finally:
        await eng.dispose()


async def _job_exists(redis_url: str, job_key: str) -> bool:
    """True when any SAQ job hash exists for *job_key*.

    A reconcile re-dispatch uses a FRESH key suffix (``run:{id}:{suffix}``) so
    SAQ key dedupe can never suppress the recovery enqueue — the existence check
    therefore scans the deterministic key AND any fresh-suffixed key.
    """
    from redis import asyncio as aioredis

    r = aioredis.from_url(redis_url)
    try:
        return len(await r.keys(f"saq:job:runs:{job_key}*")) > 0
    finally:
        await r.aclose()


@pytest.mark.asyncio
async def test_staled_running_run_with_evicted_job_is_redistpatched(
    saq_settings_env: str, db_engine: Any, test_org: uuid.UUID, test_user: uuid.UUID
) -> None:
    # Reconcile re-dispatches through dispatch_run; the SAQ path is the one under
    # test (shadow routes execute_run to Celery, which creates no SAQ job).
    from redis import asyncio as aioredis
    from saq.queue.redis import RedisQueue

    run_id, _ = await _seed_saq_run(db_engine, test_org, test_user, status="running")

    # Simulate a partial eviction: a normal enqueue whose hash was then deleted,
    # leaving the incomplete zset member behind.
    redis_client = aioredis.from_url(saq_settings_env)
    try:
        q = RedisQueue(redis_client, name="runs")
        job = await q.enqueue("modulo.core.saq_worker.execute_run", key=f"run:{run_id}")
        assert job is not None
        await redis_client.delete(job.id)  # evict the job hash
    finally:
        await redis_client.aclose()

    # Reconcile must repair + re-dispatch (staled heartbeat, no job). The
    # summary counts are GLOBAL (dispatcher_reconcile scans every org), so other
    # tests in the same session may add staled runs; assert only that OUR run
    # was among the repaired set via the deterministic job key.
    summary = await ch.dispatcher_reconcile()
    assert summary["repaired"] >= 1

    # The job now exists again (fresh dispatch), key deterministic.
    assert await _job_exists(saq_settings_env, f"run:{run_id}")


@pytest.mark.asyncio
async def test_awaiting_human_evicted_job_is_not_auto_redistpatched(
    saq_settings_env: str, db_engine: Any, test_org: uuid.UUID, test_user: uuid.UUID
) -> None:
    """F6a review: a waiting HITL run is NEVER auto-redispatched by reconcile.

    Its ``execute_run`` job COMPLETED normally at the gate — TTL expiry + stale
    heartbeat are the NORMAL waiting state, not a lost job. Re-dispatching as
    ``resume_run`` with an empty decision would silently AUTO-APPROVE the gate
    (executor.aupdate_state({'_hitl_decision': {}}) -> approved). A human acts
    via the HITL approve/reject endpoint, which dispatches ``resume_run`` itself.
    """
    from redis import asyncio as aioredis
    from saq.queue.redis import RedisQueue

    run_id, _ = await _seed_saq_run(db_engine, test_org, test_user, status="awaiting_human")
    redis_client = aioredis.from_url(saq_settings_env)
    try:
        q = RedisQueue(redis_client, name="runs")
        job = await q.enqueue("modulo.core.saq_worker.execute_run", key=f"run:{run_id}")
        await redis_client.delete(job.id)
    finally:
        await redis_client.aclose()

    # The seed provisions hitl_claims.decision_payload (parallel migration not
    # yet on this branch), so the REAL F6a guard runs: no decision committed ->
    # the waiting run is genuinely waiting and is NOT auto-redispatched.
    # Reconcile must NOT repair our run (awaiting_human is never auto-
    # redispatched). The summary's global "repaired" count may include other
    # orgs' staled runs from the same session, so assert on OUR run directly.
    await ch.dispatcher_reconcile()

    # The run must NOT be re-dispatched — no job appears and the claim token is
    # untouched (the gate stays pending on a human).
    assert not await _job_exists(saq_settings_env, f"run:{run_id}")

    from sqlalchemy import NullPool
    from sqlalchemy.ext.asyncio import create_async_engine

    eng = create_async_engine(db_engine.url.render_as_string(hide_password=False), poolclass=NullPool)
    try:
        async with eng.connect() as conn:
            row = (
                await conn.execute(text("SELECT claim_token, status FROM runs WHERE id=:rid"), {"rid": str(run_id)})
            ).first()
    finally:
        await eng.dispose()
    assert row[0] == "token-a"  # claim token not rotated
    assert row[1] == "awaiting_human"  # still waiting on the human


@pytest.mark.asyncio
async def test_capacity_deferred_run_redispatched_when_capacity_frees(
    saq_settings_env: str, db_engine: Any, test_org: uuid.UUID, test_user: uuid.UUID
) -> None:
    """F4 review: a capacity-deferred run (pending, dispatched_at NULL,
    dispatcher NULL) must be re-dispatched when capacity frees. dispatch_run
    returns deferred BEFORE recording dispatched_at/dispatcher, so the
    capacity-deferred branch must match on the creation path, not
    dispatcher='saq'. Under schema 0075 (runtime cutover) runs.claim_token is
    NOT NULL, so a capacity-deferred run carries a token and reconcile must
    preserve it (never clobber it) on re-dispatch."""
    run_id, _ = await _seed_saq_run(
        db_engine,
        test_org,
        test_user,
        status="pending",
        dispatched=False,
        dispatcher=None,
        # Under schema 0075 (runtime cutover) runs.claim_token is NOT NULL with a
        # server default (gen_random_uuid()::text), so even a never-claimed
        # capacity-deferred run carries a token at insert time. _record_saq_job
        # only writes a fresh token when the token is NULL — it must PRESERVE an
        # existing token, never clobber it.
        claim_token="seed-token",
    )

    # Re-dispatch happens (summary count is global across orgs, so assert on our
    # run's job + row state instead of the exact global "repaired" number).
    await ch.dispatcher_reconcile()

    # A fresh dispatch records dispatcher='saq' and preserves the run's existing
    # claim token (never clobbers it); the deterministic job key now exists.
    assert await _job_exists(saq_settings_env, f"run:{run_id}")

    from sqlalchemy import NullPool
    from sqlalchemy.ext.asyncio import create_async_engine

    eng = create_async_engine(db_engine.url.render_as_string(hide_password=False), poolclass=NullPool)
    try:
        async with eng.connect() as conn:
            row = (
                await conn.execute(text("SELECT dispatcher, claim_token FROM runs WHERE id=:rid"), {"rid": str(run_id)})
            ).first()
    finally:
        await eng.dispose()
    assert row[0] == "saq"
    assert row[1] == "seed-token"  # dispatch preserves an existing token; it never clobbers it


@pytest.mark.asyncio
async def test_live_job_not_repaired(
    saq_settings_env: str, db_engine: Any, test_org: uuid.UUID, test_user: uuid.UUID
) -> None:
    from redis import asyncio as aioredis
    from saq.queue.redis import RedisQueue

    run_id, _ = await _seed_saq_run(db_engine, test_org, test_user, status="running", heartbeat_stale=False)
    redis_client = aioredis.from_url(saq_settings_env)
    try:
        q = RedisQueue(redis_client, name="runs")
        await q.enqueue("modulo.core.saq_worker.execute_run", key=f"run:{run_id}")
    finally:
        await redis_client.aclose()

    # A live (non-staled) job must NOT be repaired — the job survives reconcile
    # untouched (fresh heartbeat + live job = not staled).
    await ch.dispatcher_reconcile()
    assert await _job_exists(saq_settings_env, f"run:{run_id}")


@pytest.mark.asyncio
async def test_two_connections_no_double_redispatch(
    saq_settings_env: str, db_engine: Any, test_org: uuid.UUID, test_user: uuid.UUID, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Two concurrent dispatcher_reconcile() passes (separate NullPool engines,
    real Redis) must not double-execute the same staled run.

    Reconcile re-dispatch never evicts SAQ internals (B2) — the worker's
    ATOMIC claim UPDATE is the real at-most-once dedupe. Even when both passes
    select the same stale row, exactly one subsequent claim wins, so the run is
    re-dispatched (dispatcher='saq', a job exists) and executed exactly once.
    """
    from sqlalchemy import event as sa_event
    from sqlalchemy.ext.asyncio import create_async_engine
    from sqlalchemy.pool import NullPool

    from modulo.core import pipeline_execution as pe

    run_id, _ = await _seed_saq_run(
        db_engine,
        test_org,
        test_user,
        status="pending",
        dispatched=True,
        dispatcher=None,
        claim_token="seed-token",
    )
    # B3 enqueue-failed branch: pending + dispatched_at set + dispatcher NULL +
    # enqueue_failed_at set + stale heartbeat (the seed's heartbeat is already
    # 30min stale; the enqueue-failure marker must be < the 60m TTL backstop).
    eng = create_async_engine(db_engine.url.render_as_string(hide_password=False), poolclass=NullPool)
    try:
        async with eng.connect() as conn, conn.begin():
            await conn.execute(
                text("UPDATE runs SET enqueue_failed_at = now() - interval '10 minutes' WHERE id=:rid"),
                {"rid": str(run_id)},
            )
    finally:
        await eng.dispose()

    engines: list[Any] = []

    def _make_rls_engine() -> Any:
        e = create_async_engine(db_engine.url.render_as_string(hide_password=False), poolclass=NullPool)

        @sa_event.listens_for(e.sync_engine, "checkout")
        def _set_role(dbapi_connection: object, _connection_record: object, _connection_proxy: object) -> None:
            cursor = dbapi_connection.cursor()  # type: ignore[attr-defined]
            try:
                cursor.execute('SET ROLE "modulo_integration_app"')
            finally:
                cursor.close()

        engines.append(e)
        return e

    # Per-connection engines: each reconcile pass gets a FRESH NullPool engine
    # running as the NOBYPASSRLS role (RLS applies, mirroring production).
    #
    # PR #1637 routed dispatcher_reconcile through _open_system_factory (the
    # modulo_system engine path). _get_system_engine caches its engine globally
    # (and falls back to _get_engine when MODULO_SYSTEM_DATABASE_URL is unset),
    # so patching _get_engine alone no longer guarantees each reconcile pass uses
    # the RLS role engine. Patch _open_system_factory directly to bind the
    # reconcile sessions to the NOBYPASSRLS role engine.
    from sqlalchemy.ext.asyncio import async_sessionmaker

    monkeypatch.setattr(
        ch,
        "_open_system_factory",
        lambda: async_sessionmaker(_make_rls_engine(), expire_on_commit=False, autobegin=False),
    )

    try:
        summary_a, summary_b = await asyncio.gather(ch.dispatcher_reconcile(), ch.dispatcher_reconcile())
    finally:
        for e in engines:
            await e.dispose()

    assert summary_a["redis_errors"] + summary_b["redis_errors"] == 0
    assert summary_a["repaired"] + summary_b["repaired"] >= 1, f"{summary_a} {summary_b}"

    # The run was re-dispatched (dispatcher='saq') and a SAQ job now exists.
    assert await _job_exists(saq_settings_env, f"run:{run_id}")
    eng = create_async_engine(db_engine.url.render_as_string(hide_password=False), poolclass=NullPool)
    try:
        async with eng.connect() as conn:
            row = (
                await conn.execute(
                    text("SELECT dispatcher, status FROM runs WHERE id=:rid"),
                    {"rid": str(run_id)},
                )
            ).first()
    finally:
        await eng.dispose()
    assert row[0] == "saq"
    assert row[1] == "pending"

    # Exactly-once EXECUTION: two claims on the re-dispatched run → exactly one
    # wins (the atomic claim UPDATE dedupes any concurrent re-dispatch).
    token_1 = await pe.claim_run_async(db_engine, str(run_id), str(test_org))
    token_2 = await pe.claim_run_async(db_engine, str(run_id), str(test_org))
    assert len([t for t in (token_1, token_2) if t is not None]) == 1
