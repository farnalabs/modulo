"""FAR-1223: migration 0259 - the ``pipeline_snapshots`` autonomy CHECKs.

Migration 0256 added ``max_autonomy_level`` to BOTH ``pipelines`` and
``pipeline_snapshots`` but only guarded the ``pipelines`` column with the
vocabulary CHECK; ``pipeline_snapshots.default_autonomy_level`` has been
unguarded since 0110. 0259 closes both gaps.

Lenses:

* **Chain** - 0259 chains onto 0258; 0260_run_cancel_reason (FAR-1233) chains
  onto 0259 and is the single linear head.
* **Structure (mocked ``op``)** - upgrade emits FOUR existence-gated DO blocks
  (add NOT VALID, then VALIDATE, for each of the two columns) carrying the full
  vocabulary and a TABLE-QUALIFIED ``conrelid`` gate; downgrade is the
  reconciliation-chain no-op (no DROP).
* **Model parity** - ``PipelineSnapshot.__table_args__`` declares BOTH
  constraint names with the same vocabulary and NULL semantics, so
  ``test_initial_migration``'s ORM-vs-migrated-DB parity check cannot diverge.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path
from types import ModuleType
from unittest.mock import patch

import pytest
from alembic.script import ScriptDirectory
from sqlalchemy import CheckConstraint

from modulo.db.models.pipeline_snapshot import PipelineSnapshot

_MIGRATION_REVISION = "0259_pipeline_snapshot_max_autonomy_check"
_MIGRATION_DOWN_REVISION = "0258_pipeline_accountability_owners"
_MAX_CEILING_CONSTRAINT = "ck_pipeline_snapshots_max_autonomy_level"
_DEFAULT_LEVEL_CONSTRAINT = "ck_pipeline_snapshots_default_autonomy_level"
_CONSTRAINTS = (_MAX_CEILING_CONSTRAINT, _DEFAULT_LEVEL_CONSTRAINT)
_VOCABULARY = ("manual_approval", "notify_on_complete", "fully_autonomous")
#: The existence gates must name the TABLE, not just the constraint — a
#: same-named constraint on another table must not satisfy the gate.
_REGCLASS = "'public.pipeline_snapshots'::regclass"

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


def _executed() -> list[str]:
    module = _load_migration()
    with patch.object(module, "op") as op:
        module.upgrade()
    return [call.args[0] for call in op.execute.call_args_list]


class TestChain:
    def test_single_head_is_0260(self) -> None:
        heads = ScriptDirectory(str(_VERSIONS.parent)).get_heads()
        assert heads == ["0260_run_cancel_reason"], f"expected a single head, got {heads}"

    def test_down_revision_is_0258(self) -> None:
        module = _load_migration()
        assert module.down_revision == _MIGRATION_DOWN_REVISION

    def test_revision_id_matches_filename(self) -> None:
        module = _load_migration()
        assert module.revision == _MIGRATION_REVISION


class TestUpgrade:
    def test_emits_add_then_validate_for_both_columns(self) -> None:
        executed = _executed()
        assert len(executed) == 4, (
            f"expected add-NOT-VALID + VALIDATE for EACH of the two columns, got {len(executed)} execute() calls"
        )
        adds = executed[:2]
        validates = executed[2:]

        for constraint, add_ddl in zip(_CONSTRAINTS, adds, strict=True):
            assert f"conname='{constraint}'" in add_ddl, add_ddl
            assert "IF NOT EXISTS (SELECT 1 FROM pg_constraint" in add_ddl, add_ddl
            assert f"conrelid = {_REGCLASS}" in add_ddl, add_ddl
            assert "NOT VALID;" in add_ddl, add_ddl
            assert "ALTER TABLE public.pipeline_snapshots ADD CONSTRAINT" in add_ddl, add_ddl

        for constraint, validate_ddl in zip(_CONSTRAINTS, validates, strict=True):
            assert f"conname='{constraint}'" in validate_ddl, validate_ddl
            assert f"conrelid = {_REGCLASS}" in validate_ddl, validate_ddl
            assert "NOT convalidated" in validate_ddl, validate_ddl
            assert "VALIDATE CONSTRAINT" in validate_ddl, validate_ddl

    def test_existence_gates_are_table_qualified(self) -> None:
        """FIX E: conname alone would let a same-named constraint elsewhere skip the gate."""
        executed = _executed()
        for ddl in executed:
            assert "conname='ck_pipeline_snapshots_" in ddl, ddl
            assert f"conrelid = {_REGCLASS}" in ddl, ddl
            # Exactly one pg_constraint lookup per DO block, and every lookup
            # carries the table qualification.
            assert ddl.count("pg_constraint") == 1, ddl
            assert ddl.count("conrelid") == 1, ddl

    def test_covers_null_and_full_vocabulary_for_both_columns(self) -> None:
        executed = _executed()
        joined = "\n".join(executed)
        assert "max_autonomy_level IS NULL OR" in joined
        assert "default_autonomy_level IS NULL OR" in joined
        for value in _VOCABULARY:
            assert f"'{value}'" in joined, f"0259 CHECK vocabulary missing {value!r}"

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


def _model_check(name: str) -> CheckConstraint:
    checks = [c for c in PipelineSnapshot.__table_args__ if isinstance(c, CheckConstraint)]
    match = next((c for c in checks if c.name == name), None)
    assert match is not None, f"PipelineSnapshot.__table_args__ missing {name}"
    return match


class TestModelParity:
    @pytest.mark.parametrize("constraint", _CONSTRAINTS)
    def test_pipeline_snapshot_declares_the_same_constraint(self, constraint: str) -> None:
        column = constraint.removeprefix("ck_pipeline_snapshots_")
        sqltext = str(_model_check(constraint).sqltext)
        assert f"{column} IS NULL OR" in sqltext, sqltext
        for value in _VOCABULARY:
            assert f"'{value}'" in sqltext, f"model CHECK vocabulary missing {value!r}"
        assert f"{column} IN (" in sqltext, sqltext

    def test_migration_and_model_declare_the_same_pair(self) -> None:
        declared = {
            c.name for c in PipelineSnapshot.__table_args__ if isinstance(c, CheckConstraint) and c.name is not None
        }
        source = _source()
        for constraint in _CONSTRAINTS:
            assert constraint in declared, f"model missing {constraint}"
            assert constraint in source, f"migration missing {constraint}"
