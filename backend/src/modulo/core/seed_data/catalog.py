"""Default tier catalog and feature flag definitions for seeding.

These constants seed the DB tables tier_catalog and feature_flag_catalog
at application startup.  They are seed data only — the runtime feature flag
registry lives in modulo.core.feature_flags.

``FLAGS`` is now DERIVED from ``_KNOWN_FLAGS`` (the single source of truth).
Adding a flag requires only a ``FeatureFlag(...)`` entry in
``feature_flags._KNOWN_FLAGS``; the catalog entry is generated automatically.
"""

from modulo.core.feature_flags import _KNOWN_FLAGS

TIERS: list[dict[str, str | int | bool]] = [
    {
        "tier_id": "community",
        "label": "Community",
        "rank": 0,
        "requires_license": False,
        "description": "Free tier, no license key required",
    },
    {
        "tier_id": "team",
        "label": "Team",
        "rank": 1,
        "requires_license": True,
        "description": "Self-serve paid tier with team features",
    },
]

# Derived from feature_flags._KNOWN_FLAGS — the single source of truth.
# Each flag's name/description/tier/depends_on is declared once in
# _KNOWN_FLAGS; this list is generated automatically so the DB seed
# catalog stays in sync without manual duplication.
FLAGS: list[dict[str, str | None]] = [
    {
        "name": flag.name,
        "description": flag.description,
        "tier_id": flag.tier,
        "depends_on": flag.depends_on,
    }
    for flag in _KNOWN_FLAGS
]
