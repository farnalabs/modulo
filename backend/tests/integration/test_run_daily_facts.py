"""Integration tests for the analytics facts subsystem (run_daily_facts, ADR 020).

Covers: live writer via finalize (idempotent upsert), the fallback path, a
facts-write failure that must NOT break the ledger, fact survival after the
run purge, never-started terminals, DO UPDATE correction, late-finalised runs
vs the day-batch, cross-org backfill, retention day-slice boundary, and the
TERMINAL_STATUSES ⊆ CHECK-constraint invariant.
"""

from __future__ import annotations

import uuid
from datetime import UTC, date, datetime, timedelta

import pytest
import pytest_asyncio
from sqlalchemy import event, text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from modulo.core.analytics import maintenance as maintenance_mod
from modulo.core.analytics.maintenance import backfill_facts, retention_facts, run_maintenance
from modulo.db.models.run import TERMINAL_STATUSES, Run

pytestmark = pytest.mark.integration

# BYPASSRLS role used by the maintenance tests (cross-org scans). The conftest
# FORCE-enables RLS on ``runs``/``org_daily_run_counts`` even for superusers, so
# the cross-org maintenance functions MUST run as a BYPASSRLS role (BYPASSRLS
# bypasses FORCE row-level security) with full table grants.
_BYPASS_ROLE = "analytics_bypass"

_COST_FORMULA = "tokens_input * 0.01"


# ---------------------------------------------------------------------------
# Seed helpers
# ---------------------------------------------------------------------------


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


async def _insert_run(
    session: AsyncSession,
    *,
    org_id: uuid.UUID,
    pipeline_id: uuid.UUID,
    snapshot_id: uuid.UUID,
    status: str = "complete",
    trigger_type: str = "manual",
    started_at: datetime | None = None,
    completed_at: datetime | None = None,
    created_at: datetime | None = None,
    total_cost_usd=None,
    total_tokens: int | None = None,
    run_id: uuid.UUID | None = None,
) -> uuid.UUID:
    from sqlalchemy import insert as sa_insert

    run_id = run_id or uuid.uuid4()
    values: dict[str, object] = {
        "id": run_id,
        "organisation_id": org_id,
        "pipeline_id": pipeline_id,
        "snapshot_id": snapshot_id,
        "trigger_type": trigger_type,
        "status": status,
        "input_hash": uuid.uuid4().hex,
        "langgraph_thread_id": f"thread-{run_id.hex[:16]}",
        "run_number": int(run_id.int % 10**9) + 1,
    }
    values.update(
        {
            col: value
            for col, value in (
                ("started_at", started_at),
                ("completed_at", completed_at),
                ("created_at", created_at),
                ("total_cost_usd", total_cost_usd),
                ("total_tokens", total_tokens),
            )
            if value is not None
        }
    )
    await session.execute(sa_insert(Run).values(**values))
    return run_id


async def _insert_cost_component(session: AsyncSession, org_id: uuid.UUID) -> None:
    await session.execute(
        text(
            "INSERT INTO cost_components (id, organisation_id, name, display_name, kind, rate_usd, "
            "formula, enabled, sort_order) "
            "VALUES (:id, :oid, 'llm_tokens', 'LLM Tokens', 'calculated', NULL, :formula, true, 0)",
        ),
        {
            "id": str(uuid.uuid4()),
            "oid": str(org_id),
            "formula": _COST_FORMULA,
        },
    )


async def _count_facts(session: AsyncSession, run_id: uuid.UUID) -> int:
    result = await session.execute(
        text("SELECT COUNT(*) FROM run_daily_facts WHERE run_id = :rid"),
        {"rid": str(run_id)},
    )
    return int(result.scalar_one())


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture(scope="module")
async def org(db_engine: AsyncEngine) -> uuid.UUID:
    return await _seed_org(db_engine, "AnalyticsFacts")


@pytest_asyncio.fixture(scope="module")
async def user(db_engine: AsyncEngine, org: uuid.UUID) -> uuid.UUID:
    return await _seed_user(db_engine, org, "analytics-facts@test.local")


