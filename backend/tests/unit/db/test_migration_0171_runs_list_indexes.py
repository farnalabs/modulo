"""Unit tests for migration 0171_runs_list_performance_indexes.

Structural: load the migration module and assert its contract without a
database, and pin model/migration parity for the one new runs index:

* the chain is pinned (revision -> 0170_add_residual_foreign_keys) so the
  pre-commit check-migration-heads hook can never be ambushed by a rebase;
* the upgrade creates exactly the one index the runs-list fix relies on
  (ix_runs_org_created_pipeline with a non-key INCLUDE column for the list
  total COUNT), using the repo's idempotent CREATE INDEX IF NOT EXISTS
  convention (0128/0154/0155);
* it does NOT re-create an (organisation_id, status) index — migration 0155
  already ships ix_runs_organisation_status (and ix_runs_pipeline_status) and
  a duplicate would tax the hottest write path of the biggest table;
* the downgrade drops the index;
* the Run model declares the 0171 index (create_all'd schemas and
  autogenerate stay in sync) and does not declare a redundant org+status one —
  consistent with the file's existing convention of omitting 0155's indexes.

They run without a database.
"""

import importlib.util
from pathlib import Path
from types import ModuleType

_VERSIONS = Path(__file__).resolve().parents[3] / "src" / "modulo" / "db" / "migrations" / "versions"
_MIGRATION_NAME = "0171_runs_list_performance_indexes"
_MIGRATION_PATH = _VERSIONS / f"{_MIGRATION_NAME}.py"


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
    assert module.down_revision == "0170_add_residual_foreign_keys"
    assert module.branch_labels is None
    assert module.depends_on is None


def test_upgrade_creates_only_the_list_count_index() -> None:
    code = _source_code()
    assert "ix_runs_org_created_pipeline" in code
    # The idempotency convention of 0128/0154/0155: raw CREATE INDEX IF NOT
    # EXISTS, not op.create_index.
    assert "CREATE INDEX IF NOT EXISTS ix_runs_org_created_pipeline" in code
    # The covering index must carry pipeline_id as a non-key INCLUDE column —
    # that is what keeps the list total COUNT index-only.
    assert "INCLUDE (pipeline_id)" in code
    # Migration 0155 already ships the (organisation_id, status) and
    # (pipeline_id, status) hot-query indexes; 0171 must not duplicate them.
    assert "ix_runs_org_status" not in code
    assert "op.create_index" not in code
    # Exactly one index is created.
    assert code.count("CREATE INDEX") == 1


def test_downgrade_drops_the_list_count_index() -> None:
    code = _source_code().split("def downgrade", 1)[1]
    assert "DROP INDEX IF EXISTS ix_runs_org_created_pipeline;" in code
    # Only the 0171 index is dropped — 0155's indexes are untouched.
    assert code.count("DROP INDEX") == 1


def test_model_declares_the_list_count_index_only() -> None:
    from modulo.db.models.run import Run

    index_names = {idx.name for idx in Run.__table__.indexes}
    assert "ix_runs_org_created_pipeline" in index_names, (
        "model/migration drift: ix_runs_org_created_pipeline missing from Run"
    )
    # 0155 already created ix_runs_organisation_status in the DB; the model
    # follows the file's existing convention of not declaring 0155's indexes,
    # and must never re-introduce the redundant ix_runs_org_status.
    assert "ix_runs_org_status" not in index_names, (
        "ix_runs_org_status duplicates 0155's ix_runs_organisation_status — do not re-add it"
    )
    assert "ix_runs_organisation_status" not in index_names, (
        "0155's hot-query indexes are intentionally not declared on the model; keep that convention"
    )
    # Parity on the INCLUDE column too: the model index must carry the same
    # non-key pipeline_id column the migration creates. Included columns live
    # in the postgresql dialect options, not in Index.columns.
    model_index = next(idx for idx in Run.__table__.indexes if idx.name == "ix_runs_org_created_pipeline")
    assert model_index.dialect_options["postgresql"]["include"] == ["pipeline_id"]
    assert [col.name for col in model_index.columns] == ["organisation_id", "created_at"]
