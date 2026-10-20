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
* the ROLE CEREMONY is covered in ``TestGuardedRoleCeremony``: the guarded
  ``SET LOCAL ROLE modulo_migrate`` wrapper is exercised against real Postgres
  (it activates the role, verifies it, and restores it), its no-op path (no
  ``modulo_migrate``: a fresh dev/BDD database) is exercised against real
  Postgres too, and ``upgrade``/``downgrade`` are driven over a recording
  connection to prove the cross-org DML is actually issued INSIDE the block and
  the downgrade's transient UPDATE grant is revoked after it.

    NOT exercised here: an end-to-end alembic run connected as a
    non-superuser, non-BYPASSRLS admin. The testcontainer's connection role is
    the cluster superuser — which bypasses RLS regardless of the ceremony — and
    every private database is created from it, so the policy can never refuse
    the DML in this harness. The RLS behaviour itself (unguarded INSERT raises
    "new row violates row-level security policy"; unguarded ``UPDATE ... FROM``
    silently affects 0 rows; the guarded form succeeds) was demonstrated
    out-of-band against PostgreSQL 16 with a NONSUPERUSER NOBYPASSRLS database
    owner, and is recorded on the delivery ticket.
"""

from __future__ import annotations

import importlib.util
import sqlite3
import uuid
from collections.abc import AsyncGenerator, Iterator
from contextlib import contextmanager
from pathlib import Path
from types import ModuleType, SimpleNamespace
from unittest.mock import patch
from urllib.parse import urlparse, urlunparse

import pytest
import pytest_asyncio
from alembic import command
from alembic.config import Config
from sqlalchemy import text
from sqlalchemy.engine import Connection
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


class _RecordingResult:
    """Minimal stand-in for a SQLAlchemy ``Result`` (scalars + rowcount)."""

    def __init__(
        self,
        *,
        scalar_one: object = None,
        scalar_one_or_none: object = None,
        rowcount: int = 1,
    ) -> None:
        self._scalar_one = scalar_one
        self._scalar_one_or_none = scalar_one_or_none
        self.rowcount = rowcount

    def scalar_one(self) -> object:
        return self._scalar_one

    def scalar_one_or_none(self) -> object:
        return self._scalar_one_or_none


class _RecordingBind:
    """Fake ``op.get_bind()`` that records every statement the migration issues.

    ``role_exists`` models both database shapes: ``True`` is a deployed database
    (``modulo_migrate`` provisioned by bootstrap_role before alembic runs),
    ``False`` a fresh dev/BDD database where the ceremony must no-op.
    """

    def __init__(self, *, role_exists: bool = True) -> None:
        self.dialect = SimpleNamespace(name="postgresql")
        self.sql: list[str] = []
        self._role_exists = role_exists

    def execute(self, statement: object, *_args: object, **_kwargs: object) -> _RecordingResult:
        sql = str(statement)
        self.sql.append(sql)
        if "pg_roles" in sql:
            return _RecordingResult(scalar_one_or_none=1 if self._role_exists else None)
        if "current_user" in sql:
            return _RecordingResult(scalar_one="modulo_migrate")
        if "to_regclass" in sql:
            return _RecordingResult(scalar_one_or_none="public.organisation_cost_state")
        return _RecordingResult()

    @contextmanager
    def begin_nested(self) -> Iterator[None]:
        yield


class _RecordingOperations:
    """Fake ``alembic.op`` for the Postgres branch (``get_bind`` only)."""

    def __init__(self, bind: _RecordingBind) -> None:
        self._bind = bind

    def get_bind(self) -> _RecordingBind:
        return self._bind


def _index_of(statements: list[str], needle: str) -> int:
    for index, sql in enumerate(statements):
        if needle in sql:
            return index
    raise AssertionError(f"{needle!r} was never issued; statements were: {statements}")


class TestGuardedRoleCeremony:
    """The cross-org DML runs under ``modulo_migrate``, the table's owning role.

    ``organisation_cost_state`` is ENABLE + FORCE RLS and the migration
    connection may be a non-superuser admin (``DATABASE_ADMIN_URL`` is only
    documented as "superuser or at minimum CREATEROLE + CREATEDB"), which cannot
    write it and — worse — cannot see rows to restore from. The guarded
    ``SET LOCAL ROLE`` is what makes both directions correct.

    These tests pin the ceremony's MECHANISM, not the RLS policy itself: the
    real-Postgres cases use the testcontainer's superuser connection (the only
    one available here), where RLS never applies, so they prove the role is set,
    verified and restored — while the policy's refusal of an unguarded admin and
    the silent 0-row restore were demonstrated out-of-band (see the module
    docstring).
    """

    async def test_activates_verifies_and_restores_the_owning_role(self, db_url: str, monkeypatch) -> None:
        """Inside the block ``current_user`` IS the role; afterwards it is the admin.

        The role name is swapped for a uniquely-named scratch role so the test
        never depends on (or disturbs) a cluster-wide ``modulo_migrate`` another
        fixture may have provisioned. A superuser may ``SET ROLE`` to any role,
        so no membership grant is needed.
        """
        module = _load_migration()
        scratch = f"m0324_probe_{uuid.uuid4().hex[:10]}"
        monkeypatch.setattr(module, "_MIGRATE_ROLE", scratch)
        monkeypatch.setattr(module, "_SET_LOCAL_ROLE_SQL", "SET LOCAL ROLE " + scratch)

        engine = create_async_engine(db_url, poolclass=NullPool)
        try:
            async with engine.connect() as conn:
                admin = (await conn.execute(text("SELECT current_user"))).scalar_one()
                await conn.execute(text(f'CREATE ROLE "{scratch}" NOSUPERUSER NOLOGIN BYPASSRLS'))
                await conn.commit()
            try:

                def _exercise(conn: Connection) -> None:
                    with module._dml_as_migrate_role(conn):
                        assert conn.execute(text("SELECT current_user")).scalar_one() == scratch
                    assert conn.execute(text("SELECT current_user")).scalar_one() == admin

                async with engine.connect() as conn:
                    await conn.run_sync(_exercise)
            finally:
                async with engine.connect() as conn:
                    await conn.execute(text(f'DROP ROLE IF EXISTS "{scratch}"'))
                    await conn.commit()
        finally:
            await engine.dispose()

    async def test_no_op_when_the_owning_role_is_absent(self, db_url: str, monkeypatch) -> None:
        """A database without the role runs the DML as the migration connection.

        That is the fresh dev/BDD shape, where the migration connection owns the
        schema and bypasses RLS itself, so the ceremony must neither fail nor
        change the active role.
        """
        module = _load_migration()
        absent = f"m0324_absent_{uuid.uuid4().hex[:10]}"
        monkeypatch.setattr(module, "_MIGRATE_ROLE", absent)

        engine = create_async_engine(db_url, poolclass=NullPool)
        try:

            def _exercise(conn: Connection) -> None:
                admin = conn.execute(text("SELECT current_user")).scalar_one()
                with module._dml_as_migrate_role(conn):
                    assert conn.execute(text("SELECT current_user")).scalar_one() == admin
                    assert conn.execute(text("SELECT 1")).scalar_one() == 1

            async with engine.connect() as conn:
                await conn.run_sync(_exercise)
        finally:
            await engine.dispose()

    def test_upgrade_backfills_inside_the_set_local_role_block(self) -> None:
        """The pre-drop INSERT is issued AFTER ``SET LOCAL ROLE``, before ``RESET ROLE``.

        A refactor that "sets the role" but leaves the INSERT outside the block
        would silently restore the RLS failure this ceremony exists to remove.
        """
        module = _load_migration()
        bind = _RecordingBind(role_exists=True)
        with patch.object(module, "op", _RecordingOperations(bind)):
            module.upgrade()

        assert (
            _index_of(bind.sql, "SET LOCAL ROLE modulo_migrate")
            < _index_of(bind.sql, "INSERT INTO organisation_cost_state")
            < _index_of(bind.sql, "RESET ROLE")
        )

    def test_upgrade_runs_the_backfill_unwrapped_when_the_role_is_absent(self) -> None:
        """No role -> the same INSERT, without a `SET LOCAL ROLE` around it."""
        module = _load_migration()
        bind = _RecordingBind(role_exists=False)
        with patch.object(module, "op", _RecordingOperations(bind)):
            module.upgrade()

        insert_index = _index_of(bind.sql, "INSERT INTO organisation_cost_state")
        assert _index_of(bind.sql, "SET search_path TO public") < insert_index
        assert "SET LOCAL ROLE modulo_migrate" not in "\n".join(bind.sql)

    def test_downgrade_restores_inside_the_block_and_revokes_the_transient_grant(self) -> None:
        """GRANT -> SET LOCAL ROLE -> UPDATE -> RESET ROLE -> REVOKE, in that order.

        ``modulo_migrate`` owns the cost-state table but not ``organisations``,
        so the restore needs a transient column-level UPDATE grant. It must be
        revoked (a permanent widening is not acceptable) and must exist before
        the role switch, because only the owning connection can grant it.
        """
        module = _load_migration()
        bind = _RecordingBind(role_exists=True)
        with patch.object(module, "op", _RecordingOperations(bind)):
            module.downgrade()

        grant_index = _index_of(bind.sql, "GRANT UPDATE (")
        set_index = _index_of(bind.sql, "SET LOCAL ROLE modulo_migrate")
        update_index = _index_of(bind.sql, "UPDATE organisations o SET")
        reset_index = _index_of(bind.sql, "RESET ROLE")
        revoke_index = _index_of(bind.sql, "REVOKE UPDATE (")
        assert grant_index < set_index < update_index < reset_index < revoke_index
