"""Promote organisations JSON columns to JSONB.

Revision ID: 0234_promote_organisations_json_to_jsonb
Revises: 0233_add_organisations_indexes
Create Date: 2026-09-14
"""

from alembic import op

revision = "0234_promote_organisations_json_to_jsonb"
down_revision = "0233_add_organisations_indexes"

COLUMNS = [
    "settings_json",
    "otel_config_json",
    "export_bundle_json",
    "guardrail_pins_json",
]


def upgrade() -> None:
    # Promote organisations JSON columns to JSONB to enable GIN indexing,
    # containment (@>) queries, and path extraction without loading the full
    # document. These columns are queried by application code (org settings,
    # OpenTelemetry config, export bundles, guardrail pins).
    for col in COLUMNS:
        op.execute(f"ALTER TABLE organisations ALTER COLUMN {col} TYPE jsonb USING {col}::jsonb")


def downgrade() -> None:
    for col in COLUMNS:
        op.execute(f"ALTER TABLE organisations ALTER COLUMN {col} TYPE json USING {col}::json")
