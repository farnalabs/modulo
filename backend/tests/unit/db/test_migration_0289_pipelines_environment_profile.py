"""Structural tests for migration 0289 (FAR-1558) — no database.

0289_pipelines_environment_profile adds the per-pipeline environment-profile
binding column that Slice 1 of FAR-1558 makes settable on the pipeline PATCH
route and copies onto every snapshot frozen afterwards.

Lenses:

* **Chain** — 0289 chains onto ``0288_runs_execution_origin``, and
  ``0290_scheduled_reports_due_scan`` chains onto 0289, and
  ``0291_invitations_lookup_constraints`` chains onto 0290, and
  ``0292_audit_events_resource_lookup`` chains onto 0291_invitations_lookup_constraints as the single
  linear head; the revision id matches the filename, and neither
  branch_labels nor depends_on is set (the pre-commit check-migration-heads
  hook and every ``test_single_head_*`` pin depend on that shape).
* **Structure (mocked ``op``)** — upgrade emits FOUR calls IN ORDER: the
  nullable ``add_column``, the ``create_foreign_key`` (ON DELETE SET NULL),
  the ``create_index``, and the guarded same-org tenant-trigger DO block.
  Downgrade reverses exactly those four, trigger first.
* **Tenant guard** — the trigger statement must be the idempotent
  ``IF NOT EXISTS (SELECT 1 FROM pg_trigger ...)`` form, fire on
  ``INSERT OR UPDATE OF environment_profile_id, organisation_id``, and call
  ``public.enforce_same_organisation('environment_profiles', ...)`` — the
  same shape 0110 installs for the other ``pipelines`` FK columns.
* **Model parity** — ``Pipeline.environment_profile_id`` must declare the
  same nullable Uuid column, the same SET NULL FK, and the same index name,
  so ``create_all``'d schemas and ``alembic upgrade`` stay in sync
  (``test_initial_migration``'s ORM-vs-migrated-DB parity check).
"""

from __future__ import annotations

import importlib.util
from pathlib import Path
from types import ModuleType
from unittest.mock import MagicMock, patch

import sqlalchemy as sa
from alembic.script import ScriptDirectory

from modulo.db.models.pipeline import Pipeline

_MIGRATION_REVISION = "0289_pipelines_environment_profile"
_MIGRATION_DOWN_REVISION = "0288_runs_execution_origin"
_HEAD_MIGRATION = "0292_audit_events_resource_lookup"
_TABLE = "pipelines"
_COLUMN = "environment_profile_id"
_FK_NAME = "fk_pipelines_environment_profile_id"
_INDEX_NAME = "ix_pipelines_environment_profile_id"
_TRIGGER_NAME = "trg_pipelines_environment_profile_id_tenant"
_REFERENCED_TABLE = "environment_profiles"

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


def _run(step: str) -> MagicMock:
    """Run ``upgrade()``/``downgrade()`` against a mocked ``op`` and return it."""
    module = _load_migration()
    with patch.object(module, "op") as op:
        getattr(module, step)()
    return op


class TestChain:
    def test_single_head_is_0289_pipelines_environment_profile(self) -> None:
        heads = ScriptDirectory(str(_VERSIONS.parent)).get_heads()
        assert heads == [_HEAD_MIGRATION], f"expected a single head, got {heads}"

    def test_down_revision_is_0288_runs_execution_origin(self) -> None:
        assert _load_migration().down_revision == _MIGRATION_DOWN_REVISION

    def test_revision_id_matches_filename(self) -> None:
        assert _load_migration().revision == _MIGRATION_REVISION

    def test_no_branch_labels_or_depends_on(self) -> None:
        module = _load_migration()
        assert module.branch_labels is None
        assert module.depends_on is None


