"""Unit tests for migration 0271_org_api_keys_revocation_sweep_indexes.

Structural - load the migration module and assert its contract without a
database, and pin model/migration parity for the two new ``org_api_keys``
lookup indexes:

* the chain is pinned (0271 -> ``0270_pipeline_snapshots_max_autonomy_ge_default``,
  with ``0277_run_daily_facts_trigger_dispatch_phase`` then
  ``0278_runs_workspace_drift_sweep_index`` then
  ``0279_table_autovacuum_tuning`` then
  ``0280_runs_node_deadline_watchdog_fired_count`` then
  ``0281_org_api_keys_grants`` then
  ``0282_env_profiles_kubernetes`` then
  ``0283_runs_drop_unused_indexes`` then
  ``0284_add_rejected_run_status`` then
  ``0285_system_audit_events`` then
  ``0286_pipeline_run_state`` then
  ``0287_team_rls_lifecycle_evals`` then
  ``0288_runs_execution_origin`` then
  ``0289_pipelines_environment_profile`` then
  ``0290_scheduled_reports_due_scan`` then
  ``0291_invitations_lookup_constraints`` then
  ``0292_audit_events_resource_lookup`` now the single linear head)
  so the pre-commit check-migration-heads hook and every ``test_single_head_*``
  pin cannot be ambushed by a renumber;
* the upgrade emits exactly the two ``CREATE INDEX IF NOT EXISTS`` statements
  the revocation and stale-sweep read paths rely on (``auth/api_key.py::
  revoke_run_api_key`` / ``revoke_run_api_key_sweep`` and
  ``core/housekeeping.py::_scan_stale_api_keys``), each with the exact partial
  predicate those paths are written against, leading on ``organisation_id``
  (both tables are RLS org-isolated, so the tenant column must be the index
  prefix);
* the downgrade drops exactly those two indexes;
* the ``OrgApiKey`` model declares the same two partial indexes - same name,
  same ordered key columns, same ``postgresql_where`` / ``sqlite_where``
  predicates - so ``create_all``'d schemas and autogenerate stay in sync.

They run without a database.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path
from types import ModuleType
from unittest.mock import MagicMock, patch

from alembic.script import ScriptDirectory
from sqlalchemy import Index

from modulo.db.models.api_key import OrgApiKey

_VERSIONS = Path(__file__).resolve().parents[3] / "src" / "modulo" / "db" / "migrations" / "versions"
_MIGRATION_NAME = "0271_org_api_keys_revocation_sweep_indexes"
_MIGRATION_PATH = _VERSIONS / f"{_MIGRATION_NAME}.py"
_DOWN_REVISION = "0270_pipeline_snapshots_max_autonomy_ge_default"
_HEAD_MIGRATION = "0292_audit_events_resource_lookup"
_TABLE = 'public."org_api_keys"'

#: Index name -> (ordered key columns, partial WHERE predicate). This is the
#: single source of truth asserted against BOTH the migration DDL and the ORM
#: declaration, so a one-sided edit to either fails here instead of in prod.
_INDEXES: dict[str, tuple[tuple[str, ...], str]] = {
    "ix_org_api_keys_live_run_keys": (
        ("organisation_id", "run_id"),
        "revoked_at IS NULL AND run_id IS NOT NULL",
    ),
    "ix_org_api_keys_stale_sweep": (
        ("organisation_id", "last_used_at"),
        "revoked_at IS NULL",
    ),
}


def _load_migration() -> ModuleType:
    assert _MIGRATION_PATH.exists(), f"Migration file missing: {_MIGRATION_PATH}"
    spec = importlib.util.spec_from_file_location(f"migration_{_MIGRATION_NAME}", _MIGRATION_PATH)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _source_code() -> str:
    """Return the migration's executable code, minus the module docstring."""
    source = _MIGRATION_PATH.read_text(encoding="utf-8")
    parts = source.split('"""', 2)
    return parts[2] if len(parts) >= 3 else source


def _expected_create_statement(name: str) -> str:
    columns, predicate = _INDEXES[name]
    return f"CREATE INDEX IF NOT EXISTS {name} ON {_TABLE} ({', '.join(columns)}) WHERE {predicate};"


def _executed(entry_point: str) -> list[str]:
    """Every statement the given entry point executes, in order.

    The migration routes both the creates and the drops through
    ``op.get_bind().execute(text(...))``, so the bind's ``execute`` is the
    single channel to record.
    """
    module = _load_migration()
    executed: list[str] = []

    def _record(stmt: object, *_args: object, **_kwargs: object) -> MagicMock:
        executed.append(str(getattr(stmt, "text", stmt)))
        return MagicMock()

    with patch.object(module, "op") as op:
        op.get_bind.return_value.execute.side_effect = _record
        getattr(module, entry_point)()
    return executed


