"""Integration test for migration 0291 — invitations live-lookup indexes + CHECKs.

The 0291 index DDL is raw, ``public.``-qualified Postgres SQL (the 0177
precedent), so the SQLite-portable unit template cannot execute it at all: a
true upgrade/downgrade round-trip and the CHECK legs (Postgres-only, skipped on
non-Postgres dialects) can only be proven against real Postgres. This module
migrates a private database to ``0290`` (the pre-0291 head), applies the REAL
0291 upgrade, and asserts:

* both partial live-lookup indexes exist with the exact ordered key columns and
  the shared un-consumed / un-revoked predicate;
* both domain CHECKs exist and actually REJECT a bad ``org_role`` and a
  non-64-char ``token_hash`` while accepting a valid invitation;
* the downgrade drops both indexes and both CHECKs, and a re-upgrade restores
  them (round-trip).
"""

from __future__ import annotations

import uuid
from pathlib import Path
from urllib.parse import urlparse, urlunparse

import pytest
import pytest_asyncio
from alembic import command
from alembic.config import Config
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine
from sqlalchemy.pool import NullPool
from tests.integration.test_org_sandbox_capacity import _seed_org_account

pytestmark = [pytest.mark.integration]

BACKEND_ROOT = Path(__file__).parents[3]  # backend/
MIGRATION_REV = "0291_invitations_lookup_constraints"
PREV_REV = "0290_scheduled_reports_due_scan"

_ORG_EMAIL_INDEX = "ix_invitations_org_email_live"
_EXPIRES_INDEX = "ix_invitations_expires_at_live"
_ORG_ROLE_CHECK = "ck_invitations_org_role"
_TOKEN_HASH_CHECK = "ck_invitations_token_hash_len"


def _alembic_config(db_url: str) -> Config:
    config = Config(BACKEND_ROOT / "alembic.ini")
    config.set_main_option("sqlalchemy.url", db_url)
    config.set_main_option("script_location", str(BACKEND_ROOT / "src" / "modulo" / "db" / "migrations"))
    config.config_file_name = None
    return config


def _swap_db_name(db_url: str, new_db: str) -> str:
    parsed = urlparse(db_url)
    return urlunparse(parsed._replace(path=f"/{new_db}"))


def _run_alembic(iso_url: str, cmd: str, target: str) -> None:
    """Run an alembic command against the isolated DB via the Python API.

    ``env.py`` resolves the target DB from ``DATABASE_URL`` / ``DATABASE_ADMIN_URL``,
    so both are pinned for the duration of the call.
    """
    with pytest.MonkeyPatch().context() as mp:
        mp.setenv("DATABASE_URL", iso_url)
        mp.setenv("DATABASE_ADMIN_URL", iso_url)
        getattr(command, cmd)(_alembic_config(iso_url), target)


@pytest_asyncio.fixture
async def isolated_db_url(db_url: str, monkeypatch: pytest.MonkeyPatch):
    """A fresh, private Postgres database migrated only up to ``PREV_REV``.

    Mirrors ``test_migration_0262`` / ``test_migration_0275``: a private DB
    cloned from ``template0`` so this test's migration never mutates the shared
    session schema.
    """
    admin_engine = create_async_engine(db_url, poolclass=NullPool, execution_options={"isolation_level": "AUTOCOMMIT"})
    db_name = f"m0291_iso_{uuid.uuid4().hex[:10]}"
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
    _run_alembic(iso_url, "upgrade", PREV_REV)

    yield iso_url

    admin_engine = create_async_engine(db_url, poolclass=NullPool, execution_options={"isolation_level": "AUTOCOMMIT"})
    async with admin_engine.connect() as conn:
        await conn.execute(
            text(
                "SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname = :n AND pid <> pg_backend_pid()"
            ).bindparams(n=db_name)
        )
        await conn.execute(text(f'DROP DATABASE IF EXISTS "{db_name}"'))
    await admin_engine.dispose()


async def _index_def(engine: AsyncEngine, name: str) -> str | None:
    async with engine.connect() as conn:
        row = await conn.execute(
            text(
                "SELECT indexdef FROM pg_indexes "
                "WHERE schemaname = 'public' AND tablename = 'invitations' AND indexname = :n"
            ),
            {"n": name},
        )
        return row.scalar_one_or_none()


async def _constraint_def(engine: AsyncEngine, name: str) -> str | None:
    async with engine.connect() as conn:
        row = await conn.execute(
            text(
                "SELECT pg_get_constraintdef(oid) FROM pg_constraint "
                "WHERE conname = :n AND conrelid = 'public.invitations'::regclass"
            ),
            {"n": name},
        )
        return row.scalar_one_or_none()


