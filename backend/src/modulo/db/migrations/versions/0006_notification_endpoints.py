"""Create notification_endpoints table, add status/response_code/payload_ciphertext to delivery log.

Revision ID: 0006_notification_endpoints
Revises: 0005_library_community_visibility
Create Date: 2026-06-23
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0006_notification_endpoints"
down_revision: str | None = "0005_library_community_visibility"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "notification_endpoints",
        sa.Column("url", sa.String(length=2048), nullable=False),
        sa.Column("secret_ciphertext", sa.LargeBinary(), nullable=True),
        sa.Column("events", sa.Text(), nullable=False, server_default="[]"),
        sa.Column("description", sa.String(length=500), nullable=True),
        sa.Column(
            "consecutive_dead_letter_count",
            sa.Integer(),
            server_default="0",
            nullable=False,
        ),
        sa.Column(
            "auto_disabled", sa.Boolean(), server_default="false", nullable=False
        ),
        sa.Column("disabled_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_by", sa.UUID(), nullable=True),
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("organisation_id", sa.UUID(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["organisation_id"], ["organisations.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(["created_by"], ["users.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        op.f("ix_notification_endpoints_organisation_id"),
        "notification_endpoints",
        ["organisation_id"],
        unique=False,
    )

    op.add_column(
        "notification_delivery_log",
        sa.Column(
            "status",
            sa.String(length=20),
            server_default="delivered",
            nullable=False,
        ),
    )
    op.add_column(
        "notification_delivery_log",
        sa.Column("response_code", sa.Integer(), nullable=True),
    )
    op.add_column(
        "notification_delivery_log",
        sa.Column("payload_ciphertext", sa.LargeBinary(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("notification_delivery_log", "payload_ciphertext")
    op.drop_column("notification_delivery_log", "response_code")
    op.drop_column("notification_delivery_log", "status")
    op.drop_index(
        op.f("ix_notification_endpoints_organisation_id"),
        table_name="notification_endpoints",
    )
    op.drop_table("notification_endpoints")
