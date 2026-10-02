"""FAR-1406: the deployed ``ck_runs_cancel_reason`` divergence, end to end.

PR #1025 (FAR-1104) renamed the HITL cancel-reason vocabulary by editing the
SHIPPED migration 0260 in place. A database that had already run 0260 keeps
the PRE-rename CHECK text (``hitl_gate_expired`` / ``hitl_gate_missing``),
while a fresh checkout — every CI/integration database — replays the renamed
text. Replaying the migration chain therefore NEVER reproduces the deployed
state, so this module builds it deliberately:

* a private database at ``0274`` (the chain head before this fix) with the
  constraint re-installed exactly as the deployed pre-rename 0260 created it;
* the FAR-648 terminalizer is then driven against that constraint — it is
  REJECTED, which is the live staging failure (every tick dies on
  ``violates check constraint "ck_runs_cancel_reason"``, so an expired HITL
  gate can never be terminalized);
* the REAL alembic upgrade of ``0275_run_cancel_reason_vocabulary`` runs, and
  the same sweep now terminalizes the gate: ``cancelled`` /
  ``hitl_review_expired`` / ``cancelled_by='system'``;
* legacy ``hitl_gate_*`` rows are reconciled to the current vocabulary, the
  constraint is VALIDATED over it, and the old values are refused afterwards.

Seeders are shared with ``test_org_sandbox_capacity`` (raw-SQL org/pipeline/
run helpers) and ``test_hitl_review_window_terminalize`` (the claim seeder);
the private-database fixture mirrors ``test_policy_gate_pin_migration``.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from urllib.parse import urlparse, urlunparse

import pytest
import pytest_asyncio
from alembic import command
from alembic.config import Config
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from modulo.db.rls import set_rls_org
from tests.integration.test_hitl_review_window_terminalize import _seed_claim
from tests.integration.test_org_sandbox_capacity import (
    _SANDBOX_GRAPH,
    _run_cancel_reason,
    _run_state,
    _seed_org_account,
    _seed_pipeline,
    _seed_run,
    _seed_snapshot,
)

pytestmark = [pytest.mark.integration]

BACKEND_ROOT = Path(__file__).parents[2]  # backend/
MIGRATION_REV = "0275_run_cancel_reason_vocabulary"
PREV_REV = "0274_policy_gate_pin_fingerprint_operator_control"

# The constraint EXACTLY as the deployed pre-rename 0260 created it — the
# world FAR-1104's in-place edit of that shipped migration never repaired.
_DEPLOYED_OLD_CHECK = (
    "ALTER TABLE public.runs ADD CONSTRAINT ck_runs_cancel_reason CHECK ("
    "cancel_reason IS NULL OR "
    "cancel_reason IN ('user_requested', 'agent_requested', 'hitl_gate_expired', 'hitl_gate_missing'))"
)

_GRACE_SECONDS = 3600


def _alembic_config(db_url: str) -> Config:
    config = Config(BACKEND_ROOT / "alembic.ini")
    config.set_main_option("sqlalchemy.url", db_url)
    config.set_main_option("script_location", str(BACKEND_ROOT / "src" / "modulo" / "db" / "migrations"))
    config.config_file_name = None
    return config


def _swap_db_name(db_url: str, new_db: str) -> str:
    parsed = urlparse(db_url)
    return urlunparse(parsed._replace(path=f"/{new_db}"))


def _alembic_cmd(iso_url: str, cmd: str, target: str) -> None:
    """Run an alembic command against the isolated DB via the Python API.

    Deliberately does NOT inject ``cmd_opts`` (the FAR-967 F2 shape): env.py
    infers direction from the active EnvironmentContext instead.
    """
    cfg = _alembic_config(iso_url)
    assert getattr(cfg, "cmd_opts", None) is None, "Python-API invocation must carry no cmd_opts"
    getattr(command, cmd)(cfg, target)


@pytest_asyncio.fixture
async def isolated_db_url(db_url: str, monkeypatch: pytest.MonkeyPatch):
    """A fresh, private Postgres database migrated only up to ``PREV_REV``.

    ``env.py`` resolves the target DB from ``DATABASE_URL`` /
    ``DATABASE_ADMIN_URL``, so both are pinned for every alembic call."""
    admin_engine = create_async_engine(db_url, poolclass=NullPool, execution_options={"isolation_level": "AUTOCOMMIT"})
    db_name = f"m0275_iso_{uuid.uuid4().hex[:10]}"
    async with admin_engine.connect() as conn:
        await conn.execute(text(f'CREATE DATABASE "{db_name}" WITH TEMPLATE template0'))
    await admin_engine.dispose()

    iso_url = _swap_db_name(db_url, db_name)
    engine = create_async_engine(iso_url, poolclass=NullPool)
    async with engine.connect() as conn:
        await conn.execute(
            text("CREATE TABLE IF NOT EXISTS alembic_version (version_num VARCHAR(255) NOT NULL PRIMARY KEY)")
        )
    await engine.dispose()

    monkeypatch.setenv("DATABASE_URL", iso_url)
    monkeypatch.setenv("DATABASE_ADMIN_URL", iso_url)
    with pytest.MonkeyPatch().context() as mp:
        mp.setenv("DATABASE_URL", iso_url)
        mp.setenv("DATABASE_ADMIN_URL", iso_url)
        _alembic_cmd(iso_url, "upgrade", PREV_REV)

    yield iso_url

    admin_engine = create_async_engine(db_url, poolclass=NullPool, execution_options={"isolation_level": "AUTOCOMMIT"})
    async with admin_engine.connect() as conn:
        await conn.execute(text(f'DROP DATABASE IF EXISTS "{db_name}"'))
    await admin_engine.dispose()


async def _migrate_to_target(iso_url: str) -> None:
    """Apply the REAL alembic upgrade of 0275 against the isolated database."""
    with pytest.MonkeyPatch().context() as mp:
        mp.setenv("DATABASE_URL", iso_url)
        mp.setenv("DATABASE_ADMIN_URL", iso_url)
        _alembic_cmd(iso_url, "upgrade", MIGRATION_REV)


async def _install_deployed_constraint(engine: AsyncEngine) -> None:
    """Reproduce the deployed divergence: swap in the pre-rename CHECK.

    A database migrated from the CURRENT checkout already carries 0260's
    renamed text, so the deployed world (original 0260 text, never widened by
    PR #1025) has to be installed deliberately before it can be repaired.
    """
    async with engine.begin() as conn:
        await conn.execute(text("ALTER TABLE public.runs DROP CONSTRAINT IF EXISTS ck_runs_cancel_reason"))
        await conn.execute(text(_DEPLOYED_OLD_CHECK))


async def _sweep(engine: AsyncEngine, org_id: uuid.UUID) -> int:
    """Run the FAR-648 terminalizer directly; returns the terminalized count."""
    from modulo.core.cron_helpers import _terminalize_expired_hitl_reviews

    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as session, session.begin():
        await set_rls_org(session, org_id)
        return len(await _terminalize_expired_hitl_reviews(session, org_id, grace_seconds=_GRACE_SECONDS))


async def _seed_awaiting_run_with_expired_claim(
    engine: AsyncEngine,
    org_label: str,
    pipeline_label: str,
) -> tuple[uuid.UUID, uuid.UUID]:
    """Org + account + pipeline + snapshot + ``awaiting_human`` run whose ONLY
    gate is expired, unclaimed and undecided (past both the deadline stamp and
    the legacy ``expires_at + grace`` arithmetic)."""
    org_id, user_id = await _seed_org_account(engine, org_label, cap=None)
    pipeline_id = await _seed_pipeline(engine, org_id, pipeline_label, user_id)
    snapshot_id = await _seed_snapshot(engine, org_id, pipeline_id, _SANDBOX_GRAPH)
    run_id = await _seed_run(engine, org_id, pipeline_id, snapshot_id, status="awaiting_human")
    await _seed_claim(
        engine,
        org_id,
        run_id,
        pipeline_id,
        "review-1",
        expires_at=datetime.now(UTC) - timedelta(hours=2),
        terminalize_at=datetime.now(UTC) - timedelta(minutes=5),
    )
    return org_id, run_id


async def _seed_legacy_cancelled_run(engine: AsyncEngine, org_label: str, legacy_reason: str) -> uuid.UUID:
    """A cancelled run carrying a PRE-rename reason (valid under the deployed
    CHECK, invalid under the current one — reconciliation must rewrite it)."""
    org_id, user_id = await _seed_org_account(engine, org_label, cap=None)
    pipeline_id = await _seed_pipeline(engine, org_id, f"{org_label}-pipe", user_id)
    snapshot_id = await _seed_snapshot(engine, org_id, pipeline_id, _SANDBOX_GRAPH)
    run_id = await _seed_run(engine, org_id, pipeline_id, snapshot_id, status="cancelled")
    async with engine.begin() as conn:
        await conn.execute(
            text("UPDATE runs SET cancel_reason = :reason WHERE id = :rid"),
            {"reason": legacy_reason, "rid": str(run_id)},
        )
    return run_id


async def _cancel_reason_of(engine: AsyncEngine, run_id: uuid.UUID) -> str | None:
    async with engine.connect() as conn:
        row = await conn.execute(text("SELECT cancel_reason FROM runs WHERE id = :rid"), {"rid": str(run_id)})
        return row.scalar_one()


async def _constraint_state(engine: AsyncEngine) -> tuple[str, bool]:
    """``(pg_get_constraintdef, convalidated)`` for ``ck_runs_cancel_reason``."""
    async with engine.connect() as conn:
        row = (
            await conn.execute(
                text(
                    "SELECT pg_get_constraintdef(oid), convalidated FROM pg_constraint "
                    "WHERE conname = 'ck_runs_cancel_reason' AND conrelid = 'public.runs'::regclass"
                )
            )
        ).first()
    assert row is not None, "ck_runs_cancel_reason must exist after the migration"
    return row[0], bool(row[1])


class TestDeployedConstraintVsSweep:
    async def test_expired_gate_is_rejected_then_terminalizes_after_0275(
        self,
        isolated_db_url: str,
    ) -> None:
        """The FAR-1406 failure and its fix, in one sequence against real
        Postgres: the sweep dies on the deployed CHECK, the real alembic
        upgrade of 0275 widens it, and the same sweep then terminalizes the
        expired gate (``cancelled`` / ``hitl_review_expired`` / ``system``).

        Without 0275 this test FAILS at step 3: with the revision gone the
        chain head is still 0274, env.py's at-head fast-path skips the upgrade
        run entirely, the deployed constraint survives, and the second sweep
        dies on ``ck_runs_cancel_reason`` — the pre-migration half is the live
        staging defect reproduced verbatim either way.
        """
        engine = create_async_engine(isolated_db_url, poolclass=NullPool)
        try:
            await _install_deployed_constraint(engine)
            org_id, run_id = await _seed_awaiting_run_with_expired_claim(engine, "F1406ExpiredOrg", "PipeF1406")

            # 1. Against the DEPLOYED constraint the sweep's write is refused —
            #    the exact per-tick failure observed on staging.
            with pytest.raises(IntegrityError, match="ck_runs_cancel_reason"):
                await _sweep(engine, org_id)
            status, _code = await _run_state(engine, org_id, run_id)
            assert status == "awaiting_human", "the rejected UPDATE must leave the run untouched"

            # 2. The REAL migration repairs the constraint (and reconciles rows).
            await _migrate_to_target(isolated_db_url)

            # 3. The same sweep now terminalizes the expired gate.
            count = await _sweep(engine, org_id)
            assert count == 1

            status, code = await _run_state(engine, org_id, run_id)
            assert status == "cancelled"
            assert code == "hitl_review_expired"
            reason, actor = await _run_cancel_reason(engine, org_id, run_id)
            assert reason == "hitl_review_expired"
            assert actor == "system"
        finally:
            await engine.dispose()


class TestReconciliationAndWidenedCheck:
    async def test_legacy_rows_reconcile_and_widened_check_validates(
        self,
        isolated_db_url: str,
    ) -> None:
        """0275 rewrites deployed ``hitl_gate_*`` rows to the current
        vocabulary BEFORE re-adding the CHECK (the new values are exactly what
        the stale CHECK forbids), VALIDATEs the widened constraint, and the
        old values are refused afterwards."""
        engine = create_async_engine(isolated_db_url, poolclass=NullPool)
        try:
            await _install_deployed_constraint(engine)
            expired_run = await _seed_legacy_cancelled_run(engine, "F1406LegacyExpired", "hitl_gate_expired")
            missing_run = await _seed_legacy_cancelled_run(engine, "F1406LegacyMissing", "hitl_gate_missing")

            await _migrate_to_target(isolated_db_url)

            async with engine.connect() as conn:
                version = (await conn.execute(text("SELECT version_num FROM alembic_version"))).scalar_one()
            assert version == MIGRATION_REV

            expired_reason = await _cancel_reason_of(engine, expired_run)
            missing_reason = await _cancel_reason_of(engine, missing_run)
            assert expired_reason == "hitl_review_expired"
            assert missing_reason == "hitl_review_missing"

            definition, convalidated = await _constraint_state(engine)
            assert convalidated is True, "0275 must VALIDATE the re-added CHECK"
            assert "hitl_review_expired" in definition
            assert "hitl_review_missing" in definition
            assert "hitl_gate_expired" not in definition
            assert "hitl_gate_missing" not in definition

            # The stale vocabulary is now refused (the widened CHECK is live).
            with pytest.raises(IntegrityError, match="ck_runs_cancel_reason"):
                async with engine.begin() as conn:
                    await conn.execute(
                        text("UPDATE runs SET cancel_reason = 'hitl_gate_expired' WHERE id = :rid"),
                        {"rid": str(expired_run)},
                    )
        finally:
            await engine.dispose()
