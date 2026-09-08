"""Structural tests for migration 0192_uuid_pk_server_defaults (FAR-718).

Source-only (no database): the migration's statement list must be FROZEN —
enumerated from the SQLAlchemy metadata at authoring time and committed as
explicit literals — and must exactly cover every uuid primary key (FK-parent
columns excluded). A runtime-enumerated list would silently change on future
model edits, so the migration source must not inspect metadata at all.
"""

import importlib.util
from pathlib import Path
from types import ModuleType

from sqlalchemy import Uuid
from sqlalchemy.dialects.postgresql import UUID as POSTGRES_UUID

from modulo.db.models import Base

_VERSIONS = Path(__file__).resolve().parents[3] / "src" / "modulo" / "db" / "migrations" / "versions"
_MIGRATION_NAME = "0192_uuid_pk_server_defaults"
_MIGRATION_PATH = _VERSIONS / f"{_MIGRATION_NAME}.py"

_EXPECTED_COUNT = 82


def _load_migration() -> ModuleType:
    assert _MIGRATION_PATH.exists(), f"Migration file missing: {_MIGRATION_PATH}"
    spec = importlib.util.spec_from_file_location(f"migration_{_MIGRATION_NAME}", _MIGRATION_PATH)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _source_code() -> str:
    return _MIGRATION_PATH.read_text(encoding="utf-8")


def _alter_pairs(source: str, action: str) -> set[tuple[str, str]]:
    """Extract (table, column) pairs from ``ALTER TABLE <t> ALTER COLUMN <c> <action>`` lines."""
    pairs: set[tuple[str, str]] = set()
    for line in source.splitlines():
        stripped = line.strip()
        prefix = "ALTER TABLE "
        if not stripped.startswith(f'"{prefix}'):
            continue
        parts = stripped.strip('",').split()
        # parts: ["ALTER", "TABLE", <table>, "ALTER", "COLUMN", <col>, <action>, ...]
        if len(parts) >= 8 and parts[3] == "ALTER" and parts[4] == "COLUMN" and parts[6] == action:
            pairs.add((parts[2], parts[5]))
    return pairs


def _metadata_uuid_pk_pairs() -> set[tuple[str, str]]:
    """(table, column) for every uuid primary key that is not a foreign-key parent."""
    pairs: set[tuple[str, str]] = set()
    for table in Base.metadata.sorted_tables:
        fk_parents = {fk.parent.name for fk in table.foreign_keys}
        for column in table.columns:
            if column.primary_key and isinstance(column.type, (Uuid, POSTGRES_UUID)) and column.name not in fk_parents:
                pairs.add((table.name, column.name))
    return pairs


def test_metadata_pins_chain() -> None:
    module = _load_migration()
    assert module.revision == _MIGRATION_NAME
    assert module.down_revision == "0191_bundled_runner_seed_backfill"
    assert module.branch_labels is None
    assert module.depends_on is None


def test_migration_source_is_frozen_not_runtime_enumerated() -> None:
    source = _source_code()
    # The statement list must never be derived from runtime metadata — a
    # dynamic enumeration would silently drift with future model edits.
    assert "Base.metadata" not in source
    assert "sorted_tables" not in source
    assert "inspect(" not in source


def test_upgrade_covers_exactly_every_metadata_uuid_pk() -> None:
    _load_migration()  # the migration module must import cleanly
    upgrade_pairs = _alter_pairs(_source_code(), "SET")
    metadata_pairs = _metadata_uuid_pk_pairs()
    assert len(metadata_pairs) == _EXPECTED_COUNT, f"metadata uuid-PK count drifted: {len(metadata_pairs)}"
    assert len(upgrade_pairs) == _EXPECTED_COUNT, f"migration SET statements: {sorted(upgrade_pairs)}"
    assert upgrade_pairs == metadata_pairs
    missing = metadata_pairs - upgrade_pairs
    assert not missing, f"metadata uuid PKs missing from upgrade: {sorted(missing)}"
    extra = upgrade_pairs - metadata_pairs
    assert not extra, f"upgrade alters non-uuid-PK columns: {sorted(extra)}"


def test_upgrade_defaults_are_gen_random_uuid_only() -> None:
    source = _source_code()
    set_lines = [line.strip() for line in source.splitlines() if "SET DEFAULT gen_random_uuid()" in line]
    assert len(set_lines) == _EXPECTED_COUNT


def test_downgrade_mirrors_upgrade_exactly() -> None:
    upgrade_pairs = _alter_pairs(_source_code(), "SET")
    downgrade_pairs = _alter_pairs(_source_code(), "DROP")
    assert len(downgrade_pairs) == _EXPECTED_COUNT
    assert downgrade_pairs == upgrade_pairs
    # Statement lines only (comments mention the phrase and must not count).
    statement_lines = [line.strip() for line in _source_code().splitlines() if line.strip().startswith('"ALTER TABLE ')]
    drop_lines = [line for line in statement_lines if "DROP DEFAULT" in line]
    setters = [line for line in drop_lines if "SET DEFAULT" in line]
    assert not setters, f"downgrade contains SET statements: {setters}"


def test_selectivity_excludes_non_uuid_and_fk_pks() -> None:
    metadata_pairs = _metadata_uuid_pk_pairs()
    # String primary keys must never receive a uuid default.
    assert ("tier_catalog", "tier_id") not in metadata_pairs
    assert ("tier_catalog", "name") not in metadata_pairs
    assert ("oauth_authorization_codes", "code") not in metadata_pairs
    # Composite-PK columns that are also FK parents (run_evidence) are excluded.
    assert ("run_evidence", "run_id") not in metadata_pairs
    assert ("run_evidence", "node_id") not in metadata_pairs
