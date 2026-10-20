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
  ``organisation_cost_state``, which is the AUTHORITATIVE store after FAR-1833,
  so the test MUTATES the cost-state values between upgrade and downgrade and
  asserts the MUTATED values come back. A downgrade that merely re-added empty
  columns, or that copied the pre-drop ``organisations`` values, fails that
  assertion. A value an old-code instance wrote only to ``organisations`` is
  deliberately superseded by the cost-state value — that precedence is the
  cutover's intent, not data loss.
* an organisation with NO cost-state row (legitimate: created after 0321 that
  never set a ceiling and never accrued spend) is BACKFILLED by the upgrade from
  its current ``organisations`` values BEFORE the drop, so the downgrade
  restores those values rather than defaults. That is the one class the drop
  must not lose; asserting it is what makes the drop lossless.
* the SQLite downgrade restore and the pre-drop backfill are EXECUTED against a
  throwaway sqlite3 database (stdlib) holding one organisation WITH a cost-state
  row and one WITHOUT: the former is restored from the cost-state value, the
  latter keeps the column default, and neither statement aborts. Alembic batch
  mode wraps that statement and cannot be driven here, so this pins the branch
  that the Postgres tests cannot reach; a further assertion pins the shared
  predicate constant referenced by both dialect branches and the backfill, so
  the untested branch cannot silently drift again.
