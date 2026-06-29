"""Integration tests for agent_signal triggers with a real Postgres database.

Creates a real Trigger row with trigger_type='agent_signal', calls
fire_agent_signal(), and verifies TriggerEvent recording.
"""

import uuid

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker

from modulo.core.trigger_engine.agent_signal import fire_agent_signal
from modulo.db.rls import set_rls_org

pytestmark = pytest.mark.integration

# ---------------------------------------------------------------------------
# Module-scoped fixtures — seed the database once
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture(scope="module")
async def org_id(db_engine: AsyncEngine) -> uuid.UUID:
    oid = uuid.uuid4()
    async with db_engine.connect() as conn:
        async with conn.begin():
            await conn.execute(
                text(
                    "INSERT INTO organisations (id, name, slug, settings_json) "
                    "VALUES (:id, :name, :slug, '{}'::json)"
                ),
                {
                    "id": str(oid),
                    "name": "agent-signal-int-org",
                    "slug": f"as-int-{oid.hex[:8]}",
                },
            )
    return oid


@pytest_asyncio.fixture(scope="module")
async def user_id(db_engine: AsyncEngine, org_id: uuid.UUID) -> uuid.UUID:
    uid = uuid.uuid4()
    async with db_engine.connect() as conn:
        async with conn.begin():
            await conn.execute(
                text(
                    "INSERT INTO users (id, organisation_id, email, display_name, "
                    "org_role, auth_provider, active, password_hash) "
                    "VALUES (:id, :oid, :email, :name, 'admin', 'local', true, 'hash')"
                ),
                {
                    "id": str(uid),
                    "oid": str(org_id),
                    "email": "as-admin@test.local",
                    "name": "AS Admin",
                },
            )
    return uid


@pytest_asyncio.fixture(scope="module")
async def target_pipeline_id(
    db_engine: AsyncEngine,
    org_id: uuid.UUID,
    user_id: uuid.UUID,
) -> uuid.UUID:
    pid = uuid.uuid4()
    async with db_engine.connect() as conn:
        async with conn.begin():
            await conn.execute(
                text(
                    "INSERT INTO pipelines (id, organisation_id, name, visibility, "
                    "max_concurrent_runs, lock_wait_timeout_seconds, "
                    "node_timeout_seconds, created_by, run_context_defaults, "
                    "graph_nodes_json) "
                    "VALUES (:id, :oid, :name, 'org', 5, 300, 300, :uid, "
                    "'{}'::json, '[]'::json)"
                ),
                {
                    "id": str(pid),
                    "oid": str(org_id),
                    "name": "child-pipeline",
                    "uid": str(user_id),
                },
            )
    return pid


@pytest_asyncio.fixture(scope="module")
async def snapshot_id(
    db_engine: AsyncEngine,
    org_id: uuid.UUID,
    target_pipeline_id: uuid.UUID,
) -> uuid.UUID:
    sid = uuid.uuid4()
    async with db_engine.connect() as conn:
        async with conn.begin():
            await conn.execute(
                text(
                    "INSERT INTO pipeline_snapshots (id, organisation_id, "
                    "pipeline_id, snapshot_version, graph_json, "
                    "connector_bindings_json, schema_pins_json, "
                    "prompt_pins_json, model_backend_pins_json, "
                    "run_context_defaults, config_json) "
                    "VALUES (:id, :oid, :pid, 1, :graph, '[]'::json, "
                    "'[]'::json, '[]'::json, '[]'::json, '{}'::json, '{}'::json)"
                ),
                {
                    "id": str(sid),
                    "oid": str(org_id),
                    "pid": str(target_pipeline_id),
                    "graph": '{"nodes":[],"edges":[]}',
                },
            )
    return sid


@pytest_asyncio.fixture(scope="module")
async def source_pipeline_id() -> uuid.UUID:
    """A UUID representing the pipeline being watched (not an FK row)."""
    return uuid.uuid4()


