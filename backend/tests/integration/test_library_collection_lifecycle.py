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
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from modulo.core.library_service.grant import grant_collection_agents
from modulo.core.library_service.install import install_collection
from modulo.core.library_service.runnability import compute_runnable
from modulo.core.library_service.uninstall import uninstall_collection
from modulo.db.models.collection_install import CollectionInstall

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
) -> uuid.UUID:
    prim_id = uuid.uuid4()
    content = {"definition_json": {"type": "object", "properties": {}}}
    async with db_engine.connect() as conn, conn.begin():
        await conn.execute(
            text(
                "INSERT INTO library_primitives "
                "(id, organisation_id, source, primitive_type, name, slug, "
                "author, version, tags, content_json, visibility, tier) "
                "VALUES (:id, :oid, 'local', 'schema', :name, :slug, "
                "'test', '1.0', '[]'::json, :content, 'org', 'native')"
            ),
            {
                "id": str(prim_id),
                "oid": str(org_id),
                "name": name or slug,
                "slug": slug,
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
) -> uuid.UUID:
    prim_id = uuid.uuid4()
    content: dict[str, Any] = {
        "input_schema_id": input_schema_id,
        "output_schema_id": output_schema_id,
        "prompt_template": "You are a test agent.",
        "connector_type_refs": [],
    }
    async with db_engine.connect() as conn, conn.begin():
        await conn.execute(
            text(
                "INSERT INTO library_primitives "
                "(id, organisation_id, source, primitive_type, name, slug, "
                "author, version, tags, content_json, visibility, tier) "
                "VALUES (:id, :oid, 'local', 'agent', :name, :slug, "
                "'test', '1.0', '[]'::json, :content, 'org', 'native')"
            ),
            {
                "id": str(prim_id),
                "oid": str(org_id),
                "name": name or slug,
                "slug": slug,
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
) -> uuid.UUID:
    prim_id = uuid.uuid4()
    async with db_engine.connect() as conn, conn.begin():
        await conn.execute(
            text(
                "INSERT INTO library_primitives "
                "(id, organisation_id, source, primitive_type, name, slug, "
                "author, version, tags, content_json, visibility, tier, "
                "status, manifest_pins) "
                "VALUES (:id, :oid, :source, 'library_collection', :name, :slug, "
                "'test', '1.0', '[]'::json, '{}'::json, 'org', 'native', "
                ":status, :pins)"
            ),
            {
                "id": str(prim_id),
                "oid": str(org_id),
                "source": source,
                "name": "Test Collection",
                "slug": slug,
                "status": status,
                "pins": __import__("json").dumps(manifest_pins),
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
            assert r is not None and r[0] is not None

        for new_agent_id in agent_id_map.values():
            row = await conn.execute(
                text("SELECT collection_install_id FROM agents WHERE id = :id"),
                {"id": new_agent_id},
            )
            r = row.first()
            assert r is not None and r[0] is not None

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

    assert len(result["deleted"]) > 0
    assert result["detached"] == []

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
