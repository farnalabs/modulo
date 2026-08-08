"""Sync feature flag catalog with all known feature flags

Revision ID: 0072_sync_feature_flag_catalog
Revises: 0071_analytics_facts_enrich
Create Date: 2026-08-08

Many flags in ``modulo.core.feature_flags._KNOWN_FLAGS`` (``error_tracking``,
``runtime_config``, ``rate_limits``, ``email_config``, ``scim``,
``external_secrets``, ``checkpoint_encryption``, ``audit_crypto_chain``,
``community_registry``, ``prompt_optimization``, ``pipeline_diff_rollback``,
``pipeline_delete``, ``schema_union_types``, ``migration_cli``,
``notification_log``, ``api_changelog``, ``web_vitals_analytics``) were never
added to the seed ``FLAGS`` list in ``modulo.core.seed_data.catalog``. The
startup seed (``_seed_tier_catalog`` in ``modulo.api.main``) uses ``ON
CONFLICT (name) DO NOTHING``, and ``FeatureFlagRegistry.load_from_db()``
REPLACES its hardcoded flags with ONLY the DB-backed rows. The missing flags
therefore vanish from the registry entirely, so ``feature_enabled("error_tracking")``
returns ``False`` even on the team tier — locking Team-tier pages (Error
Dashboard, Runtime Config, Rate Limits) behind the gate/402 error.

This migration UPSERTS the missing rows into ``feature_flag_catalog`` so the
DB catalog mirrors ``_KNOWN_FLAGS`` everywhere. It also seeds the
``community``/``team`` rows into ``tier_catalog`` (ON CONFLICT DO NOTHING)
because ``feature_flag_catalog.tier_id`` is FK-constrained to ``tier_catalog``
and that table is only populated by the app startup seed, which runs after
Alembic — on a fresh deployment the upsert would otherwise violate the FK.
Data-only — no tables or columns are created or dropped.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0072_sync_feature_flag_catalog"
down_revision: str | None = "0071_analytics_facts_enrich"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Flags missing from the seed catalog but present in
# ``modulo.core.feature_flags._KNOWN_FLAGS``. name -> (tier_id, description).
_FLAGS: dict[str, tuple[str, str]] = {
    "error_tracking": ("team", "External error tracking and alerting integrations"),
    "runtime_config": ("team", "Runtime configuration overrides"),
    "rate_limits": ("team", "Configure API rate limits"),
    "email_config": ("team", "SMTP email configuration for notifications"),
    "scim": ("team", "SCIM 2.0 user and group provisioning"),
    "external_secrets": ("team", "External secrets backends (Vault, AWS, 1Password, Azure Key Vault)"),
    "checkpoint_encryption": ("team", "Encrypt pipeline checkpoints at rest"),
    "audit_crypto_chain": ("team", "Cryptographic chaining of audit events for tamper evidence"),
    "community_registry": ("team", "Publish and discover community pipeline primitives"),
    "prompt_optimization": ("team", "Automated prompt tuning and optimisation"),
    "pipeline_diff_rollback": ("team", "Diff-based pipeline version comparison and rollback"),
    "pipeline_delete": ("team", "Allow hard-deleting pipelines from the UI"),
    "schema_union_types": ("team", "Union types and polymorphic schemas"),
    "migration_cli": ("team", "CLI tool for migrating pipelines across instances"),
    "notification_log": ("community", "In-app notification delivery log"),
    "api_changelog": ("community", "API changelog and version history"),
    "web_vitals_analytics": ("community", "Web Vitals analytics dashboard for monitoring frontend performance"),
}

# Tier catalog seed rows (mirrors ``modulo.core.seed_data.catalog.TIERS``). The
# ``feature_flag_catalog.tier_id`` column is FK-constrained to ``tier_catalog``,
# and ``tier_catalog`` is populated by the app startup seed — which runs AFTER
# Alembic migrations. A fresh deployment therefore has an empty ``tier_catalog``
# when this migration runs, so the flag upsert below would violate the FK. Seed
# the tiers here first (ON CONFLICT DO NOTHING keeps existing rows untouched).
_TIERS: list[dict[str, object]] = [
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


def _is_postgres(bind: sa.Connection) -> bool:
    return bind.dialect.name == "postgresql"


def _seed_tiers() -> None:
    for tier in _TIERS:
        op.execute(
            sa.text(
                """
                INSERT INTO tier_catalog (tier_id, label, rank, requires_license, description)
                VALUES (:tier_id, :label, :rank, :requires_license, :description)
                ON CONFLICT (tier_id) DO NOTHING
                """
            ).bindparams(**tier)
        )


def _upsert(name: str, tier_id: str, description: str, is_active: bool) -> None:
    _seed_tiers()
    bind = op.get_bind()
    if _is_postgres(bind):
        op.execute(
            sa.text(
                """
                INSERT INTO feature_flag_catalog (name, description, tier_id, depends_on, is_active)
                VALUES (:name, :description, :tier_id, NULL, :is_active)
                ON CONFLICT (name) DO UPDATE SET
                    tier_id = excluded.tier_id,
                    is_active = excluded.is_active,
                    description = excluded.description,
                    depends_on = excluded.depends_on
                """
            ).bindparams(
                name=name,
                description=description,
                tier_id=tier_id,
                is_active=is_active,
            )
        )
    else:
        # Non-Postgres dev backends lack ``ON CONFLICT ... DO UPDATE``; emulate
        # the upsert as delete-then-insert (the catalog is seed data, re-seeded
        # at startup).
        op.execute(sa.text("DELETE FROM feature_flag_catalog WHERE name = :name").bindparams(name=name))
        op.execute(
            sa.text(
                "INSERT INTO feature_flag_catalog (name, description, tier_id, depends_on, is_active) "
                "VALUES (:name, :description, :tier_id, NULL, :is_active)"
            ).bindparams(
                name=name,
                description=description,
                tier_id=tier_id,
                is_active=is_active,
            )
        )


def upgrade() -> None:
    for name, (tier_id, description) in _FLAGS.items():
        _upsert(name=name, tier_id=tier_id, description=description, is_active=True)


def downgrade() -> None:
    # The flags did not exist in the DB catalog before this migration; revert
    # to that state.
    for name in _FLAGS:
        op.execute(sa.text("DELETE FROM feature_flag_catalog WHERE name = :name").bindparams(name=name))
