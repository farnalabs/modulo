"""Unit tests for migration 0191_bundled_runner_seed_backfill (FAR-702 / FAR-590 D4).

Source-only (no database): assert the migration's contract by inspecting its
source and module metadata:

* the chain is pinned (revision -> 0190_hitl_claim_context_json);
* the **upgrade** captures the pre-migration identity of the legacy
  ``modulo-dev`` re-point rows into a scratch table
  (``_migration_0191_repoint_state``) so the downgrade can target EXACTLY
  those rows;
* the **per-org backfill INSERT supplies ``id`` explicitly via
  ``gen_random_uuid()``**. This is THE regression test for FAR-702: the
  legacy-baseline ``environment_profiles`` table has no server default on its
  uuid primary key, so a raw-SQL INSERT without ``id`` fails with
  ``NotNullViolation: null value in column "id"`` — which blocked every prod
  and staging deploy at the pre-migrate step since 2026-09-08 00:21 UTC. The
  migration has never applied successfully on ANY environment, so repairing it
  in place (revision id and down_revision unchanged) is safe;
* the **downgrade** reverts ONLY the captured re-point rows (by primary key,
  restoring their original description / config_json) and does NOT match on the
  post-upgrade shape (``name = template AND provider_type = runner_docker AND
  image_ref = template``), which would wrongly relabel every per-org BACKFILL
  row (FAR-590 D4 negative guards).

A DB-backed execution test lives in
``backend/tests/integration/db/test_migration_0191_*.py`` (testcontainers),
following the established repo pattern for deploy-blocking migrations.

They run without a database.
"""

import importlib.util
from pathlib import Path
from types import ModuleType

_VERSIONS = Path(__file__).resolve().parents[3] / "src" / "modulo" / "db" / "migrations" / "versions"
_MIGRATION_NAME = "0191_bundled_runner_seed_backfill"
_MIGRATION_PATH = _VERSIONS / f"{_MIGRATION_NAME}.py"
_STATE_TABLE = "_migration_0191_repoint_state"


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


def test_upgrade_captures_repoint_identity() -> None:
    full = _source_code()
    code = full.split("def upgrade", 1)[1].split("def downgrade", 1)[0]
    # The pre-upgrade identity of the modulo-dev re-points is captured into a
    # scratch table so the downgrade can target EXACTLY those rows. The table is
    # created by the _create_repoint_state_table helper (called from upgrade),
    # whose literal DDL lives outside the upgrade slice, hence checked against
    # the whole module source (``full``) not just the slice.
    assert f"CREATE TABLE IF NOT EXISTS {_STATE_TABLE}" in full
    assert "_create_repoint_state_table(bind)" in code
    assert f"INSERT INTO {_STATE_TABLE} (profile_id, prev_name, prev_description, prev_config_json)" in code
    assert "'modulo-dev'" in code
    assert "'local_docker'" in code


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


def test_downgrade_reverts_only_captured_rows() -> None:
    code = _source_code().split("def downgrade", 1)[1]
    # The downgrade joins onto the scratch table by primary key and restores the
    # original name / description / config_json.
    assert f"FROM {_STATE_TABLE} s" in code
    assert "WHERE ep.id = s.profile_id" in code
    assert "name = s.prev_name" in code
    assert "description = s.prev_description" in code
    assert "config_json = s.prev_config_json" in code
    assert f"DROP TABLE IF EXISTS {_STATE_TABLE}" in code
    # FAR-590 D4 negative guards: the downgrade must NOT re-point on the
    # post-upgrade shape (which also matches the per-org backfill rows) — that
    # would relabel every backfilled profile to local_docker / modulo-dev and
    # leave their description / config_json unrestored.
    assert "provider_type = 'local_docker', name = 'modulo-dev'" not in code
    assert "WHERE name = :tpl_name AND provider_type = 'runner_docker'" not in code
