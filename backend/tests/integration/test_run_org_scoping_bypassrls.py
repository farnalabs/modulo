"""Integration tests: org predicate on by-ID run/pipeline reads under BYPASSRLS.

The read paths ``crud.get_run`` / ``crud.list_runs`` / ``crud.get_pipeline``
carry a defence-in-depth ``organisation_id`` predicate (FAR-328): production
RLS is the active backstop (``modulo_app`` is NOBYPASSRLS), but API-facing and
MCP callers must pass the org they already know so a cross-tenant read is
impossible even if RLS were misconfigured.

These tests simulate the BYPASSRLS condition by running the CRUD reads on the
testcontainers superuser connection: per the documented behaviour (see the
``non_superuser_role`` fixture in the integration conftest), Postgres RLS never
applies to superusers even under ``FORCE ROW LEVEL SECURITY``. On that
connection the explicit org predicate is the ONLY barrier, so:

* the control test asserts an *unpredicated* superuser read DOES return the
  foreign org's row — proving the environment genuinely bypasses RLS (the one
  condition in which omitting ``organisation_id`` would be a live leak), and
* the barrier tests assert the same reads WITH ``organisation_id`` yield
  nothing (the route layer maps ``None`` to 404) for a foreign org and the
  expected row for the owning org.

If the explicit predicate were dropped from ``crud.get_run``, the barrier test
fails on the exact same code path — the unit twin-guard for this observation
was rejected as insufficient because SQLite mocks never execute the real SQL.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from modulo.db.crud.pipeline import get_pipeline
from modulo.db.crud.run import get_run, list_runs

pytestmark = pytest.mark.integration


@dataclass(frozen=True)
class _Tenant:
    """An isolated org plus the pipeline/snapshot/run chain a read needs."""

    org_id: uuid.UUID
    pipeline_id: uuid.UUID
    run_id: uuid.UUID


async def _seed_tenant(engine: AsyncEngine, user_id: uuid.UUID, label: str) -> _Tenant:
    """Commit a dedicated organisation + pipeline + snapshot + run for one test.

    ``runs`` carries ``UNIQUE(organisation_id, run_number)`` and the suite runs
    with ``-n 2`` in the pre-deploy gate, so every test mints fresh orgs and
    allocates ``run_number`` per-org (MAX + 1) instead of sharing state.
    """
    tenant = _Tenant(uuid.uuid4(), uuid.uuid4(), uuid.uuid4())
    snapshot_id = uuid.uuid4()
    async with engine.connect() as conn, conn.begin():
        await conn.execute(
            text("INSERT INTO organisations (id, name, slug, settings_json) VALUES (:id, :name, :slug, '{}'::json)"),
            {
                "id": str(tenant.org_id),
                "name": f"Org Scoping Org {tenant.org_id.hex[:8]}",
                "slug": f"orgsc-{label}-{tenant.org_id.hex[:12]}",
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
                "name": f"Org Scoping Pipeline {label}",
                "uid": str(user_id),
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
            {"id": str(snapshot_id), "pid": str(tenant.pipeline_id), "oid": str(tenant.org_id)},
        )
        await conn.execute(
            text(
                "INSERT INTO runs (id, organisation_id, pipeline_id, snapshot_id, "
                "  trigger_type, status, input_hash, langgraph_thread_id, account_id, run_number) "
                "VALUES (:id, :oid, :pid, :sid, 'manual', 'failed', 'orgscoping', :thread, :uid, "
                "  (SELECT COALESCE(MAX(run_number), 0) + 1 FROM runs WHERE organisation_id = :oid))"
            ),
            {
                "id": str(tenant.run_id),
                "oid": str(tenant.org_id),
                "pid": str(tenant.pipeline_id),
                "sid": str(snapshot_id),
                "thread": f"orgscoping-{tenant.run_id}",
                "uid": str(user_id),
            },
        )
    return tenant


@pytest.fixture
async def two_tenants(db_engine: AsyncEngine, test_user: uuid.UUID) -> tuple[_Tenant, _Tenant]:
    """Two isolated tenants: the caller's org (A) and the foreign org (B)."""
    org_a = await _seed_tenant(db_engine, test_user, "a")
    org_b = await _seed_tenant(db_engine, test_user, "b")
    return org_a, org_b


