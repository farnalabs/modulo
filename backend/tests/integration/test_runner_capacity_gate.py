"""Integration tests for the FAR-594 D8 runner capacity gate.

Drives ``runner_capacity.acquire_runner_dispatch_slot``,
``reconcile_runner_dispatch_markers``, and the HITL tombstone DIRECTLY against
a real Postgres (testcontainers) with runs seeded via raw SQL — the
``test_org_sandbox_capacity`` pattern (mocked providers, real Postgres).
"""

import asyncio
import json
import logging
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker

from modulo.core.runner_capacity import (
    RunnerCapacityDeniedError,
    acquire_runner_dispatch_slot,
    build_dispatch_marker,
    mark_runner_dispatch_cleared_at_hitl,
    reconcile_runner_dispatch_markers,
    runner_capacity_lock_keys,
)
from modulo.db.rls import set_rls_org

pytestmark = [
    pytest.mark.integration,
    pytest.mark.xdist_group(name="org_sandbox_capacity"),
]


def _thread_id(org_id: uuid.UUID, run_id: uuid.UUID) -> str:
    return f"{org_id}:{run_id}"


def _hash(seq: int) -> str:
    return f"cap-{seq}-{uuid.uuid4().hex[:8]}"


_run_number_seq = 0


def _next_run_number() -> int:
    global _run_number_seq
    _run_number_seq += 1
    return _run_number_seq


async def _seed_org(
    db_engine: AsyncEngine,
    name: str,
    cap: int | None,
    settings: dict | None = None,
) -> uuid.UUID:
    org_id = uuid.uuid4()
    org_settings = {"sandbox_concurrency_limit": cap} if cap is not None else {}
    if settings:
        org_settings.update(settings)
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
                "settings": json.dumps(org_settings),
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
    max_concurrent: int = 100,
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
            {"id": str(pipeline_id), "oid": str(org_id), "name": name, "uid": str(account_id), "mcr": max_concurrent},
        )
    return pipeline_id


async def _seed_snapshot(db_engine: AsyncEngine, org_id: uuid.UUID, pipeline_id: uuid.UUID) -> uuid.UUID:
    snapshot_id = uuid.uuid4()
    graph = {"nodes": [{"id": "sandbox-1", "node_type": "sandbox_agent", "agent_prompt": "work"}], "edges": []}
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
            {"id": str(snapshot_id), "pid": str(pipeline_id), "oid": str(org_id), "graph": json.dumps(graph)},
        )
    return snapshot_id


async def _seed_run(
    db_engine: AsyncEngine,
    org_id: uuid.UUID,
    pipeline_id: uuid.UUID,
    snapshot_id: uuid.UUID,
    *,
    status: str = "running",
    error_code: str | None = None,
    marker: str | None = None,
    claim_token: str | None = None,
    heartbeat_at: datetime | None = None,
    dispatcher: str | None = None,
    started_at: datetime | None = None,
) -> uuid.UUID:
    run_id = uuid.uuid4()
    factory = async_sessionmaker(db_engine, expire_on_commit=False)
    values: dict[str, Any] = {
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
        # Deterministic claim token derived from the run id so gate calls in
        # tests can fence against the SAME token the executor would hold.
        "claim_token": claim_token or f"tok-{run_id.hex[:12]}",
    }
    for col, value in (
        ("sandbox_dispatch_state", marker),
        ("heartbeat_at", heartbeat_at),
        ("dispatcher", dispatcher),
        ("started_at", started_at),
    ):
        if value is not None:
            values[col] = value
    async with factory() as session, session.begin():
        await set_rls_org(session, org_id)
        await session.execute(_insert_run(values))
    return run_id


def _insert_run(values: dict[str, Any]) -> Any:
    from sqlalchemy import insert

    from modulo.db.models.run import Run

    return insert(Run).values(**values)


async def _run_row(
    db_engine: AsyncEngine,
    org_id: uuid.UUID,
    run_id: uuid.UUID,
) -> dict[str, Any]:
    factory = async_sessionmaker(db_engine, expire_on_commit=False)
    async with factory() as session, session.begin():
        await set_rls_org(session, org_id)
        result = await session.execute(
            text("SELECT status, error_code, sandbox_dispatch_state FROM runs WHERE id = :rid"),
            {"rid": str(run_id)},
        )
        row = result.first()
        if row is None:
            return {}
        return {"status": row.status, "error_code": row.error_code, "marker": row.sandbox_dispatch_state}


async def _seed_org_account(
    db_engine: AsyncEngine,
    name: str,
    cap: int | None,
    settings: dict | None = None,
) -> tuple[uuid.UUID, uuid.UUID]:
    org_id = await _seed_org(db_engine, name, cap, settings=settings)
    account_id = await _seed_account(db_engine, org_id, f"{name}-{org_id.hex[:8]}@test.local")
    return org_id, account_id


