"""Farna Control native baseline migration 0002_baseline_rls."""

from alembic import op

revision: str = "0002_baseline_rls"
down_revision: str | None = "0001_baseline_schema"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""SET search_path TO public;

-- Name: agent_runner_bindings; Type: ROW SECURITY; Schema: public; Owner: -
--

ALTER TABLE public.agent_runner_bindings ENABLE ROW LEVEL SECURITY;

--

-- Name: agents; Type: ROW SECURITY; Schema: public; Owner: -
--

ALTER TABLE public.agents ENABLE ROW LEVEL SECURITY;

--

-- Name: assistant_context_sources; Type: ROW SECURITY; Schema: public; Owner: -
--

ALTER TABLE public.assistant_context_sources ENABLE ROW LEVEL SECURITY;

--

-- Name: assistant_skills; Type: ROW SECURITY; Schema: public; Owner: -
--

ALTER TABLE public.assistant_skills ENABLE ROW LEVEL SECURITY;

--

-- Name: audit_chain_heads; Type: ROW SECURITY; Schema: public; Owner: -
--

ALTER TABLE public.audit_chain_heads ENABLE ROW LEVEL SECURITY;

--

-- Name: audit_events; Type: ROW SECURITY; Schema: public; Owner: -
--

ALTER TABLE public.audit_events ENABLE ROW LEVEL SECURITY;

--

-- Name: chat_messages; Type: ROW SECURITY; Schema: public; Owner: -
--

ALTER TABLE public.chat_messages ENABLE ROW LEVEL SECURITY;

--

-- Name: chat_sessions; Type: ROW SECURITY; Schema: public; Owner: -
--

ALTER TABLE public.chat_sessions ENABLE ROW LEVEL SECURITY;

--

-- Name: collection_install; Type: ROW SECURITY; Schema: public; Owner: -
--

ALTER TABLE public.collection_install ENABLE ROW LEVEL SECURITY;

--

-- Name: collection_install_entity; Type: ROW SECURITY; Schema: public; Owner: -
--

ALTER TABLE public.collection_install_entity ENABLE ROW LEVEL SECURITY;

--

-- Name: composite_templates; Type: ROW SECURITY; Schema: public; Owner: -
--

ALTER TABLE public.composite_templates ENABLE ROW LEVEL SECURITY;

--

-- Name: connector_instances; Type: ROW SECURITY; Schema: public; Owner: -
--

ALTER TABLE public.connector_instances ENABLE ROW LEVEL SECURITY;

--

-- Name: connector_profiles; Type: ROW SECURITY; Schema: public; Owner: -
--

ALTER TABLE public.connector_profiles ENABLE ROW LEVEL SECURITY;

--

-- Name: cost_components; Type: ROW SECURITY; Schema: public; Owner: -
--

ALTER TABLE public.cost_components ENABLE ROW LEVEL SECURITY;

--

-- Name: deleted_defaults; Type: ROW SECURITY; Schema: public; Owner: -
--

ALTER TABLE public.deleted_defaults ENABLE ROW LEVEL SECURITY;

--

-- Name: dismissals; Type: ROW SECURITY; Schema: public; Owner: -
--

ALTER TABLE public.dismissals ENABLE ROW LEVEL SECURITY;

--

-- Name: environment_profiles; Type: ROW SECURITY; Schema: public; Owner: -
--

ALTER TABLE public.environment_profiles ENABLE ROW LEVEL SECURITY;

--

-- Name: error_events; Type: ROW SECURITY; Schema: public; Owner: -
--

ALTER TABLE public.error_events ENABLE ROW LEVEL SECURITY;

--

-- Name: error_forwarder_configs; Type: ROW SECURITY; Schema: public; Owner: -
--

ALTER TABLE public.error_forwarder_configs ENABLE ROW LEVEL SECURITY;

--

-- Name: error_groups; Type: ROW SECURITY; Schema: public; Owner: -
--

ALTER TABLE public.error_groups ENABLE ROW LEVEL SECURITY;

--

-- Name: error_notification_rules; Type: ROW SECURITY; Schema: public; Owner: -
--

ALTER TABLE public.error_notification_rules ENABLE ROW LEVEL SECURITY;

--

-- Name: eval_cases; Type: ROW SECURITY; Schema: public; Owner: -
--

ALTER TABLE public.eval_cases ENABLE ROW LEVEL SECURITY;

--

-- Name: eval_datasets; Type: ROW SECURITY; Schema: public; Owner: -
--

ALTER TABLE public.eval_datasets ENABLE ROW LEVEL SECURITY;

--

-- Name: eval_results; Type: ROW SECURITY; Schema: public; Owner: -
--

ALTER TABLE public.eval_results ENABLE ROW LEVEL SECURITY;

--

-- Name: eval_suites; Type: ROW SECURITY; Schema: public; Owner: -
--

ALTER TABLE public.eval_suites ENABLE ROW LEVEL SECURITY;

--

-- Name: evals; Type: ROW SECURITY; Schema: public; Owner: -
--

ALTER TABLE public.evals ENABLE ROW LEVEL SECURITY;

--

-- Name: evidence; Type: ROW SECURITY; Schema: public; Owner: -
--

ALTER TABLE public.evidence ENABLE ROW LEVEL SECURITY;

--

-- Name: evidence_redaction_rules; Type: ROW SECURITY; Schema: public; Owner: -
--

ALTER TABLE public.evidence_redaction_rules ENABLE ROW LEVEL SECURITY;

--

-- Name: external_agents; Type: ROW SECURITY; Schema: public; Owner: -
--

ALTER TABLE public.external_agents ENABLE ROW LEVEL SECURITY;

--

-- Name: feedback_records; Type: ROW SECURITY; Schema: public; Owner: -
--

ALTER TABLE public.feedback_records ENABLE ROW LEVEL SECURITY;

--

-- Name: hitl_claims; Type: ROW SECURITY; Schema: public; Owner: -
--

ALTER TABLE public.hitl_claims ENABLE ROW LEVEL SECURITY;

--

-- Name: journeys; Type: ROW SECURITY; Schema: public; Owner: -
--

ALTER TABLE public.journeys ENABLE ROW LEVEL SECURITY;

--

-- Name: library_primitives; Type: ROW SECURITY; Schema: public; Owner: -
--

ALTER TABLE public.library_primitives ENABLE ROW LEVEL SECURITY;

--

-- Name: lifecycle_map_stages; Type: ROW SECURITY; Schema: public; Owner: -
--

ALTER TABLE public.lifecycle_map_stages ENABLE ROW LEVEL SECURITY;

--

-- Name: lifecycle_maps; Type: ROW SECURITY; Schema: public; Owner: -
--

ALTER TABLE public.lifecycle_maps ENABLE ROW LEVEL SECURITY;

--

-- Name: mcp_servers; Type: ROW SECURITY; Schema: public; Owner: -
--

ALTER TABLE public.mcp_servers ENABLE ROW LEVEL SECURITY;

--

-- Name: mcp_setup_tokens; Type: ROW SECURITY; Schema: public; Owner: -
--

ALTER TABLE public.mcp_setup_tokens ENABLE ROW LEVEL SECURITY;

--

-- Name: metrics_staging; Type: ROW SECURITY; Schema: public; Owner: -
--

ALTER TABLE public.metrics_staging ENABLE ROW LEVEL SECURITY;

--

-- Name: model_backends; Type: ROW SECURITY; Schema: public; Owner: -
--

ALTER TABLE public.model_backends ENABLE ROW LEVEL SECURITY;

--

-- Name: modulo_journey_facts; Type: ROW SECURITY; Schema: public; Owner: -
--

ALTER TABLE public.modulo_journey_facts ENABLE ROW LEVEL SECURITY;

--

-- Name: node_categories; Type: ROW SECURITY; Schema: public; Owner: -
--

ALTER TABLE public.node_categories ENABLE ROW LEVEL SECURITY;

--

-- Name: node_observations; Type: ROW SECURITY; Schema: public; Owner: -
--

ALTER TABLE public.node_observations ENABLE ROW LEVEL SECURITY;

--

-- Name: nodes; Type: ROW SECURITY; Schema: public; Owner: -
--

ALTER TABLE public.nodes ENABLE ROW LEVEL SECURITY;

--

-- Name: notification_delivery_log; Type: ROW SECURITY; Schema: public; Owner: -
--

ALTER TABLE public.notification_delivery_log ENABLE ROW LEVEL SECURITY;

--

-- Name: notification_endpoints; Type: ROW SECURITY; Schema: public; Owner: -
--

ALTER TABLE public.notification_endpoints ENABLE ROW LEVEL SECURITY;

--

-- Name: notification_preferences; Type: ROW SECURITY; Schema: public; Owner: -
--

ALTER TABLE public.notification_preferences ENABLE ROW LEVEL SECURITY;

--

-- Name: notifications; Type: ROW SECURITY; Schema: public; Owner: -
--

ALTER TABLE public.notifications ENABLE ROW LEVEL SECURITY;

--

-- Name: oauth_authorization_codes; Type: ROW SECURITY; Schema: public; Owner: -
--

ALTER TABLE public.oauth_authorization_codes ENABLE ROW LEVEL SECURITY;

--

-- Name: oauth_clients; Type: ROW SECURITY; Schema: public; Owner: -
--

ALTER TABLE public.oauth_clients ENABLE ROW LEVEL SECURITY;

--

-- Name: oauth_consent_states; Type: ROW SECURITY; Schema: public; Owner: -
--

ALTER TABLE public.oauth_consent_states ENABLE ROW LEVEL SECURITY;

--

-- Name: oauth_token_families; Type: ROW SECURITY; Schema: public; Owner: -
--

ALTER TABLE public.oauth_token_families ENABLE ROW LEVEL SECURITY;

--

-- Name: onboarding_progress; Type: ROW SECURITY; Schema: public; Owner: -
--

ALTER TABLE public.onboarding_progress ENABLE ROW LEVEL SECURITY;

--

-- Name: org_api_keys; Type: ROW SECURITY; Schema: public; Owner: -
--

ALTER TABLE public.org_api_keys ENABLE ROW LEVEL SECURITY;

--

-- Name: org_daily_run_counts; Type: ROW SECURITY; Schema: public; Owner: -
--

ALTER TABLE public.org_daily_run_counts ENABLE ROW LEVEL SECURITY;

--

-- Name: org_memberships; Type: ROW SECURITY; Schema: public; Owner: -
--

ALTER TABLE public.org_memberships ENABLE ROW LEVEL SECURITY;

--

-- Name: org_mint_budget_usage; Type: ROW SECURITY; Schema: public; Owner: -
--

ALTER TABLE public.org_mint_budget_usage ENABLE ROW LEVEL SECURITY;

--

-- Name: organisation_cost_state; Type: ROW SECURITY; Schema: public; Owner: -
--

ALTER TABLE public.organisation_cost_state ENABLE ROW LEVEL SECURITY;

--

-- Name: parameter_schemas; Type: ROW SECURITY; Schema: public; Owner: -
--

ALTER TABLE public.parameter_schemas ENABLE ROW LEVEL SECURITY;

--

-- Name: parameter_sets; Type: ROW SECURITY; Schema: public; Owner: -
--

ALTER TABLE public.parameter_sets ENABLE ROW LEVEL SECURITY;

--

-- Name: pipeline_edges; Type: ROW SECURITY; Schema: public; Owner: -
--

ALTER TABLE public.pipeline_edges ENABLE ROW LEVEL SECURITY;

--

-- Name: pipeline_folders; Type: ROW SECURITY; Schema: public; Owner: -
--

ALTER TABLE public.pipeline_folders ENABLE ROW LEVEL SECURITY;

--

-- Name: pipeline_snapshots; Type: ROW SECURITY; Schema: public; Owner: -
--

ALTER TABLE public.pipeline_snapshots ENABLE ROW LEVEL SECURITY;

--

-- Name: pipelines; Type: ROW SECURITY; Schema: public; Owner: -
--

ALTER TABLE public.pipelines ENABLE ROW LEVEL SECURITY;

--

-- Name: policy_gate_decisions; Type: ROW SECURITY; Schema: public; Owner: -
--

ALTER TABLE public.policy_gate_decisions ENABLE ROW LEVEL SECURITY;

--

-- Name: policy_gates; Type: ROW SECURITY; Schema: public; Owner: -
--

ALTER TABLE public.policy_gates ENABLE ROW LEVEL SECURITY;

--

-- Name: primitive_abuse_reports; Type: ROW SECURITY; Schema: public; Owner: -
--

ALTER TABLE public.primitive_abuse_reports ENABLE ROW LEVEL SECURITY;

--

-- Name: primitive_ratings; Type: ROW SECURITY; Schema: public; Owner: -
--

ALTER TABLE public.primitive_ratings ENABLE ROW LEVEL SECURITY;

--

-- Name: publishers; Type: ROW SECURITY; Schema: public; Owner: -
--

ALTER TABLE public.publishers ENABLE ROW LEVEL SECURITY;

--

-- Name: run_attempts; Type: ROW SECURITY; Schema: public; Owner: -
--

ALTER TABLE public.run_attempts ENABLE ROW LEVEL SECURITY;

--

-- Name: run_daily_facts; Type: ROW SECURITY; Schema: public; Owner: -
--

ALTER TABLE public.run_daily_facts ENABLE ROW LEVEL SECURITY;

--

-- Name: run_evidence; Type: ROW SECURITY; Schema: public; Owner: -
--

ALTER TABLE public.run_evidence ENABLE ROW LEVEL SECURITY;

--

-- Name: run_node_outputs; Type: ROW SECURITY; Schema: public; Owner: -
--

ALTER TABLE public.run_node_outputs ENABLE ROW LEVEL SECURITY;

--

-- Name: run_number_counters; Type: ROW SECURITY; Schema: public; Owner: -
--

ALTER TABLE public.run_number_counters ENABLE ROW LEVEL SECURITY;

--

-- Name: runner_probe_cache; Type: ROW SECURITY; Schema: public; Owner: -
--

ALTER TABLE public.runner_probe_cache ENABLE ROW LEVEL SECURITY;

--

-- Name: runs; Type: ROW SECURITY; Schema: public; Owner: -
--

ALTER TABLE public.runs ENABLE ROW LEVEL SECURITY;

--

-- Name: saved_views; Type: ROW SECURITY; Schema: public; Owner: -
--

ALTER TABLE public.saved_views ENABLE ROW LEVEL SECURITY;

--

-- Name: scheduled_reports; Type: ROW SECURITY; Schema: public; Owner: -
--

ALTER TABLE public.scheduled_reports ENABLE ROW LEVEL SECURITY;

--

-- Name: schema_folders; Type: ROW SECURITY; Schema: public; Owner: -
--

ALTER TABLE public.schema_folders ENABLE ROW LEVEL SECURITY;

--

-- Name: schema_versions; Type: ROW SECURITY; Schema: public; Owner: -
--

ALTER TABLE public.schema_versions ENABLE ROW LEVEL SECURITY;

--

-- Name: schemas; Type: ROW SECURITY; Schema: public; Owner: -
--

ALTER TABLE public.schemas ENABLE ROW LEVEL SECURITY;

--

-- Name: secrets; Type: ROW SECURITY; Schema: public; Owner: -
--

ALTER TABLE public.secrets ENABLE ROW LEVEL SECURITY;

--

-- Name: snapshot_schema_pins; Type: ROW SECURITY; Schema: public; Owner: -
--

ALTER TABLE public.snapshot_schema_pins ENABLE ROW LEVEL SECURITY;

--

-- Name: spend_anomalies; Type: ROW SECURITY; Schema: public; Owner: -
--

