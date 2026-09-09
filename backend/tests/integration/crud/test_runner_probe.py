"""Two-org RLS isolation integration tests for the runner probe cache + apply
template (qa F13, following test_environment_profiles.py's pattern).

(a) probe-cache rows are org-scoped: an org-B context sees nothing of
    org-A's rows;
(b) the runners status aggregate reads only the caller org's cache rows
    (``list_runner_probe_cache`` is the endpoint's read primitive);
(c) apply-template against a cross-org row finds nothing under RLS → the
    crud helper returns None → the route 404s.
"""

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select, text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from modulo.db.bundled_runner_template import BUNDLED_RUNNER_IMAGE_REF, TEMPLATE_CONFIG_JSON
from modulo.db.crud.environment_profile import apply_bundled_runner_template
from modulo.db.crud.runner_probe import (
    PROBE_RETENTION_SECONDS,
    list_runner_probe_cache,
    prune_stale_runner_probe_rows,
    upsert_runner_probe_cache,
)
from modulo.db.models.environment_profile import EnvironmentProfile
from modulo.db.models.runner_probe_cache import RunnerProbeCache
from modulo.db.rls import set_rls_org

pytestmark = pytest.mark.integration


async def _seed_org(db_engine: AsyncEngine, org_id: uuid.UUID, slug: str) -> None:
    async with db_engine.connect() as conn, conn.begin():
        await conn.execute(
            text("INSERT INTO organisations (id, name, slug, settings_json) VALUES (:id, :name, :slug, '{}'::json)"),
            {
                "id": str(org_id),
                "name": f"Runner Probe Org {slug}",
                "slug": f"rprobe-{slug}-{org_id.hex[:8]}-{uuid.uuid4().hex[:6]}",
            },
        )


async def _seed_probe_row(
    db_engine: AsyncEngine,
    *,
    org_id: uuid.UUID,
    machine_id: str,
    probed_at: datetime,
) -> None:
    async with db_engine.connect() as conn, conn.begin():
        await conn.execute(
            text(
                "INSERT INTO runner_probe_cache "
                "(id, organisation_id, machine_id, engine_reachable, images_present, "
                "image_checks_json, engine_info_json, probe_error, probed_at) "
                "VALUES (:id, :org, :machine, true, true, '{}'::json, '{}'::json, NULL, :probed_at)"
            ),
            {
                "id": str(uuid.uuid4()),
                "org": str(org_id),
                "machine": machine_id,
                "probed_at": probed_at,
            },
        )


class TestProbeCacheRlsIsolation:
    async def test_probe_cache_rows_are_org_scoped(self, db_engine: AsyncEngine, app_engine: AsyncEngine) -> None:
        """(a) org-B's context sees nothing of org-A's probe rows."""
        org_a = uuid.uuid4()
        org_b = uuid.uuid4()
        await _seed_org(db_engine, org_a, "a")
        await _seed_org(db_engine, org_b, "b")
        probe_at = datetime.now(UTC)
        await _seed_probe_row(db_engine, org_id=org_a, machine_id="machine-1", probed_at=probe_at)
        await _seed_probe_row(db_engine, org_id=org_b, machine_id="machine-1", probed_at=probe_at)

        # app_engine runs as a non-superuser role, so the RLS policy filters.
        factory = async_sessionmaker(app_engine, expire_on_commit=False)
        async with factory() as session, session.begin():
            await set_rls_org(session, org_a)
            rows = list((await session.execute(select(RunnerProbeCache))).scalars().all())
        assert len(rows) == 1
        assert rows[0].machine_id == "machine-1"

    async def test_status_read_primitive_returns_only_caller_org_rows(
        self, db_engine: AsyncEngine, app_engine: AsyncEngine
    ) -> None:
        """(b) the status endpoint's read primitive is org-scoped."""
        org_a = uuid.uuid4()
        org_b = uuid.uuid4()
        await _seed_org(db_engine, org_a, "ra")
        await _seed_org(db_engine, org_b, "rb")
        probe_at = datetime.now(UTC)
        await _seed_probe_row(db_engine, org_id=org_a, machine_id="machine-a", probed_at=probe_at)
        await _seed_probe_row(db_engine, org_id=org_b, machine_id="machine-b", probed_at=probe_at)

        factory = async_sessionmaker(app_engine, expire_on_commit=False)
        async with factory() as session, session.begin():
            await set_rls_org(session, org_a)
            rows = await list_runner_probe_cache(session, org_id=org_a)
        machines = {row.machine_id for row in rows}
        assert machines == {"machine-a"}

    async def test_upsert_through_rls_session_writes_caller_org_only(
        self, rls_session: AsyncSession, test_org: uuid.UUID
    ) -> None:
        """The probe-cron write path through an RLS-scoped session lands in
        the caller's org and is immediately visible to it."""
        row = await upsert_runner_probe_cache(
            rls_session,
            org_id=test_org,
            machine_id="machine-rls-1",
            engine_reachable=True,
            images_present=True,
            probed_at=datetime.now(UTC),
        )
        assert row.organisation_id == test_org


