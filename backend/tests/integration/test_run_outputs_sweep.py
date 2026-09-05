"""Integration tests: the FAR-583 run-outputs catch-up sweep.

Drives the sweep leg wired into ``dispatcher_reconcile``'s per-org loop
(``cron_helpers._run_outputs_sweep_for_org`` — its own session/transaction,
per-org failure isolation) and the repo backfill helper behind it, against
the real migration-0176 table on testcontainers Postgres.

Covers (design §CATCH-UP SWEEP):

* the periodic ENTRYPOINT (``dispatcher_reconcile``) heals un-healed terminal
  runs and reports the counters (a sweep with no production caller is a
  silent critical — FAR-189);
* the interleaving matrix — sweep→dual-write, dual-write→sweep, sweep-twice,
  dual-write-twice;
* scoping properties — non-terminal runs untouched, the per-tick cap bounds
  work, the high-water mark advances (and skips already-swept runs);
* per-org error isolation (a sweep failure bumps ``outputs_sweep_org_failed``
  and never raises past the sweep leg).

Each test gets its OWN organisation (``runs`` carries
``UNIQUE(organisation_id, run_number)``; the suite runs with ``-n 2`` in the
pre-deploy gate) — xdist-safe.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import AsyncGenerator, Generator
from dataclasses import dataclass
from typing import Any

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from modulo.core import cron_helpers as ch
from modulo.db.crud.run import update_run_status
from modulo.db.rls import set_rls_org

pytestmark = pytest.mark.integration


# ---------------------------------------------------------------------------
# Fixtures + seed helpers
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _Tenant:
    """An isolated org plus the pipeline/snapshot a run needs to FK against."""

    org_id: uuid.UUID
    pipeline_id: uuid.UUID
    snapshot_id: uuid.UUID


@pytest_asyncio.fixture
async def sweep_tenant(db_engine: AsyncEngine, test_user: uuid.UUID) -> _Tenant:
    """Commit a dedicated organisation + pipeline + snapshot for one test."""
    tenant = _Tenant(uuid.uuid4(), uuid.uuid4(), uuid.uuid4())
    async with db_engine.connect() as conn, conn.begin():
        await conn.execute(
            text("INSERT INTO organisations (id, name, slug, settings_json) VALUES (:id, :name, :slug, '{}'::json)"),
            {
                "id": str(tenant.org_id),
                "name": f"Outputs Sweep Org {tenant.org_id.hex[:8]}",
                "slug": f"oso-{tenant.org_id.hex[:12]}",
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
                "name": "Outputs Sweep Pipeline",
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


@pytest.fixture(scope="module")
def sweep_redis_url() -> Generator[str, None, None]:
    """A dedicated Redis for the ``dispatcher_reconcile`` entrypoint test."""
    module = pytest.importorskip("testcontainers.community.redis")
    with module.RedisContainer("redis:7-alpine") as rc:
        yield f"redis://{rc.get_container_host_ip()}:{rc.get_exposed_port(6379)}/0"


@pytest_asyncio.fixture
async def reconcile_env(
    sweep_redis_url: str, migrated_db_url: str, monkeypatch: pytest.MonkeyPatch
) -> AsyncGenerator[str, None]:
    """Point settings at the testcontainers Postgres + THIS Redis for one test.

    Mirrors the ``saq`` conftest's ``saq_settings_env`` (the reconcile
    machinery reads ``settings.redis_url`` / rebuilds module-level engines),
    scoped to the two entrypoint tests in this file.
    """
    monkeypatch.setenv("DATABASE_URL", migrated_db_url)
    monkeypatch.setenv("REDIS_URL", sweep_redis_url)
    monkeypatch.setenv("SECRET_KEY", "a" * 40)
    monkeypatch.setenv("FERNET_KEY", "b" * 44)
    monkeypatch.setenv("MODULO_ADMIN_PASSWORD", "test")

    from modulo.settings import get_settings

    get_settings.cache_clear()
    ch._ENGINE = None

    yield sweep_redis_url

    ch._ENGINE = None
    get_settings.cache_clear()


def _dumps(value: Any) -> str:
    return json.dumps(value)


async def _seed_terminal_run(
    db_engine: AsyncEngine,
    tenant: _Tenant,
    *,
    run_number: int,
    status: str = "complete",
    outputs: dict[str, Any] | None = None,
    telemetry: dict[str, Any] | None = None,
    markers: dict[str, Any] | None = None,
    completed_offset_hours: int = 1,
    run_id: uuid.UUID | None = None,
) -> uuid.UUID:
    """Commit a run with legacy blob columns set, NO new-table rows.

    The terminal status + ``completed_at`` make the run sweep-eligible; the
    legacy-only blob state is exactly the pre-FAR-583 / kill-switch-off shape
    the catch-up sweep exists to heal. *run_id* may be pre-generated so the
    caller can build grammar-valid marker keys for the same run.
    """
    run_id = run_id or uuid.uuid4()
    async with db_engine.connect() as conn, conn.begin():
        await conn.execute(
            text(
                "INSERT INTO runs (id, organisation_id, pipeline_id, snapshot_id, "
                "trigger_type, status, run_number, input_hash, langgraph_thread_id, claim_token, "
                "outputs_json, node_telemetry_json, raw_output_markers, "
                "started_at, completed_at) "
                "VALUES (:id, :oid, :pid, :sid, 'manual', :status, :rn, :hash, :thread, :tok, "
                "CAST(:outputs AS json), CAST(:telemetry AS json), CAST(:markers AS json), "
                "now() - make_interval(hours => :offh + 1), now() - make_interval(hours => :offh))"
            ),
            {
                "id": str(run_id),
                "oid": str(tenant.org_id),
                "pid": str(tenant.pipeline_id),
                "sid": str(tenant.snapshot_id),
                "status": status,
                "rn": run_number,
                "hash": "c" * 64,
                "thread": f"sweep-{run_id}",
                "tok": f"tok-sweep-{run_id}",
                "outputs": "null" if outputs is None else _dumps(outputs),
                "telemetry": "null" if telemetry is None else _dumps(telemetry),
                "markers": "null" if markers is None else _dumps(markers),
                "offh": completed_offset_hours,
            },
        )
    return run_id


def _marker_key(run_id: uuid.UUID, node_id: str, suffix: str = "1") -> str:
    """A grammar-valid attempt key (node_runner shape) for *run_id*."""
    return f"run:{run_id}:node:{node_id}:{suffix}"


async def _fetch_new_table_rows(db_engine: AsyncEngine, run_id: uuid.UUID) -> list[tuple[str, str, Any, Any, Any]]:
    async with db_engine.connect() as conn:
        result = await conn.execute(
            text(
                "SELECT node_id, attempt_key, outputs_json, node_telemetry_json, raw_output_markers "
                "FROM run_node_outputs WHERE run_id = :rid ORDER BY node_id, attempt_key"
            ),
            {"rid": str(run_id)},
        )
        return [(r[0], r[1], r[2], r[3], r[4]) for r in result.all()]


async def _fetch_legacy_row(db_engine: AsyncEngine, run_id: uuid.UUID) -> dict[str, Any]:
    async with db_engine.connect() as conn:
        row = (
            await conn.execute(
                text("SELECT status, outputs_json, node_telemetry_json, raw_output_markers FROM runs WHERE id = :rid"),
                {"rid": str(run_id)},
            )
        ).fetchone()
    assert row is not None
    return {
        "status": row[0],
        "outputs_json": row[1],
        "node_telemetry_json": row[2],
        "raw_output_markers": row[3],
    }


@pytest_asyncio.fixture
async def rls_app_session(app_engine: AsyncEngine) -> AsyncGenerator[AsyncSession, None]:
    """Session whose connections run as a NOBYPASSRLS role, so RLS applies."""
    factory = async_sessionmaker(app_engine, expire_on_commit=False)
    session = factory()
    try:
        yield session
    finally:
        await session.close()


async def _run_sweep(tenant: _Tenant) -> dict[str, Any]:
    """Run the sweep leg for one org against a fresh summary."""
    summary = ch._dispatcher_summary()
    await ch._run_outputs_sweep_for_org(tenant.org_id, summary)
    return summary


# ---------------------------------------------------------------------------
# Entrypoint + healing + counters
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_entrypoint_heals_and_counts(db_engine: AsyncEngine, sweep_tenant: _Tenant, reconcile_env: str) -> None:
    """The periodic dispatcher_reconcile entrypoint heals + counts (FAR-189)."""
    run_a = await _seed_terminal_run(
        db_engine,
        sweep_tenant,
        run_number=1,
        outputs={"n1": {"answer": 1}},
        telemetry={"n1": {"ms": 5}},
    )
    run_b = uuid.uuid4()
    await _seed_terminal_run(
        db_engine,
        sweep_tenant,
        run_number=2,
        status="failed",
        run_id=run_b,
        markers={_marker_key(run_b, "n2"): {"delivery_done": True}},
        completed_offset_hours=2,
    )
    assert not await _fetch_new_table_rows(db_engine, run_a)
    assert not await _fetch_new_table_rows(db_engine, run_b)

    summary = await ch.dispatcher_reconcile()

    # The sweep healed OUR runs (the shared DB may hold other orgs' un-healed
    # runs — assert a lower bound, and pin OUR runs by their rows).
    assert summary["outputs_sweep_healed"] >= 2
    rows_a = await _fetch_new_table_rows(db_engine, run_a)
    assert [(r[0], r[1]) for r in rows_a] == [("n1", "__final__")]
    rows_b = await _fetch_new_table_rows(db_engine, run_b)
    assert [(r[0], r[1]) for r in rows_b] == [("n2", _marker_key(run_b, "n2"))]
    legacy_a = await _fetch_legacy_row(db_engine, run_a)
    # The legacy columns are untouched (B1 removes the legacy write, not the sweep).
    assert legacy_a["outputs_json"] == {"n1": {"answer": 1}}


@pytest.mark.asyncio
async def test_org_failure_is_isolated_and_counted(
    db_engine: AsyncEngine, sweep_tenant: _Tenant, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A sweep failure bumps outputs_sweep_org_failed and never raises."""
    await _seed_terminal_run(db_engine, sweep_tenant, run_number=1, outputs={"n1": {"v": 1}})

    async def _boom(*args: Any, **kwargs: Any) -> dict[str, Any]:
        raise RuntimeError("injected sweep failure")

    monkeypatch.setattr("modulo.db.crud.run_node_outputs.backfill_run_node_outputs_batch", _boom)
    summary = await _run_sweep(sweep_tenant)
    assert summary["outputs_sweep_org_failed"] == 1
    assert summary["outputs_sweep_healed"] == 0


