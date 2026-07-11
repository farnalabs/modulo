"""Update environment_profiles and workspace_leases schemas for ADR 001.

Adds columns for provider_type, config_json, network_policy, initialisation_strategy,
secret_refs_json, owner_team_id, visibility, status. Renames capabilities → capabilities_json.
Renames started_at → lease_started_at, expires_at → lease_expires_at.
Adds repository_url, repository_ref, output_artifact_refs_json, error_message.

Revision ID: 0087_environment_profiles
Revises: 0086_lifecycle_maps
Create Date: 2026-07-11
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0087_environment_profiles"
down_revision: str | Sequence[str] | None = "0086_lifecycle_maps"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # ── environment_profiles ──────────────────────────────────────────

    # Add new columns
    op.add_column(
        "environment_profiles",
        sa.Column("provider_type", sa.String(50), nullable=False, server_default="local_docker"),
    )
    op.add_column(
        "environment_profiles",
        sa.Column("capabilities_json", sa.JSON(), nullable=False, server_default=sa.text("'[]'::json")),
    )
    op.add_column(
        "environment_profiles",
        sa.Column("config_json", sa.JSON(), nullable=False, server_default=sa.text("'{}'::json")),
    )
    op.add_column(
        "environment_profiles",
        sa.Column("network_policy", sa.String(20), nullable=False, server_default="outbound"),
    )
    op.add_column(
        "environment_profiles",
        sa.Column("initialisation_strategy", sa.String(30), nullable=False, server_default="git_clone"),
    )
    op.add_column(
        "environment_profiles",
        sa.Column("secret_refs_json", sa.JSON(), nullable=False, server_default=sa.text("'[]'::json")),
    )
    op.add_column(
        "environment_profiles",
        sa.Column("status", sa.String(30), nullable=False, server_default="active"),
    )
    op.add_column(
        "environment_profiles",
        sa.Column("account_id", sa.UUID(), sa.ForeignKey("accounts.id", ondelete="RESTRICT"), nullable=True),
    )
    op.add_column(
        "environment_profiles",
        sa.Column("owner_team_id", sa.UUID(), sa.ForeignKey("teams.id", ondelete="RESTRICT"), nullable=True),
    )
    op.add_column(
        "environment_profiles",
        sa.Column("visibility", sa.String(10), nullable=False, server_default="org"),
    )

    # Migrate data: copy capabilities → capabilities_json
    op.execute("UPDATE environment_profiles SET capabilities_json = COALESCE(capabilities, '[]'::json)")

    # Drop old columns
    op.drop_column("environment_profiles", "capabilities")
    op.drop_column("environment_profiles", "egress_policy")
    op.drop_column("environment_profiles", "timeout_seconds")
    op.drop_column("environment_profiles", "resource_limits_json")
    op.drop_column("environment_profiles", "created_by")
    op.drop_column("environment_profiles", "is_active")

    # Alter persistence_policy from JSON to String(20)
    op.drop_column("environment_profiles", "persistence_policy")
    op.add_column(
        "environment_profiles",
        sa.Column("persistence_policy", sa.String(20), nullable=False, server_default="ephemeral"),
    )

    # Make image_ref nullable
    op.alter_column("environment_profiles", "image_ref", nullable=True, type_=sa.String(500))

    # Add check constraints
    op.create_check_constraint(
        "ck_env_profiles_visibility",
        "environment_profiles",
        "visibility IN ('org', 'team')",
    )
    op.create_check_constraint(
        "ck_env_profiles_provider_type",
        "environment_profiles",
        "provider_type IN ('local_docker', 'e2b')",
    )
    op.create_check_constraint(
        "ck_env_profiles_persistence_policy",
        "environment_profiles",
        "persistence_policy IN ('ephemeral', 'retained', 'cache')",
    )
    op.create_check_constraint(
        "ck_env_profiles_network_policy",
        "environment_profiles",
        "network_policy IN ('none', 'outbound', 'selected')",
    )

    # ── workspace_leases ──────────────────────────────────────────────

    # Add new columns
    op.add_column(
        "workspace_leases",
        sa.Column("repository_url", sa.String(1000), nullable=True),
    )
    op.add_column(
        "workspace_leases",
        sa.Column("repository_ref", sa.String(255), nullable=True),
    )
    op.add_column(
        "workspace_leases",
        sa.Column("output_artifact_refs_json", sa.JSON(), nullable=True),
    )
    op.add_column(
        "workspace_leases",
        sa.Column("error_message", sa.Text(), nullable=True),
    )

    # Rename columns
    op.alter_column("workspace_leases", "started_at", new_column_name="lease_started_at")
    op.alter_column("workspace_leases", "expires_at", new_column_name="lease_expires_at")

    # Make run_id non-nullable and change ondelete
    op.drop_constraint("workspace_leases_run_id_fkey", "workspace_leases", type_="foreignkey")
    op.alter_column("workspace_leases", "run_id", nullable=False)
    op.create_foreign_key(
        "workspace_leases_run_id_fkey",
        "workspace_leases",
        "runs",
        ["run_id"],
        ["id"],
        ondelete="RESTRICT",
    )

    # Change provider_ref to String(255)
    op.alter_column("workspace_leases", "provider_ref", type_=sa.String(255))

    # Update status column type and drop old check constraint
    op.drop_constraint("ck_workspace_leases_status", "workspace_leases", type_="check")
    op.create_check_constraint(
        "ck_workspace_leases_status",
        "workspace_leases",
        "status IN ('pending', 'running', 'completed', 'failed', 'expired')",
    )


def downgrade() -> None:
    # ── workspace_leases ──────────────────────────────────────────────

    op.drop_constraint("ck_workspace_leases_status", "workspace_leases", type_="check")
    op.create_check_constraint(
        "ck_workspace_leases_status",
        "workspace_leases",
        "status IN ('pending', 'running', 'stopped', 'failed')",
    )
    op.alter_column("workspace_leases", "provider_ref", type_=sa.String(500))
    op.drop_constraint("workspace_leases_run_id_fkey", "workspace_leases", type_="foreignkey")
    op.alter_column("workspace_leases", "run_id", nullable=True)
    op.create_foreign_key(
        "workspace_leases_run_id_fkey",
        "workspace_leases",
        "runs",
        ["run_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.alter_column("workspace_leases", "lease_started_at", new_column_name="started_at")
    op.alter_column("workspace_leases", "lease_expires_at", new_column_name="expires_at")
    op.drop_column("workspace_leases", "error_message")
    op.drop_column("workspace_leases", "output_artifact_refs_json")
    op.drop_column("workspace_leases", "repository_ref")
    op.drop_column("workspace_leases", "repository_url")

    # ── environment_profiles ──────────────────────────────────────────

    op.drop_constraint("ck_env_profiles_network_policy", "environment_profiles", type_="check")
    op.drop_constraint("ck_env_profiles_persistence_policy", "environment_profiles", type_="check")
    op.drop_constraint("ck_env_profiles_provider_type", "environment_profiles", type_="check")
    op.drop_constraint("ck_env_profiles_visibility", "environment_profiles", type_="check")

    op.drop_column("environment_profiles", "persistence_policy")
    op.add_column(
        "environment_profiles",
        sa.Column("persistence_policy", sa.JSON(), nullable=False, server_default=sa.text("'{}'::json")),
    )

    op.drop_column("environment_profiles", "visibility")
    op.drop_column("environment_profiles", "owner_team_id")
    op.drop_column("environment_profiles", "account_id")
    op.drop_column("environment_profiles", "status")
    op.drop_column("environment_profiles", "secret_refs_json")
    op.drop_column("environment_profiles", "initialisation_strategy")
    op.drop_column("environment_profiles", "network_policy")
    op.drop_column("environment_profiles", "config_json")
    op.drop_column("environment_profiles", "capabilities_json")

    op.alter_column("environment_profiles", "image_ref", nullable=False, type_=sa.String(500))
    op.add_column(
        "environment_profiles",
        sa.Column("capabilities", sa.JSON(), nullable=False, server_default=sa.text("'[]'::json")),
    )
    op.add_column(
        "environment_profiles",
        sa.Column("egress_policy", sa.String(20), nullable=True),
    )
    op.add_column(
        "environment_profiles",
        sa.Column("timeout_seconds", sa.Integer(), nullable=False, server_default=sa.text("3600")),
    )
    op.add_column(
        "environment_profiles",
        sa.Column("resource_limits_json", sa.JSON(), nullable=False, server_default=sa.text("'{}'::json")),
    )
    op.add_column(
        "environment_profiles",
        sa.Column("created_by", sa.UUID(), nullable=True),
    )
    op.add_column(
        "environment_profiles",
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.text("true")),
    )
    op.drop_column("environment_profiles", "provider_type")
