"""Unit tests for migration 0191_bundled_runner_seed_backfill (FAR-702).

Structural: load the migration module and assert its contract without a
database:

* the chain is pinned (revision -> 0190_hitl_claim_context_json);
* the per-org backfill INSERT supplies ``id`` explicitly via
  ``gen_random_uuid()``. This is THE regression test for FAR-702: the
  legacy-baseline ``environment_profiles`` table has no server default on its
  uuid primary key, so a raw-SQL INSERT without ``id`` fails with
  ``NotNullViolation: null value in column "id"`` — which blocked every prod
  and staging deploy at the pre-migrate step since 2026-09-08 00:21 UTC. The
  migration has never applied successfully on ANY environment, so repairing
  it in place (revision id and down_revision unchanged) is safe;
* the downgrade reverts exactly the rows captured at upgrade time
  (``_migration_0191_repoint_state`` scratch table), restoring
  prev_name / prev_description / prev_config_json.

They run without a database.
"""

import importlib.util
from pathlib import Path
from types import ModuleType

_VERSIONS = Path(__file__).resolve().parents[3] / "src" / "modulo" / "db" / "migrations" / "versions"
_MIGRATION_NAME = "0191_bundled_runner_seed_backfill"
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
    assert module.down_revision == "0190_hitl_claim_context_json"
    assert module.branch_labels is None
    assert module.depends_on is None


def test_backfill_insert_supplies_id() -> None:
    code = _source_code().split("def upgrade", 1)[1].split("def downgrade", 1)[0]
    insert_at = code.index("INSERT INTO environment_profiles")
    columns_at = code.index("(", insert_at)
    select_at = code.index(")", columns_at)
    column_list = code[columns_at + 1 : select_at]
    columns = [c.strip() for c in column_list.split(",")]
    # The regression: `id` must be a listed column (checked exactly — a bare
    # "id," substring would false-positive on "organisation_id,"), and the
    # SELECT must supply a value for it via the PG13+ built-in
    # gen_random_uuid().
    assert "id" in columns, "backfill INSERT column list must include `id`"
    assert columns.index("id") == 0, "`id` must be the FIRST listed column"
    assert "gen_random_uuid()" in code, "backfill SELECT must supply gen_random_uuid()"


def test_downgrade_reverts_repointed_rows() -> None:
    code = _source_code().split("def downgrade", 1)[1]
    assert "_migration_0191_repoint_state" in code
    assert "prev_name" in code
    assert "prev_description" in code
    assert "prev_config_json" in code
