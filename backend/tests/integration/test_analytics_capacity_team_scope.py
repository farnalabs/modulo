"""FAR-1343: the capacity queue boundary against a soft-deleted foreign pipeline.

Real Postgres (Testcontainers). The leak this pins lives entirely in the SQL
``_team_scope`` applies -- the pure report function never sees a foreign row
once the query excludes it -- so a pure unit test cannot exercise it.

The global soft-delete filter (``modulo.db.soft_delete``) injects
``pipelines.deleted_at IS NULL`` into the ON clause of the capacity query's
OUTER JOIN, so a soft-deleted pipeline yields NULL pipeline columns. The
pre-fix predicate (``Pipeline.owner_team_id IS NULL OR IN (caller teams)``)
therefore matched ``owner_team_id IS NULL`` and reclassified the deleted
foreign pipeline as "org-level", letting its surviving pending run into a
team-scoped caller's queue.

Reachable in practice: ``Run`` is not soft-deletable and ``Run.pipeline_id``
is ``ondelete=RESTRICT``, and ``soft_delete_pipeline`` only stamps
``deleted_at`` with no active-run guard -- so a pending run of a deleted
pipeline survives and is reported.

The fix scopes on the run's own stamped team via
``coalesce(Run.owner_team_id, Pipeline.owner_team_id)`` (the sibling analytics
reader's pattern in ``core/analytics/builder.py``), so the boundary no longer
depends on the outer join. These tests assert neither ``pending.by_pipeline``
nor ``series[].by_pipeline`` can name a pipeline outside the caller's scope,
including the fail-closed empty-team case.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import event, text
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker
from sqlalchemy.orm import Session as SASession

from modulo.core.analytics.capacity import (
    CapacityParams,
    build_capacity_rows_query,
    run_capacity_query,
)
from modulo.db.soft_delete import _apply_soft_delete_filter, register_soft_delete_filter

# The soft-delete filter is installed by an IMPORT SIDE EFFECT of
# ``modulo.db.session``; this module asserts its behaviour, so it registers
# explicitly and checks the precondition rather than relying on import order
# (2026-10-14 lesson: an unregistered filter silently asserts the inverse).
register_soft_delete_filter()

_VALID_32 = "a" * 32

pytestmark = pytest.mark.integration


def test_soft_delete_filter_is_registered() -> None:
    """Precondition guard: the assertions below are only meaningful when the
    ORM soft-delete filter is actually installed in this process."""
    assert event.contains(SASession, "do_orm_execute", _apply_soft_delete_filter)


async def _seed_team(db_engine: AsyncEngine, org_id: uuid.UUID, account_id: uuid.UUID, label: str) -> uuid.UUID:
    from modulo.db.crud.team import create_team

    factory = async_sessionmaker(db_engine, expire_on_commit=False)
    async with factory() as session, session.begin():
        await session.execute(text("SELECT set_config('app.organisation_id', :oid, true)"), {"oid": str(org_id)})
        team = await create_team(
            session,
            org_id=org_id,
            name=f"cap-scope-{label}-{uuid.uuid4().hex[:6]}",
            account_id=account_id,
        )
        return team.id


async def _seed_pipeline(
    db_engine: AsyncEngine,
    org_id: uuid.UUID,
    account_id: uuid.UUID,
    owner_team_id: uuid.UUID,
    *,
    soft_deleted: bool,
) -> tuple[uuid.UUID, uuid.UUID]:
    """A committed pipeline (+ its snapshot) owned by *owner_team_id*.

    ``soft_deleted`` stamps ``deleted_at`` -- the exact state
    ``soft_delete_pipeline`` produces -- without touching the surviving runs.
    """
    pipeline_id = uuid.uuid4()
    snapshot_id = uuid.uuid4()
    deleted_at = datetime.now(UTC) if soft_deleted else None
    async with db_engine.begin() as conn:
        await conn.execute(
            text(
                "INSERT INTO pipelines (id, organisation_id, name, account_id, max_concurrent_runs, "
                "lock_wait_timeout_seconds, node_timeout_seconds, run_context_defaults, graph_nodes_json, "
                "visibility, owner_team_id, deleted_at) "
                "VALUES (:id, :oid, :name, :aid, 10, 30, 300, '{}'::json, '[]'::json, 'team', :tid, :del)"
            ),
            {
                "id": str(pipeline_id),
                "oid": str(org_id),
                "name": f"cap-scope-{pipeline_id.hex[:8]}",
                "aid": str(account_id),
                "tid": str(owner_team_id),
                "del": deleted_at,
            },
        )
        await conn.execute(
            text(
                "INSERT INTO pipeline_snapshots (id, pipeline_id, organisation_id, snapshot_version, "
                "graph_json, connector_bindings_json, schema_pins_json, prompt_pins_json, "
                "model_backend_pins_json, run_context_defaults, config_json) "
                "VALUES (:id, :pid, :oid, 1, '{}'::json, '[]'::json, '[]'::json, '[]'::json, "
                "'[]'::json, '{}'::json, '{}'::json)"
            ),
            {"id": str(snapshot_id), "pid": str(pipeline_id), "oid": str(org_id)},
        )
    return pipeline_id, snapshot_id


async def _seed_pending_run(
    db_engine: AsyncEngine,
    org_id: uuid.UUID,
    pipeline_id: uuid.UUID,
    snapshot_id: uuid.UUID,
    owner_team_id: uuid.UUID,
) -> uuid.UUID:
    """A committed ``pending`` run (pending is a live status, so it is always in
    the capacity scan window regardless of ``created_at``)."""
    run_id = uuid.uuid4()
    async with db_engine.begin() as conn:
        await conn.execute(
            text(
                "INSERT INTO runs (id, organisation_id, pipeline_id, snapshot_id, trigger_type, status, "
                "input_hash, langgraph_thread_id, run_number, owner_team_id, created_at) "
                "VALUES (:id, :oid, :pid, :sid, 'manual', 'pending', :hash, :thread, :num, :tid, :created)"
            ),
            {
                "id": str(run_id),
                "oid": str(org_id),
                "pid": str(pipeline_id),
                "sid": str(snapshot_id),
                "hash": uuid.uuid4().hex,
                "thread": f"cap-scope-{run_id.hex[:8]}",
                "num": uuid.uuid4().int % 2_000_000_000 + 1,
                "tid": str(owner_team_id),
                "created": datetime.now(UTC) - timedelta(minutes=10),
            },
        )
    return run_id


async def _cleanup(db_engine: AsyncEngine, pipeline_ids: list[uuid.UUID]) -> None:
    """Delete runs before their pipelines/snapshots (FKs are RESTRICT)."""
    async with db_engine.begin() as conn:
        for pipeline_id in pipeline_ids:
            await conn.execute(text("DELETE FROM runs WHERE pipeline_id = :pid"), {"pid": str(pipeline_id)})
            await conn.execute(
                text("DELETE FROM pipeline_snapshots WHERE pipeline_id = :pid"), {"pid": str(pipeline_id)}
            )
            await conn.execute(text("DELETE FROM pipelines WHERE id = :pid"), {"pid": str(pipeline_id)})


async def _seed_foreign_and_caller(
    db_engine: AsyncEngine, org_id: uuid.UUID, account_id: uuid.UUID
) -> tuple[uuid.UUID, uuid.UUID, list[uuid.UUID]]:
    """Caller team + foreign team; a SOFT-DELETED foreign pipeline and a live
    caller pipeline, each carrying a surviving pending run. Returns
    ``(caller_team, foreign_pipeline_id, cleanup_ids)`` and the caller pipeline
    id is ``cleanup_ids[1]``."""
    caller_team = await _seed_team(db_engine, org_id, account_id, "caller")
    foreign_team = await _seed_team(db_engine, org_id, account_id, "foreign")
    foreign_pipeline_id, foreign_snapshot_id = await _seed_pipeline(
        db_engine, org_id, account_id, foreign_team, soft_deleted=True
    )
    caller_pipeline_id, caller_snapshot_id = await _seed_pipeline(
        db_engine, org_id, account_id, caller_team, soft_deleted=False
    )
    await _seed_pending_run(db_engine, org_id, foreign_pipeline_id, foreign_snapshot_id, foreign_team)
    await _seed_pending_run(db_engine, org_id, caller_pipeline_id, caller_snapshot_id, caller_team)
    return caller_team, foreign_pipeline_id, [foreign_pipeline_id, caller_pipeline_id]


async def test_team_scope_excludes_soft_deleted_foreign_pipeline(
    db_engine: AsyncEngine, test_org: uuid.UUID, test_user: uuid.UUID
) -> None:
    """The scoped row query must not return the deleted foreign pipeline's run,
    while still returning the caller's own live pipeline run (non-vacuous)."""
    caller_team, foreign_pipeline_id, cleanup_ids = await _seed_foreign_and_caller(db_engine, test_org, test_user)
    caller_pipeline_id = cleanup_ids[1]
    factory = async_sessionmaker(db_engine, expire_on_commit=False)
    window_start = datetime.now(UTC) - timedelta(minutes=60)
    try:
        async with factory() as session:
            rows = list(
                (
                    await session.execute(
                        build_capacity_rows_query(
                            test_org,
                            CapacityParams(team_ids=(caller_team,)),
                            window_start=window_start,
                        )
                    )
                ).all()
            )
        pipeline_ids = {str(row.pipeline_id) for row in rows}
        assert str(foreign_pipeline_id) not in pipeline_ids
        assert str(caller_pipeline_id) in pipeline_ids
    finally:
        await _cleanup(db_engine, cleanup_ids)


