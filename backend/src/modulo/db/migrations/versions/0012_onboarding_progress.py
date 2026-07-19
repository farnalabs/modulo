"""Create onboarding_progress table for action-based onboarding tracking.

Revision ID: 0012_onboarding_progress
Revises: 0011_database_review_fixes
Create Date: 2026-07-15

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import UUID

_RLS_TABLES = ("onboarding_progress",)

revision: str = "0012_onboarding_progress"
down_revision: str | None = "0011_database_review_fixes"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "onboarding_progress",
        sa.Column("id", UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column(
            "organisation_id", UUID(as_uuid=True), sa.ForeignKey("organisations.id", ondelete="CASCADE"), nullable=False
        ),
        sa.Column("completed_actions", sa.ARRAY(sa.String()), nullable=False, server_default=sa.text("'{}'")),
        sa.Column("skipped_actions", sa.ARRAY(sa.String()), nullable=False, server_default=sa.text("'{}'")),
        sa.Column("dismissed", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.UniqueConstraint("organisation_id", name="uq_onboarding_progress_org"),
    )
    op.create_index(
        op.f("ix_onboarding_progress_organisation_id"),
        "onboarding_progress",
        ["organisation_id"],
        unique=True,
    )
    op.execute("ALTER TABLE onboarding_progress ENABLE ROW LEVEL SECURITY")
    op.execute(
        "CREATE POLICY rls_org_isolation ON onboarding_progress "
        "USING (organisation_id = current_setting('app.organisation_id')::uuid)"
    )


def downgrade() -> None:
    op.execute("DROP POLICY IF EXISTS rls_org_isolation ON onboarding_progress")
    op.execute("ALTER TABLE onboarding_progress DISABLE ROW LEVEL SECURITY")
    op.drop_index(op.f("ix_onboarding_progress_organisation_id"), table_name="onboarding_progress")
    op.drop_table("onboarding_progress")
