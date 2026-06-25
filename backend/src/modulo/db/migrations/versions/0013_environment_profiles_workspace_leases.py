"""Create environment_profiles and workspace_leases tables, add columns.

Adds:
- environment_profiles table
- workspace_leases table
- environment_profile_id FK on pipeline_snapshots
- required_environment_capabilities column on agents

Revision ID: 0013_environment_profiles_workspace_leases
Revises: 0012_variant_groups
Create Date: 2026-06-24
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0013_environment_profiles_workspace_leases"
down_revision: str | None = "0012_variant_groups"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "environment_profiles",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("organisation_id", sa.UUID(), nullable=False),
        sa.Column("name", sa.String(255), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("image_ref", sa.String(500), nullable=False),
        sa.Column("capabilities", sa.JSON(), nullable=False, server_default=sa.text("'[]'::json")),
        sa.Column("egress_policy", sa.String(20), nullable=True),
        sa.Column("persistence_policy", sa.JSON(), nullable=False, server_default=sa.text("'{}'::json")),
        sa.Column("timeout_seconds", sa.Integer(), nullable=False, server_default=sa.text("3600")),
        sa.Column("resource_limits_json", sa.JSON(), nullable=False, server_default=sa.text("'{}'::json")),
        sa.Column("created_by", sa.UUID(), nullable=True),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.text("true")),
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
        sa.ForeignKeyConstraint(["organisation_id"], ["organisations.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["created_by"], ["users.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        op.f("ix_environment_profiles_organisation_id"),
        "environment_profiles",
        ["organisation_id"],
        unique=False,
    )

    op.create_table(
        "workspace_leases",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("organisation_id", sa.UUID(), nullable=False),
        sa.Column("environment_profile_id", sa.UUID(), nullable=False),
        sa.Column("run_id", sa.UUID(), nullable=True),
        sa.Column("provider_ref", sa.String(500), nullable=False),
        sa.Column("status", sa.String(20), nullable=False, server_default=sa.text("'pending'")),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("resource_usage_json", sa.JSON(), nullable=True),
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
        sa.ForeignKeyConstraint(["organisation_id"], ["organisations.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["environment_profile_id"],
            ["environment_profiles.id"],
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(["run_id"], ["runs.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        op.f("ix_workspace_leases_organisation_id"),
        "workspace_leases",
        ["organisation_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_workspace_leases_environment_profile_id"),
        "workspace_leases",
        ["environment_profile_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_workspace_leases_run_id"),
        "workspace_leases",
        ["run_id"],
        unique=False,
    )

    op.create_check_constraint(
        "ck_workspace_leases_status",
        "workspace_leases",
        "status IN ('pending', 'running', 'stopped', 'failed')",
    )

    # Add environment_profile_id to pipeline_snapshots
    op.add_column(
        "pipeline_snapshots",
        sa.Column(
            "environment_profile_id",
            sa.UUID(),
            nullable=True,
        ),
    )
    op.create_index(
        op.f("ix_pipeline_snapshots_environment_profile_id"),
        "pipeline_snapshots",
        ["environment_profile_id"],
        unique=False,
    )
    op.create_foreign_key(
        "fk_pipeline_snapshots_env_profile",
        "pipeline_snapshots",
        "environment_profiles",
        ["environment_profile_id"],
        ["id"],
        ondelete="SET NULL",
    )

    # Add required_environment_capabilities to agents
    op.add_column(
        "agents",
        sa.Column(
            "required_environment_capabilities",
            sa.JSON(),
            nullable=False,
            server_default=sa.text("'[]'::json"),
        ),
    )


def downgrade() -> None:
    op.drop_column("agents", "required_environment_capabilities")
    op.drop_constraint(
        "fk_pipeline_snapshots_env_profile",
        "pipeline_snapshots",
        type_="foreignkey",
    )
    op.drop_index(
        op.f("ix_pipeline_snapshots_environment_profile_id"),
        table_name="pipeline_snapshots",
    )
    op.drop_column("pipeline_snapshots", "environment_profile_id")
    op.drop_index(op.f("ix_workspace_leases_run_id"), table_name="workspace_leases")
    op.drop_index(
        op.f("ix_workspace_leases_environment_profile_id"),
        table_name="workspace_leases",
    )
    op.drop_index(op.f("ix_workspace_leases_organisation_id"), table_name="workspace_leases")
    op.drop_table("workspace_leases")
    op.drop_index(
        op.f("ix_environment_profiles_organisation_id"),
        table_name="environment_profiles",
    )
    op.drop_table("environment_profiles")
