"""Integration tests: ``write_evidence_row``'s tenant anchor against real Postgres.

``run_evidence`` is org-scoped by migration 0133: ``organisation_id`` is NOT NULL
with an FK to ``organisations(id)``, the table is under ``FORCE ROW LEVEL
SECURITY``, and the ``rls_org_isolation`` policy scopes it to
``app.organisation_id``. ``run_id`` is separately a NOT NULL FK to ``runs(id)``.

The unit tests build the table with ``Base.metadata.create_all`` on SQLite, which
has no ``organisations`` table and no ``PRAGMA foreign_keys=ON``, so neither the
FK nor the RLS policy is exercised there. Only a real Postgres path can pin what
actually happens when the tenant anchor cannot be resolved — which is why an
earlier revision's "derive a deterministic placeholder tenant from the run_id"
fallback passed on SQLite while being unrealizable on the deployment target.

These tests drive ``write_evidence_row`` through a NOBYPASSRLS role (the
production ``modulo_app`` scenario) and read the rows back over a superuser
connection, so the assertions are about whether the row was actually persisted
rather than about read visibility.

Each test gets its OWN organisation (and pipeline/snapshot) rather than the
shared session-scoped ``test_org``: ``runs`` carries
``UNIQUE(organisation_id, run_number)``, and the suite runs with ``-n 2`` in the
pre-deploy gate, so reusing the shared org would make ``run_number`` allocation
racy. A dedicated org makes ``run_number = 1`` trivially collision-free.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from modulo.core.pipeline_engine.evidence import write_evidence_row
from modulo.db.rls import set_rls_org

pytestmark = pytest.mark.integration

# ``run_evidence.node_id`` is a native ``Uuid`` (FK to ``nodes.id``), promoted by
# migration 0160, so every node id written through ``write_evidence_row`` must be
# a well-formed UUID — not an arbitrary langgraph-style string like "node-a".
_NODE_A = uuid.UUID("00000000-0000-0000-0000-0000000000aa")


@dataclass(frozen=True)
class _EvidenceTenant:
    """An isolated org plus the pipeline/snapshot a run needs to FK against."""

    org_id: uuid.UUID
    pipeline_id: uuid.UUID
    snapshot_id: uuid.UUID


@pytest_asyncio.fixture
async def evidence_tenant(db_engine: AsyncEngine, test_user: uuid.UUID) -> _EvidenceTenant:
    """Commit a dedicated organisation + pipeline + snapshot for one test."""
    tenant = _EvidenceTenant(uuid.uuid4(), uuid.uuid4(), uuid.uuid4())
    async with db_engine.connect() as conn, conn.begin():
        await conn.execute(
            text("INSERT INTO organisations (id, name, slug, settings_json) VALUES (:id, :name, :slug, '{}'::json)"),
            {
                "id": str(tenant.org_id),
                "name": f"Evidence Tenant Anchor Org {tenant.org_id.hex[:8]}",
                "slug": f"evid-{tenant.org_id.hex[:12]}",
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
                "name": "Evidence Tenant Anchor Pipeline",
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
    """Session whose connections run as a NOBYPASSRLS role, so RLS applies.

    Mirrors production, where the app connects as ``modulo_app`` (a non-owner
    role that ``FORCE ROW LEVEL SECURITY`` confines).
    """
    factory = async_sessionmaker(app_engine, expire_on_commit=False)
    session = factory()
    try:
        yield session
    finally:
        await session.close()


async def _insert_run(db_engine: AsyncEngine, tenant: _EvidenceTenant) -> uuid.UUID:
    """Commit a terminal run in *tenant* over a superuser connection.

    ``run_number`` is 1 because the org is dedicated to this test, so the
    ``UNIQUE(organisation_id, run_number)`` constraint cannot collide.
    """
    run_id = uuid.uuid4()
    async with db_engine.connect() as conn, conn.begin():
        await conn.execute(
            text(
                "INSERT INTO runs (id, organisation_id, pipeline_id, snapshot_id, "
                "trigger_type, status, run_number, input_hash, langgraph_thread_id) "
                "VALUES (:id, :oid, :pid, :sid, 'manual', 'complete', 1, :hash, :thread)"
            ),
            {
                "id": str(run_id),
                "oid": str(tenant.org_id),
                "pid": str(tenant.pipeline_id),
                "sid": str(tenant.snapshot_id),
                "hash": "e" * 64,
                "thread": f"evidence-tenant-{run_id}",
            },
        )
    return run_id


async def _fetch_evidence(db_engine: AsyncEngine, run_id: uuid.UUID) -> list[tuple[uuid.UUID, str]]:
    """Read run_evidence rows for *run_id* over a superuser connection (RLS bypassed)."""
    async with db_engine.connect() as conn:
        result = await conn.execute(
            text("SELECT organisation_id, evidence_state FROM run_evidence WHERE run_id = :rid"),
            {"rid": str(run_id)},
        )
        return [(row[0], row[1]) for row in result.all()]


async def test_omitted_org_resolves_from_parent_run_and_persists(
    db_engine: AsyncEngine,
    rls_app_session: AsyncSession,
    evidence_tenant: _EvidenceTenant,
) -> None:
    """With ``organisation_id`` omitted the anchor is resolved from the parent
    run, which satisfies both the FK and the RLS WITH CHECK on real Postgres."""
    run_id = await _insert_run(db_engine, evidence_tenant)

    async with rls_app_session.begin():
        await set_rls_org(rls_app_session, evidence_tenant.org_id)
        await write_evidence_row(
            rls_app_session,
            run_id=run_id,
            node_id=str(_NODE_A),
            evidence_state="verified_empty",
            evidence_detail="probe found no diff",
        )

    assert await _fetch_evidence(db_engine, run_id) == [(evidence_tenant.org_id, "verified_empty")]


async def test_missing_parent_run_skips_write_without_raising(
    db_engine: AsyncEngine,
    rls_app_session: AsyncSession,
    evidence_tenant: _EvidenceTenant,
) -> None:
    """An unresolvable parent run is skipped, not anchored to a fabricated tenant.

    This is the regression guard for the placeholder-tenant fallback: the write
    must be a deliberate no-op (the caller treats the node as unverifiable)
    rather than an INSERT that Postgres rejects.
    """
    orphan_run_id = uuid.uuid4()

    async with rls_app_session.begin():
        await set_rls_org(rls_app_session, evidence_tenant.org_id)
        await write_evidence_row(
            rls_app_session,
            run_id=orphan_run_id,
            node_id=str(_NODE_A),
            evidence_state="verified_empty",
            evidence_detail=None,
        )

    assert not await _fetch_evidence(db_engine, orphan_run_id)


async def test_fabricated_tenant_anchor_never_persists(
    db_engine: AsyncEngine,
    rls_app_session: AsyncSession,
    evidence_tenant: _EvidenceTenant,
) -> None:
    """Pins WHY the placeholder fallback was removed rather than kept.

    Anchoring a row to an organisation that does not exist can never persist:
    the ``organisation_id`` FK rejects it, and under 0133's ``rls_org_isolation``
    policy the WITH CHECK rejects it too. Whichever fires first, no row survives
    — so deriving a synthetic tenant could only ever have dropped the evidence
    (silently, when the error was an ``IntegrityError`` swallowed by the
    duplicate-key guard) or broken the caller.
    """
    # A real parent run, so the only thing wrong is the tenant anchor itself.
    run_id = await _insert_run(db_engine, evidence_tenant)
    fabricated_org = uuid.uuid5(uuid.NAMESPACE_OID, f"run-evidence:{run_id}")

    try:
        async with rls_app_session.begin():
            await set_rls_org(rls_app_session, evidence_tenant.org_id)
            # Passing the anchor explicitly skips resolution, so this is exactly
            # the INSERT the removed uuid5 fallback would have issued.
            await write_evidence_row(
                rls_app_session,
                run_id=run_id,
                node_id=str(_NODE_A),
                evidence_state="verified_empty",
                evidence_detail=None,
                organisation_id=fabricated_org,
            )
    except SQLAlchemyError:
        # The RLS WITH CHECK rejection is not an IntegrityError, so it escapes
        # write_evidence_row's duplicate-key guard and reaches the caller.
        pass

    assert not await _fetch_evidence(db_engine, run_id)
