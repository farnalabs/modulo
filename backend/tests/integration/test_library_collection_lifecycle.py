"""Integration test for Library Collections install/uninstall lifecycle (FAR-765).

DB-backed round-trip tests covering:
1. Full install → verify → uninstall cycle (2 schemas + 1 agent → pipeline created)
2. Install → modify one entity → uninstall → modified entity kept, others deleted
3. Re-install same version → idempotent-resume (no duplicate entities)

Runs against testcontainers Postgres with real Alembic migrations.
"""

from __future__ import annotations

import uuid
from typing import Any

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from modulo.core.library_service._seed_data import MODULO_ORG_ID
from modulo.core.library_service.grant import grant_collection_agents
from modulo.core.library_service.install import CollectionInstallError, install_collection
from modulo.core.library_service.runnability import compute_runnable
from modulo.core.library_service.uninstall import uninstall_collection
from modulo.core.library_service.upgrade import (
    NoUpdateAvailableError,
    UpgradeInstallNotFoundError,
    detect_collection_update,
    upgrade_collection,
)
from modulo.db.models.collection_install import CollectionInstall
from modulo.db.models.library_primitive import LibraryPrimitive
from modulo.db.models.organisation import Organisation
from modulo.db.rls import set_rls_org

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
                "INSERT INTO accounts (id, email, display_name, password_hash, "
                "auth_provider, active) "
                "VALUES (:id, :email, :name, 'hash', 'local', true) "
                "ON CONFLICT (email) DO UPDATE SET id = accounts.id "
                "RETURNING id"
            ),
            {"id": str(account_id), "email": email, "name": email},
        )
        existing = await conn.execute(
            text("SELECT id FROM accounts WHERE email = :email"),
            {"email": email},
        )
        resolved_id = uuid.UUID(str(existing.scalar_one()))
        membership = await conn.execute(
            text("SELECT id FROM org_memberships WHERE account_id = :aid AND organisation_id = :oid"),
            {"aid": str(resolved_id), "oid": str(org_id)},
        )
        if membership.first() is None:
            await conn.execute(
                text(
                    "INSERT INTO org_memberships (id, account_id, organisation_id, role) "
                    "VALUES (:mid, :aid, :oid, 'admin')"
                ),
                {"mid": str(uuid.uuid4()), "aid": str(resolved_id), "oid": str(org_id)},
            )
    return resolved_id


async def _seed_model_backend(
    db_engine: AsyncEngine,
    org_id: uuid.UUID,
    user_id: uuid.UUID,
) -> uuid.UUID:
    backend_id = uuid.uuid4()
    async with db_engine.connect() as conn, conn.begin():
        await conn.execute(
            text(
                "INSERT INTO model_backends (id, organisation_id, name, display_name, provider, "
                "model_id, credentials_ciphertext, account_id, default_params, cost_tracking, "
                "visibility, tier) "
                "VALUES (:id, :oid, 'stub', 'Stub', 'openai', 'gpt-4', "
                "decode('636970686572','hex'), :uid, "
                "'{}'::json, 'enabled', 'org', 'native')"
            ),
            {"id": str(backend_id), "oid": str(org_id), "uid": str(user_id)},
        )
    return backend_id


async def _seed_schema(
    db_engine: AsyncEngine,
    org_id: uuid.UUID,
    slug: str,
    *,
    name: str | None = None,
    version: str = "1.0",
    definition: dict[str, Any] | None = None,
) -> uuid.UUID:
    prim_id = uuid.uuid4()
    content = {"definition_json": definition or {"type": "object", "properties": {}}}
    async with db_engine.connect() as conn, conn.begin():
        await conn.execute(
            text(
                "INSERT INTO library_primitives "
                "(id, organisation_id, source, primitive_type, name, slug, "
                "author, version, tags, content_json, visibility, tier) "
                "VALUES (:id, :oid, 'local', 'schema', :name, :slug, "
                "'test', :version, '[]'::json, :content, 'org', 'native')"
            ),
            {
                "id": str(prim_id),
                "oid": str(org_id),
                "name": name or slug,
                "slug": slug,
                "version": version,
                "content": __import__("json").dumps(content),
            },
        )
    return prim_id


