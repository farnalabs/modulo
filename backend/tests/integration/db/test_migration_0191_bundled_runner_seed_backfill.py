"""Integration test for migration 0191_bundled_runner_seed_backfill (FAR-702).

Runs the real Alembic ``upgrade`` of revision 0191 against a *fresh, isolated*
live Postgres (a private database cloned from ``template0`` so the shared
session schema is never touched) and proves the deploy-blocking behaviour:

  * the per-org backfill INSERT **executes against a real Postgres** and
    supplies ``id`` via ``gen_random_uuid()`` — this is THE FAR-702 regression.
    The legacy-baseline ``environment_profiles`` table has NO server default on
    its uuid PK, so a raw-SQL INSERT without ``id`` raised
    ``NotNullViolation: null value in column "id"`` and blocked every deploy at
    the pre-migrate step. The backfilled row must carry a real, non-null UUID;
  * the **downgrade** is clean and additive-on-rollback: the scratch capture
    table (``_migration_0191_repoint_state``) is dropped, the head is reset to
    0190, and the backfilled profile row is LEFT IN PLACE (the plan's rollback
    contract — it is never auto-deleted on downgrade).

The migration has never applied successfully on ANY environment, so this is the
first end-to-end execution test for it (the established repo pattern for
deploy-blocking migrations — see ``test_migration_0126_eval_suite.py``).
"""

import types
import uuid
from collections.abc import AsyncIterator
from pathlib import Path

import pytest
import pytest_asyncio
from alembic import command
from alembic.config import Config
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine
from sqlalchemy.pool import NullPool

pytestmark = [pytest.mark.integration]

BACKEND_ROOT = Path(__file__).parents[3]  # backend/
MIGRATION_REV = "0191_bundled_runner_seed_backfill"
PREV_REV = "0190_hitl_claim_context_json"
_STATE_TABLE = "_migration_0191_repoint_state"
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

    Mirrors ``test_migration_0126_eval_suite.py::isolated_db_url``: a private
    database cloned from ``template0`` (guaranteed empty) so this test's upgrade
    + downgrade never mutates the shared session schema. ``env.py`` resolves the
    target DB from ``DATABASE_URL`` / ``DATABASE_ADMIN_URL`` (preferring the
    latter), so both are pinned to the isolated database.
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

    with pytest.MonkeyPatch().context() as mp:
        mp.setenv("DATABASE_URL", iso_url)
        mp.setenv("DATABASE_ADMIN_URL", iso_url)
        command.upgrade(_alembic_config(iso_url), PREV_REV)

    yield iso_url

    # Best-effort cleanup of the private database.
    admin_engine = create_async_engine(db_url, poolclass=NullPool, execution_options={"isolation_level": "AUTOCOMMIT"})
    async with admin_engine.connect() as conn:
        await conn.execute(text(f'DROP DATABASE IF EXISTS "{db_name}"'))
    await admin_engine.dispose()


async def _seed_org(engine: AsyncEngine, org_id: uuid.UUID, account_id: uuid.UUID) -> None:
    """Create an org with an admin membership and no environment profiles."""
    async with engine.begin() as conn:
        await conn.execute(
            text(
                "INSERT INTO organisations (id, name, slug, settings_json, created_by) "
                "VALUES (:o, 'm0191-org', 'm0191-org', '{}'::json, :a)"
            ),
            {"o": str(org_id), "a": str(account_id)},
        )
        await conn.execute(
            text(
                "INSERT INTO accounts (id, email, display_name, password_hash, auth_provider, active) "
                "VALUES (:a, 'm0191@example.com', 'm0191', 'hash', 'local', true)"
            ),
            {"a": str(account_id)},
        )
        await conn.execute(
            text("INSERT INTO org_memberships (id, account_id, organisation_id, role) VALUES (:m, :a, :o, 'admin')"),
            {"m": str(uuid.uuid4()), "a": str(account_id), "o": str(org_id)},
        )


async def test_0191_backfill_executes_with_uuid_id(isolated_db_url: str) -> None:
    db_url = isolated_db_url
    engine = create_async_engine(db_url, poolclass=NullPool)

    org_id = uuid.uuid4()
    account_id = uuid.uuid4()
    try:
        await _seed_org(engine, org_id, account_id)

        # Run the real migration (backfill executes here — this is where the
        # FAR-702 NotNullViolation previously aborted every deploy).
        command.upgrade(_alembic_config(db_url), MIGRATION_REV)

        async with engine.connect() as conn:
            rows = (
                (
                    await conn.execute(
                        text("SELECT id, name, provider_type FROM environment_profiles WHERE organisation_id = :o"),
                        {"o": str(org_id)},
                    )
                )
                .mappings()
                .all()
            )

        # Exactly one backfilled Bundled Runner profile for the org.
        backfills = [r for r in rows if r["provider_type"] == "runner_docker" and r["name"] == _TEMPLATE_NAME]
        assert len(backfills) == 1, f"expected one Bundled Runner backfill, got {rows}"
        # FAR-702 regression: the INSERT supplied a real, non-null UUID id via
        # gen_random_uuid() — without it the raw-SQL INSERT hit NotNullViolation.
        backed_id = backfills[0]["id"]
        assert backed_id is not None, "backfill row id must be non-null (FAR-702 regression)"
        assert isinstance(uuid.UUID(str(backed_id)), uuid.UUID), "backfill id must be a valid UUID"
    finally:
        await engine.dispose()


async def test_0191_downgrade_is_clean_and_additive(isolated_db_url: str) -> None:
    db_url = isolated_db_url
    engine = create_async_engine(db_url, poolclass=NullPool)

    org_id = uuid.uuid4()
    account_id = uuid.uuid4()
    try:
        await _seed_org(engine, org_id, account_id)
        command.upgrade(_alembic_config(db_url), MIGRATION_REV)

        # Downgrade one step (run 0191's downgrade). env.py's boot fast-path
        # skips programmatic invocations unless the direction is forced.
        config = _alembic_config(db_url)
        config.cmd_opts = types.SimpleNamespace(command="downgrade")
        command.downgrade(config, "-1")

        async with engine.connect() as conn:
            # Scratch capture table must be dropped on downgrade.
            scratch = (await conn.execute(text("SELECT to_regclass(:t)"), {"t": _STATE_TABLE})).scalar()
            assert scratch is None, f"scratch table {_STATE_TABLE} should be dropped on downgrade"

            # Backfilled profile is LEFT IN PLACE (additive-on-rollback contract).
            remaining = (
                await conn.execute(
                    text(
                        "SELECT count(*) FROM environment_profiles "
                        "WHERE organisation_id = :o AND name = :n AND provider_type = 'runner_docker'"
                    ),
                    {"o": str(org_id), "n": _TEMPLATE_NAME},
                )
            ).scalar()
            assert remaining == 1, f"backfill row must remain after downgrade, got {remaining}"

            # Head reset to the parent revision.
            head = (await conn.execute(text("SELECT version_num FROM alembic_version"))).scalar()
            assert head == PREV_REV, f"head should reset to {PREV_REV}, got {head}"
    finally:
        await engine.dispose()
