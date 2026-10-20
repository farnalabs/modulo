"""Integration test for migration 0324_drop_organisations_cost_columns (FAR-1902).

Against REAL Postgres (testcontainers, private database per test — the
``test_migration_0321`` harness):

* seed organisations at ``0323_pipeline_snapshots_substantive_version_index``
  (the shape this migration runs against, where BOTH the three
  ``organisations`` cost columns and ``organisation_cost_state`` exist) with
  values in the two stores that are DELIBERATELY DIFFERENT, so every assertion
  can tell which store a restored value came from;
* the REAL alembic ``upgrade`` of ``0324_drop_organisations_cost_columns``
  removes the three columns and the ``ck_organisations_cum_spend`` CHECK from
  ``organisations``, and leaves ``organisation_cost_state`` (the surviving home
  of those values) intact;
* the downgrade re-adds the columns AND restores the values from
  ``organisation_cost_state`` — the exact inverse of 0321's backfill — so the
  test MUTATES the cost-state values between upgrade and downgrade and asserts
  the MUTATED values come back. A downgrade that merely re-added empty columns,
  or that copied the pre-drop ``organisations`` values, fails that assertion.
* an organisation with NO cost-state row (legitimate: created after 0321 that
  never set a ceiling and never accrued spend) keeps the column defaults
  (NULL ceilings, ``0`` consumed) after the downgrade.
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
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine, create_async_engine
from sqlalchemy.pool import NullPool

pytestmark = [pytest.mark.integration]

BACKEND_ROOT = str(Path(__file__).resolve().parents[2])

MIGRATION_REV = "0324_drop_organisations_cost_columns"
PREV_REV = "0323_pipeline_snapshots_substantive_version_index"

_COST_COLUMNS = ("max_run_cost_cents", "spend_ceiling_cents", "org_cumulative_spend_cents")
_CHECK_NAME = "ck_organisations_cum_spend"

# The two stores must disagree, or "restored from organisation_cost_state" and
# "the old organisations value survived" are indistinguishable.
_RESTORED_SLUG = "m0324-restored"
_RESTORED_ORG_VALUES = (111, 222000, 3333)
_RESTORED_STATE_VALUES = (1500, 250000, 4242)

# Mutated AFTER the columns are dropped — the post-cutover state a real
# deployment accrues while only organisation_cost_state is being written.
_MUTATED_SLUG = "m0324-mutated"
_SEEDED_STATE_VALUES = (10, 20, 30)
_MUTATED_STATE_VALUES = (4000, 900000, 987654)

# An org with no cost-state row at all: the LEFT-JOIN semantic.
_PLAIN_SLUG = "m0324-no-state"
_PLAIN_ORG_VALUES = (999, 888, 777)


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


async def _insert_org(conn: AsyncConnection, slug: str, values: tuple[int, int, int]) -> str:
    org_id = str(uuid.uuid4())
    await conn.execute(
        text(
            "INSERT INTO organisations "
            "(id, name, slug, settings_json, max_run_cost_cents, spend_ceiling_cents, "
            " org_cumulative_spend_cents) "
            "VALUES (:id, :name, :slug, CAST('{}' AS jsonb), :max_run, :ceiling, :cumulative)"
        ),
        {
            "id": org_id,
            "name": slug,
            "slug": slug,
            "max_run": values[0],
            "ceiling": values[1],
            "cumulative": values[2],
        },
    )
    return org_id


async def _seed(engine: AsyncEngine) -> None:
    """Seed three organisations at PREV_REV, plus cost-state rows for two of them."""
    async with engine.begin() as conn:
        restored_id = await _insert_org(conn, _RESTORED_SLUG, _RESTORED_ORG_VALUES)
        mutated_id = await _insert_org(conn, _MUTATED_SLUG, (1, 2, 3))
        await _insert_org(conn, _PLAIN_SLUG, _PLAIN_ORG_VALUES)
        for org_id, state_values in (
            (restored_id, _RESTORED_STATE_VALUES),
            (mutated_id, _SEEDED_STATE_VALUES),
        ):
            await conn.execute(
                text(
                    "INSERT INTO organisation_cost_state "
                    "(org_id, max_run_cost_cents, spend_ceiling_cents, org_cumulative_spend_cents) "
                    "VALUES (:oid, :max_run, :ceiling, :cumulative)"
                ),
                {
                    "oid": org_id,
                    "max_run": state_values[0],
                    "ceiling": state_values[1],
                    "cumulative": state_values[2],
                },
            )


@pytest_asyncio.fixture
async def upgraded_db_url(db_url: str) -> AsyncGenerator[str, None]:
    """Private Postgres migrated to 0324 with three organisations seeded at 0323."""
    db_name, iso_url = await _fresh_db(db_url, "m0324")
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


async def _column_names(engine: AsyncEngine) -> list[str]:
    async with engine.connect() as conn:
        return list(
            (
                await conn.execute(
                    text(
                        "SELECT column_name FROM information_schema.columns "
                        "WHERE table_schema = 'public' AND table_name = 'organisations'"
                    )
                )
            ).scalars()
        )


async def _org_values(engine: AsyncEngine, slug: str) -> tuple[object, object, object]:
    async with engine.connect() as conn:
        row = (
            await conn.execute(
                text(
                    "SELECT max_run_cost_cents, spend_ceiling_cents, org_cumulative_spend_cents "
                    "FROM organisations WHERE slug = :slug"
                ),
                {"slug": slug},
            )
        ).one()
    return (row[0], row[1], row[2])


async def _state_values(engine: AsyncEngine, slug: str) -> tuple[object, object, object]:
    async with engine.connect() as conn:
        row = (
            await conn.execute(
                text(
                    "SELECT s.max_run_cost_cents, s.spend_ceiling_cents, s.org_cumulative_spend_cents "
                    "FROM organisation_cost_state s JOIN organisations o ON o.id = s.org_id "
                    "WHERE o.slug = :slug"
                ),
                {"slug": slug},
            )
        ).one()
    return (row[0], row[1], row[2])


class TestUpgrade:
    async def test_columns_are_dropped(self, upgraded_db_url: str) -> None:
        engine = create_async_engine(upgraded_db_url, poolclass=NullPool)
        try:
            present = set(await _column_names(engine))
        finally:
            await engine.dispose()
        assert present.isdisjoint(_COST_COLUMNS), (
            f"retired cost columns still present: {sorted(present & set(_COST_COLUMNS))}"
        )

    async def test_check_constraint_is_dropped(self, upgraded_db_url: str) -> None:
        engine = create_async_engine(upgraded_db_url, poolclass=NullPool)
        try:
            async with engine.connect() as conn:
                found = (
                    await conn.execute(
                        text(
                            "SELECT count(*) FROM pg_constraint "
                            "WHERE conname = :name AND conrelid = 'public.organisations'::regclass"
                        ),
                        {"name": _CHECK_NAME},
                    )
                ).scalar_one()
        finally:
            await engine.dispose()
        assert found == 0

    async def test_cost_state_rows_survive_the_drop(self, upgraded_db_url: str) -> None:
        engine = create_async_engine(upgraded_db_url, poolclass=NullPool)
        try:
            assert await _state_values(engine, _RESTORED_SLUG) == _RESTORED_STATE_VALUES
            assert await _state_values(engine, _MUTATED_SLUG) == _SEEDED_STATE_VALUES
        finally:
            await engine.dispose()


class TestDowngrade:
    async def test_columns_are_restored_from_organisation_cost_state(self, upgraded_db_url: str) -> None:
        engine = create_async_engine(upgraded_db_url, poolclass=NullPool)
        try:
            # Post-cutover writes land ONLY in organisation_cost_state; the
            # downgrade must bring THESE values back, not the pre-drop ones.
            async with engine.begin() as conn:
                await conn.execute(
                    text(
                        "UPDATE organisation_cost_state SET "
                        "max_run_cost_cents = :max_run, spend_ceiling_cents = :ceiling, "
                        "org_cumulative_spend_cents = :cumulative "
                        "WHERE org_id = (SELECT id FROM organisations WHERE slug = :slug)"
                    ),
                    {
                        "max_run": _MUTATED_STATE_VALUES[0],
                        "ceiling": _MUTATED_STATE_VALUES[1],
                        "cumulative": _MUTATED_STATE_VALUES[2],
                        "slug": _MUTATED_SLUG,
                    },
                )
        finally:
            await engine.dispose()

        _downgrade(upgraded_db_url, PREV_REV)

        engine = create_async_engine(upgraded_db_url, poolclass=NullPool)
        try:
            present = set(await _column_names(engine))
            assert present.issuperset(_COST_COLUMNS)
            assert await _org_values(engine, _RESTORED_SLUG) == _RESTORED_STATE_VALUES
            assert await _org_values(engine, _MUTATED_SLUG) == _MUTATED_STATE_VALUES
        finally:
            await engine.dispose()

    async def test_org_without_cost_state_row_keeps_column_defaults(self, upgraded_db_url: str) -> None:
        _downgrade(upgraded_db_url, PREV_REV)
        engine = create_async_engine(upgraded_db_url, poolclass=NullPool)
        try:
            assert await _org_values(engine, _PLAIN_SLUG) == (None, None, 0)
        finally:
            await engine.dispose()

    async def test_check_constraint_is_restored_and_validated(self, upgraded_db_url: str) -> None:
        _downgrade(upgraded_db_url, PREV_REV)
        engine = create_async_engine(upgraded_db_url, poolclass=NullPool)
        try:
            async with engine.connect() as conn:
                row = (
                    await conn.execute(
                        text(
                            "SELECT convalidated FROM pg_constraint "
                            "WHERE conname = :name AND conrelid = 'public.organisations'::regclass"
                        ),
                        {"name": _CHECK_NAME},
                    )
                ).one_or_none()
            assert row is not None, "downgrade did not restore the cumulative-spend CHECK"
            assert row[0] is True, "restored CHECK must be VALIDATE-d, not left NOT VALID"
            with pytest.raises(DBAPIError):
                async with engine.begin() as conn:
                    await conn.execute(
                        text("UPDATE organisations SET org_cumulative_spend_cents = -1 WHERE slug = :slug"),
                        {"slug": _RESTORED_SLUG},
                    )
        finally:
            await engine.dispose()
