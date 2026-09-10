"""Structural + round-trip unit tests for migration 0209_collection_install_id_entity_columns (FAR-762/FAR-761).

These run WITHOUT a database. They pin the contract the PR-review loop on PR
#353 requested a regression test for:

* The ``collection_install_id`` column is owned by migration ``0207`` (which
  adds it idempotently via ``ADD COLUMN IF NOT EXISTS``). Migration 0209 must
  NOT attempt ``op.add_column`` for that column — doing so raised
  ``DuplicateColumn`` on any DB that had already run 0207, cascading into every
  integration/BDD test. 0209 only creates the index each ORM model declares
  (``index=True``) via ``CREATE INDEX IF NOT EXISTS``.
* downgrade drops only the index (``DROP INDEX IF EXISTS``), never the column.
* The SQLite path runs without the role ceremony / ``SET search_path``.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path
from types import ModuleType
from unittest.mock import MagicMock, patch

from modulo.db.models import Agent, Pipeline, Schema

_VERSIONS = Path(__file__).resolve().parents[3] / "src" / "modulo" / "db" / "migrations" / "versions"
_MIGRATION_NAME = "0209_collection_install_id_entity_columns"
_MIGRATION_PATH = _VERSIONS / f"{_MIGRATION_NAME}.py"

_ENTITY_TABLES = ("schemas", "agents", "pipelines")
_COLUMN = "collection_install_id"


def _load_migration() -> ModuleType:
    assert _MIGRATION_PATH.exists(), f"Migration file missing: {_MIGRATION_PATH}"
    spec = importlib.util.spec_from_file_location(f"migration_{_MIGRATION_NAME}", _MIGRATION_PATH)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class _Recorder:
    """Captures op.execute SQL plus the op-level DDL calls (add_column /
    create_index) so we can assert on both the raw SQL the migration emits and
    the structural DDL it requests.
    """

    def __init__(self) -> None:
        self.sql: list[str] = []
        self.added_columns: list[tuple[str, str | None]] = []
        self.indexes: list[str] = []

    def execute(self, stmt: object, *_args: object, **_kwargs: object) -> MagicMock:
        self.sql.append(str(stmt))
        return MagicMock()

    def add_column(self, table: str, column: object, *_args: object, **_kwargs: object) -> MagicMock:
        name = getattr(column, "name", None)
        self.added_columns.append((table, name))
        return MagicMock()

    def create_index(self, name: str, *_args: object, **_kwargs: object) -> MagicMock:
        self.indexes.append(name)
        return MagicMock()

    def get_bind(self) -> MagicMock:
        return MagicMock()


def _make_bind(dialect: str) -> MagicMock:
    bind = MagicMock()
    bind.dialect.name = dialect
    return bind


def _run_upgrade(
    migration: ModuleType,
    dialect: str,
    *,
    roles_exist: bool,
    table_owner: str | None,
    column_present: bool = True,
    index_present: bool = False,
) -> _Recorder:
    """Run the migration's upgrade against a mocked ``op`` + bind.

    ``column_present`` / ``index_present`` simulate the state left by migration
    0207 (which owns the column and, on a fresh DB, the index too). The
    regression guard is that 0209 must NOT re-add a column 0207 already created,
    while still creating the ORM-declared index when it is missing.
    """
    rec = _Recorder()
    bind = _make_bind(dialect)
    inspector = MagicMock()

    def _columns(table: str) -> list[dict[str, str]]:
        return [{"name": _COLUMN}] if column_present else []

    def _indexes(table: str) -> list[dict[str, str]]:
        return [{"name": f"ix_{table}_{_COLUMN}"}] if index_present else []

    inspector.get_columns.side_effect = _columns
    inspector.get_indexes.side_effect = _indexes
    with (
        patch.object(migration, "op", rec),
        patch.object(migration, "_is_postgres", return_value=(dialect == "postgresql")),
        patch.object(migration, "_role_exists", return_value=roles_exist),
        patch.object(migration, "_table_owner", return_value=table_owner),
        patch.object(migration, "_assert_owner_is_migrate", return_value=None),
        patch.object(migration, "inspect", return_value=inspector),
    ):
        rec.get_bind = MagicMock(return_value=bind)  # type: ignore[attr-defined]
        migration.op.get_bind = MagicMock(return_value=bind)  # type: ignore[attr-defined]
        migration.upgrade()
    return rec


def _run_downgrade(migration: ModuleType) -> _Recorder:
    rec = _Recorder()
    bind = _make_bind("postgresql")
    with (
        patch.object(migration, "op", rec),
        patch.object(migration, "_is_postgres", return_value=True),
    ):
        rec.get_bind = MagicMock(return_value=bind)  # type: ignore[attr-defined]
        migration.op.get_bind = MagicMock(return_value=bind)  # type: ignore[attr-defined]
        migration.downgrade()
    return rec


class TestNoAddColumnRegression:
    """The bug from PR #353: 0209 must not re-add the column 0207 owns."""

    def test_upgrade_never_adds_collection_install_id_entity_column(self) -> None:
        migration = _load_migration()
        for dialect in ("sqlite", "postgresql"):
            rec = _run_upgrade(migration, dialect, roles_exist=True, table_owner="modulo_migrate")
            for sql in rec.sql:
                low = sql.lower()
                # The migration ran 0207 first, so the column already exists on
                # the tables it targets. Re-adding it aborts the whole chain.
                assert "add column" not in low or _COLUMN not in low or "if not exists" in low, (
                    f"0209 must not add column {_COLUMN}: {sql}"
                )
                assert not (f"add column {_COLUMN}" in low or f"add column if not exists {_COLUMN}" in low), (
                    f"0209 must not add column {_COLUMN}: {sql}"
                )
            # 0207 already owns the column, so 0209 must not request op.add_column
            # for it on any dialect (this is the DuplicateColumn regression).
            assert rec.added_columns == [], f"0209 must not add column {_COLUMN}: {rec.added_columns}"

    def test_upgrade_creates_index_if_not_exists_per_table(self) -> None:
        migration = _load_migration()
        rec = _run_upgrade(migration, "sqlite", roles_exist=False, table_owner=None)
        # 0209 requests the ORM-declared index via op.create_index for each
        # entity table (idempotently — skipped when the index already exists).
        created = rec.indexes
        assert len(created) == len(_ENTITY_TABLES), f"expected {len(_ENTITY_TABLES)} index creates, got {created}"
        for table in _ENTITY_TABLES:
            assert any(f"ix_{table}_{_COLUMN}" in s.lower() for s in created), f"missing index on {table}: {created}"


