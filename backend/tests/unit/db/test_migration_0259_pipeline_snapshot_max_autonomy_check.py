"""FAR-1223: migration 0259 - CHECK on ``pipeline_snapshots.max_autonomy_level``.

Migration 0256 added the ceiling column to BOTH ``pipelines`` and
``pipeline_snapshots`` but only guarded the ``pipelines`` column with the
vocabulary CHECK. 0259 closes the gap.

Lenses:

* **Chain** - 0259 chains onto 0258 and is the single linear head.
* **Structure (mocked ``op``)** - upgrade emits BOTH existence-gated DO blocks
  (add NOT VALID, then VALIDATE) carrying the full vocabulary; downgrade is the
  reconciliation-chain no-op (no DROP).
* **Model parity** - ``PipelineSnapshot.__table_args__`` declares the SAME
  constraint name and vocabulary, so ``test_initial_migration``'s
  ORM-vs-migrated-DB parity check cannot diverge.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path
from types import ModuleType
from unittest.mock import patch

from alembic.script import ScriptDirectory
from sqlalchemy import CheckConstraint

from modulo.db.models.pipeline_snapshot import PipelineSnapshot

_MIGRATION_REVISION = "0259_pipeline_snapshot_max_autonomy_check"
_MIGRATION_DOWN_REVISION = "0258_pipeline_accountability_owners"
_CONSTRAINT_NAME = "ck_pipeline_snapshots_max_autonomy_level"
_VOCABULARY = ("manual_approval", "notify_on_complete", "fully_autonomous")

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


class TestChain:
    def test_single_head_is_0259(self) -> None:
        heads = ScriptDirectory(str(_VERSIONS.parent)).get_heads()
        assert heads == [_MIGRATION_REVISION], f"expected a single head, got {heads}"

    def test_down_revision_is_0258(self) -> None:
        module = _load_migration()
        assert module.down_revision == _MIGRATION_DOWN_REVISION

    def test_revision_id_matches_filename(self) -> None:
        module = _load_migration()
        assert module.revision == _MIGRATION_REVISION


class TestUpgrade:
    def test_emits_existence_gated_add_then_validate(self) -> None:
        module = _load_migration()
        with patch.object(module, "op") as op:
            module.upgrade()
        executed = [call.args[0] for call in op.execute.call_args_list]
        assert len(executed) == 2, f"expected add-NOT-VALID + VALIDATE, got {len(executed)} execute() calls"
        add_ddl, validate_ddl = executed
        assert f"conname='{_CONSTRAINT_NAME}'" in add_ddl
        assert "IF NOT EXISTS (SELECT 1 FROM pg_constraint" in add_ddl
        assert "NOT VALID;" in add_ddl
        assert "ALTER TABLE public.pipeline_snapshots ADD CONSTRAINT" in add_ddl
        assert f"conname='{_CONSTRAINT_NAME}'" in validate_ddl
        assert "NOT convalidated" in validate_ddl
        assert "VALIDATE CONSTRAINT" in validate_ddl

    def test_covers_null_and_full_vocabulary(self) -> None:
        source = _source()
        assert "max_autonomy_level IS NULL OR" in source
        for value in _VOCABULARY:
            assert f"'{value}'" in source, f"0259 CHECK vocabulary missing {value!r}"

    def test_guards_the_snapshot_table(self) -> None:
        source = _source()
        assert "ALTER TABLE public.pipeline_snapshots ADD CONSTRAINT" in source


class TestDowngrade:
    def test_downgrade_is_noop(self) -> None:
        module = _load_migration()
        with patch.object(module, "op") as op:
            module.downgrade()
        op.execute.assert_not_called()
        assert "DROP CONSTRAINT" not in _source()


class TestModelParity:
    def test_pipeline_snapshot_declares_the_same_constraint(self) -> None:
        checks = [c for c in PipelineSnapshot.__table_args__ if isinstance(c, CheckConstraint)]
        match = next((c for c in checks if c.name == _CONSTRAINT_NAME), None)
        assert match is not None, f"PipelineSnapshot.__table_args__ missing {_CONSTRAINT_NAME}"
        sqltext = str(match.sqltext)
        assert "max_autonomy_level IS NULL OR" in sqltext
        for value in _VOCABULARY:
            assert f"'{value}'" in sqltext, f"model CHECK vocabulary missing {value!r}"
