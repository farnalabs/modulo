"""Unit tests for migration 0329_run_attempts (FAR-2078 storage slice).

Structural contract (no Postgres / Testcontainers needed). Sibling migrations
pin their own shape with a dedicated test; this pins the four things a reviewer
needs to trust the migration without a live DB:

* **Revision chain** — this migration's parent is pinned; the graph's single
  linear head and completeness are asserted by
  ``_migration_graph.assert_single_linear_head``; the revision id matches the
  filename; neither ``branch_labels`` nor ``depends_on`` is set (the
  check-migration-heads hook and every head assertion depend on that shape).
* **Org-scoped RLS coverage** — ``_ORG_SCOPED_TABLES`` names the new table, so
  ``test_rls_coverage.py`` sees it without parsing the DDL.
* **Upgrade (mocked ``op``)** — on Postgres it runs the new-table ceremony
  (``SET ROLE modulo_migrate`` -> create -> ownership assertion -> ENABLE/FORCE
  ROW LEVEL SECURITY + the verbatim ``rls_org_isolation`` policy -> DML grants);
  on SQLite it creates the table and index with no ceremony. The column set,
  both CASCADE foreign keys, the ``(run_id, attempt_index)`` unique constraint
  and the two CHECKs are pinned exactly.
* **Downgrade** — drops the policy, disables RLS, drops the index and the table.
* **Model parity** — the ORM model carries the same columns, constraints,
  foreign-key actions and index the migration writes, and the terminal-reason
  vocabulary has ONE spelling across the model and the migration.

The migration itself is deliberately NOT executed against a live Postgres here
(the host has no Docker/testcontainers). Real-Postgres application is CI-only.
"""

from __future__ import annotations

import importlib.util
import re
from pathlib import Path
from types import ModuleType
from unittest.mock import MagicMock, patch

import sqlalchemy as sa

from modulo.db.models.run_attempt import (
    ATTEMPT_TERMINAL_REASON_SQL,
    ATTEMPT_TERMINAL_REASON_VALUES,
    RunAttempt,
)
from tests.unit.db._migration_graph import assert_single_linear_head, script_directory

_VERSIONS = Path(__file__).resolve().parents[3] / "src" / "modulo" / "db" / "migrations" / "versions"
_MIGRATION_NAME = "0329_run_attempts"
_MIGRATION_PATH = _VERSIONS / f"{_MIGRATION_NAME}.py"
_DOWN_REVISION = "0328_evidence_index_aggregate_indexes"

_TABLE = "run_attempts"
_ORG_INDEX = "ix_run_attempts_organisation_id"
_UNIQUE = "uq_run_attempts_run_attempt_index"

_COLUMNS = {
    "id",
    "organisation_id",
    "run_id",
    "attempt_index",
    "started_at",
    "ended_at",
    "terminal_reason",
    "model_used",
    "error_code",
    "created_at",
    "updated_at",
}

_CHECKS = {
    "ck_run_attempts_attempt_index_non_negative": "attempt_index >= 0",
}


def _load_migration() -> ModuleType:
    assert _MIGRATION_PATH.exists(), f"Migration file missing: {_MIGRATION_PATH}"
    spec = importlib.util.spec_from_file_location(f"migration_{_MIGRATION_NAME}", _MIGRATION_PATH)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class _Recorder:
    """Captures op.execute SQL, table DDL fragments, indexes and drops."""

    def __init__(self) -> None:
        self.sql: list[str] = []
        self.tables: list[str] = []
        self.columns: dict[str, set[str]] = {}
        self.checks: dict[str, dict[str, str]] = {}
        self.uniques: dict[str, dict[str, tuple[str, ...]]] = {}
        self.foreign_keys: dict[str, set[tuple[str, str, str | None]]] = {}
        self.indexes: list[tuple[str, str, list[str]]] = []

    def execute(self, stmt: object, *_args: object, **_kwargs: object) -> MagicMock:
        self.sql.append(str(stmt))
        return MagicMock()

    def create_table(self, name: str, *args: object, **_kwargs: object) -> None:
        self.tables.append(name)
        # ``op.create_table`` receives UNBOUND constraints, whose ``.columns``
        # collection is empty until they are attached to a Table. Bind them to
        # a throwaway Table (with stub parent tables, so the foreign keys
        # resolve) so the assertions read exactly what a real database would
        # receive.
        metadata = sa.MetaData()
        sa.Table("organisations", metadata, sa.Column("id", sa.Uuid(), primary_key=True))
        sa.Table("runs", metadata, sa.Column("id", sa.Uuid(), primary_key=True))
        table = sa.Table(name, metadata, *args)  # type: ignore[arg-type]
        self.columns[name] = {column.name for column in table.columns}
        self.checks[name] = {
            constraint.name: str(constraint.sqltext)
            for constraint in table.constraints
            if isinstance(constraint, sa.CheckConstraint)
        }
        self.uniques[name] = {
            constraint.name: tuple(column.name for column in constraint.columns)
            for constraint in table.constraints
            if isinstance(constraint, sa.UniqueConstraint)
        }
        self.foreign_keys[name] = {
            (fk.parent.name, fk.target_fullname, fk.ondelete)
            for constraint in table.constraints
            if isinstance(constraint, sa.ForeignKeyConstraint)
            for fk in constraint.elements
        }

    def create_index(self, name: str, table_name: str, columns: list[str], *_args: object, **_kwargs: object) -> None:
        self.indexes.append((name, table_name, list(columns)))

    def drop_index(self, name: str, *_args: object, **_kwargs: object) -> None:
        self.indexes = [index for index in self.indexes if index[0] != name]

    def drop_table(self, name: str, *_args: object, **_kwargs: object) -> None:
        if name in self.tables:
            self.tables.remove(name)


