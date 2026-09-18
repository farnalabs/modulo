"""Add missing foreign-key lookup indexes (improve-database FK-index pass).

Revision ID: 0142_merge_heads_add_fk_indexes
Revises: 0141_pipeline_edge_ports
Create Date: 2026-08-25

This migration adds the missing B-tree indexes on foreign-key columns that
are used as lookup / join / owner-scoping keys but were never indexed.
Postgres does not create an index for a foreign key automatically, so queries
that filter or join on these columns performed full table scans. It is a
linear child of the single current head ``0141_pipeline_edge_ports`` (the
FAR-416 pipeline edge ports migration that landed on main after this branch
was cut).

Every index is created with IF NOT EXISTS and dropped with IF EXISTS so the
migration is safe to re-run. The corresponding ORM models now declare
``index=True`` on each column so a future ``alembic revision --autogenerate``
sees them as in-sync and will not propose dropping them.
"""

from __future__ import annotations

from alembic import op
from sqlalchemy import text

revision: str = "0142_merge_heads_add_fk_indexes"
down_revision: str | None = "0141_pipeline_edge_ports"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.execute(text("CREATE INDEX IF NOT EXISTS ix_agents_model_backend_id ON agents (model_backend_id)"))
    op.execute(text("CREATE INDEX IF NOT EXISTS ix_agents_library_id ON agents (library_id)"))
    op.execute(text("CREATE INDEX IF NOT EXISTS ix_agents_parameter_schema_id ON agents (parameter_schema_id)"))
    op.execute(text("CREATE INDEX IF NOT EXISTS ix_agents_account_id ON agents (account_id)"))
    op.execute(text("CREATE INDEX IF NOT EXISTS ix_org_api_keys_team_id ON org_api_keys (team_id)"))
    op.execute(text("CREATE INDEX IF NOT EXISTS ix_org_api_keys_account_id ON org_api_keys (account_id)"))
    op.execute(text("CREATE INDEX IF NOT EXISTS ix_audit_events_account_id ON audit_events (account_id)"))
    op.execute(
        text("CREATE INDEX IF NOT EXISTS ix_audit_chain_heads_last_event_id ON audit_chain_heads (last_event_id)")
    )
    op.execute(
        text(
            "CREATE INDEX IF NOT EXISTS ix_composite_templates_input_schema_id ON composite_templates (input_schema_id)"
        )
    )
    op.execute(
        text(
            "CREATE INDEX IF NOT EXISTS ix_composite_templates_output_schema_id ON composite_templates (output_schema_id)"
        )
    )
    op.execute(
        text(
            "CREATE INDEX IF NOT EXISTS ix_composite_templates_parameter_schema_id ON composite_templates (parameter_schema_id)"
        )
    )
    op.execute(text("CREATE INDEX IF NOT EXISTS ix_composite_templates_account_id ON composite_templates (account_id)"))
    op.execute(text("CREATE INDEX IF NOT EXISTS ix_connector_instances_account_id ON connector_instances (account_id)"))
    op.execute(
        text("CREATE INDEX IF NOT EXISTS ix_connector_instances_owner_team_id ON connector_instances (owner_team_id)")
    )
    op.execute(text("CREATE INDEX IF NOT EXISTS ix_org_daily_run_counts_team_id ON org_daily_run_counts (team_id)"))
    op.execute(
        text("CREATE INDEX IF NOT EXISTS ix_environment_profiles_account_id ON environment_profiles (account_id)")
    )
    op.execute(
        text("CREATE INDEX IF NOT EXISTS ix_environment_profiles_owner_team_id ON environment_profiles (owner_team_id)")
    )
    op.execute(text("CREATE INDEX IF NOT EXISTS ix_error_groups_sample_event_id ON error_groups (sample_event_id)"))
    op.execute(text("CREATE INDEX IF NOT EXISTS ix_error_groups_assigned_to ON error_groups (assigned_to)"))
    op.execute(text("CREATE INDEX IF NOT EXISTS ix_eval_definitions_account_id ON eval_definitions (account_id)"))
    op.execute(text("CREATE INDEX IF NOT EXISTS ix_eval_results_eval_id ON eval_results (eval_id)"))
    op.execute(text("CREATE INDEX IF NOT EXISTS ix_eval_suites_owner_team_id ON eval_suites (owner_team_id)"))
    op.execute(text("CREATE INDEX IF NOT EXISTS ix_feedback_records_account_id ON feedback_records (account_id)"))
    op.execute(
        text(
            "CREATE INDEX IF NOT EXISTS ix_feedback_records_producing_agent_id ON feedback_records (producing_agent_id)"
        )
    )
    op.execute(
        text("CREATE INDEX IF NOT EXISTS ix_feedback_records_correction_run_id ON feedback_records (correction_run_id)")
    )
    op.execute(text("CREATE INDEX IF NOT EXISTS ix_hitl_claims_required_team_id ON hitl_claims (required_team_id)"))
    op.execute(text("CREATE INDEX IF NOT EXISTS ix_hitl_claims_account_id ON hitl_claims (account_id)"))
    op.execute(text("CREATE INDEX IF NOT EXISTS ix_journeys_owner_team_id ON journeys (owner_team_id)"))
    op.execute(text("CREATE INDEX IF NOT EXISTS ix_library_primitives_forked_from ON library_primitives (forked_from)"))
    op.execute(
        text("CREATE INDEX IF NOT EXISTS ix_library_primitives_owner_team_id ON library_primitives (owner_team_id)")
    )
    op.execute(text("CREATE INDEX IF NOT EXISTS ix_library_primitives_account_id ON library_primitives (account_id)"))
    op.execute(
        text(
            "CREATE INDEX IF NOT EXISTS ix_library_primitives_update_available_version_id ON library_primitives (update_available_version_id)"
        )
    )
    op.execute(text("CREATE INDEX IF NOT EXISTS ix_lifecycle_maps_owner_team_id ON lifecycle_maps (owner_team_id)"))
    op.execute(text("CREATE INDEX IF NOT EXISTS ix_lifecycle_maps_account_id ON lifecycle_maps (account_id)"))
    op.execute(
        text("CREATE INDEX IF NOT EXISTS ix_lifecycle_map_stages_pipeline_id ON lifecycle_map_stages (pipeline_id)")
    )
    op.execute(
        text("CREATE INDEX IF NOT EXISTS ix_lifecycle_map_stages_account_id ON lifecycle_map_stages (account_id)")
    )
    op.execute(text("CREATE INDEX IF NOT EXISTS ix_mcp_setup_tokens_created_by ON mcp_setup_tokens (created_by)"))
    op.execute(text("CREATE INDEX IF NOT EXISTS ix_model_backends_owner_team_id ON model_backends (owner_team_id)"))
    op.execute(text("CREATE INDEX IF NOT EXISTS ix_model_backends_account_id ON model_backends (account_id)"))
    op.execute(text("CREATE INDEX IF NOT EXISTS ix_nodes_account_id ON nodes (account_id)"))
    op.execute(text("CREATE INDEX IF NOT EXISTS ix_node_categories_account_id ON node_categories (account_id)"))
    op.execute(text("CREATE INDEX IF NOT EXISTS ix_node_observations_account_id ON node_observations (account_id)"))
    op.execute(text("CREATE INDEX IF NOT EXISTS ix_notifications_target_user_id ON notifications (target_user_id)"))
    op.execute(
        text("CREATE INDEX IF NOT EXISTS ix_notification_endpoints_account_id ON notification_endpoints (account_id)")
    )
    op.execute(text("CREATE INDEX IF NOT EXISTS ix_notification_endpoints_team_id ON notification_endpoints (team_id)"))
    op.execute(text("CREATE INDEX IF NOT EXISTS ix_oauth_clients_account_id ON oauth_clients (account_id)"))
    op.execute(
        text("CREATE INDEX IF NOT EXISTS ix_oauth_consent_states_account_id ON oauth_consent_states (account_id)")
    )
    op.execute(text("CREATE INDEX IF NOT EXISTS ix_parameter_schemas_account_id ON parameter_schemas (account_id)"))
    op.execute(text("CREATE INDEX IF NOT EXISTS ix_parameter_sets_account_id ON parameter_sets (account_id)"))
    op.execute(text("CREATE INDEX IF NOT EXISTS ix_pipelines_folder_id ON pipelines (folder_id)"))
    op.execute(text("CREATE INDEX IF NOT EXISTS ix_pipeline_folders_parent_id ON pipeline_folders (parent_id)"))
    op.execute(text("CREATE INDEX IF NOT EXISTS ix_pipeline_folders_account_id ON pipeline_folders (account_id)"))
    op.execute(text("CREATE INDEX IF NOT EXISTS ix_pipeline_snapshots_account_id ON pipeline_snapshots (account_id)"))
    op.execute(
        text("CREATE INDEX IF NOT EXISTS ix_primitive_abuse_reports_rating_id ON primitive_abuse_reports (rating_id)")
    )
    op.execute(
        text(
            "CREATE INDEX IF NOT EXISTS ix_primitive_abuse_reports_reporter_account_id ON primitive_abuse_reports (reporter_account_id)"
        )
    )
    op.execute(
        text(
            "CREATE INDEX IF NOT EXISTS ix_primitive_abuse_reports_reviewer_account_id ON primitive_abuse_reports (reviewer_account_id)"
        )
    )
    op.execute(text("CREATE INDEX IF NOT EXISTS ix_chat_messages_parent_id ON chat_messages (parent_id)"))
    op.execute(text("CREATE INDEX IF NOT EXISTS ix_runs_snapshot_id ON runs (snapshot_id)"))
    op.execute(text("CREATE INDEX IF NOT EXISTS ix_runs_trigger_id ON runs (trigger_id)"))
    op.execute(text("CREATE INDEX IF NOT EXISTS ix_run_daily_facts_team_id ON run_daily_facts (team_id)"))
    op.execute(text("CREATE INDEX IF NOT EXISTS ix_run_daily_facts_pipeline_id ON run_daily_facts (pipeline_id)"))
    op.execute(text("CREATE INDEX IF NOT EXISTS ix_run_daily_facts_folder_id ON run_daily_facts (folder_id)"))
    op.execute(text("CREATE INDEX IF NOT EXISTS ix_run_evidence_run_id ON run_evidence (run_id)"))
    op.execute(text("CREATE INDEX IF NOT EXISTS ix_scheduled_reports_created_by ON scheduled_reports (created_by)"))
    op.execute(text("CREATE INDEX IF NOT EXISTS ix_schema_folders_parent_id ON schema_folders (parent_id)"))
    op.execute(text("CREATE INDEX IF NOT EXISTS ix_schema_folders_account_id ON schema_folders (account_id)"))
    op.execute(text("CREATE INDEX IF NOT EXISTS ix_schemas_account_id ON schemas (account_id)"))
    op.execute(text("CREATE INDEX IF NOT EXISTS ix_schema_versions_account_id ON schema_versions (account_id)"))
    op.execute(
        text("CREATE INDEX IF NOT EXISTS ix_snapshot_schema_pins_snapshot_id ON snapshot_schema_pins (snapshot_id)")
    )
    op.execute(text("CREATE INDEX IF NOT EXISTS ix_snapshot_schema_pins_schema_id ON snapshot_schema_pins (schema_id)"))
    op.execute(text("CREATE INDEX IF NOT EXISTS ix_spend_anomalies_pipeline_id ON spend_anomalies (pipeline_id)"))
    op.execute(text("CREATE INDEX IF NOT EXISTS ix_system_config_updated_by ON system_config (updated_by)"))
    op.execute(text("CREATE INDEX IF NOT EXISTS ix_teams_account_id ON teams (account_id)"))
    op.execute(text("CREATE INDEX IF NOT EXISTS ix_feature_flag_catalog_tier_id ON feature_flag_catalog (tier_id)"))
    op.execute(text("CREATE INDEX IF NOT EXISTS ix_triggers_account_id ON triggers (account_id)"))
    op.execute(text("CREATE INDEX IF NOT EXISTS ix_saved_views_account_id ON saved_views (account_id)"))
    op.execute(
        text("CREATE INDEX IF NOT EXISTS ix_webhook_payloads_trigger_event_id ON webhook_payloads (trigger_event_id)")
    )


