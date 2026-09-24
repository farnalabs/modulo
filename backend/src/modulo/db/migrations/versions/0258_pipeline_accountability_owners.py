"""Add pipeline accountability owner columns (FAR-1161 data layer).

FAR-1161 adds two accountability owner roles to pipelines, DISTINCT from
``owner_team_id`` (which remains the tenancy/visibility boundary):

* ``business_owner_id``      — UUID NULL, FK -> accounts.id, ON DELETE SET NULL
* ``reliability_owner_id``   — UUID NULL, FK -> accounts.id, ON DELETE SET NULL

Both are nullable: existing pipelines have no accountability owners until an
operator assigns them, and deleting an account must not delete the pipeline
(the reference is cleared instead).
"""

import sqlalchemy as sa
from alembic import op

revision: str = "0258_pipeline_accountability_owners"
down_revision: str | None = "0257_rename_remy_to_assistant"
branch_labels: tuple[str, ...] | None = None
depends_on: tuple[str, ...] | None = None


def upgrade() -> None:
    op.add_column(
        "pipelines",
        sa.Column(
            "business_owner_id",
            sa.Uuid(),
            nullable=True,
        ),
    )
    op.add_column(
        "pipelines",
        sa.Column(
            "reliability_owner_id",
            sa.Uuid(),
            nullable=True,
        ),
    )
    op.create_foreign_key(
        "fk_pipelines_business_owner_id",
        "pipelines",
        "accounts",
        ["business_owner_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.create_foreign_key(
        "fk_pipelines_reliability_owner_id",
        "pipelines",
        "accounts",
        ["reliability_owner_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.create_index(
        "ix_pipelines_business_owner_id",
        "pipelines",
        ["business_owner_id"],
    )
    op.create_index(
        "ix_pipelines_reliability_owner_id",
        "pipelines",
        ["reliability_owner_id"],
    )


def downgrade() -> None:
    op.drop_index("ix_pipelines_reliability_owner_id", table_name="pipelines")
    op.drop_index("ix_pipelines_business_owner_id", table_name="pipelines")
    op.drop_constraint("fk_pipelines_reliability_owner_id", "pipelines", type_="foreignkey")
    op.drop_constraint("fk_pipelines_business_owner_id", "pipelines", type_="foreignkey")
    op.drop_column("pipelines", "reliability_owner_id")
    op.drop_column("pipelines", "business_owner_id")