@pytest_asyncio.fixture(autouse=True)
async def _clean_runs_between_tests(db_engine: AsyncEngine) -> None:
    async with db_engine.connect() as conn:
        await conn.execute(text("TRUNCATE runs RESTART IDENTITY CASCADE"))
        await conn.commit()


def _gate_flags(monkeypatch: pytest.MonkeyPatch, *, flag_on: bool, lock_timeout_ms: int = 2000) -> None:
    monkeypatch.setenv("RUNNER_CAPACITY_GATE_ENABLED", "true" if flag_on else "false")
    monkeypatch.setenv("RUNNER_CAPACITY_LOCK_TIMEOUT_MS", str(lock_timeout_ms))


# ---------------------------------------------------------------------------
# Acceptance: limit 2 (explicit) → 20 concurrent mixed dispatches, exactly 2
# acquire, the rest retryable-denied; re-dispatch after freeing succeeds.
# ---------------------------------------------------------------------------


async def test_gate_admits_exactly_cap_of_concurrent_dispatches(
    db_engine: AsyncEngine,
    migrated_db_url: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("DATABASE_URL", migrated_db_url)
    _gate_flags(monkeypatch, flag_on=True)

    org_id, user_id = await _seed_org_account(db_engine, "D8Cap2", cap=2)
    pipe = await _seed_pipeline(db_engine, org_id, "PipeD8", user_id)
    snap = await _seed_snapshot(db_engine, org_id, pipe)
    factory = async_sessionmaker(db_engine, expire_on_commit=False)

    runs: list[tuple[uuid.UUID, str]] = []
    for _ in range(20):
        provider = "e2b" if len(runs) % 2 else "runner_docker"  # mixed providers
        run_id = await _seed_run(db_engine, org_id, pipe, snap)
        runs.append((run_id, provider))

    async def _attempt(item: tuple[uuid.UUID, str]) -> tuple[uuid.UUID, str | None]:
        run_id, provider = item
        try:
            slot = await acquire_runner_dispatch_slot(
                factory,
                org_id=org_id,
                run_id=str(run_id),
                claim_token=f"tok-{run_id.hex[:12]}",
                node_id="n1",
                provider=provider,
            )
        except RunnerCapacityDeniedError:
            return run_id, "denied"
        return run_id, slot.status

    results = await asyncio.gather(*[_attempt(item) for item in runs])
    acquired = [r for r, status in results if status == "acquired"]
    denied = [r for r, status in results if status == "denied"]
    assert len(acquired) == 2, f"exactly the cap must acquire; got {len(acquired)}"
    assert len(denied) == 18

    # The two winners carry committed markers (the reservation).
    markers = 0
    for run_id, _provider in runs:
        row = await _run_row(db_engine, org_id, run_id)
        if row["marker"] is not None and json.loads(row["marker"]).get("state") == "dispatching":
            markers += 1
    assert markers == 2

    # Re-dispatch: free both slots (terminalize) → a denied run now acquires.
    async with factory() as session, session.begin():
        await set_rls_org(session, org_id)
        for run_id in acquired:
            await session.execute(
                text("UPDATE runs SET status = 'complete', completed_at = now() WHERE id = :rid"),
                {"rid": str(run_id)},
            )
    loser = denied[0]
    slot = await acquire_runner_dispatch_slot(
        factory,
        org_id=org_id,
        run_id=str(loser),
        claim_token=f"tok-{loser.hex[:12]}",
        node_id="n1",
        provider="e2b",
    )
    assert slot.status == "acquired"


async def test_gate_flag_off_absent_key_is_no_gate(
    db_engine: AsyncEngine,
    migrated_db_url: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Flag-off + absent key = NO gate (the D3b flag-window contract)."""
    monkeypatch.setenv("DATABASE_URL", migrated_db_url)
    _gate_flags(monkeypatch, flag_on=False)

    org_id, user_id = await _seed_org_account(db_engine, "D8FlagOff", cap=None)  # absent key
    pipe = await _seed_pipeline(db_engine, org_id, "PipeFlagOff", user_id)
    snap = await _seed_snapshot(db_engine, org_id, pipe)
    factory = async_sessionmaker(db_engine, expire_on_commit=False)

    for _ in range(6):
        run_id = await _seed_run(db_engine, org_id, pipe, snap)
        slot = await acquire_runner_dispatch_slot(
            factory, org_id=org_id, run_id=str(run_id), claim_token=f"tok-{run_id.hex[:12]}", node_id="n1"
        )
        assert slot.status == "acquired"


async def test_gate_flag_on_default_caps_docker_tier_only(
    db_engine: AsyncEngine,
    migrated_db_url: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Default (absent key) + flag ON caps Docker-tier only: 4 Docker+legacy
    tier-less markers saturate the default 4; e2b markers don't count."""
    monkeypatch.setenv("DATABASE_URL", migrated_db_url)
    _gate_flags(monkeypatch, flag_on=True)

    org_id, user_id = await _seed_org_account(db_engine, "D8Default", cap=None)  # absent key
    pipe = await _seed_pipeline(db_engine, org_id, "PipeDefault", user_id)
    snap = await _seed_snapshot(db_engine, org_id, pipe)
    factory = async_sessionmaker(db_engine, expire_on_commit=False)

    # 3 legacy tier-less markers + 1 explicit runner_docker marker = 4 slots.
    for _idx, provider in enumerate((None, None, None, "runner_docker")):
        run_id = await _seed_run(
            db_engine,
            org_id,
            pipe,
            snap,
            marker=build_dispatch_marker(f"run:x:node:n:{_idx}", provider),
        )
        _ = run_id
    # 2 e2b markers: NOT counted into the Docker-tier default bucket.
    for _idx in range(2):
        await _seed_run(db_engine, org_id, pipe, snap, marker=build_dispatch_marker(f"run:e:{_idx}", "e2b"))

    fifth = await _seed_run(db_engine, org_id, pipe, snap)
    with pytest.raises(RunnerCapacityDeniedError, match="at capacity"):
        await acquire_runner_dispatch_slot(
            factory, org_id=org_id, run_id=str(fifth), claim_token=f"tok-{fifth.hex[:12]}", node_id="n1"
        )

    # A 6th E2B dispatch is NOT denied (the default bucket is Docker-tier only).
    sixth = await _seed_run(db_engine, org_id, pipe, snap)
    slot = await acquire_runner_dispatch_slot(
        factory, org_id=org_id, run_id=str(sixth), claim_token=f"tok-{sixth.hex[:12]}", node_id="n1", provider="e2b"
    )
    assert slot.status == "acquired"


async def test_gate_excludes_own_marker_on_redispatch(
    db_engine: AsyncEngine,
    migrated_db_url: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A run still carrying a fence-carrying stale marker cannot self-block."""
    monkeypatch.setenv("DATABASE_URL", migrated_db_url)
    _gate_flags(monkeypatch, flag_on=True)

    org_id, user_id = await _seed_org_account(db_engine, "D8Self", cap=1)
    pipe = await _seed_pipeline(db_engine, org_id, "PipeSelf", user_id)
    snap = await _seed_snapshot(db_engine, org_id, pipe)
    factory = async_sessionmaker(db_engine, expire_on_commit=False)

    claim = "tok-self-1"
    run_id = await _seed_run(
        db_engine, org_id, pipe, snap, marker=build_dispatch_marker("old", "e2b"), claim_token=claim
    )
    slot = await acquire_runner_dispatch_slot(
        factory, org_id=org_id, run_id=str(run_id), claim_token=claim, node_id="n1", provider="e2b"
    )
    assert slot.status == "acquired"


async def test_abandoned_awaiting_human_holds_no_slot(
    db_engine: AsyncEngine,
    migrated_db_url: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An abandoned awaiting_human run (live marker even) neither reduces
    available capacity nor blocks approvals — the count is running-only."""
    monkeypatch.setenv("DATABASE_URL", migrated_db_url)
    _gate_flags(monkeypatch, flag_on=True)

    org_id, user_id = await _seed_org_account(db_engine, "D8Hitl", cap=1)
    pipe = await _seed_pipeline(db_engine, org_id, "PipeHitl", user_id)
    snap = await _seed_snapshot(db_engine, org_id, pipe)
    factory = async_sessionmaker(db_engine, expire_on_commit=False)

    await _seed_run(db_engine, org_id, pipe, snap, status="awaiting_human", marker=build_dispatch_marker("k", "e2b"))

    new_run = await _seed_run(db_engine, org_id, pipe, snap)
    slot = await acquire_runner_dispatch_slot(
        factory, org_id=org_id, run_id=str(new_run), claim_token=f"tok-{new_run.hex[:12]}", node_id="n1"
    )
    assert slot.status == "acquired", "an abandoned awaiting_human run must hold no slot"


async def test_hitl_tombstone_is_written_and_capacity_neutral(
    db_engine: AsyncEngine,
    migrated_db_url: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("DATABASE_URL", migrated_db_url)
    _gate_flags(monkeypatch, flag_on=True)

    org_id, user_id = await _seed_org_account(db_engine, "D8Tomb", cap=1)
    pipe = await _seed_pipeline(db_engine, org_id, "PipeTomb", user_id)
    snap = await _seed_snapshot(db_engine, org_id, pipe)
    factory = async_sessionmaker(db_engine, expire_on_commit=False)

    claim = "tok-tomb"
    parked = await _seed_run(db_engine, org_id, pipe, snap, marker=build_dispatch_marker("k", "e2b"), claim_token=claim)
    tombstoned = await mark_runner_dispatch_cleared_at_hitl(
        factory, org_id=org_id, run_id=str(parked), claim_token=claim
    )
    assert tombstoned is True
    row = await _run_row(db_engine, org_id, parked)
    assert row["marker"] is not None
    assert json.loads(row["marker"])["state"] == "cleared_at_hitl"

    # The tombstoned parked run holds no slot → a new dispatch acquires.
    new_run = await _seed_run(db_engine, org_id, pipe, snap)
    slot = await acquire_runner_dispatch_slot(
        factory, org_id=org_id, run_id=str(new_run), claim_token=f"tok-{new_run.hex[:12]}", node_id="n1"
    )
    assert slot.status == "acquired"


async def test_gate_contention_returns_retryable_within_lock_timeout(
    db_engine: AsyncEngine,
    migrated_db_url: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A second session holding the per-org advisory lock → the gate returns a
    retryable capacity error within lock_timeout, not a hang."""
    monkeypatch.setenv("DATABASE_URL", migrated_db_url)
    _gate_flags(monkeypatch, flag_on=True, lock_timeout_ms=500)

    org_id, user_id = await _seed_org_account(db_engine, "D8Contend", cap=5)
    pipe = await _seed_pipeline(db_engine, org_id, "PipeContend", user_id)
    snap = await _seed_snapshot(db_engine, org_id, pipe)
    factory = async_sessionmaker(db_engine, expire_on_commit=False)
    run_id = await _seed_run(db_engine, org_id, pipe, snap)

    k1, k2 = runner_capacity_lock_keys(org_id)
    blocker_factory = async_sessionmaker(db_engine, expire_on_commit=False)
    blocker_session = await blocker_factory().__aenter__()
    await blocker_session.execute(  # type: ignore[union-attr]
        text("SELECT pg_advisory_lock(:k1, :k2)"),
        {"k1": k1, "k2": k2},
    )
    try:
        import time

        started = time.monotonic()
        with pytest.raises(RunnerCapacityDeniedError, match=r"degraded|capacity"):
            await acquire_runner_dispatch_slot(
                factory, org_id=org_id, run_id=str(run_id), claim_token=f"tok-{run_id.hex[:12]}", node_id="n1"
            )
        elapsed = time.monotonic() - started
        assert elapsed < 5.0, "the gate must degrade within the lock window, not hang"
    finally:
        await blocker_session.execute(  # type: ignore[union-attr]
            text("SELECT pg_advisory_unlock(:k1, :k2)"),
            {"k1": k1, "k2": k2},
        )
        await blocker_session.__aexit__(None, None, None)  # type: ignore[union-attr]


async def test_same_run_concurrent_dispatch_and_resume_no_deadlock(
    db_engine: AsyncEngine,
    migrated_db_url: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """3x+ same-run concurrent dispatch+resume overlap: the uniform
    row→advisory ordering must produce ZERO SQLSTATE 40P01 — the stress test
    proves the cycle-freedom rather than assuming it. The "resume" leg
    emulates the executor's resume ordering exactly (own-row fenced status
    write FIRST, then the advisory lock, then the marker count)."""
    monkeypatch.setenv("DATABASE_URL", migrated_db_url)
    _gate_flags(monkeypatch, flag_on=True, lock_timeout_ms=2000)

    org_id, user_id = await _seed_org_account(db_engine, "D8Stress", cap=5)
    pipe = await _seed_pipeline(db_engine, org_id, "PipeStress", user_id)
    snap = await _seed_snapshot(db_engine, org_id, pipe)
    factory = async_sessionmaker(db_engine, expire_on_commit=False)

    run_id = await _seed_run(db_engine, org_id, pipe, snap)
    claim = f"tok-{run_id.hex[:12]}"
    k1, k2 = runner_capacity_lock_keys(org_id)

    async def _dispatch_leg() -> str:
        try:
            slot = await acquire_runner_dispatch_slot(
                factory, org_id=org_id, run_id=str(run_id), claim_token=claim, node_id="n1", provider="e2b"
            )
            return slot.status
        except RunnerCapacityDeniedError:
            return "denied"

    async def _resume_leg() -> str:
        # Executor.resume ordering: update_run_status(running) on the OWN row
        # first, then the per-org advisory lock, then the count.
        try:
            async with factory() as session, session.begin():
                await set_rls_org(session, org_id)
                await session.execute(
                    text("UPDATE runs SET status = 'running' WHERE id = :rid AND claim_token = :tok"),
                    {"rid": str(run_id), "tok": claim},
                )
                await session.execute(text("SELECT pg_advisory_xact_lock(:k1, :k2)"), {"k1": k1, "k2": k2})
                await session.execute(
                    text(
                        "SELECT count(*) FROM runs WHERE organisation_id = :oid AND sandbox_dispatch_state IS NOT NULL"
                    ),
                    {"oid": str(org_id)},
                )
            return "resumed"
        except Exception as exc:
            if "40P01" in str(exc) or "deadlock" in str(exc).lower():
                return "deadlock"
            raise

    for round_index in range(4):
        outcomes = await asyncio.gather(_dispatch_leg(), _resume_leg(), _dispatch_leg(), _resume_leg())
        assert "deadlock" not in outcomes, f"round {round_index}: {outcomes}"
        assert any(status in ("acquired", "resumed", "denied") for status in outcomes)


# ---------------------------------------------------------------------------
# The state-aware reconciliation sweep (rules a)-(h)
# ---------------------------------------------------------------------------


async def _sweep(db_engine: AsyncEngine) -> dict[str, Any]:
    from modulo.settings import get_settings

    get_settings.cache_clear()
    factory = async_sessionmaker(db_engine, expire_on_commit=False)
    return await reconcile_runner_dispatch_markers(factory)


async def test_sweep_fence_components_survive_staleness(
    db_engine: AsyncEngine,
    migrated_db_url: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """(a) never clears script_executing fence components — precedence over
    the staleness rule."""
    monkeypatch.setenv("DATABASE_URL", migrated_db_url)
    org_id, user_id = await _seed_org_account(db_engine, "D8SweepFence", cap=None)
    pipe = await _seed_pipeline(db_engine, org_id, "PipeFence", user_id)
    snap = await _seed_snapshot(db_engine, org_id, pipe)
    fence_marker = json.dumps({"state": "script_executing", "attempt_key": "k"})
    run_id = await _seed_run(db_engine, org_id, pipe, snap, marker=fence_marker)

    await _sweep(db_engine)
    row = await _run_row(db_engine, org_id, run_id)
    assert row["marker"] == fence_marker, "the exactly-once fence must survive the sweep"


async def test_sweep_recoverable_runs_keep_markers(
    db_engine: AsyncEngine,
    migrated_db_url: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """(b) a running SAQ run whose heartbeat went stale is dispatcher_reconcile
    -recoverable (the re-dispatch predicate matches it) — its marker survives."""
    monkeypatch.setenv("DATABASE_URL", migrated_db_url)
    org_id, user_id = await _seed_org_account(db_engine, "D8SweepRecov", cap=None)
    pipe = await _seed_pipeline(db_engine, org_id, "PipeRecov", user_id)
    snap = await _seed_snapshot(db_engine, org_id, pipe)
    stale = datetime.now(UTC) - timedelta(minutes=30)
    marker = build_dispatch_marker("k", "e2b")
    run_id = await _seed_run(
        db_engine, org_id, pipe, snap, marker=marker, dispatcher="saq", heartbeat_at=stale, started_at=stale
    )

    await _sweep(db_engine)
    row = await _run_row(db_engine, org_id, run_id)
    assert row["marker"] == marker, "a reconciler-recoverable run's marker must NOT be cleared"


async def test_sweep_terminal_anomaly_codes_exempt(
    db_engine: AsyncEngine,
    migrated_db_url: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """(c) terminal runs carrying the rollback detector's anomaly error codes
    keep their markers."""
    monkeypatch.setenv("DATABASE_URL", migrated_db_url)
    org_id, user_id = await _seed_org_account(db_engine, "D8SweepAnom", cap=None)
    pipe = await _seed_pipeline(db_engine, org_id, "PipeAnom", user_id)
    snap = await _seed_snapshot(db_engine, org_id, pipe)
    marker = build_dispatch_marker("k", "e2b")
    run_id = await _seed_run(
        db_engine, org_id, pipe, snap, status="failed", error_code="script.side_effect_unknown", marker=marker
    )

    await _sweep(db_engine)
    row = await _run_row(db_engine, org_id, run_id)
    assert row["marker"] == marker


async def _backdate_updated_at(db_engine: AsyncEngine, org_id: uuid.UUID, run_id: uuid.UUID, hours: float) -> None:
    """Backdate ``runs.updated_at`` (the legacy marker's staleness fallback)."""
    factory = async_sessionmaker(db_engine, expire_on_commit=False)
    async with factory() as session, session.begin():
        await set_rls_org(session, org_id)
        await session.execute(
            text("UPDATE runs SET updated_at = now() - (:hours * interval '1 hour') WHERE id = :rid"),
            {"hours": hours, "rid": str(run_id)},
        )


async def test_sweep_clears_terminal_non_fence_and_transitions_stale_running(
    db_engine: AsyncEngine,
    migrated_db_url: str,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """(d)+(f)+(g): terminal non-fence markers cleared; a stale non-terminal
    RUNNING run is cleared AND terminalised (no zombie); awaiting_human leaks
    are cleared without killing the run; each clear emits
    ``runner.capacity.marker_cleared`` (the D4 container-destroy coordination
    note)."""
    monkeypatch.setenv("DATABASE_URL", migrated_db_url)
    org_id, user_id = await _seed_org_account(db_engine, "D8SweepClear", cap=None)
    pipe = await _seed_pipeline(db_engine, org_id, "PipeClear", user_id)
    snap = await _seed_snapshot(db_engine, org_id, pipe)

    terminal_run = await _seed_run(
        db_engine, org_id, pipe, snap, status="complete", marker=build_dispatch_marker("k", "e2b")
    )
    stale_running = await _seed_run(
        db_engine,
        org_id,
        pipe,
        snap,
        marker=json.dumps({"state": "dispatching", "attempt_key": "legacy"}),  # legacy tier-less
    )
    # The legacy tier-less marker has no written_at — staleness falls back to
    # runs.updated_at, which the INSERT stamped to now; backdate it.
    await _backdate_updated_at(db_engine, org_id, stale_running, hours=30)
    stale_tombstone = json.dumps(
        {"state": "cleared_at_hitl", "written_at": (datetime.now(UTC) - timedelta(hours=30)).isoformat()}
    )
    stale_awaiting = await _seed_run(
        db_engine,
        org_id,
        pipe,
        snap,
        status="awaiting_human",
        marker=stale_tombstone,
    )

    with caplog.at_level(logging.INFO, logger="modulo.core.runner_capacity"):
        await _sweep(db_engine)

    row = await _run_row(db_engine, org_id, terminal_run)
    assert row["marker"] is None, "a genuinely terminal run's non-fence marker is cleared"

    row = await _run_row(db_engine, org_id, stale_running)
    assert row["marker"] is None
    assert row["status"] == "failed", "a stale non-terminal RUNNING run transitions terminal"
    assert row["error_code"] == "worker_lost"

    row = await _run_row(db_engine, org_id, stale_awaiting)
    assert row["marker"] is None, "a stale non-fence awaiting_human leak is cleared"
    assert row["status"] == "awaiting_human", "a parked run is NOT killed over a stale marker"

    assert any("runner.capacity.marker_cleared" in m for m in caplog.messages)


async def test_sweep_live_long_running_run_untouched(
    db_engine: AsyncEngine,
    migrated_db_url: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """(e) negative test: a LIVE long-running run's marker is NOT cleared."""
    monkeypatch.setenv("DATABASE_URL", migrated_db_url)
    org_id, user_id = await _seed_org_account(db_engine, "D8SweepLive", cap=None)
    pipe = await _seed_pipeline(db_engine, org_id, "PipeLive", user_id)
    snap = await _seed_snapshot(db_engine, org_id, pipe)
    marker = build_dispatch_marker("k", "e2b")
    run_id = await _seed_run(db_engine, org_id, pipe, snap, marker=marker)

    await _sweep(db_engine)
    row = await _run_row(db_engine, org_id, run_id)
    assert row["marker"] == marker, "a live run's marker must survive"
    assert row["status"] == "running"


async def test_sweep_emits_violation_on_breach(
    db_engine: AsyncEngine,
    migrated_db_url: str,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """(h) the sweep asserts the live count ≤ cap and emits
    ``runner.capacity.violation`` on breach (the D8 rollback signal)."""
    monkeypatch.setenv("DATABASE_URL", migrated_db_url)
    org_id, user_id = await _seed_org_account(db_engine, "D8SweepViol", cap=1)
    pipe = await _seed_pipeline(db_engine, org_id, "PipeViol", user_id)
    snap = await _seed_snapshot(db_engine, org_id, pipe)
    await _seed_run(db_engine, org_id, pipe, snap, marker=build_dispatch_marker("k1", "e2b"))
    await _seed_run(db_engine, org_id, pipe, snap, marker=build_dispatch_marker("k2", "e2b"))

    with caplog.at_level(logging.ERROR, logger="modulo.core.runner_capacity"):
        await _sweep(db_engine)
    assert any("runner.capacity.violation" in m for m in caplog.messages)


# ---------------------------------------------------------------------------
# qa fixes (FAR-594 D8): two-org isolation, flag-off fidelity, CAS, OR-composed
# recoverability, precise tombstone exclusion
# ---------------------------------------------------------------------------


async def test_gate_two_org_isolation(
    db_engine: AsyncEngine,
    migrated_db_url: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """F12a: org A is at its cap; org B's dispatch is still admitted — the
    count (and the advisory lock) never leaks across tenants."""
    monkeypatch.setenv("DATABASE_URL", migrated_db_url)
    _gate_flags(monkeypatch, flag_on=True)

    org_a, user_a = await _seed_org_account(db_engine, "D8OrgA", cap=1)
    org_b, user_b = await _seed_org_account(db_engine, "D8OrgB", cap=1)
    pipe_a = await _seed_pipeline(db_engine, org_a, "PipeA", user_a)
    snap_a = await _seed_snapshot(db_engine, org_a, pipe_a)
    pipe_b = await _seed_pipeline(db_engine, org_b, "PipeB", user_b)
    snap_b = await _seed_snapshot(db_engine, org_b, pipe_b)
    factory = async_sessionmaker(db_engine, expire_on_commit=False)

    # Org A saturates its cap of 1 with a live running marker.
    await _seed_run(db_engine, org_a, pipe_a, snap_a, marker=build_dispatch_marker("k", "e2b"))
    # Org B has NO active markers at all.
    run_b = await _seed_run(db_engine, org_b, pipe_b, snap_b)

    slot = await acquire_runner_dispatch_slot(
        factory, org_id=org_b, run_id=str(run_b), claim_token=f"tok-{run_b.hex[:12]}", node_id="n1"
    )
    assert slot.status == "acquired", "org B must be admitted while org A sits at ITS OWN cap"


async def test_sweep_two_org_isolation_byte_identical_live_fence(
    db_engine: AsyncEngine,
    migrated_db_url: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """F12b: org-1's stale terminal marker is cleared while org-2's live
    fence marker stays byte-identical afterwards (updated_at untouched)."""
    monkeypatch.setenv("DATABASE_URL", migrated_db_url)
    org1, user1 = await _seed_org_account(db_engine, "D8SweepO1", cap=None)
    org2, user2 = await _seed_org_account(db_engine, "D8SweepO2", cap=None)
    pipe1 = await _seed_pipeline(db_engine, org1, "PipeO1", user1)
    snap1 = await _seed_snapshot(db_engine, org1, pipe1)
    pipe2 = await _seed_pipeline(db_engine, org2, "PipeO2", user2)
    snap2 = await _seed_snapshot(db_engine, org2, pipe2)

    stale_marker = json.dumps(
        {
            "state": "dispatching",
            "attempt_key": "old",
            "written_at": (datetime.now(UTC) - timedelta(hours=30)).isoformat(),
        }
    )
    org1_run = await _seed_run(db_engine, org1, pipe1, snap1, status="complete", marker=stale_marker)
    org2_run = await _seed_run(db_engine, org2, pipe2, snap2, marker=build_dispatch_marker("k", "e2b"))

    factory = async_sessionmaker(db_engine, expire_on_commit=False)

    async def _row_full(org_id: uuid.UUID, run_id: uuid.UUID) -> dict[str, Any]:
        async with factory() as session, session.begin():
            await set_rls_org(session, org_id)
            result = await session.execute(
                text("SELECT status, error_code, sandbox_dispatch_state, updated_at FROM runs WHERE id = :rid"),
                {"rid": str(run_id)},
            )
            row = result.first()
            return {
                "status": row.status,
                "error_code": row.error_code,
                "marker": row.sandbox_dispatch_state,
                "updated_at": row.updated_at,
            }

    before = await _row_full(org2, org2_run)
    await _sweep(db_engine)

    org1_row = await _row_full(org1, org1_run)
    assert org1_row["marker"] is None, "org-1's stale terminal marker must be cleared"
    org2_row = await _row_full(org2, org2_run)
    assert org2_row["marker"] == before["marker"], "org-2's live marker must be byte-identical"
    assert org2_row["status"] == "running"
    assert org2_row["updated_at"] == before["updated_at"], "an untouched row must not be written by the sweep"


async def test_sweep_fresh_heartbeat_nodeless_zombie_keeps_marker(
    db_engine: AsyncEngine,
    migrated_db_url: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """F3: a fresh-heartbeat nodeless zombie is matched ONLY by the nodeless
    recovery branch — the sweep's recoverability check must be the
    OR-composition (the previous AND-of-all-three misjudged it unrecoverable
    and cleared its stale marker while the reconciler would re-dispatch it)."""
    monkeypatch.setenv("DATABASE_URL", migrated_db_url)
    org_id, user_id = await _seed_org_account(db_engine, "D8SweepNodeless", cap=None)
    pipe = await _seed_pipeline(db_engine, org_id, "PipeNodeless", user_id)
    snap = await _seed_snapshot(db_engine, org_id, pipe)
    # FRESH heartbeat (the stale branch requires a stale one) + nodeless window
    # elapsed + a STALE marker (30h-old written_at — the old AND-bug case).
    marker = json.dumps(
        {
            "state": "dispatching",
            "attempt_key": "stale-attempt",
            "written_at": (datetime.now(UTC) - timedelta(hours=30)).isoformat(),
        }
    )
    run_id = await _seed_run(
        db_engine,
        org_id,
        pipe,
        snap,
        marker=marker,
        dispatcher="saq",
        heartbeat_at=datetime.now(UTC) - timedelta(seconds=30),
        started_at=datetime.now(UTC) - timedelta(minutes=30),
    )

    await _sweep(db_engine)
    row = await _run_row(db_engine, org_id, run_id)
    assert row["marker"] == marker, "a fresh-heartbeat nodeless zombie is reconciler-recoverable"
    assert row["status"] == "running", "a recoverable run must never be terminalised by the sweep"


async def test_gate_flag_off_population_and_tombstone_are_pre_d8(
    db_engine: AsyncEngine,
    migrated_db_url: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """F6: flag OFF keeps the pre-D8 behaviour EXACTLY — the count population
    is ACTIVE_RUN_STATUSES (an awaiting_human run WITH a marker still counts
    and can deny a dispatch) and the HITL tombstone never fires."""
    monkeypatch.setenv("DATABASE_URL", migrated_db_url)
    _gate_flags(monkeypatch, flag_on=False)

    org_id, user_id = await _seed_org_account(db_engine, "D8PreD8", cap=1)
    pipe = await _seed_pipeline(db_engine, org_id, "PipePreD8", user_id)
    snap = await _seed_snapshot(db_engine, org_id, pipe)
    factory = async_sessionmaker(db_engine, expire_on_commit=False)

    claim = "tok-pred8"
    parked = await _seed_run(
        db_engine,
        org_id,
        pipe,
        snap,
        status="awaiting_human",
        marker=build_dispatch_marker("k", "e2b"),
        claim_token=claim,
    )

    # Pre-D8 population: the awaiting_human marker COUNTS → cap 1 is saturated.
    other = await _seed_run(db_engine, org_id, pipe, snap)
    with pytest.raises(RunnerCapacityDeniedError, match="at capacity"):
        await acquire_runner_dispatch_slot(
            factory, org_id=org_id, run_id=str(other), claim_token=f"tok-{other.hex[:12]}", node_id="n1"
        )

    # Flag-off tombstone is a NO-OP: the marker survives the park untouched.
    tombstoned = await mark_runner_dispatch_cleared_at_hitl(
        factory, org_id=org_id, run_id=str(parked), claim_token=claim
    )
    assert tombstoned is False
    row = await _run_row(db_engine, org_id, parked)
    assert row["marker"] is not None
    assert json.loads(row["marker"])["state"] == "dispatching", "flag-off must not write the tombstone"


async def test_count_tombstone_exclusion_matches_state_field_precisely(
    db_engine: AsyncEngine,
    migrated_db_url: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """F13: the tombstone exclusion matches the marker's ``state`` FIELD — a
    live marker whose attempt_key merely CONTAINS the literal still counts
    (the previous bare ``%cleared_at_hitl%`` substring would have excluded
    it, silently under-counting)."""
    monkeypatch.setenv("DATABASE_URL", migrated_db_url)
    _gate_flags(monkeypatch, flag_on=True)

    org_id, user_id = await _seed_org_account(db_engine, "D8Precise", cap=1)
    pipe = await _seed_pipeline(db_engine, org_id, "PipePrecise", user_id)
    snap = await _seed_snapshot(db_engine, org_id, pipe)
    factory = async_sessionmaker(db_engine, expire_on_commit=False)

    tricky_marker = json.dumps({"state": "dispatching", "attempt_key": "x-cleared_at_hitl-y"})
    tricky_run = await _seed_run(db_engine, org_id, pipe, snap, marker=tricky_marker)

    # The tricky marker COUNTS (its state is dispatching) → cap 1 saturated.
    denied = await _seed_run(db_engine, org_id, pipe, snap)
    with pytest.raises(RunnerCapacityDeniedError, match="at capacity"):
        await acquire_runner_dispatch_slot(
            factory, org_id=org_id, run_id=str(denied), claim_token=f"tok-{denied.hex[:12]}", node_id="n1"
        )

    # Swap the TRICKY marker for a REAL tombstone → capacity-neutral (the
    # precise exclusion drops it) → the dispatch is admitted.
    tombstone = json.dumps({"state": "cleared_at_hitl", "written_at": datetime.now(UTC).isoformat()})
    async with factory() as session, session.begin():
        await set_rls_org(session, org_id)
        await session.execute(
            text("UPDATE runs SET sandbox_dispatch_state = :marker WHERE id = :rid"),
            {"marker": tombstone, "rid": str(tricky_run)},
        )
    slot = await acquire_runner_dispatch_slot(
        factory, org_id=org_id, run_id=str(denied), claim_token=f"tok-{denied.hex[:12]}", node_id="n1"
    )
    assert slot.status == "acquired"


async def test_sweep_no_violation_below_cap(
    db_engine: AsyncEngine,
    migrated_db_url: str,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """(h) an org sitting below cap with live dispatches MUST NOT be counted as a
    breach — ``violations`` must be 0 and no ``runner.capacity.violation`` must
    be logged. This is the regression guard for the D8 rollback signal: a healthy
    tick must stay silent so a true breach stays distinguishable from noise."""
    monkeypatch.setenv("DATABASE_URL", migrated_db_url)
    org_id, user_id = await _seed_org_account(db_engine, "D8SweepBelowCap", cap=4)
    pipe = await _seed_pipeline(db_engine, org_id, "PipeBelowCap", user_id)
    snap = await _seed_snapshot(db_engine, org_id, pipe)
    await _seed_run(db_engine, org_id, pipe, snap, marker=build_dispatch_marker("k1", "e2b"))
    await _seed_run(db_engine, org_id, pipe, snap, marker=build_dispatch_marker("k2", "e2b"))

    with caplog.at_level(logging.ERROR, logger="modulo.core.runner_capacity"):
        result = await _sweep(db_engine)
    assert result["violations"] == 0, "an org below cap must not be counted as a breach"
    assert not any("runner.capacity.violation" in m for m in caplog.messages)