async def _seed_agent(
    db_engine: AsyncEngine,
    org_id: uuid.UUID,
    slug: str,
    *,
    name: str | None = None,
    input_schema_id: str = "",
    output_schema_id: str = "",
    version: str = "1.0",
    prompt: str = "You are a test agent.",
) -> uuid.UUID:
    prim_id = uuid.uuid4()
    content: dict[str, Any] = {
        "input_schema_id": input_schema_id,
        "output_schema_id": output_schema_id,
        "prompt_template": prompt,
        "connector_type_refs": [],
    }
    async with db_engine.connect() as conn, conn.begin():
        await conn.execute(
            text(
                "INSERT INTO library_primitives "
                "(id, organisation_id, source, primitive_type, name, slug, "
                "author, version, tags, content_json, visibility, tier) "
                "VALUES (:id, :oid, 'local', 'agent', :name, :slug, "
                "'test', :version, '[]'::json, :content, 'org', 'native')"
            ),
            {
                "id": str(prim_id),
                "oid": str(org_id),
                "name": name or slug,
                "slug": slug,
                "version": version,
                "content": __import__("json").dumps(content),
            },
        )
    return prim_id


async def _seed_collection(
    db_engine: AsyncEngine,
    org_id: uuid.UUID,
    manifest_pins: list[dict[str, Any]],
    *,
    slug: str = "test-collection",
    status: str = "published",
    source: str = "local",
    version: str = "1.0",
    version_group_id: uuid.UUID | None = None,
) -> uuid.UUID:
    prim_id = uuid.uuid4()
    async with db_engine.connect() as conn, conn.begin():
        await conn.execute(
            text(
                "INSERT INTO library_primitives "
                "(id, organisation_id, source, primitive_type, name, slug, "
                "author, version, tags, content_json, visibility, tier, "
                "status, manifest_pins, version_group_id) "
                "VALUES (:id, :oid, :source, 'library_collection', :name, :slug, "
                "'test', :version, '[]'::json, '{}'::json, 'org', 'native', "
                ":status, :pins, :vgid)"
            ),
            {
                "id": str(prim_id),
                "oid": str(org_id),
                "source": source,
                "name": "Test Collection",
                "slug": slug,
                "version": version,
                "status": status,
                "pins": __import__("json").dumps(manifest_pins),
                "vgid": str(version_group_id) if version_group_id else None,
            },
        )
    return prim_id


async def _get_schema_install_id(
    session: AsyncSession,
    schema_id: uuid.UUID,
) -> uuid.UUID | None:
    stmt = text("SELECT collection_install_id FROM schemas WHERE id = :id")
    row = await session.execute(stmt, {"id": str(schema_id)})
    r = row.first()
    return uuid.UUID(str(r[0])) if r and r[0] is not None else None


async def _get_agent_install_id(
    session: AsyncSession,
    agent_id: uuid.UUID,
) -> uuid.UUID | None:
    stmt = text("SELECT collection_install_id FROM agents WHERE id = :id")
    row = await session.execute(stmt, {"id": str(agent_id)})
    r = row.first()
    return uuid.UUID(str(r[0])) if r and r[0] is not None else None


async def _get_pipeline_install_id(
    session: AsyncSession,
    pipeline_id: uuid.UUID,
) -> uuid.UUID | None:
    stmt = text("SELECT collection_install_id FROM pipelines WHERE id = :id")
    row = await session.execute(stmt, {"id": str(pipeline_id)})
    r = row.first()
    return uuid.UUID(str(r[0])) if r and r[0] is not None else None


async def _count_entities(
    db_engine: AsyncEngine,
    install_id: uuid.UUID,
) -> dict[str, int]:
    async with db_engine.connect() as conn:
        row = await conn.execute(
            text(
                "SELECT entity_type, count(*) FROM collection_install_entity "
                "WHERE install_id = :iid GROUP BY entity_type"
            ),
            {"iid": str(install_id)},
        )
        return {r[0]: r[1] for r in row.all()}


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def org(db_engine: AsyncEngine) -> uuid.UUID:
    return await _seed_org(db_engine, "CollLifecycle")


@pytest_asyncio.fixture
async def user(db_engine: AsyncEngine, org: uuid.UUID) -> uuid.UUID:
    return await _seed_user(db_engine, org, "coll-lifecycle@test.local")


@pytest_asyncio.fixture
async def model_backend(
    db_engine: AsyncEngine,
    org: uuid.UUID,
    user: uuid.UUID,
) -> uuid.UUID:
    return await _seed_model_backend(db_engine, org, user)


