"""Structural unit tests for migration 0206_deleted_defaults_signal_check (FAR-644).

These run WITHOUT a database. They pin the drift fix's contract: the chain
position (down_revision = 0205_library_collection_type), the single-target
constants (table, CHECK name, expression) whose upgrade assembles the guarded
``NOT VALID`` + ``VALIDATE`` pair — the repo's populated-table deploy-safety
precedent (0157/0165) — the state-aware pg_constraint idempotency guard
(a run interrupted between ADD NOT VALID and VALIDATE must replay as
VALIDATE, never leave the constraint silently un-validated), and the
non-Postgres no-op branch (SQLite builds schema from the ORM, so the ORM's
CHECK declaration covers the test backend; see 0141's branch rationale).
"""

import importlib.util
from pathlib import Path
from types import ModuleType

_VERSIONS = Path(__file__).resolve().parents[3] / "src" / "modulo" / "db" / "migrations" / "versions"
_MIGRATION_NAME = "0206_deleted_defaults_signal_check"
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


def test_metadata_unchanged() -> None:
    module = _load_migration()
    assert module.revision == _MIGRATION_NAME
    # Reads the migration directly instead of running alembic (no DB needed):
    # the change chains onto then-head 0205_library_collection_type.
    assert module.down_revision == "0205_library_collection_type"
    assert module.branch_labels is None
    assert module.depends_on is None


def test_upgrade_and_downgrade_are_callable() -> None:
    module = _load_migration()
    assert callable(module.upgrade)
    assert callable(module.downgrade)


def test_target_constants_match_the_orm_declaration() -> None:
    """The single-target constants must match the ORM's CheckConstraint."""
    module = _load_migration()
    assert module._TABLE == "deleted_defaults"
    assert module._CHECK_NAME == "ck_deleted_defaults_signal_nonempty"
    assert module._CHECK_EXPRESSION == "signal <> ''"


def test_constraint_name_is_defined_once_and_referenced_everywhere() -> None:
    """The CHECK name is a SINGLE string literal (definition site loses 4:
    definition + upgrade ADD + upgrade VALIDATE + downgrade DROP), and every
    SQL site references the constant — so a rename can never desynchronise one
    dangling literal."""
    # The docstring legitimately quotes the name (explaining it), so the
    # single-literal check runs against executable code only.
    raw_source = _source_code()
    assert raw_source.count("ck_deleted_defaults_signal_nonempty") == 1
    # definition + pg_constraint guard + upgrade ADD + upgrade VALIDATE + downgrade DROP.
    assert raw_source.count("_CHECK_NAME") == 5


def test_upgrade_uses_not_valid_then_validate_building_from_constants() -> None:
    """Deploy safety (the exact bug class 015x migrations prevent): a populated
    table never aborts at ADD time — the CHECK is added NOT VALID (no row scan
    at ADD time) then VALIDATE-d online. The f-string SQL MUST be built FROM
    the module constants (single source of truth) so the constants can never
    drift from the SQL actually executed."""
    code = _source_code()
    assert "ADD CONSTRAINT {_CHECK_NAME} CHECK ({_CHECK_EXPRESSION}) NOT VALID" in code
    assert "VALIDATE CONSTRAINT {_CHECK_NAME}" in code


def test_upgrade_is_idempotent_via_state_aware_pg_constraint_guard() -> None:
    """A partially-applied run must not fail with 'constraint already exists'
    (the 0157 pg_constraint guard precedent) AND must not leave the constraint
    silently NOT VALID forever — the guard is state-aware and schema-pinned."""
    code = _source_code()
    assert "SELECT convalidated FROM pg_constraint" in code
    assert "WHERE conname = :name AND conrelid = 'public.deleted_defaults'::regclass" in code
    assert "existing is True" in code
    assert "existing is None" in code
    # A pre-existing-but-unvalidated constraint replays as VALIDATE only.
    assert code.index("if existing is True") < code.index("if existing is None")


def test_downgrade_drops_the_same_constraint() -> None:
    code = _source_code()
    assert "DROP CONSTRAINT IF EXISTS {_CHECK_NAME}" in code


def test_non_postgres_branch_is_a_no_op_guard() -> None:
    """SQLite builds schema from the ORM (0141's branch rationale); the
    non-Postgres branch must return BEFORE any dialect-specific SQL."""
    code = _source_code()
    guard = code.index('if bind.dialect.name != "postgresql"')
    upgrade_sql = code.index("ALTER TABLE")
    assert guard < upgrade_sql
