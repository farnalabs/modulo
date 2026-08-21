"""v2 — Features & System.

Creates composite templates, environment profiles, workspace leases,
chat (Remy), error tracking, audit, monitoring, publishing, and system
config tables. Introduces append-only triggers (audit_events, error_events)
and NULL-org RLS carve-outs for Remy dual-ownership tables.

Revision ID: 0005_v2_features_system
Revises: 0003_v2_pipeline_runtime
Create Date: 2026-07-11

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0005_v2_features_system"
down_revision: str | None = "0003_v2_pipeline_runtime"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


_STRICT_RLS: tuple[str, ...] = (
    "audit_chain_heads",
    "audit_events",
    "chat_messages",
    "chat_sessions",
    "composite_templates",
    "error_events",
    "error_forwarder_configs",
    "error_groups",
    "error_notification_rules",
    "lifecycle_maps",
    "mcp_setup_tokens",
    "org_api_keys",
    "org_daily_run_counts",
    "publishers",
    "saved_views",
    "scheduled_reports",
    "secrets",
    "spend_anomalies",
    "variant_groups",
    "workspace_leases",
)

_NULLABLE_ORG_RLS: tuple[str, ...] = (
    "remy_skills",
    "remy_context_sources",
)

_TENANT_REFS: tuple[tuple[str, str, str], ...] = (
    ("audit_events", "account_id", "accounts"),
    ("audit_chain_heads", "last_event_id", "audit_events"),
    ("chat_sessions", "user_id", "accounts"),
    ("chat_messages", "session_id", "chat_sessions"),
    ("chat_messages", "parent_id", "chat_messages"),
    ("remy_skills", "user_id", "accounts"),
    ("remy_context_sources", "user_id", "accounts"),
    ("composite_templates", "account_id", "accounts"),
    ("workspace_leases", "environment_profile_id", "environment_profiles"),
    ("workspace_leases", "run_id", "runs"),
    ("lifecycle_maps", "account_id", "accounts"),
    ("lifecycle_maps", "owner_team_id", "teams"),
    ("mcp_setup_tokens", "created_by", "accounts"),
    ("variant_groups", "pipeline_id", "pipelines"),
    ("org_api_keys", "account_id", "accounts"),
    ("org_api_keys", "team_id", "teams"),
    ("org_daily_run_counts", "team_id", "teams"),
    ("scheduled_reports", "created_by", "accounts"),
    ("saved_views", "account_id", "accounts"),
    ("spend_anomalies", "pipeline_id", "pipelines"),
    ("error_groups", "sample_event_id", "error_events"),
    ("error_groups", "assigned_to", "accounts"),
)


def upgrade() -> None:
    _create_tables()
    _create_trigger_functions()
    _create_triggers()
    _enable_rls()


def _create_tables() -> None:
    op.create_table(
        "composite_templates",
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("sub_pipeline_graph_json", sa.JSON(), nullable=False),
        sa.Column("parameter_ports_json", sa.JSON(), nullable=False),
        sa.Column("input_schema_id", sa.Uuid(), nullable=True),
        sa.Column("output_schema_id", sa.Uuid(), nullable=True),
        sa.Column("version", sa.String(length=50), nullable=False),
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
    )
    op.create_index(
        op.f("ix_composite_templates_organisation_id"), "composite_templates", ["organisation_id"], unique=False
    )
    op.create_table(
        "workspace_leases",
        sa.Column("environment_profile_id", sa.Uuid(), nullable=False),
        sa.Column("run_id", sa.Uuid(), nullable=False),
        sa.Column("provider_ref", sa.String(length=255), nullable=False),
        sa.Column("status", sa.String(length=30), server_default="pending", nullable=False),
        sa.Column("repository_url", sa.String(length=1000), nullable=True),
        sa.Column("repository_ref", sa.String(length=255), nullable=True),
        sa.Column("lease_started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("lease_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("resource_usage_json", sa.JSON(), nullable=True),
        sa.Column("output_artifact_refs_json", sa.JSON(), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("organisation_id", sa.Uuid(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False
        ),
        sa.CheckConstraint(
            "status IN ('pending', 'running', 'completed', 'failed', 'expired')", name="ck_workspace_leases_status"
        ),
        sa.ForeignKeyConstraint(["environment_profile_id"], ["environment_profiles.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["organisation_id"], ["organisations.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["run_id"], ["runs.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        op.f("ix_workspace_leases_environment_profile_id"), "workspace_leases", ["environment_profile_id"], unique=False
    )
    op.create_index(op.f("ix_workspace_leases_organisation_id"), "workspace_leases", ["organisation_id"], unique=False)
    op.create_index(op.f("ix_workspace_leases_run_id"), "workspace_leases", ["run_id"], unique=False)
    op.create_table(
        "lifecycle_maps",
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("description", sa.String(length=2000), nullable=True),
        sa.Column("owner_team_id", sa.Uuid(), nullable=True),
        sa.Column("visibility", sa.String(length=10), server_default="org", nullable=False),
        sa.Column("version", sa.Integer(), server_default="1", nullable=False),
        sa.Column("content_json", sa.JSON(), nullable=False),
        sa.Column("archived_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("account_id", sa.Uuid(), nullable=False),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("organisation_id", sa.Uuid(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False
        ),
        sa.CheckConstraint("visibility IN ('org', 'team')", name="ck_lifecycle_maps_visibility"),
        sa.CheckConstraint("visibility = 'org' OR owner_team_id IS NOT NULL", name="ck_lifecycle_maps_team_owner"),
        sa.CheckConstraint("version > 0", name="ck_lifecycle_maps_version"),
        sa.ForeignKeyConstraint(["account_id"], ["accounts.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["organisation_id"], ["organisations.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["owner_team_id"], ["teams.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_lifecycle_maps_organisation_id"), "lifecycle_maps", ["organisation_id"], unique=False)
    op.create_table(
        "chat_sessions",
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("name", sa.String(length=255), nullable=True),
        sa.Column("provider", sa.String(length=50), nullable=False),
        sa.Column("model", sa.String(length=100), nullable=False),
        sa.Column("context_window_tokens", sa.Integer(), nullable=False),
        sa.Column("system_prompt_hash", sa.String(length=64), nullable=True),
        sa.Column("session_number", sa.Integer(), nullable=False),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("organisation_id", sa.Uuid(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False
        ),
        sa.ForeignKeyConstraint(["organisation_id"], ["organisations.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["user_id"], ["accounts.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("user_id", "session_number", name="uq_chat_sessions_user_session_number"),
    )
    op.create_index(op.f("ix_chat_sessions_organisation_id"), "chat_sessions", ["organisation_id"], unique=False)
    op.create_index(op.f("ix_chat_sessions_user_id"), "chat_sessions", ["user_id"], unique=False)
    op.create_table(
        "chat_messages",
        sa.Column("session_id", sa.Uuid(), nullable=False),
        sa.Column("role", sa.String(length=20), nullable=False),
        sa.Column("content", sa.Text(), nullable=True),
        sa.Column("tool_calls_json", sa.JSON(), nullable=True),
        sa.Column("tool_results_json", sa.JSON(), nullable=True),
        sa.Column("token_count", sa.Integer(), nullable=True),
        sa.Column("parent_id", sa.Uuid(), nullable=True),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("organisation_id", sa.Uuid(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False
        ),
        sa.CheckConstraint(
            "role IN ('user', 'assistant', 'tool_use', 'tool_result', 'summary')", name="ck_chat_messages_role"
        ),
        sa.ForeignKeyConstraint(["organisation_id"], ["organisations.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["parent_id"], ["chat_messages.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["session_id"], ["chat_sessions.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_chat_messages_organisation_id"), "chat_messages", ["organisation_id"], unique=False)
    op.create_index(op.f("ix_chat_messages_session_id"), "chat_messages", ["session_id"], unique=False)
    op.create_table(
        "remy_skills",
        sa.Column("organisation_id", sa.Uuid(), nullable=True),
        sa.Column("user_id", sa.Uuid(), nullable=True),
        sa.Column("name", sa.String(length=100), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("triggers", sa.JSON(), nullable=True),
        sa.Column("body", sa.Text(), nullable=False),
        sa.Column("active", sa.Boolean(), server_default="true", nullable=False),
        sa.Column("source_mode", sa.String(length=16), nullable=True),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False
        ),
        sa.CheckConstraint(
            "(organisation_id IS NOT NULL AND user_id IS NULL) OR (organisation_id IS NULL AND user_id IS NOT NULL)",
            name="ck_remy_skills_owner",
        ),
        sa.ForeignKeyConstraint(["organisation_id"], ["organisations.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["user_id"], ["accounts.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_remy_skills_organisation_id"), "remy_skills", ["organisation_id"], unique=False)
    op.create_index(op.f("ix_remy_skills_user_id"), "remy_skills", ["user_id"], unique=False)
    op.create_table(
        "remy_context_sources",
        sa.Column("organisation_id", sa.Uuid(), nullable=True),
        sa.Column("user_id", sa.Uuid(), nullable=True),
        sa.Column("source_key", sa.String(length=64), nullable=False),
        sa.Column("source_mode", sa.String(length=16), server_default="always_on", nullable=False),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False
        ),
        sa.CheckConstraint("source_mode IN ('always_on', 'tool', 'off')", name="ck_remy_context_sources_mode"),
        sa.CheckConstraint(
            "(organisation_id IS NOT NULL AND user_id IS NULL) OR (organisation_id IS NULL AND user_id IS NOT NULL)",
            name="ck_remy_context_sources_owner",
        ),
        sa.ForeignKeyConstraint(["organisation_id"], ["organisations.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["user_id"], ["accounts.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("organisation_id", "user_id", "source_key", name="uq_remy_context_sources_key"),
    )
    op.create_table(
        "error_events",
        sa.Column("fingerprint", sa.String(length=64), nullable=False),
        sa.Column("level", sa.String(length=20), nullable=False),
        sa.Column("message", sa.Text(), nullable=False),
        sa.Column("stacktrace", sa.Text(), nullable=True),
        sa.Column("context_json", sa.JSON(), nullable=True),
        sa.Column("source", sa.String(length=20), nullable=False),
        sa.Column("environment", sa.String(length=50), nullable=True),
        sa.Column("version", sa.String(length=50), nullable=True),
        sa.Column("status", sa.String(length=20), server_default="new", nullable=False),
        sa.Column("resolved_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("organisation_id", sa.Uuid(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False
        ),
        sa.CheckConstraint("level IN ('error', 'warning', 'critical')", name="ck_error_events_level"),
        sa.CheckConstraint("source IN ('backend', 'frontend', 'celery')", name="ck_error_events_source"),
        sa.CheckConstraint("status IN ('new', 'acknowledged', 'resolved', 'archived')", name="ck_error_events_status"),
        sa.ForeignKeyConstraint(["organisation_id"], ["organisations.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_error_events_organisation_id"), "error_events", ["organisation_id"], unique=False)
    op.create_table(
        "error_groups",
        sa.Column("fingerprint", sa.String(length=64), nullable=False),
        sa.Column("status", sa.String(length=20), server_default="new", nullable=False),
        sa.Column(
            "first_seen", sa.DateTime(timezone=True), server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False
        ),
        sa.Column("last_seen", sa.DateTime(timezone=True), server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False),
        sa.Column("count", sa.Integer(), server_default="1", nullable=False),
        sa.Column("level_peak", sa.String(length=20), server_default="error", nullable=False),
        sa.Column("sample_event_id", sa.Uuid(), nullable=True),
        sa.Column("assigned_to", sa.Uuid(), nullable=True),
        sa.Column("resolved_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("organisation_id", sa.Uuid(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False
        ),
        sa.CheckConstraint("status IN ('new', 'acknowledged', 'resolved', 'archived')", name="ck_error_groups_status"),
        sa.CheckConstraint("level_peak IN ('error', 'warning', 'critical')", name="ck_error_groups_level_peak"),
        sa.ForeignKeyConstraint(["assigned_to"], ["accounts.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["organisation_id"], ["organisations.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["sample_event_id"], ["error_events.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("organisation_id", "fingerprint", name="uq_error_groups_org_fingerprint"),
    )
    op.create_index(op.f("ix_error_groups_organisation_id"), "error_groups", ["organisation_id"], unique=False)
    op.create_table(
        "error_forwarder_configs",
        sa.Column("forwarder_type", sa.String(length=50), nullable=False),
        sa.Column("enabled", sa.Boolean(), server_default="false", nullable=False),
        sa.Column("config_json", sa.JSON(), nullable=True),
        sa.Column("last_test_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_test_ok", sa.Boolean(), nullable=True),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("organisation_id", sa.Uuid(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False
        ),
        sa.ForeignKeyConstraint(["organisation_id"], ["organisations.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("organisation_id", "forwarder_type", name="uq_org_forwarder_type"),
    )
    op.create_index(
        op.f("ix_error_forwarder_configs_organisation_id"), "error_forwarder_configs", ["organisation_id"], unique=False
    )
    op.create_table(
        "error_notification_rules",
        sa.Column("name", sa.String(length=100), nullable=False),
        sa.Column("enabled", sa.Boolean(), server_default="true", nullable=False),
        sa.Column("condition_level", sa.String(length=20), server_default="error", nullable=False),
        sa.Column("condition_min_count", sa.Integer(), server_default="1", nullable=False),
        sa.Column("condition_window_seconds", sa.Integer(), server_default="300", nullable=False),
        sa.Column("action_type", sa.String(length=20), server_default="in_app", nullable=False),
        sa.Column("webhook_url", sa.Text(), nullable=True),
        sa.Column("cooldown_seconds", sa.Integer(), server_default="300", nullable=False),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("organisation_id", sa.Uuid(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False
        ),
        sa.CheckConstraint("condition_level IN ('error', 'warning', 'critical')", name="ck_enr_condition_level"),
        sa.CheckConstraint("action_type IN ('in_app', 'email', 'webhook')", name="ck_enr_action_type"),
        sa.ForeignKeyConstraint(["organisation_id"], ["organisations.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        op.f("ix_error_notification_rules_organisation_id"),
        "error_notification_rules",
        ["organisation_id"],
        unique=False,
    )
    op.create_table(
        "audit_events",
        sa.Column("event_type", sa.String(length=100), nullable=False),
        sa.Column("account_id", sa.Uuid(), nullable=True),
        sa.Column("resource_type", sa.String(length=100), nullable=True),
        sa.Column("resource_id", sa.Uuid(), nullable=True),
        sa.Column("payload_json", sa.JSON(), nullable=False),
        sa.Column("request_id", sa.String(length=255), nullable=True),
        sa.Column("previous_hash", sa.Text(), nullable=True),
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
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_audit_events_event_type"), "audit_events", ["event_type"], unique=False)
    op.create_index(op.f("ix_audit_events_organisation_id"), "audit_events", ["organisation_id"], unique=False)
    op.create_table(
        "audit_chain_heads",
        sa.Column("organisation_id", sa.Uuid(), nullable=False),
        sa.Column("last_event_hash", sa.Text(), nullable=False),
        sa.Column("last_event_id", sa.Uuid(), nullable=True),
        sa.Column("event_count", sa.Integer(), nullable=False),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.ForeignKeyConstraint(["last_event_id"], ["audit_events.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["organisation_id"], ["organisations.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_audit_chain_heads_organisation_id"), "audit_chain_heads", ["organisation_id"], unique=True)
    op.create_table(
        "spend_anomalies",
        sa.Column("anomaly_date", sa.Date(), nullable=False),
        sa.Column("pipeline_id", sa.Uuid(), nullable=True),
        sa.Column("amount", sa.Numeric(precision=14, scale=6), nullable=False),
        sa.Column("baseline", sa.Numeric(precision=14, scale=6), nullable=False),
        sa.Column("percent_above", sa.Numeric(precision=8, scale=2), nullable=False),
        sa.Column("dismissed", sa.Boolean(), server_default="false", nullable=False),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("organisation_id", sa.Uuid(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False
        ),
        sa.ForeignKeyConstraint(["organisation_id"], ["organisations.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["pipeline_id"], ["pipelines.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_spend_anomalies_anomaly_date"), "spend_anomalies", ["anomaly_date"], unique=False)
    op.create_index(op.f("ix_spend_anomalies_organisation_id"), "spend_anomalies", ["organisation_id"], unique=False)
    op.create_table(
        "scheduled_reports",
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("report_type", sa.String(length=50), nullable=False),
        sa.Column("cron_expression", sa.String(length=100), nullable=False),
        sa.Column("config_json", sa.JSON(), nullable=True),
        sa.Column("recipient_config", sa.JSON(), nullable=True),
        sa.Column("last_sent_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("next_send_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("active", sa.Boolean(), nullable=False),
        sa.Column("created_by", sa.Uuid(), nullable=True),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("organisation_id", sa.Uuid(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False
        ),
        sa.ForeignKeyConstraint(["created_by"], ["accounts.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["organisation_id"], ["organisations.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        op.f("ix_scheduled_reports_organisation_id"), "scheduled_reports", ["organisation_id"], unique=False
    )
    op.create_index(op.f("ix_scheduled_reports_report_type"), "scheduled_reports", ["report_type"], unique=False)
    op.create_table(
        "saved_views",
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("view_type", sa.String(length=50), nullable=False),
        sa.Column("filters", sa.JSON(), nullable=False),
        sa.Column("columns", sa.JSON(), nullable=True),
        sa.Column("sort_by", sa.String(length=100), nullable=True),
        sa.Column("sort_order", sa.String(length=10), nullable=False),
        sa.Column("account_id", sa.Uuid(), nullable=False),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("organisation_id", sa.Uuid(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False
        ),
        sa.CheckConstraint("view_type IN ('run_list', 'pipeline_list', 'audit_log')", name="ck_saved_views_type"),
        sa.ForeignKeyConstraint(["account_id"], ["accounts.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["organisation_id"], ["organisations.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_saved_views_organisation_id"), "saved_views", ["organisation_id"], unique=False)
    op.create_table(
        "secrets",
        sa.Column("key", sa.String(length=255), nullable=False),
        sa.Column("encrypted_value", sa.LargeBinary(), nullable=False),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("organisation_id", sa.Uuid(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False
        ),
        sa.ForeignKeyConstraint(["organisation_id"], ["organisations.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("organisation_id", "key", name="uq_secrets_org_key"),
    )
    op.create_index(op.f("ix_secrets_key"), "secrets", ["key"], unique=False)
    op.create_index(op.f("ix_secrets_organisation_id"), "secrets", ["organisation_id"], unique=False)
    op.create_table(
        "system_config",
        sa.Column("key", sa.String(length=255), nullable=False),
        sa.Column("value", sa.JSON(), nullable=False),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False
        ),
        sa.Column("updated_by", sa.Uuid(), nullable=True),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("key"),
    )
    op.create_table(
        "tier_catalog",
        sa.Column("tier_id", sa.String(length=255), nullable=False),
        sa.Column("label", sa.String(length=255), nullable=False),
        sa.Column("rank", sa.Integer(), nullable=False),
        sa.Column("requires_license", sa.Boolean(), server_default="false", nullable=False),
        sa.Column("description", sa.String(length=2000), nullable=True),
        sa.PrimaryKeyConstraint("tier_id"),
    )
    op.create_table(
        "feature_flag_catalog",
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("description", sa.String(length=2000), nullable=True),
        sa.Column("tier_id", sa.String(length=255), nullable=False),
        sa.Column("depends_on", sa.JSON(), nullable=True),
        sa.Column("is_active", sa.Boolean(), server_default="true", nullable=False),
        sa.ForeignKeyConstraint(["tier_id"], ["tier_catalog.tier_id"]),
        sa.PrimaryKeyConstraint("name"),
    )
    op.create_table(
        "mcp_setup_tokens",
        sa.Column("resource_type", sa.String(length=50), nullable=False),
        sa.Column("resource_id", sa.Uuid(), nullable=False),
        sa.Column("token_hash", sa.String(length=64), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_by", sa.Uuid(), nullable=False),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("organisation_id", sa.Uuid(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False
        ),
        sa.ForeignKeyConstraint(["organisation_id"], ["organisations.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("token_hash"),
    )
    op.create_index(op.f("ix_mcp_setup_tokens_organisation_id"), "mcp_setup_tokens", ["organisation_id"], unique=False)
    op.create_index(op.f("ix_mcp_setup_tokens_resource_id"), "mcp_setup_tokens", ["resource_id"], unique=False)
    op.create_table(
        "variant_groups",
        sa.Column("pipeline_id", sa.Uuid(), nullable=False),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("description", sa.String(length=2000), nullable=True),
        sa.Column("variants", sa.JSON(), nullable=False),
        sa.Column("selection_strategy", sa.String(length=20), server_default="weighted", nullable=False),
        sa.Column("run_count", sa.Integer(), server_default="0", nullable=False),
        sa.Column("max_concurrent_runs", sa.Integer(), server_default="5", nullable=False),
        sa.Column("degraded_evals", sa.Boolean(), server_default="false", nullable=False),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("organisation_id", sa.Uuid(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False
        ),
        sa.CheckConstraint("selection_strategy IN ('weighted', 'single')", name="ck_variant_groups_selection_strategy"),
        sa.ForeignKeyConstraint(["organisation_id"], ["organisations.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["pipeline_id"], ["pipelines.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_variant_groups_organisation_id"), "variant_groups", ["organisation_id"], unique=False)
    op.create_index(op.f("ix_variant_groups_pipeline_id"), "variant_groups", ["pipeline_id"], unique=False)
    op.create_table(
        "org_api_keys",
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("lookup_prefix", sa.String(length=8), nullable=False),
        sa.Column("hashed_secret", sa.String(length=64), nullable=False),
        sa.Column("role", sa.String(length=20), nullable=False),
        sa.Column("team_id", sa.Uuid(), nullable=True),
        sa.Column("account_id", sa.Uuid(), nullable=False),
        sa.Column("last_used_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("organisation_id", sa.Uuid(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False
        ),
        sa.CheckConstraint("role IN ('operator', 'runner')", name="ck_org_api_keys_role"),
        sa.ForeignKeyConstraint(["account_id"], ["accounts.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["organisation_id"], ["organisations.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["team_id"], ["teams.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("lookup_prefix", name="uq_org_api_keys_lookup_prefix"),
    )
    op.create_index(op.f("ix_org_api_keys_organisation_id"), "org_api_keys", ["organisation_id"], unique=False)
    op.create_table(
        "org_daily_run_counts",
        sa.Column("run_date", sa.Date(), nullable=False),
        sa.Column("team_id", sa.Uuid(), nullable=True),
        sa.Column("run_count", sa.Integer(), server_default="0", nullable=False),
        sa.Column("total_spend_usd", sa.Numeric(precision=14, scale=6), server_default="0", nullable=False),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("organisation_id", sa.Uuid(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False
        ),
        sa.ForeignKeyConstraint(["organisation_id"], ["organisations.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["team_id"], ["teams.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("organisation_id", "team_id", "run_date", name="uq_org_daily_run_counts_org_team_date"),
    )
    op.create_index(
        op.f("ix_org_daily_run_counts_organisation_id"), "org_daily_run_counts", ["organisation_id"], unique=False
    )
    op.create_table(
        "publishers",
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("contact_email", sa.String(length=255), nullable=True),
        sa.Column("public_key_hex", sa.String(length=128), nullable=False),
        sa.Column("trust_tier", sa.String(length=10), server_default="amber", nullable=False),
        sa.Column("verified_since", sa.DateTime(timezone=True), nullable=True),
        sa.Column("website_url", sa.String(length=2000), nullable=True),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("organisation_id", sa.Uuid(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False
        ),
        sa.ForeignKeyConstraint(["organisation_id"], ["organisations.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("organisation_id", "name", name="uq_publishers_org_name"),
        sa.UniqueConstraint("organisation_id", "public_key_hex", name="uq_publishers_org_key"),
    )
    op.create_index(op.f("ix_publishers_organisation_id"), "publishers", ["organisation_id"], unique=False)
    op.create_index(op.f("ix_publishers_public_key_hex"), "publishers", ["public_key_hex"], unique=False)


def _create_trigger_functions() -> None:
    op.execute(
        sa.text("""
        CREATE FUNCTION audit_events_append_only()
        RETURNS TRIGGER AS $$
        BEGIN
            IF TG_OP = 'DELETE' THEN
                RAISE EXCEPTION 'audit_events are append-only: DELETE is not permitted';
            ELSIF TG_OP = 'UPDATE' THEN
                RAISE EXCEPTION 'audit_events are append-only: UPDATE is not permitted';
            END IF;
            RETURN NULL;
        END;
        $$ LANGUAGE plpgsql;
    """)
    )
    op.execute(
        sa.text("""
        CREATE FUNCTION error_events_append_only()
        RETURNS TRIGGER AS $$
        BEGIN
            IF TG_OP = 'DELETE' THEN
                RAISE EXCEPTION 'error_events are append-only: DELETE is not permitted';
            ELSIF TG_OP = 'UPDATE' THEN
                RAISE EXCEPTION 'error_events are append-only: UPDATE is not permitted';
            END IF;
            RETURN NULL;
        END;
        $$ LANGUAGE plpgsql;
    """)
    )


def _create_triggers() -> None:
    for child_table, child_column, parent_table in _TENANT_REFS:
        op.execute(
            sa.text(
                f'CREATE TRIGGER "trg_{child_table}_{child_column}_tenant" '
                f'BEFORE INSERT OR UPDATE OF "{child_column}", "organisation_id" ON "{child_table}" '
                f"FOR EACH ROW EXECUTE FUNCTION enforce_same_organisation('{parent_table}', '{child_column}')"
            )
        )
    op.execute(
        sa.text(
            "CREATE TRIGGER audit_events_no_update "
            "BEFORE UPDATE ON audit_events FOR EACH ROW "
            "EXECUTE FUNCTION audit_events_append_only()"
        )
    )
    op.execute(
        sa.text(
            "CREATE TRIGGER audit_events_no_delete "
            "BEFORE DELETE ON audit_events FOR EACH ROW "
            "EXECUTE FUNCTION audit_events_append_only()"
        )
    )
    op.execute(
        sa.text(
            "CREATE TRIGGER error_events_no_update "
            "BEFORE UPDATE ON error_events FOR EACH ROW "
            "EXECUTE FUNCTION error_events_append_only()"
        )
    )
    op.execute(
        sa.text(
            "CREATE TRIGGER error_events_no_delete "
            "BEFORE DELETE ON error_events FOR EACH ROW "
            "EXECUTE FUNCTION error_events_append_only()"
        )
    )


def _enable_rls() -> None:
    strict = "organisation_id = nullif(current_setting('app.organisation_id', true), '')::uuid"
    nullable_org = f"{strict} OR organisation_id IS NULL"
    for table in _STRICT_RLS:
        op.execute(sa.text(f'ALTER TABLE "{table}" ENABLE ROW LEVEL SECURITY'))
        op.execute(sa.text(f'CREATE POLICY rls_org_isolation ON "{table}" USING ({strict})'))
    for table in _NULLABLE_ORG_RLS:
        op.execute(sa.text(f'ALTER TABLE "{table}" ENABLE ROW LEVEL SECURITY'))
        op.execute(sa.text(f'CREATE POLICY rls_org_isolation ON "{table}" USING ({nullable_org})'))


def downgrade() -> None:
    for table in _NULLABLE_ORG_RLS:
        op.execute(sa.text(f'DROP POLICY IF EXISTS rls_org_isolation ON "{table}"'))
        op.execute(sa.text(f'ALTER TABLE "{table}" DISABLE ROW LEVEL SECURITY'))
    for table in _STRICT_RLS:
        op.execute(sa.text(f'DROP POLICY IF EXISTS rls_org_isolation ON "{table}"'))
        op.execute(sa.text(f'ALTER TABLE "{table}" DISABLE ROW LEVEL SECURITY'))

    op.execute(sa.text("DROP TRIGGER IF EXISTS audit_events_no_update ON audit_events"))
    op.execute(sa.text("DROP TRIGGER IF EXISTS audit_events_no_delete ON audit_events"))
    op.execute(sa.text("DROP FUNCTION IF EXISTS audit_events_append_only()"))
    op.execute(sa.text("DROP TRIGGER IF EXISTS error_events_no_update ON error_events"))
    op.execute(sa.text("DROP TRIGGER IF EXISTS error_events_no_delete ON error_events"))
    op.execute(sa.text("DROP FUNCTION IF EXISTS error_events_append_only()"))
    for child_table, child_column, _ in _TENANT_REFS:
        op.execute(sa.text(f'DROP TRIGGER IF EXISTS "trg_{child_table}_{child_column}_tenant" ON "{child_table}"'))

    op.drop_index(op.f("ix_publishers_public_key_hex"), table_name="publishers")
    op.drop_index(op.f("ix_publishers_organisation_id"), table_name="publishers")
    op.drop_table("publishers")
    op.drop_index(op.f("ix_org_daily_run_counts_organisation_id"), table_name="org_daily_run_counts")
    op.drop_table("org_daily_run_counts")
    op.drop_index(op.f("ix_org_api_keys_organisation_id"), table_name="org_api_keys")
    op.drop_table("org_api_keys")
    op.drop_index(op.f("ix_variant_groups_pipeline_id"), table_name="variant_groups")
    op.drop_index(op.f("ix_variant_groups_organisation_id"), table_name="variant_groups")
    op.drop_table("variant_groups")
    op.drop_index(op.f("ix_mcp_setup_tokens_resource_id"), table_name="mcp_setup_tokens")
    op.drop_index(op.f("ix_mcp_setup_tokens_organisation_id"), table_name="mcp_setup_tokens")
    op.drop_table("mcp_setup_tokens")
    op.drop_table("feature_flag_catalog")
    op.drop_table("tier_catalog")
    op.drop_table("system_config")
    op.drop_index(op.f("ix_secrets_organisation_id"), table_name="secrets")
    op.drop_index(op.f("ix_secrets_key"), table_name="secrets")
    op.drop_table("secrets")
    op.drop_index(op.f("ix_saved_views_organisation_id"), table_name="saved_views")
    op.drop_table("saved_views")
    op.drop_index(op.f("ix_scheduled_reports_report_type"), table_name="scheduled_reports")
    op.drop_index(op.f("ix_scheduled_reports_organisation_id"), table_name="scheduled_reports")
    op.drop_table("scheduled_reports")
    op.drop_index(op.f("ix_spend_anomalies_organisation_id"), table_name="spend_anomalies")
    op.drop_index(op.f("ix_spend_anomalies_anomaly_date"), table_name="spend_anomalies")
    op.drop_table("spend_anomalies")
    op.drop_index(op.f("ix_audit_chain_heads_organisation_id"), table_name="audit_chain_heads")
    op.drop_table("audit_chain_heads")
    op.drop_index(op.f("ix_audit_events_organisation_id"), table_name="audit_events")
    op.drop_index(op.f("ix_audit_events_event_type"), table_name="audit_events")
    op.drop_table("audit_events")
    op.drop_index(op.f("ix_error_notification_rules_organisation_id"), table_name="error_notification_rules")
    op.drop_table("error_notification_rules")
    op.drop_index(op.f("ix_error_forwarder_configs_organisation_id"), table_name="error_forwarder_configs")
    op.drop_table("error_forwarder_configs")
    op.drop_index(op.f("ix_error_groups_organisation_id"), table_name="error_groups")
    op.drop_table("error_groups")
    op.drop_index(op.f("ix_error_events_organisation_id"), table_name="error_events")
    op.drop_table("error_events")
    op.drop_table("remy_context_sources")
    op.drop_index(op.f("ix_remy_skills_user_id"), table_name="remy_skills")
    op.drop_index(op.f("ix_remy_skills_organisation_id"), table_name="remy_skills")
    op.drop_table("remy_skills")
    op.drop_index(op.f("ix_chat_messages_session_id"), table_name="chat_messages")
    op.drop_index(op.f("ix_chat_messages_organisation_id"), table_name="chat_messages")
    op.drop_table("chat_messages")
    op.drop_index(op.f("ix_chat_sessions_user_id"), table_name="chat_sessions")
    op.drop_index(op.f("ix_chat_sessions_organisation_id"), table_name="chat_sessions")
    op.drop_table("chat_sessions")
    op.drop_index(op.f("ix_lifecycle_maps_organisation_id"), table_name="lifecycle_maps")
    op.drop_table("lifecycle_maps")
    op.drop_index(op.f("ix_workspace_leases_run_id"), table_name="workspace_leases")
    op.drop_index(op.f("ix_workspace_leases_organisation_id"), table_name="workspace_leases")
    op.drop_index(op.f("ix_workspace_leases_environment_profile_id"), table_name="workspace_leases")
    op.drop_table("workspace_leases")
    op.drop_index(op.f("ix_composite_templates_organisation_id"), table_name="composite_templates")
    op.drop_table("composite_templates")