def _model_indexes() -> dict[str, Index]:
    return {idx.name: idx for idx in OrgApiKey.__table__.indexes if idx.name is not None}


class TestChain:
    def test_single_head_is_0271(self) -> None:
        heads = ScriptDirectory(str(_VERSIONS.parent)).get_heads()
        assert heads == [_HEAD_MIGRATION], f"expected a single head, got {heads}"

    def test_down_revision_is_0270_pipeline_snapshots_max_autonomy_ge_default(self) -> None:
        assert _load_migration().down_revision == _DOWN_REVISION

    def test_revision_id_matches_filename(self) -> None:
        assert _load_migration().revision == _MIGRATION_NAME

    def test_no_branch_labels_or_depends_on(self) -> None:
        module = _load_migration()
        assert module.branch_labels is None
        assert module.depends_on is None


class TestUpgrade:
    def test_upgrade_emits_the_two_create_index_statements(self) -> None:
        executed = _executed("upgrade")
        assert len(executed) == len(_INDEXES), f"expected {len(_INDEXES)} statements, got {executed}"
        for name in _INDEXES:
            assert _expected_create_statement(name) in executed, (
                f"upgrade missing the exact statement for {name}: {_expected_create_statement(name)}"
            )

    def test_partial_predicates_are_exact(self) -> None:
        """Each index carries precisely the predicate its read path filters on.

        ``ix_org_api_keys_stale_sweep``'s ``revoked_at IS NULL`` is a prefix of
        the run-key predicate, so equality against the full statement (not a
        substring probe) is what keeps the two from being confused.
        """
        executed = _executed("upgrade")
        for name, (_, predicate) in _INDEXES.items():
            statement = _expected_create_statement(name)
            assert statement in executed, statement
            assert statement.endswith(f"WHERE {predicate};"), statement

    def test_uses_the_idempotent_create_index_if_not_exists_convention(self) -> None:
        code = _source_code()
        # The idempotency convention of 0128/0155/0267: raw CREATE INDEX IF NOT
        # EXISTS, not op.create_index.
        assert "op.create_index" not in code
        assert "op.execute(f" not in code
        assert "text(f" not in code
        assert code.count("CREATE INDEX") == len(_INDEXES)

    def test_both_indexes_qualify_the_table_and_lead_on_the_tenant_column(self) -> None:
        # Both tables are RLS org-isolated, so the org-scoped predicate must
        # lead with organisation_id for the tenant filter to be the index
        # prefix. The statement comes from the executed SQL (the source splits
        # each statement across concatenated literals).
        executed = _executed("upgrade")
        for name, (columns, _) in _INDEXES.items():
            assert columns[0] == "organisation_id", f"{name} must lead on organisation_id"
            statement = _expected_create_statement(name)
            assert statement in executed, f"upgrade missing {name}"
            assert f"ON {_TABLE} ({', '.join(columns)})" in statement, statement


class TestDowngrade:
    def test_downgrade_drops_the_two_indexes(self) -> None:
        executed = _executed("downgrade")
        assert executed == [f"DROP INDEX IF EXISTS {name};" for name in _INDEXES], executed

    def test_downgrade_executes_the_drop_list(self) -> None:
        code = _source_code()
        assert code.count("DROP INDEX") == len(_INDEXES)
        # The downgrade must execute the drop list, not merely declare it.
        assert "_DROPS" in code.split("def downgrade", 1)[1]


class TestModelParity:
    def test_model_declares_the_two_indexes_with_the_same_columns(self) -> None:
        declared = _model_indexes()
        for name, (columns, _) in _INDEXES.items():
            assert name in declared, f"model/migration drift: {name} missing from the ORM"
            model_columns = tuple(col.name for col in declared[name].columns)
            assert model_columns == columns, (
                f"model/migration drift: {name} model columns {model_columns} != migration {columns}"
            )

    def test_model_partial_predicates_match_the_migration(self) -> None:
        declared = _model_indexes()
        for name, (_, predicate) in _INDEXES.items():
            index = declared[name]
            postgresql_where = index.dialect_options["postgresql"].get("where")
            sqlite_where = index.dialect_options["sqlite"].get("where")
            assert postgresql_where is not None, f"{name} model missing postgresql_where"
            assert sqlite_where is not None, f"{name} model missing sqlite_where"
            assert str(postgresql_where) == predicate, (
                f"model/migration drift: {name} postgresql_where {postgresql_where!s} != {predicate}"
            )
            assert str(sqlite_where) == predicate, (
                f"model/migration drift: {name} sqlite_where {sqlite_where!s} != {predicate}"
            )