class TestUpgrade:
    def test_emits_column_then_fk_then_index_then_trigger_in_order(self) -> None:
        op = _run("upgrade")
        names = [call[0] for call in op.mock_calls]
        assert names == ["add_column", "create_foreign_key", "create_index", "execute"], names

    def test_add_column_is_nullable_uuid_on_pipelines(self) -> None:
        op = _run("upgrade")
        op.add_column.assert_called_once()
        table, column = op.add_column.call_args.args
        assert table == _TABLE, table
        assert column.name == _COLUMN, column.name
        assert column.nullable is True, "the binding must be nullable — NULL is the default route"
        assert isinstance(column.type, sa.Uuid), type(column.type)

    def test_foreign_key_is_set_null_to_environment_profiles(self) -> None:
        op = _run("upgrade")
        op.create_foreign_key.assert_called_once()
        args = op.create_foreign_key.call_args.args
        kwargs = op.create_foreign_key.call_args.kwargs
        assert args == (_FK_NAME, _TABLE, _REFERENCED_TABLE, [_COLUMN], ["id"]), args
        # Deleting a profile must UNBIND the pipeline, never delete it.
        assert kwargs.get("ondelete") == "SET NULL", kwargs

    def test_index_is_the_single_column_binding_index(self) -> None:
        op = _run("upgrade")
        op.create_index.assert_called_once()
        args = op.create_index.call_args.args
        assert args == (_INDEX_NAME, _TABLE, [_COLUMN]), args

    def test_tenant_trigger_is_the_idempotent_same_org_form(self) -> None:
        """The trigger must be guarded, column-scoped and same-org enforced."""
        op = _run("upgrade")
        op.execute.assert_called_once()
        stmt = str(op.execute.call_args.args[0])
        assert "IF NOT EXISTS (SELECT 1 FROM pg_trigger" in stmt, stmt
        assert f"tgname='{_TRIGGER_NAME}'" in stmt, stmt
        assert "BEFORE INSERT OR UPDATE OF environment_profile_id, organisation_id" in stmt, stmt
        assert f"ON public.{_TABLE}" in stmt, stmt
        assert f"enforce_same_organisation('{_REFERENCED_TABLE}', '{_COLUMN}')" in stmt, stmt
        assert "DO $$" in stmt, stmt

    def test_no_string_formatted_ddl(self) -> None:
        """S608 / migration-fstring-sql: the DDL must be a literal, not an f-string."""
        source = _source()
        assert "op.execute(f" not in source, source


class TestDowngrade:
    def test_removes_trigger_then_index_then_constraint_then_column(self) -> None:
        op = _run("downgrade")
        names = [call[0] for call in op.mock_calls]
        assert names == ["execute", "drop_index", "drop_constraint", "drop_column"], names

    def test_drop_statements_target_the_0289_objects(self) -> None:
        op = _run("downgrade")
        trigger_stmt = str(op.execute.call_args.args[0])
        assert "DROP TRIGGER IF EXISTS" in trigger_stmt, trigger_stmt
        assert _TRIGGER_NAME in trigger_stmt, trigger_stmt
        assert f"ON public.{_TABLE}" in trigger_stmt, trigger_stmt
        op.drop_index.assert_called_once_with(_INDEX_NAME, table_name=_TABLE)
        op.drop_constraint.assert_called_once_with(_FK_NAME, _TABLE, type_="foreignkey")
        op.drop_column.assert_called_once_with(_TABLE, _COLUMN)


class TestModelParity:
    def test_model_declares_the_nullable_column_with_a_set_null_fk(self) -> None:
        assert _COLUMN in Pipeline.__table__.columns, "Pipeline must declare environment_profile_id"
        column = Pipeline.__table__.c[_COLUMN]
        assert column.nullable is True, column.nullable
        assert isinstance(column.type, sa.Uuid), type(column.type)
        fks = list(column.foreign_keys)
        assert len(fks) == 1, f"expected exactly one FK, got {len(fks)}"
        assert fks[0].target_fullname == f"{_REFERENCED_TABLE}.id", fks[0].target_fullname
        assert fks[0].ondelete == "SET NULL", fks[0].ondelete

    def test_model_declares_the_same_index_name(self) -> None:
        index = next((i for i in Pipeline.__table__.indexes if i.name == _INDEX_NAME), None)
        assert index is not None, f"Pipeline must declare {_INDEX_NAME}"
        assert [col.name for col in index.columns] == [_COLUMN]

    def test_migration_and_model_reference_the_same_table_and_column(self) -> None:
        """Guards a rename landing on one side only (parity would diverge later)."""
        source = _source()
        assert f'"{_COLUMN}"' in source, source
        assert f'"{_REFERENCED_TABLE}"' in source, source
