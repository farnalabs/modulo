"""v2 — Pipeline & Runtime Engine.

Combines 0003 (pipeline execution) and 0004 (runtime engine) to resolve
cross-dependency: agents in 0003 references model_backends in 0004.

Revision ID: 0003_v2_pipeline_runtime
Revises: 0002_v2_teams_library
Create Date: 2026-07-11

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0003_v2_pipeline_runtime"
down_revision: str | None = "0002_v2_teams_library"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


_STRICT_RLS: tuple[str, ...] = (
    "connector_instances",
    "model_backends",
    "notification_endpoints",
    "stages",
    "pipelines",
    "pipeline_edges",
    "environment_profiles",
    "pipeline_snapshots",
    "node_categories",
    "nodes",
    "triggers",
    "webhook_dedup_hashes",
    "agents",
    "runs",
    "eval_definitions",
    "eval_results",
    "hitl_claims",
    "node_observations",
    "feedback_records",
    "notification_delivery_log",
    "notifications",
    "trigger_events",
    "webhook_payloads",
)

_TEAM_SCOPED_RLS: tuple[str, ...] = (
    "pipelines",
    "stages",
    "connector_instances",
    "model_backends",
    "environment_profiles",
)

_TENANT_REFS: tuple[tuple[str, str, str], ...] = (
    ("connector_instances", "account_id", "accounts"),
    ("connector_instances", "owner_team_id", "teams"),
    ("model_backends", "account_id", "accounts"),
    ("model_backends", "owner_team_id", "teams"),
    ("notification_endpoints", "account_id", "accounts"),
    ("notification_endpoints", "team_id", "teams"),
    ("stages", "account_id", "accounts"),
    ("stages", "owner_team_id", "teams"),
    ("pipelines", "account_id", "accounts"),
    ("pipelines", "owner_team_id", "teams"),
    ("pipelines", "stage_id", "stages"),
    ("pipeline_edges", "pipeline_id", "pipelines"),
    ("environment_profiles", "account_id", "accounts"),
    ("environment_profiles", "owner_team_id", "teams"),
    ("pipeline_snapshots", "account_id", "accounts"),
    ("pipeline_snapshots", "pipeline_id", "pipelines"),
    ("pipeline_snapshots", "environment_profile_id", "environment_profiles"),
    ("node_categories", "account_id", "accounts"),
    ("nodes", "pipeline_id", "pipelines"),
    ("nodes", "parent_node_id", "nodes"),
    ("nodes", "account_id", "accounts"),
    ("triggers", "account_id", "accounts"),
    ("triggers", "pipeline_id", "pipelines"),
    ("webhook_dedup_hashes", "trigger_id", "triggers"),
    ("agents", "account_id", "accounts"),
    ("agents", "input_schema_id", "schemas"),
    ("agents", "input_schema_version", "schema_versions"),
    ("agents", "output_schema_id", "schemas"),
    ("agents", "output_schema_version", "schema_versions"),
    ("agents", "model_backend_id", "model_backends"),
    ("agents", "library_id", "library_primitives"),
    ("runs", "account_id", "accounts"),
    ("runs", "owner_team_id", "teams"),
    ("runs", "parent_run_id", "runs"),
    ("runs", "pipeline_id", "pipelines"),
    ("runs", "snapshot_id", "pipeline_snapshots"),
    ("runs", "trigger_id", "triggers"),
    ("eval_definitions", "account_id", "accounts"),
    ("eval_definitions", "pipeline_id", "pipelines"),
    ("eval_results", "eval_id", "eval_definitions"),
    ("eval_results", "run_id", "runs"),
    ("hitl_claims", "account_id", "accounts"),
    ("hitl_claims", "required_team_id", "teams"),
    ("hitl_claims", "pipeline_id", "pipelines"),
    ("hitl_claims", "run_id", "runs"),
    ("node_observations", "account_id", "accounts"),
    ("node_observations", "run_id", "runs"),
    ("feedback_records", "account_id", "accounts"),
    ("feedback_records", "correction_run_id", "runs"),
    ("feedback_records", "producing_agent_id", "agents"),
    ("feedback_records", "run_id", "runs"),
    ("notification_delivery_log", "endpoint_id", "notification_endpoints"),
    ("notification_delivery_log", "run_id", "runs"),
    ("notifications", "target_user_id", "accounts"),
    ("trigger_events", "trigger_id", "triggers"),
    ("trigger_events", "run_id", "runs"),
    ("webhook_payloads", "trigger_event_id", "trigger_events"),
)


def upgrade() -> None:
    _create_tables()
    _create_triggers()
    _enable_rls()


def _create_tables() -> None:
    op.create_table(
        "connector_instances",
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("connector_type_id", sa.String(length=255), nullable=False),
        sa.Column("account_id", sa.Uuid(), nullable=False),
        sa.Column("owner_team_id", sa.Uuid(), nullable=True),
        sa.Column("visibility", sa.String(length=10), server_default="org", nullable=False),
        sa.Column("credentials_ciphertext", sa.LargeBinary(), nullable=False),
        sa.Column("config_json", sa.JSON(), nullable=False),
        sa.Column("allowed_operations", sa.JSON(), nullable=False),
        sa.Column("status", sa.String(length=30), server_default="active", nullable=False),
        sa.Column("last_health_check_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_health_check_error", sa.String(length=2000), nullable=True),
        sa.Column("tier", sa.String(length=20), server_default="native", nullable=False),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("organisation_id", sa.Uuid(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False
        ),
        sa.CheckConstraint("visibility IN ('org', 'team')", name="ck_connector_instances_visibility"),
        sa.CheckConstraint("visibility = 'org' OR owner_team_id IS NOT NULL", name="ck_connector_instances_team_owner"),
        sa.CheckConstraint("tier IN ('native', 'preview', 'in_dev')", name="ck_connector_instances_tier"),
        sa.ForeignKeyConstraint(["account_id"], ["accounts.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["organisation_id"], ["organisations.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["owner_team_id"], ["teams.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        op.f("ix_connector_instances_connector_type_id"), "connector_instances", ["connector_type_id"], unique=False
    )
    op.create_index(
        op.f("ix_connector_instances_organisation_id"), "connector_instances", ["organisation_id"], unique=False
    )
    op.create_table(
        "model_backends",
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("display_name", sa.String(length=255), nullable=False),
        sa.Column("provider", sa.String(length=30), nullable=False),
        sa.Column("model_id", sa.String(length=255), nullable=False),
        sa.Column("credentials_ciphertext", sa.LargeBinary(), nullable=False),
        sa.Column("default_params", sa.JSON(), nullable=False),
        sa.Column("cost_tracking", sa.String(length=10), server_default="enabled", nullable=False),
        sa.Column("currency", sa.String(length=3), server_default="USD", nullable=False),
        sa.Column("owner_team_id", sa.Uuid(), nullable=True),
        sa.Column("visibility", sa.String(length=10), server_default="org", nullable=False),
        sa.Column("status", sa.String(length=30), server_default="active", nullable=False),
        sa.Column("last_health_check_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_health_check_error", sa.String(length=2000), nullable=True),
        sa.Column("fallback_backend_ids", sa.JSON(), nullable=True),
        sa.Column("account_id", sa.Uuid(), nullable=False),
        sa.Column("tier", sa.String(length=20), server_default="native", nullable=False),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("organisation_id", sa.Uuid(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False
        ),
        sa.CheckConstraint("cost_tracking IN ('enabled', 'disabled')", name="ck_model_backends_cost"),
        sa.CheckConstraint(
            "provider IN ('ai21', 'anthropic', 'azure_openai', 'bedrock', 'cohere', 'custom', 'deepseek', 'fireworks', 'gemini', 'grok', 'groq', 'jan', 'llamacpp', 'lm_studio', 'localai', 'mistral', 'ollama', 'openai', 'opencode', 'openrouter', 'perplexity', 'qwen', 'replicate', 'tgi', 'togetherai', 'vertexai', 'vllm', 'watsonx')",
            name="ck_model_backends_provider",
        ),
        sa.CheckConstraint("visibility IN ('org', 'team')", name="ck_model_backends_visibility"),
        sa.CheckConstraint("visibility = 'org' OR owner_team_id IS NOT NULL", name="ck_model_backends_team_owner"),
        sa.CheckConstraint("tier IN ('native', 'preview', 'in_dev')", name="ck_model_backends_tier"),
        sa.ForeignKeyConstraint(["account_id"], ["accounts.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["organisation_id"], ["organisations.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["owner_team_id"], ["teams.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_model_backends_organisation_id"), "model_backends", ["organisation_id"], unique=False)
    op.create_table(
        "notification_endpoints",
        sa.Column("url", sa.String(length=2048), nullable=False),
        sa.Column("secret_ciphertext", sa.LargeBinary(), nullable=True),
        sa.Column("events", sa.JSON(), server_default=sa.text("'[]'"), nullable=False),
        sa.Column("description", sa.String(length=500), nullable=True),
        sa.Column("consecutive_dead_letter_count", sa.Integer(), server_default="0", nullable=False),
        sa.Column("auto_disabled", sa.Boolean(), server_default="false", nullable=False),
        sa.Column("disabled_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("account_id", sa.Uuid(), nullable=True),
        sa.Column("team_id", sa.Uuid(), nullable=True),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("organisation_id", sa.Uuid(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False
        ),
        sa.ForeignKeyConstraint(["account_id"], ["accounts.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["organisation_id"], ["organisations.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["team_id"], ["teams.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        op.f("ix_notification_endpoints_organisation_id"), "notification_endpoints", ["organisation_id"], unique=False
    )
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
    op.create_table(
        "pipelines",
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("description", sa.String(length=2000), nullable=True),
        sa.Column("stage_id", sa.Uuid(), nullable=True),
        sa.Column("owner_team_id", sa.Uuid(), nullable=True),
        sa.Column("visibility", sa.String(length=10), server_default="org", nullable=False),
        sa.Column("max_concurrent_runs", sa.Integer(), server_default="5", nullable=False),
        sa.Column("lock_wait_timeout_seconds", sa.Integer(), server_default="300", nullable=False),
        sa.Column("node_timeout_seconds", sa.Integer(), server_default="300", nullable=False),
        sa.Column("max_duration_seconds", sa.Integer(), nullable=True),
        sa.Column("max_steps", sa.Integer(), nullable=True),
        sa.Column("token_budget", sa.Integer(), nullable=True),
        sa.Column("archived_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("run_context_defaults", sa.JSON(), nullable=False),
        sa.Column("default_autonomy_level", sa.String(length=30), server_default="manual_approval", nullable=True),
        sa.Column("graph_nodes_json", sa.JSON(), server_default=sa.text("'[]'"), nullable=False),
        sa.Column("default_feedback_handler", sa.String(length=50), nullable=True),
        sa.Column("account_id", sa.Uuid(), nullable=False),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("organisation_id", sa.Uuid(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False
        ),
        sa.CheckConstraint("visibility IN ('org', 'team')", name="ck_pipelines_visibility"),
        sa.CheckConstraint("visibility = 'org' OR owner_team_id IS NOT NULL", name="ck_pipelines_team_owner"),
        sa.CheckConstraint(
            "default_autonomy_level IN ('manual_approval', 'notify_on_complete', 'fully_autonomous')",
            name="ck_pipelines_autonomy_level",
        ),
        sa.CheckConstraint("max_concurrent_runs > 0", name="ck_pipelines_max_concurrent_runs"),
        sa.CheckConstraint("lock_wait_timeout_seconds BETWEEN 30 AND 3600", name="ck_pipelines_lock_wait_timeout"),
        sa.CheckConstraint("node_timeout_seconds > 0", name="ck_pipelines_node_timeout"),
        sa.ForeignKeyConstraint(["account_id"], ["accounts.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["organisation_id"], ["organisations.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["owner_team_id"], ["teams.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["stage_id"], ["stages.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_pipelines_organisation_id"), "pipelines", ["organisation_id"], unique=False)
    op.create_table(
        "pipeline_edges",
        sa.Column("pipeline_id", sa.Uuid(), nullable=False),
        sa.Column("source_node_id", sa.Uuid(), nullable=False),
        sa.Column("target_node_id", sa.Uuid(), nullable=False),
        sa.Column("edge_type", sa.String(length=15), server_default="normal", nullable=False),
        sa.Column("hitl_gate_config", sa.JSON(), nullable=True),
        sa.Column("condition_expression", sa.String(length=500), nullable=True),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("organisation_id", sa.Uuid(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False
        ),
        sa.CheckConstraint("edge_type IN ('normal', 'reject', 'conditional')", name="ck_pipeline_edges_type"),
        sa.ForeignKeyConstraint(["organisation_id"], ["organisations.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["pipeline_id"], ["pipelines.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "pipeline_id", "source_node_id", "target_node_id", "edge_type", name="uq_pipeline_edges_path"
        ),
    )
    op.create_index(op.f("ix_pipeline_edges_organisation_id"), "pipeline_edges", ["organisation_id"], unique=False)
    op.create_index(op.f("ix_pipeline_edges_pipeline_id"), "pipeline_edges", ["pipeline_id"], unique=False)
    op.create_table(
        "environment_profiles",
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("provider_type", sa.String(length=50), server_default="local_docker", nullable=False),
        sa.Column("image_ref", sa.String(length=500), nullable=True),
        sa.Column("capabilities_json", sa.JSON(), nullable=False),
        sa.Column("config_json", sa.JSON(), nullable=False),
        sa.Column("network_policy", sa.String(length=20), server_default="outbound", nullable=False),
        sa.Column("initialisation_strategy", sa.String(length=30), server_default="git_clone", nullable=False),
        sa.Column("secret_refs_json", sa.JSON(), nullable=False),
        sa.Column("persistence_policy", sa.String(length=20), server_default="ephemeral", nullable=False),
        sa.Column("status", sa.String(length=30), server_default="active", nullable=False),
        sa.Column("account_id", sa.Uuid(), nullable=False),
        sa.Column("owner_team_id", sa.Uuid(), nullable=True),
        sa.Column("visibility", sa.String(length=10), server_default="org", nullable=False),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("organisation_id", sa.Uuid(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False
        ),
        sa.CheckConstraint("provider_type IN ('local_docker', 'e2b')", name="ck_env_profiles_provider_type"),
        sa.CheckConstraint("network_policy IN ('none', 'outbound', 'selected')", name="ck_env_profiles_network_policy"),
        sa.CheckConstraint(
            "persistence_policy IN ('ephemeral', 'retained', 'cache')", name="ck_env_profiles_persistence_policy"
        ),
        sa.CheckConstraint("visibility IN ('org', 'team')", name="ck_env_profiles_visibility"),
        sa.ForeignKeyConstraint(["account_id"], ["accounts.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["organisation_id"], ["organisations.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["owner_team_id"], ["teams.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        op.f("ix_environment_profiles_organisation_id"), "environment_profiles", ["organisation_id"], unique=False
    )
    op.create_table(
        "pipeline_snapshots",
        sa.Column("pipeline_id", sa.Uuid(), nullable=False),
        sa.Column("snapshot_version", sa.Integer(), nullable=False),
        sa.Column("account_id", sa.Uuid(), nullable=True),
        sa.Column("environment_profile_id", sa.Uuid(), nullable=True),
        sa.Column("graph_json", sa.JSON(), nullable=False),
        sa.Column("connector_bindings_json", sa.JSON(), nullable=False),
        sa.Column("schema_pins_json", sa.JSON(), nullable=False),
        sa.Column("prompt_pins_json", sa.JSON(), nullable=False),
        sa.Column("model_backend_pins_json", sa.JSON(), nullable=False),
        sa.Column("composite_bindings_json", sa.JSON(), nullable=False),
        sa.Column("tag", sa.String(length=100), nullable=True),
        sa.Column("notes", sa.String(length=2000), nullable=True),
        sa.Column("default_autonomy_level", sa.String(length=30), nullable=True),
        sa.Column("config_json", sa.JSON(), nullable=False),
        sa.Column("run_context_defaults", sa.JSON(), nullable=False),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("organisation_id", sa.Uuid(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False
        ),
        sa.ForeignKeyConstraint(["account_id"], ["accounts.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["environment_profile_id"], ["environment_profiles.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["organisation_id"], ["organisations.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["pipeline_id"], ["pipelines.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("pipeline_id", "snapshot_version", name="uq_pipeline_snapshot_version"),
    )
    op.create_index(
        op.f("ix_pipeline_snapshots_environment_profile_id"),
        "pipeline_snapshots",
        ["environment_profile_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_pipeline_snapshots_organisation_id"), "pipeline_snapshots", ["organisation_id"], unique=False
    )
    op.create_index(op.f("ix_pipeline_snapshots_pipeline_id"), "pipeline_snapshots", ["pipeline_id"], unique=False)
    op.create_table(
        "node_categories",
        sa.Column("name", sa.String(length=100), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("color", sa.String(length=7), nullable=False),
        sa.Column("icon", sa.String(length=50), nullable=True),
        sa.Column("sort_order", sa.Integer(), nullable=False),
        sa.Column("account_id", sa.Uuid(), nullable=False),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("organisation_id", sa.Uuid(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False
        ),
        sa.ForeignKeyConstraint(["account_id"], ["accounts.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["organisation_id"], ["organisations.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("organisation_id", "name", name="uq_node_categories_org_name"),
    )
    op.create_index(op.f("ix_node_categories_organisation_id"), "node_categories", ["organisation_id"], unique=False)
    op.create_table(
        "nodes",
        sa.Column("pipeline_id", sa.Uuid(), nullable=False),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("parent_node_id", sa.Uuid(), nullable=True),
        sa.Column("timeout_seconds", sa.Integer(), nullable=True),
        sa.Column("retry_count", sa.Integer(), nullable=True),
        sa.Column("retry_delay_seconds", sa.Integer(), nullable=True),
        sa.Column("account_id", sa.Uuid(), nullable=False),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("organisation_id", sa.Uuid(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False
        ),
        sa.CheckConstraint("timeout_seconds IS NULL OR timeout_seconds > 0", name="ck_nodes_timeout_seconds"),
        sa.CheckConstraint("retry_count IS NULL OR retry_count >= 0", name="ck_nodes_retry_count"),
        sa.CheckConstraint(
            "retry_delay_seconds IS NULL OR retry_delay_seconds >= 0", name="ck_nodes_retry_delay_seconds"
        ),
        sa.ForeignKeyConstraint(["account_id"], ["accounts.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["organisation_id"], ["organisations.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["parent_node_id"], ["nodes.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["pipeline_id"], ["pipelines.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_nodes_organisation_id"), "nodes", ["organisation_id"], unique=False)
    op.create_index(op.f("ix_nodes_parent_node_id"), "nodes", ["parent_node_id"], unique=False)
    op.create_index(op.f("ix_nodes_pipeline_id"), "nodes", ["pipeline_id"], unique=False)
    op.create_table(
        "triggers",
        sa.Column("pipeline_id", sa.Uuid(), nullable=False),
        sa.Column("trigger_type", sa.String(length=20), nullable=False),
        sa.Column("active", sa.Boolean(), server_default="true", nullable=False),
        sa.Column("max_concurrent_runs", sa.Integer(), server_default="1", nullable=False),
        sa.Column("daily_spend_limit", sa.Numeric(precision=12, scale=4), nullable=True),
        sa.Column("config_json", sa.JSON(), nullable=False),
        sa.Column("account_id", sa.Uuid(), nullable=False),
        sa.Column("cron_expression", sa.String(length=100), nullable=True),
        sa.Column("cron_timezone", sa.String(length=50), nullable=True),
        sa.Column("last_fired_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("next_fire_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("organisation_id", sa.Uuid(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False
        ),
        sa.CheckConstraint(
            "trigger_type IN ('manual', 'webhook', 'cron', 'polling', 'agent_signal')", name="ck_triggers_type"
        ),
        sa.CheckConstraint("max_concurrent_runs > 0", name="ck_triggers_max_concurrent_runs"),
        sa.ForeignKeyConstraint(["account_id"], ["accounts.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["organisation_id"], ["organisations.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["pipeline_id"], ["pipelines.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_triggers_next_fire_at"), "triggers", ["next_fire_at"], unique=False)
    op.create_index(op.f("ix_triggers_organisation_id"), "triggers", ["organisation_id"], unique=False)
    op.create_index(op.f("ix_triggers_pipeline_id"), "triggers", ["pipeline_id"], unique=False)
    op.create_table(
        "webhook_dedup_hashes",
        sa.Column("trigger_id", sa.Uuid(), nullable=False),
        sa.Column("payload_hash", sa.String(length=64), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("organisation_id", sa.Uuid(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False
        ),
        sa.ForeignKeyConstraint(["organisation_id"], ["organisations.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["trigger_id"], ["triggers.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("trigger_id", "payload_hash", name="uq_webhook_dedup_trigger_hash"),
    )
    op.create_index(op.f("ix_webhook_dedup_hashes_expires_at"), "webhook_dedup_hashes", ["expires_at"], unique=False)
    op.create_index(
        op.f("ix_webhook_dedup_hashes_organisation_id"), "webhook_dedup_hashes", ["organisation_id"], unique=False
    )
    op.create_index(op.f("ix_webhook_dedup_hashes_trigger_id"), "webhook_dedup_hashes", ["trigger_id"], unique=False)
    op.create_table(
        "agents",
        sa.Column("is_executable", sa.Boolean(), nullable=False),
        sa.Column("prompt_always_visible", sa.Boolean(), nullable=False),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("description", sa.String(length=2000), nullable=True),
        sa.Column("input_schema_id", sa.Uuid(), nullable=False),
        sa.Column("input_schema_version", sa.String(length=50), nullable=False),
        sa.Column("output_schema_id", sa.Uuid(), nullable=False),
        sa.Column("output_schema_version", sa.String(length=50), nullable=False),
        sa.Column("prompt_template", sa.Text(), nullable=False),
        sa.Column("prompt_version_history", sa.JSON(), nullable=False),
        sa.Column("model_backend_id", sa.Uuid(), nullable=False),
        sa.Column("connector_type_refs", sa.JSON(), nullable=False),
        sa.Column("required_environment_capabilities", sa.JSON(), nullable=False),
        sa.Column("evals", sa.JSON(), nullable=True),
        sa.Column("retry_policy", sa.JSON(), nullable=False),
        sa.Column("max_input_length", sa.Integer(), nullable=True),
        sa.Column("token_budget", sa.Integer(), nullable=True),
        sa.Column("library_id", sa.Uuid(), nullable=True),
        sa.Column("account_id", sa.Uuid(), nullable=False),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("organisation_id", sa.Uuid(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False
        ),
        sa.ForeignKeyConstraint(["account_id"], ["accounts.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(
            ["input_schema_id", "input_schema_version", "organisation_id"],
            ["schema_versions.schema_id", "schema_versions.version", "schema_versions.organisation_id"],
            name="fk_agents_input_schema_version",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(["library_id"], ["library_primitives.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["model_backend_id"], ["model_backends.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["organisation_id"], ["organisations.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["output_schema_id", "output_schema_version", "organisation_id"],
            ["schema_versions.schema_id", "schema_versions.version", "schema_versions.organisation_id"],
            name="fk_agents_output_schema_version",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_agents_organisation_id"), "agents", ["organisation_id"], unique=False)
    op.create_table(
        "runs",
        sa.Column("pipeline_id", sa.Uuid(), nullable=False),
        sa.Column("snapshot_id", sa.Uuid(), nullable=False),
        sa.Column("trigger_id", sa.Uuid(), nullable=True),
        sa.Column("trigger_type", sa.String(length=20), nullable=False),
        sa.Column("status", sa.String(length=30), server_default="pending", nullable=False),
        sa.Column("parent_run_id", sa.Uuid(), nullable=True),
        sa.Column("run_number", sa.Integer(), nullable=False),
        sa.Column("owner_team_id", sa.Uuid(), nullable=True),
        sa.Column("account_id", sa.Uuid(), nullable=True),
        sa.Column("input_hash", sa.String(length=64), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("cancellation_requested", sa.Boolean(), server_default="false", nullable=False),
        sa.Column("total_tokens", sa.Integer(), nullable=True),
        sa.Column("total_cost_usd", sa.Numeric(precision=14, scale=6), nullable=True),
        sa.Column("node_token_usage", sa.JSON(), nullable=True),
        sa.Column("error_detail", sa.String(length=5000), nullable=True),
        sa.Column("error_code", sa.String(length=255), nullable=True),
        sa.Column("langgraph_thread_id", sa.String(length=512), nullable=False),
        sa.Column("input_payload", sa.JSON(), nullable=True),
        sa.Column("outputs_json", sa.JSON(), nullable=True),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("organisation_id", sa.Uuid(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False
        ),
        sa.CheckConstraint(
            "status IN ('pending', 'running', 'awaiting_human', 'claimed', 'waiting_for_lock', 'complete', 'failed', 'cancelled', 'eval_failed')",
            name="ck_runs_status",
        ),
        sa.CheckConstraint(
            "trigger_type IN ('manual', 'webhook', 'cron', 'polling', 'agent_signal', 'correction')",
            name="ck_runs_trigger_type",
        ),
        sa.ForeignKeyConstraint(["account_id"], ["accounts.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["organisation_id"], ["organisations.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["owner_team_id"], ["teams.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["parent_run_id"], ["runs.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["pipeline_id"], ["pipelines.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["snapshot_id"], ["pipeline_snapshots.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["trigger_id"], ["triggers.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("langgraph_thread_id"),
        sa.UniqueConstraint("organisation_id", "run_number", name="uq_runs_org_run_number"),
    )
    op.create_index(op.f("ix_runs_organisation_id"), "runs", ["organisation_id"], unique=False)
    op.create_index(op.f("ix_runs_parent_run_id"), "runs", ["parent_run_id"], unique=False)
    op.create_index(op.f("ix_runs_pipeline_id"), "runs", ["pipeline_id"], unique=False)
    op.create_table(
        "eval_definitions",
        sa.Column("pipeline_id", sa.Uuid(), nullable=False),
        sa.Column("node_id", sa.Uuid(), nullable=True),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("eval_type", sa.String(length=30), nullable=False),
        sa.Column("config_json", sa.JSON(), nullable=False),
        sa.Column("failure_behaviour", sa.String(length=10), server_default="warn", nullable=False),
        sa.Column("pass_threshold", sa.Float(), nullable=True),
        sa.Column("suite_id", sa.String(length=255), nullable=True),
        sa.Column("account_id", sa.Uuid(), nullable=False),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("organisation_id", sa.Uuid(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False
        ),
        sa.CheckConstraint(
            "eval_type IN ('llm_judge', 'regex', 'json_schema', 'custom_function')", name="ck_eval_definitions_type"
        ),
        sa.CheckConstraint("failure_behaviour IN ('warn', 'block')", name="ck_eval_definitions_failure_behaviour"),
        sa.ForeignKeyConstraint(["account_id"], ["accounts.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["organisation_id"], ["organisations.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["pipeline_id"], ["pipelines.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_eval_definitions_organisation_id"), "eval_definitions", ["organisation_id"], unique=False)
    op.create_index(op.f("ix_eval_definitions_pipeline_id"), "eval_definitions", ["pipeline_id"], unique=False)
    op.create_table(
        "eval_results",
        sa.Column("run_id", sa.Uuid(), nullable=False),
        sa.Column("node_id", sa.Uuid(), nullable=True),
        sa.Column("eval_id", sa.Uuid(), nullable=False),
        sa.Column("passed", sa.Boolean(), nullable=False),
        sa.Column("score", sa.Float(), nullable=True),
        sa.Column("detail", sa.String(length=2000), nullable=True),
        sa.Column(
            "evaluated_at", sa.DateTime(timezone=True), server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False
        ),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("organisation_id", sa.Uuid(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False
        ),
        sa.ForeignKeyConstraint(["eval_id"], ["eval_definitions.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["organisation_id"], ["organisations.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["run_id"], ["runs.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_eval_results_organisation_id"), "eval_results", ["organisation_id"], unique=False)
    op.create_index(op.f("ix_eval_results_run_id"), "eval_results", ["run_id"], unique=False)
    op.create_table(
        "hitl_claims",
        sa.Column("run_id", sa.Uuid(), nullable=False),
        sa.Column("required_team_id", sa.Uuid(), nullable=True),
        sa.Column("gate_id", sa.String(length=255), nullable=False),
        sa.Column("pipeline_id", sa.Uuid(), nullable=False),
        sa.Column("account_id", sa.Uuid(), nullable=True),
        sa.Column("claimed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("claim_token", sa.Text(), nullable=True),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("decision", sa.String(length=20), nullable=True),
        sa.Column("decision_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("delivered_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("organisation_id", sa.Uuid(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False
        ),
        sa.ForeignKeyConstraint(["account_id"], ["accounts.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["organisation_id"], ["organisations.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["pipeline_id"], ["pipelines.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["required_team_id"], ["teams.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["run_id"], ["runs.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("run_id", "gate_id", name="uq_hitl_claims_run_gate"),
    )
    op.create_index(op.f("ix_hitl_claims_organisation_id"), "hitl_claims", ["organisation_id"], unique=False)
    op.create_index(op.f("ix_hitl_claims_pipeline_id"), "hitl_claims", ["pipeline_id"], unique=False)
    op.create_index(op.f("ix_hitl_claims_run_id"), "hitl_claims", ["run_id"], unique=False)
    op.create_table(
        "node_observations",
        sa.Column("run_id", sa.Uuid(), nullable=False),
        sa.Column("node_id", sa.String(length=255), nullable=False),
        sa.Column("account_id", sa.Uuid(), nullable=True),
        sa.Column("human_observed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("organisation_id", sa.Uuid(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False
        ),
        sa.ForeignKeyConstraint(["account_id"], ["accounts.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["organisation_id"], ["organisations.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["run_id"], ["runs.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("run_id", "node_id", name="uq_node_observations_run_node"),
    )
    op.create_index(
        op.f("ix_node_observations_organisation_id"), "node_observations", ["organisation_id"], unique=False
    )
    op.create_index(op.f("ix_node_observations_run_id"), "node_observations", ["run_id"], unique=False)
    op.create_table(
        "feedback_records",
        sa.Column("run_id", sa.Uuid(), nullable=False),
        sa.Column("gate_id", sa.String(length=255), nullable=False),
        sa.Column("account_id", sa.Uuid(), nullable=False),
        sa.Column("rejection_reason", sa.Text(), nullable=False),
        sa.Column("rejected_output", sa.JSON(), nullable=False),
        sa.Column("producing_node_id", sa.String(length=255), nullable=False),
        sa.Column("producing_agent_id", sa.Uuid(), nullable=True),
        sa.Column("feedback_status", sa.String(length=20), server_default="pending", nullable=False),
        sa.Column("feedback_handler_type", sa.String(length=40), server_default="human", nullable=False),
        sa.Column("correction_run_id", sa.Uuid(), nullable=True),
        sa.Column("eval_gap", sa.Boolean(), nullable=True),
        sa.Column("needs_human_review", sa.Boolean(), nullable=True),
        sa.Column("annotation", sa.Text(), nullable=True),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("organisation_id", sa.Uuid(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False
        ),
        sa.CheckConstraint(
            "feedback_status IN ('pending', 'routing', 'correcting', 'resolved', 'escalated', 'dismissed')",
            name="ck_feedback_records_status",
        ),
        sa.CheckConstraint(
            "feedback_handler_type IN ('human', 'ai_correction', 'ai_correction_with_human_review')",
            name="ck_feedback_records_handler_type",
        ),
        sa.ForeignKeyConstraint(["account_id"], ["accounts.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["correction_run_id"], ["runs.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["organisation_id"], ["organisations.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["producing_agent_id"], ["agents.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["run_id"], ["runs.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_feedback_records_organisation_id"), "feedback_records", ["organisation_id"], unique=False)
    op.create_index(op.f("ix_feedback_records_run_id"), "feedback_records", ["run_id"], unique=False)
    op.create_table(
        "notification_delivery_log",
        sa.Column("event_type", sa.String(length=100), nullable=False),
        sa.Column("endpoint_id", sa.Uuid(), nullable=True),
        sa.Column("run_id", sa.Uuid(), nullable=True),
        sa.Column("status", sa.String(length=20), server_default="delivered", nullable=False),
        sa.Column("attempt_count", sa.Integer(), server_default="0", nullable=False),
        sa.Column("response_code", sa.Integer(), nullable=True),
        sa.Column("last_error", sa.String(length=2000), nullable=True),
        sa.Column("failed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("response_body", sa.String(length=2000), nullable=True),
        sa.Column("payload_ciphertext", sa.LargeBinary(), nullable=True),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("organisation_id", sa.Uuid(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False
        ),
        sa.ForeignKeyConstraint(["endpoint_id"], ["notification_endpoints.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["organisation_id"], ["organisations.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["run_id"], ["runs.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        op.f("ix_notification_delivery_log_endpoint_id"), "notification_delivery_log", ["endpoint_id"], unique=False
    )
    op.create_index(
        op.f("ix_notification_delivery_log_event_type"), "notification_delivery_log", ["event_type"], unique=False
    )
    op.create_index(
        op.f("ix_notification_delivery_log_organisation_id"),
        "notification_delivery_log",
        ["organisation_id"],
        unique=False,
    )
    op.create_index(op.f("ix_notification_delivery_log_run_id"), "notification_delivery_log", ["run_id"], unique=False)
    op.create_table(
        "notifications",
        sa.Column("scope", sa.String(length=20), nullable=False),
        sa.Column("target_user_id", sa.Uuid(), nullable=True),
        sa.Column("level", sa.String(length=20), nullable=False),
        sa.Column("category", sa.String(length=100), nullable=False),
        sa.Column("title", sa.String(length=500), nullable=False),
        sa.Column("body", sa.Text(), nullable=False),
        sa.Column("action_url", sa.String(length=2048), nullable=True),
        sa.Column("dismiss_strategy", sa.String(length=20), server_default="user_only", nullable=False),
        sa.Column("dismissible_at_scope", sa.Boolean(), server_default="false", nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("organisation_id", sa.Uuid(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False
        ),
        sa.CheckConstraint("scope IN ('user', 'org', 'admin')", name="ck_notifications_scope"),
        sa.CheckConstraint("level IN ('debug', 'info', 'warning', 'error')", name="ck_notifications_level"),
        sa.CheckConstraint(
            "dismiss_strategy IN ('user_only', 'org_admin', 'any_scope')", name="ck_notifications_dismiss_strategy"
        ),
        sa.ForeignKeyConstraint(["organisation_id"], ["organisations.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["target_user_id"], ["accounts.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_notifications_organisation_id"), "notifications", ["organisation_id"], unique=False)
    op.create_table(
        "dismissals",
        sa.Column("notification_id", sa.Uuid(), nullable=False),
        sa.Column("dismissed_by_user_id", sa.Uuid(), nullable=False),
        sa.Column("dismiss_scope", sa.String(length=20), nullable=False),
        sa.Column(
            "dismissed_at", sa.DateTime(timezone=True), server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False
        ),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.CheckConstraint("dismiss_scope IN ('self', 'scope')", name="ck_dismissals_scope"),
        sa.ForeignKeyConstraint(["dismissed_by_user_id"], ["accounts.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["notification_id"], ["notifications.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("notification_id", "dismissed_by_user_id", name="uq_dismissal_user_notification"),
    )
    op.create_index(op.f("ix_dismissals_notification_id"), "dismissals", ["notification_id"], unique=False)
    op.create_table(
        "trigger_events",
        sa.Column("trigger_id", sa.Uuid(), nullable=False),
        sa.Column("trigger_type", sa.String(length=20), nullable=False),
        sa.Column("raw_payload_hash", sa.String(length=64), nullable=False),
        sa.Column(
            "received_at", sa.DateTime(timezone=True), server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False
        ),
        sa.Column("validation_result", sa.String(length=50), nullable=False),
        sa.Column("run_id", sa.Uuid(), nullable=True),
        sa.Column("error_detail", sa.String(length=2000), nullable=True),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("organisation_id", sa.Uuid(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False
        ),
        sa.CheckConstraint(
            "validation_result IN ('accepted', 'passed', 'hmac_failed', 'schema_validation_failed', 'deduplicated', 'concurrency_limit_reached', 'flood_rejected', 'timestamp_expired', 'validation_failed', 'rate_limited', 'no_match', 'condition_met', 'poll_error', 'signal_fired')",
            name="ck_trigger_events_validation_result",
        ),
        sa.ForeignKeyConstraint(["organisation_id"], ["organisations.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["run_id"], ["runs.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["trigger_id"], ["triggers.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_trigger_events_organisation_id"), "trigger_events", ["organisation_id"], unique=False)
    op.create_index(op.f("ix_trigger_events_trigger_id"), "trigger_events", ["trigger_id"], unique=False)
    op.create_table(
        "webhook_payloads",
        sa.Column("trigger_event_id", sa.Uuid(), nullable=True),
        sa.Column("raw_body", sa.LargeBinary(), nullable=False),
        sa.Column("raw_payload", sa.JSON(), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("organisation_id", sa.Uuid(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False
        ),
        sa.ForeignKeyConstraint(["organisation_id"], ["organisations.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["trigger_event_id"], ["trigger_events.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_webhook_payloads_expires_at"), "webhook_payloads", ["expires_at"], unique=False)
    op.create_index(op.f("ix_webhook_payloads_organisation_id"), "webhook_payloads", ["organisation_id"], unique=False)


def _create_triggers() -> None:
    for child_table, child_column, parent_table in _TENANT_REFS:
        op.execute(
            sa.text(
                f'CREATE TRIGGER "trg_{child_table}_{child_column}_tenant" '
                f'BEFORE INSERT OR UPDATE OF "{child_column}", "organisation_id" ON "{child_table}" '
                f"FOR EACH ROW EXECUTE FUNCTION enforce_same_organisation('{parent_table}', '{child_column}')"
            )
        )


def _enable_rls() -> None:
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
    for table in _STRICT_RLS:
        op.execute(sa.text(f'ALTER TABLE "{table}" ENABLE ROW LEVEL SECURITY'))
        op.execute(sa.text(f'CREATE POLICY rls_org_isolation ON "{table}" USING ({strict})'))
    for table in _TEAM_SCOPED_RLS:
        op.execute(sa.text(f'CREATE POLICY rls_team_isolation ON "{table}" USING ({team})'))


def downgrade() -> None:
    for table in _TEAM_SCOPED_RLS:
        op.execute(sa.text(f'DROP POLICY IF EXISTS rls_team_isolation ON "{table}"'))
    for table in _STRICT_RLS:
        op.execute(sa.text(f'DROP POLICY IF EXISTS rls_org_isolation ON "{table}"'))
        op.execute(sa.text(f'ALTER TABLE "{table}" DISABLE ROW LEVEL SECURITY'))

    for child_table, child_column, _ in _TENANT_REFS:
        op.execute(sa.text(f'DROP TRIGGER IF EXISTS "trg_{child_table}_{child_column}_tenant" ON "{child_table}"'))

    op.drop_index(op.f("ix_webhook_payloads_organisation_id"), table_name="webhook_payloads")
    op.drop_index(op.f("ix_webhook_payloads_expires_at"), table_name="webhook_payloads")
    op.drop_table("webhook_payloads")
    op.drop_index(op.f("ix_trigger_events_trigger_id"), table_name="trigger_events")
    op.drop_index(op.f("ix_trigger_events_organisation_id"), table_name="trigger_events")
    op.drop_table("trigger_events")
    op.drop_index(op.f("ix_dismissals_notification_id"), table_name="dismissals")
    op.drop_table("dismissals")
    op.drop_index(op.f("ix_notifications_organisation_id"), table_name="notifications")
    op.drop_table("notifications")
    op.drop_index(op.f("ix_notification_delivery_log_run_id"), table_name="notification_delivery_log")
    op.drop_index(op.f("ix_notification_delivery_log_organisation_id"), table_name="notification_delivery_log")
    op.drop_index(op.f("ix_notification_delivery_log_event_type"), table_name="notification_delivery_log")
    op.drop_index(op.f("ix_notification_delivery_log_endpoint_id"), table_name="notification_delivery_log")
    op.drop_table("notification_delivery_log")
    op.drop_index(op.f("ix_feedback_records_run_id"), table_name="feedback_records")
    op.drop_index(op.f("ix_feedback_records_organisation_id"), table_name="feedback_records")
    op.drop_table("feedback_records")
    op.drop_index(op.f("ix_node_observations_run_id"), table_name="node_observations")
    op.drop_index(op.f("ix_node_observations_organisation_id"), table_name="node_observations")
    op.drop_table("node_observations")
    op.drop_index(op.f("ix_hitl_claims_run_id"), table_name="hitl_claims")
    op.drop_index(op.f("ix_hitl_claims_pipeline_id"), table_name="hitl_claims")
    op.drop_index(op.f("ix_hitl_claims_organisation_id"), table_name="hitl_claims")
    op.drop_table("hitl_claims")
    op.drop_index(op.f("ix_eval_results_run_id"), table_name="eval_results")
    op.drop_index(op.f("ix_eval_results_organisation_id"), table_name="eval_results")
    op.drop_table("eval_results")
    op.drop_index(op.f("ix_eval_definitions_pipeline_id"), table_name="eval_definitions")
    op.drop_index(op.f("ix_eval_definitions_organisation_id"), table_name="eval_definitions")
    op.drop_table("eval_definitions")
    op.drop_index(op.f("ix_runs_pipeline_id"), table_name="runs")
    op.drop_index(op.f("ix_runs_parent_run_id"), table_name="runs")
    op.drop_index(op.f("ix_runs_organisation_id"), table_name="runs")
    op.drop_table("runs")
    op.drop_index(op.f("ix_agents_organisation_id"), table_name="agents")
    op.drop_table("agents")
    op.drop_index(op.f("ix_webhook_dedup_hashes_trigger_id"), table_name="webhook_dedup_hashes")
    op.drop_index(op.f("ix_webhook_dedup_hashes_organisation_id"), table_name="webhook_dedup_hashes")
    op.drop_index(op.f("ix_webhook_dedup_hashes_expires_at"), table_name="webhook_dedup_hashes")
    op.drop_table("webhook_dedup_hashes")
    op.drop_index(op.f("ix_triggers_pipeline_id"), table_name="triggers")
    op.drop_index(op.f("ix_triggers_organisation_id"), table_name="triggers")
    op.drop_index(op.f("ix_triggers_next_fire_at"), table_name="triggers")
    op.drop_table("triggers")
    op.drop_index(op.f("ix_nodes_pipeline_id"), table_name="nodes")
    op.drop_index(op.f("ix_nodes_parent_node_id"), table_name="nodes")
    op.drop_index(op.f("ix_nodes_organisation_id"), table_name="nodes")
    op.drop_table("nodes")
    op.drop_index(op.f("ix_node_categories_organisation_id"), table_name="node_categories")
    op.drop_table("node_categories")
    op.drop_index(op.f("ix_pipeline_snapshots_pipeline_id"), table_name="pipeline_snapshots")
    op.drop_index(op.f("ix_pipeline_snapshots_organisation_id"), table_name="pipeline_snapshots")
    op.drop_index(op.f("ix_pipeline_snapshots_environment_profile_id"), table_name="pipeline_snapshots")
    op.drop_table("pipeline_snapshots")
    op.drop_index(op.f("ix_environment_profiles_organisation_id"), table_name="environment_profiles")
    op.drop_table("environment_profiles")
    op.drop_index(op.f("ix_pipeline_edges_pipeline_id"), table_name="pipeline_edges")
    op.drop_index(op.f("ix_pipeline_edges_organisation_id"), table_name="pipeline_edges")
    op.drop_table("pipeline_edges")
    op.drop_index(op.f("ix_pipelines_organisation_id"), table_name="pipelines")
    op.drop_table("pipelines")
    op.drop_index(op.f("ix_stages_organisation_id"), table_name="stages")
    op.drop_table("stages")
    op.drop_index(op.f("ix_notification_endpoints_organisation_id"), table_name="notification_endpoints")
    op.drop_table("notification_endpoints")
    op.drop_index(op.f("ix_model_backends_organisation_id"), table_name="model_backends")
    op.drop_table("model_backends")
    op.drop_index(op.f("ix_connector_instances_organisation_id"), table_name="connector_instances")
    op.drop_index(op.f("ix_connector_instances_connector_type_id"), table_name="connector_instances")
    op.drop_table("connector_instances")
