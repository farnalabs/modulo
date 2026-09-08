"""Integration test for migration 0191_bundled_runner_seed_backfill (FAR-701).

The production blocker: the backfill INSERT-SELECT omitted ``id`` (the column
has no DB server default — UUIDs are normally generated client-side by
SQLAlchemy), so on ANY database containing organisations the migration failed
with ``NotNullViolationError: null value in column "id" of relation
"environment_profiles"``. CI never caught it because a fresh testcontainer DB
has no organisations — the INSERT-SELECT inserted zero rows and passed
vacuously.

This test seeds POPULATED org data BEFORE 0191 runs (against a real Postgres
via testcontainers) and asserts, after the real ``alembic upgrade``:

  * the upgrade succeeds on populated data (the regression itself);
  * a legacy ``modulo-dev`` row is RE-POINTED in place (name +
    provider_type updated to the shipped template) — NOT duplicated;
  * an org without a legacy row is backfilled exactly one Bundled Runner
    profile with a server-generated id;
  * the orphan sentinel org (0172, no members, NULL created_by) gets NO
    profile (NULL-owner skip);
  * every environment_profiles row has a non-NULL account_id;
  * the downgrade reverts EXACTLY the re-pointed row (captured pre-migration
    identity restored) and leaves backfill rows in place.

The isolated-database fixture pattern mirrors ``test_migration_0126_eval_suite``:
clone ``template0`` into a private database, upgrade to the revision BEFORE
0191, seed, then run the real 0191 migration.
"""

import os
import types
import uuid
from collections.abc import AsyncIterator
from pathlib import Path
from unittest.mock import patch

import pytest
import pytest_asyncio
from alembic import command
from alembic.config import Config
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import NullPool

pytestmark = [pytest.mark.integration]

BACKEND_ROOT = Path(__file__).parents[3]  # backend/

MIGRATION_REV = "0191_bundled_runner_seed_backfill"
PREV_REV = "0190_hitl_claim_context_json"

ORPHAN_ORG_ID = "00000000-0000-0000-0000-000000000000"

# Distinctive pre-migration identity of the seeded legacy row, so the re-point
# and downgrade assertions can prove the ORIGINAL values are captured/restored.
_LEGACY_NAME = "modulo-dev"
_LEGACY_DESCRIPTION = "legacy local docker runner (pre-0191)"
_LEGACY_IMAGE_REF = "local-modulo:dev"
_LEGACY_CONFIG_JSON = '{"legacy": true}'

_TEMPLATE_NAME = "Bundled Runner (Docker)"


def _alembic_config(db_url: str) -> Config:
    config = Config(BACKEND_ROOT / "alembic.ini")
    config.set_main_option("sqlalchemy.url", db_url)
    config.set_main_option(
        "script_location",
        str(BACKEND_ROOT / "src" / "modulo" / "db" / "migrations"),
    )
    config.config_file_name = None
    return config


def _swap_db_name(db_url: str, new_db: str) -> str:
    """Return ``db_url`` with its database name replaced by ``new_db``."""
    from urllib.parse import urlparse, urlunparse

    parsed = urlparse(db_url)
    return urlunparse(parsed._replace(path=f"/{new_db}"))