class TestPoisonedOrgSavepoint:
    async def test_poisoned_org_rolls_back_only_itself(self, db_engine: AsyncEngine, app_engine: AsyncEngine) -> None:
        """(qa F2, real Postgres) one org's poisoned upsert raises
        mid-transaction; the SAVEPOINT rolls back only that org — the other
        org's row (flushed earlier in the same outer tx) persists and the
        poisoned org's does not. Mirrors the probe tick's phase-3 write
        shape: one transaction, per-org ``begin_nested`` upserts."""
        org_a = uuid.uuid4()
        org_b = uuid.uuid4()
        await _seed_org(db_engine, org_a, "sp-a")
        await _seed_org(db_engine, org_b, "sp-b")

        # The probe tick writes cross-org on the modulo_system role
        # (BYPASSRLS); the superuser db_engine is the test stand-in for
        # that role plumbing (as in TestProbeRetentionPrune).
        factory = async_sessionmaker(db_engine, expire_on_commit=False)
        async with factory() as session, session.begin():
            # org_b's row is flushed BEFORE the poisoned savepoint opens.
            await upsert_runner_probe_cache(
                session,
                org_id=org_b,
                machine_id="machine-sp",
                engine_reachable=True,
                images_present=True,
            )
            with pytest.raises(SQLAlchemyError):
                # org_a's machine_id overflows String(255) — the INSERT
                # fails at flush with a server-side truncation error.
                async with session.begin_nested():
                    await upsert_runner_probe_cache(
                        session,
                        org_id=org_a,
                        machine_id="x" * 256,
                        engine_reachable=True,
                        images_present=True,
                    )

        # The outer tx stayed usable: org_b's row committed; org_a's did not.
        async with factory() as session:
            org_b_rows = (
                (await session.execute(select(RunnerProbeCache).where(RunnerProbeCache.organisation_id == org_b)))
                .scalars()
                .all()
            )
            org_a_rows = (
                (await session.execute(select(RunnerProbeCache).where(RunnerProbeCache.organisation_id == org_a)))
                .scalars()
                .all()
            )
        assert len(org_b_rows) == 1
        assert org_b_rows[0].machine_id == "machine-sp"
        assert not org_a_rows

        # The surviving row is org-scoped-visible to its own org under RLS.
        app_factory = async_sessionmaker(app_engine, expire_on_commit=False)
        async with app_factory() as session, session.begin():
            await set_rls_org(session, org_b)
            rows = await list_runner_probe_cache(session, org_id=org_b)
        assert [row.machine_id for row in rows] == ["machine-sp"]


class TestProbeRetentionPrune:
    async def test_prune_removes_only_rows_past_retention(
        self, db_engine: AsyncEngine, app_engine: AsyncEngine
    ) -> None:
        """(corpse-row contract, qa F8): a row older than the retention
        window is pruned; a fresh row survives."""
        org = uuid.uuid4()
        await _seed_org(db_engine, org, "prune")
        await _seed_probe_row(
            db_engine,
            org_id=org,
            machine_id="machine-fresh",
            probed_at=datetime.now(UTC) - timedelta(seconds=30),
        )
        await _seed_probe_row(
            db_engine,
            org_id=org,
            machine_id="machine-corpse",
            probed_at=datetime.now(UTC) - timedelta(seconds=PROBE_RETENTION_SECONDS + 60),
        )

        # Prune runs cross-org on the system role in production; here the
        # semantics are pinned on the superuser engine (the migration's
        # modulo_system grant covers the role plumbing).
        db_factory = async_sessionmaker(db_engine, expire_on_commit=False)
        async with db_factory() as session, session.begin():
            deleted = await prune_stale_runner_probe_rows(session, retention_seconds=PROBE_RETENTION_SECONDS)
        assert deleted >= 1

        factory = async_sessionmaker(app_engine, expire_on_commit=False)
        async with factory() as session, session.begin():
            await set_rls_org(session, org)
            machines = {row.machine_id for row in await list_runner_probe_cache(session, org_id=org)}
        assert "machine-fresh" in machines
        assert "machine-corpse" not in machines


class TestApplyTemplateCrossOrg:
    async def test_apply_template_cross_org_context_returns_none(
        self, db_engine: AsyncEngine, app_engine: AsyncEngine
    ) -> None:
        """(c) apply-template against org-A's profile from an org-B context
        finds nothing under RLS → None → the route 404s; the same call from
        org-A's own context succeeds (the None is RLS-caused, not row
        shape-caused)."""
        org_a = uuid.uuid4()
        org_b = uuid.uuid4()
        await _seed_org(db_engine, org_a, "at-a")
        await _seed_org(db_engine, org_b, "at-b")

        account_id = uuid.uuid4()
        async with db_engine.connect() as conn, conn.begin():
            await conn.execute(
                text(
                    "INSERT INTO accounts (id, email, display_name, password_hash, "
                    "auth_provider, active) VALUES (:id, :email, :name, 'hash', 'local', true)"
                ),
                {
                    "id": str(account_id),
                    "email": f"at-a-{uuid.uuid4().hex[:8]}@test.local",
                    "name": "apply-template",
                },
            )
        profile_id = uuid.uuid4()
        db_factory = async_sessionmaker(db_engine, expire_on_commit=False)
        async with db_factory() as session, session.begin():
            session.add(
                EnvironmentProfile(
                    id=profile_id,
                    organisation_id=org_a,
                    name="Bundled Runner (Docker)",
                    provider_type="runner_docker",
                    image_ref=BUNDLED_RUNNER_IMAGE_REF,
                    capabilities_json=[],
                    secret_refs_json=[],
                    config_json=dict(TEMPLATE_CONFIG_JSON),
                    account_id=account_id,
                )
            )

        # org-B context: the row is invisible under RLS → None (route 404s).
        factory = async_sessionmaker(app_engine, expire_on_commit=False)
        async with factory() as session, session.begin():
            await set_rls_org(session, org_b)
            cross_org_result = await apply_bundled_runner_template(session, profile_id)
        assert cross_org_result is None

        # org-A context: the caller's own row applies.
        async with factory() as session, session.begin():
            await set_rls_org(session, org_a)
            own_org_result = await apply_bundled_runner_template(session, profile_id)
        assert own_org_result is not None
        assert own_org_result.id == profile_id