# ---------------------------------------------------------------------------
# Interleaving matrix
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_sweep_then_dual_write_replace(
    db_engine: AsyncEngine, sweep_tenant: _Tenant, rls_app_session: AsyncSession
) -> None:
    """sweep→dual-write: the REPLACE write wins after a sweep heal."""
    run_id = await _seed_terminal_run(
        db_engine, sweep_tenant, run_number=1, outputs={"a": {"v": 1}, "b": {"v": 2}}, telemetry={"a": {"t": 1}}
    )
    summary = await _run_sweep(sweep_tenant)
    assert summary["outputs_sweep_healed"] >= 1

    async with rls_app_session.begin():
        await set_rls_org(rls_app_session, sweep_tenant.org_id)
        await update_run_status(
            rls_app_session,
            run_id,
            "complete",
            outputs_json={"a": {"v": 10}},
            node_telemetry_json={"a": {"t": 2}},
        )

    rows = await _fetch_new_table_rows(db_engine, run_id)
    final_nodes = sorted(r[0] for r in rows if r[1] == "__final__" and r[0] != "__run_meta__")
    assert final_nodes == ["a"]
    assert rows[0][2] == {"v": 10}
    legacy = await _fetch_legacy_row(db_engine, run_id)
    assert legacy["outputs_json"] == {"a": {"v": 10}}


