"""Unit tests for migration 0269_webhook_dedup_check_constraints.

Structural: load the migration module and assert its contract without a
database:

* the chain is pinned (revision -> 0268_webhook_lookup_expiry_indexes) so the
  pre-commit check-migration-heads hook can never be ambushed by a renumber;
* the upgrade adds three CHECK constraints ``NOT VALID`` then ``VALIDATE``-d,
  mirroring the 0151/0165 online-constraint pattern;
* the downgrade drops the same three constraints.

They run without a database.
"""

import importlib.util
from pathlib import Path
from types import ModuleType

_VERSIONS = Path(__file__).resolve().parents[3] / "src" / "modulo" / "db" / "migrations" / "versions"
_MIGRATION_NAME = "0269_webhook_dedup_check_constraints"
_MIGRATION_PATH = _VERSIONS / f"{_MIGRATION_NAME}.py"

_CONSTRAINTS = (
    "ck_webhook_dedup_hash_length",
    "ck_webhook_dedup_expires_ordering",
    "ck_webhook_payload_expires_ordering",
)


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
    assert module.down_revision == "0268_webhook_lookup_expiry_indexes"
    assert module.branch_labels is None
    assert module.depends_on is None


def test_upgrade_adds_each_check_not_valid_then_validates() -> None:
    code = _source_code()
    for name in _CONSTRAINTS:
        assert f"ADD CONSTRAINT {name} CHECK" in code, f"upgrade missing {name}"
        assert f"VALIDATE CONSTRAINT {name}" in code, f"upgrade missing validate {name}"
    # Every ADD is paired with a VALIDATE, so a populated table never aborts the
    # upgrade on the ADD's row scan.
    assert "NOT VALID" in code
    assert code.count("ADD CONSTRAINT") == len(_CONSTRAINTS)
    assert code.count("VALIDATE CONSTRAINT") == len(_CONSTRAINTS)
    # The upgrade must execute the pairs, not merely declare them.
    assert "_CHECKS" in code.split("def upgrade", 1)[1]


def test_downgrade_drops_each_check() -> None:
    code = _source_code()
    for name in _CONSTRAINTS:
        assert f'op.drop_constraint("{name}"' in code, f"downgrade missing {name}"
    assert code.count("op.drop_constraint(") == len(_CONSTRAINTS)


def test_checks_target_the_right_tables() -> None:
    code = _source_code()
    assert "webhook_dedup_hashes ADD CONSTRAINT ck_webhook_dedup_hash_length" in code
    assert "webhook_dedup_hashes ADD CONSTRAINT ck_webhook_dedup_expires_ordering" in code
    assert "webhook_payloads ADD CONSTRAINT ck_webhook_payload_expires_ordering" in code
