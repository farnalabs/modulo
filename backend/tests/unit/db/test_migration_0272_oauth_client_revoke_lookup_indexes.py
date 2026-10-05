"""Unit tests for migration 0272_oauth_client_revoke_lookup_indexes.

Structural - load the migration module and assert its contract without a
database, and pin model/migration parity for the two new OAuth
client-revoke lookup indexes:

* the chain is pinned (0272 -> ``0271_org_api_keys_revocation_sweep_indexes``)
  with ``0273_runs_dispatch_phase`` the single linear head, so the
  pre-commit check-migration-heads hook and every ``test_single_head_*``
  pin cannot be ambushed by a renumber;
* the upgrade emits exactly the two ``CREATE INDEX IF NOT EXISTS`` statements
  the client-revoke DELETEs rely on (``auth/oauth.py::delete_oauth_client``
  against ``oauth_authorization_codes`` and ``oauth_token_families``), each
  leading on ``organisation_id`` (both tables are RLS org-isolated, so the
  tenant column must be the index prefix);
* the downgrade drops exactly those two indexes;
* the ``OAuthAuthorizationCode`` / ``OAuthTokenFamily`` models declare the
  same two indexes - same name, same ordered key columns - so
  ``create_all``'d schemas and autogenerate stay in sync.

They run without a database.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path
from types import ModuleType
from unittest.mock import MagicMock, patch

from alembic.script import ScriptDirectory
from sqlalchemy import Index

from modulo.db.models.oauth_token import OAuthAuthorizationCode, OAuthTokenFamily

_VERSIONS = Path(__file__).resolve().parents[3] / "src" / "modulo" / "db" / "migrations" / "versions"
_MIGRATION_NAME = "0272_oauth_client_revoke_lookup_indexes"
_MIGRATION_PATH = _VERSIONS / f"{_MIGRATION_NAME}.py"
_DOWN_REVISION = "0271_org_api_keys_revocation_sweep_indexes"

#: Index name -> (table, ordered key columns). This is the single source of
#: truth asserted against BOTH the migration DDL and the ORM declaration,
#: so a one-sided edit to either fails here instead of in prod.
_INDEXES: dict[str, tuple[str, tuple[str, ...]]] = {
    "ix_oauth_auth_codes_org_client": (
        'public."oauth_authorization_codes"',
        ("organisation_id", "client_id"),
    ),
    "ix_oauth_token_families_org_client": (
        'public."oauth_token_families"',
        ("organisation_id", "client_id"),
    ),
}

#: Index name -> declaring ORM model, for the model/migration parity checks.
_MODEL_BY_INDEX: dict[str, type] = {
    "ix_oauth_auth_codes_org_client": OAuthAuthorizationCode,
    "ix_oauth_token_families_org_client": OAuthTokenFamily,
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
    table, columns = _INDEXES[name]
    return f"CREATE INDEX IF NOT EXISTS {name} ON {table} ({', '.join(columns)});"


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


def _model_indexes(model: type) -> dict[str, Index]:
    return {idx.name: idx for idx in model.__table__.indexes if idx.name is not None}


class TestChain:
    def test_single_head_is_0275(self) -> None:
        heads = ScriptDirectory(str(_VERSIONS.parent)).get_heads()
        # 0273_runs_dispatch_phase (FAR-1088), then
        # 0274_policy_gate_pin_fingerprint_operator_control (FAR-967 chunk 10), then
        # 0275_run_cancel_reason_vocabulary (FAR-1406), then
        # 0276_runs_autovacuum_enabled (FAR-1419), then
        # 0277_run_daily_facts_trigger_dispatch_phase (FAR-1421), then
        # 0278_runs_workspace_drift_sweep_index (FAR-1438), then
        # 0279_table_autovacuum_tuning (FAR-1442), then
        # 0280_runs_node_deadline_watchdog_fired_count (FAR-1463), then
        # 0281_env_profiles_kubernetes (FAR-1051),
        # now chain onto this migration, so the single head moved up nine.
        assert heads == ["0281_env_profiles_kubernetes"], f"expected a single head, got {heads}"

    def test_down_revision_is_0271_org_api_keys_revocation_sweep_indexes(self) -> None:
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

    def test_uses_the_idempotent_create_index_if_not_exists_convention(self) -> None:
        code = _source_code()
        # The idempotency convention of 0128/0155/0267/0271: raw CREATE INDEX
        # IF NOT EXISTS, not op.create_index.
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
        for name, (table, columns) in _INDEXES.items():
            assert columns[0] == "organisation_id", f"{name} must lead on organisation_id"
            statement = _expected_create_statement(name)
            assert statement in executed, f"upgrade missing {name}"
            assert f"ON {table} ({', '.join(columns)})" in statement, statement


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
        for name, (_, columns) in _INDEXES.items():
            declared = _model_indexes(_MODEL_BY_INDEX[name])
            assert name in declared, f"model/migration drift: {name} missing from the ORM"
            model_columns = tuple(col.name for col in declared[name].columns)
            assert model_columns == columns, (
                f"model/migration drift: {name} model columns {model_columns} != migration {columns}"
            )
