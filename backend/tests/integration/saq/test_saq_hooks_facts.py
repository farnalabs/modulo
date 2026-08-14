"""Integration test — SAQ after_process task_failure → run_daily_facts (FAR-161).

Wires the REAL ``saq_hooks.after_process`` hook (with its own engine built from
``get_settings().database_url``) to a real testcontainers Postgres: seeds a
running run, fires a FAILED ``execute_run`` job carrying run_id/org_id/
claim_token + job.error, and asserts the runs row flips to failed/task_failure
AND a compensating ``RunDailyFact`` row exists (status='failed',
error_code='task_failure', error_detail truncated, terminal completed_at).

Also asserts the separate-session RLS contract (``set_rls_org`` is invoked on
the facts session, not just the mark session) and the fail-open contract (a
facts-session write failure never propagates out of ``after_process`` and never
rolls back the run's failed transition).
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any

import pytest
import pytest_asyncio
from saq import Status
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from modulo.core.error_tracking import saq_hooks
from modulo.settings import get_settings

pytestmark = pytest.mark.integration


# ---------------------------------------------------------------------------
# Seed helpers (mirror tests/integration/test_run_daily_facts.py)
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


async def _insert_running_run(
    db_session: AsyncSession,
    *,
    org_id: uuid.UUID,
    pipeline_id: uuid.UUID,
    snapshot_id: uuid.UUID,
    claim_token: str,
) -> uuid.UUID:
    run_id = uuid.uuid4()
    await db_session.execute(
        text(
            "INSERT INTO runs (id, organisation_id, pipeline_id, snapshot_id, trigger_type, "
            "status, input_hash, langgraph_thread_id, run_number, started_at, created_at, "
            "claim_token) "
            "VALUES (:id, :oid, :pid, :sid, 'manual', 'running', :hash, :thread, :run_number, "
            ":started, :created, :tok)"
        ),
        {
            "id": str(run_id),
            "oid": str(org_id),
            "pid": str(pipeline_id),
            "sid": str(snapshot_id),
            "hash": uuid.uuid4().hex,
            "thread": f"thread-{run_id.hex[:16]}",
            "run_number": int(run_id.int % 10**9) + 1,
            "started": datetime(2026, 8, 10, 10, 30, tzinfo=UTC),
            "created": datetime(2026, 8, 10, 10, 20, tzinfo=UTC),
            "tok": claim_token,
        },
    )
    return run_id


def _failed_job(run_id: uuid.UUID, org_id: uuid.UUID, claim_token: str, error: str) -> SimpleNamespace:
    return SimpleNamespace(
        function="modulo.core.saq_worker.execute_run",
        status=Status.FAILED,
        error=error,
        kwargs={"run_id": str(run_id), "org_id": str(org_id), "claim_token": claim_token},
    )


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture(scope="module")
async def org(db_engine: AsyncEngine) -> uuid.UUID:
    return await _seed_org(db_engine, "SaqHooksFacts")


@pytest_asyncio.fixture(scope="module")
async def user(db_engine: AsyncEngine, org: uuid.UUID) -> uuid.UUID:
    return await _seed_user(db_engine, org, "saq-hooks-facts@test.local")


@pytest_asyncio.fixture(scope="module")
async def pipeline(db_engine: AsyncEngine, org: uuid.UUID, user: uuid.UUID) -> uuid.UUID:
    return await _seed_pipeline(db_engine, org, user, "SaqHooksFacts-Pipeline")


@pytest_asyncio.fixture(scope="module")
async def snapshot(db_engine: AsyncEngine, org: uuid.UUID, pipeline: uuid.UUID) -> uuid.UUID:
    return await _seed_snapshot(db_engine, org, pipeline)


@pytest.fixture
def saq_hooks_engine(migrated_db_url: str, monkeypatch: pytest.MonkeyPatch) -> Any:
    """Point ``saq_hooks``'s process-global engine at the testcontainers DB.

    ``saq_hooks._get_engine()`` builds a fresh engine from
    ``get_settings().database_url`` when ``_ENGINE`` is None, so forcing
    ``_ENGINE = None`` after pointing settings at the migrated test DB makes the
    ``after_process`` hook write to the real test Postgres.
    """
    monkeypatch.setenv("DATABASE_URL", migrated_db_url)
    monkeypatch.setenv("SECRET_KEY", "a" * 40)
    monkeypatch.setenv("FERNET_KEY", "vK-xU7GqHLflg_GqzJ1FqWI7pHWoHSIyukf4wx-tMHI=")
    get_settings.cache_clear()
    saved = saq_hooks._ENGINE
    saq_hooks._ENGINE = None
    yield migrated_db_url
    saq_hooks._ENGINE = saved
    get_settings.cache_clear()


# ---------------------------------------------------------------------------
# after_process → task_failure → run_daily_facts
# ---------------------------------------------------------------------------


class TestAfterProcessTaskFailureFacts:
    async def test_task_failure_flips_run_and_writes_fact(
        self,
        db_session: AsyncSession,
        org: uuid.UUID,
        pipeline: uuid.UUID,
        snapshot: uuid.UUID,
        saq_hooks_engine: Any,
    ) -> None:
        run_id = await _insert_running_run(
            db_session,
            org_id=org,
            pipeline_id=pipeline,
            snapshot_id=snapshot,
            claim_token="tok-abc",
        )
        await db_session.commit()

        error = "x" * 7000
        await saq_hooks.after_process({"job": _failed_job(run_id, org, "tok-abc", error)})

        async with db_session.begin():
            row = (
                await db_session.execute(
                    text("SELECT status, error_code, error_detail, completed_at FROM runs WHERE id = :rid"),
                    {"rid": str(run_id)},
                )
            ).first()
        assert row is not None
        assert row[0] == "failed"
        assert row[1] == "task_failure"
        assert row[2] is not None
        assert len(row[2]) == 5000, "error_detail truncated to the 5000-codepoint column"
        assert row[3] is not None, "completed_at is terminal"

        async with db_session.begin():
            fact = (
                await db_session.execute(
                    text("SELECT status, error_code, completed_at FROM run_daily_facts WHERE run_id = :rid"),
                    {"rid": str(run_id)},
                )
            ).first()
        assert fact is not None, "after_process must write a compensating RunDailyFact row"
        assert fact[0] == "failed"
        assert fact[1] == "task_failure"
        assert fact[2] is not None, "the fact's completed_at is terminal"

    async def test_separate_session_rls_invoked(
        self,
        db_session: AsyncSession,
        org: uuid.UUID,
        pipeline: uuid.UUID,
        snapshot: uuid.UUID,
        saq_hooks_engine: Any,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """set_rls_org is invoked on BOTH the mark session and the separate
        facts session — the compensating write is org-scoped on its own
        connection (P6')."""
        import modulo.db.rls as rls_mod

        run_id = await _insert_running_run(
            db_session,
            org_id=org,
            pipeline_id=pipeline,
            snapshot_id=snapshot,
            claim_token="tok-abc",
        )
        await db_session.commit()

        calls: list[uuid.UUID] = []
        real = rls_mod.set_rls_org

        async def _spy(session: object, org_id: uuid.UUID) -> None:
            calls.append(org_id)
            await real(session, org_id)  # type: ignore[misc]

        monkeypatch.setattr("modulo.db.rls.set_rls_org", _spy)
        await saq_hooks.after_process({"job": _failed_job(run_id, org, "tok-abc", "boom")})

        # mark session + facts session each set the org context.
        assert len(calls) == 2, "the facts write must run in its own RLS-scoped session"
        assert all(c == org for c in calls)

    async def test_facts_session_failure_does_not_propagate(
        self,
        db_session: AsyncSession,
        org: uuid.UUID,
        pipeline: uuid.UUID,
        snapshot: uuid.UUID,
        saq_hooks_engine: Any,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Fail-open: a facts-session write failure must not escape after_process
        and must not roll back the already-committed failed transition."""
        run_id = await _insert_running_run(
            db_session,
            org_id=org,
            pipeline_id=pipeline,
            snapshot_id=snapshot,
            claim_token="tok-abc",
        )
        await db_session.commit()

        async def _boom(*_args: object, **_kwargs: object) -> None:
            raise RuntimeError("facts boom")

        monkeypatch.setattr("modulo.core.analytics.record_run_facts", _boom)
        await saq_hooks.after_process({"job": _failed_job(run_id, org, "tok-abc", "boom")})  # must not raise

        async with db_session.begin():
            status = (
                await db_session.execute(text("SELECT status FROM runs WHERE id = :rid"), {"rid": str(run_id)})
            ).scalar_one()
        assert status == "failed", "the run must still be marked failed despite the facts failure"
