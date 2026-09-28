"""0264: ``pipelines.max_autonomy_level >= default_autonomy_level`` at the DB layer.

Migration 0256/0259 guarded the VOCABULARY of both autonomy columns but not
their RELATIVE ORDER - the ``ceiling >= default`` invariant lived only in the
app-layer PATCH lock, so a race could still commit an inverted row. 0264
repairs existing inverted rows (behaviour-preserving: resolution already
computed ``base = min(default, ceiling) = ceiling`` for them) and then adds
the composite CHECK.

Lenses:

* **Chain** - 0264 chains onto ``0263_evidence_layer`` as the single linear
  head (0259's test documents the run-up through 0263).
* **Structure (mocked ``op``)** - upgrade emits THREE statements IN ORDER: the
  behaviour-preserving ``UPDATE`` repair FIRST (the CHECK cannot be added
  while an inverted row exists), then the existence-gated ``ADD ... NOT
  VALID``, then the ``VALIDATE``. Downgrade is the reconciliation-chain no-op.
* **Repair semantics** - the WHERE clause selects exactly ``ceiling IS NOT
  NULL AND rank(default) > rank(ceiling)`` (the NULL arms never match) and
  lowers the DEFAULT onto the ceiling (never the other way round - raising the
  ceiling would change behaviour).
* **Model parity** - ``Pipeline.__table_args__`` declares the same constraint
  name with the byte-identical predicate, so ``test_initial_migration``'s
  ORM-vs-migrated-DB parity check cannot diverge.
* **Docstring loudness** - the migration must say out loud that it mutates
  data.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path
from types import ModuleType
from unittest.mock import patch

import pytest
from alembic.script import ScriptDirectory
from sqlalchemy import CheckConstraint

from modulo.db.models.pipeline import Pipeline

_MIGRATION_REVISION = "0264_pipelines_max_autonomy_ge_default"
_MIGRATION_DOWN_REVISION = "0263_evidence_layer"
_HEAD_MIGRATION = "0264_pipelines_max_autonomy_ge_default"
_CONSTRAINT = "ck_pipelines_max_autonomy_ge_default"
_VOCABULARY = ("manual_approval", "notify_on_complete", "fully_autonomous")
#: The existence gates must name the TABLE, not just the constraint - a
#: same-named constraint on another table must not satisfy the gate.
_REGCLASS = "'public.pipelines'::regclass"

_VERSIONS = Path(__file__).resolve().parents[3] / "src" / "modulo" / "db" / "migrations" / "versions"
_MIGRATION_PATH = _VERSIONS / f"{_MIGRATION_REVISION}.py"

#: Both statements rank the SAME vocabulary through the SAME CASE shape - only
#: the comparison operator differs (repair ``>`` == the check's ``<=``).
_REPAIR_RANK_DEFAULT = (
    "(CASE default_autonomy_level "
    "WHEN 'manual_approval' THEN 0 "
    "WHEN 'notify_on_complete' THEN 1 "
    "WHEN 'fully_autonomous' THEN 2 END)"
)
_REPAIR_RANK_CEILING = (
    "(CASE max_autonomy_level "
    "WHEN 'manual_approval' THEN 0 "
    "WHEN 'notify_on_complete' THEN 1 "
    "WHEN 'fully_autonomous' THEN 2 END)"
)


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
    def test_single_head_is_0264(self) -> None:
        heads = ScriptDirectory(str(_VERSIONS.parent)).get_heads()
        assert heads == [_HEAD_MIGRATION], f"expected a single head, got {heads}"

    def test_down_revision_is_0263(self) -> None:
        module = _load_migration()
        assert module.down_revision == _MIGRATION_DOWN_REVISION

    def test_revision_id_matches_filename(self) -> None:
        module = _load_migration()
        assert module.revision == _MIGRATION_REVISION


class TestUpgrade:
    def test_emits_repair_then_add_then_validate_in_order(self) -> None:
        executed = _executed()
        assert len(executed) == 3, f"expected repair + add-NOT-VALID + VALIDATE, got {len(executed)} statements"
        repair, add_ddl, validate_ddl = executed

        # 1. The DATA-MUTATING repair must come first - a pre-existing
        #    inverted row would make the ADD+VALIDATE fail the scan.
        assert repair.startswith("UPDATE pipelines SET default_autonomy_level = max_autonomy_level"), repair
        # 2. The existence-gated ADD NOT VALID.
        assert f"conname='{_CONSTRAINT}'" in add_ddl, add_ddl
        assert "IF NOT EXISTS (SELECT 1 FROM pg_constraint" in add_ddl, add_ddl
        assert f"conrelid = {_REGCLASS}" in add_ddl, add_ddl
        assert "NOT VALID;" in add_ddl, add_ddl
        assert "ALTER TABLE public.pipelines ADD CONSTRAINT" in add_ddl, add_ddl
        # 3. The VALIDATE.
        assert f"conname='{_CONSTRAINT}'" in validate_ddl, validate_ddl
        assert f"conrelid = {_REGCLASS}" in validate_ddl, validate_ddl
        assert "NOT convalidated" in validate_ddl, validate_ddl
        assert "VALIDATE CONSTRAINT" in validate_ddl, validate_ddl

    def test_repair_selects_exactly_the_inverted_rows(self) -> None:
        """``ceiling IS NOT NULL`` AND ``rank(default) > rank(ceiling)``.

        A NULL ceiling is always valid (the first CHECK arm), a NULL default
        ranks NULL so ``NULL > x`` never matches - neither may be touched, and
        the DEFAULT is what moves (onto the ceiling), never the ceiling.
        """
        repair = _executed()[0]
        assert "SET default_autonomy_level = max_autonomy_level" in repair, repair
        assert "WHERE max_autonomy_level IS NOT NULL" in repair, repair
        assert f"AND {_REPAIR_RANK_DEFAULT} > {_REPAIR_RANK_CEILING}" in repair, repair
        # The ceiling is the ASSIGNMENT SOURCE, never the update target.
        assert "SET max_autonomy_level" not in repair, repair

    def test_existence_gates_are_table_qualified(self) -> None:
        """conname alone would let a same-named constraint elsewhere skip the gate."""
        executed = _executed()
        for ddl in executed[1:]:
            assert f"conname='{_CONSTRAINT}'" in ddl, ddl
            assert f"conrelid = {_REGCLASS}" in ddl, ddl
            # Exactly one pg_constraint lookup per DO block, and every lookup
            # carries the table qualification.
            assert ddl.count("pg_constraint") == 1, ddl
            assert ddl.count("conrelid") == 1, ddl

    def test_check_predicate_is_the_ranked_ceiling_ge_default(self) -> None:
        """``max IS NULL OR rank(default) <= rank(ceiling)`` over full vocabulary."""
        add_ddl = _executed()[1]
        assert "max_autonomy_level IS NULL OR " in add_ddl, add_ddl
        assert f"{_REPAIR_RANK_DEFAULT} <= {_REPAIR_RANK_CEILING}" in add_ddl, add_ddl
        for value in _VOCABULARY:
            assert f"'{value}'" in add_ddl, f"0264 CHECK vocabulary missing {value!r}"

    def test_no_string_formatted_ddl(self) -> None:
        """S608 / migration-fstring-sql: the DDL must be literal, not f-string."""
        source = _source()
        assert "op.execute(f" not in source, source
        assert "text(f" not in source, source

    def test_docstring_says_out_loud_that_it_mutates_data(self) -> None:
        """A data-mutating migration must announce itself in its docstring."""
        assert "DATA-MUTATING" in _source()
        assert "Behaviour-preserving" in _source()


class TestDowngrade:
    def test_downgrade_is_noop(self) -> None:
        module = _load_migration()
        with patch.object(module, "op") as op:
            module.downgrade()
        op.execute.assert_not_called()
        assert "DROP CONSTRAINT" not in _source()


def _model_check() -> CheckConstraint:
    checks = [c for c in Pipeline.__table_args__ if isinstance(c, CheckConstraint)]
    match = next((c for c in checks if c.name == _CONSTRAINT), None)
    assert match is not None, f"Pipeline.__table_args__ missing {_CONSTRAINT}"
    return match


class TestModelParity:
    def test_model_declares_the_constraint_with_the_same_predicate(self) -> None:
        sqltext = str(_model_check().sqltext)
        assert "max_autonomy_level IS NULL OR" in sqltext, sqltext
        assert f"{_REPAIR_RANK_DEFAULT} <= {_REPAIR_RANK_CEILING}" in sqltext, sqltext
        for value in _VOCABULARY:
            assert f"'{value}'" in sqltext, f"model CHECK vocabulary missing {value!r}"

    def test_migration_predicate_is_byte_identical_to_the_model(self) -> None:
        """The migration's CHECK body and the model's sqltext must not drift.

        The migrated-DB parity suite compares PRESENCE; this pins the SHAPE,
        so a one-sided edit to either side fails here instead of in prod.
        """
        add_ddl = _executed()[1]
        predicate = add_ddl.split("CHECK (", 1)[1].split(") NOT VALID;", 1)[0]
        assert predicate == str(_model_check().sqltext), (predicate, str(_model_check().sqltext))

    def test_migration_and_model_declare_the_same_name(self) -> None:
        declared = {c.name for c in Pipeline.__table_args__ if isinstance(c, CheckConstraint) and c.name is not None}
        assert _CONSTRAINT in declared, "model missing the constraint"
        assert _CONSTRAINT in _source(), "migration missing the constraint"

    @pytest.mark.parametrize("vocabulary_value", _VOCABULARY)
    def test_every_vocabulary_value_is_ranked_on_both_sides(self, vocabulary_value: str) -> None:
        """Both CASE arms must rank every value - an unranked value yields NULL."""
        add_ddl = _executed()[1]
        assert add_ddl.count(f"'{vocabulary_value}'") == 2, add_ddl
