"""Add CHECK constraints and FK on created_by for organisations.

Revision ID: 0236_add_organisations_constraints
Revises: 0235_promote_organisations_json_to_jsonb
Create Date: 2026-09-14
"""

from alembic import op

revision = "0236_add_organisations_constraints"
down_revision = "0235_promote_organisations_json_to_jsonb"


def upgrade() -> None:
    op.create_check_constraint(
        "ck_organisations_name_not_empty",
        "organisations",
        "LENGTH(name) > 0",
    )
    op.create_check_constraint(
        "ck_organisations_slug_not_empty",
        "organisations",
        "LENGTH(slug) > 0",
    )
    op.create_check_constraint(
        "ck_organisations_daily_spend_limit_non_negative",
        "organisations",
        "daily_spend_limit IS NULL OR daily_spend_limit >= 0",
    )
    op.create_check_constraint(
        "ck_organisations_deletion_token_expiry",
        "organisations",
        "(deletion_token IS NULL AND deletion_token_expires_at IS NULL) OR "
        "(deletion_token IS NOT NULL AND deletion_token_expires_at IS NOT NULL)",
    )
    op.create_foreign_key(
        "fk_organisations_created_by",
        "organisations",
        "accounts",
        ["created_by"],
        ["id"],
        ondelete="SET NULL",
    )


def downgrade() -> None:
    op.drop_constraint("fk_organisations_created_by", "organisations", type_="foreignkey")
    op.drop_constraint("ck_organisations_deletion_token_expiry", "organisations", type_="check")
    op.drop_constraint("ck_organisations_daily_spend_limit_non_negative", "organisations", type_="check")
    op.drop_constraint("ck_organisations_slug_not_empty", "organisations", type_="check")
    op.drop_constraint("ck_organisations_name_not_empty", "organisations", type_="check")
