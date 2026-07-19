"""Integration tests for concurrent run execution, state transitions, and concurrency limits.

Tests that the system handles multiple runs correctly under concurrency:
  1. Concurrent run creation (raw SQL inserts with unique IDs)
  2. Concurrent status transitions (pending -> running -> completed)
  3. Active run counting under concurrent status updates
  4. Trigger-level max_concurrent_runs enforcement via TriggerEngine
"""

import asyncio
import hashlib
import json
import time
import uuid

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker

from modulo.core.trigger_engine import ConcurrentRunLimitError, TriggerEngine
from modulo.db.rls import set_rls_org

pytestmark = pytest.mark.integration


# ---------------------------------------------------------------------------
# Fixtures — inherited from top-level integration/conftest.py:
#   test_org, test_user, test_pipeline, test_snapshot, test_trigger
# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _input_hash(payload: dict) -> str:
    serialised = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(serialised.encode()).hexdigest()


def _thread_id(org_id: uuid.UUID, run_id: uuid.UUID) -> str:
    return f"{org_id}:{run_id}"


# ---------------------------------------------------------------------------
# Concurrent run creation
# ---------------------------------------------------------------------------


class TestConcurrentRunCreation:
    async def test_concurrent_create_ten_runs(
        self,
        db_engine: AsyncEngine,
        test_org: uuid.UUID,
        test_pipeline: uuid.UUID,
        test_snapshot: uuid.UUID,
    ) -> None:
        count = 10
        factory = async_sessionmaker(db_engine, expire_on_commit=False)

        async def _create_one(i: int) -> uuid.UUID:
            run_id = uuid.uuid4()
            async with factory() as session, session.begin():
                await set_rls_org(session, test_org)
                await session.execute(
                    text(
                        "INSERT INTO runs (id, organisation_id, pipeline_id, "
                        "snapshot_id, trigger_type, status, input_hash, "
                        "langgraph_thread_id) "
                        "VALUES (:rid, :oid, :pid, :sid, 'manual', 'pending', "
                        ":hash, :tid)",
                    ),
                    {
                        "rid": str(run_id),
                        "oid": str(test_org),
                        "pid": str(test_pipeline),
                        "sid": str(test_snapshot),
                        "hash": _input_hash({"seq": i}),
                        "tid": _thread_id(test_org, run_id),
                    },
                )
            return run_id

        run_ids = await asyncio.gather(*[_create_one(i) for i in range(count)])
        assert len(set(run_ids)) == count, "Run IDs must be unique"

        # Verify all exist and are pending
        async with factory() as session, session.begin():
            await set_rls_org(session, test_org)
            result = await session.execute(
                text("SELECT status, count(*) FROM runs WHERE id = ANY(:ids) GROUP BY status"),
                {"ids": [str(rid) for rid in run_ids]},
            )
            rows = result.all()
            assert len(rows) == 1
            assert rows[0].status == "pending"
            assert rows[0].count == count

    async def test_concurrent_create_same_payload(
        self,
        db_engine: AsyncEngine,
        test_org: uuid.UUID,
        test_pipeline: uuid.UUID,
        test_snapshot: uuid.UUID,
    ) -> None:
        factory = async_sessionmaker(db_engine, expire_on_commit=False)
        payload = {"same": "payload"}
        hash_val = _input_hash(payload)

        async def _create_dup() -> uuid.UUID:
            run_id = uuid.uuid4()
            async with factory() as session, session.begin():
                await set_rls_org(session, test_org)
                await session.execute(
                    text(
                        "INSERT INTO runs (id, organisation_id, pipeline_id, "
                        "snapshot_id, trigger_type, status, input_hash, "
                        "langgraph_thread_id) "
                        "VALUES (:rid, :oid, :pid, :sid, 'manual', 'pending', "
                        ":hash, :tid)",
                    ),
                    {
                        "rid": str(run_id),
                        "oid": str(test_org),
                        "pid": str(test_pipeline),
                        "sid": str(test_snapshot),
                        "hash": hash_val,
                        "tid": _thread_id(test_org, run_id),
                    },
                )
            return run_id

        run_ids = await asyncio.gather(*[_create_dup() for _ in range(5)])
        assert len(set(run_ids)) == 5


