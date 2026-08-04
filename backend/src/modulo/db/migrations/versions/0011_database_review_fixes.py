"""Database schema review fixes.

Adds missing indexes, FK constraints, and addresses column type issues
identified by the comprehensive database schema review (Reviews/database-schema-review.md).

Changes:
  — Add missing indexes on commonly-filtered FK columns
  — Add missing FK constraints on UUID columns that reference other tables
  — Change eval_definitions.pass_threshold from Float to Numeric
  — Expand string lengths where needed

The index/FK creation is DRIFT-TOLERANT: each ``op.create_index`` /
``op.create_foreign_key`` is guarded by an existence check on the target table
and its columns. Pre-squash staging databases lack several 0005 tables
(``mcp_setup_tokens``, ``lifecycle_maps``) or carry a legacy
``scheduled_reports`` shape, which used to hard-fail ``alembic upgrade heads``
and block every deploy. Missing targets are now skipped with a printed warning
and repaired by ``0065_reconcile_staging_schema``. On a healthy schema every
guard passes, so behaviour is unchanged.

Revision ID: 0011_database_review_fixes
Revises: 0010_fix_enforce_same_organisation_non_uuid
Create Date: 2026-07-15

"""

from collections.abc import Sequence
from typing import Any

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


def _table_exists(bind: Any, table: str) -> bool:
    return sa.inspect(bind).has_table(table)


def _warn(message: str) -> None:
    print(message)  # noqa: T201 - printed warning surfaces in deploy logs


def _column_exists(bind: Any, table: str, column: str) -> bool:
    insp = sa.inspect(bind)
    if not insp.has_table(table):
        return False
    return any(c["name"] == column for c in insp.get_columns(table))


def _create_index_if_present(bind: Any, index_name: str, table: str, columns: list[str], **kwargs: object) -> None:
    """Create an index only if the target table and its columns exist.

    Pre-squash staging databases may be missing entire 0005 tables
    (``mcp_setup_tokens``, ``lifecycle_maps``) or carry a legacy
    ``scheduled_reports`` shape without ``created_by``. Rather than hard-fail
    ``alembic upgrade heads`` and block deploys, skip with a printed warning;
    ``0065_reconcile_staging_schema`` repairs the drift afterwards.
    """
    if not _table_exists(bind, table):
        _warn(f"SKIP index {index_name} on {table}: table missing (drift-tolerant)")
        return
    missing = [c for c in columns if not _column_exists(bind, table, c)]
    if missing:
        _warn(f"SKIP index {index_name} on {table}.{', '.join(missing)}: missing column(s) (drift-tolerant)")
        return
    op.create_index(index_name, table, columns, unique=False, **kwargs)


def _create_foreign_key_if_present(
    bind: Any,
    constraint_name: str,
    table: str,
    referent_table: str,
    columns: list[str],
    referent_columns: list[str],
    ondelete: str | None = None,
) -> None:
    """Create an FK only if the source table, source columns, and target exist.

    See ``_create_index_if_present`` for the drift-tolerance rationale.
    """
    if not _table_exists(bind, table):
        _warn(f"SKIP FK {constraint_name} on {table}: table missing (drift-tolerant)")
        return
    missing = [c for c in columns if not _column_exists(bind, table, c)]
    if missing:
        _warn(f"SKIP FK {constraint_name} on {table}: missing column(s) {', '.join(missing)} (drift-tolerant)")
        return
    if not _table_exists(bind, referent_table):
        _warn(f"SKIP FK {constraint_name} on {table}: referent table {referent_table} missing (drift-tolerant)")
        return
    missing_ref = [c for c in referent_columns if not _column_exists(bind, referent_table, c)]
    if missing_ref:
        _warn(
            f"SKIP FK {constraint_name} on {table}: missing referent column(s) "
            f"{referent_table}.{', '.join(missing_ref)} (drift-tolerant)"
        )
        return
    op.create_foreign_key(
        constraint_name,
        table,
        referent_table,
        columns,
        referent_columns,
        ondelete=ondelete,
    )


def _add_missing_indexes() -> None:
    bind = op.get_bind()
    _create_index_if_present(
        bind,
        op.f("ix_notifications_target_user_id"),
        "notifications",
        ["target_user_id"],
        postgresql_where=sa.text("target_user_id IS NOT NULL"),
    )
    _create_index_if_present(
        bind,
        op.f("ix_scheduled_reports_created_by"),
        "scheduled_reports",
        ["created_by"],
        postgresql_where=sa.text("created_by IS NOT NULL"),
    )
    _create_index_if_present(
        bind,
        op.f("ix_connector_instances_account_id"),
        "connector_instances",
        ["account_id"],
    )
    _create_index_if_present(
        bind,
        op.f("ix_node_observations_account_id"),
        "node_observations",
        ["account_id"],
        postgresql_where=sa.text("account_id IS NOT NULL"),
    )
    _create_index_if_present(
        bind,
        op.f("ix_primitive_ratings_account_id"),
        "primitive_ratings",
        ["account_id"],
        postgresql_where=sa.text("account_id IS NOT NULL"),
    )
    _create_index_if_present(
        bind,
        op.f("ix_saved_views_view_type"),
        "saved_views",
        ["view_type"],
    )
    _create_index_if_present(
        bind,
        op.f("ix_variant_groups_degraded_evals"),
        "variant_groups",
        ["degraded_evals"],
    )
    _create_index_if_present(
        bind,
        op.f("ix_lifecycle_maps_account_id"),
        "lifecycle_maps",
        ["account_id"],
    )
    _create_index_if_present(
        bind,
        op.f("ix_feedback_records_account_id"),
        "feedback_records",
        ["account_id"],
    )
    _create_index_if_present(
        bind,
        op.f("ix_agent_account_id"),
        "agents",
        ["account_id"],
    )
    _create_index_if_present(
        bind,
        op.f("ix_pipeline_snapshots_account_id"),
        "pipeline_snapshots",
        ["account_id"],
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
    bind = op.get_bind()
    _create_foreign_key_if_present(
        bind,
        "fk_mcp_setup_tokens_created_by",
        "mcp_setup_tokens",
        "accounts",
        ["created_by"],
        ["id"],
        ondelete="RESTRICT",
    )
    _create_foreign_key_if_present(
        bind,
        "fk_system_config_updated_by",
        "system_config",
        "accounts",
        ["updated_by"],
        ["id"],
        ondelete="SET NULL",
    )
    _create_foreign_key_if_present(
        bind,
        "fk_composite_templates_input_schema_id",
        "composite_templates",
        "schemas",
        ["input_schema_id"],
        ["id"],
        ondelete="SET NULL",
    )
    _create_foreign_key_if_present(
        bind,
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