@pytest_asyncio.fixture
async def schemas(
    db_engine: AsyncEngine,
    org: uuid.UUID,
) -> list[uuid.UUID]:
    s1 = await _seed_schema(db_engine, org, "input-schema", name="Input Schema")
    s2 = await _seed_schema(db_engine, org, "output-schema", name="Output Schema")
    return [s1, s2]


@pytest_asyncio.fixture
async def agent_prim(
    db_engine: AsyncEngine,
    org: uuid.UUID,
    schemas: list[uuid.UUID],
) -> uuid.UUID:
    return await _seed_agent(
        db_engine,
        org,
        "test-agent",
        name="Test Agent",
        input_schema_id=str(schemas[0]),
        output_schema_id=str(schemas[1]),
    )


# ---------------------------------------------------------------------------
# Test 1: Full install → verify → uninstall round-trip
# ---------------------------------------------------------------------------


async def test_full_install_verify_uninstall_round_trip(
    db_engine: AsyncEngine,
    db_session: AsyncSession,
    org: uuid.UUID,
    user: uuid.UUID,
    schemas: list[uuid.UUID],
    agent_prim: uuid.UUID,
    model_backend: uuid.UUID,
) -> None:
    """Install a collection with 2 schemas + 1 agent → verify all entities
    stamped → compute_runnable → uninstall → entities + record deleted.
    """
    pins = [
        {"slug": "input-schema", "version": "1.0"},
        {"slug": "output-schema", "version": "1.0"},
        {"slug": "test-agent", "version": "1.0"},
    ]
    collection_id = await _seed_collection(db_engine, org, pins)

    # --- Install ---
    install = await install_collection(db_session, org, user, collection_id)
    await db_session.commit()

    assert install.status == "installed"
    assert install.organisation_id == org
    assert install.collection_id == collection_id
    install_id = install.install_id

    # Verify entity tracking rows exist
    entity_counts = await _count_entities(db_engine, install_id)
    assert "schema" in entity_counts
    assert "agent" in entity_counts
    assert entity_counts["schema"] == 2
    assert entity_counts["agent"] == 1

    # Verify installed entities have collection_install_id stamped
    # Schemas created by materialize_import get new UUIDs; use the resolved manifest
    resolved = install.resolved_manifest or {}
    schema_id_map = resolved.get("schemas", {})
    agent_id_map = resolved.get("agents", {})
    async with db_engine.connect() as conn:
        for new_schema_id in schema_id_map.values():
            row = await conn.execute(
                text("SELECT collection_install_id FROM schemas WHERE id = :id"),
                {"id": new_schema_id},
            )
            r = row.first()
            assert r is not None
            assert r[0] is not None

        for new_agent_id in agent_id_map.values():
            row = await conn.execute(
                text("SELECT collection_install_id FROM agents WHERE id = :id"),
                {"id": new_agent_id},
            )
            r = row.first()
            assert r is not None
            assert r[0] is not None

        # Verify a pipeline was created
        row = await conn.execute(
            text("SELECT id FROM pipelines WHERE organisation_id = :oid AND collection_install_id IS NOT NULL"),
            {"oid": str(org)},
        )
        pipeline_row = row.first()
        assert pipeline_row is not None
        pipeline_id = uuid.UUID(str(pipeline_row[0]))

    # Verify runnability (org has model backend, no connectors required)
    runnable = await compute_runnable(db_session, install_id)
    assert runnable is True

    # --- Uninstall ---
    result = await uninstall_collection(db_session, org, install_id)
    await db_session.commit()

    assert result["deleted"]
    assert not result["detached"]

    # Verify install record deleted
    install_check = await db_session.get(CollectionInstall, install_id)
    assert install_check is None

    # Verify entity tracking rows deleted (CASCADE)
    async with db_engine.connect() as conn:
        row = await conn.execute(
            text("SELECT count(*) FROM collection_install_entity WHERE install_id = :iid"),
            {"iid": str(install_id)},
        )
        assert row.scalar_one() == 0

        # Verify schemas deleted
        for new_sid in schema_id_map.values():
            row = await conn.execute(
                text("SELECT id FROM schemas WHERE id = :id"),
                {"id": new_sid},
            )
            assert row.first() is None

        # Verify agent deleted
        for new_aid in agent_id_map.values():
            row = await conn.execute(
                text("SELECT id FROM agents WHERE id = :id"),
                {"id": new_aid},
            )
            assert row.first() is None

        # Verify pipeline deleted
        row = await conn.execute(
            text("SELECT id FROM pipelines WHERE id = :id"),
            {"id": str(pipeline_id)},
        )
        assert row.first() is None