def downgrade() -> None:
    op.execute(text("DROP INDEX IF EXISTS ix_agents_model_backend_id"))
    op.execute(text("DROP INDEX IF EXISTS ix_agents_library_id"))
    op.execute(text("DROP INDEX IF EXISTS ix_agents_parameter_schema_id"))
    op.execute(text("DROP INDEX IF EXISTS ix_agents_account_id"))
    op.execute(text("DROP INDEX IF EXISTS ix_org_api_keys_team_id"))
    op.execute(text("DROP INDEX IF EXISTS ix_org_api_keys_account_id"))
    op.execute(text("DROP INDEX IF EXISTS ix_audit_events_account_id"))
    op.execute(text("DROP INDEX IF EXISTS ix_audit_chain_heads_last_event_id"))
    op.execute(text("DROP INDEX IF EXISTS ix_composite_templates_input_schema_id"))
    op.execute(text("DROP INDEX IF EXISTS ix_composite_templates_output_schema_id"))
    op.execute(text("DROP INDEX IF EXISTS ix_composite_templates_parameter_schema_id"))
    op.execute(text("DROP INDEX IF EXISTS ix_composite_templates_account_id"))
    op.execute(text("DROP INDEX IF EXISTS ix_connector_instances_account_id"))
    op.execute(text("DROP INDEX IF EXISTS ix_connector_instances_owner_team_id"))
    op.execute(text("DROP INDEX IF EXISTS ix_org_daily_run_counts_team_id"))
    op.execute(text("DROP INDEX IF EXISTS ix_environment_profiles_account_id"))
    op.execute(text("DROP INDEX IF EXISTS ix_environment_profiles_owner_team_id"))
    op.execute(text("DROP INDEX IF EXISTS ix_error_groups_sample_event_id"))
    op.execute(text("DROP INDEX IF EXISTS ix_error_groups_assigned_to"))
    op.execute(text("DROP INDEX IF EXISTS ix_eval_definitions_account_id"))
    op.execute(text("DROP INDEX IF EXISTS ix_eval_results_eval_id"))
    op.execute(text("DROP INDEX IF EXISTS ix_eval_suites_owner_team_id"))
    op.execute(text("DROP INDEX IF EXISTS ix_feedback_records_account_id"))
    op.execute(text("DROP INDEX IF EXISTS ix_feedback_records_producing_agent_id"))
    op.execute(text("DROP INDEX IF EXISTS ix_feedback_records_correction_run_id"))
    op.execute(text("DROP INDEX IF EXISTS ix_hitl_claims_required_team_id"))
    op.execute(text("DROP INDEX IF EXISTS ix_hitl_claims_account_id"))
    op.execute(text("DROP INDEX IF EXISTS ix_journeys_owner_team_id"))
    op.execute(text("DROP INDEX IF EXISTS ix_library_primitives_forked_from"))
    op.execute(text("DROP INDEX IF EXISTS ix_library_primitives_owner_team_id"))
    op.execute(text("DROP INDEX IF EXISTS ix_library_primitives_account_id"))
    op.execute(text("DROP INDEX IF EXISTS ix_library_primitives_update_available_version_id"))
    op.execute(text("DROP INDEX IF EXISTS ix_lifecycle_maps_owner_team_id"))
    op.execute(text("DROP INDEX IF EXISTS ix_lifecycle_maps_account_id"))
    op.execute(text("DROP INDEX IF EXISTS ix_lifecycle_map_stages_pipeline_id"))
    op.execute(text("DROP INDEX IF EXISTS ix_lifecycle_map_stages_account_id"))
    op.execute(text("DROP INDEX IF EXISTS ix_mcp_setup_tokens_created_by"))
    op.execute(text("DROP INDEX IF EXISTS ix_model_backends_owner_team_id"))
    op.execute(text("DROP INDEX IF EXISTS ix_model_backends_account_id"))
    op.execute(text("DROP INDEX IF EXISTS ix_nodes_account_id"))
    op.execute(text("DROP INDEX IF EXISTS ix_node_categories_account_id"))
    op.execute(text("DROP INDEX IF EXISTS ix_node_observations_account_id"))
    op.execute(text("DROP INDEX IF EXISTS ix_notifications_target_user_id"))
    op.execute(text("DROP INDEX IF EXISTS ix_notification_endpoints_account_id"))
    op.execute(text("DROP INDEX IF EXISTS ix_notification_endpoints_team_id"))
    op.execute(text("DROP INDEX IF EXISTS ix_oauth_clients_account_id"))
    op.execute(text("DROP INDEX IF EXISTS ix_oauth_consent_states_account_id"))
    op.execute(text("DROP INDEX IF EXISTS ix_parameter_schemas_account_id"))
    op.execute(text("DROP INDEX IF EXISTS ix_parameter_sets_account_id"))
    op.execute(text("DROP INDEX IF EXISTS ix_pipelines_folder_id"))
    op.execute(text("DROP INDEX IF EXISTS ix_pipeline_folders_parent_id"))
    op.execute(text("DROP INDEX IF EXISTS ix_pipeline_folders_account_id"))
    op.execute(text("DROP INDEX IF EXISTS ix_pipeline_snapshots_account_id"))
    op.execute(text("DROP INDEX IF EXISTS ix_primitive_abuse_reports_rating_id"))
    op.execute(text("DROP INDEX IF EXISTS ix_primitive_abuse_reports_reporter_account_id"))
    op.execute(text("DROP INDEX IF EXISTS ix_primitive_abuse_reports_reviewer_account_id"))
    op.execute(text("DROP INDEX IF EXISTS ix_chat_messages_parent_id"))
    op.execute(text("DROP INDEX IF EXISTS ix_runs_snapshot_id"))
    op.execute(text("DROP INDEX IF EXISTS ix_runs_trigger_id"))
    op.execute(text("DROP INDEX IF EXISTS ix_run_daily_facts_team_id"))
    op.execute(text("DROP INDEX IF EXISTS ix_run_daily_facts_pipeline_id"))
    op.execute(text("DROP INDEX IF EXISTS ix_run_daily_facts_folder_id"))
    op.execute(text("DROP INDEX IF EXISTS ix_run_evidence_run_id"))
    op.execute(text("DROP INDEX IF EXISTS ix_scheduled_reports_created_by"))
    op.execute(text("DROP INDEX IF EXISTS ix_schema_folders_parent_id"))
    op.execute(text("DROP INDEX IF EXISTS ix_schema_folders_account_id"))
    op.execute(text("DROP INDEX IF EXISTS ix_schemas_account_id"))
    op.execute(text("DROP INDEX IF EXISTS ix_schema_versions_account_id"))
    op.execute(text("DROP INDEX IF EXISTS ix_snapshot_schema_pins_snapshot_id"))
    op.execute(text("DROP INDEX IF EXISTS ix_snapshot_schema_pins_schema_id"))
    op.execute(text("DROP INDEX IF EXISTS ix_spend_anomalies_pipeline_id"))
    op.execute(text("DROP INDEX IF EXISTS ix_system_config_updated_by"))
    op.execute(text("DROP INDEX IF EXISTS ix_teams_account_id"))
    op.execute(text("DROP INDEX IF EXISTS ix_feature_flag_catalog_tier_id"))
    op.execute(text("DROP INDEX IF EXISTS ix_triggers_account_id"))
    op.execute(text("DROP INDEX IF EXISTS ix_saved_views_account_id"))
    op.execute(text("DROP INDEX IF EXISTS ix_webhook_payloads_trigger_event_id"))
