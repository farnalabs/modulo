"""Unit tests for migration 0266_notification_hot_query_indexes.

Structural: load the migration module and assert its contract without a
database, and pin model/migration parity for the four new notification
indexes (the 0171 convention):

* the chain is pinned (revision -> 0265_hitl_review_window) so the
  pre-commit check-migration-heads hook can never be ambushed by a rebase;
* the upgrade creates exactly the four indexes the hot read paths in
  ``db/crud/notifications.py`` rely on, using the repo's idempotent
  ``CREATE INDEX IF NOT EXISTS`` convention (0128/0155) rather than
  ``op.create_index`` (Alembic wraps each revision in a transaction, so
  ``CONCURRENTLY`` is unavailable);
* the downgrade drops exactly those four indexes;
* ``Notification`` / ``Dismissal`` declare the same indexes so create_all'd
  schemas and autogenerate stay in sync with the migrated schema.

They run without a database.
"""

import importlib.util
from pathlib import Path
from types import ModuleType

_VERSIONS = Path(__file__).resolve().parents[3] / "src" / "modulo" / "db" / "migrations" / "versions"
_MIGRATION_NAME = "0266_notification_hot_query_indexes"
_MIGRATION_PATH = _VERSIONS / f"{_MIGRATION_NAME}.py"

#: index name -> (owning table, key columns in declaration order)
_EXPECTED_INDEXES: dict[str, tuple[str, tuple[str, ...]]] = {
    "ix_notifications_org_expires_at": ("notifications", ("organisation_id", "expires_at")),
    "ix_notifications_org_scope_target": (
        "notifications",
        ("organisation_id", "scope", "target_user_id"),
    ),
    "ix_dismissals_org_user_scope": (
        "dismissals",
        ("organisation_id", "dismissed_by_user_id", "dismiss_scope"),
    ),
    "ix_dismissals_user_scope": ("dismissals", ("dismissed_by_user_id", "dismiss_scope")),
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
    assert module.down_revision == "0265_hitl_review_window"
    assert module.branch_labels is None
    assert module.depends_on is None


def test_upgrade_creates_the_four_indexes_idempotently() -> None:
    code = _source_code()
    for name in _EXPECTED_INDEXES:
        assert f"CREATE INDEX IF NOT EXISTS {name}" in code, f"upgrade missing {name}"
    # The idempotency convention of 0128/0155: raw CREATE INDEX IF NOT EXISTS,
    # not op.create_index.
    assert "op.create_index" not in code
    # Exactly the four expected indexes are created.
    assert code.count("CREATE INDEX") == len(_EXPECTED_INDEXES)


def test_downgrade_drops_the_four_indexes() -> None:
    code = _source_code()
    for name in _EXPECTED_INDEXES:
        assert f"DROP INDEX IF EXISTS {name};" in code, f"downgrade missing {name}"
    assert code.count("DROP INDEX") == len(_EXPECTED_INDEXES)


def test_models_declare_the_four_indexes_with_matching_columns() -> None:
    from modulo.db.models.notification import Dismissal, Notification

    by_table: dict[str, dict[str, list[str]]] = {
        "notifications": {idx.name: [col.name for col in idx.columns] for idx in Notification.__table__.indexes},
        "dismissals": {idx.name: [col.name for col in idx.columns] for idx in Dismissal.__table__.indexes},
    }
    for name, (table, columns) in _EXPECTED_INDEXES.items():
        assert name in by_table[table], f"model/migration drift: {name} missing from {table}"
        assert by_table[table][name] == list(columns), (
            f"model/migration drift: {name} columns {by_table[table][name]} != {list(columns)}"
        )
