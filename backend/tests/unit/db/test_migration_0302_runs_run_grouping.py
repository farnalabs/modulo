"""Unit tests for migration 0302_runs_run_grouping (FAR-153 / §15.1).

Structural — load the migration module and pin its contract without a
database, plus ORM parity for the two new columns and the two partial
unique indexes that replace the global thread unique:

1. the chain: 0302 revises 0301_oauth_consent_state_preauth_rls, the
   migrations directory has exactly one head (0311_policy_gate_evaluation_mode), the
   revision id matches the filename, and there are no branch labels;
2. the upgrade adds EXACTLY two nullable columns with NO server default
   (metadata-only on the hot ``runs`` table), creates the ``run_group_id``
   index, DROPS ``runs_langgraph_thread_id_key``, and creates the two
   partial unique indexes with the §15.1 predicates byte-identical to the
   ``Run`` model's declarations;
3. the downgrade reverses the DDL and restores the global unique ONLY when
   no thread is shared — refusing loudly (RuntimeError, data intact) when a
   resume child still shares its parent's thread;
4. model/migration parity: ``Run.run_group_id`` / ``Run.run_group_kind``
   match the columns the migration creates, ``langgraph_thread_id`` no
   longer declares ``unique=True``, both partial indexes are declared on
   the model with the same names / unique flags / dialect predicates, and
   the closed ``RUN_GROUP_KIND_VALUES`` vocabulary is exactly
   ``{'retry', 'resume'}``.

They run without a database.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path
from types import ModuleType
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from alembic.script import ScriptDirectory
from sqlalchemy import String, UniqueConstraint, Uuid

from modulo.db.models.run import RUN_GROUP_KIND_RESUME, RUN_GROUP_KIND_RETRY, RUN_GROUP_KIND_VALUES, Run

_VERSIONS = Path(__file__).resolve().parents[3] / "src" / "modulo" / "db" / "migrations" / "versions"
_MIGRATION_NAME = "0302_runs_run_grouping"
_MIGRATION_PATH = _VERSIONS / f"{_MIGRATION_NAME}.py"
_DOWN_REVISION = "0301_oauth_consent_state_preauth_rls"
_CHAIN_HEAD_MIGRATION = "0311_policy_gate_evaluation_mode"

#: The constraint 0003/0110 put on the global thread id — dropped by the upgrade.
_THREAD_CONSTRAINT = "runs_langgraph_thread_id_key"

#: Predicates, byte-identical to the ``Run`` model ``__table_args__`` (the
#: parity between the two is what keeps ``test_initial_migration`` green on
#: the real migrated schema).
_RESUME_PARENT_PG = "run_group_kind = 'resume'"
_RESUME_PARENT_SQLITE = "run_group_kind = 'resume'"
_THREAD_NON_RESUME_PG = "run_group_kind IS DISTINCT FROM 'resume'"
_THREAD_NON_RESUME_SQLITE = "run_group_kind IS NOT 'resume'"


def _load_migration() -> ModuleType:
    assert _MIGRATION_PATH.exists(), f"Migration file missing: {_MIGRATION_PATH}"
    spec = importlib.util.spec_from_file_location(f"migration_{_MIGRATION_NAME}", _MIGRATION_PATH)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _migration_calls(entry_point: str, *, bind_scalar: int = 0) -> list[tuple[str, tuple[Any, ...], dict[str, Any]]]:
    """Every op.* call (plus ``get_bind``) the entry point makes, in order."""
    module = _load_migration()
    calls: list[tuple[str, tuple[Any, ...], dict[str, Any]]] = []

    def _record(name: str):
        def _side_effect(*args: Any, **kwargs: Any) -> Any:
            calls.append((name, args, kwargs))
            if name == "get_bind":
                bind = MagicMock()
                bind.execute.return_value.scalar.return_value = bind_scalar
                return bind
            return MagicMock()

        return _side_effect

    mock_op = MagicMock()
    for name in (
        "add_column",
        "create_index",
        "drop_constraint",
        "drop_index",
        "drop_column",
        "create_unique_constraint",
        "get_bind",
    ):
        getattr(mock_op, name).side_effect = _record(name)

    with patch.object(module, "op", mock_op):
        getattr(module, entry_point)()
    return calls


def _where_text(clause: Any) -> str:
    return str(clause.text) if hasattr(clause, "text") else str(clause)


class TestChain:
    def test_single_head_is_0302_runs_run_grouping(self) -> None:
        heads = ScriptDirectory(str(_VERSIONS.parent)).get_heads()
        assert heads == [_CHAIN_HEAD_MIGRATION], f"expected a single head, got {heads}"

    def test_down_revision_is_0301_oauth_consent_state_preauth_rls(self) -> None:
        assert _load_migration().down_revision == _DOWN_REVISION

    def test_revision_id_matches_filename(self) -> None:
        assert _load_migration().revision == _MIGRATION_NAME

    def test_no_branch_labels_or_depends_on(self) -> None:
        module = _load_migration()
        assert module.branch_labels is None
        assert module.depends_on is None


class TestUpgrade:
    def test_adds_exactly_the_two_expected_columns(self) -> None:
        calls = [c for c in _migration_calls("upgrade") if c[0] == "add_column"]
        assert len(calls) == 2, f"expected exactly two add_column calls, got {calls}"
        actual = tuple((args[0], args[1].name) for _name, args, _kwargs in calls)
        assert actual == (("runs", "run_group_id"), ("runs", "run_group_kind"))

    def test_columns_are_nullable_without_server_default(self) -> None:
        """Metadata-only ADD COLUMN: nullable, no server default (so Postgres
        does not rewrite the hot ``runs`` table)."""
        for name, args, _kwargs in _migration_calls("upgrade"):
            if name != "add_column" or args[1].name not in ("run_group_id", "run_group_kind"):
                continue
            column = args[1]
            assert column.nullable is True, f"{args[0]}.{column.name} must be nullable"
            assert column.server_default is None, f"{args[0]}.{column.name} must have no server default"

    def test_column_types_match_the_model(self) -> None:
        columns = {args[1].name: args[1] for _n, args, _k in _migration_calls("upgrade") if _n == "add_column"}
        assert isinstance(columns["run_group_id"].type, Uuid)
        assert isinstance(columns["run_group_kind"].type, String)
        assert columns["run_group_kind"].type.length == 16

    def test_drops_the_global_thread_unique_constraint(self) -> None:
        drops = [c for c in _migration_calls("upgrade") if c[0] == "drop_constraint"]
        assert len(drops) == 1, f"expected exactly one drop_constraint call, got {drops}"
        _name, args, kwargs = drops[0]
        assert args == (_THREAD_CONSTRAINT, "runs"), args
        assert kwargs.get("type_") == "unique", kwargs

    def test_creates_the_run_group_id_index(self) -> None:
        creates = [
            (args, kwargs)
            for _n, args, kwargs in _migration_calls("upgrade")
            if _n == "create_index" and args and args[0] == "ix_runs_run_group_id"
        ]
        assert len(creates) == 1, f"expected ix_runs_run_group_id, got {creates}"
        args, kwargs = creates[0]
        assert args == ("ix_runs_run_group_id", "runs", ["run_group_id"]), args
        assert kwargs.get("unique") is not True

    def test_creates_the_resume_parent_partial_unique_with_model_predicate(self) -> None:
        creates = [
            (args, kwargs)
            for _n, args, kwargs in _migration_calls("upgrade")
            if _n == "create_index" and args and args[0] == "uq_runs_resume_parent"
        ]
        assert len(creates) == 1, f"expected uq_runs_resume_parent, got {creates}"
        args, kwargs = creates[0]
        assert args == ("uq_runs_resume_parent", "runs", ["parent_run_id"]), args
        assert kwargs.get("unique") is True
        assert _where_text(kwargs["postgresql_where"]) == _RESUME_PARENT_PG
        assert _where_text(kwargs["sqlite_where"]) == _RESUME_PARENT_SQLITE

    def test_creates_the_thread_non_resume_partial_unique_with_model_predicate(self) -> None:
        creates = [
            (args, kwargs)
            for _n, args, kwargs in _migration_calls("upgrade")
            if _n == "create_index" and args and args[0] == "uq_runs_thread_non_resume"
        ]
        assert len(creates) == 1, f"expected uq_runs_thread_non_resume, got {creates}"
        args, kwargs = creates[0]
        assert args == ("uq_runs_thread_non_resume", "runs", ["langgraph_thread_id"]), args
        assert kwargs.get("unique") is True
        assert _where_text(kwargs["postgresql_where"]) == _THREAD_NON_RESUME_PG
        assert _where_text(kwargs["sqlite_where"]) == _THREAD_NON_RESUME_SQLITE

    def test_upgrade_drops_the_constraint_before_creating_the_partial_uniques(self) -> None:
        """Ordering narrative: the constraint drop and the two replacement
        indexes are all inside one transaction (env.py wraps the revision),
        but the recorded order must match the documented one."""
        names = [c[0] for c in _migration_calls("upgrade")]
        drop_at = names.index("drop_constraint")
        partial_indexes = [i for i, n in enumerate(names) if n == "create_index" and i > drop_at]
        assert len(partial_indexes) == 2, f"expected both partial uniques after the drop, got {names}"


class TestDowngrade:
    def test_drops_the_indexes_and_columns_then_restores_the_constraint(self) -> None:
        calls = _migration_calls("downgrade", bind_scalar=0)
        names = [c[0] for c in calls]
        # indexes and columns go first (the partial predicates reference
        # run_group_kind, so the indexes must fall before the column) ...
        assert names.index("drop_index") < names.index("drop_column")
        assert names.index("drop_column") < names.index("create_unique_constraint")
        dropped_indexes = {c[1][0] for c in calls if c[0] == "drop_index"}
        assert dropped_indexes == {"uq_runs_thread_non_resume", "uq_runs_resume_parent", "ix_runs_run_group_id"}
        dropped_columns = {c[1][1] for c in calls if c[0] == "drop_column"}
        assert dropped_columns == {"run_group_kind", "run_group_id"}
        restored = [c for c in calls if c[0] == "create_unique_constraint"]
        assert restored[0][1] == (_THREAD_CONSTRAINT, "runs", ["langgraph_thread_id"]), restored

    def test_refuses_to_restore_the_global_unique_when_threads_are_shared(self) -> None:
        """A resume child shares its parent's thread — restoring the global
        unique would either fail cryptically or (worse) tempt a destructive
        de-dup. The downgrade refuses with an actionable error instead (the
        0151/0298 downgrade-refusal precedent); data stays intact."""
        with pytest.raises(RuntimeError, match="shared by multiple runs"):
            _migration_calls("downgrade", bind_scalar=2)

    def test_shared_thread_guard_runs_before_the_constraint_return(self) -> None:
        calls = [c[0] for c in _migration_calls("downgrade", bind_scalar=0)]
        assert "get_bind" in calls
        assert calls.index("get_bind") < calls.index("create_unique_constraint")


class TestModelParity:
    def test_run_group_id_column_matches_the_migration(self) -> None:
        column = Run.__table__.c.run_group_id
        assert isinstance(column.type, Uuid)
        assert column.nullable is True
        assert column.index is True  # ix_runs_run_group_id
        index_names = {ix.name for ix in Run.__table__.indexes}
        assert "ix_runs_run_group_id" in index_names

    def test_run_group_kind_column_matches_the_migration(self) -> None:
        column = Run.__table__.c.run_group_kind
        assert isinstance(column.type, String)
        assert column.type.length == 16
        assert column.nullable is True

    def test_langgraph_thread_id_is_no_longer_globally_unique(self) -> None:
        """§15.1: the global UNIQUE(langgraph_thread_id) is replaced by the
        two partial uniques below — a resume child MUST share its parent's
        thread to continue the checkpoint."""
        assert not Run.__table__.c.langgraph_thread_id.unique
        constrained = {
            col.name
            for constraint in Run.__table__.constraints
            if isinstance(constraint, UniqueConstraint)
            for col in constraint.columns
        }
        assert "langgraph_thread_id" not in constrained

    def _index(self, name: str):
        matches = [ix for ix in Run.__table__.indexes if ix.name == name]
        assert len(matches) == 1, f"expected exactly one index {name}, got {matches}"
        return matches[0]

    def test_resume_parent_partial_unique_declared_on_the_model(self) -> None:
        index = self._index("uq_runs_resume_parent")
        assert index.unique is True
        assert [c.name for c in index.columns] == ["parent_run_id"]
        assert str(index.dialect_options["postgresql"]["where"].text) == _RESUME_PARENT_PG
        assert str(index.dialect_options["sqlite"]["where"].text) == _RESUME_PARENT_SQLITE

    def test_thread_non_resume_partial_unique_declared_on_the_model(self) -> None:
        index = self._index("uq_runs_thread_non_resume")
        assert index.unique is True
        assert [c.name for c in index.columns] == ["langgraph_thread_id"]
        assert str(index.dialect_options["postgresql"]["where"].text) == _THREAD_NON_RESUME_PG
        assert str(index.dialect_options["sqlite"]["where"].text) == _THREAD_NON_RESUME_SQLITE

    def test_run_group_kind_vocabulary_is_the_closed_retry_resume_set(self) -> None:
        assert frozenset({RUN_GROUP_KIND_RETRY, RUN_GROUP_KIND_RESUME}) == RUN_GROUP_KIND_VALUES
        assert RUN_GROUP_KIND_RETRY == "retry"
        assert RUN_GROUP_KIND_RESUME == "resume"
