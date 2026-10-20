"""Unit tests for migration 0322_pipeline_snapshots_substantive_version_index.

Structural: load the migration module and assert its contract without a
database, mirroring test_migration_0171_runs_list_indexes.py.

* the chain is pinned (revision -> 0321_organisation_cost_state) so the
  pre-commit check-migration-heads hook cannot be ambushed by a rebase;
* the upgrade creates exactly one index, with the repo's idempotent
  ``CREATE INDEX IF NOT EXISTS`` convention (0187/0171/0182);
* the index is PARTIAL with the predicate the FAR-1895 filtered query reduces
  to for a one-element exclusion list (the query emits
  ``created_kind NOT IN ('run')``, which reduces to ``created_kind <> 'run'``)
  and the query's ordering key (``pipeline_id, snapshot_version DESC``) — that
  is what lets Postgres take the partial index instead of walking the unbounded
  run streak per request;
* the downgrade drops the index;
* no statement interpolates an identifier with an f-string (the semgrep
  migration-fstring-sql / raw-sql-fstring gates block that in new migrations).
"""

import importlib.util
from pathlib import Path
from types import ModuleType

_VERSIONS = Path(__file__).resolve().parents[3] / "src" / "modulo" / "db" / "migrations" / "versions"
_MIGRATION_NAME = "0322_pipeline_snapshots_substantive_version_index"
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
    assert module.down_revision == "0321_organisation_cost_state"
    assert module.branch_labels is None
    assert module.depends_on is None


def test_upgrade_creates_the_partial_substantive_version_index() -> None:
    code = _source_code()
    assert "CREATE INDEX IF NOT EXISTS ix_pipeline_snapshots_version_substantive" in code
    # The ordering key the filtered query uses, DESC.
    assert "ON pipeline_snapshots (pipeline_id, snapshot_version DESC)" in code
    # PARTIAL: the predicate the filtered query reduces to for one value.
    assert "WHERE created_kind <> 'run'" in code
    assert "op.create_index" not in code
    # Exactly one index is created.
    assert code.count("CREATE INDEX") == 1


def test_downgrade_drops_the_substantive_version_index() -> None:
    module = _load_migration()
    assert module._DROP_SQL == "DROP INDEX IF EXISTS ix_pipeline_snapshots_version_substantive"
    code = _source_code()
    assert "op.execute(_DROP_SQL)" in code
    # Exactly one drop statement, and it is the literal (not interpolated).
    assert code.count("DROP INDEX") == 1


def test_no_interpolated_ddl_statement() -> None:
    """The migration uses literal DDL, never an f-string.

    The semgrep migration-fstring-sql / raw-sql-fstring rules BLOCK any
    interpolated DDL in a migration after 0247. An f-string drop (the shape
    this test was written against) ships red; this pins the literal form.
    """
    code = _source_code()
    assert "op.execute(f" not in code
    assert "DROP INDEX IF EXISTS {" not in code