ALTER TABLE public.spend_anomalies ENABLE ROW LEVEL SECURITY;

--

-- Name: sso_providers; Type: ROW SECURITY; Schema: public; Owner: -
--

ALTER TABLE public.sso_providers ENABLE ROW LEVEL SECURITY;

--

-- Name: suite_runs; Type: ROW SECURITY; Schema: public; Owner: -
--

ALTER TABLE public.suite_runs ENABLE ROW LEVEL SECURITY;

--

-- Name: system_map_attribution_bindings; Type: ROW SECURITY; Schema: public; Owner: -
--

ALTER TABLE public.system_map_attribution_bindings ENABLE ROW LEVEL SECURITY;

--

-- Name: system_map_loop_admission_attempts; Type: ROW SECURITY; Schema: public; Owner: -
--

ALTER TABLE public.system_map_loop_admission_attempts ENABLE ROW LEVEL SECURITY;

--

-- Name: system_map_loop_configs; Type: ROW SECURITY; Schema: public; Owner: -
--

ALTER TABLE public.system_map_loop_configs ENABLE ROW LEVEL SECURITY;

--

-- Name: system_map_loop_reservations; Type: ROW SECURITY; Schema: public; Owner: -
--

ALTER TABLE public.system_map_loop_reservations ENABLE ROW LEVEL SECURITY;

--

-- Name: system_map_nodes; Type: ROW SECURITY; Schema: public; Owner: -
--

ALTER TABLE public.system_map_nodes ENABLE ROW LEVEL SECURITY;

--

-- Name: system_map_relationships; Type: ROW SECURITY; Schema: public; Owner: -
--

ALTER TABLE public.system_map_relationships ENABLE ROW LEVEL SECURITY;

--

-- Name: system_map_revisions; Type: ROW SECURITY; Schema: public; Owner: -
--

ALTER TABLE public.system_map_revisions ENABLE ROW LEVEL SECURITY;

--

-- Name: system_maps; Type: ROW SECURITY; Schema: public; Owner: -
--

ALTER TABLE public.system_maps ENABLE ROW LEVEL SECURITY;

--

-- Name: team_memberships; Type: ROW SECURITY; Schema: public; Owner: -
--

ALTER TABLE public.team_memberships ENABLE ROW LEVEL SECURITY;

--

-- Name: teams; Type: ROW SECURITY; Schema: public; Owner: -
--

ALTER TABLE public.teams ENABLE ROW LEVEL SECURITY;

--

-- Name: token_families; Type: ROW SECURITY; Schema: public; Owner: -
--

ALTER TABLE public.token_families ENABLE ROW LEVEL SECURITY;

--

-- Name: trigger_event_findings; Type: ROW SECURITY; Schema: public; Owner: -
--

ALTER TABLE public.trigger_event_findings ENABLE ROW LEVEL SECURITY;

--

-- Name: trigger_events; Type: ROW SECURITY; Schema: public; Owner: -
--

ALTER TABLE public.trigger_events ENABLE ROW LEVEL SECURITY;

--

-- Name: triggers; Type: ROW SECURITY; Schema: public; Owner: -
--

ALTER TABLE public.triggers ENABLE ROW LEVEL SECURITY;

--

-- Name: variant_batch_state; Type: ROW SECURITY; Schema: public; Owner: -
--

ALTER TABLE public.variant_batch_state ENABLE ROW LEVEL SECURITY;

--

-- Name: variant_groups; Type: ROW SECURITY; Schema: public; Owner: -
--

ALTER TABLE public.variant_groups ENABLE ROW LEVEL SECURITY;

--

-- Name: web_vital_events; Type: ROW SECURITY; Schema: public; Owner: -
--

ALTER TABLE public.web_vital_events ENABLE ROW LEVEL SECURITY;

--

-- Name: webhook_dedup_hashes; Type: ROW SECURITY; Schema: public; Owner: -
--

ALTER TABLE public.webhook_dedup_hashes ENABLE ROW LEVEL SECURITY;

--

-- Name: webhook_payloads; Type: ROW SECURITY; Schema: public; Owner: -
--

ALTER TABLE public.webhook_payloads ENABLE ROW LEVEL SECURITY;

--
-- PostgreSQL database dump complete
--

ALTER TABLE ONLY public.agent_runner_bindings FORCE ROW LEVEL SECURITY;

ALTER TABLE ONLY public.agents FORCE ROW LEVEL SECURITY;

ALTER TABLE ONLY public.collection_install FORCE ROW LEVEL SECURITY;

ALTER TABLE ONLY public.collection_install_entity FORCE ROW LEVEL SECURITY;

ALTER TABLE ONLY public.connector_profiles FORCE ROW LEVEL SECURITY;

ALTER TABLE ONLY public.dismissals FORCE ROW LEVEL SECURITY;

ALTER TABLE ONLY public.eval_cases FORCE ROW LEVEL SECURITY;

ALTER TABLE ONLY public.eval_datasets FORCE ROW LEVEL SECURITY;

ALTER TABLE ONLY public.eval_suites FORCE ROW LEVEL SECURITY;

ALTER TABLE ONLY public.evals FORCE ROW LEVEL SECURITY;

ALTER TABLE ONLY public.evidence FORCE ROW LEVEL SECURITY;

ALTER TABLE ONLY public.evidence_redaction_rules FORCE ROW LEVEL SECURITY;

ALTER TABLE ONLY public.external_agents FORCE ROW LEVEL SECURITY;

ALTER TABLE ONLY public.mcp_servers FORCE ROW LEVEL SECURITY;

ALTER TABLE ONLY public.notification_preferences FORCE ROW LEVEL SECURITY;

ALTER TABLE ONLY public.org_mint_budget_usage FORCE ROW LEVEL SECURITY;

ALTER TABLE ONLY public.organisation_cost_state FORCE ROW LEVEL SECURITY;

ALTER TABLE ONLY public.policy_gate_decisions FORCE ROW LEVEL SECURITY;

ALTER TABLE ONLY public.policy_gates FORCE ROW LEVEL SECURITY;

ALTER TABLE ONLY public.run_attempts FORCE ROW LEVEL SECURITY;

ALTER TABLE ONLY public.run_evidence FORCE ROW LEVEL SECURITY;

ALTER TABLE ONLY public.run_node_outputs FORCE ROW LEVEL SECURITY;

ALTER TABLE ONLY public.runner_probe_cache FORCE ROW LEVEL SECURITY;

ALTER TABLE ONLY public.suite_runs FORCE ROW LEVEL SECURITY;

ALTER TABLE ONLY public.system_map_attribution_bindings FORCE ROW LEVEL SECURITY;

ALTER TABLE ONLY public.system_map_loop_admission_attempts FORCE ROW LEVEL SECURITY;

ALTER TABLE ONLY public.system_map_loop_configs FORCE ROW LEVEL SECURITY;

ALTER TABLE ONLY public.system_map_loop_reservations FORCE ROW LEVEL SECURITY;

ALTER TABLE ONLY public.system_map_nodes FORCE ROW LEVEL SECURITY;

ALTER TABLE ONLY public.system_map_relationships FORCE ROW LEVEL SECURITY;

ALTER TABLE ONLY public.system_map_revisions FORCE ROW LEVEL SECURITY;

ALTER TABLE ONLY public.system_maps FORCE ROW LEVEL SECURITY;

ALTER TABLE ONLY public.trigger_event_findings FORCE ROW LEVEL SECURITY;

ALTER TABLE ONLY public.variant_batch_state FORCE ROW LEVEL SECURITY;

-- Name: agent_runner_bindings rls_org_isolation; Type: POLICY; Schema: public; Owner: -
--

CREATE POLICY rls_org_isolation ON public.agent_runner_bindings USING ((organisation_id = (NULLIF(current_setting('app.organisation_id'::text, true), ''::text))::uuid));


--

-- Name: agents rls_org_isolation; Type: POLICY; Schema: public; Owner: -
--

CREATE POLICY rls_org_isolation ON public.agents USING ((organisation_id = (NULLIF(current_setting('app.organisation_id'::text, true), ''::text))::uuid));


--

-- Name: assistant_context_sources rls_org_isolation; Type: POLICY; Schema: public; Owner: -
--

CREATE POLICY rls_org_isolation ON public.assistant_context_sources USING (((organisation_id = (NULLIF(current_setting('app.organisation_id'::text, true), ''::text))::uuid) OR (organisation_id IS NULL)));


--

-- Name: assistant_skills rls_org_isolation; Type: POLICY; Schema: public; Owner: -
--

CREATE POLICY rls_org_isolation ON public.assistant_skills USING (((organisation_id = (NULLIF(current_setting('app.organisation_id'::text, true), ''::text))::uuid) OR (organisation_id IS NULL)));


--

-- Name: audit_chain_heads rls_org_isolation; Type: POLICY; Schema: public; Owner: -
--

CREATE POLICY rls_org_isolation ON public.audit_chain_heads USING ((organisation_id = (NULLIF(current_setting('app.organisation_id'::text, true), ''::text))::uuid));


--

-- Name: audit_events rls_org_isolation; Type: POLICY; Schema: public; Owner: -
--

CREATE POLICY rls_org_isolation ON public.audit_events USING ((organisation_id = (NULLIF(current_setting('app.organisation_id'::text, true), ''::text))::uuid));


--

-- Name: chat_messages rls_org_isolation; Type: POLICY; Schema: public; Owner: -
--

CREATE POLICY rls_org_isolation ON public.chat_messages USING ((organisation_id = (NULLIF(current_setting('app.organisation_id'::text, true), ''::text))::uuid));


--

-- Name: chat_sessions rls_org_isolation; Type: POLICY; Schema: public; Owner: -
--

CREATE POLICY rls_org_isolation ON public.chat_sessions USING ((organisation_id = (NULLIF(current_setting('app.organisation_id'::text, true), ''::text))::uuid));


--

-- Name: collection_install rls_org_isolation; Type: POLICY; Schema: public; Owner: -
--

CREATE POLICY rls_org_isolation ON public.collection_install USING ((organisation_id = (NULLIF(current_setting('app.organisation_id'::text, true), ''::text))::uuid));


--

-- Name: collection_install_entity rls_org_isolation; Type: POLICY; Schema: public; Owner: -
--

CREATE POLICY rls_org_isolation ON public.collection_install_entity USING ((install_id IN ( SELECT collection_install.install_id
   FROM public.collection_install
  WHERE (collection_install.organisation_id = (NULLIF(current_setting('app.organisation_id'::text, true), ''::text))::uuid))));


--

-- Name: composite_templates rls_org_isolation; Type: POLICY; Schema: public; Owner: -
--

CREATE POLICY rls_org_isolation ON public.composite_templates USING ((organisation_id = (NULLIF(current_setting('app.organisation_id'::text, true), ''::text))::uuid));


--

-- Name: connector_profiles rls_org_isolation; Type: POLICY; Schema: public; Owner: -
--

CREATE POLICY rls_org_isolation ON public.connector_profiles USING ((organisation_id = (NULLIF(current_setting('app.organisation_id'::text, true), ''::text))::uuid));


--

-- Name: cost_components rls_org_isolation; Type: POLICY; Schema: public; Owner: -
--

CREATE POLICY rls_org_isolation ON public.cost_components USING ((organisation_id = (NULLIF(current_setting('app.organisation_id'::text, true), ''::text))::uuid));


--

-- Name: deleted_defaults rls_org_isolation; Type: POLICY; Schema: public; Owner: -
--

CREATE POLICY rls_org_isolation ON public.deleted_defaults USING ((organisation_id = (NULLIF(current_setting('app.organisation_id'::text, true), ''::text))::uuid));


--

-- Name: dismissals rls_org_isolation; Type: POLICY; Schema: public; Owner: -
--

CREATE POLICY rls_org_isolation ON public.dismissals USING ((organisation_id = (NULLIF(current_setting('app.organisation_id'::text, true), ''::text))::uuid));


--

-- Name: error_events rls_org_isolation; Type: POLICY; Schema: public; Owner: -
--

CREATE POLICY rls_org_isolation ON public.error_events USING ((organisation_id = (NULLIF(current_setting('app.organisation_id'::text, true), ''::text))::uuid));


--

-- Name: error_forwarder_configs rls_org_isolation; Type: POLICY; Schema: public; Owner: -
--

CREATE POLICY rls_org_isolation ON public.error_forwarder_configs USING ((organisation_id = (NULLIF(current_setting('app.organisation_id'::text, true), ''::text))::uuid));


--

-- Name: error_groups rls_org_isolation; Type: POLICY; Schema: public; Owner: -
--

CREATE POLICY rls_org_isolation ON public.error_groups USING ((organisation_id = (NULLIF(current_setting('app.organisation_id'::text, true), ''::text))::uuid));


--

-- Name: error_notification_rules rls_org_isolation; Type: POLICY; Schema: public; Owner: -
--

CREATE POLICY rls_org_isolation ON public.error_notification_rules USING ((organisation_id = (NULLIF(current_setting('app.organisation_id'::text, true), ''::text))::uuid));


--

-- Name: eval_cases rls_org_isolation; Type: POLICY; Schema: public; Owner: -
--

CREATE POLICY rls_org_isolation ON public.eval_cases USING ((organisation_id = (NULLIF(current_setting('app.organisation_id'::text, true), ''::text))::uuid));


--

-- Name: eval_results rls_org_isolation; Type: POLICY; Schema: public; Owner: -
--

CREATE POLICY rls_org_isolation ON public.eval_results USING ((organisation_id = (NULLIF(current_setting('app.organisation_id'::text, true), ''::text))::uuid));


--

-- Name: evals rls_org_isolation; Type: POLICY; Schema: public; Owner: -
--

CREATE POLICY rls_org_isolation ON public.evals USING ((organisation_id = (NULLIF(current_setting('app.organisation_id'::text, true), ''::text))::uuid));


--

-- Name: evidence rls_org_isolation; Type: POLICY; Schema: public; Owner: -
--

CREATE POLICY rls_org_isolation ON public.evidence USING ((organisation_id = (NULLIF(current_setting('app.organisation_id'::text, true), ''::text))::uuid));


--

-- Name: evidence_redaction_rules rls_org_isolation; Type: POLICY; Schema: public; Owner: -
--

CREATE POLICY rls_org_isolation ON public.evidence_redaction_rules USING ((organisation_id = (NULLIF(current_setting('app.organisation_id'::text, true), ''::text))::uuid));


--

-- Name: feedback_records rls_org_isolation; Type: POLICY; Schema: public; Owner: -
--

CREATE POLICY rls_org_isolation ON public.feedback_records USING ((organisation_id = (NULLIF(current_setting('app.organisation_id'::text, true), ''::text))::uuid));


--

-- Name: hitl_claims rls_org_isolation; Type: POLICY; Schema: public; Owner: -
--

CREATE POLICY rls_org_isolation ON public.hitl_claims USING ((organisation_id = (NULLIF(current_setting('app.organisation_id'::text, true), ''::text))::uuid));


--

-- Name: journeys rls_org_isolation; Type: POLICY; Schema: public; Owner: -
--

CREATE POLICY rls_org_isolation ON public.journeys USING ((organisation_id = (NULLIF(current_setting('app.organisation_id'::text, true), ''::text))::uuid));


--

-- Name: lifecycle_map_stages rls_org_isolation; Type: POLICY; Schema: public; Owner: -
--

CREATE POLICY rls_org_isolation ON public.lifecycle_map_stages USING ((organisation_id = (NULLIF(current_setting('app.organisation_id'::text, true), ''::text))::uuid));


--

