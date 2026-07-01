"""Create error_events and error_groups tables for native error tracking.

Revision ID: 0051_error_tracking
Revises: 0050_composite_templates
Create Date: 2026-07-01
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0051_error_tracking"
down_revision: str | Sequence[str] | None = "0050_composite_templates"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "error_events",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("organisation_id", sa.Uuid(), sa.ForeignKey("organisations.id", ondelete="CASCADE"), nullable=False),
        sa.Column("fingerprint", sa.String(64), nullable=False),
        sa.Column("level", sa.String(20), nullable=False),
        sa.Column("message", sa.Text(), nullable=False),
        sa.Column("stacktrace", sa.Text(), nullable=True),
        sa.Column("context_json", sa.JSON(), nullable=True),
        sa.Column("source", sa.String(20), nullable=False),
        sa.Column("environment", sa.String(50), nullable=True),
        sa.Column("version", sa.String(50), nullable=True),
        sa.Column("status", sa.String(20), nullable=False, server_default="new"),
        sa.Column("resolved_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.current_timestamp(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.current_timestamp(), nullable=False),
    )

    op.create_table(
        "error_groups",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("organisation_id", sa.Uuid(), sa.ForeignKey("organisations.id", ondelete="CASCADE"), nullable=False),
        sa.Column("fingerprint", sa.String(64), nullable=False),
        sa.Column("status", sa.String(20), nullable=False, server_default="new"),
        sa.Column("first_seen", sa.DateTime(timezone=True), server_default=sa.func.current_timestamp(), nullable=False),
        sa.Column("last_seen", sa.DateTime(timezone=True), server_default=sa.func.current_timestamp(), nullable=False),
        sa.Column("count", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("level_peak", sa.String(20), nullable=False, server_default="error"),
        sa.Column("sample_event_id", sa.Uuid(), sa.ForeignKey("error_events.id", ondelete="SET NULL"), nullable=True),
        sa.Column("assigned_to", sa.Uuid(), sa.ForeignKey("accounts.id", ondelete="SET NULL"), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.current_timestamp(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.current_timestamp(), nullable=False),
    )

    op.create_index("ix_error_events_organisation_id", "error_events", ["organisation_id"])
    op.create_index("ix_error_events_org_fingerprint", "error_events", ["organisation_id", "fingerprint"])
    op.create_index("ix_error_groups_organisation_id", "error_groups", ["organisation_id"])
    op.create_index("ix_error_groups_org_status_last_seen", "error_groups", ["organisation_id", "status", "last_seen"])

    op.create_unique_constraint(
        "uq_error_groups_org_fingerprint",
        "error_groups",
        ["organisation_id", "fingerprint"],
    )

    op.create_check_constraint(
        "ck_error_events_level",
        "error_events",
        sa.text("level IN ('error', 'warning', 'critical')"),
    )
    op.create_check_constraint(
        "ck_error_events_source",
        "error_events",
        sa.text("source IN ('backend', 'frontend', 'celery')"),
    )
    op.create_check_constraint(
        "ck_error_events_status",
        "error_events",
        sa.text("status IN ('new', 'acknowledged', 'resolved', 'archived')"),
    )
    op.create_check_constraint(
        "ck_error_groups_status",
        "error_groups",
        sa.text("status IN ('new', 'acknowledged', 'resolved', 'archived')"),
    )
    op.create_check_constraint(
        "ck_error_groups_level_peak",
        "error_groups",
        sa.text("level_peak IN ('error', 'warning', 'critical')"),
    )

    # Append-only trigger function and triggers for error_events
    op.execute(
        sa.text("""
        CREATE OR REPLACE FUNCTION error_events_append_only()
        RETURNS TRIGGER AS $$
        BEGIN
            IF TG_OP = 'DELETE' THEN
                RAISE EXCEPTION 'error_events are append-only: DELETE is not permitted';
            ELSIF TG_OP = 'UPDATE' THEN
                RAISE EXCEPTION 'error_events are append-only: UPDATE is not permitted';
            END IF;
            RETURN NULL;
        END;
        $$ LANGUAGE plpgsql;
        """)
    )

    op.execute(
        sa.text("""
        CREATE TRIGGER error_events_no_update
            BEFORE UPDATE ON error_events
            FOR EACH ROW
            EXECUTE FUNCTION error_events_append_only();
        """)
    )

    op.execute(
        sa.text("""
        CREATE TRIGGER error_events_no_delete
            BEFORE DELETE ON error_events
            FOR EACH ROW
            EXECUTE FUNCTION error_events_append_only();
        """)
    )


def downgrade() -> None:
    op.execute(sa.text("DROP TRIGGER IF EXISTS error_events_no_delete ON error_events"))
    op.execute(sa.text("DROP TRIGGER IF EXISTS error_events_no_update ON error_events"))
    op.execute(sa.text("DROP FUNCTION IF EXISTS error_events_append_only()"))

    op.drop_constraint("ck_error_groups_level_peak", "error_groups", type_="check")
    op.drop_constraint("ck_error_groups_status", "error_groups", type_="check")
    op.drop_constraint("ck_error_events_status", "error_events", type_="check")
    op.drop_constraint("ck_error_events_source", "error_events", type_="check")
    op.drop_constraint("ck_error_events_level", "error_events", type_="check")

    op.drop_constraint("uq_error_groups_org_fingerprint", "error_groups", type_="unique")

    op.drop_index("ix_error_groups_org_status_last_seen", table_name="error_groups")
    op.drop_index("ix_error_groups_organisation_id", table_name="error_groups")
    op.drop_index("ix_error_events_org_fingerprint", table_name="error_events")
    op.drop_index("ix_error_events_organisation_id", table_name="error_events")

    op.drop_table("error_groups")
    op.drop_table("error_events")
