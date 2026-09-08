"""Structural tests for migration 0191_bundled_runner_seed_backfill (FAR-590 D4).

Source-only (no database): assert the migration pins its chain and — critically —
that the **downgrade** reverts only the captured re-point rows (by primary key,
restoring their original description / config_json) and does NOT match on the
post-upgrade shape (``name = template AND provider_type = runner_docker AND
image_ref = template``), which would wrongly relabel every per-org BACKFILL row.
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
    # whose literal DDL lives outside the upgrade slice.
    assert f"CREATE TABLE IF NOT EXISTS {_STATE_TABLE}" in full
    assert "_create_repoint_state_table(bind)" in code
    assert f"INSERT INTO {_STATE_TABLE} (profile_id, prev_name, prev_description, prev_config_json)" in code
    assert "'modulo-dev'" in code
    assert "'local_docker'" in code


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
    # It must NOT re-point on the post-upgrade shape (which also matches the
    # per-org backfill rows) — that would relabel every backfilled profile.
    assert "provider_type = 'local_docker', name = 'modulo-dev'" not in code
    assert "WHERE name = :tpl_name AND provider_type = 'runner_docker'" not in code
