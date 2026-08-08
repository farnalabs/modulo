"""Name-sync test for the 0072_sync_feature_flag_catalog migration.

The migration upserts flags into ``feature_flag_catalog`` that the seed catalog
(``modulo.core.seed_data.catalog.FLAGS``) previously missed. If a flag is added
to ``_KNOWN_FLAGS`` (or to ``catalog.FLAGS``) without updating the migration's
``_FLAGS`` dict, existing deployments seeded with ``ON CONFLICT DO NOTHING``
never pick it up. This test keeps the migration's flag list in sync with the
source of truth.
"""

import importlib.util
from pathlib import Path
from types import ModuleType

import pytest
from alembic.script import ScriptDirectory

from modulo.core.feature_flags import _KNOWN_FLAGS
from modulo.core.seed_data.catalog import FLAGS

_MIGRATION_NAME = "0072_sync_feature_flag_catalog"
_MIGRATION_PATH = (
    Path(__file__).resolve().parents[3] / "src" / "modulo" / "db" / "migrations" / "versions" / f"{_MIGRATION_NAME}.py"
)

# Flags the migration must upsert (the FAR-114 sync set).
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


@pytest.fixture(scope="module")
def migration() -> ModuleType:
    assert _MIGRATION_PATH.exists(), f"Migration file missing: {_MIGRATION_PATH}"
    spec = importlib.util.spec_from_file_location(f"migration_{_MIGRATION_NAME}", _MIGRATION_PATH)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _versions_dir() -> Path:
    return _MIGRATION_PATH.parent


def _migrations_dir() -> Path:
    return _versions_dir().parent


def _script() -> ScriptDirectory:
    return ScriptDirectory(str(_migrations_dir()))


class TestMigrationFlagSync:
    def test_migration_upserts_expected_flag_set(self, migration: ModuleType) -> None:
        assert set(migration._FLAGS) == _EXPECTED_FLAGS, (
            "migration _FLAGS must cover exactly the FAR-114 sync set "
            f"(missing: {sorted(_EXPECTED_FLAGS - set(migration._FLAGS))}, "
            f"extra: {sorted(set(migration._FLAGS) - _EXPECTED_FLAGS)})"
        )

    def test_migration_flag_tiers_match_known_flags(self, migration: ModuleType) -> None:
        known_tiers = {flag.name: flag.tier for flag in _KNOWN_FLAGS}
        for name, (tier_id, _description) in migration._FLAGS.items():
            assert known_tiers[name] == tier_id, (
                f"migration tier for {name} is {tier_id!r}, _KNOWN_FLAGS says {known_tiers[name]!r}"
            )

    def test_migration_flags_are_still_known(self, migration: ModuleType) -> None:
        known = {flag.name for flag in _KNOWN_FLAGS}
        unknown = sorted(set(migration._FLAGS) - known)
        assert not unknown, f"migration references flags removed from _KNOWN_FLAGS: {unknown}"

    def test_migration_is_head_and_revises_existing_revision(self, migration: ModuleType) -> None:
        script = _script()
        assert migration.revision in script.get_heads(), (
            f"migration {migration.revision} must be a head (no other migration revises it)"
        )
        existing = {rev.revision for rev in script.walk_revisions()}
        assert migration.down_revision in existing, (
            f"down_revision {migration.down_revision!r} must reference an existing migration revision"
        )


class TestSeedCatalogFlagSet:
    def test_seed_catalog_contains_the_17_synced_flags(self) -> None:
        seeded = {entry["name"] for entry in FLAGS}
        missing = sorted(_EXPECTED_FLAGS - seeded)
        assert not missing, f"catalog.FLAGS still missing FAR-114 flags: {missing}"