async def _insert_invitation(
    engine: AsyncEngine,
    *,
    org_id: uuid.UUID,
    account_id: uuid.UUID,
    org_role: str,
    token_hash: str,
) -> None:
    async with engine.begin() as conn:
        await conn.execute(
            text(
                "INSERT INTO invitations (id, organisation_id, email, display_name, "
                "org_role, token_hash, invited_by, expires_at) "
                "VALUES (:id, :o, :e, 'Invitee', :r, :t, :b, now() + interval '1 day')"
            ),
            {
                "id": str(uuid.uuid4()),
                "o": str(org_id),
                "e": f"{uuid.uuid4().hex[:8]}@m0291.test",
                "r": org_role,
                "t": token_hash,
                "b": str(account_id),
            },
        )


async def test_0291_indexes_checks_and_roundtrip(isolated_db_url: str) -> None:
    engine = create_async_engine(isolated_db_url, poolclass=NullPool)
    try:
        _run_alembic(isolated_db_url, "upgrade", MIGRATION_REV)

        async with engine.connect() as conn:
            version = (await conn.execute(text("SELECT version_num FROM alembic_version"))).scalar_one()
        assert version == MIGRATION_REV

        # --- Index shape + predicate parity (real DDL, not substring-routed mocks) ---
        org_email_def = await _index_def(engine, _ORG_EMAIL_INDEX)
        assert org_email_def is not None, f"{_ORG_EMAIL_INDEX} missing after 0291"
        assert "(organisation_id, email)" in org_email_def, org_email_def
        assert "consumed_at IS NULL" in org_email_def, org_email_def
        assert "revoked_at IS NULL" in org_email_def, org_email_def

        expires_def = await _index_def(engine, _EXPIRES_INDEX)
        assert expires_def is not None, f"{_EXPIRES_INDEX} missing after 0291"
        assert "(expires_at)" in expires_def, expires_def
        assert "consumed_at IS NULL" in expires_def, expires_def
        assert "revoked_at IS NULL" in expires_def, expires_def

        # --- CHECK presence ---
        org_role_def = await _constraint_def(engine, _ORG_ROLE_CHECK)
        assert org_role_def is not None, f"{_ORG_ROLE_CHECK} missing after 0291"
        for role in ("admin", "operator", "runner", "viewer"):
            assert role in org_role_def, org_role_def
        token_hash_def = await _constraint_def(engine, _TOKEN_HASH_CHECK)
        assert token_hash_def is not None, f"{_TOKEN_HASH_CHECK} missing after 0291"
        assert "length" in token_hash_def, token_hash_def
        assert "64" in token_hash_def, token_hash_def

        # --- CHECK effectiveness: the constraints actually refuse bad rows ---
        org_id, account_id = await _seed_org_account(engine, "M0291Org", cap=None)
        await _insert_invitation(engine, org_id=org_id, account_id=account_id, org_role="admin", token_hash="a" * 64)
        with pytest.raises(IntegrityError, match=_ORG_ROLE_CHECK):
            await _insert_invitation(
                engine, org_id=org_id, account_id=account_id, org_role="superuser", token_hash="b" * 64
            )
        with pytest.raises(IntegrityError, match=_TOKEN_HASH_CHECK):
            await _insert_invitation(
                engine, org_id=org_id, account_id=account_id, org_role="viewer", token_hash="deadbeef"
            )

        # --- Round-trip: downgrade removes both indexes and both CHECKs ---
        _run_alembic(isolated_db_url, "downgrade", PREV_REV)
        assert await _index_def(engine, _ORG_EMAIL_INDEX) is None
        assert await _index_def(engine, _EXPIRES_INDEX) is None
        assert await _constraint_def(engine, _ORG_ROLE_CHECK) is None
        assert await _constraint_def(engine, _TOKEN_HASH_CHECK) is None

        # --- Re-upgrade restores them ---
        _run_alembic(isolated_db_url, "upgrade", MIGRATION_REV)
        assert await _index_def(engine, _ORG_EMAIL_INDEX) is not None
        assert await _index_def(engine, _EXPIRES_INDEX) is not None
        assert await _constraint_def(engine, _ORG_ROLE_CHECK) is not None
        assert await _constraint_def(engine, _TOKEN_HASH_CHECK) is not None
    finally:
        await engine.dispose()