# ---------------------------------------------------------------------------
# Test 2: Install → modify entity → uninstall → modified kept, others deleted
# ---------------------------------------------------------------------------


async def test_uninstall_detaches_modified_entity(
    db_engine: AsyncEngine,
    db_session: AsyncSession,
    org: uuid.UUID,
    user: uuid.UUID,
    model_backend: uuid.UUID,
) -> None:
    """Install → clear collection_install_id on one schema (simulating user
    modification) → uninstall → modified schema kept (provenance detached),
    other entities deleted.
    """
    await _seed_schema(db_engine, org, "input-schema", name="Input Schema")
    await _seed_schema(db_engine, org, "output-schema", name="Output Schema")
    await _seed_agent(db_engine, org, "test-agent", name="Test Agent")

    pins = [
        {"slug": "input-schema", "version": "1.0"},
        {"slug": "output-schema", "version": "1.0"},
        {"slug": "test-agent", "version": "1.0"},
    ]
    collection_id = await _seed_collection(db_engine, org, pins)

    install = await install_collection(db_session, org, user, collection_id)
    await db_session.commit()
    install_id = install.install_id

    resolved = install.resolved_manifest or {}
    schema_id_map = resolved.get("schemas", {})
    agent_id_map = resolved.get("agents", {})
    schema_ids = list(schema_id_map.values())
    agent_ids = list(agent_id_map.values())

    # Simulate user modification: clear install_id on one schema
    async with db_engine.connect() as conn, conn.begin():
        await conn.execute(
            text("UPDATE schemas SET collection_install_id = NULL WHERE id = :sid"),
            {"sid": schema_ids[0]},
        )

    # Uninstall
    result = await uninstall_collection(db_session, org, install_id)
    await db_session.commit()

    # Modified schema should be in "detached" list
    detached_ids = {d["entity_id"] for d in result["detached"]}
    assert schema_ids[0] in detached_ids

    # Modified schema should still exist in DB
    async with db_engine.connect() as conn:
        row = await conn.execute(
            text("SELECT id FROM schemas WHERE id = :id"),
            {"id": schema_ids[0]},
        )
        assert row.first() is not None

        # Other schema (unmodified) should be deleted
        row = await conn.execute(
            text("SELECT id FROM schemas WHERE id = :id"),
            {"id": schema_ids[1]},
        )
        assert row.first() is None

        # Agent should be deleted
        for aid in agent_ids:
            row = await conn.execute(
                text("SELECT id FROM agents WHERE id = :id"),
                {"id": aid},
            )
            assert row.first() is None


# ---------------------------------------------------------------------------
# Test 3: Re-install same version → idempotent-resume
# ---------------------------------------------------------------------------


async def test_reinstall_same_version_idempotent(
    db_engine: AsyncEngine,
    db_session: AsyncSession,
    org: uuid.UUID,
    user: uuid.UUID,
    model_backend: uuid.UUID,
) -> None:
    """Install → uninstall → re-install same version → verify entities
    re-created without duplication and install record re-created.
    """
    await _seed_schema(db_engine, org, "input-schema", name="Input Schema")
    await _seed_schema(db_engine, org, "output-schema", name="Output Schema")
    await _seed_agent(db_engine, org, "test-agent", name="Test Agent")

    pins = [
        {"slug": "input-schema", "version": "1.0"},
        {"slug": "output-schema", "version": "1.0"},
        {"slug": "test-agent", "version": "1.0"},
    ]
    collection_id = await _seed_collection(db_engine, org, pins)

    # First install
    install1 = await install_collection(db_session, org, user, collection_id)
    await db_session.commit()
    install_id1 = install1.install_id

    # Uninstall
    await uninstall_collection(db_session, org, install_id1)
    await db_session.commit()

    # Verify schemas/agent deleted
    async with db_engine.connect() as conn:
        row = await conn.execute(text("SELECT count(*) FROM schemas WHERE organisation_id = :oid"), {"oid": str(org)})
        assert row.scalar_one() == 0

    # Clean up the library_primitive created by materialize_import (not
    # cleaned up by uninstall, which only handles entities)
    async with db_engine.connect() as conn, conn.begin():
        await conn.execute(
            text(
                "DELETE FROM library_primitives WHERE organisation_id = :oid "
                "AND primitive_type = 'workflow' AND source = 'local'"
            ),
            {"oid": str(org)},
        )

    # Second install (re-install same version)
    install2 = await install_collection(db_session, org, user, collection_id)
    await db_session.commit()
    install_id2 = install2.install_id

    # Install record should be re-created with a NEW install_id
    assert install_id2 != install_id1
    assert install2.status == "installed"

    # Verify entity tracking rows exist
    entity_counts = await _count_entities(db_engine, install_id2)
    assert entity_counts.get("schema", 0) == 2
    assert entity_counts.get("agent", 0) == 1


