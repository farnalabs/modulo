"""Integration test for migration 0311_service_accounts_lifecycle (FAR-1478).

Against REAL Postgres (testcontainers, private database per test — the
``test_migration_0306`` harness):

* seed a human account + a service account + their org memberships at
  ``0310_library_cluster_db_hardening`` (the deployed shape, where neither the
  decommission column nor the role-ceiling trigger exists);
* the REAL alembic ``upgrade`` of ``0311_service_accounts_lifecycle``:
  - ``trg_org_memberships_service_role_ceiling`` REFUSES an ``admin`` org role
    for the service account (SQLSTATE 23514, the check_violation the
    application surfaces mirror as 422) on BOTH insert and update, while an
    ``operator`` grant and an ``admin`` grant for the HUMAN account still pass;
  - ``ck_accounts_decommissioned_service`` refuses a decommissioned HUMAN row
    and accepts one on the service row;
  - a downgrade drops the trigger, the function, the CHECK and the column.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncGenerator
from pathlib import Path
from urllib.parse import urlparse, urlunparse

import pytest
import pytest_asyncio
from alembic import command
from alembic.config import Config
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine
from sqlalchemy.pool import NullPool

pytestmark = [pytest.mark.integration]

BACKEND_ROOT = str(Path(__file__).resolve().parents[3])

MIGRATION_REV = "0311_service_accounts_lifecycle"
PREV_REV = "0310_library_cluster_db_hardening"


def _alembic_config(db_url: str) -> Config:
    config = Config(f"{BACKEND_ROOT}/alembic.ini")
    config.set_main_option("sqlalchemy.url", db_url)
    config.set_main_option("script_location", f"{BACKEND_ROOT}/src/modulo/db/migrations")
    config.config_file_name = None
    return config


def _swap_db_name(db_url: str, new_db: str) -> str:
    parsed = urlparse(db_url)
    return urlunparse(parsed._replace(path=f"/{new_db}"))


def _alembic_cmd(iso_url: str, cmd: str, target: str) -> None:
    cfg = _alembic_config(iso_url)
    assert getattr(cfg, "cmd_opts", None) is None, "Python-API invocation must carry no cmd_opts"
    getattr(command, cmd)(cfg, target)


async def _fresh_db(db_url: str, prefix: str) -> tuple[str, str]:
    admin_engine = create_async_engine(db_url, poolclass=NullPool, execution_options={"isolation_level": "AUTOCOMMIT"})
    db_name = f"{prefix}_{uuid.uuid4().hex[:10]}"
    async with admin_engine.connect() as conn:
        await conn.execute(text(f'CREATE DATABASE "{db_name}" WITH TEMPLATE template0'))
    await admin_engine.dispose()
    return db_name, _swap_db_name(db_url, db_name)


async def _drop_db(db_url: str, db_name: str) -> None:
    admin_engine = create_async_engine(db_url, poolclass=NullPool, execution_options={"isolation_level": "AUTOCOMMIT"})
    async with admin_engine.connect() as conn:
        await conn.execute(text(f'DROP DATABASE IF EXISTS "{db_name}"'))
    await admin_engine.dispose()


def _migrate(iso_url: str, target: str) -> None:
    with pytest.MonkeyPatch().context() as mp:
        mp.setenv("DATABASE_URL", iso_url)
        mp.setenv("DATABASE_ADMIN_URL", iso_url)
        _alembic_cmd(iso_url, "upgrade", target)


def _downgrade(iso_url: str, target: str) -> None:
    with pytest.MonkeyPatch().context() as mp:
        mp.setenv("DATABASE_URL", iso_url)
        mp.setenv("DATABASE_ADMIN_URL", iso_url)
        _alembic_cmd(iso_url, "downgrade", target)


@pytest_asyncio.fixture
async def upgraded_db_url(db_url: str) -> AsyncGenerator[str, None]:
    """Private Postgres migrated to 0311 with human + service principals seeded at 0310_library_cluster_db_hardening."""
    db_name, iso_url = await _fresh_db(db_url, "m0311")
    try:
        _migrate(iso_url, PREV_REV)
        engine = create_async_engine(iso_url, poolclass=NullPool)
        try:
            await _seed(engine)
        finally:
            await engine.dispose()
        _migrate(iso_url, MIGRATION_REV)
        yield iso_url
    finally:
        await _drop_db(db_url, db_name)


async def _seed(engine: AsyncEngine) -> dict[str, uuid.UUID]:
    """Seed at PREV_REV: one human admin + one service operator, same org."""
    ids = {
        "human": uuid.uuid4(),
        "service": uuid.uuid4(),
        "org": uuid.uuid4(),
        "human_membership": uuid.uuid4(),
        "service_membership": uuid.uuid4(),
    }
    async with engine.begin() as conn:
        await conn.execute(
            text(
                "INSERT INTO organisations (id, name, slug, settings_json) VALUES (:id, 'm0311', 'm0311', '{}'::json)"
            ),
            {"id": str(ids["org"])},
        )
        await conn.execute(
            text(
                "INSERT INTO accounts (id, email, display_name, password_hash, auth_provider, active) "
                "VALUES (:id, 'm0311-human@example.com', 'm0311-human', 'hash', 'local', true)"
            ),
            {"id": str(ids["human"])},
        )
        # The FAR-1385 shape: a service row carries NO human-shaped identity.
        await conn.execute(
            text(
                "INSERT INTO accounts (id, email, display_name, password_hash, auth_provider, active, kind) "
                "VALUES (:id, NULL, 'm0311-svc', NULL, NULL, true, 'service')"
            ),
            {"id": str(ids["service"])},
        )
        await conn.execute(
            text(
                "INSERT INTO org_memberships (id, account_id, organisation_id, role) VALUES (:mid, :aid, :oid, 'admin')"
            ),
            {"mid": str(ids["human_membership"]), "aid": str(ids["human"]), "oid": str(ids["org"])},
        )
        await conn.execute(
            text(
                "INSERT INTO org_memberships (id, account_id, organisation_id, role) "
                "VALUES (:mid, :aid, :oid, 'operator')"
            ),
            {"mid": str(ids["service_membership"]), "aid": str(ids["service"]), "oid": str(ids["org"])},
        )
    return ids


def _pgcode(exc: DBAPIError) -> str:
    """SQLSTATE from either driver spelling (psycopg ``pgcode`` / asyncpg ``sqlstate``)."""
    orig = getattr(exc, "orig", None)
    return str(getattr(orig, "pgcode", None) or getattr(orig, "sqlstate", "") or "")


async def _expect_check_violation(engine: AsyncEngine, sql: str, params: dict[str, str]) -> DBAPIError:
    """Execute ``sql`` expecting a DBAPIError; return it for the caller's assert."""
    async with engine.connect() as conn:
        with pytest.raises(DBAPIError) as exc_info:
            await conn.execute(text(sql), params)
        await conn.rollback()
    return exc_info.value