-- Name: mcp_setup_tokens rls_org_isolation; Type: POLICY; Schema: public; Owner: -
--

CREATE POLICY rls_org_isolation ON public.mcp_setup_tokens USING ((organisation_id = (NULLIF(current_setting('app.organisation_id'::text, true), ''::text))::uuid));


--

-- Name: metrics_staging rls_org_isolation; Type: POLICY; Schema: public; Owner: -
--

CREATE POLICY rls_org_isolation ON public.metrics_staging USING ((organisation_id = (NULLIF(current_setting('app.organisation_id'::text, true), ''::text))::uuid));


--

-- Name: modulo_journey_facts rls_org_isolation; Type: POLICY; Schema: public; Owner: -
--

CREATE POLICY rls_org_isolation ON public.modulo_journey_facts USING ((organisation_id = (NULLIF(current_setting('app.organisation_id'::text, true), ''::text))::uuid));


--

-- Name: node_categories rls_org_isolation; Type: POLICY; Schema: public; Owner: -
--

CREATE POLICY rls_org_isolation ON public.node_categories USING ((organisation_id = (NULLIF(current_setting('app.organisation_id'::text, true), ''::text))::uuid));


--

-- Name: node_observations rls_org_isolation; Type: POLICY; Schema: public; Owner: -
--

CREATE POLICY rls_org_isolation ON public.node_observations USING ((organisation_id = (NULLIF(current_setting('app.organisation_id'::text, true), ''::text))::uuid));


--

-- Name: nodes rls_org_isolation; Type: POLICY; Schema: public; Owner: -
--

CREATE POLICY rls_org_isolation ON public.nodes USING ((organisation_id = (NULLIF(current_setting('app.organisation_id'::text, true), ''::text))::uuid));


--

-- Name: notification_delivery_log rls_org_isolation; Type: POLICY; Schema: public; Owner: -
--

CREATE POLICY rls_org_isolation ON public.notification_delivery_log USING ((organisation_id = (NULLIF(current_setting('app.organisation_id'::text, true), ''::text))::uuid));


--

-- Name: notification_endpoints rls_org_isolation; Type: POLICY; Schema: public; Owner: -
--

CREATE POLICY rls_org_isolation ON public.notification_endpoints USING ((organisation_id = (NULLIF(current_setting('app.organisation_id'::text, true), ''::text))::uuid));


--

-- Name: notification_preferences rls_org_isolation; Type: POLICY; Schema: public; Owner: -
--

CREATE POLICY rls_org_isolation ON public.notification_preferences FOR SELECT USING ((organisation_id = (NULLIF(current_setting('app.organisation_id'::text, true), ''::text))::uuid));


--

-- Name: notifications rls_org_isolation; Type: POLICY; Schema: public; Owner: -
--

CREATE POLICY rls_org_isolation ON public.notifications USING ((organisation_id = (NULLIF(current_setting('app.organisation_id'::text, true), ''::text))::uuid));


--

-- Name: oauth_authorization_codes rls_org_isolation; Type: POLICY; Schema: public; Owner: -
--

CREATE POLICY rls_org_isolation ON public.oauth_authorization_codes USING ((organisation_id = (NULLIF(current_setting('app.organisation_id'::text, true), ''::text))::uuid));


--

-- Name: oauth_clients rls_org_isolation; Type: POLICY; Schema: public; Owner: -
--

CREATE POLICY rls_org_isolation ON public.oauth_clients USING (((organisation_id = (NULLIF(current_setting('app.organisation_id'::text, true), ''::text))::uuid) OR (NULLIF(current_setting('app.organisation_id'::text, true), ''::text) IS NULL)));


--

-- Name: oauth_consent_states rls_org_isolation; Type: POLICY; Schema: public; Owner: -
--

CREATE POLICY rls_org_isolation ON public.oauth_consent_states USING (((organisation_id = (NULLIF(current_setting('app.organisation_id'::text, true), ''::text))::uuid) OR (NULLIF(current_setting('app.organisation_id'::text, true), ''::text) IS NULL)));


--

-- Name: oauth_token_families rls_org_isolation; Type: POLICY; Schema: public; Owner: -
--

CREATE POLICY rls_org_isolation ON public.oauth_token_families USING ((organisation_id = (NULLIF(current_setting('app.organisation_id'::text, true), ''::text))::uuid));


--

-- Name: onboarding_progress rls_org_isolation; Type: POLICY; Schema: public; Owner: -
--

CREATE POLICY rls_org_isolation ON public.onboarding_progress USING ((organisation_id = (current_setting('app.organisation_id'::text))::uuid));


--

-- Name: org_api_keys rls_org_isolation; Type: POLICY; Schema: public; Owner: -
--

CREATE POLICY rls_org_isolation ON public.org_api_keys USING ((organisation_id = (NULLIF(current_setting('app.organisation_id'::text, true), ''::text))::uuid));


--

-- Name: org_daily_run_counts rls_org_isolation; Type: POLICY; Schema: public; Owner: -
--

CREATE POLICY rls_org_isolation ON public.org_daily_run_counts USING ((organisation_id = (NULLIF(current_setting('app.organisation_id'::text, true), ''::text))::uuid));


--

-- Name: org_memberships rls_org_isolation; Type: POLICY; Schema: public; Owner: -
--

CREATE POLICY rls_org_isolation ON public.org_memberships USING (((organisation_id = (NULLIF(current_setting('app.organisation_id'::text, true), ''::text))::uuid) OR (NULLIF(current_setting('app.organisation_id'::text, true), ''::text) IS NULL)));


--

-- Name: org_mint_budget_usage rls_org_isolation; Type: POLICY; Schema: public; Owner: -
--

CREATE POLICY rls_org_isolation ON public.org_mint_budget_usage USING ((organisation_id = (NULLIF(current_setting('app.organisation_id'::text, true), ''::text))::uuid));


--

-- Name: organisation_cost_state rls_org_isolation; Type: POLICY; Schema: public; Owner: -
--

CREATE POLICY rls_org_isolation ON public.organisation_cost_state USING ((org_id = (NULLIF(current_setting('app.organisation_id'::text, true), ''::text))::uuid));


--

-- Name: parameter_schemas rls_org_isolation; Type: POLICY; Schema: public; Owner: -
--

CREATE POLICY rls_org_isolation ON public.parameter_schemas USING ((organisation_id = (NULLIF(current_setting('app.organisation_id'::text, true), ''::text))::uuid));


--

-- Name: parameter_sets rls_org_isolation; Type: POLICY; Schema: public; Owner: -
--

CREATE POLICY rls_org_isolation ON public.parameter_sets USING ((organisation_id = (NULLIF(current_setting('app.organisation_id'::text, true), ''::text))::uuid));


--

-- Name: pipeline_edges rls_org_isolation; Type: POLICY; Schema: public; Owner: -
--

CREATE POLICY rls_org_isolation ON public.pipeline_edges USING ((organisation_id = (NULLIF(current_setting('app.organisation_id'::text, true), ''::text))::uuid));


--

-- Name: pipeline_folders rls_org_isolation; Type: POLICY; Schema: public; Owner: -
--

CREATE POLICY rls_org_isolation ON public.pipeline_folders USING ((organisation_id = (NULLIF(current_setting('app.organisation_id'::text, true), ''::text))::uuid));


--

-- Name: pipeline_snapshots rls_org_isolation; Type: POLICY; Schema: public; Owner: -
--

CREATE POLICY rls_org_isolation ON public.pipeline_snapshots USING ((organisation_id = (NULLIF(current_setting('app.organisation_id'::text, true), ''::text))::uuid));


--

-- Name: policy_gate_decisions rls_org_isolation; Type: POLICY; Schema: public; Owner: -
--

CREATE POLICY rls_org_isolation ON public.policy_gate_decisions USING ((organisation_id = (NULLIF(current_setting('app.organisation_id'::text, true), ''::text))::uuid));


--

-- Name: policy_gates rls_org_isolation; Type: POLICY; Schema: public; Owner: -
--

CREATE POLICY rls_org_isolation ON public.policy_gates USING ((organisation_id = (NULLIF(current_setting('app.organisation_id'::text, true), ''::text))::uuid));


--

-- Name: primitive_abuse_reports rls_org_isolation; Type: POLICY; Schema: public; Owner: -
--

CREATE POLICY rls_org_isolation ON public.primitive_abuse_reports USING ((organisation_id = (NULLIF(current_setting('app.organisation_id'::text, true), ''::text))::uuid));


--

-- Name: primitive_ratings rls_org_isolation; Type: POLICY; Schema: public; Owner: -
--

CREATE POLICY rls_org_isolation ON public.primitive_ratings USING ((organisation_id = (NULLIF(current_setting('app.organisation_id'::text, true), ''::text))::uuid));


--

-- Name: publishers rls_org_isolation; Type: POLICY; Schema: public; Owner: -
--

CREATE POLICY rls_org_isolation ON public.publishers USING ((organisation_id = (NULLIF(current_setting('app.organisation_id'::text, true), ''::text))::uuid));


--

-- Name: run_attempts rls_org_isolation; Type: POLICY; Schema: public; Owner: -
--

CREATE POLICY rls_org_isolation ON public.run_attempts USING ((organisation_id = (NULLIF(current_setting('app.organisation_id'::text, true), ''::text))::uuid));


--

-- Name: run_daily_facts rls_org_isolation; Type: POLICY; Schema: public; Owner: -
--

CREATE POLICY rls_org_isolation ON public.run_daily_facts USING ((organisation_id = (NULLIF(current_setting('app.organisation_id'::text, true), ''::text))::uuid));


--

-- Name: run_evidence rls_org_isolation; Type: POLICY; Schema: public; Owner: -
--

CREATE POLICY rls_org_isolation ON public.run_evidence USING ((organisation_id = (NULLIF(current_setting('app.organisation_id'::text, true), ''::text))::uuid));


--

-- Name: run_node_outputs rls_org_isolation; Type: POLICY; Schema: public; Owner: -
--

CREATE POLICY rls_org_isolation ON public.run_node_outputs USING ((organisation_id = (NULLIF(current_setting('app.organisation_id'::text, true), ''::text))::uuid));


--

-- Name: run_number_counters rls_org_isolation; Type: POLICY; Schema: public; Owner: -
--

CREATE POLICY rls_org_isolation ON public.run_number_counters USING ((organisation_id = (NULLIF(current_setting('app.organisation_id'::text, true), ''::text))::uuid));


--

-- Name: runner_probe_cache rls_org_isolation; Type: POLICY; Schema: public; Owner: -
--

CREATE POLICY rls_org_isolation ON public.runner_probe_cache USING ((organisation_id = (NULLIF(current_setting('app.organisation_id'::text, true), ''::text))::uuid));


--

-- Name: runs rls_org_isolation; Type: POLICY; Schema: public; Owner: -
--

CREATE POLICY rls_org_isolation ON public.runs USING ((organisation_id = (NULLIF(current_setting('app.organisation_id'::text, true), ''::text))::uuid));


--

-- Name: saved_views rls_org_isolation; Type: POLICY; Schema: public; Owner: -
--

CREATE POLICY rls_org_isolation ON public.saved_views USING ((organisation_id = (NULLIF(current_setting('app.organisation_id'::text, true), ''::text))::uuid));


--

-- Name: scheduled_reports rls_org_isolation; Type: POLICY; Schema: public; Owner: -
--

CREATE POLICY rls_org_isolation ON public.scheduled_reports USING ((organisation_id = (NULLIF(current_setting('app.organisation_id'::text, true), ''::text))::uuid));


--

-- Name: schema_folders rls_org_isolation; Type: POLICY; Schema: public; Owner: -
--

CREATE POLICY rls_org_isolation ON public.schema_folders USING ((organisation_id = (NULLIF(current_setting('app.organisation_id'::text, true), ''::text))::uuid));


--

-- Name: schema_versions rls_org_isolation; Type: POLICY; Schema: public; Owner: -
--

CREATE POLICY rls_org_isolation ON public.schema_versions USING ((organisation_id = (NULLIF(current_setting('app.organisation_id'::text, true), ''::text))::uuid));


--

-- Name: schemas rls_org_isolation; Type: POLICY; Schema: public; Owner: -
--

CREATE POLICY rls_org_isolation ON public.schemas USING ((organisation_id = (NULLIF(current_setting('app.organisation_id'::text, true), ''::text))::uuid));


--

-- Name: secrets rls_org_isolation; Type: POLICY; Schema: public; Owner: -
--

CREATE POLICY rls_org_isolation ON public.secrets USING ((organisation_id = (NULLIF(current_setting('app.organisation_id'::text, true), ''::text))::uuid));


--

-- Name: snapshot_schema_pins rls_org_isolation; Type: POLICY; Schema: public; Owner: -
--

CREATE POLICY rls_org_isolation ON public.snapshot_schema_pins USING ((organisation_id = (NULLIF(current_setting('app.organisation_id'::text, true), ''::text))::uuid));


--

-- Name: spend_anomalies rls_org_isolation; Type: POLICY; Schema: public; Owner: -
--

CREATE POLICY rls_org_isolation ON public.spend_anomalies USING ((organisation_id = (NULLIF(current_setting('app.organisation_id'::text, true), ''::text))::uuid));


--

-- Name: sso_providers rls_org_isolation; Type: POLICY; Schema: public; Owner: -
--

CREATE POLICY rls_org_isolation ON public.sso_providers USING ((organisation_id = (NULLIF(current_setting('app.organisation_id'::text, true), ''::text))::uuid));


--

-- Name: suite_runs rls_org_isolation; Type: POLICY; Schema: public; Owner: -
--

CREATE POLICY rls_org_isolation ON public.suite_runs USING ((organisation_id = (NULLIF(current_setting('app.organisation_id'::text, true), ''::text))::uuid));


--

-- Name: system_map_attribution_bindings rls_org_isolation; Type: POLICY; Schema: public; Owner: -
--

CREATE POLICY rls_org_isolation ON public.system_map_attribution_bindings USING ((organisation_id = (NULLIF(current_setting('app.organisation_id'::text, true), ''::text))::uuid)) WITH CHECK ((organisation_id = (NULLIF(current_setting('app.organisation_id'::text, true), ''::text))::uuid));


--

-- Name: system_map_loop_admission_attempts rls_org_isolation; Type: POLICY; Schema: public; Owner: -
--

CREATE POLICY rls_org_isolation ON public.system_map_loop_admission_attempts USING ((organisation_id = (NULLIF(current_setting('app.organisation_id'::text, true), ''::text))::uuid)) WITH CHECK ((organisation_id = (NULLIF(current_setting('app.organisation_id'::text, true), ''::text))::uuid));


--

-- Name: system_map_loop_configs rls_org_isolation; Type: POLICY; Schema: public; Owner: -
--

CREATE POLICY rls_org_isolation ON public.system_map_loop_configs USING ((organisation_id = (NULLIF(current_setting('app.organisation_id'::text, true), ''::text))::uuid)) WITH CHECK ((organisation_id = (NULLIF(current_setting('app.organisation_id'::text, true), ''::text))::uuid));


--

-- Name: system_map_loop_reservations rls_org_isolation; Type: POLICY; Schema: public; Owner: -
--

CREATE POLICY rls_org_isolation ON public.system_map_loop_reservations USING ((organisation_id = (NULLIF(current_setting('app.organisation_id'::text, true), ''::text))::uuid)) WITH CHECK ((organisation_id = (NULLIF(current_setting('app.organisation_id'::text, true), ''::text))::uuid));


--

-- Name: system_map_nodes rls_org_isolation; Type: POLICY; Schema: public; Owner: -
--

