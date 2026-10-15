"""Unit tests for migration 0313_collection_install_drop_installed_entities.

Structural contract (no Postgres needed — the bind is mocked):

* **Revision chain** — 0313 revises ``0312_memberships_active_lookups``; the
  migrations directory has exactly one head
  (``0314_service_accounts_lifecycle``, which revises this migration, pinned by
  all sibling chain tests), the revision id matches the filename, and neither
  ``branch_labels`` nor ``depends_on`` is set (the pre-commit
  check-migration-heads hook and every ``test_single_head_*`` pin depend on
  that shape).
* **Upgrade (mocked ``op``)** — drops the duplicate
  ``collection_install.installed_entities`` JSON column, GATED on an inspector
  existence check so the upgrade is idempotent/re-runnable.
* **Downgrade** — re-adds the column (empty; see the migration docstring's
  data note), typed jsonb on PostgreSQL / generic JSON elsewhere, and skips
  when the column is already there.
* **Model parity** — the ORM model no longer declares ``installed_entities``
  (the ``collection_install_entity`` child table is the single source of
  truth, FAR-1754).
"""

from __future__ import annotations

import importlib.util
from pathlib import Path
from types import ModuleType
from unittest.mock import MagicMock, patch

from alembic.script import ScriptDirectory
from sqlalchemy.dialects import postgresql
from sqlalchemy.dialects.postgresql import JSONB

from modulo.db.models.collection_install import CollectionInstall, CollectionInstallEntity

_MIGRATION_REVISION = "0313_collection_install_drop_installed_entities"
_MIGRATION_DOWN_REVISION = "0312_memberships_active_lookups"
_HEAD_MIGRATION = "0314_service_accounts_lifecycle"

_DROPPED_COLUMN = "installed_entities"
_INSTALL_TABLE = "collection_install"

_VERSIONS = Path(__file__).resolve().parents[3] / "src" / "modulo" / "db" / "migrations" / "versions"
_MIGRATION_PATH = _VERSIONS / f"{_MIGRATION_REVISION}.py"


def _load_migration() -> ModuleType:
    assert _MIGRATION_PATH.exists(), f"Migration file missing: {_MIGRATION_PATH}"
    spec = importlib.util.spec_from_file_location(f"migration_{_MIGRATION_REVISION}", _MIGRATION_PATH)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class _Recorder:
    """Captures op.add_column and op.drop_column calls."""

    def __init__(self) -> None:
        self.added: list[tuple[str, str]] = []
        self.added_columns: list[tuple[str, object]] = []
        self.dropped: list[tuple[str, str]] = []

    def execute(self, stmt: object, *_args: object, **_kwargs: object) -> MagicMock:
        return MagicMock()

    def add_column(self, table_name: str, column: object, *_args: object, **_kwargs: object) -> None:
        self.added.append((table_name, getattr(column, "name", str(column))))
        self.added_columns.append((table_name, column))

    def drop_column(self, table_name: str, column_name: str, *_args: object, **_kwargs: object) -> None:
        self.dropped.append((table_name, column_name))


def _run(what: str, *, column_exists: bool) -> _Recorder:
    """Run *what* with a mocked ``op`` and a stubbed inspector check."""
    module = _load_migration()
    rec = _Recorder()
    with (
        patch.object(module, "op", rec),
        patch.object(module, "_column_exists", return_value=column_exists),
    ):
        getattr(module, what)()
    return rec


class TestChain:
    def test_single_head_is_0314_service_accounts_lifecycle(self) -> None:
        heads = ScriptDirectory(str(_VERSIONS.parent)).get_heads()
        assert heads == [_HEAD_MIGRATION], f"expected a single head, got {heads}"

    def test_down_revision_is_0312_memberships_active_lookups(self) -> None:
        assert _load_migration().down_revision == _MIGRATION_DOWN_REVISION

    def test_revision_id_matches_filename(self) -> None:
        assert _load_migration().revision == _MIGRATION_REVISION

    def test_no_branch_labels_or_depends_on(self) -> None:
        module = _load_migration()
        assert module.branch_labels is None
        assert module.depends_on is None


class TestUpgrade:
    def test_drops_the_duplicate_json_column_when_present(self) -> None:
        rec = _run("upgrade", column_exists=True)
        assert (_INSTALL_TABLE, _DROPPED_COLUMN) in rec.dropped

    def test_upgrade_is_idempotent_when_column_already_gone(self) -> None:
        rec = _run("upgrade", column_exists=False)
        assert not rec.dropped, f"expected no drop_column when the column is absent, got {rec.dropped}"


class TestDowngrade:
    def test_readds_the_column_when_absent(self) -> None:
        rec = _run("downgrade", column_exists=False)
        assert (_INSTALL_TABLE, _DROPPED_COLUMN) in rec.added

    def test_downgrade_skips_when_the_column_already_exists(self) -> None:
        rec = _run("downgrade", column_exists=True)
        assert not rec.added, f"expected no add_column when the column exists, got {rec.added}"

    def test_readded_column_is_nullable_jsonb_on_postgresql(self) -> None:
        rec = _run("downgrade", column_exists=False)
        added = [
            col
            for table, col in rec.added_columns
            if table == _INSTALL_TABLE and getattr(col, "name", None) == _DROPPED_COLUMN
        ]
        assert len(added) == 1, f"expected exactly one added column, got {added}"
        column = added[0]
        impl = column.type.dialect_impl(postgresql.dialect())
        assert isinstance(impl, JSONB), f"resolved to {type(impl)!r}, expected JSONB"
        assert column.nullable is True


class TestModelParity:
    def test_model_no_longer_declares_the_dropped_column(self) -> None:
        """FAR-1754: the JSON copy is GONE from the ORM — the
        ``collection_install_entity`` child table is the single source of
        truth for what an install wrote."""
        columns = set(CollectionInstall.__table__.columns.keys())
        assert _DROPPED_COLUMN not in columns, "collection_install.installed_entities must be dropped (FAR-1754)"

    def test_entity_child_table_is_intact(self) -> None:
        columns = set(CollectionInstallEntity.__table__.columns.keys())
        assert "install_id" in columns
        assert "entity_type" in columns
        assert "entity_id" in columns
