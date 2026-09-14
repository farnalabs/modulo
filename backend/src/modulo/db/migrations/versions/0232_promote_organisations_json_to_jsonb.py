"""Promote organisations JSON columns to JSONB.

Revision ID: 0232_promote_organisations_json_to_jsonb
Revises: 0231_add_organisations_indexes
Create Date: 2026-09-14
"""

import sqlalchemy as sa
from alembic import op

revision = "0232_promote_organisations_json_to_jsonb"
down_revision = "0231_add_organisations_indexes"

COLUMNS = [
    "settings_json",
    "otel_config_json",
    "export_bundle_json",
    "guardrail_pins_json",
]


def upgrade() -> None:
    for col in COLUMNS:
        op.alter_column(
            "organisations",
            col,
            type_=sa.JSON().with_variant(sa.JSONB(), "postgresql"),
            existing_nullable=False,
        )


def downgrade() -> None:
    for col in COLUMNS:
        op.alter_column(
            "organisations",
            col,
            type_=sa.JSON(),
            existing_nullable=False,
        )
