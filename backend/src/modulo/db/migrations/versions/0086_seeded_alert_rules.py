"""Seeded alert rules: signal columns + deleted_defaults tombstone (FAR-151).

Revision ID: 0086_seeded_alert_rules
Revises: 0085_journey_facts
Create Date: 2026-08-12

Adds the per-signal ingestion + seeded-defaults machinery (§15.6/§15.8):

1. ``error_events.signal`` — the per-signal ingestion marker (``agent.failed``,
   ``agent.no_op``, ``agent.stall``, ``contract.schema``, or a
   harness/sandbox/connector error class). NULL for legacy events.
2. ``error_notification_rules.signal`` — the signal a rule matches (NULL for
   legacy level-based rules).
3. ``error_notification_rules.is_default`` — True for seeded default rules; a
   version-bump re-seed force-updates only rows still ``is_default=true``.
4. ``deleted_defaults`` tombstone table (org_id, signal) — restore-defaults
   skips tombstoned signals; a per-rule restore clears the tombstone.

Tenant model mirrors ``journeys`` (FAR-142): strict org RLS
(``rls_org_isolation``). There is no ``owner_team_id`` column, so no
``enforce_same_organisation`` tenant trigger is needed.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0086_seeded_alert_rules"
down_revision: str | None = "0085_journey_facts"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("error_events", sa.Column("signal", sa.String(length=100), nullable=True))
    op.add_column(
        "error_notification_rules",
        sa.Column("signal", sa.String(length=100), nullable=True),
    )
    op.add_column(
        "error_notification_rules",
        sa.Column("is_default", sa.Boolean(), nullable=False, server_default=sa.false()),
    )
    op.create_table(
        "deleted_defaults",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("organisation_id", sa.Uuid(), nullable=False),
        sa.Column("signal", sa.String(length=100), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["organisation_id"], ["organisations.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("organisation_id", "signal", name="uq_deleted_defaults_org_signal"),
    )
    op.create_index(
        op.f("ix_deleted_defaults_organisation_id"),
        "deleted_defaults",
        ["organisation_id"],
        unique=False,
    )
    # Literal DDL so the RLS-coverage architecture test can detect this table
    # (it scans for `ALTER TABLE "<table>" ENABLE ROW LEVEL SECURITY`).
    strict = "organisation_id = nullif(current_setting('app.organisation_id', true), '')::uuid"
    op.execute(sa.text('ALTER TABLE "deleted_defaults" ENABLE ROW LEVEL SECURITY'))
    op.execute(sa.text(f'CREATE POLICY rls_org_isolation ON "deleted_defaults" USING ({strict})'))


def downgrade() -> None:
    op.execute(sa.text('DROP POLICY IF EXISTS rls_org_isolation ON "deleted_defaults"'))
    op.execute(sa.text('ALTER TABLE "deleted_defaults" DISABLE ROW LEVEL SECURITY'))
    op.drop_index(op.f("ix_deleted_defaults_organisation_id"), table_name="deleted_defaults")
    op.drop_table("deleted_defaults")
    op.drop_column("error_notification_rules", "is_default")
    op.drop_column("error_notification_rules", "signal")
    op.drop_column("error_events", "signal")
