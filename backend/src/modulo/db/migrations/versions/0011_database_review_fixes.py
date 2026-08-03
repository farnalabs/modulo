"""Database schema review fixes.

Adds missing indexes, FK constraints, and addresses column type issues
identified by the comprehensive database schema review (Reviews/database-schema-review.md).

Changes:
  — Add missing indexes on commonly-filtered FK columns
  — Add missing FK constraints on UUID columns that reference other tables
  — Change eval_definitions.pass_threshold from Float to Numeric
  — Expand string lengths where needed

Revision ID: 0011_database_review_fixes
Revises: 0010_fix_enforce_same_organisation_non_uuid
Create Date: 2026-07-15

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0011_database_review_fixes"
down_revision: str | None = "0010_fix_enforce_same_organisation_non_uuid"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _has_column(bind, table: str, column: str) -> bool:
    """Return True if ``table.column`` exists in the connected DB."""
    insp = sa.inspect(bind)
    return column in {c["name"] for c in insp.get_columns(table)}


def upgrade() -> None:
    bind = op.get_bind()
    _add_missing_indexes(bind)
    _add_missing_foreign_keys()
    _fix_column_types()
    _add_trigger_based_fk_checks()


def downgrade() -> None:
    _remove_trigger_based_fk_checks()
    _revert_column_types()
    _remove_missing_foreign_keys()
    _remove_missing_indexes()


def _add_missing_indexes(bind) -> None:
    # Deployed DBs that ran a pre-squash 0005 (or older) migration may lack
    # columns that later migrations add (e.g. account_id, created_by, view_type).
    # Guard every index creation on column existence so `alembic upgrade` doesn't
    # fail with "column does not exist"; the column + index are added idempotently
    # by later reconciliation migrations (0035/0037 and PR #618).
    if _has_column(bind, "notifications", "target_user_id"):
        op.create_index(
            op.f("ix_notifications_target_user_id"),
            "notifications",
            ["target_user_id"],
            unique=False,
            postgresql_where=sa.text("target_user_id IS NOT NULL"),
        )
    if _has_column(bind, "scheduled_reports", "created_by"):
        op.create_index(
            op.f("ix_scheduled_reports_created_by"),
            "scheduled_reports",
            ["created_by"],
            unique=False,
            postgresql_where=sa.text("created_by IS NOT NULL"),
        )
    if _has_column(bind, "connector_instances", "account_id"):
        op.create_index(
            op.f("ix_connector_instances_account_id"),
            "connector_instances",
            ["account_id"],
            unique=False,
        )
    if _has_column(bind, "node_observations", "account_id"):
        op.create_index(
            op.f("ix_node_observations_account_id"),
            "node_observations",
            ["account_id"],
            unique=False,
            postgresql_where=sa.text("account_id IS NOT NULL"),
        )
    if _has_column(bind, "primitive_ratings", "account_id"):
        op.create_index(
            op.f("ix_primitive_ratings_account_id"),
            "primitive_ratings",
            ["account_id"],
            unique=False,
            postgresql_where=sa.text("account_id IS NOT NULL"),
        )
    if _has_column(bind, "saved_views", "view_type"):
        op.create_index(
            op.f("ix_saved_views_view_type"),
            "saved_views",
            ["view_type"],
            unique=False,
        )
    if _has_column(bind, "variant_groups", "degraded_evals"):
        op.create_index(
            op.f("ix_variant_groups_degraded_evals"),
            "variant_groups",
            ["degraded_evals"],
            unique=False,
        )
    if _has_column(bind, "lifecycle_maps", "account_id"):
        op.create_index(
            op.f("ix_lifecycle_maps_account_id"),
            "lifecycle_maps",
            ["account_id"],
            unique=False,
        )
    if _has_column(bind, "feedback_records", "account_id"):
        op.create_index(
            op.f("ix_feedback_records_account_id"),
            "feedback_records",
            ["account_id"],
            unique=False,
        )
    if _has_column(bind, "agents", "account_id"):
        op.create_index(
            op.f("ix_agent_account_id"),
            "agents",
            ["account_id"],
            unique=False,
        )
    if _has_column(bind, "pipeline_snapshots", "account_id"):
        op.create_index(
            op.f("ix_pipeline_snapshots_account_id"),
            "pipeline_snapshots",
            ["account_id"],
            unique=False,
            postgresql_where=sa.text("account_id IS NOT NULL"),
        )


def _remove_missing_indexes() -> None:
    op.drop_index(op.f("ix_notifications_target_user_id"), table_name="notifications")
    op.drop_index(op.f("ix_scheduled_reports_created_by"), table_name="scheduled_reports")
    op.drop_index(op.f("ix_connector_instances_account_id"), table_name="connector_instances")
    op.drop_index(op.f("ix_node_observations_account_id"), table_name="node_observations")
    op.drop_index(op.f("ix_primitive_ratings_account_id"), table_name="primitive_ratings")
    op.drop_index(op.f("ix_saved_views_view_type"), table_name="saved_views")
    op.drop_index(op.f("ix_variant_groups_degraded_evals"), table_name="variant_groups")
    op.drop_index(op.f("ix_lifecycle_maps_account_id"), table_name="lifecycle_maps")
    op.drop_index(op.f("ix_feedback_records_account_id"), table_name="feedback_records")
    op.drop_index(op.f("ix_agent_account_id"), table_name="agents")
    op.drop_index(op.f("ix_pipeline_snapshots_account_id"), table_name="pipeline_snapshots")


def _add_missing_foreign_keys() -> None:
    op.create_foreign_key(
        "fk_mcp_setup_tokens_created_by",
        "mcp_setup_tokens",
        "accounts",
        ["created_by"],
        ["id"],
        ondelete="RESTRICT",
    )
    op.create_foreign_key(
        "fk_system_config_updated_by",
        "system_config",
        "accounts",
        ["updated_by"],
        ["id"],
        ondelete="SET NULL",
    )
    op.create_foreign_key(
        "fk_composite_templates_input_schema_id",
        "composite_templates",
        "schemas",
        ["input_schema_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.create_foreign_key(
        "fk_composite_templates_output_schema_id",
        "composite_templates",
        "schemas",
        ["output_schema_id"],
        ["id"],
        ondelete="SET NULL",
    )


def _remove_missing_foreign_keys() -> None:
    op.drop_constraint("fk_mcp_setup_tokens_created_by", "mcp_setup_tokens", type_="foreignkey")
    op.drop_constraint("fk_system_config_updated_by", "system_config", type_="foreignkey")
    op.drop_constraint("fk_composite_templates_input_schema_id", "composite_templates", type_="foreignkey")
    op.drop_constraint("fk_composite_templates_output_schema_id", "composite_templates", type_="foreignkey")


def _fix_column_types() -> None:
    op.alter_column(
        "eval_definitions",
        "pass_threshold",
        type_=sa.Numeric(8, 4),
        postgresql_using="pass_threshold::numeric(8,4)",
    )


def _revert_column_types() -> None:
    op.alter_column(
        "eval_definitions",
        "pass_threshold",
        type_=sa.Float(),
        postgresql_using="pass_threshold::double precision",
    )


def _add_trigger_based_fk_checks() -> None:
    op.execute(
        sa.text("""
        DO $$
        BEGIN
            IF NOT EXISTS (
                SELECT 1 FROM pg_trigger
                WHERE tgname = 'trg_org_api_keys_account_id_tenant'
            ) THEN
                CREATE TRIGGER "trg_org_api_keys_account_id_tenant"
                BEFORE INSERT OR UPDATE OF "account_id", "organisation_id" ON "org_api_keys"
                FOR EACH ROW EXECUTE FUNCTION enforce_same_organisation('accounts', 'account_id');
            END IF;
        END $$;
        """)
    )


def _remove_trigger_based_fk_checks() -> None:
    op.execute(
        sa.text("""
        DROP TRIGGER IF EXISTS "trg_org_api_keys_account_id_tenant" ON "org_api_keys";
        """)
    )
