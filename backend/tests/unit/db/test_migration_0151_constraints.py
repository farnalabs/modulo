"""Unit tests for migration 0151_fix_constraints (improve-database).

Structural: load the migration module and assert its contract without a
database. These pin the deploy-safety behaviour the PR reviewer required:

* Every CHECK is added ``NOT VALID`` then ``VALIDATE``-d, so a populated table
  never aborts the upgrade if a historical row violates the constraint.
* The slug drop is guarded (``DROP CONSTRAINT IF EXISTS``) so a DB whose unique
  was created under a different name (e.g. via ``create_all``) does not
  hard-fail.
* The downgrade de-duplicates rows sharing a slug (``ROW_NUMBER() OVER
  (PARTITION BY slug ...)``) BEFORE re-creating the full ``organisations_slug_key``
  UNIQUE, mirroring 0127. After the upgrade's own use-case (an active row and a
  soft-deleted row coexisting on one slug) the un-guarded re-add would raise a
  unique-violation and abort the downgrade.
* The downgrade drops the partial index by name only (no ``postgresql_where``).

They run without a database.
"""

import importlib.util
from pathlib import Path
from types import ModuleType

_VERSIONS = Path(__file__).resolve().parents[3] / "src" / "modulo" / "db" / "migrations" / "versions"
_MIGRATION_NAME = "0151_fix_constraints"
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


def _downgrade_source() -> str:
    """Return just the body of the ``downgrade`` function (for scoped asserts)."""
    code = _source_code()
    return code.split("def downgrade", 1)[1]


def test_metadata_pins_chain() -> None:
    module = _load_migration()
    assert module.revision == _MIGRATION_NAME
    assert module.down_revision == "0150_add_router_no_match_status"
    assert module.branch_labels is None
    assert module.depends_on is None


def test_check_contract_has_fourteen_constraints() -> None:
    module = _load_migration()
    expected = {
        ("run_evidence", "ck_run_evidence_state"),
        ("pipeline_snapshots", "ck_pipeline_snapshots_version_kind"),
        ("pipeline_snapshots", "ck_pipeline_snapshots_created_kind"),
        ("pipeline_snapshots", "ck_pipeline_snapshots_channel"),
        ("oauth_authorization_codes", "ck_oauth_auth_codes_challenge_method"),
        ("organisations", "ck_organisations_cum_spend"),
        ("org_daily_run_counts", "ck_org_daily_run_counts_run_count"),
        ("org_daily_run_counts", "ck_org_daily_run_counts_total_spend"),
        ("org_daily_run_counts", "ck_org_daily_run_counts_refused_spend"),
        ("spend_anomalies", "ck_spend_anomalies_amount"),
        ("spend_anomalies", "ck_spend_anomalies_baseline"),
        ("library_primitives", "ck_library_primitives_dl_count"),
        ("library_primitives", "ck_library_primitives_review_count"),
        ("suite_runs", "ck_suite_runs_case_counts"),
    }
    assert {(table, name) for table, name, _expr in module._CHECKS} == expected


def test_upgrade_adds_checks_not_valid_then_validates() -> None:
    """CHECKs must be added NOT VALID (no row scan) then VALIDATE-d online.

    A plain ``op.create_check_constraint`` validates immediately and aborts the
    upgrade on any pre-existing violating row — the reviewer's deploy-time risk.
    """
    code = _source_code()
    assert "NOT VALID" in code, "CHECKs must be added NOT VALID"
    assert "VALIDATE CONSTRAINT" in code, "CHECKs must be VALIDATE-d online"


def test_upgrade_drops_slug_constraint_guarded() -> None:
    """The slug drop must be guarded (``IF EXISTS``), not a blind op.drop_constraint."""
    code = _source_code()
    assert "DROP CONSTRAINT IF EXISTS organisations_slug_key" in code
    assert "op.drop_constraint" not in code, "blind op.drop_constraint is not idempotent-safe"


def test_downgrade_de_dups_before_recreating_full_unique() -> None:
    """Downgrade must de-dup shared slugs before re-adding the full UNIQUE.

    Otherwise an active + soft-deleted row on one slug (the upgrade's own
    use-case) makes the re-add raise unique-violation and abort the downgrade.
    """
    code = _source_code()
    assert "ROW_NUMBER() OVER (PARTITION BY slug" in code, "downgrade must de-dup by slug"
    assert "create_unique_constraint" in code, "downgrade must restore organisations_slug_key"
    # The de-dup DELETE must appear before the full-unique re-creation.
    de_dup_pos = code.index("ROW_NUMBER() OVER (PARTITION BY slug")
    recreate_pos = code.index("create_unique_constraint")
    assert de_dup_pos < recreate_pos, "de-dup must run before re-creating the full UNIQUE"


def test_downgrade_drops_index_by_name_only() -> None:
    """Drop the partial index by name — no ``postgresql_where`` kwarg."""
    code = _downgrade_source()
    assert 'op.drop_index("uq_organisations_slug", table_name="organisations")' in code
    assert "postgresql_where" not in code, "drop_index must not carry postgresql_where"


def test_downgrade_drops_all_checks_guarded() -> None:
    code = _downgrade_source()
    # The downgrade iterates the CHECK contract and drops each guarded.
    assert "for table, name, _expr in _CHECKS" in code
    assert 'DROP CONSTRAINT IF EXISTS {name}"' in code, "downgrade must drop each CHECK guarded"
