"""Unit tests for migration 0308_library_cluster_db_hardening.

Structural contract (no Postgres needed):

* **Revision chain** - 0308 revises ``0307_runs_guardrail_override_resume_pending``; the
  migrations directory has exactly one head
  (``0308_library_cluster_db_hardening``, pinned by all sibling chain tests),
  the revision id matches the filename, and neither ``branch_labels`` nor
  ``depends_on`` is set (the pre-commit check-migration-heads hook and every
  ``test_single_head_*`` pin depend on that shape).
* **Upgrade (mocked ``op``)** - creates the three new ``library_primitives``
  indexes, adds ``collection_install.deleted_at`` / ``created_by`` (guarded by
  the inspector check), converts the 0207 unique constraint into a partial
  unique index of the same name, and drops the redundant 0165 CHECK pair.
* **Idempotency** - with the columns already present, the upgrade adds no
  column; every index/constraint statement is ``IF EXISTS`` / ``IF NOT
  EXISTS`` shaped.
* **Downgrade** - reverses everything: drops the new indexes, restores the
  0207 unique constraint and the 0165 CHECK pair, drops ``created_by`` (+
  FK/index) and ``deleted_at``.
* **Model parity** - the ORM models carry the same index/column shape the
  migration writes, so model/migration drift cannot silently reappear.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path
from types import ModuleType
from unittest.mock import MagicMock, patch

from alembic.script import ScriptDirectory
from sqlalchemy import Index

from modulo.db.models.collection_install import CollectionInstall
from modulo.db.models.composite_template import CompositeTemplate
from modulo.db.models.library_primitive import LibraryPrimitive

_MIGRATION_REVISION = "0308_library_cluster_db_hardening"
_MIGRATION_DOWN_REVISION = "0307_runs_guardrail_override_resume_pending"
_HEAD_MIGRATION = "0308_library_cluster_db_hardening"

_INSTALL_UNIQUE_INDEX = "uq_collection_install_org_collection"
_CREATED_BY_FK = "fk_collection_install_created_by"
_0165_CHECKS = ("ck_library_primitive_download_count", "ck_library_primitive_review_count")

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


def _source() -> str:
    return _MIGRATION_PATH.read_text(encoding="utf-8")


class _Recorder:
    """Captures op.execute SQL, op.add_column and op.drop_column calls."""

    def __init__(self) -> None:
        self.sql: list[str] = []
        self.added: list[tuple[str, str]] = []
        self.dropped: list[tuple[str, str]] = []

    def execute(self, stmt: object, *_args: object, **_kwargs: object) -> MagicMock:
        self.sql.append(str(stmt))
        return MagicMock()

    def add_column(self, table_name: str, column: object, *_args: object, **_kwargs: object) -> None:
        self.added.append((table_name, getattr(column, "name", str(column))))

    def drop_column(self, table_name: str, column_name: str, *_args: object, **_kwargs: object) -> None:
        self.dropped.append((table_name, column_name))


def _run(what: str, *, columns_exist: bool = False) -> _Recorder:
    """Run *what* with a mocked ``op`` and a stubbed inspector column check."""
    module = _load_migration()
    rec = _Recorder()
    with (
        patch.object(module, "op", rec),
        patch.object(module, "_column_exists", return_value=columns_exist),
    ):
        getattr(module, what)()
    return rec


def _joined(rec: _Recorder) -> str:
    return "\n".join(rec.sql)


class TestChain:
    def test_single_head_is_0308_library_cluster_db_hardening(self) -> None:
        heads = ScriptDirectory(str(_VERSIONS.parent)).get_heads()
        assert heads == [_HEAD_MIGRATION], f"expected a single head, got {heads}"

    def test_down_revision_is_0307_runs_guardrail_override_resume_pending(self) -> None:
        assert _load_migration().down_revision == _MIGRATION_DOWN_REVISION

    def test_revision_id_matches_filename(self) -> None:
        assert _load_migration().revision == _MIGRATION_REVISION

    def test_no_branch_labels_or_depends_on(self) -> None:
        module = _load_migration()
        assert module.branch_labels is None
        assert module.depends_on is None


class TestUpgrade:
    def test_creates_the_three_library_primitive_indexes(self) -> None:
        joined = _joined(_run("upgrade"))
        assert "CREATE INDEX IF NOT EXISTS ix_library_primitives_version_group_id" in joined
        assert "CREATE INDEX IF NOT EXISTS ix_library_primitives_type_slug" in joined
        assert "CREATE INDEX IF NOT EXISTS ix_library_primitives_visibility_contribution" in joined

    def test_community_listing_index_is_partial_on_deleted_at(self) -> None:
        rec = _run("upgrade")
        matching = [ddl for ddl in rec.sql if "ix_library_primitives_visibility_contribution" in ddl]
        assert len(matching) == 1, f"expected exactly one visibility index statement, got {matching}"
        assert "WHERE deleted_at IS NULL" in matching[0]

    def test_adds_deleted_at_and_created_by_when_absent(self) -> None:
        rec = _run("upgrade", columns_exist=False)
        assert ("collection_install", "deleted_at") in rec.added
        assert ("collection_install", "created_by") in rec.added

    def test_no_column_adds_when_already_present(self) -> None:
        rec = _run("upgrade", columns_exist=True)
        assert not rec.added, f"expected no add_column calls when columns exist, got {rec.added}"

    def test_converts_unique_constraint_to_partial_unique_index(self) -> None:
        joined = _joined(_run("upgrade"))
        assert f"ALTER TABLE collection_install DROP CONSTRAINT IF EXISTS {_INSTALL_UNIQUE_INDEX}" in joined
        create_line = next(ddl for ddl in _run("upgrade").sql if "CREATE UNIQUE INDEX" in ddl)
        assert _INSTALL_UNIQUE_INDEX in create_line
        assert "WHERE deleted_at IS NULL" in create_line

    def test_adds_created_by_fk_and_index(self) -> None:
        joined = _joined(_run("upgrade", columns_exist=False))
        assert f"ALTER TABLE collection_install ADD CONSTRAINT {_CREATED_BY_FK}" in joined
        assert "CREATE INDEX IF NOT EXISTS ix_collection_install_created_by" in joined

    def test_drops_the_redundant_0165_checks(self) -> None:
        joined = _joined(_run("upgrade"))
        for check_name in _0165_CHECKS:
            assert f"ALTER TABLE library_primitives DROP CONSTRAINT IF EXISTS {check_name}" in joined

    def test_keeps_the_0151_check_pair(self) -> None:
        joined = _joined(_run("upgrade"))
        assert "ck_library_primitives_dl_count" not in joined
        assert "ck_library_primitives_review_count" not in joined


class TestDowngrade:
    def test_drops_the_new_indexes(self) -> None:
        joined = _joined(_run("downgrade"))
        assert "DROP INDEX IF EXISTS ix_library_primitives_version_group_id" in joined
        assert "DROP INDEX IF EXISTS ix_library_primitives_type_slug" in joined
        assert "DROP INDEX IF EXISTS ix_library_primitives_visibility_contribution" in joined
        assert "DROP INDEX IF EXISTS ix_collection_install_created_by" in joined

    def test_restores_the_0207_unique_constraint(self) -> None:
        joined = _joined(_run("downgrade"))
        assert f"DROP INDEX IF EXISTS {_INSTALL_UNIQUE_INDEX}" in joined
        assert (
            f"ALTER TABLE collection_install ADD CONSTRAINT {_INSTALL_UNIQUE_INDEX} "
            "UNIQUE (organisation_id, collection_id)" in joined
        )

    def test_drops_created_by_and_deleted_at(self) -> None:
        rec = _run("downgrade")
        assert ("collection_install", "created_by") in rec.dropped
        assert ("collection_install", "deleted_at") in rec.dropped

    def test_drops_the_created_by_fk(self) -> None:
        joined = _joined(_run("downgrade"))
        assert f"ALTER TABLE collection_install DROP CONSTRAINT IF EXISTS {_CREATED_BY_FK}" in joined

    def test_restores_the_0165_checks(self) -> None:
        joined = _joined(_run("downgrade"))
        assert "ALTER TABLE library_primitives ADD CONSTRAINT ck_library_primitive_download_count" in joined
        assert "ALTER TABLE library_primitives ADD CONSTRAINT ck_library_primitive_review_count" in joined


class TestMigrationSource:
    def test_never_edits_shipped_revisions(self) -> None:
        # 0165 / 0207 / 0156 are shipped; 0308 changes their effects via NEW
        # DDL, never by editing their files.
        source = _source()
        assert "0165_add_check_constraints.py" not in source
        assert "0207_collection_install_tracking.py" not in source
        assert "0156_add_soft_delete_partial_uniques.py" not in source

    def test_upgrade_and_downgrade_never_use_concurrently(self) -> None:
        # env.py's single transaction makes CONCURRENTLY impossible (the
        # module docstring explains why). The docstring legitimately NAMES
        # ``CREATE INDEX CONCURRENTLY`` while explaining why it is unavailable —
        # so the assertion runs against the EXECUTED statements, not the source.
        executed = _joined(_run("upgrade")) + _joined(_run("downgrade"))
        assert "CONCURRENTLY" not in executed


class TestModelParity:
    def test_collection_install_carries_soft_delete_and_actor(self) -> None:
        columns = set(CollectionInstall.__table__.columns.keys())
        assert "deleted_at" in columns
        assert "created_by" in columns

    def test_collection_install_unique_is_a_partial_unique_index(self) -> None:
        indexes = [i for i in CollectionInstall.__table__.indexes if i.name == _INSTALL_UNIQUE_INDEX]
        assert len(indexes) == 1, f"expected one {_INSTALL_UNIQUE_INDEX} index, got {indexes}"
        assert isinstance(indexes[0], Index)
        assert indexes[0].unique
        pg_where = indexes[0].dialect_options["postgresql"].get("where")
        assert pg_where is not None
        assert "deleted_at IS NULL" in str(pg_where)
        constraints = [c.name for c in CollectionInstall.__table__.constraints]
        assert _INSTALL_UNIQUE_INDEX not in constraints

    def test_library_primitive_declares_the_new_indexes(self) -> None:
        names = {i.name for i in LibraryPrimitive.__table__.indexes}
        assert "ix_library_primitives_type_slug" in names
        assert "ix_library_primitives_visibility_contribution" in names

    def test_library_primitive_version_group_id_is_indexed(self) -> None:
        column = LibraryPrimitive.__table__.columns["version_group_id"]
        assert column.index

    def test_composite_template_declares_the_0156_partial_unique(self) -> None:
        indexes = [i for i in CompositeTemplate.__table__.indexes if i.name == "uq_composite_templates_org_name"]
        assert len(indexes) == 1, f"expected one uq_composite_templates_org_name index, got {indexes}"
        assert indexes[0].unique
        pg_where = indexes[0].dialect_options["postgresql"].get("where")
        assert pg_where is not None
