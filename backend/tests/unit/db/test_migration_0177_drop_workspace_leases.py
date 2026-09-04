"""Unit tests for migration 0177_drop_workspace_leases (FAR-587 / ADR 029).

Structural: load the migration module and assert its contract without a
database:

* the chain is pinned (revision -> 0176_env_profiles_runner_docker);
* the upgrade REFUSES (RAISE EXCEPTION) when lease rows exist — a database
  that somehow acquired lease rows is never silently destroyed — and drops
  the table otherwise;
* the downgrade recreates the table EMPTY with its original shape (columns,
  PK, status CHECK, RESTRICT foreign keys, indexes, same-org tenant
  triggers, RLS policy) — lease row data cannot be restored;
* the WorkspaceLease model is gone from the metadata (model/migration
  parity: no ORM table for a dropped DB table).

They run without a database.
"""

import importlib.util
from pathlib import Path
from types import ModuleType

_VERSIONS = Path(__file__).resolve().parents[3] / "src" / "modulo" / "db" / "migrations" / "versions"
_MIGRATION_NAME = "0177_drop_workspace_leases"
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
    assert module.down_revision == "0176_env_profiles_runner_docker"
    assert module.branch_labels is None
    assert module.depends_on is None


def test_upgrade_refuses_when_lease_rows_exist() -> None:
    code = _source_code().split("def upgrade", 1)[1].split("def downgrade", 1)[0]
    assert "SELECT count(*) INTO lease_rows FROM public.workspace_leases" in code
    assert "RAISE EXCEPTION" in code
    assert "Refusing to drop workspace_leases" in code
    # The guarded drop happens only after the row-count guard.
    guard_at = code.index("lease_rows > 0")
    drop_at = code.index("DROP TABLE public.workspace_leases")
    assert guard_at < drop_at


def test_downgrade_recreates_empty_table_with_original_shape() -> None:
    code = _source_code().split("def downgrade", 1)[1]
    # Guarded create (only when the table is absent).
    assert "IF NOT EXISTS" in code
    assert "CREATE TABLE public.workspace_leases" in code
    # Original columns/constraints from 0005 + 0110.
    for fragment in (
        "environment_profile_id uuid NOT NULL",
        "run_id uuid NOT NULL",
        "ck_workspace_leases_status",
        "workspace_leases_environment_profile_id_fkey",
        "workspace_leases_run_id_fkey",
        "ON DELETE RESTRICT",
        "ix_workspace_leases_run_id",
        "trg_workspace_leases_run_id_tenant",
        "ENABLE ROW LEVEL SECURITY",
        "rls_org_isolation",
    ):
        assert fragment in code, f"downgrade must recreate: {fragment}"


def test_model_metadata_has_no_workspace_leases_table() -> None:
    from modulo.db.models import Base

    assert "workspace_leases" not in Base.metadata.tables
