"""Integration test for migration 0321_organisation_cost_state (FAR-1833).

Against REAL Postgres (testcontainers, private database per test — the
``test_migration_0315`` harness):

* seed TWO organisations at ``0320_evidence_redaction_rules`` (the deployed
  shape, before this table exists): one carrying a NON-ZERO
  ``org_cumulative_spend_cents`` and non-NULL ``max_run_cost_cents`` /
  ``spend_ceiling_cents``, and one on the column defaults;
* the REAL alembic ``upgrade`` of ``0321_organisation_cost_state`` backfills
  one ``organisation_cost_state`` row per organisation, and the seeded values
  are copied EXACTLY (this is what makes the test non-vacuous — a backfill of
  zeros would fail it);
* the migration is purely ADDITIVE (expand half of an expand/contract cutover):
  the three ``organisations`` columns and the ``ck_organisations_cum_spend``
  CHECK are deliberately LEFT IN PLACE and still hold their original values —
  dropping them in the same release that stops reading them would break
  old-code instances during the rolling-deploy window;
* the ``ck_organisation_cost_state_cum_spend`` CHECK refuses a negative
  cumulative total;
* a downgrade drops the new table and its RLS policy, and leaves the three
  ``organisations`` columns untouched.
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

MIGRATION_REV = "0321_organisation_cost_state"
PREV_REV = "0320_evidence_redaction_rules"

_SEEDED_SLUG = "m0317-seeded"
_PLAIN_SLUG = "m0317-plain"
_SEEDED_MAX_RUN = 1500
_SEEDED_CEILING = 250000
_SEEDED_CUMULATIVE = 4242


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
    """Private Postgres migrated to 0321 with two organisations seeded at 0320."""
    db_name, iso_url = await _fresh_db(db_url, "m0317")
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


async def _seed(engine: AsyncEngine) -> None:
    """Seed at PREV_REV: one org with non-default cost values, one on defaults."""
    async with engine.begin() as conn:
        await conn.execute(
            text(
                "INSERT INTO organisations "
                "(id, name, slug, settings_json, max_run_cost_cents, spend_ceiling_cents, "
                " org_cumulative_spend_cents) "
                "VALUES (:id, :name, :slug, '{}'::json, :max_run, :ceiling, :cumulative)"
            ),
            {
                "id": str(uuid.uuid4()),
                "name": "m0317-seeded",
                "slug": _SEEDED_SLUG,
                "max_run": _SEEDED_MAX_RUN,
                "ceiling": _SEEDED_CEILING,
                "cumulative": _SEEDED_CUMULATIVE,
            },
        )
        await conn.execute(
            text("INSERT INTO organisations (id, name, slug, settings_json) VALUES (:id, :name, :slug, '{}'::json)"),
            {"id": str(uuid.uuid4()), "name": "m0317-plain", "slug": _PLAIN_SLUG},
        )


class TestBackfill:
    async def test_backfill_copies_nonzero_values_exactly(self, upgraded_db_url: str) -> None:
        engine = create_async_engine(upgraded_db_url, poolclass=NullPool)
        try:
            async with engine.connect() as conn:
                row = (
                    await conn.execute(
                        text(
                            "SELECT s.max_run_cost_cents, s.spend_ceiling_cents, "
                            "s.org_cumulative_spend_cents "
                            "FROM organisation_cost_state s "
                            "JOIN organisations o ON o.id = s.org_id "
                            "WHERE o.slug = :slug"
                        ),
                        {"slug": _SEEDED_SLUG},
                    )
                ).one()
            assert tuple(row) == (_SEEDED_MAX_RUN, _SEEDED_CEILING, _SEEDED_CUMULATIVE)
        finally:
            await engine.dispose()

    async def test_backfill_gives_every_org_a_row(self, upgraded_db_url: str) -> None:
        engine = create_async_engine(upgraded_db_url, poolclass=NullPool)
        try:
            async with engine.connect() as conn:
                rows = (
                    await conn.execute(
                        text(
                            "SELECT o.slug, s.max_run_cost_cents, s.spend_ceiling_cents, "
                            "s.org_cumulative_spend_cents "
                            "FROM organisations o "
                            "LEFT JOIN organisation_cost_state s ON s.org_id = o.id"
                        )
                    )
                ).all()
            by_slug = {row[0]: tuple(row[1:]) for row in rows}
            assert by_slug[_SEEDED_SLUG] == (_SEEDED_MAX_RUN, _SEEDED_CEILING, _SEEDED_CUMULATIVE)
            assert by_slug[_PLAIN_SLUG] == (None, None, 0)
        finally:
            await engine.dispose()

    async def test_organisations_columns_are_left_in_place(self, upgraded_db_url: str) -> None:
        """The expand half: the three source columns survive, values intact."""
        engine = create_async_engine(upgraded_db_url, poolclass=NullPool)
        try:
            async with engine.connect() as conn:
                present = (
                    await conn.execute(
                        text(
                            "SELECT column_name FROM information_schema.columns "
                            "WHERE table_name = 'organisations' "
                            "AND column_name IN ('max_run_cost_cents', 'spend_ceiling_cents', "
                            "'org_cumulative_spend_cents') "
                            "ORDER BY column_name"
                        )
                    )
                ).scalars()
                assert sorted(present) == [
                    "max_run_cost_cents",
                    "org_cumulative_spend_cents",
                    "spend_ceiling_cents",
                ]
                # Additive means the ORIGINAL values are still there — the
                # upgrade must not rewrite or null them out.
                row = (
                    await conn.execute(
                        text(
                            "SELECT max_run_cost_cents, spend_ceiling_cents, "
                            "org_cumulative_spend_cents FROM organisations WHERE slug = :slug"
                        ),
                        {"slug": _SEEDED_SLUG},
                    )
                ).one()
            assert tuple(row) == (_SEEDED_MAX_RUN, _SEEDED_CEILING, _SEEDED_CUMULATIVE)
            # The CHECK that references the cumulative column was not dropped.
            async with engine.connect() as conn:
                constraint = (
                    await conn.execute(
                        text("SELECT conname FROM pg_constraint WHERE conname = 'ck_organisations_cum_spend'")
                    )
                ).scalar_one()
            assert constraint == "ck_organisations_cum_spend"
        finally:
            await engine.dispose()


class TestCheckConstraint:
    async def test_negative_cumulative_is_refused(self, upgraded_db_url: str) -> None:
        engine = create_async_engine(upgraded_db_url, poolclass=NullPool)
        org_id = uuid.uuid4()
        try:
            async with engine.begin() as conn:
                await conn.execute(
                    text(
                        "INSERT INTO organisations (id, name, slug, settings_json) "
                        "VALUES (:id, :name, :slug, '{}'::json)"
                    ),
                    {"id": str(org_id), "name": "m0317-neg", "slug": "m0317-neg"},
                )
            with pytest.raises(DBAPIError):
                async with engine.begin() as conn:
                    await conn.execute(
                        text(
                            "INSERT INTO organisation_cost_state (org_id, org_cumulative_spend_cents) VALUES (:oid, -1)"
                        ),
                        {"oid": str(org_id)},
                    )
        finally:
            await engine.dispose()


class TestDowngrade:
    async def test_downgrade_drops_the_table_and_leaves_org_columns_untouched(self, upgraded_db_url: str) -> None:
        _downgrade(upgraded_db_url, PREV_REV)
        engine = create_async_engine(upgraded_db_url, poolclass=NullPool)
        try:
            async with engine.connect() as conn:
                present = (
                    await conn.execute(text("SELECT to_regclass('public.organisation_cost_state')"))
                ).scalar_one()
                assert present is None
                # The RLS policy is gone with the table (never left dangling).
                policy_count = (
                    await conn.execute(
                        text(
                            "SELECT count(*) FROM pg_policies "
                            "WHERE tablename = 'organisation_cost_state' "
                            "AND policyname = 'rls_org_isolation'"
                        )
                    )
                ).scalar_one()
                assert policy_count == 0
                # Additive means DOWNGRADE never touched the org columns either:
                # they still exist and still hold the ORIGINAL seeded values.
                columns = (
                    await conn.execute(
                        text(
                            "SELECT column_name FROM information_schema.columns "
                            "WHERE table_name = 'organisations' "
                            "AND column_name IN ('max_run_cost_cents', 'spend_ceiling_cents', "
                            "'org_cumulative_spend_cents') "
                            "ORDER BY column_name"
                        )
                    )
                ).scalars()
                assert sorted(columns) == [
                    "max_run_cost_cents",
                    "org_cumulative_spend_cents",
                    "spend_ceiling_cents",
                ]
                row = (
                    await conn.execute(
                        text(
                            "SELECT max_run_cost_cents, spend_ceiling_cents, "
                            "org_cumulative_spend_cents FROM organisations WHERE slug = :slug"
                        ),
                        {"slug": _SEEDED_SLUG},
                    )
                ).one()
                assert tuple(row) == (_SEEDED_MAX_RUN, _SEEDED_CEILING, _SEEDED_CUMULATIVE)
                constraint = (
                    await conn.execute(
                        text("SELECT conname FROM pg_constraint WHERE conname = 'ck_organisations_cum_spend'")
                    )
                ).scalar_one()
                assert constraint == "ck_organisations_cum_spend"
        finally:
            await engine.dispose()
