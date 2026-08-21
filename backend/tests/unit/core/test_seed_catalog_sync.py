"""Seed catalog sync tests — catalog.FLAGS must mirror feature_flags._KNOWN_FLAGS.

The startup seed (``_seed_tier_catalog``) inserts ``catalog.FLAGS`` into
``feature_flag_catalog`` with ``ON CONFLICT (name) DO NOTHING``, and
``FeatureFlagRegistry.load_from_db()`` replaces its flags with ONLY the
DB-backed rows. Any flag present in ``_KNOWN_FLAGS`` but missing from
``catalog.FLAGS`` silently vanishes from the registry, locking its feature
behind the gate even on the right tier. These tests prevent that drift.
"""

from modulo.core.feature_flags import _KNOWN_FLAGS
from modulo.core.seed_data.catalog import FLAGS

# Flags added to the seed catalog in FAR-114 (previously absent from the DB seed).
_SYNCED_FLAGS: set[str] = {
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


def _seed_by_name() -> dict[str, dict[str, object]]:
    return {entry["name"]: entry for entry in FLAGS}


class TestSeedCatalogMirror:
    def test_seed_catalog_contains_every_known_flag(self) -> None:
        known = {flag.name for flag in _KNOWN_FLAGS}
        seeded = set(_seed_by_name())
        missing = sorted(known - seeded)
        assert not missing, f"catalog.FLAGS is missing flags from _KNOWN_FLAGS: {missing}"

    def test_seed_catalog_tiers_match_known_flags(self) -> None:
        known_tiers = {flag.name: flag.tier for flag in _KNOWN_FLAGS}
        for name, entry in _seed_by_name().items():
            assert name in known_tiers, f"catalog.FLAGS has unexpected flag {name}"
            assert entry["tier_id"] == known_tiers[name], (
                f"catalog.FLAGS tier for {name} is {entry['tier_id']!r}, _KNOWN_FLAGS says {known_tiers[name]!r}"
            )

    def test_seed_catalog_has_no_duplicate_names(self) -> None:
        names = [entry["name"] for entry in FLAGS]
        assert len(names) == len(set(names)), "catalog.FLAGS contains duplicate names"


class TestFar114SyncedFlags:
    def test_all_synced_flags_are_seeded(self) -> None:
        seeded = set(_seed_by_name())
        missing = sorted(_SYNCED_FLAGS - seeded)
        assert not missing, f"FAR-114 flags missing from catalog.FLAGS: {missing}"

    def test_synced_flag_tiers(self) -> None:
        seeded = _seed_by_name()
        team_flags = {
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
        }
        community_flags = _SYNCED_FLAGS - team_flags
        for name in team_flags:
            assert seeded[name]["tier_id"] == "team", f"{name} should be team tier"
        for name in community_flags:
            assert seeded[name]["tier_id"] == "community", f"{name} should be community tier"
