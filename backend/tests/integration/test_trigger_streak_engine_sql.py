"""Real-Postgres SQL coverage for the FAR-190 no-delivery streak engine.

Unit tests assert substrings of the SQL constants; this file runs the engine's
raw SQL against a real Postgres (testcontainers) and asserts the actual walk
behaviour:

* the streak walk counts consecutive no-delivery terminal runs and stops at the
  first delivered / excluded / unclassified run (fail-closed),
* the guarded deactivation UPDATE fires EXACTLY ONCE with the right streak value
  and flips ``active=false``; a second sweep tick is a no-op,
* equal-completed_at ordering is deterministic via the ``r.id`` tie-break,
* the boundary is ``GREATEST(last_delivery_at, streak_epoch)`` — runs predating
  the epoch never count and a delivered run after the epoch extends the boundary
  (pre-epoch history can never mass-deactivate on tick 1),
* the partial-index reshape: the engine's per-trigger query is served by
  ``ix_runs_streak_engine`` (the old ``run_classification IS NOT NULL`` partial
  predicate could never be implied by the engine's ``->> 'value'`` filters).

The deactivation helper runs under a NON-SUPERUSER role so Postgres RLS actually
filters (mirrors production ``modulo_app``); the seeds go through the superuser
engine so they are visible to every role.
"""

from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime, timedelta

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from modulo.core import trigger_streak as ts
from modulo.db.models.run import TERMINAL_STATUSES

pytestmark = pytest.mark.integration


async def _seed_org(db_engine: AsyncEngine, name: str) -> uuid.UUID:
    org_id = uuid.uuid4()
    async with db_engine.connect() as conn, conn.begin():
        await conn.execute(
            text(
                "INSERT INTO organisations (id, name, slug, settings_json) VALUES (:id, :name, :slug, '{}'::json)",
            ),
            {"id": str(org_id), "name": name, "slug": f"{name}-{org_id.hex[:8]}"},
        )
    return org_id


async def _seed_user(db_engine: AsyncEngine, org_id: uuid.UUID, email: str) -> uuid.UUID:
    account_id = uuid.uuid4()
    async with db_engine.connect() as conn, conn.begin():
        await conn.execute(
            text(
                "INSERT INTO accounts (id, email, display_name, auth_provider, active, password_hash) "
                "VALUES (:id, :email, :name, 'local', true, 'hash')",
            ),
            {"id": str(account_id), "email": email, "name": f"Admin {email}"},
        )
        await conn.execute(
            text(
                "INSERT INTO org_memberships (id, account_id, organisation_id, role) "
                "VALUES (:mid, :aid, :oid, 'admin')",
            ),
            {"mid": str(uuid.uuid4()), "aid": str(account_id), "oid": str(org_id)},
        )
    return account_id


async def _seed_pipeline(db_engine: AsyncEngine, org_id: uuid.UUID, user_id: uuid.UUID, name: str) -> uuid.UUID:
    pipeline_id = uuid.uuid4()
    async with db_engine.connect() as conn, conn.begin():
        await conn.execute(
            text(
                "INSERT INTO pipelines (id, organisation_id, name, description, account_id, "
                "max_concurrent_runs, lock_wait_timeout_seconds, node_timeout_seconds, "
                "run_context_defaults, graph_nodes_json, default_autonomy_level, visibility) "
                "VALUES (:id, :oid, :name, :desc, :uid, 5, 30, 300, "
                "'{}'::json, '[]'::json, 'manual_approval', 'org')",
            ),
            {
                "id": str(pipeline_id),
                "oid": str(org_id),
                "name": name,
                "desc": f"Pipeline for {name}",
                "uid": str(user_id),
            },
        )
    return pipeline_id