@pytest.mark.asyncio
async def test_dual_write_then_sweep_is_a_noop(
    db_engine: AsyncEngine, sweep_tenant: _Tenant, rls_app_session: AsyncSession
) -> None:
    """dual-write→sweep: an already-fed run is skipped, rows unchanged."""
    run_id = await _seed_terminal_run(db_engine, sweep_tenant, run_number=1)
    async with rls_app_session.begin():
        await set_rls_org(rls_app_session, sweep_tenant.org_id)
        await update_run_status(
            rls_app_session,
            run_id,
            "complete",
            outputs_json={"n1": {"a": 1}},
            node_telemetry_json={"n1": {"t": 1}},
        )
    before = await _fetch_new_table_rows(db_engine, run_id)
    assert before

    summary = await _run_sweep(sweep_tenant)
    # The run's representation is complete — the sweep healed nothing.
    assert summary["outputs_sweep_healed"] == 0
    assert await _fetch_new_table_rows(db_engine, run_id) == before


@pytest.mark.asyncio
async def test_sweep_twice_is_idempotent(db_engine: AsyncEngine, sweep_tenant: _Tenant) -> None:
    """sweep-twice: the second pass heals nothing new."""
    run_id = uuid.uuid4()
    await _seed_terminal_run(
        db_engine,
        sweep_tenant,
        run_number=1,
        outputs={"n1": {"v": 1}},
        markers={_marker_key(run_id, "n1"): {"m": 1}},
        run_id=run_id,
    )
    first = await _run_sweep(sweep_tenant)
    assert first["outputs_sweep_healed"] >= 1
    before = await _fetch_new_table_rows(db_engine, run_id)
    assert before

    second = await _run_sweep(sweep_tenant)
    assert second["outputs_sweep_healed"] == 0
    assert await _fetch_new_table_rows(db_engine, run_id) == before