class TestServiceRoleCeilingTrigger:
    async def test_insert_admin_for_service_is_refused(self, upgraded_db_url: str) -> None:
        engine = create_async_engine(upgraded_db_url, poolclass=NullPool)
        org_id = uuid.uuid4()
        service_id = uuid.uuid4()
        try:
            async with engine.begin() as conn:
                await conn.execute(
                    text(
                        "INSERT INTO organisations (id, name, slug, settings_json) VALUES (:id, 'o', 'o', '{}'::json)"
                    ),
                    {"id": str(org_id)},
                )
                await conn.execute(
                    text(
                        "INSERT INTO accounts (id, email, display_name, password_hash, auth_provider, active, kind) "
                        "VALUES (:id, NULL, 'svc', NULL, NULL, true, 'service')"
                    ),
                    {"id": str(service_id)},
                )
            err = await _expect_check_violation(
                engine,
                "INSERT INTO org_memberships (id, account_id, organisation_id, role) "
                "VALUES (:mid, :aid, :oid, 'admin')",
                {"mid": str(uuid.uuid4()), "aid": str(service_id), "oid": str(org_id)},
            )
            assert _pgcode(err) == "23514"
        finally:
            await engine.dispose()

    async def test_update_to_admin_for_service_is_refused(self, upgraded_db_url: str) -> None:
        engine = create_async_engine(upgraded_db_url, poolclass=NullPool)
        try:
            ids = await _seed_one_service(engine)
            err = await _expect_check_violation(
                engine,
                "UPDATE org_memberships SET role = 'admin' WHERE account_id = :aid",
                {"aid": str(ids["service"])},
            )
            assert _pgcode(err) == "23514"
        finally:
            await engine.dispose()

    async def test_operator_grant_for_service_still_passes(self, upgraded_db_url: str) -> None:
        engine = create_async_engine(upgraded_db_url, poolclass=NullPool)
        try:
            ids = await _seed_one_service(engine)
            async with engine.begin() as conn:
                result = await conn.execute(
                    text("UPDATE org_memberships SET role = 'runner' WHERE account_id = :aid RETURNING role"),
                    {"aid": str(ids["service"])},
                )
                assert result.scalar_one() == "runner"
        finally:
            await engine.dispose()

    async def test_admin_grant_for_human_still_passes(self, upgraded_db_url: str) -> None:
        engine = create_async_engine(upgraded_db_url, poolclass=NullPool)
        try:
            ids = await _seed_one_human(engine)
            async with engine.begin() as conn:
                result = await conn.execute(
                    text("UPDATE org_memberships SET role = 'admin' WHERE account_id = :aid RETURNING role"),
                    {"aid": str(ids["human"])},
                )
                assert result.scalar_one() == "admin"
        finally:
            await engine.dispose()


