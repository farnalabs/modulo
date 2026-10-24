"""Runless trigger-event guardrail findings (FAR-2028).

Revision ID: 0333_trigger_event_findings
Revises: 0332_eval_mute_policy
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0333_trigger_event_findings"
down_revision: str | None = "0332_eval_mute_policy"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    if not str(op.get_bind().dialect.name).startswith("postgres"):
        return
    # This migration owns the composite key required by the tenant-scoped
    # finding foreign key. Migration history has no earlier owner for it, so
    # create and remove it as part of this migration rather than conditionally
    # touching a constraint that may belong to another revision.
    op.create_unique_constraint(
        "uq_trigger_events_id_organisation_id",
        "trigger_events",
        ["id", "organisation_id"],
    )
    op.create_table(
        "trigger_event_findings",
        sa.Column("id", sa.Uuid(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("organisation_id", sa.Uuid(), nullable=False),
        sa.Column("trigger_event_id", sa.Uuid(), nullable=False),
        sa.Column("eval_id", sa.Uuid(), nullable=False),
        sa.Column("passed", sa.Boolean(), nullable=False),
        sa.Column("outcome", sa.Text(), nullable=False),
        sa.Column("score", sa.Float(), nullable=True),
        sa.Column("detail", sa.Text(), nullable=True),
        sa.Column("resolution", sa.Text(), server_default="pending", nullable=False),
        sa.Column("resolution_reason", sa.Text(), nullable=True),
        sa.Column("resolved_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("resolved_by", sa.Uuid(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("id", "organisation_id", name="uq_trigger_event_findings_id_organisation_id"),
        sa.UniqueConstraint(
            "organisation_id",
            "trigger_event_id",
            "eval_id",
            name="uq_trigger_event_findings_org_event_eval",
        ),
        sa.ForeignKeyConstraint(["organisation_id"], ["organisations.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["trigger_event_id", "organisation_id"],
            ["trigger_events.id", "trigger_events.organisation_id"],
            ondelete="CASCADE",
            name="fk_trigger_event_findings_event_org",
        ),
        sa.ForeignKeyConstraint(
            ["eval_id", "organisation_id"],
            ["evals.id", "evals.organisation_id"],
            ondelete="CASCADE",
            name="fk_trigger_event_findings_eval_org",
        ),
        sa.ForeignKeyConstraint(["resolved_by"], ["accounts.id"], ondelete="SET NULL"),
        sa.CheckConstraint(
            "resolution IN ('accepted', 'dismissed', 'superseded', 'pending')",
            name="ck_trigger_event_findings_resolution",
        ),
    )
    op.execute("ALTER TABLE public.trigger_event_findings ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE public.trigger_event_findings FORCE ROW LEVEL SECURITY")
    op.execute("DROP POLICY IF EXISTS rls_org_isolation ON public.trigger_event_findings")
    op.execute(
        "CREATE POLICY rls_org_isolation ON public.trigger_event_findings USING "
        "(organisation_id = nullif(current_setting('app.organisation_id', true), '')::uuid)"
    )


def downgrade() -> None:
    if not str(op.get_bind().dialect.name).startswith("postgres"):
        return
    op.drop_table("trigger_event_findings")
    op.drop_constraint("uq_trigger_events_id_organisation_id", "trigger_events", type_="unique")