# ---------------------------------------------------------------------------
# Test 4: Grant on community-sourced collection
# ---------------------------------------------------------------------------


async def test_grant_community_collection_agents(
    db_engine: AsyncEngine,
    db_session: AsyncSession,
    org: uuid.UUID,
    user: uuid.UUID,
    model_backend: uuid.UUID,
) -> None:
    """Install a community-sourced collection → grant → verify agents_granted."""
    await _seed_schema(db_engine, org, "input-schema", name="Input Schema")
    await _seed_agent(db_engine, org, "test-agent", name="Test Agent")
    pins = [
        {"slug": "input-schema", "version": "1.0"},
        {"slug": "test-agent", "version": "1.0"},
    ]
    collection_id = await _seed_collection(
        db_engine,
        org,
        pins,
        source="community",
    )

    install = await install_collection(db_session, org, user, collection_id)
    await db_session.commit()

    assert install.community_sourced is True
    assert install.agents_granted is False

    # Grant
    granted = await grant_collection_agents(db_session, org, install.install_id)
    await db_session.commit()

    assert granted.agents_granted is True

    # Verify persisted
    refreshed = await db_session.get(CollectionInstall, install.install_id)
    assert refreshed is not None
    assert refreshed.agents_granted is True


# ---------------------------------------------------------------------------
# Test 5: Install a SHIPPED modulo collection (ADR 032 - prove the fix)
# ---------------------------------------------------------------------------


async def test_install_shipped_github_pr_reviewer_collection(
    db_engine: AsyncEngine,
    db_session: AsyncSession,
    org: uuid.UUID,
    user: uuid.UUID,
    model_backend: uuid.UUID,
) -> None:
    """Install the shipped in-code github-pr-reviewer collection (ADR 032 §4).

    Nothing is pre-seeded in this org's RLS slice for the collection or its
    pins — they come from the library_service resolver (in-code modulo
    registry). Asserts the full round trip: schema + agents (standalone +
    template) + pipeline materialised, provenance stamped, the installed
    agent's output schema round-trips to the installed schema entity, and
    the connector checklist carries the github connector requirement.
    """
    from modulo.core.library_service._seed_data import _MODULO_PRIMITIVES

    collection = next(p for p in _MODULO_PRIMITIVES if p.slug == "github-pr-reviewer")

    install = await install_collection(db_session, org, user, collection.id)
    await db_session.commit()

    assert install.status == "installed"
    assert install.organisation_id == org
    assert install.community_sourced is False
    install_id = install.install_id

    # Entity round trip: 1 schema, 5 agents (1 standalone + 4 template), 1 pipeline
    entity_counts = await _count_entities(db_engine, install_id)
    assert entity_counts.get("schema", 0) == 1
    assert entity_counts.get("agent", 0) == 5
    assert entity_counts.get("pipeline", 0) == 1

    # Provenance stamped on all installed entities
    resolved = install.resolved_manifest or {}
    schema_id_map = resolved.get("schemas", {})
    agent_id_map = resolved.get("agents", {})
    installed_schema_ids = list(schema_id_map.values())
    installed_agent_ids = list(agent_id_map.values())
    assert len(installed_schema_ids) == 1
    assert len(installed_agent_ids) == 5

    async with db_engine.connect() as conn:
        for entity_id in installed_schema_ids:
            row = await conn.execute(
                text("SELECT collection_install_id FROM schemas WHERE id = :id"),
                {"id": entity_id},
            )
            r = row.first()
            assert r is not None
            assert r[0] == install_id

        for entity_id in installed_agent_ids:
            row = await conn.execute(
                text("SELECT collection_install_id FROM agents WHERE id = :id"),
                {"id": entity_id},
            )
            r = row.first()
            assert r is not None
            assert r[0] == install_id

        # The standalone PR Review Agent's output schema resolves to the
        # INSTALLED schema entity (slug wiring from the agent pin content).
        row = await conn.execute(
            text("SELECT output_schema_id FROM agents WHERE id = ANY(:aids) AND output_schema_id = ANY(:sids)"),
            {"aids": list(installed_agent_ids), "sids": list(installed_schema_ids)},
        )
        assert row.first() is not None

        # A pipeline was created
        row = await conn.execute(
            text("SELECT id FROM pipelines WHERE collection_install_id = :iid"),
            {"iid": str(install_id)},
        )
        assert row.first() is not None

    # Connector checklist: the resolved agent pins require the github connector
    checklist = install.connector_checklist or []
    assert any(c.get("connector_type_id") == "github" for c in checklist)