CREATE POLICY rls_org_isolation ON public.system_map_nodes USING ((organisation_id = (NULLIF(current_setting('app.organisation_id'::text, true), ''::text))::uuid)) WITH CHECK ((organisation_id = (NULLIF(current_setting('app.organisation_id'::text, true), ''::text))::uuid));


--

-- Name: system_map_relationships rls_org_isolation; Type: POLICY; Schema: public; Owner: -
--

CREATE POLICY rls_org_isolation ON public.system_map_relationships USING ((organisation_id = (NULLIF(current_setting('app.organisation_id'::text, true), ''::text))::uuid)) WITH CHECK ((organisation_id = (NULLIF(current_setting('app.organisation_id'::text, true), ''::text))::uuid));


--

-- Name: system_map_revisions rls_org_isolation; Type: POLICY; Schema: public; Owner: -
--

CREATE POLICY rls_org_isolation ON public.system_map_revisions USING ((organisation_id = (NULLIF(current_setting('app.organisation_id'::text, true), ''::text))::uuid)) WITH CHECK ((organisation_id = (NULLIF(current_setting('app.organisation_id'::text, true), ''::text))::uuid));


--

-- Name: team_memberships rls_org_isolation; Type: POLICY; Schema: public; Owner: -
--

CREATE POLICY rls_org_isolation ON public.team_memberships USING ((organisation_id = (NULLIF(current_setting('app.organisation_id'::text, true), ''::text))::uuid));


--

-- Name: teams rls_org_isolation; Type: POLICY; Schema: public; Owner: -
--

CREATE POLICY rls_org_isolation ON public.teams USING ((organisation_id = (NULLIF(current_setting('app.organisation_id'::text, true), ''::text))::uuid));


--

-- Name: token_families rls_org_isolation; Type: POLICY; Schema: public; Owner: -
--

CREATE POLICY rls_org_isolation ON public.token_families USING (((organisation_id = (NULLIF(current_setting('app.organisation_id'::text, true), ''::text))::uuid) OR (NULLIF(current_setting('app.organisation_id'::text, true), ''::text) IS NULL)));


--

-- Name: trigger_event_findings rls_org_isolation; Type: POLICY; Schema: public; Owner: -
--

CREATE POLICY rls_org_isolation ON public.trigger_event_findings USING ((organisation_id = (NULLIF(current_setting('app.organisation_id'::text, true), ''::text))::uuid));


--

-- Name: trigger_events rls_org_isolation; Type: POLICY; Schema: public; Owner: -
--

CREATE POLICY rls_org_isolation ON public.trigger_events USING ((organisation_id = (NULLIF(current_setting('app.organisation_id'::text, true), ''::text))::uuid));


--

-- Name: triggers rls_org_isolation; Type: POLICY; Schema: public; Owner: -
--

CREATE POLICY rls_org_isolation ON public.triggers USING ((organisation_id = (NULLIF(current_setting('app.organisation_id'::text, true), ''::text))::uuid));


--

-- Name: variant_batch_state rls_org_isolation; Type: POLICY; Schema: public; Owner: -
--

CREATE POLICY rls_org_isolation ON public.variant_batch_state USING ((organisation_id = (NULLIF(current_setting('app.organisation_id'::text, true), ''::text))::uuid));


--

-- Name: variant_groups rls_org_isolation; Type: POLICY; Schema: public; Owner: -
--

CREATE POLICY rls_org_isolation ON public.variant_groups USING ((organisation_id = (NULLIF(current_setting('app.organisation_id'::text, true), ''::text))::uuid));


--

-- Name: web_vital_events rls_org_isolation; Type: POLICY; Schema: public; Owner: -
--

CREATE POLICY rls_org_isolation ON public.web_vital_events USING ((organisation_id = (NULLIF(current_setting('app.organisation_id'::text, true), ''::text))::uuid));


--

-- Name: webhook_dedup_hashes rls_org_isolation; Type: POLICY; Schema: public; Owner: -
--

CREATE POLICY rls_org_isolation ON public.webhook_dedup_hashes USING ((organisation_id = (NULLIF(current_setting('app.organisation_id'::text, true), ''::text))::uuid));


--

-- Name: webhook_payloads rls_org_isolation; Type: POLICY; Schema: public; Owner: -
--

CREATE POLICY rls_org_isolation ON public.webhook_payloads USING ((organisation_id = (NULLIF(current_setting('app.organisation_id'::text, true), ''::text))::uuid));


--

-- Name: connector_instances rls_team_isolation; Type: POLICY; Schema: public; Owner: -
--

CREATE POLICY rls_team_isolation ON public.connector_instances USING (((organisation_id = (NULLIF(current_setting('app.organisation_id'::text, true), ''::text))::uuid) AND (((visibility)::text = 'org'::text) OR (visibility IS NULL) OR (owner_team_id IS NULL) OR (owner_team_id IN ( SELECT team_memberships.team_id
   FROM public.team_memberships
  WHERE (team_memberships.account_id = (NULLIF(current_setting('app.user_id'::text, true), ''::text))::uuid))) OR (NULLIF(current_setting('app.org_role'::text, true), ''::text) = 'admin'::text) OR (NULLIF(current_setting('app.execution_context'::text, true), ''::text) = 'true'::text))));


--

-- Name: environment_profiles rls_team_isolation; Type: POLICY; Schema: public; Owner: -
--

CREATE POLICY rls_team_isolation ON public.environment_profiles USING (((organisation_id = (NULLIF(current_setting('app.organisation_id'::text, true), ''::text))::uuid) AND (((visibility)::text = 'org'::text) OR (visibility IS NULL) OR (owner_team_id IS NULL) OR (owner_team_id IN ( SELECT team_memberships.team_id
   FROM public.team_memberships
  WHERE (team_memberships.account_id = (NULLIF(current_setting('app.user_id'::text, true), ''::text))::uuid))) OR (NULLIF(current_setting('app.org_role'::text, true), ''::text) = 'admin'::text) OR (NULLIF(current_setting('app.execution_context'::text, true), ''::text) = 'true'::text))));


--

-- Name: eval_datasets rls_team_isolation; Type: POLICY; Schema: public; Owner: -
--

CREATE POLICY rls_team_isolation ON public.eval_datasets USING (((organisation_id = (NULLIF(current_setting('app.organisation_id'::text, true), ''::text))::uuid) AND (((visibility)::text = 'org'::text) OR (visibility IS NULL) OR (owner_team_id IS NULL) OR (owner_team_id IN ( SELECT team_memberships.team_id
   FROM public.team_memberships
  WHERE (team_memberships.account_id = (NULLIF(current_setting('app.user_id'::text, true), ''::text))::uuid))) OR (NULLIF(current_setting('app.org_role'::text, true), ''::text) = 'admin'::text) OR (NULLIF(current_setting('app.execution_context'::text, true), ''::text) = 'true'::text))));


--

-- Name: eval_suites rls_team_isolation; Type: POLICY; Schema: public; Owner: -
--

CREATE POLICY rls_team_isolation ON public.eval_suites USING (((organisation_id = (NULLIF(current_setting('app.organisation_id'::text, true), ''::text))::uuid) AND (((visibility)::text = 'org'::text) OR (visibility IS NULL) OR (owner_team_id IS NULL) OR (owner_team_id IN ( SELECT team_memberships.team_id
   FROM public.team_memberships
  WHERE (team_memberships.account_id = (NULLIF(current_setting('app.user_id'::text, true), ''::text))::uuid))) OR (NULLIF(current_setting('app.org_role'::text, true), ''::text) = 'admin'::text) OR (NULLIF(current_setting('app.execution_context'::text, true), ''::text) = 'true'::text))));


--

-- Name: external_agents rls_team_isolation; Type: POLICY; Schema: public; Owner: -
--

CREATE POLICY rls_team_isolation ON public.external_agents USING (((organisation_id = (NULLIF(current_setting('app.organisation_id'::text, true), ''::text))::uuid) AND (((visibility)::text = 'org'::text) OR (visibility IS NULL) OR (owner_team_id IS NULL) OR (owner_team_id IN ( SELECT team_memberships.team_id
   FROM public.team_memberships
  WHERE (team_memberships.account_id = (NULLIF(current_setting('app.user_id'::text, true), ''::text))::uuid))) OR (NULLIF(current_setting('app.org_role'::text, true), ''::text) = 'admin'::text) OR (NULLIF(current_setting('app.execution_context'::text, true), ''::text) = 'true'::text))));


--

-- Name: library_primitives rls_team_isolation; Type: POLICY; Schema: public; Owner: -
--

CREATE POLICY rls_team_isolation ON public.library_primitives USING (((organisation_id = (NULLIF(current_setting('app.organisation_id'::text, true), ''::text))::uuid) AND (((visibility)::text = 'org'::text) OR (visibility IS NULL) OR (owner_team_id IS NULL) OR (owner_team_id IN ( SELECT team_memberships.team_id
   FROM public.team_memberships
  WHERE (team_memberships.account_id = (NULLIF(current_setting('app.user_id'::text, true), ''::text))::uuid))) OR (NULLIF(current_setting('app.org_role'::text, true), ''::text) = 'admin'::text) OR (NULLIF(current_setting('app.execution_context'::text, true), ''::text) = 'true'::text))));


--

-- Name: lifecycle_maps rls_team_isolation; Type: POLICY; Schema: public; Owner: -
--

CREATE POLICY rls_team_isolation ON public.lifecycle_maps USING (((organisation_id = (NULLIF(current_setting('app.organisation_id'::text, true), ''::text))::uuid) AND (((visibility)::text = 'org'::text) OR (visibility IS NULL) OR (owner_team_id IS NULL) OR (owner_team_id IN ( SELECT team_memberships.team_id
   FROM public.team_memberships
  WHERE (team_memberships.account_id = (NULLIF(current_setting('app.user_id'::text, true), ''::text))::uuid))) OR (NULLIF(current_setting('app.org_role'::text, true), ''::text) = 'admin'::text) OR (NULLIF(current_setting('app.execution_context'::text, true), ''::text) = 'true'::text))));


--

-- Name: mcp_servers rls_team_isolation; Type: POLICY; Schema: public; Owner: -
--

CREATE POLICY rls_team_isolation ON public.mcp_servers USING (((organisation_id = (NULLIF(current_setting('app.organisation_id'::text, true), ''::text))::uuid) AND (((visibility)::text = 'org'::text) OR (visibility IS NULL) OR (owner_team_id IS NULL) OR (owner_team_id IN ( SELECT team_memberships.team_id
   FROM public.team_memberships
  WHERE (team_memberships.account_id = (NULLIF(current_setting('app.user_id'::text, true), ''::text))::uuid))) OR (NULLIF(current_setting('app.org_role'::text, true), ''::text) = 'admin'::text) OR (NULLIF(current_setting('app.execution_context'::text, true), ''::text) = 'true'::text))));


--

-- Name: model_backends rls_team_isolation; Type: POLICY; Schema: public; Owner: -
--

CREATE POLICY rls_team_isolation ON public.model_backends USING (((organisation_id = (NULLIF(current_setting('app.organisation_id'::text, true), ''::text))::uuid) AND (((visibility)::text = 'org'::text) OR (visibility IS NULL) OR (owner_team_id IS NULL) OR (owner_team_id IN ( SELECT team_memberships.team_id
   FROM public.team_memberships
  WHERE (team_memberships.account_id = (NULLIF(current_setting('app.user_id'::text, true), ''::text))::uuid))) OR (NULLIF(current_setting('app.org_role'::text, true), ''::text) = 'admin'::text) OR (NULLIF(current_setting('app.execution_context'::text, true), ''::text) = 'true'::text))));


--

-- Name: pipelines rls_team_isolation; Type: POLICY; Schema: public; Owner: -
--

CREATE POLICY rls_team_isolation ON public.pipelines USING (((organisation_id = (NULLIF(current_setting('app.organisation_id'::text, true), ''::text))::uuid) AND (((visibility)::text = 'org'::text) OR (visibility IS NULL) OR (owner_team_id IS NULL) OR (owner_team_id IN ( SELECT team_memberships.team_id
   FROM public.team_memberships
  WHERE (team_memberships.account_id = (NULLIF(current_setting('app.user_id'::text, true), ''::text))::uuid))) OR (NULLIF(current_setting('app.org_role'::text, true), ''::text) = 'admin'::text) OR (NULLIF(current_setting('app.execution_context'::text, true), ''::text) = 'true'::text))));


--

-- Name: system_maps rls_team_isolation; Type: POLICY; Schema: public; Owner: -
--

CREATE POLICY rls_team_isolation ON public.system_maps USING (((organisation_id = (NULLIF(current_setting('app.organisation_id'::text, true), ''::text))::uuid) AND (((visibility)::text = 'org'::text) OR (visibility IS NULL) OR (owner_team_id IS NULL) OR (owner_team_id IN ( SELECT team_memberships.team_id
   FROM public.team_memberships
  WHERE (team_memberships.account_id = (NULLIF(current_setting('app.user_id'::text, true), ''::text))::uuid))) OR (NULLIF(current_setting('app.org_role'::text, true), ''::text) = 'admin'::text) OR (NULLIF(current_setting('app.execution_context'::text, true), ''::text) = 'true'::text)))) WITH CHECK (((organisation_id = (NULLIF(current_setting('app.organisation_id'::text, true), ''::text))::uuid) AND (((visibility)::text = 'org'::text) OR (visibility IS NULL) OR (owner_team_id IS NULL) OR (owner_team_id IN ( SELECT team_memberships.team_id
   FROM public.team_memberships
  WHERE (team_memberships.account_id = (NULLIF(current_setting('app.user_id'::text, true), ''::text))::uuid))) OR (NULLIF(current_setting('app.org_role'::text, true), ''::text) = 'admin'::text) OR (NULLIF(current_setting('app.execution_context'::text, true), ''::text) = 'true'::text))));


--

-- Name: notification_preferences rls_user_isolation_delete; Type: POLICY; Schema: public; Owner: -
--

CREATE POLICY rls_user_isolation_delete ON public.notification_preferences FOR DELETE USING (((organisation_id = (NULLIF(current_setting('app.organisation_id'::text, true), ''::text))::uuid) AND (account_id = (NULLIF(current_setting('app.user_id'::text, true), ''::text))::uuid)));


--

-- Name: notification_preferences rls_user_isolation_insert; Type: POLICY; Schema: public; Owner: -
--

CREATE POLICY rls_user_isolation_insert ON public.notification_preferences FOR INSERT WITH CHECK (((organisation_id = (NULLIF(current_setting('app.organisation_id'::text, true), ''::text))::uuid) AND (account_id = (NULLIF(current_setting('app.user_id'::text, true), ''::text))::uuid)));


--

-- Name: notification_preferences rls_user_isolation_update; Type: POLICY; Schema: public; Owner: -
--

CREATE POLICY rls_user_isolation_update ON public.notification_preferences FOR UPDATE USING (((organisation_id = (NULLIF(current_setting('app.organisation_id'::text, true), ''::text))::uuid) AND (account_id = (NULLIF(current_setting('app.user_id'::text, true), ''::text))::uuid))) WITH CHECK (((organisation_id = (NULLIF(current_setting('app.organisation_id'::text, true), ''::text))::uuid) AND (account_id = (NULLIF(current_setting('app.user_id'::text, true), ''::text))::uuid)));


--

-- Name: audit_events_append_only(); Type: FUNCTION; Schema: public; Owner: -
--

CREATE FUNCTION public.audit_events_append_only() RETURNS trigger
    LANGUAGE plpgsql
    AS $$
