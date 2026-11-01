"""FAR-2265: boot seeding of system schemas under the real RLS-bound app role.

``schemas`` is protected by ``rls_org_isolation``. The boot seeder opens a fresh
session per org, so unless ``seed_system_schemas`` scopes the transaction to the
org itself, the existence check sees no rows and the INSERT violates the policy
(``system_schemas.seed_org_failed``). SQLite cannot reproduce this, so the test
runs as the non-superuser ``modulo_app`` role against a migrated Postgres.
"""

import uuid

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker

from modulo.db.seed import SYSTEM_SCHEMAS, seed_system_schemas

pytestmark = pytest.mark.integration


async def _count_system_schemas(db_engine: AsyncEngine, org_id: uuid.UUID) -> int:
    async with db_engine.connect() as conn:
        result = await conn.execute(
            text("SELECT count(*) FROM schemas WHERE organisation_id = :oid AND system = true"),
            {"oid": str(org_id)},
        )
        return int(result.scalar_one())


async def test_seed_system_schemas_succeeds_without_caller_rls_context(
    modulo_app_engine: AsyncEngine, db_engine: AsyncEngine
) -> None:
    org_id = uuid.uuid4()
    account_id = uuid.uuid4()
    async with db_engine.connect() as conn, conn.begin():
        await conn.execute(
            text(
                "INSERT INTO organisations (id, name, slug, settings_json) VALUES (:id, 'seed rls', :slug, '{}'::json)"
            ),
            {"id": str(org_id), "slug": f"seed-{org_id.hex[:8]}"},
        )
        await conn.execute(
            text(
                "INSERT INTO accounts (id, email, display_name, password_hash, auth_provider, active) "
                "VALUES (:id, :email, 'seed', 'hash', 'local', true)"
            ),
            {"id": str(account_id), "email": f"seed-{account_id.hex[:8]}@example.com"},
        )
        await conn.execute(
            text(
                "INSERT INTO org_memberships (id, organisation_id, account_id, role) VALUES (:id, :oid, :aid, 'admin')"
            ),
            {"id": str(uuid.uuid4()), "oid": str(org_id), "aid": str(account_id)},
        )

    factory = async_sessionmaker(modulo_app_engine, expire_on_commit=False)
    # Fresh session, NO set_rls_org by the caller: exactly the boot seeder shape.
    for _ in range(2):  # second pass proves idempotency
        async with factory() as session, session.begin():
            await seed_system_schemas(session, org_id, account_id)

    assert await _count_system_schemas(db_engine, org_id) == len(SYSTEM_SCHEMAS)
