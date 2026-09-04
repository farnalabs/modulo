"""Integration tests: the FAR-583 run-outputs dual-write chokepoints on real
Postgres.

Drives ``update_run_status`` (ORM branch + fenced branch) and the core
catch/orchestrate contract (:func:`guard_dual_write`) through a NOBYPASSRLS
role (the production ``modulo_app`` scenario), so the RLS-scoped dual-write,
the fail-closed abort, the transient retry, and the separate-session
terminalization are exercised against the real migration-0176 table.

Each test gets its OWN organisation (and pipeline/snapshot) rather than the
shared session-scoped ``test_org``: ``runs`` carries
``UNIQUE(organisation_id, run_number)`` and the suite runs with ``-n 2`` in
the pre-deploy gate, so a dedicated org makes ``run_number = 1`` trivially
collision-free (xdist-safe).

The orchestration's terminalize + error-event legs use the standalone
``saq_hooks`` engine (``settings.database_url`` — pointed at the testcontainer
by the session conftest); the Redis counter bumps are best-effort and are
swallowed when no Redis is reachable, exactly as in production.
"""

from __future__ import annotations

import itertools
import json
import uuid
from dataclasses import dataclass
from typing import Any
from unittest.mock import patch

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.exc import OperationalError
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from modulo.core.run_outputs_dualwrite import DUAL_WRITE_ENABLED_KEY, guard_dual_write
from modulo.core.runtime_config.store import get_runtime_config_store
from modulo.db.crud.run import update_run_status
from modulo.db.crud.run_node_outputs import DualWriteError, read_run_blobs_with_fallback
from modulo.db.rls import set_rls_org

pytestmark = pytest.mark.integration


def patch_replace(replacement: Any) -> Any:
    """Patch the chokepoint's REPLACE helper at the crud.run module boundary."""
    return patch("modulo.db.crud.run.replace_run_node_outputs", replacement)


@dataclass(frozen=True)
class _Tenant:
    """An isolated org plus the pipeline/snapshot a run needs to FK against."""

    org_id: uuid.UUID
    pipeline_id: uuid.UUID
    snapshot_id: uuid.UUID


@pytest_asyncio.fixture
async def dual_write_tenant(db_engine: AsyncEngine, test_user: uuid.UUID) -> _Tenant:
    """Commit a dedicated organisation + pipeline + snapshot for one test."""
    tenant = _Tenant(uuid.uuid4(), uuid.uuid4(), uuid.uuid4())
    async with db_engine.connect() as conn, conn.begin():
        await conn.execute(
            text("INSERT INTO organisations (id, name, slug, settings_json) VALUES (:id, :name, :slug, '{}'::json)"),
            {
                "id": str(tenant.org_id),
                "name": f"Dual Write Org {tenant.org_id.hex[:8]}",
                "slug": f"dwo-{tenant.org_id.hex[:12]}",
            },
        )
        await conn.execute(
            text(
                "INSERT INTO pipelines (id, organisation_id, name, account_id, "
                "max_concurrent_runs, lock_wait_timeout_seconds, node_timeout_seconds, "
                "run_context_defaults, graph_nodes_json) "
                "VALUES (:id, :oid, :name, :uid, 10, 30, 300, '{}'::json, '[]'::json)"
            ),
            {
                "id": str(tenant.pipeline_id),
                "oid": str(tenant.org_id),
                "name": "Dual Write Pipeline",
                "uid": str(test_user),
            },
        )
        await conn.execute(
            text(
                "INSERT INTO pipeline_snapshots (id, pipeline_id, organisation_id, "
                "snapshot_version, graph_json, connector_bindings_json, "
                "schema_pins_json, prompt_pins_json, model_backend_pins_json, "
                "run_context_defaults, config_json) "
                "VALUES (:id, :pid, :oid, 1, '{}'::json, '[]'::json, "
                "'[]'::json, '[]'::json, '[]'::json, '{}'::json, '{}'::json)"
            ),
            {"id": str(tenant.snapshot_id), "pid": str(tenant.pipeline_id), "oid": str(tenant.org_id)},
        )
    return tenant


@pytest_asyncio.fixture
async def rls_app_session(app_engine: AsyncEngine) -> AsyncSession:
    """Session whose connections run as a NOBYPASSRLS role, so RLS applies."""
    factory = async_sessionmaker(app_engine, expire_on_commit=False)
    session = factory()
    try:
        yield session
    finally:
        await session.close()