@pytest_asyncio.fixture(scope="module")
async def pipeline(db_engine: AsyncEngine, org: uuid.UUID, user: uuid.UUID) -> uuid.UUID:
    return await _seed_pipeline(db_engine, org, user, "AnalyticsFacts-Pipeline")


@pytest_asyncio.fixture(scope="module")
async def snapshot(db_engine: AsyncEngine, org: uuid.UUID, pipeline: uuid.UUID) -> uuid.UUID:
    return await _seed_snapshot(db_engine, org, pipeline)


@pytest_asyncio.fixture(scope="module")
async def org_b(db_engine: AsyncEngine) -> uuid.UUID:
    return await _seed_org(db_engine, "AnalyticsFacts-B")


@pytest_asyncio.fixture(scope="module")
async def pipeline_b(db_engine: AsyncEngine, org_b: uuid.UUID, user: uuid.UUID) -> uuid.UUID:
    return await _seed_pipeline(db_engine, org_b, user, "AnalyticsFacts-PipelineB")


@pytest_asyncio.fixture(scope="module")
async def snapshot_b(db_engine: AsyncEngine, org_b: uuid.UUID, pipeline_b: uuid.UUID) -> uuid.UUID:
    return await _seed_snapshot(db_engine, org_b, pipeline_b)


@pytest_asyncio.fixture(scope="module")
async def bypass_engine(migrated_db_url: str, db_engine: AsyncEngine) -> AsyncEngine:
    """Engine whose connections SET ROLE to a dedicated BYPASSRLS role (cross-org scans)."""
    async with db_engine.connect() as conn:
        await conn.execute(text(f'DROP ROLE IF EXISTS "{_BYPASS_ROLE}"'))
        await conn.execute(text(f'CREATE ROLE "{_BYPASS_ROLE}" NOSUPERUSER BYPASSRLS NOLOGIN'))
        await conn.execute(text(f'GRANT USAGE ON SCHEMA public TO "{_BYPASS_ROLE}"'))
        await conn.execute(
            text(f'GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO "{_BYPASS_ROLE}"')
        )
        await conn.execute(text(f'GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO "{_BYPASS_ROLE}"'))
        await conn.commit()

    engine = create_async_engine(migrated_db_url, echo=False, poolclass=NullPool)

    @event.listens_for(engine.sync_engine, "checkout")
    def _set_role_on_checkout(
        dbapi_connection: object,
        _connection_record: object,
        _connection_proxy: object,
    ) -> None:
        cursor = dbapi_connection.cursor()  # type: ignore[attr-defined]
        try:
            cursor.execute(f'SET ROLE "{_BYPASS_ROLE}"')
        finally:
            cursor.close()

    yield engine
    await engine.dispose()
    async with db_engine.connect() as conn:
        await conn.execute(text(f'DROP OWNED BY "{_BYPASS_ROLE}"'))
        await conn.execute(text(f'DROP ROLE IF EXISTS "{_BYPASS_ROLE}"'))
        await conn.commit()


# ---------------------------------------------------------------------------
# Live writer via finalize
# ---------------------------------------------------------------------------