@pytest.fixture
def superuser_session(db_engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    """Session factory on the testcontainer superuser — the BYPASSRLS simulation.

    Postgres RLS never applies to superusers even under ``FORCE ROW LEVEL
    SECURITY`` (verified — see the integration conftest's ``non_superuser_role``
    fixture), so on this connection the explicit ``organisation_id`` predicate
    is the only cross-tenant barrier.
    """
    return async_sessionmaker(db_engine, expire_on_commit=False)


class TestByPassRlsCrossTenantReads:
    """Cross-tenant by-ID reads must yield nothing (routes render 404)."""

    async def test_unpredicated_read_returns_foreign_row(
        self, superuser_session: async_sessionmaker[AsyncSession], two_tenants: tuple[_Tenant, _Tenant]
    ) -> None:
        """CONTROL: without the org predicate the superuser read DOES leak.

        This pins the environment: RLS is provably not filtering (else the
        foreign row would already be invisible), so the predicate asserts in
        the barrier tests are held up by the predicate itself, not by RLS.
        If this ever fails, RLS is somehow being applied and the barrier
        below is no longer the ONLY defence — the suite must be re-read.
        """
        _org_a, org_b = two_tenants
        async with superuser_session() as s, s.begin():
            leaked = await get_run(s, org_b.run_id)
            assert leaked is not None, (
                "Superuser read of a foreign org's run returned nothing — this "
                "environment no longer simulates BYPASSRLS, so the barrier "
                "tests in this module are no longer exercising the explicit "
                "org predicate in isolation."
            )
            assert leaked.organisation_id == org_b.org_id

    async def test_get_run_with_org_predicate_hides_foreign_run(
        self, superuser_session: async_sessionmaker[AsyncSession], two_tenants: tuple[_Tenant, _Tenant]
    ) -> None:
        """BARRIER: org A asking for org B's run by ID yields None (route → 404)."""
        org_a, org_b = two_tenants
        async with superuser_session() as s, s.begin():
            hidden = await get_run(s, org_b.run_id, organisation_id=org_a.org_id)
            assert hidden is None, "Cross-tenant get_run leaked a foreign run — defence-in-depth predicate is missing"

    async def test_get_run_with_own_org_predicate_finds_run(
        self, superuser_session: async_sessionmaker[AsyncSession], two_tenants: tuple[_Tenant, _Tenant]
    ) -> None:
        """POSITIVE: the predicate must not break the owning org's access."""
        org_a, _org_b = two_tenants
        async with superuser_session() as s, s.begin():
            own = await get_run(s, org_a.run_id, organisation_id=org_a.org_id)
            assert own is not None, "Org-scoped get_run failed to return the org's OWN run"
            assert own.id == org_a.run_id

    async def test_list_runs_org_scoped_excludes_foreign_org(
        self, superuser_session: async_sessionmaker[AsyncSession], two_tenants: tuple[_Tenant, _Tenant]
    ) -> None:
        """BARRIER: the org-scoped list never surfaces a foreign org's run.

        The list predicate filters on ``Run.organisation_id`` directly; the
        unpredicated list is the same leak surface in BYPASSRLS conditions.
        """
        org_a, org_b = two_tenants
        async with superuser_session() as s, s.begin():
            scoped = await list_runs(s, organisation_id=org_a.org_id, page_size=50)
            ids = {run.id for run in scoped.items}
            assert org_b.run_id not in ids, "Cross-tenant list_runs leaked a foreign org's run"
            assert org_a.run_id in ids, "Org-scoped list_runs omitted the org's own run"

    async def test_get_pipeline_with_org_predicate_hides_foreign_pipeline(
        self, superuser_session: async_sessionmaker[AsyncSession], two_tenants: tuple[_Tenant, _Tenant]
    ) -> None:
        """BARRIER: org A asking for org B's pipeline by ID yields None (route → 404)."""
        org_a, org_b = two_tenants
        async with superuser_session() as s, s.begin():
            hidden = await get_pipeline(s, org_b.pipeline_id, organisation_id=org_a.org_id)
            assert hidden is None, "Cross-tenant get_pipeline leaked a foreign pipeline"
            own = await get_pipeline(s, org_a.pipeline_id, organisation_id=org_a.org_id)
            assert own is not None, "Org-scoped get_pipeline failed to return the org's OWN pipeline"