async def _insert_run(
    db_engine: AsyncEngine,
    tenant: _Tenant,
    *,
    status: str = "running",
    claim_token: str | None = None,
) -> uuid.UUID:
    """Commit a run in *tenant* over a superuser connection.

    ``run_number`` is 1 because the org is dedicated to this test, so the
    ``UNIQUE(organisation_id, run_number)`` constraint cannot collide.
    """
    run_id = uuid.uuid4()
    # runs.claim_token is NOT NULL (gen_random_uuid() server default in
    # production); non-fenced tests bind a benign explicit seed value.
    tok_value: Any = claim_token or f"tok-seed-{run_id}"
    async with db_engine.connect() as conn, conn.begin():
        await conn.execute(
            text(
                "INSERT INTO runs (id, organisation_id, pipeline_id, snapshot_id, "
                "trigger_type, status, run_number, input_hash, langgraph_thread_id, claim_token) "
                "VALUES (:id, :oid, :pid, :sid, 'manual', :status, 1, :hash, :thread, :tok)"
            ),
            {
                "id": str(run_id),
                "oid": str(tenant.org_id),
                "pid": str(tenant.pipeline_id),
                "sid": str(tenant.snapshot_id),
                "status": status,
                "hash": "d" * 64,
                "thread": f"dual-write-{run_id}",
                "tok": tok_value,
            },
        )
    return run_id


async def _fetch_run_row(db_engine: AsyncEngine, run_id: uuid.UUID) -> dict[str, Any]:
    """Read the run row's status/blob columns over a superuser connection."""
    async with db_engine.connect() as conn:
        row = (
            await conn.execute(
                text(
                    "SELECT status, error_code, outputs_json, node_telemetry_json, completed_at "
                    "FROM runs WHERE id = :rid"
                ),
                {"rid": str(run_id)},
            )
        ).fetchone()
    assert row is not None
    return {
        "status": row[0],
        "error_code": row[1],
        "outputs_json": row[2],
        "node_telemetry_json": row[3],
        "completed_at": row[4],
    }


async def _fetch_new_table_rows(db_engine: AsyncEngine, run_id: uuid.UUID) -> list[tuple[str, str, Any, Any, Any]]:
    """Read run_node_outputs rows over a superuser connection (RLS bypassed)."""
    async with db_engine.connect() as conn:
        result = await conn.execute(
            text(
                "SELECT node_id, attempt_key, outputs_json, node_telemetry_json, raw_output_markers "
                "FROM run_node_outputs WHERE run_id = :rid ORDER BY node_id, attempt_key"
            ),
            {"rid": str(run_id)},
        )
        return [(r[0], r[1], r[2], r[3], r[4]) for r in result.all()]


async def _fetch_error_events(db_engine: AsyncEngine, org_id: uuid.UUID, pattern: str) -> list[tuple[str, str]]:
    async with db_engine.connect() as conn:
        result = await conn.execute(
            text(
                "SELECT source, message FROM error_events "
                "WHERE organisation_id = :oid AND message LIKE :pattern ORDER BY created_at"
            ),
            {"oid": str(org_id), "pattern": pattern},
        )
        return [(r[0], r[1]) for r in result.all()]


async def _read_blobs(rls_session: AsyncSession, tenant: _Tenant, run_id: uuid.UUID) -> Any:
    async with rls_session.begin():
        await set_rls_org(rls_session, tenant.org_id)
        return await read_run_blobs_with_fallback(rls_session, run_id=run_id, organisation_id=tenant.org_id)


def _hard_failure() -> OperationalError:
    """A non-retryable SQL failure shaped like a real RLS rejection (42501)."""
    err = OperationalError("stmt", {}, Exception("permission denied"))
    err.orig = type("_FakePG", (Exception,), {"sqlstate": "42501"})("42501 permission denied")
    return err


def _retryable_failure() -> OperationalError:
    err = OperationalError("stmt", {}, Exception("could not serialize"))
    err.orig = type("_FakePG", (Exception,), {"sqlstate": "40001"})("40001 could not serialize")
    return err


# ---------------------------------------------------------------------------
# Success + REPLACE semantics
# ---------------------------------------------------------------------------


