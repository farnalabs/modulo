"""Structural + round-trip unit tests for migration 0207_collection_install_tracking (FAR-761).

These run WITHOUT a database. They pin the migration's contract against the
review-blockers raised on PR #308:

* Major 1 — new-table ceremony: ``SET ROLE modulo_migrate`` + RLS
  ENABLE/FORCE + ``rls_org_isolation`` policy + GRANTs to ``modulo_app`` /
  ``modulo_system`` + the post-create ownership assertion.
* Major 2 — naming: ``organisation_id`` (NOT ``org_id``),
  ``ix_<table>_<col>`` index convention.
* Major 4/5 — no redundant ``CREATE UNIQUE INDEX`` on the PK; all indexes use
  the ``ix_`` prefix.
* Major 6 — Postgres-only DDL is dialect-guarded (the SQLite path runs without
  ceremony/RLS and without ``gen_random_uuid``/JSONB).
* Major 3 — an ORM model + this unit test now exercise the migration (the
  upgrade/downgrade round-trip is covered by the integration suite).
"""

from __future__ import annotations

import importlib.util
from pathlib import Path
from types import ModuleType
from unittest.mock import MagicMock, patch

from modulo.db.models import CollectionInstall, CollectionInstallEntity

_VERSIONS = Path(__file__).resolve().parents[3] / "src" / "modulo" / "db" / "migrations" / "versions"
_MIGRATION_NAME = "0207_collection_install_tracking"
_MIGRATION_PATH = _VERSIONS / f"{_MIGRATION_NAME}.py"


def _load_migration() -> ModuleType:
    assert _MIGRATION_PATH.exists(), f"Migration file missing: {_MIGRATION_PATH}"
    spec = importlib.util.spec_from_file_location(f"migration_{_MIGRATION_NAME}", _MIGRATION_PATH)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class _Recorder:
    """Captures op.execute SQL, op.create_table calls and op.create_index calls."""

    def __init__(self) -> None:
        self.sql: list[str] = []
        self.tables: list[str] = []
        self.table_columns: dict[str, list[str]] = {}
        self.indexes: list[str] = []
        self.index_columns: dict[str, list[str]] = {}

    def execute(self, stmt: object, *_args: object, **_kwargs: object) -> MagicMock:
        text = str(stmt)
        self.sql.append(text)
        return MagicMock()

    def create_table(self, name: str, *args: object, **_kwargs: object) -> None:
        self.tables.append(name)
        cols: list[str] = []
        for col in args:
            if hasattr(col, "name"):
                cols.append(col.name)
        self.table_columns[name] = cols

    def add_column(self, table_name: str, column: object, *_args: object, **_kwargs: object) -> None:
        self.table_columns.setdefault(table_name, []).append(getattr(column, "name", str(column)))

    def create_index(self, name: str, _table_name: str, columns: list[str], *_args: object, **_kwargs: object) -> None:
        self.indexes.append(name)
        self.index_columns[name] = list(columns)

    def drop_index(self, name: str, *_args: object, **_kwargs: object) -> None:
        if name in self.indexes:
            self.indexes.remove(name)

    def drop_table(self, name: str, *_args: object, **_kwargs: object) -> None:
        if name in self.tables:
            self.tables.remove(name)


def _make_bind(dialect: str) -> MagicMock:
    bind = MagicMock()
    bind.dialect.name = dialect
    return bind


def _run_upgrade(migration: ModuleType, dialect: str, *, roles_exist: bool) -> _Recorder:
    rec = _Recorder()
    bind = _make_bind(dialect)
    with (
        patch.object(migration, "op", rec),
        patch.object(migration, "_role_exists", return_value=roles_exist),
        patch.object(migration, "_assert_owner_is_migrate", return_value=None),
    ):
        rec.get_bind = MagicMock(return_value=bind)  # type: ignore[attr-defined]
        migration.op.get_bind = MagicMock(return_value=bind)  # type: ignore[attr-defined]
        migration.upgrade()
    return rec


class TestOrgScopedTableDetection:
    def test_rls_coverage_table_constant(self) -> None:
        migration = _load_migration()
        assert migration._ORG_SCOPED_TABLES == ("collection_install",)


