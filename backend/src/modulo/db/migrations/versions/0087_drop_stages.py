"""Drop the Stage Board feature — stages table + pipelines.stage_id (FAR-152).

Revision ID: 0087_drop_stages
Revises: 0086_seeded_alert_rules
Create Date: 2026-08-12

The Stage Board is superseded by the Runs list status filters, Pipeline
Folders, and the Lifecycle Map. It is deleted, not deprecated.

Drops:
1. The ``stages`` table (created in 0003_v2_pipeline_runtime) including its
   RLS policies (``rls_org_isolation``, ``rls_team_isolation``), tenant
   triggers, and org index.
2. ``pipelines.stage_id`` (FK to ``stages.id``, ondelete SET NULL) and its
   tenant trigger ``trg_pipelines_stage_id_tenant``.

The journey/lifecycle-map projection (``lifecycle_map_stages``, ``journeys``,
FAR-141..145) is a DIFFERENT feature and is untouched.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0087_drop_stages"
down_revision: str | None = "0086_seeded_alert_rules"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # 1. Drop the pipelines.stage_id tenant trigger + FK + column first — it
    #    references the stages table and would block the DROP TABLE.
    op.execute(sa.text('DROP TRIGGER IF EXISTS "trg_pipelines_stage_id_tenant" ON "pipelines"'))
    op.drop_constraint("pipelines_stage_id_fkey", "pipelines", type_="foreignkey")
    op.drop_column("pipelines", "stage_id")

    # 2. Drop the stages table with its RLS policies, tenant triggers, and
    #    organisation index.
    op.execute(sa.text('DROP POLICY IF EXISTS rls_team_isolation ON "stages"'))
    op.execute(sa.text('DROP POLICY IF EXISTS rls_org_isolation ON "stages"'))
    op.execute(sa.text('ALTER TABLE "stages" DISABLE ROW LEVEL SECURITY'))
    op.execute(sa.text('DROP TRIGGER IF EXISTS "trg_stages_account_id_tenant" ON "stages"'))
    op.execute(sa.text('DROP TRIGGER IF EXISTS "trg_stages_owner_team_id_tenant" ON "stages"'))
    op.drop_index(op.f("ix_stages_organisation_id"), table_name="stages")
    op.drop_table("stages")


def downgrade() -> None:
    # Recreate the stages table exactly as 0003 defined it (restore path only —
    # data is not recoverable; this recreates the empty schema).
    op.create_table(
        "stages",
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("description", sa.String(length=2000), nullable=True),
        sa.Column("position", sa.Integer(), server_default="0", nullable=False),
        sa.Column("owner_team_id", sa.Uuid(), nullable=True),
        sa.Column("visibility", sa.String(length=10), server_default="org", nullable=False),
        sa.Column("account_id", sa.Uuid(), nullable=False),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("organisation_id", sa.Uuid(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False
        ),
        sa.CheckConstraint("visibility IN ('org', 'team')", name="ck_stages_visibility"),
        sa.CheckConstraint("visibility = 'org' OR owner_team_id IS NOT NULL", name="ck_stages_team_owner"),
        sa.ForeignKeyConstraint(["account_id"], ["accounts.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["organisation_id"], ["organisations.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["owner_team_id"], ["teams.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_stages_organisation_id"), "stages", ["organisation_id"], unique=False)

    # Re-add pipelines.stage_id FK + tenant trigger.
    op.add_column(
        "pipelines",
        sa.Column("stage_id", sa.Uuid(), nullable=True),
    )
    op.create_foreign_key(
        "pipelines_stage_id_fkey",
        "pipelines",
        "stages",
        ["stage_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.execute(
        sa.text(
            'CREATE TRIGGER "trg_pipelines_stage_id_tenant" '
            'BEFORE INSERT OR UPDATE OF "stage_id", "organisation_id" ON "pipelines" '
            "FOR EACH ROW EXECUTE FUNCTION enforce_same_organisation('stages', 'stage_id')"
        )
    )

    # Re-apply RLS + tenant triggers (mirror 0002/0003/0033 semantics).
    strict = "organisation_id = nullif(current_setting('app.organisation_id', true), '')::uuid"
    team = (
        "(visibility = 'org' OR visibility IS NULL) "
        "OR (owner_team_id IS NULL) "
        "OR (owner_team_id IN ("
        "SELECT team_id FROM team_memberships "
        "WHERE account_id = nullif(current_setting('app.user_id', true), '')::uuid"
        ")) "
        "OR (nullif(current_setting('app.org_role', true), '') = 'admin')"
    )
    op.execute(sa.text('ALTER TABLE "stages" ENABLE ROW LEVEL SECURITY'))
    op.execute(sa.text(f'CREATE POLICY rls_org_isolation ON "stages" USING ({strict})'))
    op.execute(sa.text(f'CREATE POLICY rls_team_isolation ON "stages" USING ({team})'))
    op.execute(
        sa.text(
            'CREATE TRIGGER "trg_stages_account_id_tenant" '
            'BEFORE INSERT OR UPDATE OF "account_id", "organisation_id" ON "stages" '
            "FOR EACH ROW EXECUTE FUNCTION enforce_same_organisation('accounts', 'account_id')"
        )
    )
    op.execute(
        sa.text(
            'CREATE TRIGGER "trg_stages_owner_team_id_tenant" '
            'BEFORE INSERT OR UPDATE OF "owner_team_id", "organisation_id" ON "stages" '
            "FOR EACH ROW EXECUTE FUNCTION enforce_same_organisation('teams', 'owner_team_id')"
        )
    )
