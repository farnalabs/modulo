"""Farna Control native baseline migration 0352_baseline_privileges.

This baseline intentionally owns every FORCE-RLS table as
`farna_control_migrate`. That closes a pre-existing gap where four tables
were superuser-owned, and deliberately differs from the historical chain in
exactly those four ownership entries.
"""

from alembic import op

revision: str = "0352_baseline_privileges"
down_revision: str | None = "0351_baseline_rls"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""SET search_path TO public;

ALTER TABLE public.agents OWNER TO farna_control_migrate;
ALTER TABLE public.dismissals OWNER TO farna_control_migrate;
ALTER TABLE public.run_evidence OWNER TO farna_control_migrate;
ALTER TABLE public.trigger_event_findings OWNER TO farna_control_migrate;
ALTER TABLE public.accounts OWNER TO farna_control_migrate;
ALTER TABLE public.agent_runner_bindings OWNER TO farna_control_migrate;
ALTER TABLE public.collection_install OWNER TO farna_control_migrate;
ALTER TABLE public.collection_install_entity OWNER TO farna_control_migrate;
ALTER TABLE public.connector_profiles OWNER TO farna_control_migrate;
ALTER TABLE public.cost_components OWNER TO farna_control_migrate;
ALTER TABLE public.eval_cases OWNER TO farna_control_migrate;
ALTER TABLE public.eval_datasets OWNER TO farna_control_migrate;
ALTER TABLE public.eval_suites OWNER TO farna_control_migrate;
ALTER TABLE public.evals OWNER TO farna_control_migrate;
ALTER TABLE public.evidence OWNER TO farna_control_migrate;
ALTER TABLE public.evidence_redaction_rules OWNER TO farna_control_migrate;
ALTER TABLE public.external_agents OWNER TO farna_control_migrate;
ALTER TABLE public.mcp_servers OWNER TO farna_control_migrate;
ALTER TABLE public.notification_preferences OWNER TO farna_control_migrate;
ALTER TABLE public.org_api_keys OWNER TO farna_control_migrate;
ALTER TABLE public.org_memberships OWNER TO farna_control_migrate;
ALTER TABLE public.org_mint_budget_usage OWNER TO farna_control_migrate;
ALTER TABLE public.organisation_cost_state OWNER TO farna_control_migrate;
ALTER TABLE public.policy_gate_decisions OWNER TO farna_control_migrate;
ALTER TABLE public.policy_gates OWNER TO farna_control_migrate;
ALTER TABLE public.run_attempts OWNER TO farna_control_migrate;
ALTER TABLE public.run_node_outputs OWNER TO farna_control_migrate;
ALTER TABLE public.runner_probe_cache OWNER TO farna_control_migrate;
ALTER TABLE public.suite_runs OWNER TO farna_control_migrate;
ALTER TABLE public.system_map_attribution_bindings OWNER TO farna_control_migrate;
ALTER TABLE public.system_map_loop_admission_attempts OWNER TO farna_control_migrate;
ALTER TABLE public.system_map_loop_configs OWNER TO farna_control_migrate;
ALTER TABLE public.system_map_loop_reservations OWNER TO farna_control_migrate;
ALTER TABLE public.system_map_nodes OWNER TO farna_control_migrate;
ALTER TABLE public.system_map_relationships OWNER TO farna_control_migrate;
ALTER TABLE public.system_map_revisions OWNER TO farna_control_migrate;
ALTER TABLE public.system_maps OWNER TO farna_control_migrate;
ALTER TABLE public.token_families OWNER TO farna_control_migrate;
ALTER TABLE public.variant_batch_state OWNER TO farna_control_migrate;

ALTER DEFAULT PRIVILEGES FOR ROLE postgres IN SCHEMA public GRANT ALL ON SEQUENCES TO farna_control_app;
ALTER DEFAULT PRIVILEGES FOR ROLE postgres IN SCHEMA public GRANT USAGE ON SEQUENCES TO farna_control_system;


--
-- Name: DEFAULT PRIVILEGES FOR TABLES; Type: DEFAULT ACL; Schema: public; Owner: postgres
--

ALTER DEFAULT PRIVILEGES FOR ROLE postgres IN SCHEMA public GRANT SELECT,INSERT,DELETE,UPDATE ON TABLES TO farna_control_app;
ALTER DEFAULT PRIVILEGES FOR ROLE postgres IN SCHEMA public GRANT SELECT,INSERT,DELETE,UPDATE ON TABLES TO farna_control_system;


--
-- PostgreSQL database dump complete
--

