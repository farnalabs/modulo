"""Name-sync test for the feature-flag catalog surface (final state).

The migration chain was squashed into three idempotent reconciliation
migrations. The old ``0072_sync_feature_flag_catalog`` migration (which
upserted flags into ``feature_flag_catalog`` that the seed catalog missed) and
the ``0105`` head no longer exist. The feature-flag catalog is now created by
the reconciliation chain (``0109_schema_teams_library`` adds the table
columns) and populated at application startup from
``modulo.core.seed_data.catalog.FLAGS``. These tests assert:

* the reconciliation chain creates the ``feature_flag_catalog`` columns the
  seed path writes to,
* the seed catalog covers the full ``_KNOWN_FLAGS`` set — a flag added to
  ``_KNOWN_FLAGS`` without a matching ``catalog.FLAGS`` entry (and a startup
  seed that upserts it) never appears for existing deployments,
* every expected flag is present in the seed catalog.
"""

import importlib.util
from pathlib import Path
from types import ModuleType

from alembic.script import ScriptDirectory

from modulo.core.feature_flags import _KNOWN_FLAGS
from modulo.core.seed_data.catalog import FLAGS

_HEAD_MIGRATION_NAME = "0150_add_router_no_match_status"
_HEAD_MIGRATION_PATH = (
    Path(__file__).resolve().parents[3]
    / "src"
    / "modulo"
    / "db"
    / "migrations"
    / "versions"
    / (f"{_HEAD_MIGRATION_NAME}.py")
)

# Flags the old 0072 sync migration upserted (the FAR-114 sync set). The seed
# catalog must still cover all of them.
_EXPECTED_FLAGS: set[str] = {
    "error_tracking",
    "runtime_config",
    "rate_limits",
    "email_config",
    "scim",
    "external_secrets",
    "checkpoint_encryption",
    "audit_crypto_chain",
    "community_registry",
    "prompt_optimization",
    "pipeline_diff_rollback",
    "pipeline_delete",
    "schema_union_types",
    "migration_cli",
    "notification_log",
    "api_changelog",
    "web_vitals_analytics",
}

# The reconciliation migration that adds the feature_flag_catalog columns.
_CATALOG_MIGRATION_NAME = "0109_schema_teams_library"
_CATALOG_MIGRATION_PATH = (
    Path(__file__).resolve().parents[3]
    / "src"
    / "modulo"
    / "db"
    / "migrations"
    / "versions"
    / (f"{_CATALOG_MIGRATION_NAME}.py")
)


def _load_migration(name: str, path: Path) -> ModuleType:
    assert path.exists(), f"Migration file missing: {path}"
    spec = importlib.util.spec_from_file_location(f"migration_{name}", path)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _script() -> ScriptDirectory:
    return ScriptDirectory(str(_HEAD_MIGRATION_PATH.parent.parent))


class TestReconciliationChain:
    def test_single_head_is_0008(self) -> None:
        script = _script()
        assert script.get_heads() == [_HEAD_MIGRATION_NAME], (
            f"expected a single head {_HEAD_MIGRATION_NAME}, got {script.get_heads()}"
        )

    def test_0007_creates_feature_flag_catalog_columns(self) -> None:
        """The reconciliation chain must create the columns the startup seed
        writes to; a fresh DB missing any of them fails the seed upsert."""
        path = _CATALOG_MIGRATION_PATH
        assert path.exists(), f"Migration file missing: {path}"
        source = path.read_text(encoding="utf-8")
        for column in ("name", "description", "tier_id", "depends_on", "is_active"):
            assert f'ADD COLUMN IF NOT EXISTS "{column}"' in source, f"0007 missing feature_flag_catalog.{column}"


class TestSeedCatalogFlagSet:
    def test_seed_catalog_covers_every_known_flag(self) -> None:
        """Every runtime-flagged feature must have a seed catalog entry — the
        seed (startup upsert) is the only thing that makes a flag available on
        already-deployed DBs."""
        seeded = {entry["name"] for entry in FLAGS}
        known = {flag.name for flag in _KNOWN_FLAGS}
        missing = sorted(known - seeded)
        assert not missing, f"catalog.FLAGS missing entries for known flags: {missing}"

    def test_seed_catalog_contains_the_17_synced_flags(self) -> None:
        seeded = {entry["name"] for entry in FLAGS}
        missing = sorted(_EXPECTED_FLAGS - seeded)
        assert not missing, f"catalog.FLAGS still missing FAR-114 flags: {missing}"