async def test_success_path_populates_both_stores_byte_identical(
    db_engine: AsyncEngine,
    rls_app_session: AsyncSession,
    dual_write_tenant: _Tenant,
) -> None:
    run_id = await _insert_run(db_engine, dual_write_tenant)
    outputs: dict[str, Any] = {"n1": {"answer": 42, "nested": {"b": 2}}}
    telemetry: dict[str, Any] = {"n1": {"agent_status": "completed", "wall_clock_time_ms": 900}}

    async with rls_app_session.begin():
        await set_rls_org(rls_app_session, dual_write_tenant.org_id)
        updated = await update_run_status(
            rls_app_session,
            run_id,
            "complete",
            outputs_json=outputs,
            node_telemetry_json=telemetry,
        )
    assert updated is not None
    assert updated.status == "complete"

    legacy = await _fetch_run_row(db_engine, run_id)
    assert legacy["status"] == "complete"
    rows = await _fetch_new_table_rows(db_engine, run_id)
    final_rows = [r for r in rows if r[1] == "__final__"]
    assert len(final_rows) == 1
    assert final_rows[0][0] == "n1"

    blobs = await _read_blobs(rls_app_session, dual_write_tenant, run_id)
    # Byte-identical reassembly: the serialized bytes of the reassembled dicts
    # equal the legacy column's serialized bytes.
    assert json.dumps(blobs.outputs, default=str) == json.dumps(legacy["outputs_json"], default=str)
    assert json.dumps(blobs.telemetry, default=str) == json.dumps(legacy["node_telemetry_json"], default=str)
    assert blobs.outputs == outputs
    assert blobs.telemetry == telemetry


async def test_shrinking_replace_through_orm_branch(
    db_engine: AsyncEngine,
    rls_app_session: AsyncSession,
    dual_write_tenant: _Tenant,
) -> None:
    run_id = await _insert_run(db_engine, dual_write_tenant)
    async with rls_app_session.begin():
        await set_rls_org(rls_app_session, dual_write_tenant.org_id)
        await update_run_status(
            rls_app_session,
            run_id,
            "complete",
            outputs_json={"a": 1, "b": 2},
            node_telemetry_json={"a": {"t": 1}, "b": {"t": 2}},
        )

    async with rls_app_session.begin():
        await set_rls_org(rls_app_session, dual_write_tenant.org_id)
        await update_run_status(
            rls_app_session,
            run_id,
            "complete",
            outputs_json={"a": 1},
            node_telemetry_json={"a": {"t": 1}},
        )

    rows = await _fetch_new_table_rows(db_engine, run_id)
    node_ids = sorted(r[0] for r in rows if r[1] == "__final__")
    assert node_ids == ["a"]
    legacy = await _fetch_run_row(db_engine, run_id)
    assert legacy["outputs_json"] == {"a": 1}
    assert legacy["node_telemetry_json"] == {"a": {"t": 1}}


async def test_shrinking_replace_through_fenced_branch(
    db_engine: AsyncEngine,
    rls_app_session: AsyncSession,
    dual_write_tenant: _Tenant,
) -> None:
    run_id = await _insert_run(db_engine, dual_write_tenant, claim_token="tok-1")
    async with rls_app_session.begin():
        await set_rls_org(rls_app_session, dual_write_tenant.org_id)
        await update_run_status(
            rls_app_session,
            run_id,
            "complete",
            outputs_json={"a": 1, "b": 2},
            claim_token="tok-1",
        )

    async with rls_app_session.begin():
        await set_rls_org(rls_app_session, dual_write_tenant.org_id)
        await update_run_status(
            rls_app_session,
            run_id,
            "complete",
            outputs_json={"a": 1},
            claim_token="tok-1",
        )

    rows = await _fetch_new_table_rows(db_engine, run_id)
    node_ids = sorted(r[0] for r in rows if r[1] == "__final__")
    assert node_ids == ["a"]
    legacy = await _fetch_run_row(db_engine, run_id)
    assert legacy["outputs_json"] == {"a": 1}


async def test_fenced_rowcount_zero_writes_no_new_table_rows(
    db_engine: AsyncEngine,
    rls_app_session: AsyncSession,
    dual_write_tenant: _Tenant,
) -> None:
    run_id = await _insert_run(db_engine, dual_write_tenant, claim_token="tok-a")
    async with rls_app_session.begin():
        await set_rls_org(rls_app_session, dual_write_tenant.org_id)
        result = await update_run_status(
            rls_app_session,
            run_id,
            "complete",
            outputs_json={"n1": {"a": 1}},
            claim_token="tok-stale",
        )
    assert result is None
    assert not await _fetch_new_table_rows(db_engine, run_id)
    legacy = await _fetch_run_row(db_engine, run_id)
    assert legacy["status"] == "running"
    assert legacy["outputs_json"] is None