GRANT ALL ON SCHEMA public TO farna_control_migrate;
GRANT ALL ON SCHEMA public TO farna_control_app;
GRANT USAGE ON SCHEMA public TO farna_control_breakglass;
GRANT USAGE ON SCHEMA public TO farna_control_system;
GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public._migration_0191_repoint_state TO farna_control_app;
GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public._migration_0191_repoint_state TO farna_control_system;
GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.accounts TO farna_control_app;
GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.accounts TO farna_control_system;
GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.agents TO farna_control_app;
GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.agents TO farna_control_system;
GRANT REFERENCES ON TABLE public.agents TO farna_control_migrate;
GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.alembic_version TO farna_control_app;
GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.alembic_version TO farna_control_system;
GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.assistant_context_sources TO farna_control_app;
GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.assistant_context_sources TO farna_control_system;
GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.assistant_skills TO farna_control_app;
GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.assistant_skills TO farna_control_system;
GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.audit_chain_heads TO farna_control_app;
GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.audit_chain_heads TO farna_control_system;
GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.audit_events TO farna_control_app;
GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.audit_events TO farna_control_system;
GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.chat_messages TO farna_control_app;
GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.chat_messages TO farna_control_system;
GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.chat_sessions TO farna_control_app;
GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.chat_sessions TO farna_control_system;
GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.collection_install TO farna_control_system;
GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.collection_install_entity TO farna_control_system;
GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.composite_templates TO farna_control_app;
GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.composite_templates TO farna_control_system;
GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.connector_instances TO farna_control_app;
GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.connector_instances TO farna_control_system;
GRANT REFERENCES ON TABLE public.connector_instances TO farna_control_migrate;
GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.cost_components TO farna_control_app;
GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.cost_components TO farna_control_system;
GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.deleted_defaults TO farna_control_app;
GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.deleted_defaults TO farna_control_system;
GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.dismissals TO farna_control_app;
GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.dismissals TO farna_control_system;
GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.environment_profiles TO farna_control_app;
GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.environment_profiles TO farna_control_system;
GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.error_events TO farna_control_app;
GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.error_events TO farna_control_system;
GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.error_forwarder_configs TO farna_control_app;
GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.error_forwarder_configs TO farna_control_system;
GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.error_groups TO farna_control_app;
GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.error_groups TO farna_control_system;
GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.error_notification_rules TO farna_control_app;
GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.error_notification_rules TO farna_control_system;
GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.eval_backfill_violations TO farna_control_app;
GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.eval_backfill_violations TO farna_control_system;
GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.eval_results TO farna_control_app;
GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.eval_results TO farna_control_system;
GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.eval_suites TO farna_control_app;
GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.eval_suites TO farna_control_system;
GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.evals TO farna_control_app;
GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.evals TO farna_control_system;
GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.evidence TO farna_control_app;
GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.evidence TO farna_control_system;
GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.evidence_redaction_rules TO farna_control_app;
GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.evidence_redaction_rules TO farna_control_system;
GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.external_agents TO farna_control_system;
GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.feature_flag_catalog TO farna_control_app;
GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.feature_flag_catalog TO farna_control_system;
GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.feedback_records TO farna_control_app;
GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.feedback_records TO farna_control_system;
GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.hitl_claims TO farna_control_app;
GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.hitl_claims TO farna_control_system;
GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.invitations TO farna_control_app;
GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.invitations TO farna_control_system;
GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.journeys TO farna_control_app;
GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.journeys TO farna_control_system;
GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.library_primitives TO farna_control_app;
GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.library_primitives TO farna_control_system;
GRANT REFERENCES ON TABLE public.library_primitives TO farna_control_migrate;
GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.library_sync_state TO farna_control_app;
GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.library_sync_state TO farna_control_system;
GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.lifecycle_map_stages TO farna_control_app;
GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.lifecycle_map_stages TO farna_control_system;
GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.lifecycle_maps TO farna_control_app;
GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.lifecycle_maps TO farna_control_system;
GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.mcp_servers TO farna_control_system;
GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.mcp_setup_tokens TO farna_control_app;
GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.mcp_setup_tokens TO farna_control_system;
GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.metrics_staging TO farna_control_app;
GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.metrics_staging TO farna_control_system;
GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.model_backends TO farna_control_app;
GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.model_backends TO farna_control_system;
GRANT REFERENCES ON TABLE public.model_backends TO farna_control_migrate;
GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.modulo_journey_facts TO farna_control_app;
GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.modulo_journey_facts TO farna_control_system;
GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.node_categories TO farna_control_app;
GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.node_categories TO farna_control_system;
GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.node_observations TO farna_control_app;
GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.node_observations TO farna_control_system;
GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.nodes TO farna_control_app;
GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.nodes TO farna_control_system;
GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.notification_delivery_log TO farna_control_app;
GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.notification_delivery_log TO farna_control_system;
GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.notification_endpoints TO farna_control_app;
GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.notification_endpoints TO farna_control_system;
GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.notifications TO farna_control_app;
GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.notifications TO farna_control_system;
GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.oauth_authorization_codes TO farna_control_app;
GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.oauth_authorization_codes TO farna_control_system;
GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.oauth_clients TO farna_control_app;
GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.oauth_clients TO farna_control_system;
GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.oauth_consent_states TO farna_control_app;
GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.oauth_consent_states TO farna_control_system;
GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.oauth_token_families TO farna_control_app;
GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.oauth_token_families TO farna_control_system;
GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.onboarding_progress TO farna_control_app;
GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.onboarding_progress TO farna_control_system;
GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.org_api_keys TO farna_control_app;
GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.org_api_keys TO farna_control_system;
GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.org_daily_run_counts TO farna_control_app;
GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.org_daily_run_counts TO farna_control_system;
GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.org_memberships TO farna_control_app;
GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.org_memberships TO farna_control_system;
GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.org_mint_budget_usage TO farna_control_system;
GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.organisation_cost_state TO farna_control_system;
GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.organisations TO farna_control_app;
GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.organisations TO farna_control_system;
GRANT SELECT,REFERENCES ON TABLE public.organisations TO farna_control_migrate;
GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.parameter_schemas TO farna_control_app;
GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.parameter_schemas TO farna_control_system;
GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.parameter_sets TO farna_control_app;
GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.parameter_sets TO farna_control_system;
GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.pipeline_edges TO farna_control_app;
GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.pipeline_edges TO farna_control_system;
GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.pipeline_folders TO farna_control_app;
GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.pipeline_folders TO farna_control_system;
GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.pipeline_snapshots TO farna_control_app;
GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.pipeline_snapshots TO farna_control_system;
GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.pipelines TO farna_control_app;
GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.pipelines TO farna_control_system;
GRANT REFERENCES ON TABLE public.pipelines TO farna_control_migrate;
GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.policy_gate_decisions TO farna_control_app;
GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.policy_gate_decisions TO farna_control_system;
GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.policy_gates TO farna_control_app;
GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.policy_gates TO farna_control_system;
GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.primitive_abuse_reports TO farna_control_app;
GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.primitive_abuse_reports TO farna_control_system;
GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.primitive_ratings TO farna_control_app;
GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.primitive_ratings TO farna_control_system;
GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.publishers TO farna_control_app;
GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.publishers TO farna_control_system;
GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.run_attempts TO farna_control_system;
GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.run_daily_facts TO farna_control_app;
GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.run_daily_facts TO farna_control_system;
GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.run_evidence TO farna_control_app;
GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.run_evidence TO farna_control_system;
GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.run_node_outputs_quarantine TO farna_control_app;
GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.run_node_outputs_quarantine TO farna_control_system;
GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.run_number_counters TO farna_control_app;
GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.run_number_counters TO farna_control_system;
GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.runner_probe_cache TO farna_control_system;
GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.runs TO farna_control_app;
GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.runs TO farna_control_system;
GRANT REFERENCES ON TABLE public.runs TO farna_control_migrate;
GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.saved_views TO farna_control_app;
GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.saved_views TO farna_control_system;
GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.scheduled_reports TO farna_control_app;
GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.scheduled_reports TO farna_control_system;
GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.schema_folders TO farna_control_app;
GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.schema_folders TO farna_control_system;
GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.schema_versions TO farna_control_app;
GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.schema_versions TO farna_control_system;
GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.schemas TO farna_control_app;
GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.schemas TO farna_control_system;
GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.secrets TO farna_control_app;
GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.secrets TO farna_control_system;
GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.snapshot_schema_pins TO farna_control_app;
GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.snapshot_schema_pins TO farna_control_system;
GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.spend_anomalies TO farna_control_app;
GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.spend_anomalies TO farna_control_system;
GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.sso_providers TO farna_control_app;
GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.sso_providers TO farna_control_system;
GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.system_audit_events TO farna_control_app;
GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.system_audit_events TO farna_control_system;
GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.system_config TO farna_control_app;
GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.system_config TO farna_control_system;
GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.system_map_attribution_bindings TO farna_control_system;
GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.system_map_loop_admission_attempts TO farna_control_system;
GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.system_map_loop_configs TO farna_control_system;
GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.system_map_loop_reservations TO farna_control_system;
GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.system_map_nodes TO farna_control_system;
GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.system_map_relationships TO farna_control_system;
GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.system_map_revisions TO farna_control_system;
GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.system_maps TO farna_control_system;
GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.team_memberships TO farna_control_app;
GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.team_memberships TO farna_control_system;
GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.teams TO farna_control_app;
GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.teams TO farna_control_system;
GRANT REFERENCES ON TABLE public.teams TO farna_control_migrate;
GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.tier_catalog TO farna_control_app;
GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.tier_catalog TO farna_control_system;
GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.token_families TO farna_control_app;
GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.token_families TO farna_control_system;
GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.trigger_event_findings TO farna_control_app;
GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.trigger_event_findings TO farna_control_system;
GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.trigger_events TO farna_control_app;
GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.trigger_events TO farna_control_system;
GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.triggers TO farna_control_app;
GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.triggers TO farna_control_system;
GRANT REFERENCES ON TABLE public.triggers TO farna_control_migrate;
GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.variant_batch_state TO farna_control_system;
GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.variant_groups TO farna_control_app;
GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.variant_groups TO farna_control_system;
GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.web_vital_events TO farna_control_app;
GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.web_vital_events TO farna_control_system;
GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.webhook_dedup_hashes TO farna_control_app;
GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.webhook_dedup_hashes TO farna_control_system;
GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.webhook_payloads TO farna_control_app;
GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.webhook_payloads TO farna_control_system;""")


def downgrade() -> None:
    op.execute("""""")