# ---------------------------------------------------------------------------
# Concurrent status transitions
# ---------------------------------------------------------------------------


class TestConcurrentStatusTransitions:
    async def test_concurrent_transition_to_running(
        self,
        db_engine: AsyncEngine,
        test_org: uuid.UUID,
        test_pipeline: uuid.UUID,
        test_snapshot: uuid.UUID,
    ) -> None:
        count = 10
        factory = async_sessionmaker(db_engine, expire_on_commit=False)
        run_ids = []

        async with factory() as session, session.begin():
            await set_rls_org(session, test_org)
            for i in range(count):
                run_id = uuid.uuid4()
                await session.execute(
                    text(
                        "INSERT INTO runs (id, organisation_id, pipeline_id, "
                        "snapshot_id, trigger_type, status, input_hash, "
                        "langgraph_thread_id) "
                        "VALUES (:rid, :oid, :pid, :sid, 'manual', 'pending', "
                        ":hash, :tid)",
                    ),
                    {
                        "rid": str(run_id),
                        "oid": str(test_org),
                        "pid": str(test_pipeline),
                        "sid": str(test_snapshot),
                        "hash": _input_hash({"seq": i}),
                        "tid": _thread_id(test_org, run_id),
                    },
                )
                run_ids.append(run_id)

        async def _transition(rid: uuid.UUID) -> None:
            async with factory() as session, session.begin():
                await set_rls_org(session, test_org)
                await session.execute(
                    text("UPDATE runs SET status = 'running', started_at = NOW() WHERE id = :id"),
                    {"id": str(rid)},
                )

        await asyncio.gather(*[_transition(rid) for rid in run_ids])

        async with factory() as session, session.begin():
            await set_rls_org(session, test_org)
            result = await session.execute(
                text("SELECT status, count(*) FROM runs WHERE id = ANY(:ids) GROUP BY status"),
                {"ids": [str(rid) for rid in run_ids]},
            )
            rows = result.all()
            assert len(rows) == 1
            assert rows[0].status == "running"
            assert rows[0].count == count

    async def test_concurrent_full_state_machine(
        self,
        db_engine: AsyncEngine,
        test_org: uuid.UUID,
        test_pipeline: uuid.UUID,
        test_snapshot: uuid.UUID,
    ) -> None:
        count = 10
        factory = async_sessionmaker(db_engine, expire_on_commit=False)
        run_ids = []

        async with factory() as session, session.begin():
            await set_rls_org(session, test_org)
            for i in range(count):
                run_id = uuid.uuid4()
                await session.execute(
                    text(
                        "INSERT INTO runs (id, organisation_id, pipeline_id, "
                        "snapshot_id, trigger_type, status, input_hash, "
                        "langgraph_thread_id) "
                        "VALUES (:rid, :oid, :pid, :sid, 'manual', 'pending', "
                        ":hash, :tid)",
                    ),
                    {
                        "rid": str(run_id),
                        "oid": str(test_org),
                        "pid": str(test_pipeline),
                        "sid": str(test_snapshot),
                        "hash": _input_hash({"seq": i}),
                        "tid": _thread_id(test_org, run_id),
                    },
                )
                run_ids.append(run_id)

        async def _full(rid: uuid.UUID) -> None:
            async with factory() as session:
                async with session.begin():
                    await set_rls_org(session, test_org)
                    await session.execute(
                        text("UPDATE runs SET status = 'running', started_at = NOW() WHERE id = :id"),
                        {"id": str(rid)},
                    )
                await asyncio.sleep(0.01)
            async with factory() as session, session.begin():
                await set_rls_org(session, test_org)
                await session.execute(
                    text("UPDATE runs SET status = 'complete', completed_at = NOW() WHERE id = :id"),
                    {"id": str(rid)},
                )

        await asyncio.gather(*[_full(rid) for rid in run_ids])

        async with factory() as session, session.begin():
            await set_rls_org(session, test_org)
            result = await session.execute(
                text("SELECT status, count(*) FROM runs WHERE id = ANY(:ids) GROUP BY status"),
                {"ids": [str(rid) for rid in run_ids]},
            )
            rows = result.all()
            assert len(rows) == 1
            assert rows[0].status == "complete"
            assert rows[0].count == count