BEGIN
    IF TG_OP = 'UPDATE' THEN
        RAISE EXCEPTION 'audit_events are append-only: UPDATE is not permitted';
    ELSIF TG_OP = 'DELETE' THEN
        IF pg_trigger_depth() <= 1 THEN
            RAISE EXCEPTION 'audit_events are append-only: DELETE is not permitted';
        END IF;
        RETURN OLD;
    END IF;
    RETURN NEW;
END;
$$;


--

-- Name: check_team_privilege_cap(); Type: FUNCTION; Schema: public; Owner: -
--

CREATE FUNCTION public.check_team_privilege_cap() RETURNS trigger
    LANGUAGE plpgsql
    AS $$ DECLARE _user_org_role TEXT; _org_level INT; _team_level INT; BEGIN SELECT role INTO _user_org_role FROM org_memberships WHERE account_id = NEW.account_id AND organisation_id = NEW.organisation_id; _org_level := CASE _user_org_role WHEN 'viewer' THEN 0 WHEN 'runner' THEN 1 WHEN 'operator' THEN 2 WHEN 'admin' THEN 3 ELSE -1 END; _team_level := CASE NEW.role WHEN 'viewer' THEN 0 WHEN 'runner' THEN 1 WHEN 'operator' THEN 2 ELSE -1 END; IF _team_level > _org_level THEN RAISE EXCEPTION 'Team role "%" exceeds org role "%" for account %', NEW.role, _user_org_role, NEW.account_id; END IF; RETURN NEW; END; $$;


--

-- Name: deactivate_break_glass(uuid, uuid, boolean); Type: FUNCTION; Schema: public; Owner: -
--

CREATE FUNCTION public.deactivate_break_glass(caller_account_id uuid, target_account_id uuid, force_last_admin boolean DEFAULT false) RETURNS void
    LANGUAGE plpgsql SECURITY DEFINER
    SET search_path TO 'pg_catalog', 'public'
    SET row_security TO 'off'
    AS $_$ DECLARE tgt_org RECORD; k1 int4; k2 int4; is_operator bool; is_bg_target bool; BEGIN is_operator := session_user = 'farna_control_breakglass'; is_bg_target := EXISTS (SELECT 1 FROM public.accounts WHERE id = $2 AND is_break_glass IS TRUE); IF $3 AND NOT is_operator THEN RAISE EXCEPTION 'force_last_admin requires operator' USING ERRCODE = 'M2010'; END IF; IF NOT (is_operator OR EXISTS (SELECT 1 FROM public.org_memberships caller WHERE caller.account_id = $1 AND caller.deactivated_at IS NULL AND caller.role = 'admin' AND EXISTS (SELECT 1 FROM public.org_memberships tgt WHERE tgt.account_id = $2 AND tgt.organisation_id = caller.organisation_id)) OR (EXISTS (SELECT 1 FROM public.accounts c WHERE c.id = $1 AND c.is_break_glass IS TRUE AND c.active IS TRUE) AND is_bg_target AND EXISTS (SELECT 1 FROM public.org_memberships cm JOIN public.org_memberships tm ON tm.organisation_id = cm.organisation_id WHERE cm.account_id = $1 AND tm.account_id = $2))) THEN RAISE EXCEPTION 'caller not authorized to deactivate target' USING ERRCODE = 'M2010'; END IF;  FOR tgt_org IN SELECT DISTINCT organisation_id FROM public.org_memberships WHERE account_id = $2 AND deactivated_at IS NULL ORDER BY organisation_id LOOP SELECT ('x' || substr(md5(tgt_org.organisation_id::text), 1, 8))::bit(32)::int4, ('x' || substr(md5(tgt_org.organisation_id::text), 9, 8))::bit(32)::int4 INTO k1, k2; PERFORM pg_advisory_xact_lock(k1, k2); END LOOP;  /* FAR-539: the M2020 last-admin guard is PER-ORG — it covers only the orgs this call tombstones. The operator branch tombstones EVERY org of the target, so is_operator keeps the all-orgs iteration verbatim; the non-operator branch tombstones only the caller's active-admin orgs (the same predicate as the org_memberships UPDATE below), so the guard iterates exactly that set. A target who is the last admin of org-B can be deactivated from org-A because org-B is untouched and cannot be orphaned. */  FOR tgt_org IN SELECT DISTINCT om.organisation_id FROM public.org_memberships om WHERE om.account_id = $2 AND om.deactivated_at IS NULL AND (is_operator OR EXISTS (SELECT 1 FROM public.org_memberships caller WHERE caller.account_id = $1 AND caller.organisation_id = om.organisation_id AND caller.deactivated_at IS NULL AND caller.role = 'admin')) LOOP IF NOT $3 AND (SELECT count(*) FROM public.org_memberships om JOIN public.accounts a ON a.id = om.account_id WHERE om.organisation_id = tgt_org.organisation_id AND om.deactivated_at IS NULL AND om.role = 'admin' AND a.active IS TRUE AND a.is_break_glass IS FALSE AND a.id <> $2) = 0 AND EXISTS (SELECT 1 FROM public.org_memberships om2 JOIN public.accounts a2 ON a2.id = om2.account_id WHERE om2.organisation_id = tgt_org.organisation_id AND a2.is_break_glass IS FALSE) THEN RAISE EXCEPTION 'deactivation would orphan org' USING ERRCODE = 'M2020'; END IF; END LOOP;  /* gh-1794/FAR-533: the global accounts.active flip is OPERATOR-ONLY (break-glass = global ban). */ IF is_operator THEN UPDATE public.token_families SET is_blacklisted = true, blacklisted_at = now() WHERE account_id = $2; UPDATE public.org_api_keys SET revoked_at = now() WHERE account_id = $2 AND revoked_at IS NULL; UPDATE public.org_memberships SET deactivated_at = now() WHERE account_id = $2; UPDATE public.accounts SET active = false WHERE id = $2; IF NOT FOUND THEN RAISE EXCEPTION 'target does not exist' USING ERRCODE = 'M2040'; END IF; ELSE  /* gh-1794/FAR-533: per-org deactivation — the org-membership deactivated_at tombstone written below IS the deactivation signal; accounts.active is left untouched so other orgs are unaffected. The explicit existence check preserves the M2040 semantics the old accounts UPDATE provided. */ IF NOT EXISTS (SELECT 1 FROM public.accounts WHERE id = $2) THEN RAISE EXCEPTION 'target does not exist' USING ERRCODE = 'M2040'; END IF; UPDATE public.token_families SET is_blacklisted = true, blacklisted_at = now() WHERE account_id = $2 AND family_id IN (SELECT tf.family_id FROM public.token_families tf JOIN public.org_memberships caller ON caller.organisation_id = tf.organisation_id WHERE tf.account_id = $2 AND caller.account_id = $1 AND caller.deactivated_at IS NULL AND caller.role = 'admin'); UPDATE public.org_api_keys SET revoked_at = now() WHERE account_id = $2 AND revoked_at IS NULL AND organisation_id IN (SELECT caller.organisation_id FROM public.org_memberships caller WHERE caller.account_id = $1 AND caller.deactivated_at IS NULL AND caller.role = 'admin'); UPDATE public.org_memberships SET deactivated_at = now() WHERE account_id = $2 AND organisation_id IN (SELECT caller.organisation_id FROM public.org_memberships caller WHERE caller.account_id = $1 AND caller.deactivated_at IS NULL AND caller.role = 'admin'); END IF; IF is_bg_target THEN UPDATE public.accounts SET break_glass_expires_at = NULL, break_glass_deactivated_at = now(), password_hash = gen_random_uuid()::text WHERE id = $2; END IF; END $_$;


--

-- Name: enforce_account_kind_immutable(); Type: FUNCTION; Schema: public; Owner: -
--

CREATE FUNCTION public.enforce_account_kind_immutable() RETURNS trigger
    LANGUAGE plpgsql
    SET search_path TO 'pg_catalog', 'public'
    AS $$
BEGIN
    IF NEW.kind IS DISTINCT FROM OLD.kind THEN
        RAISE EXCEPTION 'account kind is immutable'
            USING ERRCODE = '23514',
                  DETAIL = 'ADR 056/058: an account''s kind is fixed at creation.',
                  HINT = 'Provision a new service account instead of converting an existing row.';
    END IF;
    RETURN NEW;
END;
$$;


--

-- Name: enforce_library_fork_provenance(); Type: FUNCTION; Schema: public; Owner: -
--

CREATE FUNCTION public.enforce_library_fork_provenance() RETURNS trigger
    LANGUAGE plpgsql
    AS $$ DECLARE parent_source text; BEGIN IF TG_OP = 'UPDATE' AND OLD.forked_from IS DISTINCT FROM NEW.forked_from THEN RAISE EXCEPTION 'library primitive forked_from is immutable' USING ERRCODE = '23514'; END IF; IF NEW.forked_from IS NOT NULL THEN SELECT source INTO parent_source FROM library_primitives WHERE id = NEW.forked_from; IF parent_source IS DISTINCT FROM 'registry' THEN RAISE EXCEPTION 'forked_from must reference a registry primitive' USING ERRCODE = '23514'; END IF; END IF; RETURN NEW; END; $$;


--

-- Name: enforce_same_organisation(); Type: FUNCTION; Schema: public; Owner: -
--

CREATE FUNCTION public.enforce_same_organisation() RETURNS trigger
    LANGUAGE plpgsql
    AS $_$ DECLARE referenced_id uuid; referenced_organisation_id uuid; child_organisation_id uuid; col_exists boolean; col_type text; BEGIN SELECT data_type INTO col_type FROM information_schema.columns WHERE table_name = TG_TABLE_NAME AND column_name = TG_ARGV[1]; IF col_type IS DISTINCT FROM 'uuid' THEN RETURN NEW; END IF; referenced_id := (to_jsonb(NEW) ->> TG_ARGV[1])::uuid; IF referenced_id IS NULL THEN RETURN NEW; END IF; child_organisation_id := (to_jsonb(NEW) ->> 'organisation_id')::uuid; SELECT COUNT(*) > 0 INTO col_exists FROM information_schema.columns WHERE table_name = TG_ARGV[0] AND column_name = 'organisation_id'; IF NOT col_exists THEN RETURN NEW; END IF; EXECUTE format('SELECT organisation_id FROM %I WHERE id = $1', TG_ARGV[0]) INTO referenced_organisation_id USING referenced_id; IF referenced_organisation_id IS NULL OR referenced_organisation_id <> child_organisation_id THEN RAISE EXCEPTION 'cross-organisation reference from %.% to %', TG_TABLE_NAME, TG_ARGV[1], TG_ARGV[0] USING ERRCODE = '23503'; END IF; RETURN NEW; END; $_$;


--

-- Name: enforce_service_role_ceiling(); Type: FUNCTION; Schema: public; Owner: -
--

CREATE FUNCTION public.enforce_service_role_ceiling() RETURNS trigger
    LANGUAGE plpgsql
    SET search_path TO 'pg_catalog', 'public'
    AS $$
DECLARE
    target_kind text;
BEGIN
    IF NEW.role = 'admin' THEN
        SELECT kind INTO target_kind FROM public.accounts WHERE id = NEW.account_id;
        IF target_kind = 'service' THEN
            RAISE EXCEPTION 'service principals cannot hold the admin role'
                USING ERRCODE = '23514',
                      DETAIL = 'ADR 058: a service account''s org-role ceiling is operator.',
                      HINT = 'Grant operator, runner or viewer instead.';
        END IF;
    END IF;
    RETURN NEW;
END;
$$;


--

-- Name: enforce_system_map_owner_team(); Type: FUNCTION; Schema: public; Owner: -
--

CREATE FUNCTION public.enforce_system_map_owner_team() RETURNS trigger
    LANGUAGE plpgsql
    AS $$
            BEGIN
                IF NEW.visibility = 'team' AND NEW.owner_team_id IS NULL THEN
                    RAISE EXCEPTION 'team-visible system maps require an owner team'
                        USING ERRCODE = '23514';
                END IF;

                IF NEW.owner_team_id IS NOT NULL AND NOT EXISTS (
                    SELECT 1
                    FROM public.teams
                    WHERE teams.id = NEW.owner_team_id
                      AND teams.organisation_id = NEW.organisation_id
                      AND teams.deleted_at IS NULL
                ) THEN
                    RAISE EXCEPTION 'system map owner team must belong to the same organisation'
                        USING ERRCODE = '23503';
                END IF;

                RETURN NEW;
            END;
            $$;


--

-- Name: enforce_system_map_reservation_run(); Type: FUNCTION; Schema: public; Owner: -
--

CREATE FUNCTION public.enforce_system_map_reservation_run() RETURNS trigger
    LANGUAGE plpgsql
    AS $$
            BEGIN
                IF NOT EXISTS (
                    SELECT 1 FROM public.runs
                    WHERE runs.id = NEW.run_id
                      AND runs.organisation_id = NEW.organisation_id
                ) THEN
                    RAISE EXCEPTION 'system map reservation run must belong to the same organisation'
                        USING ERRCODE = '23503';
                END IF;
                RETURN NEW;
            END;
            $$;


--

-- Name: error_events_append_only(); Type: FUNCTION; Schema: public; Owner: -
--

CREATE FUNCTION public.error_events_append_only() RETURNS trigger
    LANGUAGE plpgsql
    AS $$
BEGIN
    IF TG_OP = 'UPDATE' THEN
        RAISE EXCEPTION 'error_events are append-only: UPDATE is not permitted';
    ELSIF TG_OP = 'DELETE' THEN
        IF pg_trigger_depth() <= 1 THEN
            RAISE EXCEPTION 'error_events are append-only: DELETE is not permitted';
        END IF;
        RETURN OLD;
    END IF;
    RETURN NEW;
END;
$$;


--

-- Name: lookup_api_key_org(text); Type: FUNCTION; Schema: public; Owner: -
--

CREATE FUNCTION public.lookup_api_key_org(lookup_prefix_value text) RETURNS uuid
    LANGUAGE sql STABLE SECURITY DEFINER
    SET search_path TO 'public'
    AS $$ SELECT organisation_id FROM org_api_keys WHERE lookup_prefix = lookup_prefix_value AND revoked_at IS NULL LIMIT 1 $$;


--

-- Name: system_audit_events_append_only(); Type: FUNCTION; Schema: public; Owner: -
--

CREATE FUNCTION public.system_audit_events_append_only() RETURNS trigger
    LANGUAGE plpgsql
    AS $$
            BEGIN
                IF TG_OP = 'DELETE' THEN
                    RAISE EXCEPTION 'system_audit_events are append-only: DELETE is not permitted';
                ELSIF TG_OP = 'UPDATE' THEN
                    RAISE EXCEPTION 'system_audit_events are append-only: UPDATE is not permitted';
                END IF;
                RETURN NULL;
            END;
            $$;


SET default_tablespace = '';

SET default_table_access_method = heap;

--

-- Name: audit_events audit_events_no_delete; Type: TRIGGER; Schema: public; Owner: -
--

CREATE TRIGGER audit_events_no_delete BEFORE DELETE ON public.audit_events FOR EACH ROW EXECUTE FUNCTION public.audit_events_append_only();


--

-- Name: audit_events audit_events_no_update; Type: TRIGGER; Schema: public; Owner: -
--

CREATE TRIGGER audit_events_no_update BEFORE UPDATE ON public.audit_events FOR EACH ROW EXECUTE FUNCTION public.audit_events_append_only();


--

-- Name: error_events error_events_no_delete; Type: TRIGGER; Schema: public; Owner: -
--

CREATE TRIGGER error_events_no_delete BEFORE DELETE ON public.error_events FOR EACH ROW EXECUTE FUNCTION public.error_events_append_only();


