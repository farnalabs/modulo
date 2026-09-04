"""Unit tests for migration 0176_env_profiles_runner_docker (FAR-587).

Structural: load the migration module and assert its contract without a
database, and pin model/migration parity for the provider_type vocabulary:

* the chain is pinned (revision -> 0174_per_org_last_admin_guard, the true
  head — 0175 is spliced mid-chain);
* the upgrade widens ``ck_env_profiles_provider_type`` with 'runner_docker'
  (guarded ``DROP CONSTRAINT IF EXISTS`` + re-add) and drops the legacy
  ``'local_docker'`` server default (guarded: only when the default is the
  legacy value, so an operator-set default is never removed);
* the downgrade re-points ``runner_docker`` rows to the legacy alias before
  narrowing the CHECK and restores the default;
* the model declares the widened CHECK and carries no ``server_default``.

They run without a database.
"""

import importlib.util
from pathlib import Path
from types import ModuleType

_VERSIONS = Path(__file__).resolve().parents[3] / "src" / "modulo" / "db" / "migrations" / "versions"
_MIGRATION_NAME = "0176_env_profiles_runner_docker"
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
    assert module.down_revision == "0174_per_org_last_admin_guard"
    assert module.branch_labels is None
    assert module.depends_on is None


def test_upgrade_widens_check_and_keeps_legacy_values() -> None:
    code = _source_code().split("def upgrade", 1)[1].split("def downgrade", 1)[0]
    assert "ck_env_profiles_provider_type" in code
    assert "DROP CONSTRAINT IF EXISTS ck_env_profiles_provider_type" in code
    assert "'runner_docker'::character varying" in code
    # Existing vocabulary is preserved — nothing is re-pointed on upgrade.
    assert "'local_docker'::character varying" in code
    assert "'e2b'::character varying" in code
    assert "'local'::character varying" in code


def test_upgrade_drops_default_with_legacy_value_guard() -> None:
    code = _source_code().split("def upgrade", 1)[1].split("def downgrade", 1)[0]
    assert "ALTER COLUMN provider_type DROP DEFAULT" in code
    # The drop is guarded on the exact legacy expression — an operator-set
    # default is never silently removed.
    assert "'''local_docker''::character varying" in code


def test_downgrade_repoints_runner_docker_and_restores_guarded() -> None:
    code = _source_code().split("def downgrade", 1)[1]
    # Re-point BEFORE narrowing the vocabulary (semantics-preserving alias).
    repoint_at = code.index("SET provider_type = 'local_docker'")
    narrow_at = code.index("DROP CONSTRAINT IF EXISTS ck_env_profiles_provider_type")
    assert repoint_at < narrow_at
    assert "WHERE provider_type = 'runner_docker'" in code
    # Guarded restore of the CHECK (only when absent) and of the default.
    assert "IF NOT EXISTS" in code
    assert "SET DEFAULT 'local_docker'::character varying" in code


def test_model_parity_widened_check_no_server_default() -> None:
    from modulo.db.models.environment_profile import EnvironmentProfile

    constraint = next(
        c for c in EnvironmentProfile.__table_args__ if getattr(c, "name", None) == "ck_env_profiles_provider_type"
    )
    sql = str(getattr(constraint, "sqltext", constraint))
    assert "runner_docker" in sql
    assert "local_docker" in sql

    column = EnvironmentProfile.__table__.columns["provider_type"]
    assert column.nullable is False
    assert column.server_default is None, (
        "model/migration drift: the DB default was dropped by 0176; the model must not re-introduce it"
    )