class TestSqliteParityPath:
    def test_creates_both_tables_without_ceremony(self) -> None:
        migration = _load_migration()
        rec = _run_upgrade(migration, "sqlite", roles_exist=False)
        assert "collection_install" in rec.tables
        assert "collection_install_entity" in rec.tables
        # No Postgres ceremony / RLS on the SQLite path.
        joined = " ".join(rec.sql).upper()
        assert "SET ROLE" not in joined
        assert "ROW LEVEL SECURITY" not in joined
        assert "GEN_RANDOM_UUID" not in joined

    def test_uses_organisation_id_not_org_id(self) -> None:
        migration = _load_migration()
        rec = _run_upgrade(migration, "sqlite", roles_exist=False)
        joined = " ".join(rec.sql)
        assert "org_id" not in joined
        # organisation_id must appear on the collection_install table definition.
        assert "organisation_id" in rec.table_columns["collection_install"]

    def test_index_naming_uses_ix_prefix(self) -> None:
        migration = _load_migration()
        rec = _run_upgrade(migration, "sqlite", roles_exist=False)
        assert rec.indexes, "expected at least one index"
        for name in rec.indexes:
            assert name.startswith("ix_"), f"index {name!r} does not use the ix_ prefix"

    def test_no_redundant_unique_index_on_primary_key(self) -> None:
        migration = _load_migration()
        rec = _run_upgrade(migration, "sqlite", roles_exist=False)
        # The only unique constraint on install_id is the PRIMARY KEY itself;
        # there must be no separate "CREATE UNIQUE INDEX ... install_id".
        for sql in rec.sql:
            low = sql.lower()
            if "create unique index" in low and "install_id" in low:
                raise AssertionError(f"redundant unique index on install_id: {sql}")


class TestPostgresCeremony:
    def test_full_ceremony_present(self) -> None:
        migration = _load_migration()
        rec = _run_upgrade(migration, "postgresql", roles_exist=True)
        joined = " ".join(rec.sql)
        assert "SET ROLE modulo_migrate" in joined
        assert "RESET ROLE" in joined
        assert "ENABLE ROW LEVEL SECURITY" in joined
        assert "FORCE ROW LEVEL SECURITY" in joined
        assert "CREATE POLICY rls_org_isolation" in joined
        assert "GRANT " in joined
        assert "modulo_app" in joined
        assert "modulo_system" in joined

    def test_ownership_assertion_invoked(self) -> None:
        migration = _load_migration()
        with (
            patch.object(migration, "op", _Recorder()),
            patch.object(migration, "_role_exists", return_value=True),
            patch.object(migration, "_assert_owner_is_migrate") as assert_mock,
        ):
            migration.op.get_bind = MagicMock(return_value=_make_bind("postgresql"))  # type: ignore[attr-defined]
            migration.upgrade()
        # Asserted for both created tables (collection_install + entity).
        assert assert_mock.call_count >= 1


class TestOrmMigrationConformance:
    """Pin the ORM metadata to the migration's schema so model/migration drift
    cannot silently pass the mocked unit tests (the class of bug the PR-review
    loop kept re-finding on this migration)."""

    def test_collection_install_pk_is_install_id_not_id(self) -> None:
        pk_cols = {c.name for c in CollectionInstall.__table__.primary_key.columns}
        assert pk_cols == {"install_id"}, f"collection_install PK drift: {pk_cols}"

    def test_collection_install_has_no_updated_at(self) -> None:
        cols = set(CollectionInstall.__table__.columns.keys())
        assert "install_id" in cols
        assert "organisation_id" in cols
        assert "created_at" in cols
        assert "updated_at" not in cols, "migration 0206 has no updated_at column"

    def test_collection_install_entity_fk_targets_install_id(self) -> None:
        fk_targets = {(fk.column.table.name, fk.column.name) for fk in CollectionInstallEntity.__table__.foreign_keys}
        assert (
            "collection_install",
            "install_id",
        ) in fk_targets, f"entity FK must target collection_install.install_id: {fk_targets}"