# ---------------------------------------------------------------------------
# Fail-closed abort + orchestration
# ---------------------------------------------------------------------------


async def test_fail_closed_abort_legacy_row_survives_and_orchestration_terminalizes(
    db_engine: AsyncEngine,
    rls_app_session: AsyncSession,
    dual_write_tenant: _Tenant,
) -> None:
    run_id = await _insert_run(db_engine, dual_write_tenant)

    async def _failing_replace(*args: Any, **kwargs: Any) -> None:
        raise _hard_failure()

    async def _injected_abort() -> None:
        async with rls_app_session.begin():
            await set_rls_org(rls_app_session, dual_write_tenant.org_id)
            async with guard_dual_write(rls_app_session):
                await update_run_status(
                    rls_app_session,
                    run_id,
                    "complete",
                    outputs_json={"n1": {"a": 1}},
                    node_telemetry_json={"n1": {"t": 1}},
                )

    with patch_replace(_failing_replace), pytest.raises(DualWriteError):
        await _injected_abort()

    # Legacy row survives COMMITTED — not half-written: the failed write's
    # status/blobs are gone, the pre-write state is intact.
    legacy = await _fetch_run_row(db_engine, run_id)
    assert legacy["status"] == "failed"
    assert legacy["error_code"] == "dual_write_failed"
    assert legacy["outputs_json"] is None
    assert legacy["node_telemetry_json"] is None
    assert legacy["completed_at"] is not None
    # NO orphan new-table row.
    assert not await _fetch_new_table_rows(db_engine, run_id)
    # The orchestration's aggregated error event landed (source='backend').
    events = await _fetch_error_events(db_engine, dual_write_tenant.org_id, "%dual-write failed%")
    assert len(events) == 1
    assert events[0][0] == "backend"

    # A subsequent write for the same run succeeds (and dual-writes).
    async with rls_app_session.begin():
        await set_rls_org(rls_app_session, dual_write_tenant.org_id)
        updated = await update_run_status(
            rls_app_session,
            run_id,
            "complete",
            outputs_json={"n1": {"a": 1}},
        )
    assert updated is not None
    rows = await _fetch_new_table_rows(db_engine, run_id)
    final_node_ids = [r[0] for r in rows if r[1] == "__final__"]
    assert final_node_ids == ["n1"]


async def test_fenced_abort_claim_token_fence_honored(
    db_engine: AsyncEngine,
    rls_app_session: AsyncSession,
    dual_write_tenant: _Tenant,
) -> None:
    run_id = await _insert_run(db_engine, dual_write_tenant, claim_token="tok-keep")

    async def _failing_replace(*args: Any, **kwargs: Any) -> None:
        raise _hard_failure()

    async def _injected_fenced_abort() -> None:
        async with rls_app_session.begin():
            await set_rls_org(rls_app_session, dual_write_tenant.org_id)
            async with guard_dual_write(rls_app_session):
                await update_run_status(
                    rls_app_session,
                    run_id,
                    "complete",
                    outputs_json={"n1": {"a": 1}},
                    claim_token="tok-keep",
                )

    with patch_replace(_failing_replace), pytest.raises(DualWriteError):
        await _injected_fenced_abort()

    legacy = await _fetch_run_row(db_engine, run_id)
    assert legacy["status"] == "failed"
    assert legacy["error_code"] == "dual_write_failed"
    assert legacy["outputs_json"] is None


