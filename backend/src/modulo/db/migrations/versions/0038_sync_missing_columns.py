"""Sync all tables with ORM model columns that were added without migration.

Revision ID: 0038_sync_missing_columns
Revises: 0036_conditional_edges
Create Date: 2026-06-28
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0038_sync_missing_columns"
down_revision: str | Sequence[str] | None = "0036_conditional_edges"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _add_col(table: str, col: str, col_type: str, default: str | None = None) -> None:
    sql = f"ALTER TABLE {table} ADD COLUMN IF NOT EXISTS {col} {col_type}"
    if default is not None:
        sql += f" DEFAULT {default}"
    op.execute(sql)


def _add_fk_if_not_exists(table: str, constraint: str, col: str, ref_table: str, ref_col: str, on_delete: str) -> None:
    op.execute(
        f"DO $$ BEGIN "
        f"IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = '{constraint}') THEN "
        f"ALTER TABLE {table} ADD CONSTRAINT {constraint} "
        f"FOREIGN KEY ({col}) REFERENCES {ref_table}({ref_col}) ON DELETE {on_delete}; "
        f"END IF; END $$;"
    )


def upgrade() -> None:
    # agents
    _add_col("agents", "max_input_length", "INTEGER")
    _add_col("agents", "token_budget", "INTEGER")
    _add_col("agents", "library_id", "UUID")
    _add_fk_if_not_exists("agents", "fk_agents_library_id", "library_id", "library_primitives", "id", "SET NULL")

    # pipeline_snapshots
    _add_col("pipeline_snapshots", "environment_profile_id", "UUID")
    _add_col("pipeline_snapshots", "default_autonomy_level", "VARCHAR(30)")
    _add_col("pipeline_snapshots", "tag", "VARCHAR(100)")
    _add_col("pipeline_snapshots", "notes", "VARCHAR(2000)")
    _add_col("pipeline_snapshots", "config_json", "JSON", "'{}'")
    _add_col("pipeline_snapshots", "run_context_defaults", "JSON", "'{}'")

    # pipelines (additional columns from model)
    _add_col("pipelines", "node_timeout_seconds", "INTEGER", "300")
    _add_col("pipelines", "max_concurrent_runs", "INTEGER", "1")
    _add_col("pipelines", "default_autonomy_level", "VARCHAR(30)")
    _add_col("pipelines", "environment_profile_id", "UUID")
    _add_col("pipelines", "description", "VARCHAR(2000)")
    _add_col("pipelines", "config_json", "JSON", "'{}'")
    _add_col("pipelines", "run_context_defaults", "JSON", "'{}'")
    _add_col("pipelines", "tags", "JSON", "'[]'")

    # runs
    _add_col("runs", "run_context_override_json", "JSON")
    _add_col("runs", "trigger_event_id", "UUID")
    _add_col("runs", "parent_run_id", "UUID")
    _add_col("runs", "correction_run_id", "UUID")

    # eval_results
    _add_col("eval_results", "node_id", "UUID")

    # eval_definitions
    _add_col("eval_definitions", "pass_threshold", "FLOAT")
    _add_col("eval_definitions", "suite_id", "VARCHAR(255)")

    # feedback_records
    _add_col("feedback_records", "gate_id", "VARCHAR(255)")
    _add_col("feedback_records", "rejected_by", "UUID")
    _add_col("feedback_records", "rejection_reason", "TEXT")
    _add_col("feedback_records", "rejected_output", "JSON", "'{}'")
    _add_col("feedback_records", "producing_node_id", "VARCHAR(255)")
    _add_col("feedback_records", "producing_agent_id", "UUID")
    _add_col("feedback_records", "feedback_status", "VARCHAR(30)", "'pending'")
    _add_col("feedback_records", "feedback_handler_type", "VARCHAR(30)", "'human'")
    _add_col("feedback_records", "correction_run_id", "UUID")
    _add_col("feedback_records", "eval_gap", "BOOLEAN")
    _add_col("feedback_records", "needs_human_review", "BOOLEAN", "false")

    # stages
    _add_col("stages", "description", "VARCHAR(2000)")
    _add_col("stages", "position", "INTEGER", "0")
    _add_col("stages", "owner_team_id", "UUID")
    _add_col("stages", "visibility", "VARCHAR(10)", "'org'")

    # model_backends
    _add_col("model_backends", "display_name", "VARCHAR(255)")
    _add_col("model_backends", "credentials_ciphertext", "BYTEA", "'\\x'")
    _add_col("model_backends", "default_params", "JSON", "'{}'")
    _add_col("model_backends", "cost_tracking", "VARCHAR(10)", "'enabled'")
    _add_col("model_backends", "currency", "VARCHAR(3)", "'USD'")
    _add_col("model_backends", "owner_team_id", "UUID")
    _add_col("model_backends", "status", "VARCHAR(30)", "'active'")
    _add_col("model_backends", "last_health_check_at", "TIMESTAMPTZ")
    _add_col("model_backends", "last_health_check_error", "VARCHAR(2000)")
    _add_col("model_backends", "fallback_backend_ids", "JSON")

    # users
    _add_col("users", "display_name", "VARCHAR(255)")
    _add_col("users", "auth_provider", "VARCHAR(20)", "'local'")
    _add_col("users", "last_login_at", "TIMESTAMPTZ")
    _add_col("users", "is_active", "BOOLEAN", "true")
    _add_col("users", "preferences_json", "JSON", "'{}'")

    # api_keys
    _add_col("org_api_keys", "hashed_secret", "VARCHAR(255)")
    _add_col("org_api_keys", "lookup_prefix", "VARCHAR(20)")
    _add_col("org_api_keys", "expires_at", "TIMESTAMPTZ")


def downgrade() -> None:
    pass