"""

from __future__ import annotations

import importlib.util
import sqlite3
import uuid
from collections.abc import AsyncGenerator
from pathlib import Path
from types import ModuleType
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

# An org with no cost-state row at all at 0323: the upgrade's pre-drop backfill
# must capture these values, and the downgrade must bring them back.
_PLAIN_SLUG = "m0324-no-state"
_PLAIN_ORG_VALUES = (999, 888, 777)

_MIGRATION_PATH = (
    Path(BACKEND_ROOT) / "src" / "modulo" / "db" / "migrations" / "versions" / "0324_drop_organisations_cost_columns.py"
)


def _load_migration() -> ModuleType:
    """Import the revision module by path (it is not a package module)."""
    spec = importlib.util.spec_from_file_location("migration_0324_drop_org_cost_columns", _MIGRATION_PATH)
    assert spec is not None, f"cannot build an import spec for {_MIGRATION_PATH}"
    assert spec.loader is not None, f"no loader for {_MIGRATION_PATH}"
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


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

    async def test_org_without_cost_state_row_is_backfilled_before_the_drop(self, upgraded_db_url: str) -> None:
        """The pre-drop backfill captures the only surviving copy of its values."""
        engine = create_async_engine(upgraded_db_url, poolclass=NullPool)
        try:
            assert await _state_values(engine, _PLAIN_SLUG) == _PLAIN_ORG_VALUES
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

    async def test_org_without_cost_state_row_gets_its_pre_drop_values_back(self, upgraded_db_url: str) -> None:
        """A rewind is lossless for the only class that had no cost-state row.

        The org was seeded at 0323 with values in ``organisations`` and no
        cost-state row; the upgrade's backfill captured them before the drop,
        so the downgrade restores them. Asserting the design's actual guarantee
        (losslessness), not the ``(None, None, 0)`` defaults that a lossy drop
        would have left behind.
        """
        _downgrade(upgraded_db_url, PREV_REV)
        engine = create_async_engine(upgraded_db_url, poolclass=NullPool)
        try:
            assert await _org_values(engine, _PLAIN_SLUG) == _PLAIN_ORG_VALUES
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


class TestSqliteRestoreStatements:
    """Execute the SQLite branch's SQL against a throwaway sqlite3 database.

    Alembic batch mode wraps ``_downgrade_sqlite`` and cannot be driven in this
    harness (there is no SQLite migration chain), so the statements themselves
    are executed directly with the stdlib ``sqlite3`` module. Both organisations
    classes are present in every scenario: one WITH a cost-state row and one
    WITHOUT.
    """

    def _seeded(self, tmp_path: Path) -> sqlite3.Connection:
        conn = sqlite3.connect(tmp_path / "m0324.sqlite3")
        conn.executescript(
            "CREATE TABLE organisations ("
            " id TEXT PRIMARY KEY,"
            " max_run_cost_cents INTEGER,"
            " spend_ceiling_cents INTEGER,"
            " org_cumulative_spend_cents INTEGER NOT NULL DEFAULT 0"
            ");"
            "CREATE TABLE organisation_cost_state ("
            " org_id TEXT PRIMARY KEY,"
            " max_run_cost_cents INTEGER,"
            " spend_ceiling_cents INTEGER,"
            " org_cumulative_spend_cents INTEGER NOT NULL DEFAULT 0"
            ");"
            "INSERT INTO organisations (id, max_run_cost_cents, spend_ceiling_cents,"
            " org_cumulative_spend_cents) VALUES ('with-state', 1, 2, 3);"
            "INSERT INTO organisations (id, max_run_cost_cents, spend_ceiling_cents,"
            " org_cumulative_spend_cents) VALUES ('no-state', 7, 8, 9);"
            "INSERT INTO organisation_cost_state (org_id, max_run_cost_cents,"
            " spend_ceiling_cents, org_cumulative_spend_cents) VALUES ('with-state', 10, 20, 30);"
        )
        return conn

    @staticmethod
    def _org_rows(conn: sqlite3.Connection) -> dict[str, tuple[object, object, object]]:
        return {
            str(row[0]): (row[1], row[2], row[3])
            for row in conn.execute(
                "SELECT id, max_run_cost_cents, spend_ceiling_cents, org_cumulative_spend_cents FROM organisations"
            )
        }

    @staticmethod
    def _state_rows(conn: sqlite3.Connection) -> dict[str, tuple[object, object, object]]:
        return {
            str(row[0]): (row[1], row[2], row[3])
            for row in conn.execute(
                "SELECT org_id, max_run_cost_cents, spend_ceiling_cents, org_cumulative_spend_cents "
                "FROM organisation_cost_state"
            )
        }

    def test_restore_does_not_abort_on_an_organisation_without_a_state_row(self, tmp_path: Path) -> None:
        """The pre-fix statement raised ``IntegrityError`` (NOT NULL) here.

        The scalar subquery yielded NULL for ``no-state`` and the NOT NULL
        cumulative column rejected the entire UPDATE, so the downgrade aborted
        for the whole database. The EXISTS-guarded statement restores the
        matching row and leaves the other on the column default.
        """
        module = _load_migration()
        conn = self._seeded(tmp_path)
        try:
            # The downgrade's freshly re-added column starts at the default.
            conn.execute(
                "UPDATE organisations SET max_run_cost_cents = NULL, spend_ceiling_cents = NULL, "
                "org_cumulative_spend_cents = 0"
            )
            conn.execute(module._RESTORE_SQLITE_SQL)
            rows = self._org_rows(conn)
        finally:
            conn.close()
        assert rows == {"with-state": (10, 20, 30), "no-state": (None, None, 0)}

    def test_backfill_then_restore_is_lossless_for_an_organisation_without_a_state_row(self, tmp_path: Path) -> None:
        module = _load_migration()
        conn = self._seeded(tmp_path)
        try:
            conn.execute(module._BACKFILL_MISSING_SQL)
            state = self._state_rows(conn)
            assert state == {"with-state": (10, 20, 30), "no-state": (7, 8, 9)}, (
                "the pre-drop backfill must capture the no-row organisation's values and leave the existing row alone"
            )
            # The drop replaces the table from the model shape: the columns come
            # back at their defaults, and only organisation_cost_state remains.
            conn.execute(
                "UPDATE organisations SET max_run_cost_cents = NULL, spend_ceiling_cents = NULL, "
                "org_cumulative_spend_cents = 0"
            )
            conn.execute(module._RESTORE_SQLITE_SQL)
            rows = self._org_rows(conn)
        finally:
            conn.close()
        assert rows == {"with-state": (10, 20, 30), "no-state": (7, 8, 9)}


class TestSharedRestorePredicate:
    """Pin the ONE predicate both dialect branches (and the backfill) use."""

    def test_all_restore_statements_reference_the_same_predicate_constant(self) -> None:
        module = _load_migration()
        predicate = module._COST_STATE_MATCH_PREDICATE
        assert predicate == "s.org_id = o.id"
        assert predicate in module._RESTORE_SQL
        assert predicate in module._RESTORE_SQLITE_SQL
        assert predicate in module._BACKFILL_MISSING_SQL