async def test_team_scope_empty_team_fails_closed_on_soft_deleted_foreign_pipeline(
    db_engine: AsyncEngine, test_org: uuid.UUID, test_user: uuid.UUID
) -> None:
    """An empty caller team is FAIL-CLOSED: only org-level pipelines are visible,
    so neither the deleted foreign pipeline nor the caller's own pipeline's run
    may be returned."""
    _caller_team, foreign_pipeline_id, cleanup_ids = await _seed_foreign_and_caller(db_engine, test_org, test_user)
    caller_pipeline_id = cleanup_ids[1]
    factory = async_sessionmaker(db_engine, expire_on_commit=False)
    window_start = datetime.now(UTC) - timedelta(minutes=60)
    try:
        async with factory() as session:
            rows = list(
                (
                    await session.execute(
                        build_capacity_rows_query(
                            test_org,
                            CapacityParams(team_ids=()),
                            window_start=window_start,
                        )
                    )
                ).all()
            )
        pipeline_ids = {str(row.pipeline_id) for row in rows}
        assert str(foreign_pipeline_id) not in pipeline_ids
        assert str(caller_pipeline_id) not in pipeline_ids
    finally:
        await _cleanup(db_engine, cleanup_ids)


async def test_run_capacity_query_report_never_names_a_foreign_pipeline(
    db_engine: AsyncEngine, db_url: str, test_org: uuid.UUID, test_user: uuid.UUID
) -> None:
    """Service-layer coverage for ``run_capacity_query`` / ``_team_scope`` / the
    ``visible_pipeline_ids`` derivation: the assembled report's
    ``pending.by_pipeline`` and every ``series[].by_pipeline`` must not name the
    deleted foreign pipeline, while the caller's own pipeline is reported."""
    from modulo.settings import Settings

    caller_team, foreign_pipeline_id, cleanup_ids = await _seed_foreign_and_caller(db_engine, test_org, test_user)
    caller_pipeline_id = cleanup_ids[1]
    factory = async_sessionmaker(db_engine, expire_on_commit=False)
    settings = Settings(
        database_url=db_url,
        secret_key=_VALID_32,
        fernet_key=_VALID_32,
        modulo_csrf_enabled=False,
        modulo_auth_rate_limit_enabled=False,
        redis_url="",
        modulo_admin_password="",
    )
    try:
        report = await run_capacity_query(
            org_id=test_org,
            params=CapacityParams(team_ids=(caller_team,)),
            factory=factory,
            settings=settings,
        )
        pending_ids = {entry["pipeline_id"] for entry in report["pending"]["by_pipeline"]}
        assert str(foreign_pipeline_id) not in pending_ids
        assert str(caller_pipeline_id) in pending_ids

        series_ids = {entry["pipeline_id"] for bucket in report["series"] for entry in bucket["by_pipeline"]}
        assert str(foreign_pipeline_id) not in series_ids
        assert str(caller_pipeline_id) in series_ids
    finally:
        await _cleanup(db_engine, cleanup_ids)