@pytest_asyncio.fixture
async def isolated_db_url(db_url: str, monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[str]:
    """A fresh, private Postgres database migrated only up to ``PREV_REV``.

    0191 mutates ``environment_profiles`` rows for every organisation in the
    database; running it against the shared session ``migrated_db_url`` would
    rewrite (and, on downgrade, relabel) rows other integration tests rely on.
    Give this test its own database so the shared schema is never touched.

    The isolated database is provisioned from ``template0`` (guaranteed empty)
    rather than the default ``template1``. ``migrations/env.py`` overrides the
    alembic ``sqlalchemy.url`` with ``DATABASE_URL``/``DATABASE_ADMIN_URL`` from
    the environment, so both are pinned to the isolated database here —
    otherwise ``command.upgrade`` would run against the shared session database.
    """
    admin_engine = create_async_engine(db_url, poolclass=NullPool, execution_options={"isolation_level": "AUTOCOMMIT"})
    db_name = f"m0191_iso_{uuid.uuid4().hex[:10]}"
    async with admin_engine.connect() as conn:
        await conn.execute(text(f'CREATE DATABASE "{db_name}" WITH TEMPLATE template0'))
    await admin_engine.dispose()

    iso_url = _swap_db_name(db_url, db_name)
    monkeypatch.setenv("DATABASE_URL", iso_url)
    monkeypatch.setenv("DATABASE_ADMIN_URL", iso_url)
    eng = create_async_engine(iso_url, poolclass=NullPool)
    async with eng.connect() as conn:
        await conn.execute(
            text("CREATE TABLE IF NOT EXISTS alembic_version (version_num VARCHAR(255) NOT NULL PRIMARY KEY)")
        )
        await conn.commit()
    await eng.dispose()

    with patch.dict(
        os.environ,
        {"DATABASE_URL": iso_url, "DATABASE_ADMIN_URL": iso_url},
    ):
        command.upgrade(_alembic_config(iso_url), PREV_REV)

    try:
        yield iso_url
    finally:
        admin_engine = create_async_engine(
            db_url, poolclass=NullPool, execution_options={"isolation_level": "AUTOCOMMIT"}
        )
        async with admin_engine.connect() as conn:
            await conn.execute(
                text(
                    "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                    "WHERE datname = :n AND pid <> pg_backend_pid()"
                ).bindparams(n=db_name)
            )
            await conn.execute(text(f'DROP DATABASE IF EXISTS "{db_name}"'))
        await admin_engine.dispose()


async def _seed_populated_orgs(
    db_url: str,
    org_repoint: uuid.UUID,
    org_backfill: uuid.UUID,
    org_nullowner: uuid.UUID,
) -> None:
    """Seed populated org data at PREV_REV, before 0191 runs.

    * ``org_repoint`` — a healthy org with an active admin membership and a
      live legacy ``modulo-dev`` (local_docker) profile row: exercises the
      IN-PLACE re-point path.
    * ``org_backfill`` — a healthy org with an active admin membership and NO
      profile row: exercises the per-org backfill INSERT (the path that hit
      the missing-``id`` NOT NULL violation on every populated database).
    * ``org_nullowner`` — a NON-ORPHAN org (random UUID) with NO memberships
      and a NULL ``created_by``: exercises the ``owner.account_id IS NOT NULL``
      guard in the backfill's owner LATERAL. Its resolved owner is NULL, so the
      backfill must SKIP it rather than violating the NOT NULL account_id
      constraint (the orphan sentinel org is excluded by a different rule, so
      this proves the NULL-owner guard independently).

    The orphan sentinel org (no members, NULL created_by, nil UUID) is already
    present — migration 0172 seeds it earlier in the chain and 0191 must skip
    it too.
    """
    engine = create_async_engine(db_url, poolclass=NullPool)
    try:
        async with engine.begin() as conn:
            for oid, slug in [(org_repoint, "m0191-repoint-org"), (org_backfill, "m0191-backfill-org")]:
                await conn.execute(
                    text("INSERT INTO organisations (id, name, slug, settings_json) VALUES (:id, :n, :s, '{}'::json)"),
                    {"id": str(oid), "n": slug, "s": slug},
                )
                await conn.execute(
                    text(
                        "INSERT INTO accounts (id, email, display_name, password_hash, "
                        "auth_provider, active) VALUES (:id, :e, :n, 'hash', 'local', true)"
                    ),
                    {"id": str(uuid.uuid4()), "e": f"{slug}@example.com", "n": slug},
                )
                await conn.execute(
                    text(
                        "INSERT INTO org_memberships (id, organisation_id, account_id, role) "
                        "VALUES (:id, :oid, (SELECT id FROM accounts WHERE email = :e), 'admin')"
                    ),
                    {"id": str(uuid.uuid4()), "oid": str(oid), "e": f"{slug}@example.com"},
                )

            # org_nullowner: a non-orphan org with NO memberships and a NULL
            # created_by. It must NOT receive a backfilled profile.
            await conn.execute(
                text("INSERT INTO organisations (id, name, slug, settings_json) VALUES (:id, :n, :s, '{}'::json)"),
                {"id": str(org_nullowner), "n": "m0191-nullowner-org", "s": "m0191-nullowner-org"},
            )

            # Legacy modulo-dev row for the re-point org only.
            await conn.execute(
                text(
                    "INSERT INTO environment_profiles "
                    "(id, organisation_id, account_id, name, description, provider_type, "
                    "image_ref, capabilities_json, config_json) "
                    "VALUES (:id, :oid, (SELECT id FROM accounts WHERE email = :e), "
                    ":name, :description, 'local_docker', :image_ref, '[]'::json, "
                    "CAST(:config AS jsonb))"
                ),
                {
                    "id": str(uuid.uuid4()),
                    "oid": str(org_repoint),
                    "e": "m0191-repoint-org@example.com",
                    "name": _LEGACY_NAME,
                    "description": _LEGACY_DESCRIPTION,
                    "image_ref": _LEGACY_IMAGE_REF,
                    "config": _LEGACY_CONFIG_JSON,
                },
            )
    finally:
        await engine.dispose()


async def _live_profiles(db_url: str, org: uuid.UUID | None = None) -> list:
    """Fetch live environment_profiles rows (optionally scoped to one org)."""
    engine = create_async_engine(db_url, poolclass=NullPool)
    try:
        async with engine.connect() as conn:
            if org is None:
                rows = (
                    await conn.execute(
                        text(
                            "SELECT organisation_id::text, name, provider_type, account_id::text "
                            "FROM environment_profiles WHERE deleted_at IS NULL"
                        )
                    )
                ).fetchall()
            else:
                rows = (
                    await conn.execute(
                        text(
                            "SELECT organisation_id::text, name, provider_type, account_id::text "
                            "FROM environment_profiles WHERE deleted_at IS NULL AND organisation_id = :o"
                        ),
                        {"o": str(org)},
                    )
                ).fetchall()
    finally:
        await engine.dispose()
    return rows


async def test_0191_repoint_and_backfill_on_populated_database(isolated_db_url, monkeypatch) -> None:
    db_url = isolated_db_url
    monkeypatch.setenv("DATABASE_URL", db_url)
    monkeypatch.setenv("DATABASE_ADMIN_URL", db_url)
    config = _alembic_config(db_url)

    org_repoint = uuid.uuid4()
    org_backfill = uuid.uuid4()
    org_nullowner = uuid.uuid4()
    await _seed_populated_orgs(db_url, org_repoint, org_backfill, org_nullowner)

    # The orphan sentinel org must exist at PREV_REV (seeded by 0172).
    orphan_rows = await _live_profiles(db_url, uuid.UUID(ORPHAN_ORG_ID))
    assert orphan_rows == []

    # Run the real migration — 0191 executes here. Before the fix this raised
    # NotNullViolationError on any DB containing organisations (FAR-701).
    command.upgrade(config, "heads")

    # --- Re-point org: exactly ONE live profile, updated IN PLACE (no duplicate) ---
    repoint_rows = await _live_profiles(db_url, org_repoint)
    assert len(repoint_rows) == 1, "re-pointed org must keep exactly one live profile"
    assert repoint_rows[0][1] == _TEMPLATE_NAME, "legacy row should be re-pointed to the template name"
    assert repoint_rows[0][2] == "runner_docker", "legacy row should be re-pointed to runner_docker"

    # --- Backfill org: exactly ONE inserted profile with a generated id ---
    backfill_rows = await _live_profiles(db_url, org_backfill)
    assert len(backfill_rows) == 1, "backfill org should receive exactly one Bundled Runner profile"
    assert backfill_rows[0][1] == _TEMPLATE_NAME
    assert backfill_rows[0][2] == "runner_docker"

    # --- Orphan sentinel org: NO profile at all (NULL-owner skip) ---
    orphan_rows = await _live_profiles(db_url, uuid.UUID(ORPHAN_ORG_ID))
    assert orphan_rows == [], "orphan sentinel org must not own a Bundled Runner profile"

    # --- NULL-owner non-orphan org: NO profile (owner.account_id IS NOT NULL guard) ---
    nullowner_rows = await _live_profiles(db_url, org_nullowner)
    assert nullowner_rows == [], "non-orphan org with no members and NULL created_by must not own a profile"

    # --- Global NOT NULL sanity: every live profile row has an owner ---
    all_rows = await _live_profiles(db_url)
    null_owners = [r for r in all_rows if r[3] is None]
    assert null_owners == [], "every environment_profiles row must have a non-NULL account_id"

    # --- Downgrade reverts EXACTLY the re-pointed row (captured identity) ---
    # env.py's boot fast-path skips migrations when invoked programmatically
    # (config.cmd_opts is None -> _invocation_is_upgrade() wrongly True, and
    # the DB is at head). Force the downgrade direction so it actually runs.
    config.cmd_opts = types.SimpleNamespace(command="downgrade")
    command.downgrade(config, "-1")

    engine = create_async_engine(db_url, poolclass=NullPool)
    try:
        async with engine.connect() as conn:
            reverted = (
                await conn.execute(
                    text(
                        "SELECT name, provider_type, description, image_ref, config_json::text "
                        "FROM environment_profiles WHERE organisation_id = :o AND deleted_at IS NULL"
                    ),
                    {"o": str(org_repoint)},
                )
            ).fetchall()
            assert len(reverted) == 1, "downgrade must leave the re-point org with exactly one profile"
            assert reverted[0][0] == _LEGACY_NAME, "downgrade must restore the original name"
            assert reverted[0][1] == "local_docker", "downgrade must restore local_docker"
            assert reverted[0][2] == _LEGACY_DESCRIPTION, "downgrade must restore the original description"
            assert reverted[0][3] == _LEGACY_IMAGE_REF, "downgrade must restore the original image_ref"
            assert reverted[0][4] == _LEGACY_CONFIG_JSON, "downgrade must restore the original config_json"

            # Backfill rows are LEFT IN PLACE per the plan's rollback contract.
            backfilled = (
                await conn.execute(
                    text("SELECT count(*) FROM environment_profiles WHERE organisation_id = :o AND deleted_at IS NULL"),
                    {"o": str(org_backfill)},
                )
            ).scalar()
            assert backfilled == 1, "downgrade must leave backfill rows in place"

            scratch = (
                await conn.execute(
                    text(
                        "SELECT count(*) FROM information_schema.tables "
                        "WHERE table_name = '_migration_0191_repoint_state'"
                    )
                )
            ).scalar()
            assert scratch == 0, "the re-point scratch table must be dropped by the downgrade"
    finally:
        await engine.dispose()