def _make_bind(dialect: str) -> MagicMock:
    bind = MagicMock()
    bind.dialect.name = dialect
    return bind


def _run(what: str, dialect: str, *, roles_exist: bool) -> _Recorder:
    migration = _load_migration()
    recorder = _Recorder()
    bind = _make_bind(dialect)
    recorder.get_bind = MagicMock(return_value=bind)  # type: ignore[attr-defined]
    with (
        patch.object(migration, "op", recorder),
        patch.object(migration, "_role_exists", return_value=roles_exist),
        patch.object(migration, "_assert_owner_is_migrate", return_value=None),
    ):
        getattr(migration, what)()
    return recorder


def _joined(recorder: _Recorder) -> str:
    return "\n".join(recorder.sql)


class TestChain:
    def test_single_linear_head(self) -> None:
        assert_single_linear_head(script_directory())

    def test_down_revision_is_0328_evidence_index_aggregate_indexes(self) -> None:
        assert _load_migration().down_revision == _DOWN_REVISION

    def test_revision_id_matches_filename(self) -> None:
        assert _load_migration().revision == _MIGRATION_NAME

    def test_no_branch_labels_or_depends_on(self) -> None:
        module = _load_migration()
        assert module.branch_labels is None
        assert module.depends_on is None


class TestOrgScopedCoverage:
    def test_org_scoped_tables_names_the_new_table(self) -> None:
        # test_rls_coverage.py reads this tuple to prove every org-scoped ORM
        # table has an ENABLE ROW LEVEL SECURITY migration.
        assert _load_migration()._ORG_SCOPED_TABLES == (_TABLE,)


class TestUpgradeSqlite:
    def test_creates_the_table_without_ceremony(self) -> None:
        recorder = _run("upgrade", "sqlite", roles_exist=False)
        assert recorder.tables == [_TABLE]
        joined = _joined(recorder).upper()
        assert "SET ROLE" not in joined
        assert "ROW LEVEL SECURITY" not in joined

    def test_column_set_is_pinned(self) -> None:
        recorder = _run("upgrade", "sqlite", roles_exist=False)
        assert recorder.columns[_TABLE] == _COLUMNS

    def test_no_backfill_runs(self) -> None:
        # Per-attempt history cannot be reconstructed; the table starts empty.
        joined = _joined(_run("upgrade", "sqlite", roles_exist=False)).upper()
        assert "INSERT INTO" not in joined

    def test_creates_only_the_org_index(self) -> None:
        # The (run_id, attempt_index) unique constraint already indexes the
        # run's attempt sequence; a second index on the pair would be redundant.
        recorder = _run("upgrade", "sqlite", roles_exist=False)
        assert recorder.indexes == [(_ORG_INDEX, _TABLE, ["organisation_id"])]

    def test_unique_constraint_is_on_run_and_attempt_index(self) -> None:
        recorder = _run("upgrade", "sqlite", roles_exist=False)
        assert recorder.uniques[_TABLE] == {_UNIQUE: ("run_id", "attempt_index")}

    def test_both_foreign_keys_cascade(self) -> None:
        recorder = _run("upgrade", "sqlite", roles_exist=False)
        assert recorder.foreign_keys[_TABLE] == {
            ("organisation_id", "organisations.id", "CASCADE"),
            ("run_id", "runs.id", "CASCADE"),
        }

    def test_check_constraints_are_pinned(self) -> None:
        recorder = _run("upgrade", "sqlite", roles_exist=False)
        checks = recorder.checks[_TABLE]
        assert checks["ck_run_attempts_terminal_reason"] == _load_migration()._TERMINAL_REASON_SQL
        assert {name: checks[name] for name in _CHECKS} == _CHECKS