# ---------------------------------------------------------------------------
# Active run counting under concurrency
# ---------------------------------------------------------------------------


class TestConcurrentActiveRunCounting:
    async def test_concurrent_count_with_transitions(
        self,
        db_engine: AsyncEngine,
        test_org: uuid.UUID,
        test_user: uuid.UUID,
    ) -> None:
        """Uses a dedicated pipeline to avoid count pollution from other tests."""
        factory = async_sessionmaker(db_engine, expire_on_commit=False)

        # Create a private pipeline + snapshot for this test
        pipeline_id = uuid.uuid4()
        snapshot_id = uuid.uuid4()
        async with factory() as session, session.begin():
            await set_rls_org(session, test_org)
            await session.execute(
                text(
                    "INSERT INTO pipelines (id, organisation_id, name, account_id, "
                    "max_concurrent_runs, lock_wait_timeout_seconds, "
                    "node_timeout_seconds, run_context_defaults, graph_nodes_json) "
                    "VALUES (:id, :oid, :name, :uid, 10, 30, 300, '{}'::json, '[]'::json)",
                ),
                {
                    "id": str(pipeline_id),
                    "oid": str(test_org),
                    "name": "Count Test Pipeline",
                    "uid": str(test_user),
                },
            )
            await session.execute(
                text(
                    "INSERT INTO pipeline_snapshots (id, pipeline_id, organisation_id, "
                    "snapshot_version, graph_json, connector_bindings_json, "
                    "schema_pins_json, prompt_pins_json, model_backend_pins_json, "
                    "run_context_defaults, config_json) "
                    "VALUES (:id, :pid, :oid, 1, '{}'::json, '[]'::json, "
                    "'[]'::json, '[]'::json, '[]'::json, '{}'::json, '{}'::json)",
                ),
                {"id": str(snapshot_id), "pid": str(pipeline_id), "oid": str(test_org)},
            )

        run_ids = []
        async with factory() as session, session.begin():
            await set_rls_org(session, test_org)
            for i in range(20):
                run_id = uuid.uuid4()
                await session.execute(
                    text(
                        "INSERT INTO runs (id, organisation_id, pipeline_id, "
                        "snapshot_id, trigger_type, status, input_hash, "
                        "langgraph_thread_id) "
                        "VALUES (:rid, :oid, :pid, :sid, 'manual', 'pending', "
                        ":hash, :tid)",
                    ),
                    {
                        "rid": str(run_id),
                        "oid": str(test_org),
                        "pid": str(pipeline_id),
                        "sid": str(snapshot_id),
                        "hash": _input_hash({"seq": i}),
                        "tid": _thread_id(test_org, run_id),
                    },
                )
                run_ids.append(run_id)

        async def _make_active(rid: uuid.UUID) -> None:
            async with factory() as session, session.begin():
                await set_rls_org(session, test_org)
                await session.execute(
                    text("UPDATE runs SET status = 'running' WHERE id = :id"),
                    {"id": str(rid)},
                )

        await asyncio.gather(*[_make_active(rid) for rid in run_ids[:10]])

        async def _make_terminal(rid: uuid.UUID) -> None:
            async with factory() as session, session.begin():
                await set_rls_org(session, test_org)
                await session.execute(
                    text("UPDATE runs SET status = 'complete' WHERE id = :id"),
                    {"id": str(rid)},
                )

        await asyncio.gather(*[_make_terminal(rid) for rid in run_ids[10:]])

        async with factory() as session, session.begin():
            await set_rls_org(session, test_org)
            result = await session.execute(
                text(
                    "SELECT count(*) FROM runs WHERE pipeline_id = :pid "
                    "AND status IN ('pending', 'running', 'awaiting_human', "
                    "'claimed', 'waiting_for_lock')",
                ),
                {"pid": str(pipeline_id)},
            )
            assert result.scalar_one() == 10


