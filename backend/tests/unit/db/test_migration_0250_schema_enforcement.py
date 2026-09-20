"""FAR-902: structural unit tests for migration 0250_schema_enforcement_telemetry.

These run WITHOUT a database. They pin the migration's contract: the column
shape (schema_enforcement_json on run_node_outputs), the CHECK constraint,
the partial index predicate, the revision chain position, and the
deploy-safety markers (Postgres-guarded upgrade/downgrade).
"""

from __future__ import annotations

import importlib.util
from pathlib import Path
from types import ModuleType

_VERSIONS = Path(__file__).resolve().parents[3] / "src" / "modulo" / "db" / "migrations" / "versions"
_MIGRATION_NAME = "0250_schema_enforcement_telemetry"
_MIGRATION_PATH = _VERSIONS / f"{_MIGRATION_NAME}.py"


def _load_migration() -> ModuleType:
    assert _MIGRATION_PATH.exists(), f"Migration file missing: {_MIGRATION_PATH}"
    spec = importlib.util.spec_from_file_location(f"migration_{_MIGRATION_NAME}", _MIGRATION_PATH)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_revision_chain_position() -> None:
    """The migration extends the current head (0249_validation_level)."""
    module = _load_migration()
    assert module.revision == "0250_schema_enforcement_telemetry"
    assert module.down_revision == "0249_validation_level"
    assert module.branch_labels is None
    assert module.depends_on is None


def test_column_added() -> None:
    """Upgrade adds schema_enforcement_json to run_node_outputs."""
    source = _MIGRATION_PATH.read_text(encoding="utf-8")
    assert "schema_enforcement_json" in source
    assert "run_node_outputs" in source


def test_check_constraint_present() -> None:
    """The CHECK constraint enforces __final__ sentinel exclusion."""
    source = _MIGRATION_PATH.read_text(encoding="utf-8")
    assert "ck_run_node_outputs_final_no_enforcement" in source
    assert "attempt_key <> '__final__' OR schema_enforcement_json IS NULL" in source


def test_partial_index_predicate_matches_sweep_query() -> None:
    """The partial index predicate must match the analytics sweep query filter.

    The sweep query in enforcement_sweep.py uses:
      WHERE rno.schema_enforcement_json IS NOT NULL
        AND rno.attempt_key <> '__final__'

    The partial index must have the same predicate.
    """
    source = _MIGRATION_PATH.read_text(encoding="utf-8")
    assert "ix_run_node_outputs_enforcement_pending" in source
    assert "schema_enforcement_json IS NOT NULL" in source
    assert "attempt_key <> '__final__'" in source


def test_downgrade_drops_column() -> None:
    """Downgrade removes the column, CHECK constraint, and index."""
    source = _MIGRATION_PATH.read_text(encoding="utf-8")
    assert "drop_index" in source
    assert "drop_constraint" in source
    assert "drop_column" in source


def test_postgres_guarded() -> None:
    """Upgrade and downgrade are guarded by _is_postgres()."""
    source = _MIGRATION_PATH.read_text(encoding="utf-8")
    assert "_is_postgres()" in source


def test_idempotent_upgrade() -> None:
    """Upgrade checks for column existence before adding."""
    source = _MIGRATION_PATH.read_text(encoding="utf-8")
    assert "_has_column" in source