class TestIndexNamesMatchOrm:
    def test_orm_index_names_match_migration(self) -> None:
        # SQLAlchemy's index=True default name is ix_<table>_<column>. Confirm
        # the ORM declares an index on collection_install_id for each entity
        # table, and that its name matches what 0209 creates.
        for model in (Schema, Agent, Pipeline):
            indexes = {ix.name for ix in model.__table__.indexes}
            matching = {name for name in indexes if name.endswith(f"_{_COLUMN}")}
            assert matching, f"{model.__tablename__} declares no index ending in _{_COLUMN}: {indexes}"
            assert any(name == f"ix_{model.__tablename__}_{_COLUMN}" for name in matching), (
                f"{model.__tablename__} index name mismatch: {matching}"
            )


class TestSqliteParityPath:
    def test_no_role_ceremony_on_sqlite(self) -> None:
        migration = _load_migration()
        rec = _run_upgrade(migration, "sqlite", roles_exist=False, table_owner=None)
        joined = " ".join(rec.sql).upper()
        assert "SET ROLE" not in joined
        assert "SET SEARCH_PATH" not in joined


class TestDowngradeDropsOnlyIndex:
    def test_downgrade_drops_index_not_column(self) -> None:
        migration = _load_migration()
        rec = _run_downgrade(migration)
        dropped = [s for s in rec.sql if "drop index if exists" in s.lower()]
        assert len(dropped) == len(_ENTITY_TABLES), f"expected {len(_ENTITY_TABLES)} drops, got {dropped}"
        for table in _ENTITY_TABLES:
            assert any(f"ix_{table}_{_COLUMN}" in s.lower() for s in dropped), f"missing drop on {table}: {dropped}"
        joined = " ".join(rec.sql).lower()
        assert "drop column" not in joined, "downgrade must not drop the column owned by 0207"