--

-- Name: error_events error_events_no_update; Type: TRIGGER; Schema: public; Owner: -
--

CREATE TRIGGER error_events_no_update BEFORE UPDATE ON public.error_events FOR EACH ROW EXECUTE FUNCTION public.error_events_append_only();


--

-- Name: system_audit_events system_audit_events_no_delete; Type: TRIGGER; Schema: public; Owner: -
--

CREATE TRIGGER system_audit_events_no_delete BEFORE DELETE ON public.system_audit_events FOR EACH ROW EXECUTE FUNCTION public.system_audit_events_append_only();


--

-- Name: system_audit_events system_audit_events_no_update; Type: TRIGGER; Schema: public; Owner: -
--

CREATE TRIGGER system_audit_events_no_update BEFORE UPDATE ON public.system_audit_events FOR EACH ROW EXECUTE FUNCTION public.system_audit_events_append_only();


--

-- Name: accounts trg_accounts_kind_immutable; Type: TRIGGER; Schema: public; Owner: -
--

CREATE TRIGGER trg_accounts_kind_immutable BEFORE UPDATE OF kind ON public.accounts FOR EACH ROW EXECUTE FUNCTION public.enforce_account_kind_immutable();


--

-- Name: agents trg_agents_account_id_tenant; Type: TRIGGER; Schema: public; Owner: -
--

CREATE TRIGGER trg_agents_account_id_tenant BEFORE INSERT OR UPDATE OF account_id, organisation_id ON public.agents FOR EACH ROW EXECUTE FUNCTION public.enforce_same_organisation('accounts', 'account_id');


--

-- Name: agents trg_agents_created_by_tenant; Type: TRIGGER; Schema: public; Owner: -
--

CREATE TRIGGER trg_agents_created_by_tenant BEFORE INSERT OR UPDATE OF created_by, organisation_id ON public.agents FOR EACH ROW EXECUTE FUNCTION public.enforce_same_organisation('accounts', 'created_by');


--

-- Name: agents trg_agents_input_schema_id_tenant; Type: TRIGGER; Schema: public; Owner: -
--

CREATE TRIGGER trg_agents_input_schema_id_tenant BEFORE INSERT OR UPDATE OF input_schema_id, organisation_id ON public.agents FOR EACH ROW EXECUTE FUNCTION public.enforce_same_organisation('schemas', 'input_schema_id');


--

-- Name: agents trg_agents_library_id_tenant; Type: TRIGGER; Schema: public; Owner: -
--

CREATE TRIGGER trg_agents_library_id_tenant BEFORE INSERT OR UPDATE OF library_id, organisation_id ON public.agents FOR EACH ROW EXECUTE FUNCTION public.enforce_same_organisation('library_primitives', 'library_id');


--

-- Name: agents trg_agents_model_backend_id_tenant; Type: TRIGGER; Schema: public; Owner: -
--

CREATE TRIGGER trg_agents_model_backend_id_tenant BEFORE INSERT OR UPDATE OF model_backend_id, organisation_id ON public.agents FOR EACH ROW EXECUTE FUNCTION public.enforce_same_organisation('model_backends', 'model_backend_id');


--

-- Name: agents trg_agents_output_schema_id_tenant; Type: TRIGGER; Schema: public; Owner: -
--

CREATE TRIGGER trg_agents_output_schema_id_tenant BEFORE INSERT OR UPDATE OF output_schema_id, organisation_id ON public.agents FOR EACH ROW EXECUTE FUNCTION public.enforce_same_organisation('schemas', 'output_schema_id');


--

-- Name: agents trg_agents_parameter_schema_id_tenant; Type: TRIGGER; Schema: public; Owner: -
--

CREATE TRIGGER trg_agents_parameter_schema_id_tenant BEFORE INSERT OR UPDATE OF parameter_schema_id, organisation_id ON public.agents FOR EACH ROW EXECUTE FUNCTION public.enforce_same_organisation('parameter_schemas', 'parameter_schema_id');


--

-- Name: assistant_context_sources trg_assistant_context_sources_account_id_tenant; Type: TRIGGER; Schema: public; Owner: -
--

CREATE TRIGGER trg_assistant_context_sources_account_id_tenant BEFORE INSERT OR UPDATE OF account_id, organisation_id ON public.assistant_context_sources FOR EACH ROW EXECUTE FUNCTION public.enforce_same_organisation('accounts', 'account_id');


--

-- Name: assistant_skills trg_assistant_skills_account_id_tenant; Type: TRIGGER; Schema: public; Owner: -
--

CREATE TRIGGER trg_assistant_skills_account_id_tenant BEFORE INSERT OR UPDATE OF account_id, organisation_id ON public.assistant_skills FOR EACH ROW EXECUTE FUNCTION public.enforce_same_organisation('accounts', 'account_id');


--

-- Name: audit_chain_heads trg_audit_chain_heads_last_event_id_tenant; Type: TRIGGER; Schema: public; Owner: -
--

CREATE TRIGGER trg_audit_chain_heads_last_event_id_tenant BEFORE INSERT OR UPDATE OF last_event_id, organisation_id ON public.audit_chain_heads FOR EACH ROW EXECUTE FUNCTION public.enforce_same_organisation('audit_events', 'last_event_id');


--

-- Name: audit_events trg_audit_events_account_id_tenant; Type: TRIGGER; Schema: public; Owner: -
--

CREATE TRIGGER trg_audit_events_account_id_tenant BEFORE INSERT OR UPDATE OF account_id, organisation_id ON public.audit_events FOR EACH ROW EXECUTE FUNCTION public.enforce_same_organisation('accounts', 'account_id');


--

-- Name: chat_messages trg_chat_messages_parent_id_tenant; Type: TRIGGER; Schema: public; Owner: -
--

CREATE TRIGGER trg_chat_messages_parent_id_tenant BEFORE INSERT OR UPDATE OF parent_id, organisation_id ON public.chat_messages FOR EACH ROW EXECUTE FUNCTION public.enforce_same_organisation('chat_messages', 'parent_id');


--

-- Name: chat_messages trg_chat_messages_session_id_tenant; Type: TRIGGER; Schema: public; Owner: -
--

CREATE TRIGGER trg_chat_messages_session_id_tenant BEFORE INSERT OR UPDATE OF session_id, organisation_id ON public.chat_messages FOR EACH ROW EXECUTE FUNCTION public.enforce_same_organisation('chat_sessions', 'session_id');


--

-- Name: chat_sessions trg_chat_sessions_account_id_tenant; Type: TRIGGER; Schema: public; Owner: -
--

CREATE TRIGGER trg_chat_sessions_account_id_tenant BEFORE INSERT OR UPDATE OF account_id, organisation_id ON public.chat_sessions FOR EACH ROW EXECUTE FUNCTION public.enforce_same_organisation('accounts', 'account_id');


--

-- Name: composite_templates trg_composite_templates_account_id_tenant; Type: TRIGGER; Schema: public; Owner: -
--

CREATE TRIGGER trg_composite_templates_account_id_tenant BEFORE INSERT OR UPDATE OF account_id, organisation_id ON public.composite_templates FOR EACH ROW EXECUTE FUNCTION public.enforce_same_organisation('accounts', 'account_id');


--

-- Name: connector_instances trg_connector_instances_account_id_tenant; Type: TRIGGER; Schema: public; Owner: -
--

CREATE TRIGGER trg_connector_instances_account_id_tenant BEFORE INSERT OR UPDATE OF account_id, organisation_id ON public.connector_instances FOR EACH ROW EXECUTE FUNCTION public.enforce_same_organisation('accounts', 'account_id');


--

-- Name: connector_instances trg_connector_instances_created_by_tenant; Type: TRIGGER; Schema: public; Owner: -
--

CREATE TRIGGER trg_connector_instances_created_by_tenant BEFORE INSERT OR UPDATE OF created_by, organisation_id ON public.connector_instances FOR EACH ROW EXECUTE FUNCTION public.enforce_same_organisation('accounts', 'created_by');


--

-- Name: connector_instances trg_connector_instances_owner_team_id_tenant; Type: TRIGGER; Schema: public; Owner: -
--

CREATE TRIGGER trg_connector_instances_owner_team_id_tenant BEFORE INSERT OR UPDATE OF owner_team_id, organisation_id ON public.connector_instances FOR EACH ROW EXECUTE FUNCTION public.enforce_same_organisation('teams', 'owner_team_id');


--

-- Name: environment_profiles trg_environment_profiles_account_id_tenant; Type: TRIGGER; Schema: public; Owner: -
--

CREATE TRIGGER trg_environment_profiles_account_id_tenant BEFORE INSERT OR UPDATE OF account_id, organisation_id ON public.environment_profiles FOR EACH ROW EXECUTE FUNCTION public.enforce_same_organisation('accounts', 'account_id');


--

-- Name: environment_profiles trg_environment_profiles_owner_team_id_tenant; Type: TRIGGER; Schema: public; Owner: -
--

CREATE TRIGGER trg_environment_profiles_owner_team_id_tenant BEFORE INSERT OR UPDATE OF owner_team_id, organisation_id ON public.environment_profiles FOR EACH ROW EXECUTE FUNCTION public.enforce_same_organisation('teams', 'owner_team_id');


--

-- Name: error_groups trg_error_groups_assigned_to_tenant; Type: TRIGGER; Schema: public; Owner: -
--

CREATE TRIGGER trg_error_groups_assigned_to_tenant BEFORE INSERT OR UPDATE OF assigned_to, organisation_id ON public.error_groups FOR EACH ROW EXECUTE FUNCTION public.enforce_same_organisation('accounts', 'assigned_to');


--

-- Name: error_groups trg_error_groups_sample_event_id_tenant; Type: TRIGGER; Schema: public; Owner: -
--

CREATE TRIGGER trg_error_groups_sample_event_id_tenant BEFORE INSERT OR UPDATE OF sample_event_id, organisation_id ON public.error_groups FOR EACH ROW EXECUTE FUNCTION public.enforce_same_organisation('error_events', 'sample_event_id');


--

-- Name: eval_results trg_eval_results_eval_id_tenant; Type: TRIGGER; Schema: public; Owner: -
--

CREATE TRIGGER trg_eval_results_eval_id_tenant BEFORE INSERT OR UPDATE OF eval_id, organisation_id ON public.eval_results FOR EACH ROW EXECUTE FUNCTION public.enforce_same_organisation('evals', 'eval_id');


--

-- Name: eval_results trg_eval_results_run_id_tenant; Type: TRIGGER; Schema: public; Owner: -
--

CREATE TRIGGER trg_eval_results_run_id_tenant BEFORE INSERT OR UPDATE OF run_id, organisation_id ON public.eval_results FOR EACH ROW EXECUTE FUNCTION public.enforce_same_organisation('runs', 'run_id');


--

-- Name: feedback_records trg_feedback_records_account_id_tenant; Type: TRIGGER; Schema: public; Owner: -
--

CREATE TRIGGER trg_feedback_records_account_id_tenant BEFORE INSERT OR UPDATE OF account_id, organisation_id ON public.feedback_records FOR EACH ROW EXECUTE FUNCTION public.enforce_same_organisation('accounts', 'account_id');


--

-- Name: feedback_records trg_feedback_records_correction_run_id_tenant; Type: TRIGGER; Schema: public; Owner: -
--

CREATE TRIGGER trg_feedback_records_correction_run_id_tenant BEFORE INSERT OR UPDATE OF correction_run_id, organisation_id ON public.feedback_records FOR EACH ROW EXECUTE FUNCTION public.enforce_same_organisation('runs', 'correction_run_id');


--

-- Name: feedback_records trg_feedback_records_producing_agent_id_tenant; Type: TRIGGER; Schema: public; Owner: -
--

CREATE TRIGGER trg_feedback_records_producing_agent_id_tenant BEFORE INSERT OR UPDATE OF producing_agent_id, organisation_id ON public.feedback_records FOR EACH ROW EXECUTE FUNCTION public.enforce_same_organisation('agents', 'producing_agent_id');


--

-- Name: feedback_records trg_feedback_records_run_id_tenant; Type: TRIGGER; Schema: public; Owner: -
--

CREATE TRIGGER trg_feedback_records_run_id_tenant BEFORE INSERT OR UPDATE OF run_id, organisation_id ON public.feedback_records FOR EACH ROW EXECUTE FUNCTION public.enforce_same_organisation('runs', 'run_id');


--

-- Name: hitl_claims trg_hitl_claims_account_id_tenant; Type: TRIGGER; Schema: public; Owner: -
--

CREATE TRIGGER trg_hitl_claims_account_id_tenant BEFORE INSERT OR UPDATE OF account_id, organisation_id ON public.hitl_claims FOR EACH ROW EXECUTE FUNCTION public.enforce_same_organisation('accounts', 'account_id');


--

-- Name: hitl_claims trg_hitl_claims_pipeline_id_tenant; Type: TRIGGER; Schema: public; Owner: -
--

CREATE TRIGGER trg_hitl_claims_pipeline_id_tenant BEFORE INSERT OR UPDATE OF pipeline_id, organisation_id ON public.hitl_claims FOR EACH ROW EXECUTE FUNCTION public.enforce_same_organisation('pipelines', 'pipeline_id');


--

-- Name: hitl_claims trg_hitl_claims_required_team_id_tenant; Type: TRIGGER; Schema: public; Owner: -
--

CREATE TRIGGER trg_hitl_claims_required_team_id_tenant BEFORE INSERT OR UPDATE OF required_team_id, organisation_id ON public.hitl_claims FOR EACH ROW EXECUTE FUNCTION public.enforce_same_organisation('teams', 'required_team_id');


--

-- Name: hitl_claims trg_hitl_claims_run_id_tenant; Type: TRIGGER; Schema: public; Owner: -
--

CREATE TRIGGER trg_hitl_claims_run_id_tenant BEFORE INSERT OR UPDATE OF run_id, organisation_id ON public.hitl_claims FOR EACH ROW EXECUTE FUNCTION public.enforce_same_organisation('runs', 'run_id');


--

-- Name: journeys trg_journeys_owner_team_id_tenant; Type: TRIGGER; Schema: public; Owner: -
--

CREATE TRIGGER trg_journeys_owner_team_id_tenant BEFORE INSERT OR UPDATE OF owner_team_id, organisation_id ON public.journeys FOR EACH ROW EXECUTE FUNCTION public.enforce_same_organisation('teams', 'owner_team_id');


--

-- Name: library_primitives trg_library_primitives_account_id_tenant; Type: TRIGGER; Schema: public; Owner: -
--

CREATE TRIGGER trg_library_primitives_account_id_tenant BEFORE INSERT OR UPDATE OF account_id, organisation_id ON public.library_primitives FOR EACH ROW EXECUTE FUNCTION public.enforce_same_organisation('accounts', 'account_id');


--

-- Name: library_primitives trg_library_primitives_fork_provenance; Type: TRIGGER; Schema: public; Owner: -
--

CREATE TRIGGER trg_library_primitives_fork_provenance BEFORE INSERT OR UPDATE OF forked_from ON public.library_primitives FOR EACH ROW EXECUTE FUNCTION public.enforce_library_fork_provenance();


--

-- Name: library_primitives trg_library_primitives_forked_from_tenant; Type: TRIGGER; Schema: public; Owner: -
--

CREATE TRIGGER trg_library_primitives_forked_from_tenant BEFORE INSERT OR UPDATE OF forked_from, organisation_id ON public.library_primitives FOR EACH ROW EXECUTE FUNCTION public.enforce_same_organisation('library_primitives', 'forked_from');


