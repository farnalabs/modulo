"""Unit tests for migration 0267_notification_hot_query_indexes.

Structural: load the migration module and assert its contract without a
database, and pin model/migration parity for the four new notification/
dismissal lookup indexes:

* the chain is pinned (revision -> 0266_guardrail_policy_gate_sweep) so the
  pre-commit check-migration-heads hook can never be ambushed by a renumber;
* the upgrade creates exactly the four hot-query indexes the crud read paths
  rely on, using the repo's idempotent ``CREATE INDEX IF NOT EXISTS``
  convention (0128/0155);
* the downgrade drops exactly those four indexes;
* the ``Notification``/``Dismissal`` models declare the same four indexes with
  the same key columns, so ``create_all``'d schemas and autogenerate stay in
  sync.

They run without a database.
"""

import importlib.util
from pathlib import Path
from types import ModuleType

from modulo.db.models.notification import Dismissal, Notification

_VERSIONS = Path(__file__).resolve().parents[3] / "src" / "modulo" / "db" / "migrations" / "versions"
_MIGRATION_NAME = "0267_notification_hot_query_indexes"
_MIGRATION_PATH = _VERSIONS / f"{_MIGRATION_NAME}.py"

_INDEXES = (
    "ix_notifications_org_expires_at",
    "ix_notifications_org_scope_target",
    "ix_dismissals_org_user_scope",
    "ix_dismissals_user_scope",
)

#: Index name -> ordered key columns; the model must declare the same shape.
_INDEX_COLUMNS = {
    "ix_notifications_org_expires_at": ["organisation_id", "expires_at"],
    "ix_notifications_org_scope_target": ["organisation_id", "scope", "target_user_id"],
    "ix_dismissals_org_user_scope": ["organisation_id", "dismissed_by_user_id", "dismiss_scope"],
    "ix_dismissals_user_scope": ["dismissed_by_user_id", "dismiss_scope"],
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


def test_metadata_pins_chain() -> None:
    module = _load_migration()
    assert module.revision == _MIGRATION_NAME
    assert module.down_revision == "0266_guardrail_policy_gate_sweep"
    assert module.branch_labels is None
    assert module.depends_on is None


def test_upgrade_creates_the_four_lookup_indexes() -> None:
    code = _source_code()
    for name in _INDEXES:
        assert f"CREATE INDEX IF NOT EXISTS {name} " in code, f"upgrade missing {name}"
    # The idempotency convention of 0128/0155: raw CREATE INDEX IF NOT EXISTS,
    # not op.create_index.
    assert "op.create_index" not in code
    assert code.count("CREATE INDEX") == len(_INDEXES)


def test_downgrade_drops_the_four_lookup_indexes() -> None:
    code = _source_code()
    for name in _INDEXES:
        assert f"DROP INDEX IF EXISTS {name};" in code, f"downgrade missing {name}"
    assert code.count("DROP INDEX") == len(_INDEXES)
    # The downgrade must execute the drop list, not merely declare it.
    assert "_DROPS" in code.split("def downgrade", 1)[1]


def test_models_declare_the_four_lookup_indexes() -> None:
    declared: dict[str, list[str]] = {}
    for table in (Notification.__table__, Dismissal.__table__):
        for idx in table.indexes:
            declared[idx.name] = [col.name for col in idx.columns]

    for name, columns in _INDEX_COLUMNS.items():
        assert name in declared, f"model/migration drift: {name} missing from the ORM"
        assert declared[name] == columns, (
            f"model/migration drift: {name} model columns {declared[name]} != migration {columns}"
        )