class TestUpgradePostgres:
    def test_full_ceremony_present(self) -> None:
        joined = _joined(_run("upgrade", "postgresql", roles_exist=True))
        assert "SET ROLE modulo_migrate" in joined
        assert "RESET ROLE" in joined
        assert "ENABLE ROW LEVEL SECURITY" in joined
        assert "FORCE ROW LEVEL SECURITY" in joined
        assert "CREATE POLICY rls_org_isolation" in joined
        assert "GRANT SELECT, INSERT, UPDATE, DELETE ON run_attempts TO modulo_app" in joined
        assert "GRANT SELECT, INSERT, UPDATE, DELETE ON run_attempts TO modulo_system" in joined
        assert "GRANT REFERENCES ON TABLE public.runs TO modulo_migrate" in joined

    def test_policy_is_the_canonical_org_isolation_predicate(self) -> None:
        migration = _load_migration()
        assert (
            "organisation_id = nullif(current_setting('app.organisation_id', true), '')::uuid"
            in migration._CREATE_POLICY_STATEMENT
        )
        recorder = _run("upgrade", "postgresql", roles_exist=True)
        assert recorder.sql.count(migration._CREATE_POLICY_STATEMENT) == 1
        for statement in migration._ENABLE_RLS_STATEMENTS:
            assert recorder.sql.count(statement) == 1

    def test_ownership_assertion_is_invoked(self) -> None:
        migration = _load_migration()
        recorder = _Recorder()
        recorder.get_bind = MagicMock(return_value=_make_bind("postgresql"))  # type: ignore[attr-defined]
        with (
            patch.object(migration, "op", recorder),
            patch.object(migration, "_role_exists", return_value=True),
            patch.object(migration, "_assert_owner_is_migrate") as assert_mock,
        ):
            migration.upgrade()
        assert_mock.assert_called_once_with(recorder.get_bind.return_value, _TABLE)

    def test_owner_assertion_skipped_when_migrate_role_absent(self) -> None:
        migration = _load_migration()
        recorder = _Recorder()
        recorder.get_bind = MagicMock(return_value=_make_bind("postgresql"))  # type: ignore[attr-defined]
        with (
            patch.object(migration, "op", recorder),
            patch.object(migration, "_role_exists", return_value=False),
            patch.object(migration, "_assert_owner_is_migrate") as assert_mock,
        ):
            migration.upgrade()
        assert recorder.tables == [_TABLE]
        assert_mock.assert_not_called()


class TestDowngrade:
    def test_drops_policy_and_disables_rls(self) -> None:
        joined = _joined(_run("downgrade", "postgresql", roles_exist=True))
        assert f'DROP POLICY IF EXISTS rls_org_isolation ON "{_TABLE}"' in joined
        assert f'ALTER TABLE "{_TABLE}" DISABLE ROW LEVEL SECURITY' in joined

    def test_drops_index_and_table(self) -> None:
        recorder = _run("downgrade", "postgresql", roles_exist=True)
        assert not recorder.indexes
        assert not recorder.tables

    def test_sqlite_downgrade_skips_rls_statements(self) -> None:
        joined = _joined(_run("downgrade", "sqlite", roles_exist=False))
        assert "ROW LEVEL SECURITY" not in joined
        assert "DROP POLICY" not in joined


class TestModelParity:
    def test_table_name(self) -> None:
        assert RunAttempt.__tablename__ == _TABLE

    def test_model_and_migration_columns_agree(self) -> None:
        assert set(RunAttempt.__table__.columns.keys()) == _COLUMNS

    def test_model_and_migration_checks_agree(self) -> None:
        model_checks = {
            c.name: str(c.sqltext) for c in RunAttempt.__table__.constraints if isinstance(c, sa.CheckConstraint)
        }
        assert model_checks == _run("upgrade", "sqlite", roles_exist=False).checks[_TABLE]

    def test_model_and_migration_unique_constraint_agree(self) -> None:
        model_uniques = {
            c.name: tuple(column.name for column in c.columns)
            for c in RunAttempt.__table__.constraints
            if isinstance(c, sa.UniqueConstraint)
        }
        assert model_uniques == _run("upgrade", "sqlite", roles_exist=False).uniques[_TABLE]

    def test_model_and_migration_foreign_keys_agree(self) -> None:
        model_fks = {
            (fk.parent.name, fk.target_fullname, fk.ondelete)
            for constraint in RunAttempt.__table__.constraints
            if isinstance(constraint, sa.ForeignKeyConstraint)
            for fk in constraint.elements
        }
        assert model_fks == _run("upgrade", "sqlite", roles_exist=False).foreign_keys[_TABLE]

    def test_model_declares_the_org_index_and_no_redundant_pair_index(self) -> None:
        names = {index.name for index in RunAttempt.__table__.indexes}
        assert names == {_ORG_INDEX}

    def test_terminal_reason_vocabulary_is_single_sourced(self) -> None:
        # The migration carries a frozen copy; it must list exactly the model's
        # vocabulary, so a value added to one without the other fails here.
        quoted = set(re.findall(r"'([a-z_]+)'", _load_migration()._TERMINAL_REASON_SQL))
        assert quoted == set(ATTEMPT_TERMINAL_REASON_VALUES)
        assert _load_migration()._TERMINAL_REASON_SQL == ATTEMPT_TERMINAL_REASON_SQL