--

-- Name: library_primitives trg_library_primitives_owner_team_id_tenant; Type: TRIGGER; Schema: public; Owner: -
--

CREATE TRIGGER trg_library_primitives_owner_team_id_tenant BEFORE INSERT OR UPDATE OF owner_team_id, organisation_id ON public.library_primitives FOR EACH ROW EXECUTE FUNCTION public.enforce_same_organisation('teams', 'owner_team_id');


--

-- Name: library_primitives trg_library_primitives_update_available_version_id_tenant; Type: TRIGGER; Schema: public; Owner: -
--

CREATE TRIGGER trg_library_primitives_update_available_version_id_tenant BEFORE INSERT OR UPDATE OF update_available_version_id, organisation_id ON public.library_primitives FOR EACH ROW EXECUTE FUNCTION public.enforce_same_organisation('library_primitives', 'update_available_version_id');


--

-- Name: library_primitives trg_library_primitives_version_group_id_tenant; Type: TRIGGER; Schema: public; Owner: -
--

CREATE TRIGGER trg_library_primitives_version_group_id_tenant BEFORE INSERT OR UPDATE OF version_group_id, organisation_id ON public.library_primitives FOR EACH ROW EXECUTE FUNCTION public.enforce_same_organisation('library_primitives', 'version_group_id');


--

-- Name: lifecycle_map_stages trg_lifecycle_map_stages_account_id_tenant; Type: TRIGGER; Schema: public; Owner: -
--

CREATE TRIGGER trg_lifecycle_map_stages_account_id_tenant BEFORE INSERT OR UPDATE OF account_id, organisation_id ON public.lifecycle_map_stages FOR EACH ROW EXECUTE FUNCTION public.enforce_same_organisation('accounts', 'account_id');


--

-- Name: lifecycle_map_stages trg_lifecycle_map_stages_map_id_tenant; Type: TRIGGER; Schema: public; Owner: -
--

CREATE TRIGGER trg_lifecycle_map_stages_map_id_tenant BEFORE INSERT OR UPDATE OF map_id, organisation_id ON public.lifecycle_map_stages FOR EACH ROW EXECUTE FUNCTION public.enforce_same_organisation('lifecycle_maps', 'map_id');


--

-- Name: lifecycle_map_stages trg_lifecycle_map_stages_pipeline_id_tenant; Type: TRIGGER; Schema: public; Owner: -
--

CREATE TRIGGER trg_lifecycle_map_stages_pipeline_id_tenant BEFORE INSERT OR UPDATE OF pipeline_id, organisation_id ON public.lifecycle_map_stages FOR EACH ROW EXECUTE FUNCTION public.enforce_same_organisation('pipelines', 'pipeline_id');


--

-- Name: lifecycle_maps trg_lifecycle_maps_account_id_tenant; Type: TRIGGER; Schema: public; Owner: -
--

CREATE TRIGGER trg_lifecycle_maps_account_id_tenant BEFORE INSERT OR UPDATE OF account_id, organisation_id ON public.lifecycle_maps FOR EACH ROW EXECUTE FUNCTION public.enforce_same_organisation('accounts', 'account_id');


--

-- Name: lifecycle_maps trg_lifecycle_maps_owner_team_id_tenant; Type: TRIGGER; Schema: public; Owner: -
--

CREATE TRIGGER trg_lifecycle_maps_owner_team_id_tenant BEFORE INSERT OR UPDATE OF owner_team_id, organisation_id ON public.lifecycle_maps FOR EACH ROW EXECUTE FUNCTION public.enforce_same_organisation('teams', 'owner_team_id');


--

-- Name: mcp_setup_tokens trg_mcp_setup_tokens_created_by_tenant; Type: TRIGGER; Schema: public; Owner: -
--

CREATE TRIGGER trg_mcp_setup_tokens_created_by_tenant BEFORE INSERT OR UPDATE OF created_by, organisation_id ON public.mcp_setup_tokens FOR EACH ROW EXECUTE FUNCTION public.enforce_same_organisation('accounts', 'created_by');


--

-- Name: model_backends trg_model_backends_account_id_tenant; Type: TRIGGER; Schema: public; Owner: -
--

CREATE TRIGGER trg_model_backends_account_id_tenant BEFORE INSERT OR UPDATE OF account_id, organisation_id ON public.model_backends FOR EACH ROW EXECUTE FUNCTION public.enforce_same_organisation('accounts', 'account_id');


--

-- Name: model_backends trg_model_backends_owner_team_id_tenant; Type: TRIGGER; Schema: public; Owner: -
--

CREATE TRIGGER trg_model_backends_owner_team_id_tenant BEFORE INSERT OR UPDATE OF owner_team_id, organisation_id ON public.model_backends FOR EACH ROW EXECUTE FUNCTION public.enforce_same_organisation('teams', 'owner_team_id');


--

-- Name: node_categories trg_node_categories_account_id_tenant; Type: TRIGGER; Schema: public; Owner: -
--

CREATE TRIGGER trg_node_categories_account_id_tenant BEFORE INSERT OR UPDATE OF account_id, organisation_id ON public.node_categories FOR EACH ROW EXECUTE FUNCTION public.enforce_same_organisation('accounts', 'account_id');


--

-- Name: node_observations trg_node_observations_account_id_tenant; Type: TRIGGER; Schema: public; Owner: -
--

CREATE TRIGGER trg_node_observations_account_id_tenant BEFORE INSERT OR UPDATE OF account_id, organisation_id ON public.node_observations FOR EACH ROW EXECUTE FUNCTION public.enforce_same_organisation('accounts', 'account_id');


--

-- Name: node_observations trg_node_observations_run_id_tenant; Type: TRIGGER; Schema: public; Owner: -
--

CREATE TRIGGER trg_node_observations_run_id_tenant BEFORE INSERT OR UPDATE OF run_id, organisation_id ON public.node_observations FOR EACH ROW EXECUTE FUNCTION public.enforce_same_organisation('runs', 'run_id');


--

-- Name: nodes trg_nodes_account_id_tenant; Type: TRIGGER; Schema: public; Owner: -
--

CREATE TRIGGER trg_nodes_account_id_tenant BEFORE INSERT OR UPDATE OF account_id, organisation_id ON public.nodes FOR EACH ROW EXECUTE FUNCTION public.enforce_same_organisation('accounts', 'account_id');


--

-- Name: nodes trg_nodes_parent_node_id_tenant; Type: TRIGGER; Schema: public; Owner: -
--

CREATE TRIGGER trg_nodes_parent_node_id_tenant BEFORE INSERT OR UPDATE OF parent_node_id, organisation_id ON public.nodes FOR EACH ROW EXECUTE FUNCTION public.enforce_same_organisation('nodes', 'parent_node_id');


--

-- Name: nodes trg_nodes_pipeline_id_tenant; Type: TRIGGER; Schema: public; Owner: -
--

CREATE TRIGGER trg_nodes_pipeline_id_tenant BEFORE INSERT OR UPDATE OF pipeline_id, organisation_id ON public.nodes FOR EACH ROW EXECUTE FUNCTION public.enforce_same_organisation('pipelines', 'pipeline_id');


--

-- Name: notification_delivery_log trg_notification_delivery_log_endpoint_id_tenant; Type: TRIGGER; Schema: public; Owner: -
--

CREATE TRIGGER trg_notification_delivery_log_endpoint_id_tenant BEFORE INSERT OR UPDATE OF endpoint_id, organisation_id ON public.notification_delivery_log FOR EACH ROW EXECUTE FUNCTION public.enforce_same_organisation('notification_endpoints', 'endpoint_id');


--

-- Name: notification_delivery_log trg_notification_delivery_log_run_id_tenant; Type: TRIGGER; Schema: public; Owner: -
--

CREATE TRIGGER trg_notification_delivery_log_run_id_tenant BEFORE INSERT OR UPDATE OF run_id, organisation_id ON public.notification_delivery_log FOR EACH ROW EXECUTE FUNCTION public.enforce_same_organisation('runs', 'run_id');


--

-- Name: notification_endpoints trg_notification_endpoints_account_id_tenant; Type: TRIGGER; Schema: public; Owner: -
--

CREATE TRIGGER trg_notification_endpoints_account_id_tenant BEFORE INSERT OR UPDATE OF account_id, organisation_id ON public.notification_endpoints FOR EACH ROW EXECUTE FUNCTION public.enforce_same_organisation('accounts', 'account_id');


--

-- Name: notification_endpoints trg_notification_endpoints_team_id_tenant; Type: TRIGGER; Schema: public; Owner: -
--

CREATE TRIGGER trg_notification_endpoints_team_id_tenant BEFORE INSERT OR UPDATE OF team_id, organisation_id ON public.notification_endpoints FOR EACH ROW EXECUTE FUNCTION public.enforce_same_organisation('teams', 'team_id');


--

-- Name: notifications trg_notifications_target_user_id_tenant; Type: TRIGGER; Schema: public; Owner: -
--

CREATE TRIGGER trg_notifications_target_user_id_tenant BEFORE INSERT OR UPDATE OF target_user_id, organisation_id ON public.notifications FOR EACH ROW EXECUTE FUNCTION public.enforce_same_organisation('accounts', 'target_user_id');


--

-- Name: oauth_clients trg_oauth_clients_account_id_tenant; Type: TRIGGER; Schema: public; Owner: -
--

CREATE TRIGGER trg_oauth_clients_account_id_tenant BEFORE INSERT OR UPDATE OF account_id, organisation_id ON public.oauth_clients FOR EACH ROW EXECUTE FUNCTION public.enforce_same_organisation('accounts', 'account_id');


--

-- Name: oauth_clients trg_oauth_clients_team_id_tenant; Type: TRIGGER; Schema: public; Owner: -
--

CREATE TRIGGER trg_oauth_clients_team_id_tenant BEFORE INSERT OR UPDATE OF team_id, organisation_id ON public.oauth_clients FOR EACH ROW EXECUTE FUNCTION public.enforce_same_organisation('teams', 'team_id');


--

-- Name: org_api_keys trg_org_api_keys_account_id_tenant; Type: TRIGGER; Schema: public; Owner: -
--

CREATE TRIGGER trg_org_api_keys_account_id_tenant BEFORE INSERT OR UPDATE OF account_id, organisation_id ON public.org_api_keys FOR EACH ROW EXECUTE FUNCTION public.enforce_same_organisation('accounts', 'account_id');


--

-- Name: org_api_keys trg_org_api_keys_team_id_tenant; Type: TRIGGER; Schema: public; Owner: -
--

CREATE TRIGGER trg_org_api_keys_team_id_tenant BEFORE INSERT OR UPDATE OF team_id, organisation_id ON public.org_api_keys FOR EACH ROW EXECUTE FUNCTION public.enforce_same_organisation('teams', 'team_id');


--

-- Name: org_daily_run_counts trg_org_daily_run_counts_team_id_tenant; Type: TRIGGER; Schema: public; Owner: -
--

CREATE TRIGGER trg_org_daily_run_counts_team_id_tenant BEFORE INSERT OR UPDATE OF team_id, organisation_id ON public.org_daily_run_counts FOR EACH ROW EXECUTE FUNCTION public.enforce_same_organisation('teams', 'team_id');


--

-- Name: org_memberships trg_org_memberships_account_id_tenant; Type: TRIGGER; Schema: public; Owner: -
--

CREATE TRIGGER trg_org_memberships_account_id_tenant BEFORE INSERT OR UPDATE OF account_id, organisation_id ON public.org_memberships FOR EACH ROW EXECUTE FUNCTION public.enforce_same_organisation('accounts', 'account_id');


--

-- Name: org_memberships trg_org_memberships_service_role_ceiling; Type: TRIGGER; Schema: public; Owner: -
--

CREATE TRIGGER trg_org_memberships_service_role_ceiling BEFORE INSERT OR UPDATE OF role ON public.org_memberships FOR EACH ROW EXECUTE FUNCTION public.enforce_service_role_ceiling();


--

-- Name: pipeline_edges trg_pipeline_edges_pipeline_id_tenant; Type: TRIGGER; Schema: public; Owner: -
--

CREATE TRIGGER trg_pipeline_edges_pipeline_id_tenant BEFORE INSERT OR UPDATE OF pipeline_id, organisation_id ON public.pipeline_edges FOR EACH ROW EXECUTE FUNCTION public.enforce_same_organisation('pipelines', 'pipeline_id');


--

-- Name: pipeline_snapshots trg_pipeline_snapshots_account_id_tenant; Type: TRIGGER; Schema: public; Owner: -
--

CREATE TRIGGER trg_pipeline_snapshots_account_id_tenant BEFORE INSERT OR UPDATE OF account_id, organisation_id ON public.pipeline_snapshots FOR EACH ROW EXECUTE FUNCTION public.enforce_same_organisation('accounts', 'account_id');


--

-- Name: pipeline_snapshots trg_pipeline_snapshots_environment_profile_id_tenant; Type: TRIGGER; Schema: public; Owner: -
--

CREATE TRIGGER trg_pipeline_snapshots_environment_profile_id_tenant BEFORE INSERT OR UPDATE OF environment_profile_id, organisation_id ON public.pipeline_snapshots FOR EACH ROW EXECUTE FUNCTION public.enforce_same_organisation('environment_profiles', 'environment_profile_id');


--

-- Name: pipeline_snapshots trg_pipeline_snapshots_pipeline_id_tenant; Type: TRIGGER; Schema: public; Owner: -
--

CREATE TRIGGER trg_pipeline_snapshots_pipeline_id_tenant BEFORE INSERT OR UPDATE OF pipeline_id, organisation_id ON public.pipeline_snapshots FOR EACH ROW EXECUTE FUNCTION public.enforce_same_organisation('pipelines', 'pipeline_id');


--

-- Name: pipelines trg_pipelines_account_id_tenant; Type: TRIGGER; Schema: public; Owner: -
--

CREATE TRIGGER trg_pipelines_account_id_tenant BEFORE INSERT OR UPDATE OF account_id, organisation_id ON public.pipelines FOR EACH ROW EXECUTE FUNCTION public.enforce_same_organisation('accounts', 'account_id');


--

-- Name: pipelines trg_pipelines_environment_profile_id_tenant; Type: TRIGGER; Schema: public; Owner: -
--

CREATE TRIGGER trg_pipelines_environment_profile_id_tenant BEFORE INSERT OR UPDATE OF environment_profile_id, organisation_id ON public.pipelines FOR EACH ROW EXECUTE FUNCTION public.enforce_same_organisation('environment_profiles', 'environment_profile_id');


--

-- Name: pipelines trg_pipelines_owner_team_id_tenant; Type: TRIGGER; Schema: public; Owner: -
--

CREATE TRIGGER trg_pipelines_owner_team_id_tenant BEFORE INSERT OR UPDATE OF owner_team_id, organisation_id ON public.pipelines FOR EACH ROW EXECUTE FUNCTION public.enforce_same_organisation('teams', 'owner_team_id');


--

-- Name: primitive_abuse_reports trg_primitive_abuse_reports_primitive_id_tenant; Type: TRIGGER; Schema: public; Owner: -
--

CREATE TRIGGER trg_primitive_abuse_reports_primitive_id_tenant BEFORE INSERT OR UPDATE OF primitive_id, organisation_id ON public.primitive_abuse_reports FOR EACH ROW EXECUTE FUNCTION public.enforce_same_organisation('library_primitives', 'primitive_id');


--

-- Name: primitive_abuse_reports trg_primitive_abuse_reports_rating_id_tenant; Type: TRIGGER; Schema: public; Owner: -
--