# ---------------------------------------------------------------------------
# Test 6: Shipped install persists ONLY the collection row, under the
#         migration-seeded sentinel org (FAR-826 review findings 2 + 3)
# ---------------------------------------------------------------------------


async def test_shipped_install_persists_only_collection_row_under_sentinel_rls(
    db_engine: AsyncEngine,
    db_session: AsyncSession,
    org: uuid.UUID,
    user: uuid.UUID,
    model_backend: uuid.UUID,
    modulo_app_engine: AsyncEngine,
) -> None:
    """Install the shipped github-pr-reviewer collection and prove the new
    persistence contract end-to-end against real RLS:

    1. ONLY the collection row is persisted under the sentinel org (migration
       0230 seeds it) — pin rows are never written, the in-code registry stays
       the source of truth for pin content.
    2. ``collection_install.collection_id`` points at the DB row that actually
       exists (tuple-keyed upsert, not the registry id).
    3. The sentinel Organisation row is readable by the REAL ``modulo_app``
       role under the installing org's RLS context — the ``organisations``
       table has no RLS, so the install-path sentinel lookup cannot be blocked
       by tenancy.
    4. The sentinel-owned collection row is readable by ``modulo_app`` ONLY
       under the sentinel org's RLS context (library_primitives RLS is
       org-scoped) — proving the install path's RLS-context switch is real.
    5. A second install in the same org is refused ("already installed") and
       does not duplicate the sentinel row.
    """
    from modulo.core.library_service._seed_data import _MODULO_PRIMITIVES

    collection = next(p for p in _MODULO_PRIMITIVES if p.slug == "github-pr-reviewer")

    install = await install_collection(db_session, org, user, collection.id)
    await db_session.commit()

    # 1 + 2: exactly one sentinel-owned library_primitives row — the collection.
    async with db_engine.connect() as conn:
        rows = await conn.execute(
            text(
                "SELECT id, organisation_id, source, slug, version FROM library_primitives WHERE organisation_id = :oid"
            ),
            {"oid": str(MODULO_ORG_ID)},
        )
        sentinel_rows = rows.all()
    assert len(sentinel_rows) == 1
    persisted = sentinel_rows[0]
    assert persisted.source == "modulo"
    assert persisted.slug == collection.slug
    assert persisted.version == collection.version
    assert install.collection_id == persisted.id

    # 3: sentinel org readable by the app role under the TEST org's RLS context.
    app_factory = async_sessionmaker(modulo_app_engine, expire_on_commit=False)
    async with app_factory() as app_session:
        await app_session.execute(text("SELECT 1"))  # begin the RLS-scoped transaction
        await set_rls_org(app_session, org)
        sentinel_org = await app_session.get(Organisation, MODULO_ORG_ID)
        assert sentinel_org is not None

    # 4: sentinel-owned collection row visible to the app role ONLY under the
    # sentinel org's RLS context.
    async with app_factory() as app_session:
        await app_session.execute(text("SELECT 1"))  # begin the RLS-scoped transaction
        await set_rls_org(app_session, MODULO_ORG_ID)
        row = await app_session.get(LibraryPrimitive, install.collection_id)
        assert row is not None
        assert row.slug == collection.slug

    # 5: re-install is refused and does not duplicate the sentinel row.
    try:
        with pytest.raises(CollectionInstallError, match="already installed"):
            await install_collection(db_session, org, user, collection.id)
    finally:
        await db_session.rollback()
    async with db_engine.connect() as conn:
        count = await conn.execute(
            text("SELECT count(*) FROM library_primitives WHERE organisation_id = :oid"),
            {"oid": str(MODULO_ORG_ID)},
        )
        assert count.scalar_one() == 1