class TestLiveWriter:
    async def test_finalise_twice_yields_one_fact(
        self,
        db_session: AsyncSession,
        org: uuid.UUID,
        pipeline: uuid.UUID,
        snapshot: uuid.UUID,
    ) -> None:
        from modulo.core.cost_controller.finalize import finalize_cost

        run_id = await _insert_run(
            db_session,
            org_id=org,
            pipeline_id=pipeline,
            snapshot_id=snapshot,
            started_at=datetime(2026, 8, 1, 10, 0, tzinfo=UTC),
        )
        await db_session.commit()

        for _ in range(2):
            async with db_session.begin():
                await db_session.execute(
                    text("SELECT set_config('app.organisation_id', :oid, true)"), {"oid": str(org)}
                )
                run = await db_session.get(Run, run_id)
                await finalize_cost(
                    db_session,
                    run_id=run_id,
                    org_id=org,
                    status="complete",
                    segment_node_token_usage=None,
                    segment_completed_node_outputs=None,
                    node_type_map={},
                    is_terminal=True,
                )
                assert run is not None

        async with db_session.begin():
            await db_session.execute(text("SELECT set_config('app.organisation_id', :oid, true)"), {"oid": str(org)})
            assert await _count_facts(db_session, run_id) == 1

    async def test_fallback_path_writes_fact(
        self,
        db_session: AsyncSession,
        org: uuid.UUID,
        pipeline: uuid.UUID,
        snapshot: uuid.UUID,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from modulo.core.cost_controller.finalize import finalize_cost

        run_id = await _insert_run(
            db_session,
            org_id=org,
            pipeline_id=pipeline,
            snapshot_id=snapshot,
            started_at=datetime(2026, 8, 2, 9, 0, tzinfo=UTC),
        )
        await db_session.commit()

        async def _boom(*args, **kwargs):
            raise RuntimeError("component read failed")

        monkeypatch.setattr("modulo.core.cost_controller.finalize.load_live_components", _boom)

        async with db_session.begin():
            await db_session.execute(text("SELECT set_config('app.organisation_id', :oid, true)"), {"oid": str(org)})
            await finalize_cost(
                db_session,
                run_id=run_id,
                org_id=org,
                status="failed",
                segment_node_token_usage={"n1": {"input_tokens": 10, "output_tokens": 5, "total_tokens": 15}},
                segment_completed_node_outputs=None,
                node_type_map={"n1": "agent"},
                error_code="E2B_FAILURE",
                error_detail="sandbox died",
                is_terminal=True,
            )

        async with db_session.begin():
            await db_session.execute(text("SELECT set_config('app.organisation_id', :oid, true)"), {"oid": str(org)})
            row = (
                await db_session.execute(
                    text(
                        "SELECT status, total_cost_usd, total_tokens, trigger_type, run_date "
                        "FROM run_daily_facts WHERE run_id = :rid"
                    ),
                    {"rid": str(run_id)},
                )
            ).first()
        assert row is not None, "fallback path must write a fact"
        assert row[0] == "failed"
        assert row[3] == "manual"
        assert row[4] == date(2026, 8, 2)

    async def test_facts_write_failure_does_not_break_ledger(
        self,
        db_session: AsyncSession,
        org: uuid.UUID,
        pipeline: uuid.UUID,
        snapshot: uuid.UUID,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from modulo.core.cost_controller.finalize import finalize_cost

        run_id = await _insert_run(
            db_session,
            org_id=org,
            pipeline_id=pipeline,
            snapshot_id=snapshot,
            started_at=datetime(2026, 8, 3, 12, 0, tzinfo=UTC),
        )
        await _insert_cost_component(db_session, org)
        await db_session.commit()

        def _boom(*args, **kwargs):
            raise RuntimeError("simulated facts insert failure")

        monkeypatch.setattr("modulo.core.analytics.pg_insert", _boom)

        async with db_session.begin():
            await db_session.execute(text("SELECT set_config('app.organisation_id', :oid, true)"), {"oid": str(org)})
            await finalize_cost(
                db_session,
                run_id=run_id,
                org_id=org,
                status="complete",
                segment_node_token_usage={"n1": {"input_tokens": 1000, "output_tokens": 0, "total_tokens": 1000}},
                segment_completed_node_outputs=None,
                node_type_map={"n1": "agent"},
                is_terminal=True,
            )

        async with db_session.begin():
            await db_session.execute(text("SELECT set_config('app.organisation_id', :oid, true)"), {"oid": str(org)})
            run_status = (
                await db_session.execute(text("SELECT status FROM runs WHERE id = :rid"), {"rid": str(run_id)})
            ).scalar_one()
            ledger_total = (
                await db_session.execute(
                    text(
                        "SELECT total_spend_usd FROM org_daily_run_counts "
                        "WHERE organisation_id = :oid AND team_id IS NULL AND run_date = '2026-08-03'"
                    ),
                    {"oid": str(org)},
                )
            ).scalar_one_or_none()
            facts = await _count_facts(db_session, run_id)

        assert run_status == "complete", "run status must commit despite the facts failure"
        assert ledger_total is not None, "ledger must commit despite the facts failure"
        assert float(ledger_total) > 0, "ledger must commit despite the facts failure"
        assert facts == 0, "the failed facts write must be rolled back"

    async def test_purge_runs_facts_survive(
        self,
        db_session: AsyncSession,
        org: uuid.UUID,
        pipeline: uuid.UUID,
        snapshot: uuid.UUID,
    ) -> None:
        run_id = await _insert_run(
            db_session,
            org_id=org,
            pipeline_id=pipeline,
            snapshot_id=snapshot,
            status="complete",
            started_at=datetime(2026, 7, 1, 8, 0, tzinfo=UTC),
        )
        await db_session.commit()

        async with db_session.begin():
            await db_session.execute(text("SELECT set_config('app.organisation_id', :oid, true)"), {"oid": str(org)})
            run = await db_session.get(Run, run_id)
            from modulo.core.analytics import record_run_facts

            assert run is not None
            await record_run_facts(db_session, run)
        await db_session.commit()

        # Delete the source run — the fact must survive (no FK).
        async with db_session.begin():
            await db_session.execute(text("SELECT set_config('app.organisation_id', :oid, true)"), {"oid": str(org)})
            await db_session.execute(text("DELETE FROM runs WHERE id = :rid"), {"rid": str(run_id)})

        async with db_session.begin():
            await db_session.execute(text("SELECT set_config('app.organisation_id', :oid, true)"), {"oid": str(org)})
            row = (
                await db_session.execute(
                    text("SELECT run_id, organisation_id FROM run_daily_facts WHERE run_id = :rid"),
                    {"rid": str(run_id)},
                )
            ).first()
        assert row is not None, "facts must survive the run purge (no FK on run_id)"
        assert uuid.UUID(str(row[1])) == org

    async def test_never_started_terminal_run(
        self,
        db_session: AsyncSession,
        org: uuid.UUID,
        pipeline: uuid.UUID,
        snapshot: uuid.UUID,
    ) -> None:
        created_at = datetime(2026, 8, 4, 23, 30, tzinfo=UTC)
        run_id = await _insert_run(
            db_session,
            org_id=org,
            pipeline_id=pipeline,
            snapshot_id=snapshot,
            status="cancelled",
            created_at=created_at,
            started_at=None,
            completed_at=None,
        )
        await db_session.commit()

        async with db_session.begin():
            await db_session.execute(text("SELECT set_config('app.organisation_id', :oid, true)"), {"oid": str(org)})
            run = await db_session.get(Run, run_id)
            from modulo.core.analytics import record_run_facts

            assert run is not None
            await record_run_facts(db_session, run)

        async with db_session.begin():
            await db_session.execute(text("SELECT set_config('app.organisation_id', :oid, true)"), {"oid": str(org)})
            row = (
                await db_session.execute(
                    text("SELECT run_date, duration_ms, status FROM run_daily_facts WHERE run_id = :rid"),
                    {"rid": str(run_id)},
                )
            ).first()
        assert row is not None
        # COALESCE(run.started_at, run.created_at) — never-started run dates to created_at.
        assert row[0] == created_at.date()
        assert row[1] is None, "never-started run has no duration"
        assert row[2] == "cancelled"

    async def test_correction_do_update(
        self,
        db_session: AsyncSession,
        org: uuid.UUID,
        pipeline: uuid.UUID,
        snapshot: uuid.UUID,
    ) -> None:
        from modulo.core.analytics import record_run_facts

        run_id = await _insert_run(
            db_session,
            org_id=org,
            pipeline_id=pipeline,
            snapshot_id=snapshot,
            status="failed",
            started_at=datetime(2026, 8, 5, 10, 0, tzinfo=UTC),
        )
        await db_session.commit()

        async with db_session.begin():
            await db_session.execute(text("SELECT set_config('app.organisation_id', :oid, true)"), {"oid": str(org)})
            run = await db_session.get(Run, run_id)
            assert run is not None
            await record_run_facts(db_session, run)

        # Correct the run to complete and re-write — the fact must be updated in place.
        async with db_session.begin():
            await db_session.execute(text("SELECT set_config('app.organisation_id', :oid, true)"), {"oid": str(org)})
            await db_session.execute(
                text("UPDATE runs SET status = 'complete' WHERE id = :rid"),
                {"rid": str(run_id)},
            )
            db_session.expire_all()
            run = await db_session.get(Run, run_id)
            assert run is not None
            await record_run_facts(db_session, run)

        async with db_session.begin():
            await db_session.execute(text("SELECT set_config('app.organisation_id', :oid, true)"), {"oid": str(org)})
            row = (
                await db_session.execute(
                    text("SELECT status FROM run_daily_facts WHERE run_id = :rid"),
                    {"rid": str(run_id)},
                )
            ).first()
            facts = await _count_facts(db_session, run_id)
        assert facts == 1, "the upsert must not create a second fact"
        assert row[0] == "complete", "DO UPDATE must correct the fact's status"


# ---------------------------------------------------------------------------
# Maintenance: backfill / retention
# ---------------------------------------------------------------------------


class TestMaintenance:
    async def test_late_finalised_run_caught_by_next_day_batch(
        self,
        bypass_engine: AsyncEngine,
        org: uuid.UUID,
        pipeline: uuid.UUID,
        snapshot: uuid.UUID,
    ) -> None:
        factory = async_sessionmaker(bypass_engine, expire_on_commit=False)
        day = datetime(2026, 8, 6, 12, 0, tzinfo=UTC).date()
        started = datetime(2026, 8, 6, 9, 0, tzinfo=UTC)

        run_a = uuid.uuid4()
        async with factory() as session, session.begin():
            await _insert_run(
                session,
                org_id=org,
                pipeline_id=pipeline,
                snapshot_id=snapshot,
                status="complete",
                started_at=started,
                run_id=run_a,
            )

        async with factory() as session, session.begin():
            await session.execute(text("SELECT set_config('timezone', 'UTC', true)"))
            await backfill_facts(session, day)

        # A SECOND terminal run finalized AFTER the day-batch must be caught by
        # the NEXT batch (anti-join) without touching the existing fact.
        run_b = uuid.uuid4()
        async with factory() as session, session.begin():
            await _insert_run(
                session,
                org_id=org,
                pipeline_id=pipeline,
                snapshot_id=snapshot,
                status="failed",
                started_at=started + timedelta(hours=1),
                run_id=run_b,
            )

        async with factory() as session, session.begin():
            await session.execute(text("SELECT set_config('timezone', 'UTC', true)"))
            await backfill_facts(session, day)

        async with factory() as session:
            rows = (
                await session.execute(
                    text("SELECT run_id, status FROM run_daily_facts WHERE run_date = :day"),
                    {"day": day},
                )
            ).all()
        run_facts = {uuid.UUID(str(r[0])): r[1] for r in rows}
        assert run_a in run_facts, "the first day-batch must backfill run A"
        assert run_b in run_facts, "the next day-batch must catch the late-finalised run B"
        assert run_facts[run_b] == "failed"

    async def test_cross_org_backfill_keeps_org_attribution(
        self,
        bypass_engine: AsyncEngine,
        org: uuid.UUID,
        pipeline: uuid.UUID,
        snapshot: uuid.UUID,
        org_b: uuid.UUID,
        pipeline_b: uuid.UUID,
        snapshot_b: uuid.UUID,
    ) -> None:
        factory = async_sessionmaker(bypass_engine, expire_on_commit=False)
        day = datetime(2026, 8, 7, 12, 0, tzinfo=UTC).date()

        run_a = uuid.uuid4()
        run_b = uuid.uuid4()
        run_pending = uuid.uuid4()
        async with factory() as session, session.begin():
            await _insert_run(
                session,
                org_id=org,
                pipeline_id=pipeline,
                snapshot_id=snapshot,
                status="complete",
                started_at=datetime(2026, 8, 7, 8, 0, tzinfo=UTC),
                run_id=run_a,
            )
            await _insert_run(
                session,
                org_id=org_b,
                pipeline_id=pipeline_b,
                snapshot_id=snapshot_b,
                status="complete",
                started_at=datetime(2026, 8, 7, 8, 30, tzinfo=UTC),
                run_id=run_b,
            )
            # A NON-terminal run must never be backfilled.
            await _insert_run(
                session,
                org_id=org,
                pipeline_id=pipeline,
                snapshot_id=snapshot,
                status="running",
                started_at=datetime(2026, 8, 7, 8, 45, tzinfo=UTC),
                run_id=run_pending,
            )

        async with factory() as session, session.begin():
            await session.execute(text("SELECT set_config('timezone', 'UTC', true)"))
            await backfill_facts(session, day)

        async with factory() as session:
            rows = (
                await session.execute(
                    text("SELECT run_id, organisation_id FROM run_daily_facts WHERE run_date = :day"),
                    {"day": day},
                )
            ).all()
        facts = {uuid.UUID(str(r[0])): uuid.UUID(str(r[1])) for r in rows}
        assert facts.get(run_a) == org, "fact org must equal the source run's org"
        assert facts.get(run_b) == org_b, "fact org must equal the source run's org"
        assert run_pending not in facts, "non-terminal runs must never be backfilled"

    async def test_run_maintenance_autobegin_false_session_factory(
        self,
        bypass_engine: AsyncEngine,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # saq_worker._make_session_factory() builds sessions with
        # autobegin=False. run_maintenance must begin an explicit transaction
        # BEFORE probing the dialect via session.connection() — a bare probe on
        # an autobegin=False session raises InvalidRequestError, which killed
        # the daily cron before any SQL ran (backfill/reconcile/retention never
        # executed and the non-Postgres no-op path was equally dead).
        async def _noop(session: object, *args: object, **kwargs: object) -> dict[str, object]:
            return {}

        monkeypatch.setattr(maintenance_mod, "backfill_batches", _noop)
        monkeypatch.setattr(maintenance_mod, "reconcile_facts", _noop)
        monkeypatch.setattr(maintenance_mod, "retention_facts", _noop)

        factory = async_sessionmaker(bypass_engine, expire_on_commit=False, autobegin=False)
        result = await run_maintenance(factory)

        assert result["skipped"] is False
        assert result.get("maintenance_failed") is not True, "run_maintenance must complete the maintenance pass"

    async def test_retention_day_slice_boundary(
        self,
        bypass_engine: AsyncEngine,
        org: uuid.UUID,
        pipeline: uuid.UUID,
        snapshot: uuid.UUID,
    ) -> None:
        factory = async_sessionmaker(bypass_engine, expire_on_commit=False)
        cutoff = datetime(2026, 6, 1, 0, 0, tzinfo=UTC).date()

        old_day = datetime(2025, 5, 31, 9, 0, tzinfo=UTC)
        boundary_day = datetime(2026, 6, 1, 9, 0, tzinfo=UTC)
        old_run = uuid.uuid4()
        boundary_run = uuid.uuid4()
        async with factory() as session, session.begin():
            await _insert_run(
                session,
                org_id=org,
                pipeline_id=pipeline,
                snapshot_id=snapshot,
                status="complete",
                started_at=old_day,
                run_id=old_run,
            )
            await _insert_run(
                session,
                org_id=org,
                pipeline_id=pipeline,
                snapshot_id=snapshot,
                status="complete",
                started_at=boundary_day,
                run_id=boundary_run,
            )

        async with factory() as session, session.begin():
            await session.execute(text("SELECT set_config('timezone', 'UTC', true)"))
            await backfill_facts(session, old_day.date())
            await backfill_facts(session, boundary_day.date())

        async with factory() as session, session.begin():
            result = await retention_facts(session, cutoff=cutoff)

        assert result["retention_deleted"] >= 1
        async with factory() as session:
            remaining = set((await session.execute(text("SELECT run_id FROM run_daily_facts"))).scalars().all())
        assert old_run not in remaining, "facts older than the cutoff must be deleted"
        assert boundary_run in remaining, "facts exactly on the cutoff boundary must be kept"


# ---------------------------------------------------------------------------
# FAR-102: live-writer enrichment
# ---------------------------------------------------------------------------


class TestLiveWriterEnrichment:
    """record_run_facts must snapshot the FAR-102 enrichment columns."""

    async def _seed_snapshot_with_graph(
        self,
        db_engine: AsyncEngine,
        org_id: uuid.UUID,
        pipeline_id: uuid.UUID,
    ) -> uuid.UUID:
        # The shared ``pipeline`` fixture already holds a version-1 snapshot, so
        # use version 2 (unique per pipeline_id) for the graph-bearing snapshot.
        snapshot_id = uuid.uuid4()
        async with db_engine.connect() as conn, conn.begin():
            await conn.execute(
                text(
                    "INSERT INTO pipeline_snapshots (id, pipeline_id, organisation_id, "
                    "snapshot_version, graph_json, connector_bindings_json, schema_pins_json, "
                    "prompt_pins_json, model_backend_pins_json, run_context_defaults, config_json) "
                    "VALUES (:id, :pid, :oid, 2, :gjson, '[]'::json, "
                    "'[]'::json, '[]'::json, '[]'::json, '{}'::json, '{}'::json)"
                ),
                {
                    "id": str(snapshot_id),
                    "pid": str(pipeline_id),
                    "oid": str(org_id),
                    "gjson": (
                        '{"nodes": [{"id": "n1", "node_type": "agent", "timeout_seconds": 120}, '
                        '{"id": "n2", "node_type": "sandbox_agent", "timeout_seconds": 600}]}'
                    ),
                },
            )
        return snapshot_id

    async def test_record_run_facts_snapshots_enriched_columns(
        self,
        db_session: AsyncSession,
        db_engine: AsyncEngine,
        org: uuid.UUID,
        pipeline: uuid.UUID,
        snapshot: uuid.UUID,
    ) -> None:
        from modulo.core.analytics import record_run_facts

        # A REAL parent run in the same org — runs.parent_run_id is FK'd with a
        # same-organisation trigger, so a synthetic UUID would be rejected.
        parent_run_id = await _insert_run(
            db_session,
            org_id=org,
            pipeline_id=pipeline,
            snapshot_id=snapshot,
            status="complete",
            started_at=datetime(2026, 8, 7, 9, 0, tzinfo=UTC),
        )
        await db_session.commit()

        snapshot_id = await self._seed_snapshot_with_graph(db_engine, org, pipeline)
        run_id = uuid.uuid4()
        async with db_session.begin():
            await db_session.execute(
                text(
                    "INSERT INTO runs (id, organisation_id, pipeline_id, snapshot_id, trigger_type, "
                    "status, input_hash, langgraph_thread_id, run_number, started_at, completed_at, "
                    "dispatched_at, heartbeat_at, claim_count, cancellation_requested, error_code, "
                    "outputs_json, rate_limit_key, parent_run_id, dispatcher) "
                    "VALUES (:id, :oid, :pid, :sid, 'cron', 'failed', :hash, :thread, 42, "
                    ":started, :completed, :dispatched, :heartbeat, 7, true, 'executor_stalled', "
                    ":outjson, 'rl:key', :parent, 'saq')"
                ),
                {
                    "id": str(run_id),
                    "oid": str(org),
                    "pid": str(pipeline),
                    "sid": str(snapshot_id),
                    "hash": uuid.uuid4().hex,
                    "thread": f"thread-enrich-{run_id.hex[:8]}",
                    "started": datetime(2026, 8, 7, 10, 0, tzinfo=UTC),
                    "dispatched": datetime(2026, 8, 7, 10, 0, 15, tzinfo=UTC),
                    "completed": datetime(2026, 8, 7, 11, 0, 0, tzinfo=UTC),
                    "heartbeat": datetime(2026, 8, 7, 10, 59, 0, tzinfo=UTC),
                    "outjson": '{"node_a": {"result": "ok"}}',
                    "parent": str(parent_run_id),
                },
            )
        async with db_session.begin():
            await db_session.execute(text("SELECT set_config('app.organisation_id', :oid, true)"), {"oid": str(org)})
            run = await db_session.get(Run, run_id)
            assert run is not None
            await record_run_facts(db_session, run)

        async with db_session.begin():
            await db_session.execute(text("SELECT set_config('app.organisation_id', :oid, true)"), {"oid": str(org)})
            row = (
                await db_session.execute(
                    text(
                        "SELECT error_code, claim_count, queue_wait_ms, final_idle_ms, "
                        "cancellation_requested, dispatcher, node_count, sandbox_agent_node_count, "
                        "max_node_timeout_seconds, parent_run_id, snapshot_id, run_number, "
                        "output_bytes, rate_limited "
                        "FROM run_daily_facts WHERE run_id = :rid"
                    ),
                    {"rid": str(run_id)},
                )
            ).first()
        assert row is not None
        assert row[0] == "executor_stalled"
        assert row[1] == 7
        assert row[2] == 15000, "queue_wait_ms = dispatched - started"
        assert row[3] == 60000, "final_idle_ms = completed - heartbeat"
        assert row[4] is True
        assert row[5] == "saq"
        assert row[6] == 2, "node_count from the snapshot graph_json"
        assert row[7] == 1, "sandbox_agent_node_count from the snapshot graph_json"
        assert row[8] == 600, "max_node_timeout_seconds from the snapshot graph_json"
        assert uuid.UUID(str(row[9])) == parent_run_id
        assert uuid.UUID(str(row[10])) == snapshot_id
        assert row[11] == 42
        assert row[12] is not None, "output_bytes from outputs_json"
        assert row[12] > 0, "output_bytes from outputs_json"
        assert row[13] is True, "rate_limited from rate_limit_key"

    async def test_record_run_facts_malformed_graph_is_null_safe(
        self,
        db_session: AsyncSession,
        org: uuid.UUID,
        pipeline: uuid.UUID,
        snapshot: uuid.UUID,
    ) -> None:
        """A snapshot with an empty/malformed graph_json must not crash the writer.

        The shared ``snapshot`` fixture carries ``{}`` graph_json — no ``nodes``
        list — so the graph-derived fields degrade to ``(0, 0, None)``.
        """
        from modulo.core.analytics import record_run_facts

        run_id = uuid.uuid4()
        async with db_session.begin():
            await db_session.execute(
                text(
                    "INSERT INTO runs (id, organisation_id, pipeline_id, snapshot_id, trigger_type, "
                    "status, input_hash, langgraph_thread_id, run_number, started_at, completed_at) "
                    "VALUES (:id, :oid, :pid, :sid, 'manual', 'complete', :hash, :thread, 5, "
                    ":started, :completed)"
                ),
                {
                    "id": str(run_id),
                    "oid": str(org),
                    "pid": str(pipeline),
                    "sid": str(snapshot),
                    "hash": uuid.uuid4().hex,
                    "thread": f"thread-malformed-{run_id.hex[:8]}",
                    "started": datetime(2026, 8, 7, 9, 0, tzinfo=UTC),
                    "completed": datetime(2026, 8, 7, 9, 30, 0, tzinfo=UTC),
                },
            )
        async with db_session.begin():
            await db_session.execute(text("SELECT set_config('app.organisation_id', :oid, true)"), {"oid": str(org)})
            run = await db_session.get(Run, run_id)
            assert run is not None
            await record_run_facts(db_session, run)

        async with db_session.begin():
            await db_session.execute(text("SELECT set_config('app.organisation_id', :oid, true)"), {"oid": str(org)})
            row = (
                await db_session.execute(
                    text(
                        "SELECT node_count, sandbox_agent_node_count, max_node_timeout_seconds "
                        "FROM run_daily_facts WHERE run_id = :rid"
                    ),
                    {"rid": str(run_id)},
                )
            ).first()
        assert row is not None
        assert row[0] == 0, "malformed graph → node_count 0, never a crash"
        assert row[1] == 0
        assert row[2] is None, "malformed graph → max timeout None, never a crash"


# ---------------------------------------------------------------------------
# Invariant: TERMINAL_STATUSES ⊆ runs.status CHECK constraint
# ---------------------------------------------------------------------------


class TestTerminalStatusesInvariant:
    async def test_terminal_statuses_subset_of_check_constraint(self, db_engine: AsyncEngine) -> None:
        import re

        async with db_engine.connect() as conn:
            result = await conn.execute(
                text(
                    "SELECT pg_get_constraintdef(oid) FROM pg_constraint "
                    "WHERE conrelid = 'public.runs'::regclass AND conname = 'ck_runs_status'"
                )
            )
            definition = result.scalar_one()
        check_values = set(re.findall(r"'([^']+)'", definition))
        for status in TERMINAL_STATUSES:
            assert status in check_values, f"TERMINAL_STATUSES {status!r} not in runs.status CHECK constraint"