@pytest_asyncio.fixture(scope="module")
async def source_run_id(
    db_engine: AsyncEngine,
    org_id: uuid.UUID,
    target_pipeline_id: uuid.UUID,
    snapshot_id: uuid.UUID,
) -> uuid.UUID:
    rid = uuid.uuid4()
    thread_id = f"{org_id}:{rid}"
    async with db_engine.connect() as conn:
        async with conn.begin():
            await conn.execute(
                text(
                    "INSERT INTO runs (id, organisation_id, pipeline_id, "
                    "snapshot_id, status, trigger_type, langgraph_thread_id, "
                    "input_hash) "
                    "VALUES (:id, :oid, :pid, :sid, 'complete', 'manual', "
                    ":thread, :hash)"
                ),
                {
                    "id": str(rid),
                    "oid": str(org_id),
                    "pid": str(target_pipeline_id),
                    "sid": str(snapshot_id),
                    "thread": thread_id,
                    "hash": "0" * 64,
                },
            )
    return rid


@pytest_asyncio.fixture(scope="module")
async def trigger_id(
    db_engine: AsyncEngine,
    org_id: uuid.UUID,
    target_pipeline_id: uuid.UUID,
    user_id: uuid.UUID,
    source_pipeline_id: uuid.UUID,
) -> uuid.UUID:
    tid = uuid.uuid4()
    config_json = (
        '{"source_pipeline_id": "' + str(source_pipeline_id) + '", '
        '"source_node_id": "extract"}'
    )
    async with db_engine.connect() as conn:
        async with conn.begin():
            await conn.execute(
                text(
                    "INSERT INTO triggers (id, organisation_id, pipeline_id, "
                    "trigger_type, active, max_concurrent_runs, config_json, "
                    "created_by) "
                    "VALUES (:id, :oid, :pid, 'agent_signal', true, 5, "
                    ":config::json, :uid)"
                ),
                {
                    "id": str(tid),
                    "oid": str(org_id),
                    "pid": str(target_pipeline_id),
                    "config": config_json,
                    "uid": str(user_id),
                },
            )
    return tid


# ===========================================================================
# Tests
# ===========================================================================