# ---------------------------------------------------------------------------
# Test 7 (FAR-759): version-upgrade / "update available" flow
# ---------------------------------------------------------------------------


async def test_collection_upgrade_surfaces_delta_reconciles_and_preserves_modified(
    db_engine: AsyncEngine,
    db_session: AsyncSession,
    org: uuid.UUID,
    user: uuid.UUID,
    model_backend: uuid.UUID,
) -> None:
    """An installed collection with a newer published version surfaces an
    "update available" delta per entity type; upgrading reconciles unmodified
    entities in place while PRESERVING operator-modified ones (provenance
    detached OR content drifted from the install-time fingerprint).
    """
    # --- Arrange: v1 collection (2 schemas + 2 agents), install it ---
    def_a: dict[str, Any] = {"type": "object", "properties": {"a": {"type": "string"}}}
    def_a2: dict[str, Any] = {"type": "object", "properties": {"a2": {"type": "string"}}}
    def_b: dict[str, Any] = {"type": "object", "properties": {"b": {"type": "string"}}}
    def_b2: dict[str, Any] = {"type": "object", "properties": {"b2": {"type": "string"}}}

    input_pin_v1 = await _seed_schema(db_engine, org, "input-schema", name="Input Schema", definition=def_a)
    output_pin_v1 = await _seed_schema(db_engine, org, "output-schema", name="Output Schema", definition=def_b)
    clean_pin_v1 = await _seed_agent(db_engine, org, "clean-agent", name="Clean Agent", prompt="P1 clean")
    edited_pin_v1 = await _seed_agent(db_engine, org, "edited-agent", name="Edited Agent", prompt="P1 edited")

    pins_v1 = [
        {"slug": "input-schema", "version": "1.0"},
        {"slug": "output-schema", "version": "1.0"},
        {"slug": "clean-agent", "version": "1.0"},
        {"slug": "edited-agent", "version": "1.0"},
    ]
    collection_v1 = await _seed_collection(db_engine, org, pins_v1, slug="upgrade-collection")

    install = await install_collection(db_session, org, user, collection_v1)
    await db_session.commit()
    install_id = install.install_id

    # Install-time snapshot (FAR-759) exists so a later upgrade can tell
    # modified entities from unmodified ones.
    resolved = install.resolved_manifest or {}
    assert resolved.get("pins"), "install must snapshot the resolved pins"
    assert resolved.get("pin_entity_ids"), "install must snapshot pin→entity ids"
    assert resolved.get("entity_fingerprints"), "install must snapshot entity fingerprints"

    input_local = uuid.UUID(resolved["schemas"][str(input_pin_v1)])
    output_local = uuid.UUID(resolved["schemas"][str(output_pin_v1)])
    clean_local = uuid.UUID(resolved["agents"][str(clean_pin_v1)])
    edited_local = uuid.UUID(resolved["agents"][str(edited_pin_v1)])

    # --- Act: operator modifies two entities (the two "do not clobber" cases) ---
    async with db_engine.connect() as conn, conn.begin():
        # Content drift on the agent (provenance kept) — content-fingerprint path.
        await conn.execute(
            text("UPDATE agents SET prompt_template = :p WHERE id = :id"),
            {"p": "OPERATOR CUSTOM PROMPT", "id": str(edited_local)},
        )
        # Provenance detach on the schema — the uninstall convention.
        await conn.execute(
            text("UPDATE schemas SET collection_install_id = NULL WHERE id = :id"),
            {"id": str(output_local)},
        )

    # Publish a newer collection version v2 (same slug, group-linked).
    await _seed_schema(db_engine, org, "input-schema", version="1.1", definition=def_a2)
    await _seed_schema(db_engine, org, "output-schema", version="1.1", definition=def_b2)
    await _seed_agent(db_engine, org, "clean-agent", version="1.1", prompt="P2 clean")
    await _seed_agent(db_engine, org, "edited-agent", version="1.1", prompt="P2 edited")
    pins_v2 = [
        {"slug": "input-schema", "version": "1.1"},
        {"slug": "output-schema", "version": "1.1"},
        {"slug": "clean-agent", "version": "1.1"},
        {"slug": "edited-agent", "version": "1.1"},
    ]
    collection_v2 = await _seed_collection(
        db_engine, org, pins_v2, slug="upgrade-collection", version="1.1", version_group_id=collection_v1
    )

    # --- Detection: "update available" with a per-entity-type delta ---
    report = await detect_collection_update(db_session, org, install_id, collection_v1)
    await db_session.rollback()
    assert report["update_available"] is True
    assert report["current_version"] == "1.0"
    assert report["available_version"] == "1.1"
    delta = report["delta"]
    assert delta is not None
    assert set(delta) == {"schemas", "agents", "pipelines"}

    schema_entries = {e["slug"]: e for e in delta["schemas"]}
    assert schema_entries["input-schema"]["change"] == "changed"
    assert schema_entries["input-schema"]["action"] == "upgrade"
    assert schema_entries["input-schema"]["entities"][0]["entity_id"] == str(input_local)
    assert schema_entries["input-schema"]["entities"][0]["modified"] is False
    assert schema_entries["output-schema"]["entities"][0]["modified"] is True

    agent_entries = {e["slug"]: e for e in delta["agents"]}
    assert agent_entries["clean-agent"]["change"] == "changed"
    assert agent_entries["clean-agent"]["entities"][0]["modified"] is False
    assert agent_entries["edited-agent"]["entities"][0]["modified"] is True

    # The synthesised pipeline entity is surfaced too.
    pipeline_entries = delta["pipelines"]
    assert len(pipeline_entries) == 1
    assert pipeline_entries[0]["entities"][0]["entity_type"] == "pipeline"

    # --- Upgrade: reconcile unmodified, preserve modified ---
    result = await upgrade_collection(
        db_session,
        org_id=org,
        created_by=user,
        install_id=install_id,
        collection_id=collection_v1,
    )
    await db_session.commit()

    assert result["from_version"] == "1.0"
    assert result["to_version"] == "1.1"
    reconciled = {(r["slug"], r["entity_type"]) for r in result["reconciled"]}
    assert ("input-schema", "schema") in reconciled
    assert ("clean-agent", "agent") in reconciled
    preserved = {(p["slug"], p["entity_type"]) for p in result["preserved"]}
    assert ("output-schema", "schema") in preserved
    assert ("edited-agent", "agent") in preserved

    # --- Verify: reconciled entities match the new pin content ---
    async with db_engine.connect() as conn:
        row = await conn.execute(
            text(
                "SELECT sv.definition_json FROM schema_versions sv "
                "WHERE sv.schema_id = :sid ORDER BY sv.version_number DESC LIMIT 1"
            ),
            {"sid": str(input_local)},
        )
        upgraded_def = row.scalar_one()
        assert upgraded_def == def_a2

        row = await conn.execute(
            text("SELECT prompt_template FROM agents WHERE id = :id"),
            {"id": str(clean_local)},
        )
        assert row.scalar_one() == "P2 clean"

    # --- Verify: modified entities are untouched ---
    async with db_engine.connect() as conn:
        row = await conn.execute(
            text(
                "SELECT sv.definition_json FROM schema_versions sv "
                "WHERE sv.schema_id = :sid ORDER BY sv.version_number DESC LIMIT 1"
            ),
            {"sid": str(output_local)},
        )
        assert row.scalar_one() == def_b
        row = await conn.execute(
            text("SELECT collection_install_id FROM schemas WHERE id = :id"),
            {"id": str(output_local)},
        )
        assert row.scalar_one() is None

        row = await conn.execute(
            text("SELECT prompt_template FROM agents WHERE id = :id"),
            {"id": str(edited_local)},
        )
        assert row.scalar_one() == "OPERATOR CUSTOM PROMPT"

    # --- Verify: the install record now points at the new version row ---
    refreshed = await db_session.get(CollectionInstall, install_id)
    assert refreshed is not None
    assert refreshed.collection_version == "1.1"
    assert refreshed.collection_id == collection_v2

    # --- After the upgrade there is no further update; a second upgrade 409s ---
    report2 = await detect_collection_update(db_session, org, install_id, collection_v2)
    assert report2["update_available"] is False
    assert report2["delta"] is None

    with pytest.raises(NoUpdateAvailableError):
        await upgrade_collection(
            db_session,
            org_id=org,
            created_by=user,
            install_id=install_id,
            collection_id=collection_v2,
        )
    await db_session.rollback()

    # The old (v1) install handle is gone — detection refuses the stale pair.
    with pytest.raises(UpgradeInstallNotFoundError):
        await detect_collection_update(db_session, org, install_id, collection_v1)
    await db_session.rollback()
