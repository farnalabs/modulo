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


def _guard_role(role: str, sql: str) -> str:
    """Wrap SQL in a DO block that no-ops if the target role does not exist."""
    # Role and SQL are hardcoded constants in this migration; B608/S608 are false positives.
    return (
        "DO $$\n"  # nosec B608  # noqa: S608
        "BEGIN\n"
        "    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = '" + role + "') THEN\n"
        "        EXECUTE " + repr(sql) + ";\n"
        "    END IF;\n"
        "END $$;"
    )


def upgrade() -> None:
    # Table ownership changes — guarded on farna_control_migrate existence
    tables_to_own = [
        "agents",
        "dismissals",
        "run_evidence",
        "trigger_event_findings",
        "accounts",
        "agent_runner_bindings",
        "collection_install",
        "collection_install_entity",
        "connector_profiles",
        "cost_components",
        "eval_cases",
        "eval_datasets",
        "eval_suites",
        "evals",
        "evidence",
        "evidence_redaction_rules",
        "external_agents",
        "mcp_servers",
        "notification_preferences",
        "org_api_keys",
        "org_memberships",
        "org_mint_budget_usage",
        "organisation_cost_state",
        "policy_gate_decisions",
        "policy_gates",
        "run_attempts",
        "run_node_outputs",
        "runner_probe_cache",
        "suite_runs",
        "system_map_attribution_bindings",
        "system_map_loop_admission_attempts",
        "system_map_loop_configs",
        "system_map_loop_reservations",
        "system_map_nodes",
        "system_map_relationships",
        "system_map_revisions",
        "system_maps",
        "token_families",
        "variant_batch_state",
    ]
    for table in tables_to_own:
        op.execute(_guard_role("farna_control_migrate", f"ALTER TABLE public.{table} OWNER TO farna_control_migrate"))

    # Default privileges — dynamically determine the current superuser role
    # and set default privileges for that role. Guard each target role.
    op.execute("""
DO $$
DECLARE
    superuser_role text := current_user;
BEGIN
    -- Default privileges for sequences
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'farna_control_app') THEN
        EXECUTE format('ALTER DEFAULT PRIVILEGES FOR ROLE %I IN SCHEMA public GRANT ALL ON SEQUENCES TO farna_control_app', superuser_role);
        EXECUTE format('ALTER DEFAULT PRIVILEGES FOR ROLE %I IN SCHEMA public GRANT SELECT, INSERT, DELETE, UPDATE ON TABLES TO farna_control_app', superuser_role);
    END IF;
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'farna_control_system') THEN
        EXECUTE format('ALTER DEFAULT PRIVILEGES FOR ROLE %I IN SCHEMA public GRANT USAGE ON SEQUENCES TO farna_control_system', superuser_role);
        EXECUTE format('ALTER DEFAULT PRIVILEGES FOR ROLE %I IN SCHEMA public GRANT SELECT, INSERT, DELETE, UPDATE ON TABLES TO farna_control_system', superuser_role);
    END IF;
END $$;
""")

    # Grants — guarded on each target role's existence
    grant_statements = [
        ("farna_control_migrate", "GRANT ALL ON SCHEMA public TO farna_control_migrate"),
        ("farna_control_app", "GRANT ALL ON SCHEMA public TO farna_control_app"),
        ("farna_control_breakglass", "GRANT USAGE ON SCHEMA public TO farna_control_breakglass"),
        ("farna_control_system", "GRANT USAGE ON SCHEMA public TO farna_control_system"),
        ("farna_control_app", "GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public._migration_0191_repoint_state TO farna_control_app"),
        ("farna_control_system", "GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public._migration_0191_repoint_state TO farna_control_system"),
        ("farna_control_app", "GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.accounts TO farna_control_app"),
        ("farna_control_system", "GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.accounts TO farna_control_system"),
        ("farna_control_app", "GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.agents TO farna_control_app"),
        ("farna_control_system", "GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.agents TO farna_control_system"),
        ("farna_control_migrate", "GRANT REFERENCES ON TABLE public.agents TO farna_control_migrate"),
        ("farna_control_app", "GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.alembic_version TO farna_control_app"),
        ("farna_control_system", "GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.alembic_version TO farna_control_system"),
        ("farna_control_app", "GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.assistant_context_sources TO farna_control_app"),
        ("farna_control_system", "GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.assistant_context_sources TO farna_control_system"),
        ("farna_control_app", "GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.assistant_skills TO farna_control_app"),
        ("farna_control_system", "GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.assistant_skills TO farna_control_system"),
        ("farna_control_app", "GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.audit_chain_heads TO farna_control_app"),
        ("farna_control_system", "GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.audit_chain_heads TO farna_control_system"),
        ("farna_control_app", "GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.audit_events TO farna_control_app"),
        ("farna_control_system", "GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.audit_events TO farna_control_system"),
        ("farna_control_app", "GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.chat_messages TO farna_control_app"),
        ("farna_control_system", "GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.chat_messages TO farna_control_system"),
        ("farna_control_app", "GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.chat_sessions TO farna_control_app"),
        ("farna_control_system", "GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.chat_sessions TO farna_control_system"),
        ("farna_control_system", "GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.collection_install TO farna_control_system"),
        ("farna_control_system", "GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.collection_install_entity TO farna_control_system"),
        ("farna_control_app", "GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.composite_templates TO farna_control_app"),
        ("farna_control_system", "GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.composite_templates TO farna_control_system"),
        ("farna_control_app", "GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.connector_instances TO farna_control_app"),
        ("farna_control_system", "GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.connector_instances TO farna_control_system"),
        ("farna_control_migrate", "GRANT REFERENCES ON TABLE public.connector_instances TO farna_control_migrate"),
        ("farna_control_app", "GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.cost_components TO farna_control_app"),
        ("farna_control_system", "GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.cost_components TO farna_control_system"),
        ("farna_control_app", "GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.deleted_defaults TO farna_control_app"),
        ("farna_control_system", "GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.deleted_defaults TO farna_control_system"),
        ("farna_control_app", "GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.dismissals TO farna_control_app"),
        ("farna_control_system", "GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.dismissals TO farna_control_system"),
        ("farna_control_app", "GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.environment_profiles TO farna_control_app"),
        ("farna_control_system", "GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.environment_profiles TO farna_control_system"),
        ("farna_control_app", "GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.error_events TO farna_control_app"),
        ("farna_control_system", "GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.error_events TO farna_control_system"),
        ("farna_control_app", "GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.error_forwarder_configs TO farna_control_app"),
        ("farna_control_system", "GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.error_forwarder_configs TO farna_control_system"),
        ("farna_control_app", "GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.error_groups TO farna_control_app"),
        ("farna_control_system", "GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.error_groups TO farna_control_system"),
        ("farna_control_app", "GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.error_notification_rules TO farna_control_app"),
        ("farna_control_system", "GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.error_notification_rules TO farna_control_system"),
        ("farna_control_app", "GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.eval_backfill_violations TO farna_control_app"),
        ("farna_control_system", "GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.eval_backfill_violations TO farna_control_system"),
        ("farna_control_app", "GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.eval_results TO farna_control_app"),
        ("farna_control_system", "GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.eval_results TO farna_control_system"),
        ("farna_control_app", "GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.eval_suites TO farna_control_app"),
        ("farna_control_system", "GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.eval_suites TO farna_control_system"),
        ("farna_control_app", "GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.evals TO farna_control_app"),
        ("farna_control_system", "GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.evals TO farna_control_system"),
        ("farna_control_app", "GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.evidence TO farna_control_app"),
        ("farna_control_system", "GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.evidence TO farna_control_system"),
        ("farna_control_app", "GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.evidence_redaction_rules TO farna_control_app"),
        ("farna_control_system", "GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.evidence_redaction_rules TO farna_control_system"),
        ("farna_control_system", "GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.external_agents TO farna_control_system"),
        ("farna_control_app", "GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.feature_flag_catalog TO farna_control_app"),
        ("farna_control_system", "GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.feature_flag_catalog TO farna_control_system"),
        ("farna_control_app", "GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.feedback_records TO farna_control_app"),
        ("farna_control_system", "GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.feedback_records TO farna_control_system"),
        ("farna_control_app", "GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.hitl_claims TO farna_control_app"),
        ("farna_control_system", "GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.hitl_claims TO farna_control_system"),
        ("farna_control_app", "GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.invitations TO farna_control_app"),
        ("farna_control_system", "GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.invitations TO farna_control_system"),
        ("farna_control_app", "GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.journeys TO farna_control_app"),
        ("farna_control_system", "GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.journeys TO farna_control_system"),
        ("farna_control_app", "GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.library_primitives TO farna_control_app"),
        ("farna_control_system", "GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.library_primitives TO farna_control_system"),
        ("farna_control_migrate", "GRANT REFERENCES ON TABLE public.library_primitives TO farna_control_migrate"),
        ("farna_control_app", "GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.library_sync_state TO farna_control_app"),
        ("farna_control_system", "GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.library_sync_state TO farna_control_system"),
        ("farna_control_app", "GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.lifecycle_map_stages TO farna_control_app"),
        ("farna_control_system", "GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.lifecycle_map_stages TO farna_control_system"),
        ("farna_control_app", "GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.lifecycle_maps TO farna_control_app"),
        ("farna_control_system", "GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.lifecycle_maps TO farna_control_system"),
        ("farna_control_system", "GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.mcp_servers TO farna_control_system"),
        ("farna_control_app", "GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.mcp_setup_tokens TO farna_control_app"),
        ("farna_control_system", "GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.mcp_setup_tokens TO farna_control_system"),
        ("farna_control_app", "GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.metrics_staging TO farna_control_app"),
        ("farna_control_system", "GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.metrics_staging TO farna_control_system"),
        ("farna_control_app", "GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.model_backends TO farna_control_app"),
        ("farna_control_system", "GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.model_backends TO farna_control_system"),
        ("farna_control_migrate", "GRANT REFERENCES ON TABLE public.model_backends TO farna_control_migrate"),
        ("farna_control_app", "GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.modulo_journey_facts TO farna_control_app"),
        ("farna_control_system", "GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.modulo_journey_facts TO farna_control_system"),
        ("farna_control_app", "GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.node_categories TO farna_control_app"),
        ("farna_control_system", "GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.node_categories TO farna_control_system"),
        ("farna_control_app", "GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.node_observations TO farna_control_app"),
        ("farna_control_system", "GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.node_observations TO farna_control_system"),
        ("farna_control_app", "GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.nodes TO farna_control_app"),
        ("farna_control_system", "GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.nodes TO farna_control_system"),
        ("farna_control_app", "GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.notification_delivery_log TO farna_control_app"),
        ("farna_control_system", "GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.notification_delivery_log TO farna_control_system"),
        ("farna_control_app", "GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.notification_endpoints TO farna_control_app"),
        ("farna_control_system", "GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.notification_endpoints TO farna_control_system"),
        ("farna_control_app", "GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.notifications TO farna_control_app"),
        ("farna_control_system", "GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.notifications TO farna_control_system"),
        ("farna_control_app", "GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.oauth_authorization_codes TO farna_control_app"),
        ("farna_control_system", "GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.oauth_authorization_codes TO farna_control_system"),
        ("farna_control_app", "GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.oauth_clients TO farna_control_app"),
        ("farna_control_system", "GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.oauth_clients TO farna_control_system"),
        ("farna_control_app", "GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.oauth_consent_states TO farna_control_app"),
        ("farna_control_system", "GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.oauth_consent_states TO farna_control_system"),
        ("farna_control_app", "GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.oauth_token_families TO farna_control_app"),
        ("farna_control_system", "GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.oauth_token_families TO farna_control_system"),
        ("farna_control_app", "GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.onboarding_progress TO farna_control_app"),
        ("farna_control_system", "GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.onboarding_progress TO farna_control_system"),
        ("farna_control_app", "GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.org_api_keys TO farna_control_app"),
        ("farna_control_system", "GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.org_api_keys TO farna_control_system"),
        ("farna_control_app", "GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.org_daily_run_counts TO farna_control_app"),
        ("farna_control_system", "GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.org_daily_run_counts TO farna_control_system"),
        ("farna_control_app", "GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.org_memberships TO farna_control_app"),
        ("farna_control_system", "GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.org_memberships TO farna_control_system"),
        ("farna_control_system", "GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.org_mint_budget_usage TO farna_control_system"),
        ("farna_control_system", "GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.organisation_cost_state TO farna_control_system"),
        ("farna_control_app", "GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.organisations TO farna_control_app"),
        ("farna_control_system", "GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.organisations TO farna_control_system"),
        ("farna_control_migrate", "GRANT SELECT,REFERENCES ON TABLE public.organisations TO farna_control_migrate"),
        ("farna_control_app", "GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.parameter_schemas TO farna_control_app"),
        ("farna_control_system", "GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.parameter_schemas TO farna_control_system"),
        ("farna_control_app", "GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.parameter_sets TO farna_control_app"),
        ("farna_control_system", "GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.parameter_sets TO farna_control_system"),
        ("farna_control_app", "GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.pipeline_edges TO farna_control_app"),
        ("farna_control_system", "GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.pipeline_edges TO farna_control_system"),
        ("farna_control_app", "GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.pipeline_folders TO farna_control_app"),
        ("farna_control_system", "GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.pipeline_folders TO farna_control_system"),
        ("farna_control_app", "GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.pipeline_snapshots TO farna_control_app"),
        ("farna_control_system", "GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.pipeline_snapshots TO farna_control_system"),
        ("farna_control_app", "GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.pipelines TO farna_control_app"),
        ("farna_control_system", "GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.pipelines TO farna_control_system"),
        ("farna_control_migrate", "GRANT REFERENCES ON TABLE public.pipelines TO farna_control_migrate"),
        ("farna_control_app", "GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.policy_gate_decisions TO farna_control_app"),
        ("farna_control_system", "GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.policy_gate_decisions TO farna_control_system"),
        ("farna_control_app", "GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.policy_gates TO farna_control_app"),
        ("farna_control_system", "GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.policy_gates TO farna_control_system"),
        ("farna_control_app", "GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.primitive_abuse_reports TO farna_control_app"),
        ("farna_control_system", "GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.primitive_abuse_reports TO farna_control_system"),
        ("farna_control_app", "GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.primitive_ratings TO farna_control_app"),
        ("farna_control_system", "GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.primitive_ratings TO farna_control_system"),
        ("farna_control_app", "GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.publishers TO farna_control_app"),
        ("farna_control_system", "GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.publishers TO farna_control_system"),
        ("farna_control_system", "GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.run_attempts TO farna_control_system"),
        ("farna_control_app", "GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.run_daily_facts TO farna_control_app"),
        ("farna_control_system", "GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.run_daily_facts TO farna_control_system"),
        ("farna_control_app", "GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.run_evidence TO farna_control_app"),
        ("farna_control_system", "GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.run_evidence TO farna_control_system"),
        ("farna_control_app", "GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.run_node_outputs_quarantine TO farna_control_app"),
        ("farna_control_system", "GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.run_node_outputs_quarantine TO farna_control_system"),
        ("farna_control_app", "GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.run_number_counters TO farna_control_app"),
        ("farna_control_system", "GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.run_number_counters TO farna_control_system"),
        ("farna_control_system", "GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.runner_probe_cache TO farna_control_system"),
        ("farna_control_app", "GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.runs TO farna_control_app"),
        ("farna_control_system", "GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.runs TO farna_control_system"),
        ("farna_control_migrate", "GRANT REFERENCES ON TABLE public.runs TO farna_control_migrate"),
        ("farna_control_app", "GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.saved_views TO farna_control_app"),
        ("farna_control_system", "GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.saved_views TO farna_control_system"),
        ("farna_control_app", "GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.scheduled_reports TO farna_control_app"),
        ("farna_control_system", "GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.scheduled_reports TO farna_control_system"),
        ("farna_control_app", "GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.schema_folders TO farna_control_app"),
        ("farna_control_system", "GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.schema_folders TO farna_control_system"),
        ("farna_control_app", "GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.schema_versions TO farna_control_app"),
        ("farna_control_system", "GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.schema_versions TO farna_control_system"),
        ("farna_control_app", "GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.schemas TO farna_control_app"),
        ("farna_control_system", "GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.schemas TO farna_control_system"),
        ("farna_control_app", "GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.secrets TO farna_control_app"),
        ("farna_control_system", "GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.secrets TO farna_control_system"),
        ("farna_control_app", "GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.snapshot_schema_pins TO farna_control_app"),
        ("farna_control_system", "GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.snapshot_schema_pins TO farna_control_system"),
        ("farna_control_app", "GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.spend_anomalies TO farna_control_app"),
        ("farna_control_system", "GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.spend_anomalies TO farna_control_system"),
        ("farna_control_app", "GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.sso_providers TO farna_control_app"),
        ("farna_control_system", "GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.sso_providers TO farna_control_system"),
        ("farna_control_app", "GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.system_audit_events TO farna_control_app"),
        ("farna_control_system", "GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.system_audit_events TO farna_control_system"),
        ("farna_control_app", "GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.system_config TO farna_control_app"),
        ("farna_control_system", "GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.system_config TO farna_control_system"),
        ("farna_control_system", "GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.system_map_attribution_bindings TO farna_control_system"),
        ("farna_control_system", "GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.system_map_loop_admission_attempts TO farna_control_system"),
        ("farna_control_system", "GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.system_map_loop_configs TO farna_control_system"),
        ("farna_control_system", "GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.system_map_loop_reservations TO farna_control_system"),
        ("farna_control_system", "GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.system_map_nodes TO farna_control_system"),
        ("farna_control_system", "GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.system_map_relationships TO farna_control_system"),
        ("farna_control_system", "GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.system_map_revisions TO farna_control_system"),
        ("farna_control_system", "GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.system_maps TO farna_control_system"),
        ("farna_control_app", "GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.team_memberships TO farna_control_app"),
        ("farna_control_system", "GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.team_memberships TO farna_control_system"),
        ("farna_control_app", "GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.teams TO farna_control_app"),
        ("farna_control_system", "GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.teams TO farna_control_system"),
        ("farna_control_migrate", "GRANT REFERENCES ON TABLE public.teams TO farna_control_migrate"),
        ("farna_control_app", "GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.tier_catalog TO farna_control_app"),
        ("farna_control_system", "GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.tier_catalog TO farna_control_system"),
        ("farna_control_app", "GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.token_families TO farna_control_app"),
        ("farna_control_system", "GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.token_families TO farna_control_system"),
        ("farna_control_app", "GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.trigger_event_findings TO farna_control_app"),
        ("farna_control_system", "GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.trigger_event_findings TO farna_control_system"),
        ("farna_control_app", "GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.trigger_events TO farna_control_app"),
        ("farna_control_system", "GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.trigger_events TO farna_control_system"),
        ("farna_control_app", "GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.triggers TO farna_control_app"),
        ("farna_control_system", "GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.triggers TO farna_control_system"),
        ("farna_control_migrate", "GRANT REFERENCES ON TABLE public.triggers TO farna_control_migrate"),
        ("farna_control_system", "GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.variant_batch_state TO farna_control_system"),
        ("farna_control_app", "GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.variant_groups TO farna_control_app"),
        ("farna_control_system", "GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.variant_groups TO farna_control_system"),
        ("farna_control_app", "GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.web_vital_events TO farna_control_app"),
        ("farna_control_system", "GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.web_vital_events TO farna_control_system"),
        ("farna_control_app", "GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.webhook_dedup_hashes TO farna_control_app"),
        ("farna_control_system", "GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.webhook_dedup_hashes TO farna_control_system"),
        ("farna_control_app", "GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.webhook_payloads TO farna_control_app"),
        ("farna_control_system", "GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.webhook_payloads TO farna_control_system"),
    ]

    for role, sql in grant_statements:
        op.execute(_guard_role(role, sql))


def downgrade() -> None:
    op.execute("""""")