@pytest.mark.asyncio
async def test_dual_write_twice_then_sweep(
    db_engine: AsyncEngine, sweep_tenant: _Tenant, rls_app_session: AsyncSession
) -> None:
    """dual-write-twice→sweep: the final REPLACE state survives the sweep."""
    run_id = await _seed_terminal_run(db_engine, sweep_tenant, run_number=1)
    for outputs in ({"a": {"v": 1}, "b": {"v": 2}}, {"a": {"v": 3}}):
        async with rls_app_session.begin():
            await set_rls_org(rls_app_session, sweep_tenant.org_id)
            await update_run_status(rls_app_session, run_id, "complete", outputs_json=outputs)
    summary = await _run_sweep(sweep_tenant)
    assert summary["outputs_sweep_healed"] == 0
    rows = await _fetch_new_table_rows(db_engine, run_id)
    assert sorted(r[0] for r in rows if r[1] == "__final__") == ["a"]
    assert rows[0][2] == {"v": 3}


# ---------------------------------------------------------------------------
# Scoping properties
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_non_terminal_run_untouched(db_engine: AsyncEngine, sweep_tenant: _Tenant) -> None:
    """The sweep is TERMINAL-only: a non-terminal run's blobs stay un-healed."""
    run_id = await _seed_terminal_run(db_engine, sweep_tenant, run_number=1, status="unknown", outputs={"n1": {"v": 1}})
    summary = await _run_sweep(sweep_tenant)
    assert summary["outputs_sweep_healed"] == 0
    assert not await _fetch_new_table_rows(db_engine, run_id)


@pytest.mark.asyncio
async def test_cap_bounds_work_and_high_water_advances(
    db_engine: AsyncEngine, sweep_tenant: _Tenant, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Per-tick cap bounds the drain; the high-water mark skips healed runs."""
    run_a = await _seed_terminal_run(
        db_engine, sweep_tenant, run_number=1, outputs={"a": {"v": 1}}, completed_offset_hours=3
    )
    run_b = await _seed_terminal_run(
        db_engine, sweep_tenant, run_number=2, outputs={"b": {"v": 1}}, completed_offset_hours=2
    )
    run_c = await _seed_terminal_run(
        db_engine, sweep_tenant, run_number=3, outputs={"c": {"v": 1}}, completed_offset_hours=1
    )

    monkeypatch.setattr(ch, "_OUTPUTS_SWEEP_TICK_CAP", 1)
    summary = await _run_sweep(sweep_tenant)
    assert summary["outputs_sweep_healed"] == 1
    # The oldest completed_at run healed; the others untouched this tick.
    assert await _fetch_new_table_rows(db_engine, run_a)
    assert not await _fetch_new_table_rows(db_engine, run_b)
    assert not await _fetch_new_table_rows(db_engine, run_c)

    # The next tick's high-water mark skips the healed run and advances.
    summary2 = await _run_sweep(sweep_tenant)
    assert summary2["outputs_sweep_healed"] == 1
    assert await _fetch_new_table_rows(db_engine, run_b)
    assert not await _fetch_new_table_rows(db_engine, run_c)


@pytest.mark.asyncio
async def test_markers_only_run_is_healed(db_engine: AsyncEngine, sweep_tenant: _Tenant) -> None:
    """A markers-only run heals its attempt-keyed rows (unknown keys kept)."""
    run_id = uuid.uuid4()
    await _seed_terminal_run(
        db_engine,
        sweep_tenant,
        run_number=1,
        markers={_marker_key(run_id, "n1"): {"delivery_done": True}, "junk-key": {"raw": "x"}},
        run_id=run_id,
    )
    summary = await _run_sweep(sweep_tenant)
    assert summary["outputs_sweep_healed"] >= 1
    rows = await _fetch_new_table_rows(db_engine, run_id)
    by_key = {r[1]: r[0] for r in rows}
    # The grammar-valid key parses to its node id; the junk key is preserved
    # as evidence on an '__unknown__' node row (FAR-188).
    assert by_key[_marker_key(run_id, "n1")] == "n1"
    assert by_key["junk-key"] == "__unknown__"


@pytest.mark.asyncio
async def test_sweep_selects_oldest_first(
    db_engine: AsyncEngine, sweep_tenant: _Tenant, monkeypatch: pytest.MonkeyPatch
) -> None:
    """With the cap bound, the sweep consumes the OLDEST completed_at first."""
    older = await _seed_terminal_run(
        db_engine, sweep_tenant, run_number=1, outputs={"o": {"v": 1}}, completed_offset_hours=5
    )
    newer = await _seed_terminal_run(
        db_engine, sweep_tenant, run_number=2, outputs={"n": {"v": 1}}, completed_offset_hours=1
    )
    monkeypatch.setattr(ch, "_OUTPUTS_SWEEP_TICK_CAP", 1)
    summary = await _run_sweep(sweep_tenant)
    assert summary["outputs_sweep_healed"] == 1
    assert await _fetch_new_table_rows(db_engine, older)
    assert not await _fetch_new_table_rows(db_engine, newer)