async def _seed_snapshot(db_engine: AsyncEngine, org_id: uuid.UUID, pipeline_id: uuid.UUID) -> uuid.UUID:
    snapshot_id = uuid.uuid4()
    async with db_engine.connect() as conn, conn.begin():
        await conn.execute(
            text(
                "INSERT INTO pipeline_snapshots (id, pipeline_id, organisation_id, "
                "snapshot_version, graph_json, connector_bindings_json, schema_pins_json, "
                "prompt_pins_json, model_backend_pins_json, run_context_defaults, config_json) "
                "VALUES (:id, :pid, :oid, 1, '{}'::json, '[]'::json, "
                "'[]'::json, '[]'::json, '[]'::json, '{}'::json, '{}'::json)",
            ),
            {"id": str(snapshot_id), "pid": str(pipeline_id), "oid": str(org_id)},
        )
    return snapshot_id


async def _seed_ongoing_trigger(
    db_engine: AsyncEngine,
    *,
    org_id: uuid.UUID,
    pipeline_id: uuid.UUID,
    account_id: uuid.UUID,
    streak_epoch: datetime | None,
    config_json: dict | None = None,
) -> uuid.UUID:
    trigger_id = uuid.uuid4()
    async with db_engine.connect() as conn, conn.begin():
        await conn.execute(
            text(
                "INSERT INTO triggers (id, organisation_id, pipeline_id, account_id, trigger_type, "
                "active, max_concurrent_runs, daily_spend_limit, config_json, streak_epoch) "
                "VALUES (:id, :oid, :pid, :aid, 'ongoing', true, 2, 100.0, :cfg, :epoch)",
            ),
            {
                "id": str(trigger_id),
                "oid": str(org_id),
                "pid": str(pipeline_id),
                "aid": str(account_id),
                "cfg": json.dumps(config_json or {}),
                "epoch": streak_epoch,
            },
        )
    return trigger_id


async def _insert_run(
    db_engine: AsyncEngine,
    *,
    org_id: uuid.UUID,
    pipeline_id: uuid.UUID,
    snapshot_id: uuid.UUID,
    trigger_id: uuid.UUID,
    status: str,
    completed_at: datetime,
    classification_value: str,
    reason: str = "no_delivery",
    run_id: uuid.UUID | None = None,
    delivered_pr_urls: list[str] | None = None,
) -> uuid.UUID:
    run_id = run_id or uuid.uuid4()
    async with db_engine.connect() as conn, conn.begin():
        await conn.execute(
            text(
                "INSERT INTO runs (id, organisation_id, pipeline_id, snapshot_id, trigger_id, "
                "trigger_type, status, run_number, input_hash, langgraph_thread_id, "
                "started_at, completed_at, run_classification) "
                "VALUES (:id, :oid, :pid, :sid, :tid, 'ongoing', :status, :run_number, :ihash, "
                ":thread, :started, :completed, :cls)",
            ),
            {
                "id": str(run_id),
                "oid": str(org_id),
                "pid": str(pipeline_id),
                "sid": str(snapshot_id),
                "tid": str(trigger_id),
                "status": status,
                "run_number": int(run_id.int % 10**9) + 1,
                "ihash": uuid.uuid4().hex,
                "thread": f"thread-{run_id.hex}",
                "started": completed_at - timedelta(minutes=2),
                "completed": completed_at,
                "cls": json.dumps(
                    {
                        "value": classification_value,
                        "reason": reason,
                        "delivered_pr_urls": delivered_pr_urls or [],
                        "computed_at": completed_at.isoformat(),
                    }
                ),
            },
        )
    return run_id


@pytest_asyncio.fixture(scope="module")
async def org(db_engine: AsyncEngine) -> uuid.UUID:
    return await _seed_org(db_engine, "StreakEngine")


@pytest_asyncio.fixture(scope="module")
async def user(db_engine: AsyncEngine, org: uuid.UUID) -> uuid.UUID:
    return await _seed_user(db_engine, org, "streak-engine@test.local")


@pytest_asyncio.fixture(scope="module")
async def pipeline(db_engine: AsyncEngine, org: uuid.UUID, user: uuid.UUID) -> uuid.UUID:
    return await _seed_pipeline(db_engine, org, user, "StreakEngine-Pipeline")


@pytest_asyncio.fixture(scope="module")
async def snapshot(db_engine: AsyncEngine, org: uuid.UUID, pipeline: uuid.UUID) -> uuid.UUID:
    return await _seed_snapshot(db_engine, org, pipeline)