# ---------------------------------------------------------------------------
# Trigger-level max_concurrent_runs enforcement
# ---------------------------------------------------------------------------


class TestMaxConcurrentRunsEnforcement:
    """Each test uses its own private trigger + pipeline to avoid state pollution."""

    async def _make_private_trigger(
        self,
        db_engine: AsyncEngine,
        test_org: uuid.UUID,
        test_user: uuid.UUID,
        max_concurrent: int,
    ) -> dict[str, uuid.UUID]:
        """Create a dedicated pipeline, snapshot, and trigger for one test."""
        factory = async_sessionmaker(db_engine, expire_on_commit=False)
        pipeline_id = uuid.uuid4()
        snapshot_id = uuid.uuid4()
        trigger_id = uuid.uuid4()
        async with factory() as session, session.begin():
            await set_rls_org(session, test_org)
            await session.execute(
                text(
                    "INSERT INTO pipelines (id, organisation_id, name, account_id, "
                    "max_concurrent_runs, lock_wait_timeout_seconds, "
                    "node_timeout_seconds, run_context_defaults, graph_nodes_json) "
                    "VALUES (:id, :oid, :name, :uid, 10, 30, 300, '{}'::json, '[]'::json)",
                ),
                {
                    "id": str(pipeline_id),
                    "oid": str(test_org),
                    "name": f"Trigger Test {trigger_id.hex[:8]}",
                    "uid": str(test_user),
                },
            )
            await session.execute(
                text(
                    "INSERT INTO pipeline_snapshots (id, pipeline_id, organisation_id, "
                    "snapshot_version, graph_json, connector_bindings_json, "
                    "schema_pins_json, prompt_pins_json, model_backend_pins_json, "
                    "run_context_defaults, config_json) "
                    "VALUES (:id, :pid, :oid, 1, '{}'::json, '[]'::json, "
                    "'[]'::json, '[]'::json, '[]'::json, '{}'::json, '{}'::json)",
                ),
                {"id": str(snapshot_id), "pid": str(pipeline_id), "oid": str(test_org)},
            )
            await session.execute(
                text(
                    "INSERT INTO triggers (id, organisation_id, pipeline_id, "
                    "trigger_type, active, max_concurrent_runs, config_json, account_id) "
                    "VALUES (:id, :oid, :pid, 'webhook', true, :mcr, '{}'::json, :uid)",
                ),
                {
                    "id": str(trigger_id),
                    "oid": str(test_org),
                    "pid": str(pipeline_id),
                    "mcr": max_concurrent,
                    "uid": str(test_user),
                },
            )
        return {"pipeline_id": pipeline_id, "snapshot_id": snapshot_id, "trigger_id": trigger_id}

    async def _fill_active_runs(
        self,
        db_engine: AsyncEngine,
        test_org: uuid.UUID,
        pipeline_id: uuid.UUID,
        snapshot_id: uuid.UUID,
        trigger_id: uuid.UUID,
        count: int,
        tag: str,
    ) -> None:
        factory = async_sessionmaker(db_engine, expire_on_commit=False)
        async with factory() as session, session.begin():
            await set_rls_org(session, test_org)
            for i in range(count):
                run_id = uuid.uuid4()
                tid_str = f"{test_org}:{tag}-{i}"
                await session.execute(
                    text(
                        "INSERT INTO runs (id, organisation_id, pipeline_id, "
                        "snapshot_id, trigger_type, status, input_hash, "
                        "langgraph_thread_id, trigger_id) "
                        "VALUES (:rid, :oid, :pid, :sid, 'webhook', 'running', "
                        ":hash, :tid, :tid2)",
                    ),
                    {
                        "rid": str(run_id),
                        "oid": str(test_org),
                        "pid": str(pipeline_id),
                        "sid": str(snapshot_id),
                        "hash": hashlib.sha256(f"{tag}-{i}".encode()).hexdigest(),
                        "tid": tid_str,
                        "tid2": str(trigger_id),
                    },
                )

    async def test_trigger_rejects_when_at_limit(
        self,
        db_engine: AsyncEngine,
        test_org: uuid.UUID,
        test_user: uuid.UUID,
    ) -> None:
        ctx = await self._make_private_trigger(db_engine, test_org, test_user, max_concurrent=5)
        await self._fill_active_runs(
            db_engine,
            test_org,
            ctx["pipeline_id"],
            ctx["snapshot_id"],
            ctx["trigger_id"],
            5,
            "reject",
        )

        engine = TriggerEngine()
        factory = async_sessionmaker(db_engine, expire_on_commit=False)
        async with factory() as session, session.begin():
            await set_rls_org(session, test_org)
            with pytest.raises(ConcurrentRunLimitError) as exc_info:
                await engine.handle_webhook(
                    session,
                    trigger_id=ctx["trigger_id"],
                    org_id=test_org,
                    raw_body=b'{"event": "push"}',
                    raw_payload={"event": "push"},
                    hmac_signature=None,
                    modulo_timestamp=str(int(time.time())),
                    snapshot_id=ctx["snapshot_id"],
                )
            assert exc_info.value.limit == 5

    async def test_trigger_accepts_when_below_limit(
        self,
        db_engine: AsyncEngine,
        test_org: uuid.UUID,
        test_user: uuid.UUID,
    ) -> None:
        ctx = await self._make_private_trigger(db_engine, test_org, test_user, max_concurrent=5)

        engine = TriggerEngine()
        factory = async_sessionmaker(db_engine, expire_on_commit=False)
        async with factory() as session, session.begin():
            await set_rls_org(session, test_org)
            run, event, _ = await engine.handle_webhook(
                session,
                trigger_id=ctx["trigger_id"],
                org_id=test_org,
                raw_body=b'{"event": "accepted-test"}',
                raw_payload={"event": "accepted-test"},
                hmac_signature=None,
                modulo_timestamp=str(int(time.time())),
                snapshot_id=ctx["snapshot_id"],
            )
            assert run is not None
            assert run.status == "pending"
            assert event.validation_result == "accepted"

    async def test_trigger_rejects_when_limit_configured_lower(
        self,
        db_engine: AsyncEngine,
        test_org: uuid.UUID,
        test_user: uuid.UUID,
    ) -> None:
        ctx = await self._make_private_trigger(db_engine, test_org, test_user, max_concurrent=3)
        await self._fill_active_runs(
            db_engine,
            test_org,
            ctx["pipeline_id"],
            ctx["snapshot_id"],
            ctx["trigger_id"],
            3,
            "lower",
        )

        engine = TriggerEngine()
        factory = async_sessionmaker(db_engine, expire_on_commit=False)
        async with factory() as session, session.begin():
            await set_rls_org(session, test_org)
            with pytest.raises(ConcurrentRunLimitError) as exc_info:
                await engine.handle_webhook(
                    session,
                    trigger_id=ctx["trigger_id"],
                    org_id=test_org,
                    raw_body=b'{"event": "over-limit"}',
                    raw_payload={"event": "over-limit"},
                    hmac_signature=None,
                    modulo_timestamp=str(int(time.time())),
                    snapshot_id=ctx["snapshot_id"],
                )
            assert exc_info.value.limit == 3