CREATE TRIGGER trg_primitive_abuse_reports_rating_id_tenant BEFORE INSERT OR UPDATE OF rating_id, organisation_id ON public.primitive_abuse_reports FOR EACH ROW EXECUTE FUNCTION public.enforce_same_organisation('primitive_ratings', 'rating_id');


--

-- Name: primitive_abuse_reports trg_primitive_abuse_reports_reporter_account_id_tenant; Type: TRIGGER; Schema: public; Owner: -
--

CREATE TRIGGER trg_primitive_abuse_reports_reporter_account_id_tenant BEFORE INSERT OR UPDATE OF reporter_account_id, organisation_id ON public.primitive_abuse_reports FOR EACH ROW EXECUTE FUNCTION public.enforce_same_organisation('accounts', 'reporter_account_id');


--

-- Name: primitive_abuse_reports trg_primitive_abuse_reports_reviewer_account_id_tenant; Type: TRIGGER; Schema: public; Owner: -
--

CREATE TRIGGER trg_primitive_abuse_reports_reviewer_account_id_tenant BEFORE INSERT OR UPDATE OF reviewer_account_id, organisation_id ON public.primitive_abuse_reports FOR EACH ROW EXECUTE FUNCTION public.enforce_same_organisation('accounts', 'reviewer_account_id');


--

-- Name: primitive_ratings trg_primitive_ratings_account_id_tenant; Type: TRIGGER; Schema: public; Owner: -
--

CREATE TRIGGER trg_primitive_ratings_account_id_tenant BEFORE INSERT OR UPDATE OF account_id, organisation_id ON public.primitive_ratings FOR EACH ROW EXECUTE FUNCTION public.enforce_same_organisation('accounts', 'account_id');


--

-- Name: primitive_ratings trg_primitive_ratings_primitive_id_tenant; Type: TRIGGER; Schema: public; Owner: -
--

CREATE TRIGGER trg_primitive_ratings_primitive_id_tenant BEFORE INSERT OR UPDATE OF primitive_id, organisation_id ON public.primitive_ratings FOR EACH ROW EXECUTE FUNCTION public.enforce_same_organisation('library_primitives', 'primitive_id');


--

-- Name: runs trg_runs_account_id_tenant; Type: TRIGGER; Schema: public; Owner: -
--

CREATE TRIGGER trg_runs_account_id_tenant BEFORE INSERT OR UPDATE OF account_id, organisation_id ON public.runs FOR EACH ROW EXECUTE FUNCTION public.enforce_same_organisation('accounts', 'account_id');


--

-- Name: runs trg_runs_owner_team_id_tenant; Type: TRIGGER; Schema: public; Owner: -
--

CREATE TRIGGER trg_runs_owner_team_id_tenant BEFORE INSERT OR UPDATE OF owner_team_id, organisation_id ON public.runs FOR EACH ROW EXECUTE FUNCTION public.enforce_same_organisation('teams', 'owner_team_id');


--

-- Name: runs trg_runs_parent_run_id_tenant; Type: TRIGGER; Schema: public; Owner: -
--

CREATE TRIGGER trg_runs_parent_run_id_tenant BEFORE INSERT OR UPDATE OF parent_run_id, organisation_id ON public.runs FOR EACH ROW EXECUTE FUNCTION public.enforce_same_organisation('runs', 'parent_run_id');


--

-- Name: runs trg_runs_pipeline_id_tenant; Type: TRIGGER; Schema: public; Owner: -
--

CREATE TRIGGER trg_runs_pipeline_id_tenant BEFORE INSERT OR UPDATE OF pipeline_id, organisation_id ON public.runs FOR EACH ROW EXECUTE FUNCTION public.enforce_same_organisation('pipelines', 'pipeline_id');


--

-- Name: runs trg_runs_snapshot_id_tenant; Type: TRIGGER; Schema: public; Owner: -
--

CREATE TRIGGER trg_runs_snapshot_id_tenant BEFORE INSERT OR UPDATE OF snapshot_id, organisation_id ON public.runs FOR EACH ROW EXECUTE FUNCTION public.enforce_same_organisation('pipeline_snapshots', 'snapshot_id');


--

-- Name: runs trg_runs_trigger_id_tenant; Type: TRIGGER; Schema: public; Owner: -
--

CREATE TRIGGER trg_runs_trigger_id_tenant BEFORE INSERT OR UPDATE OF trigger_id, organisation_id ON public.runs FOR EACH ROW EXECUTE FUNCTION public.enforce_same_organisation('triggers', 'trigger_id');


--

-- Name: saved_views trg_saved_views_account_id_tenant; Type: TRIGGER; Schema: public; Owner: -
--

CREATE TRIGGER trg_saved_views_account_id_tenant BEFORE INSERT OR UPDATE OF account_id, organisation_id ON public.saved_views FOR EACH ROW EXECUTE FUNCTION public.enforce_same_organisation('accounts', 'account_id');


--

-- Name: scheduled_reports trg_scheduled_reports_created_by_tenant; Type: TRIGGER; Schema: public; Owner: -
--

CREATE TRIGGER trg_scheduled_reports_created_by_tenant BEFORE INSERT OR UPDATE OF created_by, organisation_id ON public.scheduled_reports FOR EACH ROW EXECUTE FUNCTION public.enforce_same_organisation('accounts', 'created_by');


--

-- Name: schema_versions trg_schema_versions_account_id_tenant; Type: TRIGGER; Schema: public; Owner: -
--

CREATE TRIGGER trg_schema_versions_account_id_tenant BEFORE INSERT OR UPDATE OF account_id, organisation_id ON public.schema_versions FOR EACH ROW EXECUTE FUNCTION public.enforce_same_organisation('accounts', 'account_id');


--

-- Name: schema_versions trg_schema_versions_schema_id_tenant; Type: TRIGGER; Schema: public; Owner: -
--

CREATE TRIGGER trg_schema_versions_schema_id_tenant BEFORE INSERT OR UPDATE OF schema_id, organisation_id ON public.schema_versions FOR EACH ROW EXECUTE FUNCTION public.enforce_same_organisation('schemas', 'schema_id');


--

-- Name: schemas trg_schemas_account_id_tenant; Type: TRIGGER; Schema: public; Owner: -
--

CREATE TRIGGER trg_schemas_account_id_tenant BEFORE INSERT OR UPDATE OF account_id, organisation_id ON public.schemas FOR EACH ROW EXECUTE FUNCTION public.enforce_same_organisation('accounts', 'account_id');


--

-- Name: spend_anomalies trg_spend_anomalies_pipeline_id_tenant; Type: TRIGGER; Schema: public; Owner: -
--

CREATE TRIGGER trg_spend_anomalies_pipeline_id_tenant BEFORE INSERT OR UPDATE OF pipeline_id, organisation_id ON public.spend_anomalies FOR EACH ROW EXECUTE FUNCTION public.enforce_same_organisation('pipelines', 'pipeline_id');


--

-- Name: system_map_attribution_bindings trg_system_map_attribution_bindings_pipeline_id_tenant; Type: TRIGGER; Schema: public; Owner: -
--

CREATE TRIGGER trg_system_map_attribution_bindings_pipeline_id_tenant BEFORE INSERT OR UPDATE OF pipeline_id, organisation_id ON public.system_map_attribution_bindings FOR EACH ROW EXECUTE FUNCTION public.enforce_same_organisation('pipelines', 'pipeline_id');


--

-- Name: system_map_attribution_bindings trg_system_map_attribution_bindings_trigger_id_tenant; Type: TRIGGER; Schema: public; Owner: -
--

CREATE TRIGGER trg_system_map_attribution_bindings_trigger_id_tenant BEFORE INSERT OR UPDATE OF trigger_id, organisation_id ON public.system_map_attribution_bindings FOR EACH ROW EXECUTE FUNCTION public.enforce_same_organisation('triggers', 'trigger_id');


--

-- Name: system_map_loop_configs trg_system_map_loop_configs_action_pipeline_id_tenant; Type: TRIGGER; Schema: public; Owner: -
--

CREATE TRIGGER trg_system_map_loop_configs_action_pipeline_id_tenant BEFORE INSERT OR UPDATE OF action_pipeline_id, organisation_id ON public.system_map_loop_configs FOR EACH ROW EXECUTE FUNCTION public.enforce_same_organisation('pipelines', 'action_pipeline_id');


--

-- Name: system_map_loop_reservations trg_system_map_loop_reservations_run_id_tenant; Type: TRIGGER; Schema: public; Owner: -
--

CREATE TRIGGER trg_system_map_loop_reservations_run_id_tenant BEFORE INSERT OR UPDATE OF run_id, organisation_id ON public.system_map_loop_reservations FOR EACH ROW EXECUTE FUNCTION public.enforce_system_map_reservation_run();


--

-- Name: system_map_nodes trg_system_map_nodes_connector_instance_id_tenant; Type: TRIGGER; Schema: public; Owner: -
--

CREATE TRIGGER trg_system_map_nodes_connector_instance_id_tenant BEFORE INSERT OR UPDATE OF connector_instance_id, organisation_id ON public.system_map_nodes FOR EACH ROW EXECUTE FUNCTION public.enforce_same_organisation('connector_instances', 'connector_instance_id');


--

-- Name: system_map_nodes trg_system_map_nodes_eval_id_tenant; Type: TRIGGER; Schema: public; Owner: -
--

CREATE TRIGGER trg_system_map_nodes_eval_id_tenant BEFORE INSERT OR UPDATE OF eval_id, organisation_id ON public.system_map_nodes FOR EACH ROW EXECUTE FUNCTION public.enforce_same_organisation('evals', 'eval_id');


--

-- Name: system_map_nodes trg_system_map_nodes_owner_team_id_tenant; Type: TRIGGER; Schema: public; Owner: -
--

CREATE TRIGGER trg_system_map_nodes_owner_team_id_tenant BEFORE INSERT OR UPDATE OF owner_team_id, organisation_id ON public.system_map_nodes FOR EACH ROW EXECUTE FUNCTION public.enforce_same_organisation('teams', 'owner_team_id');


--

-- Name: system_map_nodes trg_system_map_nodes_pipeline_id_tenant; Type: TRIGGER; Schema: public; Owner: -
--

CREATE TRIGGER trg_system_map_nodes_pipeline_id_tenant BEFORE INSERT OR UPDATE OF pipeline_id, organisation_id ON public.system_map_nodes FOR EACH ROW EXECUTE FUNCTION public.enforce_same_organisation('pipelines', 'pipeline_id');


--

-- Name: system_maps trg_system_maps_owner_team_tenant; Type: TRIGGER; Schema: public; Owner: -
--

CREATE TRIGGER trg_system_maps_owner_team_tenant BEFORE INSERT OR UPDATE OF owner_team_id, organisation_id, visibility ON public.system_maps FOR EACH ROW EXECUTE FUNCTION public.enforce_system_map_owner_team();


--

-- Name: team_memberships trg_team_memberships_account_id_tenant; Type: TRIGGER; Schema: public; Owner: -
--

CREATE TRIGGER trg_team_memberships_account_id_tenant BEFORE INSERT OR UPDATE OF account_id, organisation_id ON public.team_memberships FOR EACH ROW EXECUTE FUNCTION public.enforce_same_organisation('accounts', 'account_id');


--

-- Name: team_memberships trg_team_memberships_team_id_tenant; Type: TRIGGER; Schema: public; Owner: -
--

CREATE TRIGGER trg_team_memberships_team_id_tenant BEFORE INSERT OR UPDATE OF team_id, organisation_id ON public.team_memberships FOR EACH ROW EXECUTE FUNCTION public.enforce_same_organisation('teams', 'team_id');


--

-- Name: team_memberships trg_team_privilege_cap; Type: TRIGGER; Schema: public; Owner: -
--

CREATE TRIGGER trg_team_privilege_cap BEFORE INSERT OR UPDATE ON public.team_memberships FOR EACH ROW EXECUTE FUNCTION public.check_team_privilege_cap();


--

-- Name: teams trg_teams_account_id_tenant; Type: TRIGGER; Schema: public; Owner: -
--

CREATE TRIGGER trg_teams_account_id_tenant BEFORE INSERT OR UPDATE OF account_id, organisation_id ON public.teams FOR EACH ROW EXECUTE FUNCTION public.enforce_same_organisation('accounts', 'account_id');


--

-- Name: token_families trg_token_families_account_id_tenant; Type: TRIGGER; Schema: public; Owner: -
--

CREATE TRIGGER trg_token_families_account_id_tenant BEFORE INSERT OR UPDATE OF account_id, organisation_id ON public.token_families FOR EACH ROW EXECUTE FUNCTION public.enforce_same_organisation('accounts', 'account_id');


--

-- Name: trigger_events trg_trigger_events_run_id_tenant; Type: TRIGGER; Schema: public; Owner: -
--

CREATE TRIGGER trg_trigger_events_run_id_tenant BEFORE INSERT OR UPDATE OF run_id, organisation_id ON public.trigger_events FOR EACH ROW EXECUTE FUNCTION public.enforce_same_organisation('runs', 'run_id');


--

-- Name: trigger_events trg_trigger_events_trigger_id_tenant; Type: TRIGGER; Schema: public; Owner: -
--

CREATE TRIGGER trg_trigger_events_trigger_id_tenant BEFORE INSERT OR UPDATE OF trigger_id, organisation_id ON public.trigger_events FOR EACH ROW EXECUTE FUNCTION public.enforce_same_organisation('triggers', 'trigger_id');


--

-- Name: triggers trg_triggers_account_id_tenant; Type: TRIGGER; Schema: public; Owner: -
--

CREATE TRIGGER trg_triggers_account_id_tenant BEFORE INSERT OR UPDATE OF account_id, organisation_id ON public.triggers FOR EACH ROW EXECUTE FUNCTION public.enforce_same_organisation('accounts', 'account_id');


--

-- Name: triggers trg_triggers_pipeline_id_tenant; Type: TRIGGER; Schema: public; Owner: -
--

CREATE TRIGGER trg_triggers_pipeline_id_tenant BEFORE INSERT OR UPDATE OF pipeline_id, organisation_id ON public.triggers FOR EACH ROW EXECUTE FUNCTION public.enforce_same_organisation('pipelines', 'pipeline_id');


--

-- Name: variant_groups trg_variant_groups_pipeline_id_tenant; Type: TRIGGER; Schema: public; Owner: -
--

CREATE TRIGGER trg_variant_groups_pipeline_id_tenant BEFORE INSERT OR UPDATE OF pipeline_id, organisation_id ON public.variant_groups FOR EACH ROW EXECUTE FUNCTION public.enforce_same_organisation('pipelines', 'pipeline_id');


--

-- Name: webhook_dedup_hashes trg_webhook_dedup_hashes_trigger_id_tenant; Type: TRIGGER; Schema: public; Owner: -
--

CREATE TRIGGER trg_webhook_dedup_hashes_trigger_id_tenant BEFORE INSERT OR UPDATE OF trigger_id, organisation_id ON public.webhook_dedup_hashes FOR EACH ROW EXECUTE FUNCTION public.enforce_same_organisation('triggers', 'trigger_id');


--

-- Name: webhook_payloads trg_webhook_payloads_trigger_event_id_tenant; Type: TRIGGER; Schema: public; Owner: -
--

CREATE TRIGGER trg_webhook_payloads_trigger_event_id_tenant BEFORE INSERT OR UPDATE OF trigger_event_id, organisation_id ON public.webhook_payloads FOR EACH ROW EXECUTE FUNCTION public.enforce_same_organisation('trigger_events', 'trigger_event_id');


--""")


def downgrade() -> None:
    op.execute("""""")