@pytest_asyncio.fixture(scope="module")
async def app_factory(app_engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    """Session factory over the non-superuser engine (RLS applies, mirrors prod)."""
    return async_sessionmaker(app_engine, expire_on_commit=False, autobegin=False)


def _now() -> datetime:
    return datetime.now(UTC)


@pytest.mark.asyncio
async def test_streak_walk_deactivates_once_with_correct_streak(
    db_engine: AsyncEngine,
    app_factory: async_sessionmaker[AsyncSession],
    org: uuid.UUID,
    pipeline: uuid.UUID,
    snapshot: uuid.UUID,
    user: uuid.UUID,
) -> None:
    """Five consecutive no-delivery terminal runs after the epoch -> the guarded
    UPDATE deactivates EXACTLY ONCE with streak=5; a second sweep tick is a no-op
    (idempotent concurrent ticks)."""
    trigger_id = await _seed_ongoing_trigger(
        db_engine, org_id=org, pipeline_id=pipeline, account_id=user, streak_epoch=_now() - timedelta(days=2)
    )
    epoch = _now() - timedelta(days=2)
    for i in range(5):
        await _insert_run(
            db_engine,
            org_id=org,
            pipeline_id=pipeline,
            snapshot_id=snapshot,
            trigger_id=trigger_id,
            status="failed",
            completed_at=epoch + timedelta(hours=1) + timedelta(hours=i),
            classification_value="no_delivery",
            reason="infra_error",
        )

    deactivated = await ts._deactivate_trigger_on_no_delivery_streak(
        app_factory, org_id=org, trigger_id=trigger_id, threshold=5, window_cutoff=_now()
    )
    assert deactivated is not None
    assert deactivated["id"] == trigger_id
    assert int(deactivated["streak"]) == 5

    # The deactivation is durable: the trigger is now inactive.
    async with db_engine.connect() as conn:
        row = (
            await conn.execute(text("SELECT active FROM triggers WHERE id = :tid"), {"tid": str(trigger_id)})
        ).scalar_one()
    assert row is False

    # FAR-192: the deactivation lifecycle writes a TriggerEvent row with
    # validation_result='auto_deactivated' inside the deactivation transaction.
    # Before migration 0104 widened ``ck_trigger_events_validation_result`` this
    # insert was rejected by the CHECK constraint, which rolled back the whole
    # deactivation — the engine silently never deactivated on production. This
    # assertion exercises the widened vocabulary against real Postgres.
    async with db_engine.connect() as conn:
        event_rows = (
            await conn.execute(
                text(
                    "SELECT validation_result, error_detail FROM trigger_events "
                    "WHERE organisation_id = :oid AND trigger_id = :tid "
                    "AND validation_result = 'auto_deactivated'"
                ),
                {"oid": str(org), "tid": str(trigger_id)},
            )
        ).all()
    assert len(event_rows) == 1, "expected exactly one auto_deactivated TriggerEvent row"
    assert event_rows[0][0] == "auto_deactivated"
    assert "auto-deactivated" in (event_rows[0][1] or ""), f"unexpected error_detail: {event_rows[0][1]!r}"

    # A second tick is a no-op (AND active guard).
    second = await ts._deactivate_trigger_on_no_delivery_streak(
        app_factory, org_id=org, trigger_id=trigger_id, threshold=5, window_cutoff=_now()
    )
    assert second is None


@pytest.mark.asyncio
async def test_delivered_run_breaks_streak(
    db_engine: AsyncEngine,
    app_factory: async_sessionmaker[AsyncSession],
    org: uuid.UUID,
    pipeline: uuid.UUID,
    snapshot: uuid.UUID,
    user: uuid.UUID,
) -> None:
    """A delivered run between no-deliveries breaks the walk: only the trailing
    no-delivery runs after the last delivery count."""
    trigger_id = await _seed_ongoing_trigger(
        db_engine, org_id=org, pipeline_id=pipeline, account_id=user, streak_epoch=_now() - timedelta(days=2)
    )
    epoch = _now() - timedelta(days=2)
    for i in range(3):
        await _insert_run(
            db_engine,
            org_id=org,
            pipeline_id=pipeline,
            snapshot_id=snapshot,
            trigger_id=trigger_id,
            status="failed",
            completed_at=epoch + timedelta(hours=1) + timedelta(hours=i),
            classification_value="no_delivery",
        )
    await _insert_run(
        db_engine,
        org_id=org,
        pipeline_id=pipeline,
        snapshot_id=snapshot,
        trigger_id=trigger_id,
        status="complete",
        completed_at=epoch + timedelta(hours=4),
        classification_value="delivered",
        delivered_pr_urls=["https://github.com/x/y/pull/1"],
    )
    for i in range(2):
        await _insert_run(
            db_engine,
            org_id=org,
            pipeline_id=pipeline,
            snapshot_id=snapshot,
            trigger_id=trigger_id,
            status="failed",
            completed_at=epoch + timedelta(hours=5) + timedelta(hours=i),
            classification_value="no_delivery",
        )

    # Only the 2 trailing no-deliveries count -> below threshold 5 -> no deactivation.
    deactivated = await ts._deactivate_trigger_on_no_delivery_streak(
        app_factory, org_id=org, trigger_id=trigger_id, threshold=5, window_cutoff=_now()
    )
    assert deactivated is None

    # With threshold 2 the trailing streak deactivates with streak=2.
    deactivated = await ts._deactivate_trigger_on_no_delivery_streak(
        app_factory, org_id=org, trigger_id=trigger_id, threshold=2, window_cutoff=_now()
    )
    assert deactivated is not None
    assert int(deactivated["streak"]) == 2


@pytest.mark.asyncio
async def test_excluded_mid_walk_breaks(
    db_engine: AsyncEngine,
    app_factory: async_sessionmaker[AsyncSession],
    org: uuid.UUID,
    pipeline: uuid.UUID,
    snapshot: uuid.UUID,
    user: uuid.UUID,
) -> None:
    """An excluded run (operator/HITL-cancelled, budget_exceeded) between
    no-deliveries stops the walk fail-closed — the streak never spans across
    it."""
    trigger_id = await _seed_ongoing_trigger(
        db_engine, org_id=org, pipeline_id=pipeline, account_id=user, streak_epoch=_now() - timedelta(days=2)
    )
    epoch = _now() - timedelta(days=2)
    for i in range(3):
        await _insert_run(
            db_engine,
            org_id=org,
            pipeline_id=pipeline,
            snapshot_id=snapshot,
            trigger_id=trigger_id,
            status="failed",
            completed_at=epoch + timedelta(hours=1) + timedelta(hours=i),
            classification_value="no_delivery",
        )
    await _insert_run(
        db_engine,
        org_id=org,
        pipeline_id=pipeline,
        snapshot_id=snapshot,
        trigger_id=trigger_id,
        status="cancelled",
        completed_at=epoch + timedelta(hours=4),
        classification_value="excluded",
        reason="cancelled",
    )
    for i in range(3):
        await _insert_run(
            db_engine,
            org_id=org,
            pipeline_id=pipeline,
            snapshot_id=snapshot,
            trigger_id=trigger_id,
            status="failed",
            completed_at=epoch + timedelta(hours=5) + timedelta(hours=i),
            classification_value="no_delivery",
        )

    # Only the 3 trailing no-deliveries count (the excluded run breaks the walk).
    deactivated = await ts._deactivate_trigger_on_no_delivery_streak(
        app_factory, org_id=org, trigger_id=trigger_id, threshold=3, window_cutoff=_now()
    )
    assert deactivated is not None
    assert int(deactivated["streak"]) == 3


@pytest.mark.asyncio
async def test_equal_completed_at_ordering(
    db_engine: AsyncEngine,
    app_factory: async_sessionmaker[AsyncSession],
    org: uuid.UUID,
    pipeline: uuid.UUID,
    snapshot: uuid.UUID,
    user: uuid.UUID,
) -> None:
    """Equal completed_at runs get a deterministic total order (id tie-break): a
    delivered run at the same instant as no-deliveries stops the walk."""
    trigger_id = await _seed_ongoing_trigger(
        db_engine, org_id=org, pipeline_id=pipeline, account_id=user, streak_epoch=_now() - timedelta(days=2)
    )
    same_instant = _now() - timedelta(hours=1)
    # A no-delivery and a delivered run at the SAME completed_at. The delivered
    # one has a LARGER id (created later) -> it is the newest at that instant ->
    # the walk stops immediately (streak 0). The ids are constructed
    # deterministically (``dl.int = nd.int + 1``) so the tie-break ordering is
    # always as specified — independent random draws would fail ~50% of runs.
    nd = uuid.uuid4()
    dl = uuid.UUID(int=nd.int + 1)  # delivered run always has the larger id
    await _insert_run(
        db_engine,
        org_id=org,
        pipeline_id=pipeline,
        snapshot_id=snapshot,
        trigger_id=trigger_id,
        status="failed",
        completed_at=same_instant,
        classification_value="no_delivery",
        run_id=nd,
    )
    await _insert_run(
        db_engine,
        org_id=org,
        pipeline_id=pipeline,
        snapshot_id=snapshot,
        trigger_id=trigger_id,
        status="complete",
        completed_at=same_instant,
        classification_value="delivered",
        delivered_pr_urls=["https://github.com/x/y/pull/2"],
        run_id=dl,
    )

    deactivated = await ts._deactivate_trigger_on_no_delivery_streak(
        app_factory, org_id=org, trigger_id=trigger_id, threshold=1, window_cutoff=_now()
    )
    assert deactivated is None, "a delivered run at the same instant stops the walk"


@pytest.mark.asyncio
async def test_boundary_greatest_excludes_pre_epoch_history(
    db_engine: AsyncEngine,
    app_factory: async_sessionmaker[AsyncSession],
    org: uuid.UUID,
    pipeline: uuid.UUID,
    snapshot: uuid.UUID,
    user: uuid.UUID,
) -> None:
    """Runs predating streak_epoch never count (post-deploy grace) and a
    delivered run AFTER the epoch extends the boundary (GREATEST)."""
    epoch = _now() - timedelta(days=2)
    trigger_id = await _seed_ongoing_trigger(
        db_engine, org_id=org, pipeline_id=pipeline, account_id=user, streak_epoch=epoch
    )
    # Runs BEFORE the epoch (pre-existing history) — must never count.
    for i in range(5):
        await _insert_run(
            db_engine,
            org_id=org,
            pipeline_id=pipeline,
            snapshot_id=snapshot,
            trigger_id=trigger_id,
            status="failed",
            completed_at=epoch - timedelta(hours=1) - timedelta(hours=i),
            classification_value="no_delivery",
        )
    # A delivered run AFTER the epoch extends the boundary past the epoch.
    await _insert_run(
        db_engine,
        org_id=org,
        pipeline_id=pipeline,
        snapshot_id=snapshot,
        trigger_id=trigger_id,
        status="complete",
        completed_at=epoch + timedelta(hours=2),
        classification_value="delivered",
        delivered_pr_urls=["https://github.com/x/y/pull/3"],
    )
    # Two no-deliveries after that delivery.
    for i in range(2):
        await _insert_run(
            db_engine,
            org_id=org,
            pipeline_id=pipeline,
            snapshot_id=snapshot,
            trigger_id=trigger_id,
            status="failed",
            completed_at=epoch + timedelta(hours=3) + timedelta(hours=i),
            classification_value="no_delivery",
        )

    deactivated = await ts._deactivate_trigger_on_no_delivery_streak(
        app_factory, org_id=org, trigger_id=trigger_id, threshold=2, window_cutoff=_now()
    )
    assert deactivated is not None
    assert int(deactivated["streak"]) == 2, "only the post-delivery no-deliveries count"


@pytest.mark.asyncio
async def test_unclassified_run_breaks_fail_closed(
    db_engine: AsyncEngine,
    app_factory: async_sessionmaker[AsyncSession],
    org: uuid.UUID,
    pipeline: uuid.UUID,
    snapshot: uuid.UUID,
    user: uuid.UUID,
) -> None:
    """A terminal run with an 'unclassified' classification record stops the
    walk fail-closed — deactivation can never ride on uncertain evidence."""
    trigger_id = await _seed_ongoing_trigger(
        db_engine, org_id=org, pipeline_id=pipeline, account_id=user, streak_epoch=_now() - timedelta(days=2)
    )
    epoch = _now() - timedelta(days=2)
    for i in range(3):
        await _insert_run(
            db_engine,
            org_id=org,
            pipeline_id=pipeline,
            snapshot_id=snapshot,
            trigger_id=trigger_id,
            status="failed",
            completed_at=epoch + timedelta(hours=1) + timedelta(hours=i),
            classification_value="no_delivery",
        )
    await _insert_run(
        db_engine,
        org_id=org,
        pipeline_id=pipeline,
        snapshot_id=snapshot,
        trigger_id=trigger_id,
        status="complete",
        completed_at=epoch + timedelta(hours=4),
        classification_value="unclassified",
        reason="classifier_error",
    )

    deactivated = await ts._deactivate_trigger_on_no_delivery_streak(
        app_factory, org_id=org, trigger_id=trigger_id, threshold=3, window_cutoff=_now()
    )
    assert deactivated is None, "the unclassified run stops the walk"


@pytest.mark.asyncio
async def test_streak_sql_uses_the_reshaped_index(
    db_engine: AsyncEngine,
    org: uuid.UUID,
    pipeline: uuid.UUID,
    snapshot: uuid.UUID,
    user: uuid.UUID,
) -> None:
    """The engine's per-trigger query is served by the (non-partial)
    ``ix_runs_streak_engine`` index. The old partial predicate
    (``run_classification IS NOT NULL``) could never be implied by the engine's
    ``->> 'value'`` filters (FAR-190 qa FIX 6b)."""
    trigger_id = await _seed_ongoing_trigger(
        db_engine, org_id=org, pipeline_id=pipeline, account_id=user, streak_epoch=_now() - timedelta(days=2)
    )
    for i in range(5):
        await _insert_run(
            db_engine,
            org_id=org,
            pipeline_id=pipeline,
            snapshot_id=snapshot,
            trigger_id=trigger_id,
            status="failed",
            completed_at=_now() - timedelta(hours=2) + timedelta(minutes=i),
            classification_value="no_delivery",
        )
    status_list = ",".join(f"'{s}'" for s in sorted(TERMINAL_STATUSES))
    # Mirror the engine's recency query: ``_STREAK_NEWEST_REASON_SQL`` walks
    # newest-first (``ORDER BY completed_at DESC, id DESC``), which is exactly
    # the keyset ``ix_runs_streak_engine`` (trigger_id, completed_at DESC) is
    # shaped for. With ``enable_seqscan = off`` the planner must use an index;
    # the ORDER BY makes ``ix_runs_streak_engine`` the only one that serves the
    # sort without a Sort node, so the assertion is deterministic on a tiny
    # table (a competing ``ix_runs_trigger_id_created_at`` would need an
    # explicit sort and lose). RESET runs in ``finally`` so a failed EXPLAIN
    # never leaves the connection with seqscan disabled.
    explain_sql = (
        "EXPLAIN SELECT id FROM runs WHERE trigger_id = :tid "
        "AND status IN (__STATUSES__) AND completed_at IS NOT NULL "
        "AND completed_at >= :cutoff "
        "ORDER BY completed_at DESC"
    ).replace("__STATUSES__", status_list)
    async with db_engine.connect() as conn:
        await conn.execute(text("SET enable_seqscan = off"))
        try:
            plan = await conn.execute(
                text(explain_sql),
                {"tid": str(trigger_id), "cutoff": _now() - timedelta(days=2)},
            )
            rows = [r[0] for r in plan.fetchall()]
        finally:
            await conn.execute(text("RESET enable_seqscan"))
    joined = "\n".join(rows)
    assert "ix_runs_streak_engine" in joined, f"expected the streak index in the plan:\n{joined}"
