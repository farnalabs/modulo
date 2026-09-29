"""Unit tests for migration 0268_webhook_lookup_expiry_indexes.

Structural: load the migration module and assert its contract without a
database:

* the chain is pinned (revision -> 0267_notification_hot_query_indexes) so the
  pre-commit check-migration-heads hook can never be ambushed by a renumber;
* the upgrade creates exactly the three webhook lookup indexes the replay and
  expiry read paths rely on, using the repo's idempotent
  ``CREATE INDEX IF NOT EXISTS`` convention (0128/0155/0267);
* the downgrade drops exactly those three indexes.

They run without a database.
"""

import importlib.util
from pathlib import Path
from types import ModuleType

_VERSIONS = Path(__file__).resolve().parents[3] / "src" / "modulo" / "db" / "migrations" / "versions"
_MIGRATION_NAME = "0268_webhook_lookup_expiry_indexes"
_MIGRATION_PATH = _VERSIONS / f"{_MIGRATION_NAME}.py"

_INDEXES = (
    "ix_webhook_payloads_trigger_event_org",
    "ix_webhook_dedup_hashes_org_expires",
    "ix_webhook_payloads_org_expires",
)


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
    assert module.down_revision == "0267_notification_hot_query_indexes"
    assert module.branch_labels is None
    assert module.depends_on is None


def test_upgrade_creates_the_three_lookup_indexes() -> None:
    code = _source_code()
    for name in _INDEXES:
        assert f"CREATE INDEX IF NOT EXISTS {name} " in code, f"upgrade missing {name}"
    # The idempotency convention of 0128/0155/0267: raw CREATE INDEX IF NOT
    # EXISTS, not op.create_index.
    assert "op.create_index" not in code
    assert code.count("CREATE INDEX") == len(_INDEXES)


def test_downgrade_drops_the_three_lookup_indexes() -> None:
    code = _source_code()
    for name in _INDEXES:
        assert f"DROP INDEX IF EXISTS {name};" in code, f"downgrade missing {name}"
    assert code.count("DROP INDEX") == len(_INDEXES)
    # The downgrade must execute the drop list, not merely declare it.
    assert "_DROPS" in code.split("def downgrade", 1)[1]


def test_org_scoped_composites_lead_on_tenant_column() -> None:
    code = _source_code()
    # Both tables are RLS org-isolated, so the org-scoped expiry arms must lead
    # with organisation_id for the tenant predicate to be the index prefix.
    assert 'ON public."webhook_dedup_hashes" (organisation_id, expires_at)' in code
    assert 'ON public."webhook_payloads" (organisation_id, expires_at)' in code