async def _seed_one_service(engine: AsyncEngine) -> dict[str, uuid.UUID]:
    """A private org + service account + operator membership (isolated per test)."""
    ids = {"org": uuid.uuid4(), "service": uuid.uuid4()}
    async with engine.begin() as conn:
        await conn.execute(
            text(
                "INSERT INTO organisations (id, name, slug, settings_json) VALUES (:id, 'svc-org', 'svc', '{}'::json)"
            ),
            {"id": str(ids["org"])},
        )
        await conn.execute(
            text(
                "INSERT INTO accounts (id, email, display_name, password_hash, auth_provider, active, kind) "
                "VALUES (:id, NULL, 'svc', NULL, NULL, true, 'service')"
            ),
            {"id": str(ids["service"])},
        )
        await conn.execute(
            text(
                "INSERT INTO org_memberships (id, account_id, organisation_id, role) "
                "VALUES (:mid, :aid, :oid, 'operator')"
            ),
            {"mid": str(uuid.uuid4()), "aid": str(ids["service"]), "oid": str(ids["org"])},
        )
    return ids


async def _seed_one_human(engine: AsyncEngine) -> dict[str, uuid.UUID]:
    ids = {"org": uuid.uuid4(), "human": uuid.uuid4()}
    async with engine.begin() as conn:
        await conn.execute(
            text(
                "INSERT INTO organisations (id, name, slug, settings_json) VALUES (:id, 'human-org', 'hum', '{}'::json)"
            ),
            {"id": str(ids["org"])},
        )
        await conn.execute(
            text(
                "INSERT INTO accounts (id, email, display_name, password_hash, auth_provider, active) "
                "VALUES (:id, 'hum@example.com', 'hum', 'hash', 'local', true)"
            ),
            {"id": str(ids["human"])},
        )
        await conn.execute(
            text(
                "INSERT INTO org_memberships (id, account_id, organisation_id, role) "
                "VALUES (:mid, :aid, :oid, 'viewer')"
            ),
            {"mid": str(uuid.uuid4()), "aid": str(ids["human"]), "oid": str(ids["org"])},
        )
    return ids


class TestDecommissionedCheck:
    async def test_decommissioned_human_row_is_refused(self, upgraded_db_url: str) -> None:
        engine = create_async_engine(upgraded_db_url, poolclass=NullPool)
        try:
            err = await _expect_check_violation(
                engine,
                "UPDATE accounts SET decommissioned_at = now() WHERE email = 'm0311-human@example.com'",
                {},
            )
            assert _pgcode(err) == "23514"
        finally:
            await engine.dispose()

    async def test_decommissioned_service_row_is_accepted(self, upgraded_db_url: str) -> None:
        engine = create_async_engine(upgraded_db_url, poolclass=NullPool)
        try:
            async with engine.begin() as conn:
                result = await conn.execute(
                    text(
                        "UPDATE accounts SET decommissioned_at = now() "
                        "WHERE display_name = 'm0311-svc' RETURNING decommissioned_at IS NOT NULL"
                    )
                )
                assert result.scalar_one() is True
        finally:
            await engine.dispose()

    async def test_column_and_trigger_exist_after_upgrade(self, upgraded_db_url: str) -> None:
        engine = create_async_engine(upgraded_db_url, poolclass=NullPool)
        try:
            async with engine.connect() as conn:
                col = (
                    await conn.execute(
                        text(
                            "SELECT COUNT(*) FROM information_schema.columns "
                            "WHERE table_name = 'accounts' AND column_name = 'decommissioned_at'"
                        )
                    )
                ).scalar_one()
                assert col == 1
                trigger = (
                    await conn.execute(
                        text(
                            "SELECT COUNT(*) FROM pg_trigger WHERE tgname = 'trg_org_memberships_service_role_ceiling'"
                        )
                    )
                ).scalar_one()
                assert trigger == 1
        finally:
            await engine.dispose()

    async def test_downgrade_drops_column_and_trigger(self, upgraded_db_url: str) -> None:
        _downgrade(upgraded_db_url, PREV_REV)
        engine = create_async_engine(upgraded_db_url, poolclass=NullPool)
        try:
            async with engine.connect() as conn:
                col = (
                    await conn.execute(
                        text(
                            "SELECT COUNT(*) FROM information_schema.columns "
                            "WHERE table_name = 'accounts' AND column_name = 'decommissioned_at'"
                        )
                    )
                ).scalar_one()
                assert col == 0
                trigger = (
                    await conn.execute(
                        text(
                            "SELECT COUNT(*) FROM pg_trigger WHERE tgname = 'trg_org_memberships_service_role_ceiling'"
                        )
                    )
                ).scalar_one()
                assert trigger == 0
        finally:
            await engine.dispose()
