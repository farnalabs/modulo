"""Structural + round-trip unit tests for migration 0210_collection_install_id_columns (FAR-765).

These run WITHOUT a database. They pin the contract that the denormalised
``collection_install_id`` pointer is added to the three entity tables
(``schemas`` / ``agents`` / ``pipelines``) by THIS migration, not by
``0207_collection_install_tracking``.

Rationale (FAR-765 review blocker): ``0207`` was already merged and applied on
main, so deployed databases at 0207+ would never re-run an amended ``0207``
``upgrade()`` and the columns would stay missing (``UndefinedColumnError`` at
runtime). Adding a fresh chained revision (0210 chains off 0209_community_gate)
guarantees the columns are created on every database that has not yet run this
revision. This test guards that the columns live in 0210 and would catch a
regression that folds them back into 0207.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path
from types import ModuleType
from unittest.mock import MagicMock, patch

_VERSIONS = Path(__file__).resolve().parents[3] / "src" / "modulo" / "db" / "migrations" / "versions"
_MIGRATION_NAME = "0210_collection_install_id_columns"
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
    """Captures op.execute SQL, op.add_column calls and op.create_index calls."""

    def __init__(self) -> None:
        self.sql: list[str] = []
        self.columns: dict[str, list[str]] = {}
        self.indexes: list[str] = []
        self.index_columns: dict[str, list[str]] = {}

    def execute(self, stmt: object, *_args: object, **_kwargs: object) -> MagicMock:
        self.sql.append(str(stmt))
        return MagicMock()

    def add_column(self, table_name: str, column: object, *_args: object, **_kwargs: object) -> None:
        self.columns.setdefault(table_name, []).append(getattr(column, "name", str(column)))

    def drop_column(self, table_name: str, column_name: str, *_args: object, **_kwargs: object) -> None:
        cols = self.columns.get(table_name)
        if cols and column_name in cols:
            cols.remove(column_name)

    def create_index(self, name: str, _table_name: str, columns: list[str], *_args: object, **_kwargs: object) -> None:
        self.indexes.append(name)
        self.index_columns[name] = list(columns)

    def drop_index(self, name: str, *_args: object, **_kwargs: object) -> None:
        if name in self.indexes:
            self.indexes.remove(name)

    def drop_table(self, _name: str, *_args: object, **_kwargs: object) -> None:  # pragma: no cover - not used here
        pass


def _make_bind(dialect: str) -> MagicMock:
    bind = MagicMock()
    bind.dialect.name = dialect
    return bind


def _run_upgrade(migration: ModuleType, dialect: str) -> _Recorder:
    rec = _Recorder()
    bind = _make_bind(dialect)
    with patch.object(migration, "op", rec):
        rec.get_bind = MagicMock(return_value=bind)  # type: ignore[attr-defined]
        migration.op.get_bind = MagicMock(return_value=bind)  # type: ignore[attr-defined]
        migration.upgrade()
    return rec


class TestEntityProvenanceColumns:
    """The ORM models (schema.py / agent.py / pipeline.py) declare a
    ``collection_install_id`` pointer that ``install.py`` / ``uninstall.py``
    stamp and clear on the live entity rows. This migration MUST add the
    denormalised column to the three entity tables (FAR-765 review blocker).
    """

    def test_adds_collection_install_id_to_entity_tables(self) -> None:
        migration = _load_migration()
        for dialect in ("sqlite", "postgresql"):
            rec = _run_upgrade(migration, dialect)
            for table in ("schemas", "agents", "pipelines"):
                cols = rec.columns.get(table, [])
                assert "collection_install_id" in cols, (
                    f"{dialect}: migration 0210 must add collection_install_id to {table}; got {cols}"
                )
                idx = f"ix_{table}_collection_install_id"
                assert idx in rec.indexes, f"{dialect}: migration 0210 must create index {idx}; got {rec.indexes}"

    def test_downgrade_drops_entity_columns(self) -> None:
        migration = _load_migration()
        rec = _Recorder()
        bind = _make_bind("postgresql")
        with patch.object(migration, "op", rec):
            rec.get_bind = MagicMock(return_value=bind)  # type: ignore[attr-defined]
            migration.op.get_bind = MagicMock(return_value=bind)  # type: ignore[attr-defined]
            migration.downgrade()
        for table in ("pipelines", "agents", "schemas"):
            assert "collection_install_id" not in rec.columns.get(table, []), (
                f"downgrade must drop collection_install_id from {table}"
            )

    def test_only_adds_entity_columns_no_new_tables(self) -> None:
        """A re-chained revision must not re-create the 0207 tables."""
        migration = _load_migration()
        rec = _run_upgrade(migration, "sqlite")
        assert not any("CREATE TABLE" in s.upper() for s in rec.sql), f"0210 must not create tables; got SQL: {rec.sql}"