async def test_unknown_run_is_never_transitioned_by_the_orchestration(
    db_engine: AsyncEngine,
    rls_app_session: AsyncSession,
    dual_write_tenant: _Tenant,
) -> None:
    run_id = await _insert_run(db_engine, dual_write_tenant, status="unknown")

    async def _failing_replace(*args: Any, **kwargs: Any) -> None:
        raise _hard_failure()

    async def _injected_unknown_abort() -> None:
        async with rls_app_session.begin():
            await set_rls_org(rls_app_session, dual_write_tenant.org_id)
            async with guard_dual_write(rls_app_session):
                await update_run_status(
                    rls_app_session,
                    run_id,
                    "complete",
                    outputs_json={"n1": {"a": 1}},
                )

    with patch_replace(_failing_replace), pytest.raises(DualWriteError):
        await _injected_unknown_abort()

    legacy = await _fetch_run_row(db_engine, run_id)
    assert legacy["status"] == "unknown"
    assert legacy["outputs_json"] is None
    # The event still fires (evidence-only for an unknown run).
    events = await _fetch_error_events(db_engine, dual_write_tenant.org_id, "%dual-write failed%")
    assert len(events) == 1


async def test_transient_retry_succeeds_without_failure_event(
    db_engine: AsyncEngine,
    rls_app_session: AsyncSession,
    dual_write_tenant: _Tenant,
) -> None:
    run_id = await _insert_run(db_engine, dual_write_tenant)
    from modulo.db.crud import run as run_crud

    real_replace = run_crud.replace_run_node_outputs
    calls: list[int] = []

    async def _flaky_replace(*args: Any, **kwargs: Any) -> None:
        calls.append(1)
        if len(calls) == 1:
            raise _retryable_failure()
        await real_replace(*args, **kwargs)

    with patch_replace(_flaky_replace):
        async with rls_app_session.begin():
            await set_rls_org(rls_app_session, dual_write_tenant.org_id)
            updated = await update_run_status(
                rls_app_session,
                run_id,
                "complete",
                outputs_json={"n1": {"a": 1}},
            )
    assert updated is not None
    assert len(calls) == 2
    rows = await _fetch_new_table_rows(db_engine, run_id)
    final_node_ids = [r[0] for r in rows if r[1] == "__final__"]
    assert final_node_ids == ["n1"]
    # No failure event for a successful retry.
    assert not await _fetch_error_events(db_engine, dual_write_tenant.org_id, "%dual-write failed%")


# ---------------------------------------------------------------------------
# Kill-switch OFF
# ---------------------------------------------------------------------------


async def test_kill_switch_off_legacy_only_write_with_degraded_event(
    db_engine: AsyncEngine,
    rls_app_session: AsyncSession,
    dual_write_tenant: _Tenant,
) -> None:
    run_id = await _insert_run(db_engine, dual_write_tenant)
    store = get_runtime_config_store()
    store.set_override(DUAL_WRITE_ENABLED_KEY, "false")
    try:
        async with rls_app_session.begin():
            await set_rls_org(rls_app_session, dual_write_tenant.org_id)
            await update_run_status(
                rls_app_session,
                run_id,
                "complete",
                outputs_json={"n1": {"a": 1}},
            )
    finally:
        store.clear_override(DUAL_WRITE_ENABLED_KEY)

    legacy = await _fetch_run_row(db_engine, run_id)
    assert legacy["status"] == "complete"
    assert legacy["outputs_json"] == {"n1": {"a": 1}}
    # Zero new-table rows — the legacy-only write is a true emergency valve.
    assert not await _fetch_new_table_rows(db_engine, run_id)


async def test_kill_switch_off_edge_triggered_degraded_event(
    db_engine: AsyncEngine,
    rls_app_session: AsyncSession,
    dual_write_tenant: _Tenant,
) -> None:
    """The edge gate fires the degraded event exactly once per window."""
    from modulo.core import run_outputs_dualwrite as module

    run_id = await _insert_run(db_engine, dual_write_tenant)
    store = get_runtime_config_store()
    store.set_override(DUAL_WRITE_ENABLED_KEY, "false")
    edge_counter = itertools.count()
    edge_values = itertools.cycle([True, False, False])

    async def _edge(key: str, ttl: int) -> bool | None:
        next(edge_counter)
        return next(edge_values)

    try:
        with patch.object(module, "_redis_set_nx", _edge):
            for _ in range(3):
                async with rls_app_session.begin():
                    await set_rls_org(rls_app_session, dual_write_tenant.org_id)
                    await update_run_status(
                        rls_app_session,
                        run_id,
                        "complete",
                        outputs_json={"n1": {"a": 1}},
                    )
    finally:
        store.clear_override(DUAL_WRITE_ENABLED_KEY)

    events = await _fetch_error_events(db_engine, dual_write_tenant.org_id, "%dual-write disabled%")
    assert len(events) == 1
    assert events[0][0] == "backend"