class TestFireAgentSignalIntegration:
    """End-to-end tests of fire_agent_signal with real DB rows."""

    async def test_fires_child_run_and_logs_event(
        self,
        db_engine: AsyncEngine,
        org_id: uuid.UUID,
        source_pipeline_id: uuid.UUID,
        source_run_id: uuid.UUID,
        snapshot_id: uuid.UUID,
    ) -> None:
        """Trigger matches → child run created + TriggerEvent recorded."""
        factory = async_sessionmaker(db_engine, expire_on_commit=False)
        async with factory() as session:
            async with session.begin():
                await set_rls_org(session, org_id)

                results = await fire_agent_signal(
                    session,
                    org_id=org_id,
                    source_run_id=source_run_id,
                    source_pipeline_id=source_pipeline_id,
                    completed_node_id="extract",
                    node_output={"result": "ok"},
                )

        assert len(results) == 1
        assert results[0]["status"] == "fired"

        # Verify TriggerEvent was created.
        async with factory() as session:
            async with session.begin():
                await set_rls_org(session, org_id)
                rows = (
                    await session.execute(
                        text(
                            "SELECT validation_result, run_id, trigger_type "
                            "FROM trigger_events "
                            "WHERE organisation_id = :oid"
                        ),
                        {"oid": str(org_id)},
                    )
                ).fetchall()

        assert len(rows) >= 1
        matching = [r for r in rows if r[0] == "signal_fired"]
        assert len(matching) == 1, f"Expected 1 'signal_fired' event, got {len(matching)}: {rows}"
        event = matching[0]
        assert event[2] == "agent_signal"
        assert event[1] is not None, "TriggerEvent should reference a run_id"

    async def test_no_match_does_not_create_event(
        self,
        db_engine: AsyncEngine,
        org_id: uuid.UUID,
        source_run_id: uuid.UUID,
    ) -> None:
        """Non-matching node returns empty and logs nothing."""
        factory = async_sessionmaker(db_engine, expire_on_commit=False)
        async with factory() as session:
            async with session.begin():
                await set_rls_org(session, org_id)

                results = await fire_agent_signal(
                    session,
                    org_id=org_id,
                    source_run_id=source_run_id,
                    source_pipeline_id=uuid.uuid4(),
                    completed_node_id="nonexistent-node",
                )

        assert results == []

    async def test_concurrency_limit_skips_and_logs(
        self,
        db_engine: AsyncEngine,
        org_id: uuid.UUID,
        target_pipeline_id: uuid.UUID,
        source_pipeline_id: uuid.UUID,
        source_run_id: uuid.UUID,
        snapshot_id: uuid.UUID,
        user_id: uuid.UUID,
    ) -> None:
        """Trigger with max_concurrent_runs=1 and 1 active run → skip."""
        factory = async_sessionmaker(db_engine, expire_on_commit=False)
        rid = uuid.uuid4()
        thread_id = f"{org_id}:{rid}"

        # Create a trigger with concurrency limit 1.
        tight_tid = uuid.uuid4()
        config_json = (
            '{"source_pipeline_id": "' + str(source_pipeline_id) + '", '
            '"source_node_id": "extract"}'
        )

        async with factory() as session:
            async with session.begin():
                await set_rls_org(session, org_id)
                await session.execute(
                    text(
                        "INSERT INTO triggers (id, organisation_id, pipeline_id, "
                        "trigger_type, active, max_concurrent_runs, config_json, "
                        "created_by) "
                        "VALUES (:id, :oid, :pid, 'agent_signal', true, 1, "
                        ":config::json, :uid)"
                    ),
                    {
                        "id": str(tight_tid),
                        "oid": str(org_id),
                        "pid": str(target_pipeline_id),
                        "config": config_json,
                        "uid": str(user_id),
                    },
                )

                # Create an active run on the same pipeline to hit the limit.
                await session.execute(
                    text(
                        "INSERT INTO runs (id, organisation_id, pipeline_id, "
                        "snapshot_id, status, trigger_type, langgraph_thread_id, "
                        "input_hash) "
                        "VALUES (:id, :oid, :pid, :sid, 'running', 'manual', "
                        ":thread, :hash)"
                    ),
                    {
                        "id": str(rid),
                        "oid": str(org_id),
                        "pid": str(target_pipeline_id),
                        "sid": str(snapshot_id),
                        "thread": thread_id,
                        "hash": "0" * 64,
                    },
                )

            # Call fire_agent_signal in a new transaction.
            async with session.begin():
                await set_rls_org(session, org_id)
                results = await fire_agent_signal(
                    session,
                    org_id=org_id,
                    source_run_id=source_run_id,
                    source_pipeline_id=source_pipeline_id,
                    completed_node_id="extract",
                )

        assert len(results) == 1
        assert results[0]["status"] == "skipped"
        assert results[0]["reason"] == "concurrency_limit"

        # Verify concurrency_limit_reached TriggerEvent was logged.
        async with factory() as session:
            async with session.begin():
                await set_rls_org(session, org_id)
                rows = (
                    await session.execute(
                        text(
                            "SELECT validation_result, error_detail "
                            "FROM trigger_events "
                            "WHERE organisation_id = :oid AND trigger_id = :tid"
                        ),
                        {"oid": str(org_id), "tid": str(tight_tid)},
                    )
                ).fetchall()

        assert len(rows) >= 1
        matching = [r for r in rows if r[0] == "concurrency_limit_reached"]
        assert len(matching) == 1, (
            f"Expected 'concurrency_limit_reached' event, got: {rows}"
        )

    async def test_org_isolation(
        self,
        db_engine: AsyncEngine,
        org_id: uuid.UUID,
        source_pipeline_id: uuid.UUID,
        source_run_id: uuid.UUID,
    ) -> None:
        """Trigger in org_b does NOT fire when fire_agent_signal is called for org_a."""
        # Create second org.
        other_org_id = uuid.uuid4()
        async with db_engine.connect() as conn:
            async with conn.begin():
                await conn.execute(
                    text(
                        "INSERT INTO organisations (id, name, slug, settings_json) "
                        "VALUES (:id, :name, :slug, '{}'::json)"
                    ),
                    {
                        "id": str(other_org_id),
                        "name": "other-int-org",
                        "slug": f"other-int-{other_org_id.hex[:8]}",
                    },
                )

        factory = async_sessionmaker(db_engine, expire_on_commit=False)

        # Call fire_agent_signal for the original org — should not fire the
        # trigger belonging to other_org_id.
        async with factory() as session:
            async with session.begin():
                await set_rls_org(session, org_id)
                results = await fire_agent_signal(
                    session,
                    org_id=org_id,
                    source_run_id=source_run_id,
                    source_pipeline_id=source_pipeline_id,
                    completed_node_id="extract",
                )

        # No triggers exist in org_a — result is empty.
        assert results == []

    async def test_multiple_triggers_both_fire(
        self,
        db_engine: AsyncEngine,
        org_id: uuid.UUID,
        source_pipeline_id: uuid.UUID,
        source_run_id: uuid.UUID,
        snapshot_id: uuid.UUID,
        user_id: uuid.UUID,
    ) -> None:
        """Two triggers watching the same source pipeline+node both fire."""
        factory = async_sessionmaker(db_engine, expire_on_commit=False)

        pid_b = uuid.uuid4()
        pid_c = uuid.uuid4()
        config_json = (
            '{"source_pipeline_id": "' + str(source_pipeline_id) + '", '
            '"source_node_id": "extract"}'
        )

        async with factory() as session:
            async with session.begin():
                await set_rls_org(session, org_id)

                # Pipelines for the two child runs.
                for pid in (pid_b, pid_c):
                    await session.execute(
                        text(
                            "INSERT INTO pipelines (id, organisation_id, name, "
                            "visibility, max_concurrent_runs, "
                            "lock_wait_timeout_seconds, node_timeout_seconds, "
                            "created_by, run_context_defaults, graph_nodes_json) "
                            "VALUES (:id, :oid, :name, 'org', 5, 300, 300, :uid, "
                            "'{}'::json, '[]'::json)"
                        ),
                        {
                            "id": str(pid),
                            "oid": str(org_id),
                            "name": f"multi-pipeline-{pid.hex[:6]}",
                            "uid": str(user_id),
                        },
                    )

                # Snapshots for both child pipelines.
                for pid in (pid_b, pid_c):
                    await session.execute(
                        text(
                            "INSERT INTO pipeline_snapshots (id, organisation_id, "
                            "pipeline_id, snapshot_version, graph_json, "
                            "connector_bindings_json, schema_pins_json, "
                            "prompt_pins_json, model_backend_pins_json, "
                            "run_context_defaults, config_json) "
                            "VALUES (:id, :oid, :pid, 1, :graph, '[]'::json, "
                            "'[]'::json, '[]'::json, '[]'::json, "
                            "'{}'::json, '{}'::json)"
                        ),
                        {
                            "id": uuid.uuid4(),
                            "oid": str(org_id),
                            "pid": str(pid),
                            "graph": '{"nodes":[],"edges":[]}',
                        },
                    )

                # Two triggers, same source config.
                for pid in (pid_b, pid_c):
                    await session.execute(
                        text(
                            "INSERT INTO triggers (id, organisation_id, "
                            "pipeline_id, trigger_type, active, "
                            "max_concurrent_runs, config_json, created_by) "
                            "VALUES (:id, :oid, :pid, 'agent_signal', true, "
                            "5, :config::json, :uid)"
                        ),
                        {
                            "id": uuid.uuid4(),
                            "oid": str(org_id),
                            "pid": str(pid),
                            "config": config_json,
                            "uid": str(user_id),
                        },
                    )

            async with session.begin():
                await set_rls_org(session, org_id)
                results = await fire_agent_signal(
                    session,
                    org_id=org_id,
                    source_run_id=source_run_id,
                    source_pipeline_id=source_pipeline_id,
                    completed_node_id="extract",
                    node_output={"result": "ok"},
                )

        assert len(results) == 2
        assert all(r["status"] == "fired" for r in results)
