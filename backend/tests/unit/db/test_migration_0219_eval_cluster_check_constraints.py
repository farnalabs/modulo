"""Unit tests for migration 0219_eval_cluster_check_constraints.

Structural: load the migration module and assert its contract without a
database. These pin the deploy-safety and de-duplication behaviour the PR
reviewer required:

* The migration adds ONLY the eval-cluster CHECKs that are NOT already
  enforced by ``0157_add_numeric_check_constraints``. In particular it must
  NOT re-add ``pass_threshold BETWEEN 0 AND 1`` (``ck_eval_definitions_pass_threshold``),
  ``minimum_delta BETWEEN 0 AND 1`` (``ck_eval_suites_minimum_delta``), or
  ``claimed_cost >= 0`` (``ck_eval_suite_runs_claimed_cost``) — doing so
  duplicates an existing constraint under a new name, so the same columns pay
  for redundant checks on every INSERT/UPDATE.
* The upgrade must be idempotent via a ``pg_constraint`` existence guard, NOT
  a bare ``op.create_check_constraint`` (which emits ``ALTER TABLE ... ADD
  CONSTRAINT`` with no ``IF NOT EXISTS`` and is not re-runnable after a partial
  failure). The docstring must not falsely claim ``IF NOT EXISTS`` idempotency.
* The downgrade drops every constraint by name guarded with ``IF EXISTS``.

They run without a database.
"""

import importlib.util
from pathlib import Path
from types import ModuleType

_VERSIONS = Path(__file__).resolve().parents[3] / "src" / "modulo" / "db" / "migrations" / "versions"
_MIGRATION_NAME = "0219_eval_cluster_check_constraints"
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
    assert module.down_revision == "0218_eval_cluster_indexes"
    assert module.branch_labels is None
    assert module.depends_on is None


def test_constraint_contract_has_nine_constraints() -> None:
    module = _load_migration()
    expected = {
        ("ck_eval_definitions_version_gte_1", "eval_definitions"),
        ("ck_eval_suites_baseline_window_gte_1", "eval_suites"),
        ("ck_eval_suites_cooldown_gte_0", "eval_suites"),
        ("ck_eval_suites_version_gte_1", "eval_suites"),
        ("ck_eval_datasets_version_gte_1", "eval_datasets"),
        ("ck_suite_runs_version_gte_0", "suite_runs"),
        ("ck_suite_runs_dataset_version_gte_1", "suite_runs"),
        ("ck_suite_runs_total_cost_non_negative", "suite_runs"),
        ("ck_suite_runs_case_counts_consistent", "suite_runs"),
    }
    assert {(name, table) for name, table, _expr in module._CONSTRAINTS} == expected


def test_no_duplicate_of_0157_constraints() -> None:
    """0219 must not re-add the [0,1] range / claimed_cost checks from 0157.

    0157 already enforces pass_threshold (ck_eval_definitions_pass_threshold),
    minimum_delta (ck_eval_suites_minimum_delta), and claimed_cost
    (ck_eval_suite_runs_claimed_cost) under those exact names.
    """
    module = _load_migration()
    names = {name for name, _table, _expr in module._CONSTRAINTS}
    for forbidden in (
        "ck_eval_definitions_pass_threshold",
        "ck_eval_definitions_pass_threshold_range",
        "ck_eval_suites_minimum_delta",
        "ck_eval_suites_minimum_delta_range",
        "ck_eval_suite_runs_claimed_cost",
    ):
        assert forbidden not in names, f"0219 must not duplicate 0157 constraint {forbidden}"

    # The cost constraint must cover only total_cost_usd, not claimed_cost.
    cost_exprs = [expr for name, table, expr in module._CONSTRAINTS if name == "ck_suite_runs_total_cost_non_negative"]
    assert len(cost_exprs) == 1, "exactly one cost constraint expected"
    assert "claimed_cost" not in cost_exprs[0], "claimed_cost is already enforced by 0157"
    assert "total_cost_usd" in cost_exprs[0]


def test_upgrade_is_idempotent_via_pg_constraint_guard() -> None:
    """Upgrade must guard each ADD CONSTRAINT with a pg_constraint existence
    check (like 0157/0153/0110), NOT a bare op.create_check_constraint.

    A bare ``op.create_check_constraint`` emits ALTER TABLE ... ADD CONSTRAINT
    with no IF NOT EXISTS, so a partially-applied run fails on re-run.
    """
    code = _source_code()
    assert "pg_constraint" in code, "upgrade must guard with a pg_constraint existence check"
    assert "op.create_check_constraint" not in code, "bare op.create_check_constraint is not idempotent-safe"
    assert "ADD CONSTRAINT {name} CHECK" in code, "upgrade must add the CHECK guarded by name"


def test_docstring_does_not_falsely_claim_if_not_exists() -> None:
    """The docstring must not claim a plain IF NOT EXISTS idempotency that
    op.create_check_constraint does not provide.
    """
    source = _MIGRATION_PATH.read_text(encoding="utf-8")
    docstring = source.split('"""', 2)[1]
    assert "IF NOT EXISTS" not in docstring, "docstring must not falsely claim IF NOT EXISTS idempotency"
    assert "pg_constraint" in source, "migration must document the pg_constraint guard it uses"


def test_downgrade_drops_all_constraints_guarded() -> None:
    code = _source_code()
    assert "for name, table, _ in reversed(_CONSTRAINTS)" in code
    assert "DROP CONSTRAINT IF EXISTS {name}" in code, "downgrade must drop each CHECK guarded"
