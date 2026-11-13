"""Farna Control native baseline migration 0350_baseline_schema."""

from alembic import op

revision: str = "0350_baseline_schema"
down_revision: str | None = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""SET search_path TO public;

CREATE TABLE IF NOT EXISTS public.alembic_version (version_num varchar(255) NOT NULL PRIMARY KEY);

-- Name: modulo_internal; Type: SCHEMA; Schema: -; Owner: -
--

CREATE SCHEMA modulo_internal;


--

-- Name: _migration_0191_repoint_state; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public._migration_0191_repoint_state (
    profile_id uuid NOT NULL,
    prev_name text NOT NULL,
    prev_description text,
    prev_config_json jsonb
);


--

-- Name: accounts; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.accounts (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    email character varying(320),
    display_name character varying(255) NOT NULL,
    password_hash character varying(255),
    auth_provider character varying(20) DEFAULT 'local'::character varying,
    sso_subject character varying(512),
    active boolean DEFAULT true NOT NULL,
    last_login timestamp with time zone,
    is_system_admin boolean DEFAULT false NOT NULL,
    created_at timestamp with time zone DEFAULT CURRENT_TIMESTAMP NOT NULL,
    updated_at timestamp with time zone DEFAULT CURRENT_TIMESTAMP NOT NULL,
    is_break_glass boolean DEFAULT false NOT NULL,
    break_glass_expires_at timestamp with time zone,
    break_glass_deactivated_at timestamp with time zone,
    preferences jsonb DEFAULT '{}'::json NOT NULL,
    must_change_password boolean DEFAULT false NOT NULL,
    kind character varying(16) DEFAULT 'human'::character varying NOT NULL,
    decommissioned_at timestamp with time zone,
    CONSTRAINT ck_accounts_auth_provider CHECK (((auth_provider)::text = ANY ((ARRAY['local'::character varying, 'oidc'::character varying, 'saml'::character varying, 'scim'::character varying])::text[]))),
    CONSTRAINT ck_accounts_break_glass_expiry CHECK (((NOT is_break_glass) OR (break_glass_expires_at IS NOT NULL) OR (break_glass_deactivated_at IS NOT NULL))),
    CONSTRAINT ck_accounts_decommissioned_service CHECK (((decommissioned_at IS NULL) OR ((kind)::text = 'service'::text))),
    CONSTRAINT ck_accounts_kind CHECK (((kind)::text = ANY ((ARRAY['human'::character varying, 'service'::character varying])::text[]))),
    CONSTRAINT ck_accounts_service_identity CHECK ((((kind)::text = 'human'::text) OR ((email IS NULL) AND (password_hash IS NULL) AND (sso_subject IS NULL)))),
    CONSTRAINT ck_accounts_service_privilege CHECK ((((kind)::text = 'human'::text) OR ((NOT is_system_admin) AND (NOT is_break_glass))))
);


--

-- Name: COLUMN accounts.kind; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.accounts.kind IS 'Principal kind: ''human'' (default) or ''service''';


--

-- Name: agent_runner_bindings; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.agent_runner_bindings (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    organisation_id uuid NOT NULL,
    agent_id uuid NOT NULL,
    model_backend_id uuid NOT NULL,
    target_env_var character varying(128) NOT NULL,
    source_field character varying(64) NOT NULL,
    account_id uuid NOT NULL,
    created_at timestamp with time zone DEFAULT CURRENT_TIMESTAMP NOT NULL,
    updated_at timestamp with time zone DEFAULT CURRENT_TIMESTAMP NOT NULL
);



--

-- Name: agents; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.agents (
    is_executable boolean NOT NULL,
    prompt_always_visible boolean NOT NULL,
    name character varying(255) NOT NULL,
    description character varying(2000),
    input_schema_id uuid,
    input_schema_version character varying(50),
    output_schema_id uuid,
    output_schema_version character varying(50),
    prompt_template text NOT NULL,
    model_backend_id uuid,
    max_input_length integer,
    token_budget integer,
    library_id uuid,
    account_id uuid NOT NULL,
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    organisation_id uuid NOT NULL,
    created_at timestamp with time zone DEFAULT CURRENT_TIMESTAMP NOT NULL,
    updated_at timestamp with time zone DEFAULT CURRENT_TIMESTAMP NOT NULL,
    parameter_schema_id uuid,
    template_id uuid,
    deleted_at timestamp with time zone,
    deleted_by uuid,
    created_by uuid,
    updated_by uuid,
    agent_commands jsonb,
    prompt_version_history jsonb NOT NULL,
    connector_type_refs jsonb NOT NULL,
    required_environment_capabilities jsonb NOT NULL,
    evals jsonb,
    retry_policy jsonb NOT NULL,
    collection_install_id uuid,
    schema_profile character varying(30),
    CONSTRAINT ck_agents_max_input_length CHECK (((max_input_length IS NULL) OR (max_input_length > 0))),
    CONSTRAINT ck_agents_schema_profile CHECK (((schema_profile IS NULL) OR ((schema_profile)::text = ANY ((ARRAY['verbatim'::character varying, 'provider-strict'::character varying, 'runtime-sdk'::character varying])::text[])))),
    CONSTRAINT ck_agents_token_budget CHECK (((token_budget IS NULL) OR (token_budget > 0)))
);



--

-- Name: assistant_context_sources; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.assistant_context_sources (
    organisation_id uuid,
    account_id uuid,
    source_key character varying(64) NOT NULL,
    source_mode character varying(16) DEFAULT 'always_on'::character varying NOT NULL,
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    created_at timestamp with time zone DEFAULT CURRENT_TIMESTAMP NOT NULL,
    updated_at timestamp with time zone DEFAULT CURRENT_TIMESTAMP NOT NULL,
    CONSTRAINT ck_assistant_context_sources_mode CHECK (((source_mode)::text = ANY ((ARRAY['always_on'::character varying, 'tool'::character varying, 'off'::character varying])::text[]))),
    CONSTRAINT ck_assistant_context_sources_owner CHECK ((((organisation_id IS NOT NULL) AND (account_id IS NULL)) OR ((organisation_id IS NULL) AND (account_id IS NOT NULL))))
);


--

-- Name: assistant_skills; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.assistant_skills (
    organisation_id uuid,
    account_id uuid,
    name character varying(100) NOT NULL,
    description text,
    body text NOT NULL,
    active boolean DEFAULT true NOT NULL,
    source_mode character varying(16),
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    created_at timestamp with time zone DEFAULT CURRENT_TIMESTAMP NOT NULL,
    updated_at timestamp with time zone DEFAULT CURRENT_TIMESTAMP NOT NULL,
    triggers jsonb,
    CONSTRAINT ck_assistant_skills_owner CHECK ((((organisation_id IS NOT NULL) AND (account_id IS NULL)) OR ((organisation_id IS NULL) AND (account_id IS NOT NULL))))
);


--

-- Name: audit_chain_heads; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.audit_chain_heads (
    organisation_id uuid NOT NULL,
    last_event_hash text NOT NULL,
    last_event_id uuid,
    event_count integer DEFAULT 0 NOT NULL,
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    CONSTRAINT ck_audit_event_event_count CHECK ((event_count >= 0))
);


--

-- Name: audit_events; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.audit_events (
    event_type character varying(100) NOT NULL,
    account_id uuid,
    resource_type character varying(100),
    resource_id uuid,
    request_id character varying(255),
    previous_hash text,
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    organisation_id uuid NOT NULL,
    created_at timestamp with time zone DEFAULT CURRENT_TIMESTAMP NOT NULL,
    updated_at timestamp with time zone DEFAULT CURRENT_TIMESTAMP NOT NULL,
    payload_json jsonb NOT NULL
);


--

-- Name: chat_messages; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.chat_messages (
    session_id uuid NOT NULL,
    role character varying(20) NOT NULL,
    content text,
    token_count integer,
    parent_id uuid,
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    organisation_id uuid NOT NULL,
    created_at timestamp with time zone DEFAULT CURRENT_TIMESTAMP NOT NULL,
    tool_calls_json jsonb,
    tool_results_json jsonb,
    CONSTRAINT ck_chat_messages_role CHECK (((role)::text = ANY ((ARRAY['user'::character varying, 'assistant'::character varying, 'tool_use'::character varying, 'tool_result'::character varying, 'summary'::character varying])::text[])))
);


--

-- Name: chat_sessions; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.chat_sessions (
    account_id uuid NOT NULL,
    name character varying(255),
    provider character varying(50) NOT NULL,
    model character varying(100) NOT NULL,
    context_window_tokens integer NOT NULL,
    system_prompt_hash character varying(64),
    session_number integer NOT NULL,
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    organisation_id uuid NOT NULL,
    created_at timestamp with time zone DEFAULT CURRENT_TIMESTAMP NOT NULL,
    updated_at timestamp with time zone DEFAULT CURRENT_TIMESTAMP NOT NULL
);


--

-- Name: collection_install; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.collection_install (
    install_id uuid DEFAULT gen_random_uuid() NOT NULL,
    collection_id uuid NOT NULL,
    collection_version character varying(50),
    organisation_id uuid NOT NULL,
    status character varying(20) NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    resolved_manifest jsonb,
    connector_checklist jsonb,
    community_sourced boolean DEFAULT false NOT NULL,
    agents_granted boolean DEFAULT false NOT NULL,
    deleted_at timestamp with time zone,
    created_by uuid,
    CONSTRAINT ck_collection_install_status CHECK (((status)::text = ANY ((ARRAY['installed'::character varying, 'failed'::character varying])::text[])))
);



--

-- Name: collection_install_entity; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.collection_install_entity (
    install_id uuid NOT NULL,
    entity_type character varying(50) NOT NULL,
    entity_id uuid NOT NULL
);



--

-- Name: composite_templates; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.composite_templates (
    name character varying(255) NOT NULL,
    description text,
    input_schema_id uuid,
    output_schema_id uuid,
    version character varying(50) NOT NULL,
    account_id uuid NOT NULL,
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    organisation_id uuid NOT NULL,
    created_at timestamp with time zone DEFAULT CURRENT_TIMESTAMP NOT NULL,
    updated_at timestamp with time zone DEFAULT CURRENT_TIMESTAMP NOT NULL,
    parameter_schema_id uuid,
    deleted_at timestamp with time zone,
    sub_pipeline_graph_json jsonb NOT NULL,
    parameter_ports_json jsonb NOT NULL
);


--

-- Name: connector_instances; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.connector_instances (
    name character varying(255) NOT NULL,
    connector_type_id character varying(255) NOT NULL,
    account_id uuid NOT NULL,
    owner_team_id uuid,
    visibility character varying(10) DEFAULT 'org'::character varying NOT NULL,
    credentials_ciphertext bytea NOT NULL,
    status character varying(30) DEFAULT 'active'::character varying NOT NULL,
    last_health_check_at timestamp with time zone,
    last_health_check_error character varying(2000),
    tier character varying(20) DEFAULT 'native'::character varying NOT NULL,
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    organisation_id uuid NOT NULL,
    created_at timestamp with time zone DEFAULT CURRENT_TIMESTAMP NOT NULL,
    updated_at timestamp with time zone DEFAULT CURRENT_TIMESTAMP NOT NULL,
    deleted_at timestamp with time zone,
    deleted_by uuid,
    created_by uuid,
    updated_by uuid,
    config_json jsonb NOT NULL,
    allowed_operations jsonb NOT NULL,
    degraded_at timestamp with time zone,
    last_skip_error character varying(2000),
    validation_level character varying(30),
    CONSTRAINT ck_connector_instances_status CHECK (((status)::text = ANY (ARRAY[('active'::character varying)::text, ('disabled'::character varying)::text]))),
    CONSTRAINT ck_connector_instances_team_owner CHECK ((((visibility)::text = 'org'::text) OR (owner_team_id IS NOT NULL))),
    CONSTRAINT ck_connector_instances_tier CHECK (((tier)::text = ANY ((ARRAY['native'::character varying, 'preview'::character varying, 'in_dev'::character varying])::text[]))),
    CONSTRAINT ck_connector_instances_visibility CHECK (((visibility)::text = ANY ((ARRAY['org'::character varying, 'team'::character varying])::text[])))
);


--

-- Name: connector_profiles; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.connector_profiles (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    organisation_id uuid NOT NULL,
    connector_instance_id uuid NOT NULL,
    auth_mode character varying(20) NOT NULL,
    auth_in character varying(10),
    auth_query_param_name character varying(128),
    idempotent boolean DEFAULT false NOT NULL,
    idempotency_key_header character varying(128),
    response_max_bytes integer,
    timeout_seconds double precision,
    verify_tls boolean DEFAULT true NOT NULL,
    created_at timestamp with time zone DEFAULT CURRENT_TIMESTAMP NOT NULL,
    updated_at timestamp with time zone DEFAULT CURRENT_TIMESTAMP NOT NULL,
    CONSTRAINT ck_connector_profiles_auth_header_inverse CHECK ((((auth_in)::text <> 'header'::text) OR (auth_query_param_name IS NULL))),
    CONSTRAINT ck_connector_profiles_auth_in CHECK (((auth_in IS NULL) OR ((auth_in)::text = ANY ((ARRAY['header'::character varying, 'query'::character varying])::text[])))),
    CONSTRAINT ck_connector_profiles_auth_mode CHECK (((auth_mode)::text = ANY ((ARRAY['bearer'::character varying, 'api_key'::character varying, 'basic'::character varying])::text[]))),
    CONSTRAINT ck_connector_profiles_auth_query_param CHECK ((((auth_in)::text <> 'query'::text) OR (auth_query_param_name IS NOT NULL))),
    CONSTRAINT ck_connector_profiles_response_max_bytes_positive CHECK (((response_max_bytes IS NULL) OR (response_max_bytes > 0))),
    CONSTRAINT ck_connector_profiles_timeout_positive CHECK (((timeout_seconds IS NULL) OR (timeout_seconds > (0)::double precision)))
);



--

-- Name: cost_components; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.cost_components (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    organisation_id uuid NOT NULL,
    created_at timestamp with time zone DEFAULT CURRENT_TIMESTAMP NOT NULL,
    updated_at timestamp with time zone DEFAULT CURRENT_TIMESTAMP NOT NULL,
    deleted_at timestamp with time zone,
    name character varying(64) NOT NULL,
    display_name character varying(128) NOT NULL,
    kind character varying(20) NOT NULL,
    rate_usd numeric(18,6),
    rate_fallback character varying(32),
    formula character varying(256),
    report_key character varying(64),
    enabled boolean DEFAULT true NOT NULL,
    sort_order integer DEFAULT 0 NOT NULL,
    CONSTRAINT ck_cost_component_sort_order CHECK ((sort_order >= 0)),
    CONSTRAINT ck_cost_components_kind CHECK (((kind)::text = ANY (ARRAY[('calculated'::character varying)::text, ('self_reported'::character varying)::text])))
);


--

-- Name: deleted_defaults; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.deleted_defaults (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    organisation_id uuid NOT NULL,
    signal character varying(100) NOT NULL,
    created_at timestamp with time zone DEFAULT CURRENT_TIMESTAMP NOT NULL,
    updated_at timestamp with time zone DEFAULT CURRENT_TIMESTAMP NOT NULL,
    CONSTRAINT ck_deleted_defaults_signal_nonempty CHECK (((signal)::text <> ''::text))
);


--

-- Name: dismissals; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.dismissals (
    notification_id uuid NOT NULL,
    dismissed_by_user_id uuid NOT NULL,
    dismiss_scope character varying(20) NOT NULL,
    dismissed_at timestamp with time zone DEFAULT CURRENT_TIMESTAMP NOT NULL,
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    organisation_id uuid NOT NULL,
    CONSTRAINT ck_dismissals_scope CHECK (((dismiss_scope)::text = ANY ((ARRAY['self'::character varying, 'scope'::character varying])::text[])))
);



--

-- Name: environment_profiles; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.environment_profiles (
    name character varying(255) NOT NULL,
    description text,
    provider_type character varying(50) NOT NULL,
    image_ref character varying(500),
    network_policy character varying(20) DEFAULT 'outbound'::character varying NOT NULL,
    initialisation_strategy character varying(30) DEFAULT 'git_clone'::character varying NOT NULL,
    persistence_policy character varying(20) DEFAULT 'ephemeral'::character varying NOT NULL,
    status character varying(30) DEFAULT 'active'::character varying NOT NULL,
    account_id uuid NOT NULL,
    owner_team_id uuid,
    visibility character varying(10) DEFAULT 'org'::character varying NOT NULL,
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    organisation_id uuid NOT NULL,
    created_at timestamp with time zone DEFAULT CURRENT_TIMESTAMP NOT NULL,
    updated_at timestamp with time zone DEFAULT CURRENT_TIMESTAMP NOT NULL,
    deleted_at timestamp with time zone,
    capabilities_json jsonb NOT NULL,
    config_json jsonb NOT NULL,
    secret_refs_json jsonb NOT NULL,
    workspace_inputs json DEFAULT '[]'::json,
    max_node_seconds integer DEFAULT 3300 NOT NULL,
    CONSTRAINT ck_env_profiles_initialisation_strategy CHECK (((initialisation_strategy)::text = ANY ((ARRAY['blank'::character varying, 'git_clone'::character varying, 'managed_inputs'::character varying, 'worktree'::character varying])::text[]))),
    CONSTRAINT ck_env_profiles_network_policy CHECK (((network_policy)::text = ANY ((ARRAY['none'::character varying, 'outbound'::character varying, 'selected'::character varying])::text[]))),
    CONSTRAINT ck_env_profiles_persistence_policy CHECK (((persistence_policy)::text = ANY ((ARRAY['ephemeral'::character varying, 'retained'::character varying, 'cache'::character varying])::text[]))),
    CONSTRAINT ck_env_profiles_provider_type CHECK (((provider_type)::text = ANY (ARRAY[('e2b'::character varying)::text, ('kubernetes'::character varying)::text, ('local'::character varying)::text, ('local_docker'::character varying)::text, ('runner_docker'::character varying)::text]))),
    CONSTRAINT ck_env_profiles_visibility CHECK (((visibility)::text = ANY ((ARRAY['org'::character varying, 'team'::character varying])::text[])))
);


--

-- Name: error_events; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.error_events (
    fingerprint character varying(64) NOT NULL,
    level character varying(20) NOT NULL,
    message text NOT NULL,
    stacktrace text,
    source character varying(20) NOT NULL,
    environment character varying(50),
    version character varying(50),
    status character varying(20) DEFAULT 'new'::character varying NOT NULL,
    resolved_at timestamp with time zone,
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    organisation_id uuid NOT NULL,
    created_at timestamp with time zone DEFAULT CURRENT_TIMESTAMP NOT NULL,
    updated_at timestamp with time zone DEFAULT CURRENT_TIMESTAMP NOT NULL,
    signal character varying(100),
    context_json jsonb,
    CONSTRAINT ck_error_events_level CHECK (((level)::text = ANY ((ARRAY['error'::character varying, 'warning'::character varying, 'critical'::character varying])::text[]))),
    CONSTRAINT ck_error_events_source CHECK (((source)::text = ANY (ARRAY[('backend'::character varying)::text, ('frontend'::character varying)::text, ('celery'::character varying)::text, ('saq'::character varying)::text]))),
    CONSTRAINT ck_error_events_status CHECK (((status)::text = ANY ((ARRAY['new'::character varying, 'acknowledged'::character varying, 'resolved'::character varying, 'archived'::character varying])::text[])))
);


--

-- Name: error_forwarder_configs; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.error_forwarder_configs (
    forwarder_type character varying(50) NOT NULL,
    enabled boolean DEFAULT false NOT NULL,
    last_test_at timestamp with time zone,
    last_test_ok boolean,
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    organisation_id uuid NOT NULL,
    created_at timestamp with time zone DEFAULT CURRENT_TIMESTAMP NOT NULL,
    updated_at timestamp with time zone DEFAULT CURRENT_TIMESTAMP NOT NULL,
    deleted_at timestamp with time zone,
    config_json jsonb
);


--

-- Name: error_groups; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.error_groups (
    fingerprint character varying(64) NOT NULL,
    status character varying(20) DEFAULT 'new'::character varying NOT NULL,
    first_seen timestamp with time zone DEFAULT CURRENT_TIMESTAMP NOT NULL,
    last_seen timestamp with time zone DEFAULT CURRENT_TIMESTAMP NOT NULL,
    count integer DEFAULT 1 NOT NULL,
    level_peak character varying(20) DEFAULT 'error'::character varying NOT NULL,
    sample_event_id uuid,
    assigned_to uuid,
    resolved_at timestamp with time zone,
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    organisation_id uuid NOT NULL,
    created_at timestamp with time zone DEFAULT CURRENT_TIMESTAMP NOT NULL,
    updated_at timestamp with time zone DEFAULT CURRENT_TIMESTAMP NOT NULL,
    CONSTRAINT ck_error_group_count CHECK ((count >= 0)),
    CONSTRAINT ck_error_groups_level_peak CHECK (((level_peak)::text = ANY ((ARRAY['error'::character varying, 'warning'::character varying, 'critical'::character varying])::text[]))),
    CONSTRAINT ck_error_groups_status CHECK (((status)::text = ANY ((ARRAY['new'::character varying, 'acknowledged'::character varying, 'resolved'::character varying, 'archived'::character varying])::text[])))
);


--

-- Name: error_notification_rules; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.error_notification_rules (
    name character varying(100) NOT NULL,
    enabled boolean DEFAULT true NOT NULL,
    condition_level character varying(20) DEFAULT 'error'::character varying NOT NULL,
    condition_min_count integer DEFAULT 1 NOT NULL,
    condition_window_seconds integer DEFAULT 300 NOT NULL,
    action_type character varying(20) DEFAULT 'in_app'::character varying NOT NULL,
    webhook_url text,
    cooldown_seconds integer DEFAULT 300 NOT NULL,
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    organisation_id uuid NOT NULL,
    created_at timestamp with time zone DEFAULT CURRENT_TIMESTAMP NOT NULL,
    updated_at timestamp with time zone DEFAULT CURRENT_TIMESTAMP NOT NULL,
    signal character varying(100),
    is_default boolean DEFAULT false NOT NULL,
    deleted_at timestamp with time zone,
    CONSTRAINT ck_enr_action_type CHECK (((action_type)::text = ANY ((ARRAY['in_app'::character varying, 'email'::character varying, 'webhook'::character varying])::text[]))),
    CONSTRAINT ck_enr_condition_level CHECK (((condition_level)::text = ANY ((ARRAY['error'::character varying, 'warning'::character varying, 'critical'::character varying])::text[]))),
    CONSTRAINT ck_error_notification_rule_cooldown CHECK ((cooldown_seconds >= 0)),
    CONSTRAINT ck_error_notification_rule_min_count CHECK ((condition_min_count > 0)),
    CONSTRAINT ck_error_notification_rule_window CHECK ((condition_window_seconds > 0))
);


--

-- Name: eval_backfill_violations; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.eval_backfill_violations (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    migration_revision text NOT NULL,
    eval_definition_id uuid NOT NULL,
    eval_name text,
    eval_type text NOT NULL,
    node_id uuid,
    violated_exclusions jsonb NOT NULL,
    created_at timestamp with time zone DEFAULT CURRENT_TIMESTAMP NOT NULL
);


--

-- Name: eval_cases; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.eval_cases (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    organisation_id uuid NOT NULL,
    dataset_id uuid NOT NULL,
    input_hash character varying(64) NOT NULL,
    deleted_at timestamp with time zone,
    deleted_by uuid,
    created_at timestamp with time zone DEFAULT CURRENT_TIMESTAMP NOT NULL,
    updated_at timestamp with time zone DEFAULT CURRENT_TIMESTAMP NOT NULL,
    input_payload jsonb NOT NULL,
    expected_output jsonb
);



--

-- Name: eval_datasets; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.eval_datasets (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    organisation_id uuid NOT NULL,
    owner_team_id uuid,
    visibility character varying(10) DEFAULT 'org'::character varying NOT NULL,
    name character varying(255) NOT NULL,
    version integer DEFAULT 1 NOT NULL,
    deleted_at timestamp with time zone,
    deleted_by uuid,
    created_at timestamp with time zone DEFAULT CURRENT_TIMESTAMP NOT NULL,
    updated_at timestamp with time zone DEFAULT CURRENT_TIMESTAMP NOT NULL,
    CONSTRAINT ck_eval_datasets_version_gte_1 CHECK ((version >= 1)),
    CONSTRAINT ck_eval_datasets_visibility CHECK (((visibility)::text = ANY ((ARRAY['org'::character varying, 'team'::character varying])::text[])))
);



--

-- Name: eval_results; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.eval_results (
    run_id uuid,
    node_id uuid,
    eval_id uuid NOT NULL,
    passed boolean NOT NULL,
    score double precision,
    detail character varying(2000),
    evaluated_at timestamp with time zone DEFAULT CURRENT_TIMESTAMP NOT NULL,
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    organisation_id uuid NOT NULL,
    created_at timestamp with time zone DEFAULT CURRENT_TIMESTAMP NOT NULL,
    updated_at timestamp with time zone DEFAULT CURRENT_TIMESTAMP NOT NULL,
    observed boolean DEFAULT false NOT NULL,
    suite_run_id uuid,
    eval_definition_version integer,
    outcome character varying(16) NOT NULL,
    judge_result_json jsonb,
    judge_confidence_kind character varying(32),
    error_code character varying(64),
    resolution character varying(20) DEFAULT 'pending'::character varying NOT NULL,
    resolution_reason text,
    resolved_at timestamp with time zone,
    resolved_by uuid,
    CONSTRAINT ck_eval_results_judge_confidence_kind CHECK (((judge_confidence_kind IS NULL) OR ((judge_confidence_kind)::text = ANY ((ARRAY['vendor_claimed_calibrated'::character varying, 'model_self_reported'::character varying, 'unavailable'::character varying])::text[])))),
    CONSTRAINT ck_eval_results_outcome CHECK (((outcome)::text = ANY ((ARRAY['pass'::character varying, 'fail'::character varying, 'error'::character varying])::text[]))),
    CONSTRAINT ck_eval_results_resolution CHECK (((resolution)::text = ANY ((ARRAY['accepted'::character varying, 'dismissed'::character varying, 'superseded'::character varying, 'pending'::character varying])::text[]))),
    CONSTRAINT ck_eval_results_run_xor_suite CHECK (((run_id IS NULL) <> (suite_run_id IS NULL)))
);


--

-- Name: eval_suites; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.eval_suites (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    organisation_id uuid NOT NULL,
    owner_team_id uuid,
    visibility character varying(10) DEFAULT 'org'::character varying NOT NULL,
    name character varying(255) NOT NULL,
    description character varying(2000),
    eval_definition_ids jsonb DEFAULT '[]'::jsonb NOT NULL,
    input_set_ref character varying(255),
    legacy_suite_id character varying(255),
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    version integer DEFAULT 1 NOT NULL,
    pre_version_raw json,
    baseline_window integer,
    minimum_delta numeric(8,4),
    cooldown integer,
    CONSTRAINT ck_eval_suites_baseline_window_gte_1 CHECK (((baseline_window IS NULL) OR (baseline_window >= 1))),
    CONSTRAINT ck_eval_suites_cooldown_gte_0 CHECK (((cooldown IS NULL) OR (cooldown >= 0))),
    CONSTRAINT ck_eval_suites_minimum_delta CHECK (((minimum_delta IS NULL) OR ((minimum_delta >= (0)::numeric) AND (minimum_delta <= (1)::numeric)))),
    CONSTRAINT ck_eval_suites_team_owner CHECK ((((visibility)::text = 'org'::text) OR (owner_team_id IS NOT NULL))),
    CONSTRAINT ck_eval_suites_version_gte_1 CHECK ((version >= 1)),
    CONSTRAINT ck_eval_suites_visibility CHECK (((visibility)::text = ANY ((ARRAY['org'::character varying, 'team'::character varying])::text[])))
);



--

-- Name: evals; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.evals (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    organisation_id uuid NOT NULL,
    created_at timestamp with time zone DEFAULT CURRENT_TIMESTAMP NOT NULL,
    updated_at timestamp with time zone DEFAULT CURRENT_TIMESTAMP NOT NULL,
    version integer DEFAULT 1 NOT NULL,
    pre_version_raw json,
    pipeline_id uuid NOT NULL,
    node_id uuid,
    name text,
    suite_id text,
    eval_suite_id uuid,
    account_id uuid NOT NULL,
    eval_type text NOT NULL,
    config_json jsonb DEFAULT '{}'::json NOT NULL,
    pass_threshold numeric,
    deleted_at timestamp with time zone,
    deleted_by uuid,
    acceptance_rate double precision,
    is_muted boolean DEFAULT false NOT NULL,
    muted_at timestamp with time zone,
    muted_by uuid,
    mute_threshold double precision,
    mute_min_outcomes integer,
    CONSTRAINT ck_evals_mute_min_outcomes CHECK (((mute_min_outcomes IS NULL) OR (mute_min_outcomes > 0))),
    CONSTRAINT ck_evals_mute_threshold CHECK (((mute_threshold IS NULL) OR ((mute_threshold >= (0)::double precision) AND (mute_threshold <= (1)::double precision)))),
    CONSTRAINT ck_evals_pass_threshold CHECK (((pass_threshold IS NULL) OR ((pass_threshold >= (0)::numeric) AND (pass_threshold <= (1)::numeric)))),
    CONSTRAINT ck_evals_type CHECK ((eval_type = ANY (ARRAY[('model_judge'::character varying)::text, ('regex'::character varying)::text, ('json_schema'::character varying)::text, ('custom_function'::character varying)::text, ('guardrail'::character varying)::text, ('human_set'::character varying)::text])))
);



--

-- Name: evidence; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.evidence (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    organisation_id uuid NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    key text NOT NULL,
    subject_type text NOT NULL,
    subject_id text NOT NULL,
    value jsonb,
    observed_at timestamp with time zone DEFAULT now() NOT NULL,
    producer_type text NOT NULL,
    producer_id uuid,
    CONSTRAINT ck_evidence_producer_type CHECK ((producer_type = ANY (ARRAY['eval'::text, 'policy_gate'::text, 'run'::text, 'system_state'::text, 'derived'::text])))
);



--

-- Name: evidence_redaction_rules; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.evidence_redaction_rules (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    organisation_id uuid NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    key_pattern text NOT NULL,
    role text NOT NULL,
    redacted_fields text[] DEFAULT '{}'::text[] NOT NULL,
    deleted_at timestamp with time zone,
    CONSTRAINT ck_evidence_redaction_rules_role CHECK ((role = ANY (ARRAY['admin'::text, 'operator'::text, 'viewer'::text])))
);



--

-- Name: external_agents; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.external_agents (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    organisation_id uuid NOT NULL,
    name character varying(255) NOT NULL,
    runtime_kind character varying(50) DEFAULT 'other'::character varying NOT NULL,
    host_descriptor character varying(500),
    status character varying(30) DEFAULT 'active'::character varying NOT NULL,
    discovery_provenance character varying(30) DEFAULT 'manual'::character varying NOT NULL,
    first_seen timestamp with time zone,
    last_seen timestamp with time zone,
    visibility character varying(10) DEFAULT 'org'::character varying NOT NULL,
    owner_team_id uuid,
    deleted_at timestamp with time zone,
    created_at timestamp with time zone DEFAULT CURRENT_TIMESTAMP NOT NULL,
    updated_at timestamp with time zone DEFAULT CURRENT_TIMESTAMP NOT NULL,
    CONSTRAINT ck_external_agents_discovery_provenance CHECK (((discovery_provenance)::text = ANY ((ARRAY['manual'::character varying, 'mcp'::character varying, 'webhook'::character varying, 'forge'::character varying])::text[]))),
    CONSTRAINT ck_external_agents_runtime_kind CHECK (((runtime_kind)::text = ANY ((ARRAY['claude-code'::character varying, 'cursor'::character varying, 'opencode'::character varying, 'other'::character varying])::text[]))),
    CONSTRAINT ck_external_agents_status CHECK (((status)::text = ANY ((ARRAY['active'::character varying, 'inactive'::character varying, 'unknown'::character varying])::text[]))),
    CONSTRAINT ck_external_agents_team_owner CHECK ((((visibility)::text = 'org'::text) OR (owner_team_id IS NOT NULL))),
    CONSTRAINT ck_external_agents_visibility CHECK (((visibility)::text = ANY ((ARRAY['org'::character varying, 'team'::character varying])::text[])))
);



--

-- Name: feature_flag_catalog; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.feature_flag_catalog (
    name character varying(255) NOT NULL,
    description character varying(2000),
    tier_id character varying(255) NOT NULL,
    is_active boolean DEFAULT true NOT NULL,
    depends_on jsonb
);


--

-- Name: feedback_records; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.feedback_records (
    run_id uuid NOT NULL,
    review_id character varying(255) NOT NULL,
    account_id uuid NOT NULL,
    rejection_reason text NOT NULL,
    producing_node_id uuid NOT NULL,
    producing_agent_id uuid,
    feedback_status character varying(20) DEFAULT 'pending'::character varying NOT NULL,
    feedback_handler_type character varying(40) DEFAULT 'human'::character varying NOT NULL,
    correction_run_id uuid,
    eval_gap boolean,
    needs_human_review boolean,
    annotation text,
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    organisation_id uuid NOT NULL,
    created_at timestamp with time zone DEFAULT CURRENT_TIMESTAMP NOT NULL,
    updated_at timestamp with time zone DEFAULT CURRENT_TIMESTAMP NOT NULL,
    correction_state jsonb,
    rejected_output jsonb NOT NULL,
    CONSTRAINT ck_feedback_records_handler_type CHECK (((feedback_handler_type)::text = ANY ((ARRAY['human'::character varying, 'ai_correction'::character varying, 'ai_correction_with_human_review'::character varying])::text[]))),
    CONSTRAINT ck_feedback_records_status CHECK (((feedback_status)::text = ANY ((ARRAY['pending'::character varying, 'routing'::character varying, 'correcting'::character varying, 'resolved'::character varying, 'escalated'::character varying, 'dismissed'::character varying])::text[])))
);


--

-- Name: hitl_claims; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.hitl_claims (
    run_id uuid NOT NULL,
    required_team_id uuid,
    review_id character varying(255) NOT NULL,
    pipeline_id uuid NOT NULL,
    account_id uuid,
    claimed_at timestamp with time zone,
    claim_token text,
    expires_at timestamp with time zone DEFAULT (now() + '00:15:00'::interval) NOT NULL,
    decision character varying(20),
    decision_at timestamp with time zone,
    delivered_at timestamp with time zone,
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    organisation_id uuid NOT NULL,
    created_at timestamp with time zone DEFAULT CURRENT_TIMESTAMP NOT NULL,
    updated_at timestamp with time zone DEFAULT CURRENT_TIMESTAMP NOT NULL,
    overdue_notified_at timestamp with time zone,
    decision_payload jsonb,
    context_json jsonb,
    gate_config_json jsonb,
    decided_by uuid,
    terminalize_at timestamp with time zone,
    CONSTRAINT ck_hitl_claims_decision CHECK (((decision IS NULL) OR ((decision)::text = ANY ((ARRAY['approved'::character varying, 'rejected'::character varying, 'deliver_manual'::character varying])::text[]))))
);


--

-- Name: invitations; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.invitations (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    organisation_id uuid NOT NULL,
    email character varying(320) NOT NULL,
    display_name character varying(255) NOT NULL,
    org_role character varying(20) NOT NULL,
    token_hash character varying(64) NOT NULL,
    invited_by uuid NOT NULL,
    expires_at timestamp with time zone NOT NULL,
    consumed_at timestamp with time zone,
    revoked_at timestamp with time zone,
    created_at timestamp with time zone DEFAULT CURRENT_TIMESTAMP NOT NULL,
    updated_at timestamp with time zone DEFAULT CURRENT_TIMESTAMP NOT NULL,
    CONSTRAINT ck_invitations_org_role CHECK (((org_role)::text = ANY ((ARRAY['admin'::character varying, 'operator'::character varying, 'runner'::character varying, 'viewer'::character varying])::text[]))),
    CONSTRAINT ck_invitations_token_hash_len CHECK ((length((token_hash)::text) = 64))
);


--

-- Name: journeys; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.journeys (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    organisation_id uuid NOT NULL,
    owner_team_id uuid,
    kind character varying(64) NOT NULL,
    ref character varying(255) NOT NULL,
    canonical_work_item_id uuid NOT NULL,
    latest_terminal_run_id uuid,
    map_id uuid,
    map_version integer,
    stage_id character varying(255),
    stage_name character varying(255),
    "position" integer,
    latest_status character varying(30),
    latest_provenance character varying(30),
    run_count integer DEFAULT 0 NOT NULL,
    created_at timestamp with time zone DEFAULT CURRENT_TIMESTAMP NOT NULL,
    updated_at timestamp with time zone DEFAULT CURRENT_TIMESTAMP NOT NULL,
    provenance character varying(30) DEFAULT 'derived'::character varying NOT NULL,
    first_seen_source character varying(30),
    dismissed_at timestamp with time zone,
    dismissed_by uuid,
    reason text,
    CONSTRAINT ck_journey_run_count CHECK ((run_count >= 0))
);


--

-- Name: library_primitives; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.library_primitives (
    source character varying(20) NOT NULL,
    primitive_type character varying(20) NOT NULL,
    name character varying(255) NOT NULL,
    slug character varying(255) NOT NULL,
    description character varying(2000),
    author character varying(255) NOT NULL,
    version character varying(50) NOT NULL,
    source_url character varying(2000),
    forked_from uuid,
    checksum character varying(128),
    ed25519_signature character varying(256),
    verified boolean,
    category character varying(50),
    download_count integer,
    average_rating numeric(3,2),
    review_count integer,
    owner_team_id uuid,
    visibility character varying(10) DEFAULT 'org'::character varying NOT NULL,
    contribution_status character varying(20),
    auto_update boolean DEFAULT true NOT NULL,
    account_id uuid,
    version_group_id uuid,
    update_available_version_id uuid,
    tier character varying(20) DEFAULT 'native'::character varying NOT NULL,
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    organisation_id uuid NOT NULL,
    created_at timestamp with time zone DEFAULT CURRENT_TIMESTAMP NOT NULL,
    updated_at timestamp with time zone DEFAULT CURRENT_TIMESTAMP NOT NULL,
    deleted_at timestamp with time zone,
    tags jsonb NOT NULL,
    content_json jsonb NOT NULL,
    status character varying(20),
    manifest_pins jsonb,
    trust_header jsonb,
    CONSTRAINT ck_library_primitives_contribution_status CHECK (((contribution_status)::text = ANY ((ARRAY['draft'::character varying, 'review_queue'::character varying, 'published'::character varying])::text[]))),
    CONSTRAINT ck_library_primitives_dl_count CHECK (((download_count IS NULL) OR (download_count >= 0))),
    CONSTRAINT ck_library_primitives_rating CHECK (((average_rating IS NULL) OR ((average_rating >= (1)::numeric) AND (average_rating <= (5)::numeric)))),
    CONSTRAINT ck_library_primitives_review_count CHECK (((review_count IS NULL) OR (review_count >= 0))),
    CONSTRAINT ck_library_primitives_source CHECK (((source)::text = ANY ((ARRAY['local'::character varying, 'registry'::character varying, 'modulo'::character varying, 'community'::character varying])::text[]))),
    CONSTRAINT ck_library_primitives_source_fields CHECK (((((source)::text = 'local'::text) AND (source_url IS NULL) AND (checksum IS NULL) AND (ed25519_signature IS NULL) AND (verified IS NULL) AND (download_count IS NULL) AND (average_rating IS NULL) AND (review_count IS NULL)) OR (((source)::text = 'modulo'::text) AND (source_url IS NULL) AND (checksum IS NULL) AND (ed25519_signature IS NULL) AND (verified IS NULL) AND (download_count IS NULL) AND (average_rating IS NULL) AND (review_count IS NULL)) OR (((source)::text = 'community'::text) AND (source_url IS NULL) AND (checksum IS NULL) AND (ed25519_signature IS NULL) AND (download_count IS NULL) AND (average_rating IS NULL) AND (review_count IS NULL)) OR (((source)::text = 'registry'::text) AND (owner_team_id IS NULL) AND ((visibility)::text = 'org'::text) AND (forked_from IS NULL) AND (source_url IS NOT NULL) AND (checksum IS NOT NULL) AND (verified IS NOT NULL) AND (download_count IS NOT NULL)))),
    CONSTRAINT ck_library_primitives_status CHECK (((status IS NULL) OR ((status)::text = ANY ((ARRAY['draft'::character varying, 'published'::character varying])::text[])))),
    CONSTRAINT ck_library_primitives_team_owner CHECK ((((visibility)::text = ANY ((ARRAY['org'::character varying, 'community'::character varying])::text[])) OR (owner_team_id IS NOT NULL))),
    CONSTRAINT ck_library_primitives_tier CHECK (((tier)::text = ANY ((ARRAY['native'::character varying, 'preview'::character varying, 'in_dev'::character varying])::text[]))),
    CONSTRAINT ck_library_primitives_type CHECK (((primitive_type)::text = ANY ((ARRAY['schema'::character varying, 'workflow'::character varying, 'agent'::character varying, 'integration'::character varying, 'test_fixture'::character varying, 'pipeline_template'::character varying, 'composite'::character varying, 'lifecycle_map'::character varying, 'library_collection'::character varying])::text[]))),
    CONSTRAINT ck_library_primitives_visibility CHECK (((visibility)::text = ANY ((ARRAY['org'::character varying, 'team'::character varying, 'community'::character varying])::text[])))
);


--

-- Name: library_sync_state; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.library_sync_state (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    last_synced_at timestamp with time zone DEFAULT CURRENT_TIMESTAMP NOT NULL,
    last_success_at timestamp with time zone,
    last_error character varying(2000),
    created_at timestamp with time zone DEFAULT CURRENT_TIMESTAMP NOT NULL,
    updated_at timestamp with time zone DEFAULT CURRENT_TIMESTAMP NOT NULL,
    manifest_json jsonb NOT NULL,
    catalog_json jsonb NOT NULL
);


--

-- Name: lifecycle_map_stages; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.lifecycle_map_stages (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    organisation_id uuid NOT NULL,
    map_id uuid NOT NULL,
    version integer DEFAULT 1 NOT NULL,
    stage_id character varying(255) NOT NULL,
    stage_name character varying(255) NOT NULL,
    "position" integer DEFAULT 0 NOT NULL,
    stage_type character varying(20) NOT NULL,
    pipeline_id uuid,
    account_id uuid NOT NULL,
    created_at timestamp with time zone DEFAULT CURRENT_TIMESTAMP NOT NULL,
    updated_at timestamp with time zone DEFAULT CURRENT_TIMESTAMP NOT NULL,
    CONSTRAINT ck_lifecycle_map_stages_type CHECK (((stage_type)::text = ANY (ARRAY[('modulo'::character varying)::text, ('external'::character varying)::text, ('manual'::character varying)::text, ('placeholder'::character varying)::text])))
);


--

-- Name: lifecycle_maps; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.lifecycle_maps (
    name character varying(255) NOT NULL,
    description character varying(2000),
    owner_team_id uuid,
    visibility character varying(10) DEFAULT 'org'::character varying NOT NULL,
    version integer DEFAULT 1 NOT NULL,
    archived_at timestamp with time zone,
    account_id uuid NOT NULL,
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    organisation_id uuid NOT NULL,
    created_at timestamp with time zone DEFAULT CURRENT_TIMESTAMP NOT NULL,
    updated_at timestamp with time zone DEFAULT CURRENT_TIMESTAMP NOT NULL,
    deleted_at timestamp with time zone,
    updated_by uuid,
    content_json jsonb NOT NULL,
    CONSTRAINT ck_lifecycle_maps_team_owner CHECK ((((visibility)::text = 'org'::text) OR (owner_team_id IS NOT NULL))),
    CONSTRAINT ck_lifecycle_maps_version CHECK ((version > 0)),
    CONSTRAINT ck_lifecycle_maps_visibility CHECK (((visibility)::text = ANY ((ARRAY['org'::character varying, 'team'::character varying])::text[])))
);


--

-- Name: mcp_servers; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.mcp_servers (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    organisation_id uuid NOT NULL,
    name character varying(255) NOT NULL,
    transport character varying(30) NOT NULL,
    endpoint character varying(2000),
    command character varying(2000),
    declared_scopes jsonb NOT NULL,
    owner_account_id uuid,
    credentials_ciphertext bytea,
    tools_json jsonb NOT NULL,
    tools_hash character varying(64),
    approval_status character varying(30) DEFAULT 'unapproved'::character varying NOT NULL,
    approved_by uuid,
    approved_at timestamp with time zone,
    approved_hash character varying(64),
    last_observed_at timestamp with time zone,
    visibility character varying(10) DEFAULT 'org'::character varying NOT NULL,
    owner_team_id uuid,
    deleted_at timestamp with time zone,
    created_at timestamp with time zone DEFAULT CURRENT_TIMESTAMP NOT NULL,
    updated_at timestamp with time zone DEFAULT CURRENT_TIMESTAMP NOT NULL,
    CONSTRAINT ck_mcp_servers_approval_status CHECK (((approval_status)::text = ANY ((ARRAY['unapproved'::character varying, 'approved'::character varying, 'approved-stale'::character varying, 'blocked'::character varying])::text[]))),
    CONSTRAINT ck_mcp_servers_team_owner CHECK ((((visibility)::text = 'org'::text) OR (owner_team_id IS NOT NULL))),
    CONSTRAINT ck_mcp_servers_transport CHECK (((transport)::text = ANY ((ARRAY['stdio'::character varying, 'sse'::character varying, 'http'::character varying, 'websocket'::character varying])::text[]))),
    CONSTRAINT ck_mcp_servers_visibility CHECK (((visibility)::text = ANY ((ARRAY['org'::character varying, 'team'::character varying])::text[])))
);



--

-- Name: mcp_setup_tokens; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.mcp_setup_tokens (
    resource_type character varying(50) NOT NULL,
    resource_id uuid NOT NULL,
    token_hash character varying(64) NOT NULL,
    expires_at timestamp with time zone NOT NULL,
    completed_at timestamp with time zone,
    created_by uuid NOT NULL,
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    organisation_id uuid NOT NULL,
    created_at timestamp with time zone DEFAULT CURRENT_TIMESTAMP NOT NULL,
    updated_at timestamp with time zone DEFAULT CURRENT_TIMESTAMP NOT NULL
);


--

-- Name: metrics_staging; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.metrics_staging (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    organisation_id uuid NOT NULL,
    event_id character varying(128) NOT NULL,
    event_type character varying(64) NOT NULL,
    payload jsonb DEFAULT '{}'::jsonb NOT NULL,
    recorded_at timestamp with time zone DEFAULT CURRENT_TIMESTAMP NOT NULL,
    created_at timestamp with time zone DEFAULT CURRENT_TIMESTAMP NOT NULL,
    updated_at timestamp with time zone DEFAULT CURRENT_TIMESTAMP NOT NULL
);


--

-- Name: model_backends; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.model_backends (
    name character varying(255) NOT NULL,
    display_name character varying(255) NOT NULL,
    provider character varying(30) NOT NULL,
    model_id character varying(255) NOT NULL,
    credentials_ciphertext bytea NOT NULL,
    cost_tracking character varying(10) DEFAULT 'enabled'::character varying NOT NULL,
    currency character varying(3) DEFAULT 'USD'::character varying NOT NULL,
    owner_team_id uuid,
    visibility character varying(10) DEFAULT 'org'::character varying NOT NULL,
    status character varying(30) DEFAULT 'active'::character varying NOT NULL,
    last_health_check_at timestamp with time zone,
    last_health_check_error character varying(2000),
    account_id uuid NOT NULL,
    tier character varying(20) DEFAULT 'native'::character varying NOT NULL,
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    organisation_id uuid NOT NULL,
    created_at timestamp with time zone DEFAULT CURRENT_TIMESTAMP NOT NULL,
    updated_at timestamp with time zone DEFAULT CURRENT_TIMESTAMP NOT NULL,
    default_params jsonb NOT NULL,
    fallback_backend_ids jsonb,
    validation_level character varying(30),
    CONSTRAINT ck_model_backends_cost CHECK (((cost_tracking)::text = ANY ((ARRAY['enabled'::character varying, 'disabled'::character varying])::text[]))),
    CONSTRAINT ck_model_backends_provider CHECK (((provider)::text = ANY ((ARRAY['ai21'::character varying, 'anthropic'::character varying, 'azure_openai'::character varying, 'bedrock'::character varying, 'cohere'::character varying, 'custom'::character varying, 'deepseek'::character varying, 'fireworks'::character varying, 'gemini'::character varying, 'grok'::character varying, 'groq'::character varying, 'jan'::character varying, 'llamacpp'::character varying, 'lm_studio'::character varying, 'localai'::character varying, 'mistral'::character varying, 'ollama'::character varying, 'openai'::character varying, 'opencode'::character varying, 'openrouter'::character varying, 'perplexity'::character varying, 'qwen'::character varying, 'replicate'::character varying, 'tgi'::character varying, 'togetherai'::character varying, 'vertexai'::character varying, 'vllm'::character varying, 'watsonx'::character varying])::text[]))),
    CONSTRAINT ck_model_backends_team_owner CHECK ((((visibility)::text = 'org'::text) OR (owner_team_id IS NOT NULL))),
    CONSTRAINT ck_model_backends_tier CHECK (((tier)::text = ANY ((ARRAY['native'::character varying, 'preview'::character varying, 'in_dev'::character varying])::text[]))),
    CONSTRAINT ck_model_backends_visibility CHECK (((visibility)::text = ANY ((ARRAY['org'::character varying, 'team'::character varying])::text[])))
);


--

-- Name: modulo_journey_facts; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.modulo_journey_facts (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    organisation_id uuid NOT NULL,
    run_id uuid NOT NULL,
    writer character varying(30) NOT NULL,
    parse_failures integer DEFAULT 0 NOT NULL,
    finalise_attempts integer DEFAULT 0 NOT NULL,
    created_at timestamp with time zone DEFAULT CURRENT_TIMESTAMP NOT NULL
);


--

-- Name: node_categories; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.node_categories (
    name character varying(100) NOT NULL,
    description text,
    color character varying(7) NOT NULL,
    icon character varying(50),
    sort_order integer NOT NULL,
    account_id uuid NOT NULL,
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    organisation_id uuid NOT NULL,
    created_at timestamp with time zone DEFAULT CURRENT_TIMESTAMP NOT NULL,
    updated_at timestamp with time zone DEFAULT CURRENT_TIMESTAMP NOT NULL,
    deleted_at timestamp with time zone
);


--

-- Name: node_observations; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.node_observations (
    run_id uuid NOT NULL,
    node_id uuid NOT NULL,
    account_id uuid,
    human_observed_at timestamp with time zone NOT NULL,
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    organisation_id uuid NOT NULL,
    created_at timestamp with time zone DEFAULT CURRENT_TIMESTAMP NOT NULL,
    updated_at timestamp with time zone DEFAULT CURRENT_TIMESTAMP NOT NULL
);


--

-- Name: nodes; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.nodes (
    pipeline_id uuid NOT NULL,
    name character varying(255) NOT NULL,
    description text,
    parent_node_id uuid,
    timeout_seconds integer DEFAULT 300 NOT NULL,
    retry_count integer,
    retry_delay_seconds integer,
    account_id uuid NOT NULL,
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    organisation_id uuid NOT NULL,
    created_at timestamp with time zone DEFAULT CURRENT_TIMESTAMP NOT NULL,
    updated_at timestamp with time zone DEFAULT CURRENT_TIMESTAMP NOT NULL,
    CONSTRAINT ck_nodes_retry_count CHECK (((retry_count IS NULL) OR (retry_count >= 0))),
    CONSTRAINT ck_nodes_retry_delay_seconds CHECK (((retry_delay_seconds IS NULL) OR (retry_delay_seconds >= 0))),
    CONSTRAINT ck_nodes_timeout_seconds CHECK (((timeout_seconds IS NULL) OR (timeout_seconds > 0)))
);


--

-- Name: notification_delivery_log; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.notification_delivery_log (
    event_type character varying(100) NOT NULL,
    endpoint_id uuid,
    run_id uuid,
    status character varying(20) DEFAULT 'delivered'::character varying NOT NULL,
    attempt_count integer DEFAULT 0 NOT NULL,
    response_code integer,
    last_error character varying(2000),
    failed_at timestamp with time zone,
    response_body character varying(2000),
    payload_ciphertext bytea,
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    organisation_id uuid NOT NULL,
    created_at timestamp with time zone DEFAULT CURRENT_TIMESTAMP NOT NULL,
    updated_at timestamp with time zone DEFAULT CURRENT_TIMESTAMP NOT NULL,
    CONSTRAINT ck_notification_delivery_attempt_count CHECK ((attempt_count >= 0)),
    CONSTRAINT ck_notification_delivery_log_status CHECK (((status)::text = ANY ((ARRAY['delivered'::character varying, 'failed'::character varying, 'dead_lettered'::character varying, 'in_app'::character varying])::text[])))
);


--

-- Name: notification_endpoints; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.notification_endpoints (
    url character varying(2048) NOT NULL,
    secret_ciphertext bytea,
    description character varying(500),
    consecutive_dead_letter_count integer DEFAULT 0 NOT NULL,
    auto_disabled boolean DEFAULT false NOT NULL,
    disabled_at timestamp with time zone,
    account_id uuid,
    team_id uuid,
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    organisation_id uuid NOT NULL,
    created_at timestamp with time zone DEFAULT CURRENT_TIMESTAMP NOT NULL,
    updated_at timestamp with time zone DEFAULT CURRENT_TIMESTAMP NOT NULL,
    deleted_at timestamp with time zone,
    events jsonb DEFAULT '[]'::json NOT NULL,
    CONSTRAINT ck_notification_endpoint_dead_letter CHECK ((consecutive_dead_letter_count >= 0))
);


--

-- Name: notification_preferences; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.notification_preferences (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    organisation_id uuid NOT NULL,
    account_id uuid NOT NULL,
    category character varying(100) NOT NULL,
    created_at timestamp with time zone DEFAULT CURRENT_TIMESTAMP NOT NULL,
    updated_at timestamp with time zone DEFAULT CURRENT_TIMESTAMP NOT NULL
);



--

-- Name: notifications; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.notifications (
    scope character varying(20) NOT NULL,
    target_user_id uuid,
    level character varying(20) NOT NULL,
    category character varying(100) NOT NULL,
    title character varying(500) NOT NULL,
    body text NOT NULL,
    action_url character varying(2048),
    dismiss_strategy character varying(20) DEFAULT 'user_only'::character varying NOT NULL,
    dismissible_at_scope boolean DEFAULT false NOT NULL,
    expires_at timestamp with time zone DEFAULT (now() + '90 days'::interval) NOT NULL,
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    organisation_id uuid NOT NULL,
    created_at timestamp with time zone DEFAULT CURRENT_TIMESTAMP NOT NULL,
    updated_at timestamp with time zone DEFAULT CURRENT_TIMESTAMP NOT NULL,
    CONSTRAINT ck_notifications_dismiss_strategy CHECK (((dismiss_strategy)::text = ANY ((ARRAY['user_only'::character varying, 'org_admin'::character varying, 'any_scope'::character varying])::text[]))),
    CONSTRAINT ck_notifications_level CHECK (((level)::text = ANY ((ARRAY['debug'::character varying, 'info'::character varying, 'warning'::character varying, 'error'::character varying])::text[]))),
    CONSTRAINT ck_notifications_scope CHECK (((scope)::text = ANY ((ARRAY['user'::character varying, 'org'::character varying, 'admin'::character varying])::text[])))
);


--

-- Name: oauth_authorization_codes; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.oauth_authorization_codes (
    code character varying(128) NOT NULL,
    client_id character varying(64) NOT NULL,
    organisation_id uuid NOT NULL,
    scopes text NOT NULL,
    redirect_uri character varying(1024) NOT NULL,
    code_challenge character varying(128),
    expires_at timestamp with time zone NOT NULL,
    used boolean NOT NULL,
    created_at timestamp with time zone DEFAULT CURRENT_TIMESTAMP NOT NULL,
    account_id uuid NOT NULL,
    code_challenge_method character varying(8) DEFAULT 'S256'::character varying NOT NULL,
    CONSTRAINT ck_oauth_auth_codes_challenge_method CHECK (((code_challenge_method)::text = 'S256'::text))
);


--

-- Name: oauth_clients; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.oauth_clients (
    client_id character varying(64) NOT NULL,
    client_secret_hash character varying(128) NOT NULL,
    name character varying(255) NOT NULL,
    scopes text NOT NULL,
    redirect_uris text NOT NULL,
    account_id uuid,
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    organisation_id uuid NOT NULL,
    created_at timestamp with time zone DEFAULT CURRENT_TIMESTAMP NOT NULL,
    updated_at timestamp with time zone DEFAULT CURRENT_TIMESTAMP NOT NULL,
    team_id uuid
);


--

-- Name: oauth_consent_states; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.oauth_consent_states (
    state character varying(128) NOT NULL,
    client_id character varying(64) NOT NULL,
    redirect_uri character varying(1024) NOT NULL,
    scopes json NOT NULL,
    code_challenge character varying(128) NOT NULL,
    organisation_id uuid NOT NULL,
    expires_at timestamp with time zone NOT NULL,
    consumed boolean DEFAULT false NOT NULL,
    account_id uuid,
    created_at timestamp with time zone DEFAULT CURRENT_TIMESTAMP NOT NULL
);


--

-- Name: oauth_token_families; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.oauth_token_families (
    family_id uuid DEFAULT gen_random_uuid() NOT NULL,
    client_id character varying(64) NOT NULL,
    organisation_id uuid NOT NULL,
    max_sequence integer NOT NULL,
    is_blacklisted boolean NOT NULL,
    created_at timestamp with time zone DEFAULT CURRENT_TIMESTAMP NOT NULL,
    blacklisted_at timestamp with time zone
);


--

-- Name: onboarding_progress; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.onboarding_progress (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    organisation_id uuid NOT NULL,
    completed_actions character varying[] DEFAULT '{}'::character varying[] NOT NULL,
    skipped_actions character varying[] DEFAULT '{}'::character varying[] NOT NULL,
    dismissed boolean DEFAULT false NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL
);


--

-- Name: org_api_keys; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.org_api_keys (
    name character varying(255) NOT NULL,
    lookup_prefix character varying(8) NOT NULL,
    hashed_secret character varying(64) NOT NULL,
    role character varying(20) NOT NULL,
    team_id uuid,
    account_id uuid NOT NULL,
    last_used_at timestamp with time zone,
    expires_at timestamp with time zone DEFAULT (now() + '365 days'::interval) NOT NULL,
    revoked_at timestamp with time zone,
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    organisation_id uuid NOT NULL,
    created_at timestamp with time zone DEFAULT CURRENT_TIMESTAMP NOT NULL,
    updated_at timestamp with time zone DEFAULT CURRENT_TIMESTAMP NOT NULL,
    run_id uuid,
    scope character varying(10) DEFAULT 'org'::character varying NOT NULL,
    grants text,
    CONSTRAINT ck_org_api_keys_role CHECK ((((role)::text = ANY ((ARRAY['operator'::character varying, 'runner'::character varying])::text[])) OR (((role)::text = 'admin'::text) AND ((scope)::text = 'user'::text)))),
    CONSTRAINT ck_org_api_keys_scope CHECK (((scope)::text = ANY ((ARRAY['org'::character varying, 'user'::character varying])::text[])))
);


--

-- Name: COLUMN org_api_keys.grants; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.org_api_keys.grants IS 'Space-separated permission grants; NULL=legacy role bundle, ''''=deny-all';


--

-- Name: org_daily_run_counts; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.org_daily_run_counts (
    run_date date NOT NULL,
    team_id uuid,
    run_count integer DEFAULT 0 NOT NULL,
    total_spend_usd numeric(14,6) DEFAULT '0'::numeric NOT NULL,
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    organisation_id uuid NOT NULL,
    created_at timestamp with time zone DEFAULT CURRENT_TIMESTAMP NOT NULL,
    updated_at timestamp with time zone DEFAULT CURRENT_TIMESTAMP NOT NULL,
    clamped boolean DEFAULT false NOT NULL,
    refused_spend_usd numeric(14,6) DEFAULT '0'::numeric NOT NULL,
    CONSTRAINT ck_daily_run_count_run_count CHECK ((run_count >= 0)),
    CONSTRAINT ck_org_daily_run_counts_refused_spend CHECK ((refused_spend_usd >= (0)::numeric)),
    CONSTRAINT ck_org_daily_run_counts_run_count CHECK ((run_count >= 0)),
    CONSTRAINT ck_org_daily_run_counts_total_spend CHECK ((total_spend_usd >= (0)::numeric))
);


--

-- Name: org_memberships; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.org_memberships (
    account_id uuid NOT NULL,
    role character varying(20) DEFAULT 'runner'::character varying NOT NULL,
    joined_at timestamp with time zone DEFAULT CURRENT_TIMESTAMP NOT NULL,
    deactivated_at timestamp with time zone,
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    organisation_id uuid NOT NULL,
    created_at timestamp with time zone DEFAULT CURRENT_TIMESTAMP NOT NULL,
    updated_at timestamp with time zone DEFAULT CURRENT_TIMESTAMP NOT NULL,
    CONSTRAINT ck_org_memberships_role CHECK (((role)::text = ANY (ARRAY[('admin'::character varying)::text, ('operator'::character varying)::text, ('runner'::character varying)::text, ('viewer'::character varying)::text])))
);


--

-- Name: org_mint_budget_usage; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.org_mint_budget_usage (
    organisation_id uuid NOT NULL,
    window_start timestamp with time zone NOT NULL,
    used integer DEFAULT 0 NOT NULL
);



--

-- Name: organisation_cost_state; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.organisation_cost_state (
    org_id uuid NOT NULL,
    max_run_cost_cents integer,
    spend_ceiling_cents integer,
    org_cumulative_spend_cents integer DEFAULT 0 NOT NULL,
    CONSTRAINT ck_organisation_cost_state_cum_spend CHECK ((org_cumulative_spend_cents >= 0))
);



--

-- Name: organisations; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.organisations (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    name character varying(255) NOT NULL,
    slug character varying(255) NOT NULL,
    status character varying(20) DEFAULT 'active'::character varying NOT NULL,
    created_at timestamp with time zone DEFAULT CURRENT_TIMESTAMP NOT NULL,
    deleted_at timestamp with time zone,
    created_by uuid,
    plan_id character varying(255),
    daily_spend_limit numeric(14,6),
    deletion_token character varying(128),
    deletion_token_expires_at timestamp with time zone,
    authz_enforce boolean DEFAULT true NOT NULL,
    triggers_paused boolean DEFAULT false NOT NULL,
    triggers_paused_at timestamp with time zone,
    guardrails_kill_switch boolean DEFAULT false NOT NULL,
    guardrails_kill_switch_at timestamp with time zone,
    settings_json jsonb NOT NULL,
    otel_config_json jsonb DEFAULT '{}'::json NOT NULL,
    export_bundle_json jsonb,
    guardrail_pins_json jsonb,
    CONSTRAINT ck_organisations_daily_spend_limit_non_negative CHECK (((daily_spend_limit IS NULL) OR (daily_spend_limit >= (0)::numeric))),
    CONSTRAINT ck_organisations_deletion_token_expiry CHECK ((((deletion_token IS NULL) AND (deletion_token_expires_at IS NULL)) OR ((deletion_token IS NOT NULL) AND (deletion_token_expires_at IS NOT NULL)))),
    CONSTRAINT ck_organisations_guardrails_kill_switch_at CHECK (((NOT guardrails_kill_switch) OR (guardrails_kill_switch_at IS NOT NULL))),
    CONSTRAINT ck_organisations_name_not_empty CHECK ((length((name)::text) > 0)),
    CONSTRAINT ck_organisations_slug_not_empty CHECK ((length((slug)::text) > 0)),
    CONSTRAINT ck_organisations_status CHECK (((status)::text = ANY ((ARRAY['active'::character varying, 'suspended'::character varying, 'deleted'::character varying])::text[]))),
    CONSTRAINT ck_organisations_triggers_paused_at CHECK (((NOT triggers_paused) OR (triggers_paused_at IS NOT NULL)))
);


--

-- Name: parameter_schemas; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.parameter_schemas (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    organisation_id uuid NOT NULL,
    name character varying(255) NOT NULL,
    description text,
    version integer DEFAULT 1 NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    account_id uuid NOT NULL,
    deleted_at timestamp with time zone,
    parameters jsonb DEFAULT '[]'::jsonb NOT NULL
);


--

-- Name: parameter_sets; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.parameter_sets (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    parameter_schema_id uuid NOT NULL,
    organisation_id uuid NOT NULL,
    account_id uuid NOT NULL,
    version integer DEFAULT 1 NOT NULL,
    schema_version integer NOT NULL,
    name character varying(255) NOT NULL,
    description text,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    deleted_at timestamp with time zone,
    "values" jsonb DEFAULT '{}'::jsonb NOT NULL
);


--

-- Name: pipeline_edges; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.pipeline_edges (
    pipeline_id uuid NOT NULL,
    source_node_id uuid NOT NULL,
    target_node_id uuid NOT NULL,
    edge_type character varying(15) DEFAULT 'normal'::character varying NOT NULL,
    hitl_review_config json,
    condition_expression character varying(500),
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    organisation_id uuid NOT NULL,
    created_at timestamp with time zone DEFAULT CURRENT_TIMESTAMP NOT NULL,
    updated_at timestamp with time zone DEFAULT CURRENT_TIMESTAMP NOT NULL,
    source_port character varying(64) DEFAULT 'out'::character varying NOT NULL,
    target_port character varying(64) DEFAULT 'in'::character varying NOT NULL,
    retry json,
    on_failure_target character varying(64),
    CONSTRAINT ck_pipeline_edges_type CHECK (((edge_type)::text = ANY ((ARRAY['normal'::character varying, 'reject'::character varying, 'conditional'::character varying])::text[])))
);


--

-- Name: pipeline_folders; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.pipeline_folders (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    organisation_id uuid NOT NULL,
    name character varying(255) NOT NULL,
    parent_id uuid,
    sort_order integer DEFAULT 0 NOT NULL,
    account_id uuid NOT NULL,
    created_at timestamp with time zone DEFAULT CURRENT_TIMESTAMP NOT NULL,
    updated_at timestamp with time zone DEFAULT CURRENT_TIMESTAMP NOT NULL
);


--

-- Name: pipeline_snapshots; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.pipeline_snapshots (
    pipeline_id uuid NOT NULL,
    snapshot_version integer NOT NULL,
    account_id uuid,
    environment_profile_id uuid,
    tag character varying(100),
    notes character varying(2000),
    default_autonomy_level character varying(30),
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    organisation_id uuid NOT NULL,
    created_at timestamp with time zone DEFAULT CURRENT_TIMESTAMP NOT NULL,
    updated_at timestamp with time zone DEFAULT CURRENT_TIMESTAMP NOT NULL,
    guardrail_pins_fingerprint character varying(64),
    graph_json jsonb NOT NULL,
    connector_bindings_json jsonb NOT NULL,
    schema_pins_json jsonb NOT NULL,
    prompt_pins_json jsonb NOT NULL,
    model_backend_pins_json jsonb NOT NULL,
    composite_bindings_json jsonb,
    parameter_bindings_json jsonb,
    guardrail_pins_json jsonb,
    config_json jsonb DEFAULT '{}'::json NOT NULL,
    run_context_defaults jsonb NOT NULL,
    version_kind character varying(10) DEFAULT 'run'::character varying NOT NULL,
    created_kind character varying(10) DEFAULT 'run'::character varying NOT NULL,
    channel character varying(10) DEFAULT 'none'::character varying NOT NULL,
    draft boolean DEFAULT false NOT NULL,
    stdout_retention_config json,
    max_autonomy_level character varying(30),
    policy_gate_pins_json json,
    policy_gate_pins_fingerprint character varying(64),
    CONSTRAINT ck_pipeline_snapshots_channel CHECK (((channel)::text = ANY ((ARRAY['none'::character varying, 'stable'::character varying, 'canary'::character varying])::text[]))),
    CONSTRAINT ck_pipeline_snapshots_created_kind CHECK (((created_kind)::text = ANY ((ARRAY['initial'::character varying, 'edit'::character varying, 'rollback'::character varying, 'run'::character varying])::text[]))),
    CONSTRAINT ck_pipeline_snapshots_default_autonomy_level CHECK (((default_autonomy_level IS NULL) OR ((default_autonomy_level)::text = ANY ((ARRAY['manual_approval'::character varying, 'notify_on_complete'::character varying, 'fully_autonomous'::character varying])::text[])))),
    CONSTRAINT ck_pipeline_snapshots_max_autonomy_ge_default CHECK (((max_autonomy_level IS NULL) OR (
CASE default_autonomy_level
    WHEN 'manual_approval'::text THEN 0
    WHEN 'notify_on_complete'::text THEN 1
    WHEN 'fully_autonomous'::text THEN 2
    ELSE NULL::integer
END <=
CASE max_autonomy_level
    WHEN 'manual_approval'::text THEN 0
    WHEN 'notify_on_complete'::text THEN 1
    WHEN 'fully_autonomous'::text THEN 2
    ELSE NULL::integer
END))),
    CONSTRAINT ck_pipeline_snapshots_max_autonomy_level CHECK (((max_autonomy_level IS NULL) OR ((max_autonomy_level)::text = ANY ((ARRAY['manual_approval'::character varying, 'notify_on_complete'::character varying, 'fully_autonomous'::character varying])::text[])))),
    CONSTRAINT ck_pipeline_snapshots_version_kind CHECK (((version_kind)::text = ANY ((ARRAY['edit'::character varying, 'run'::character varying, 'draft'::character varying])::text[])))
);


--

-- Name: pipelines; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.pipelines (
    name character varying(255) NOT NULL,
    description character varying(2000),
    owner_team_id uuid,
    visibility character varying(10) DEFAULT 'org'::character varying NOT NULL,
    max_concurrent_runs integer DEFAULT 5 NOT NULL,
    lock_wait_timeout_seconds integer DEFAULT 300 NOT NULL,
    node_timeout_seconds integer DEFAULT 300 NOT NULL,
    max_duration_seconds integer DEFAULT 3600 NOT NULL,
    max_steps integer,
    token_budget integer,
    archived_at timestamp with time zone,
    default_autonomy_level character varying(30) DEFAULT 'manual_approval'::character varying,
    graph_nodes_json json DEFAULT '[]'::json NOT NULL,
    default_feedback_handler character varying(50),
    account_id uuid NOT NULL,
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    organisation_id uuid NOT NULL,
    created_at timestamp with time zone DEFAULT CURRENT_TIMESTAMP NOT NULL,
    updated_at timestamp with time zone DEFAULT CURRENT_TIMESTAMP NOT NULL,
    folder_id uuid,
    deleted_at timestamp with time zone,
    stale_run_timeout_minutes integer DEFAULT 30 NOT NULL,
    retry_policy json DEFAULT '{}'::json NOT NULL,
    circuit_breaker_threshold numeric(14,6),
    circuit_breaker_tripped boolean DEFAULT false NOT NULL,
    circuit_breaker_tripped_at timestamp with time zone,
    run_context_defaults jsonb DEFAULT '{}'::json NOT NULL,
    rate_limit_config jsonb,
    deleted_by uuid,
    collection_install_id uuid,
    stdout_retention_config json,
    max_autonomy_level character varying(30),
    business_owner_id uuid,
    reliability_owner_id uuid,
    hitl_review_window_seconds integer,
    run_enabled boolean DEFAULT true NOT NULL,
    run_disabled_reason character varying(20),
    run_disabled_at timestamp with time zone,
    environment_profile_id uuid,
    benign_signatures json,
    earned_autonomy_level character varying(30),
    earned_autonomy_updated_at timestamp with time zone,
    queue_timeout_seconds integer,
    CONSTRAINT ck_pipelines_autonomy_level CHECK (((default_autonomy_level)::text = ANY ((ARRAY['manual_approval'::character varying, 'notify_on_complete'::character varying, 'fully_autonomous'::character varying])::text[]))),
    CONSTRAINT ck_pipelines_circuit_breaker_threshold_positive CHECK (((circuit_breaker_threshold IS NULL) OR (circuit_breaker_threshold > (0)::numeric))),
    CONSTRAINT ck_pipelines_earned_autonomy_level CHECK (((earned_autonomy_level IS NULL) OR ((earned_autonomy_level)::text = ANY ((ARRAY['manual_approval'::character varying, 'notify_on_complete'::character varying, 'fully_autonomous'::character varying])::text[])))),
    CONSTRAINT ck_pipelines_hitl_review_window CHECK (((hitl_review_window_seconds IS NULL) OR ((hitl_review_window_seconds >= 60) AND (hitl_review_window_seconds <= 604800)))),
    CONSTRAINT ck_pipelines_lock_wait_timeout CHECK (((lock_wait_timeout_seconds >= 30) AND (lock_wait_timeout_seconds <= 3600))),
    CONSTRAINT ck_pipelines_max_autonomy_ge_default CHECK (((max_autonomy_level IS NULL) OR (
CASE default_autonomy_level
    WHEN 'manual_approval'::text THEN 0
    WHEN 'notify_on_complete'::text THEN 1
    WHEN 'fully_autonomous'::text THEN 2
    ELSE NULL::integer
END <=
CASE max_autonomy_level
    WHEN 'manual_approval'::text THEN 0
    WHEN 'notify_on_complete'::text THEN 1
    WHEN 'fully_autonomous'::text THEN 2
    ELSE NULL::integer
END))),
    CONSTRAINT ck_pipelines_max_autonomy_level CHECK (((max_autonomy_level IS NULL) OR ((max_autonomy_level)::text = ANY ((ARRAY['manual_approval'::character varying, 'notify_on_complete'::character varying, 'fully_autonomous'::character varying])::text[])))),
    CONSTRAINT ck_pipelines_max_concurrent_runs CHECK ((max_concurrent_runs > 0)),
    CONSTRAINT ck_pipelines_max_duration_positive CHECK ((max_duration_seconds > 0)),
    CONSTRAINT ck_pipelines_max_steps_positive CHECK (((max_steps IS NULL) OR (max_steps > 0))),
    CONSTRAINT ck_pipelines_node_timeout CHECK ((node_timeout_seconds > 0)),
    CONSTRAINT ck_pipelines_queue_timeout CHECK (((queue_timeout_seconds IS NULL) OR ((queue_timeout_seconds >= 1) AND (queue_timeout_seconds <= 604800)))),
    CONSTRAINT ck_pipelines_run_disabled_reason CHECK (((run_disabled_reason IS NULL) OR ((run_disabled_reason)::text = ANY ((ARRAY['operator'::character varying, 'circuit_breaker'::character varying])::text[])))),
    CONSTRAINT ck_pipelines_run_enabled CHECK ((run_enabled OR ((run_disabled_reason IS NOT NULL) AND (run_disabled_at IS NOT NULL)))),
    CONSTRAINT ck_pipelines_stale_timeout_positive CHECK ((stale_run_timeout_minutes > 0)),
    CONSTRAINT ck_pipelines_team_owner CHECK ((((visibility)::text = 'org'::text) OR (owner_team_id IS NOT NULL))),
    CONSTRAINT ck_pipelines_token_budget_positive CHECK (((token_budget IS NULL) OR (token_budget > 0))),
    CONSTRAINT ck_pipelines_visibility CHECK (((visibility)::text = ANY ((ARRAY['org'::character varying, 'team'::character varying])::text[])))
);


--

-- Name: policy_gate_decisions; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.policy_gate_decisions (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    organisation_id uuid NOT NULL,
    created_at timestamp with time zone DEFAULT CURRENT_TIMESTAMP NOT NULL,
    updated_at timestamp with time zone DEFAULT CURRENT_TIMESTAMP NOT NULL,
    policy_gate_id uuid NOT NULL,
    eval_id uuid NOT NULL,
    resolved_action text DEFAULT 'continue'::text NOT NULL,
    error_detail character varying(2000),
    node_id uuid,
    eval_result_id uuid,
    run_id uuid,
    policy_gate_version integer DEFAULT 1 NOT NULL,
    CONSTRAINT ck_policy_gate_decisions_resolved_action CHECK ((resolved_action = ANY (ARRAY['continue'::text, 'warn'::text, 'block'::text, 'escalate'::text])))
);



--

-- Name: policy_gates; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.policy_gates (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    organisation_id uuid NOT NULL,
    created_at timestamp with time zone DEFAULT CURRENT_TIMESTAMP NOT NULL,
    updated_at timestamp with time zone DEFAULT CURRENT_TIMESTAMP NOT NULL,
    version integer DEFAULT 1 NOT NULL,
    pre_version_raw json,
    eval_id uuid NOT NULL,
    node_id uuid NOT NULL,
    action text NOT NULL,
    deleted_at timestamp with time zone,
    deleted_by uuid,
    enabled boolean DEFAULT true NOT NULL,
    enabled_at timestamp with time zone DEFAULT now(),
    disabled_at timestamp with time zone,
    evaluation_mode text DEFAULT 'automatic'::text NOT NULL,
    CONSTRAINT ck_policy_gates_action CHECK ((action = ANY (ARRAY['warn'::text, 'block'::text, 'escalate'::text]))),
    CONSTRAINT ck_policy_gates_enabled_timestamps CHECK (((enabled AND (enabled_at IS NOT NULL) AND (disabled_at IS NULL)) OR ((NOT enabled) AND (disabled_at IS NOT NULL) AND (enabled_at IS NULL)))),
    CONSTRAINT ck_policy_gates_evaluation_mode CHECK ((evaluation_mode = ANY (ARRAY['automatic'::text, 'human'::text])))
);



--

-- Name: primitive_abuse_reports; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.primitive_abuse_reports (
    primitive_id uuid NOT NULL,
    rating_id uuid,
    reporter_account_id uuid,
    reason character varying(500) NOT NULL,
    status character varying(20) DEFAULT 'pending'::character varying NOT NULL,
    reviewed_at timestamp with time zone,
    reviewer_account_id uuid,
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    organisation_id uuid NOT NULL,
    created_at timestamp with time zone DEFAULT CURRENT_TIMESTAMP NOT NULL,
    updated_at timestamp with time zone DEFAULT CURRENT_TIMESTAMP NOT NULL,
    CONSTRAINT ck_abuse_reports_status CHECK (((status)::text = ANY ((ARRAY['pending'::character varying, 'reviewed'::character varying, 'dismissed'::character varying])::text[])))
);


--

-- Name: primitive_ratings; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.primitive_ratings (
    primitive_id uuid NOT NULL,
    account_id uuid,
    thumbs_up boolean NOT NULL,
    comment text,
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    organisation_id uuid NOT NULL,
    created_at timestamp with time zone DEFAULT CURRENT_TIMESTAMP NOT NULL,
    updated_at timestamp with time zone DEFAULT CURRENT_TIMESTAMP NOT NULL
);


--

-- Name: publishers; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.publishers (
    name character varying(255) NOT NULL,
    contact_email character varying(255),
    public_key_hex character varying(128) NOT NULL,
    trust_tier character varying(10) DEFAULT 'amber'::character varying NOT NULL,
    verified_since timestamp with time zone,
    website_url character varying(2000),
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    organisation_id uuid NOT NULL,
    created_at timestamp with time zone DEFAULT CURRENT_TIMESTAMP NOT NULL,
    updated_at timestamp with time zone DEFAULT CURRENT_TIMESTAMP NOT NULL,
    CONSTRAINT ck_publishers_trust_tier CHECK (((trust_tier)::text = ANY ((ARRAY['green'::character varying, 'amber'::character varying])::text[])))
);


--

-- Name: run_attempts; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.run_attempts (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    organisation_id uuid NOT NULL,
    run_id uuid NOT NULL,
    attempt_index integer NOT NULL,
    started_at timestamp with time zone DEFAULT CURRENT_TIMESTAMP NOT NULL,
    ended_at timestamp with time zone,
    terminal_reason character varying(30),
    model_used character varying(255),
    error_code character varying(255),
    created_at timestamp with time zone DEFAULT CURRENT_TIMESTAMP NOT NULL,
    updated_at timestamp with time zone DEFAULT CURRENT_TIMESTAMP NOT NULL,
    CONSTRAINT ck_run_attempts_attempt_index_non_negative CHECK ((attempt_index >= 0)),
    CONSTRAINT ck_run_attempts_terminal_reason CHECK (((terminal_reason)::text = ANY ((ARRAY['deadline'::character varying, 'stall'::character varying, 'guard_cap'::character varying, 'worker_lost'::character varying, 'provider_error'::character varying, 'completed'::character varying, 'reset'::character varying])::text[])))
);



--

-- Name: run_daily_facts; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.run_daily_facts (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    organisation_id uuid NOT NULL,
    created_at timestamp with time zone DEFAULT CURRENT_TIMESTAMP NOT NULL,
    updated_at timestamp with time zone DEFAULT CURRENT_TIMESTAMP NOT NULL,
    run_id uuid NOT NULL,
    run_date date NOT NULL,
    team_id uuid,
    team_name character varying(255),
    pipeline_id uuid,
    pipeline_name character varying(255),
    folder_id uuid,
    trigger_type character varying(20) NOT NULL,
    status character varying(30) NOT NULL,
    total_cost_usd numeric(14,6),
    total_tokens integer,
    duration_ms bigint,
    error_code character varying(255),
    claim_count integer,
    queue_wait_ms bigint,
    final_idle_ms bigint,
    cancellation_requested boolean,
    dispatcher character varying(20),
    node_count integer,
    sandbox_agent_node_count integer,
    max_node_timeout_seconds integer,
    parent_run_id uuid,
    snapshot_id uuid,
    run_number integer,
    output_bytes bigint,
    rate_limited boolean,
    dispatched_at timestamp with time zone,
    started_at timestamp with time zone,
    completed_at timestamp with time zone,
    total_queue_wait_ms bigint,
    telemetry_bytes bigint,
    batch_id uuid,
    workspace_inputs_count integer,
    enforcement_native_count integer,
    enforcement_verbatim_count integer,
    enforcement_repair_count integer,
    enforcement_wasted_count integer,
    trigger_id uuid,
    dispatch_phase text,
    dispatch_phase_entered_at timestamp with time zone,
    node_deadline_watchdog_fired_count integer,
    execution_origin character varying(20),
    benign boolean,
    autonomy_level character varying(30),
    CONSTRAINT ck_run_daily_facts_status CHECK (((status)::text = ANY ((ARRAY['pending'::character varying, 'running'::character varying, 'awaiting_human'::character varying, 'claimed'::character varying, 'unknown'::character varying, 'complete'::character varying, 'failed'::character varying, 'cancelled'::character varying, 'eval_failed'::character varying, 'stalled'::character varying, 'budget_exceeded'::character varying, 'router_no_match'::character varying, 'compensation_failed'::character varying, 'cost_ceiling_exceeded'::character varying, 'rejected'::character varying])::text[]))),
    CONSTRAINT ck_run_daily_facts_trigger_type CHECK (((trigger_type)::text = ANY ((ARRAY['manual'::character varying, 'webhook'::character varying, 'cron'::character varying, 'polling'::character varying, 'agent_signal'::character varying, 'ongoing'::character varying, 'correction'::character varying, 'slack_app_mention'::character varying, 'rerun'::character varying])::text[])))
);


--

-- Name: COLUMN run_daily_facts.enforcement_native_count; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.run_daily_facts.enforcement_native_count IS 'count of attempts with native structured output (FAR-902)';


--

-- Name: COLUMN run_daily_facts.enforcement_verbatim_count; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.run_daily_facts.enforcement_verbatim_count IS 'count of attempts with verbatim output (FAR-902)';


--

-- Name: COLUMN run_daily_facts.enforcement_repair_count; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.run_daily_facts.enforcement_repair_count IS 'total repair loop invocations across all attempts (FAR-902)';


--

-- Name: COLUMN run_daily_facts.enforcement_wasted_count; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.run_daily_facts.enforcement_wasted_count IS 'total wasted attempts (schema rejection only, FAR-902)';


--

-- Name: COLUMN run_daily_facts.trigger_id; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.run_daily_facts.trigger_id IS 'NOT a FK — facts survive the run purge (ADR 020); NULL for manual runs and for pre-FAR-1421 facts';


--

-- Name: COLUMN run_daily_facts.dispatch_phase_entered_at; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.run_daily_facts.dispatch_phase_entered_at IS 'when runs.dispatch_phase was last entered — from Run.dispatch_phase_entered_at';


--

-- Name: COLUMN run_daily_facts.node_deadline_watchdog_fired_count; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.run_daily_facts.node_deadline_watchdog_fired_count IS 'node-deadline watchdog firings — from Run.node_deadline_watchdog_fired_count (NULL for pre-FAR-1463 facts)';


--

-- Name: COLUMN run_daily_facts.execution_origin; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.run_daily_facts.execution_origin IS 'run-level execution provenance — from runs.execution_origin at finalize (FAR-1141); NULL for facts finalized before this shipped';


--

-- Name: run_evidence; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.run_evidence (
    run_id uuid NOT NULL,
    node_id uuid DEFAULT gen_random_uuid() NOT NULL,
    evidence_state character varying(20) NOT NULL,
    evidence_detail character varying(2000),
    evidence_written_at timestamp with time zone DEFAULT CURRENT_TIMESTAMP NOT NULL,
    organisation_id uuid NOT NULL,
    CONSTRAINT ck_run_evidence_state CHECK (((evidence_state)::text = ANY ((ARRAY['has_work'::character varying, 'verified_empty'::character varying, 'unverifiable'::character varying])::text[])))
);



--

-- Name: run_node_outputs; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.run_node_outputs (
    run_id uuid NOT NULL,
    node_id text NOT NULL,
    attempt_key text NOT NULL,
    organisation_id uuid NOT NULL,
    outputs_json jsonb,
    node_telemetry_json jsonb,
    raw_output_markers jsonb,
    created_at timestamp with time zone DEFAULT CURRENT_TIMESTAMP NOT NULL,
    updated_at timestamp with time zone DEFAULT CURRENT_TIMESTAMP NOT NULL,
    artifacts_json jsonb,
    schema_enforcement_json json,
    completed_at timestamp with time zone,
    CONSTRAINT ck_run_node_outputs_final_no_enforcement CHECK (((attempt_key <> '__final__'::text) OR (schema_enforcement_json IS NULL))),
    CONSTRAINT ck_run_node_outputs_final_no_markers CHECK (((attempt_key <> '__final__'::text) OR (raw_output_markers IS NULL))),
    CONSTRAINT ck_run_node_outputs_meta_shape CHECK (((node_id <> '__run_meta__'::text) OR ((jsonb_typeof(outputs_json) = 'object'::text) AND (((outputs_json - 'empty_outputs'::text) - 'empty_telemetry'::text) = '{}'::jsonb) AND (jsonb_typeof((outputs_json -> 'empty_outputs'::text)) = 'boolean'::text) AND (jsonb_typeof((outputs_json -> 'empty_telemetry'::text)) = 'boolean'::text))))
);



--

-- Name: run_node_outputs_quarantine; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.run_node_outputs_quarantine (
    run_id uuid NOT NULL,
    organisation_id uuid NOT NULL,
    legacy_outputs_json jsonb,
    legacy_node_telemetry_json jsonb,
    legacy_raw_output_markers jsonb,
    quarantined_at timestamp with time zone DEFAULT CURRENT_TIMESTAMP NOT NULL
);


--

-- Name: run_number_counters; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.run_number_counters (
    organisation_id uuid NOT NULL,
    next_run_number integer DEFAULT 1 NOT NULL
);


--

-- Name: runner_probe_cache; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.runner_probe_cache (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    organisation_id uuid NOT NULL,
    machine_id character varying(255) NOT NULL,
    engine_reachable boolean NOT NULL,
    images_present boolean,
    image_checks_json json NOT NULL,
    engine_info_json json NOT NULL,
    probe_error character varying(500),
    probed_at timestamp with time zone NOT NULL,
    created_at timestamp with time zone DEFAULT CURRENT_TIMESTAMP NOT NULL,
    updated_at timestamp with time zone DEFAULT CURRENT_TIMESTAMP NOT NULL
);



--

-- Name: runs; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.runs (
    pipeline_id uuid NOT NULL,
    snapshot_id uuid NOT NULL,
    trigger_id uuid,
    trigger_type character varying(20) NOT NULL,
    status character varying(30) DEFAULT 'pending'::character varying NOT NULL,
    parent_run_id uuid,
    run_number integer DEFAULT 0 NOT NULL,
    owner_team_id uuid,
    account_id uuid,
    input_hash character varying(64) NOT NULL,
    started_at timestamp with time zone,
    completed_at timestamp with time zone,
    cancellation_requested boolean DEFAULT false NOT NULL,
    total_tokens integer,
    total_cost_usd numeric(14,6),
    error_detail text,
    error_code character varying(255),
    langgraph_thread_id character varying(512) NOT NULL,
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    organisation_id uuid NOT NULL,
    created_at timestamp with time zone DEFAULT CURRENT_TIMESTAMP NOT NULL,
    updated_at timestamp with time zone DEFAULT CURRENT_TIMESTAMP NOT NULL,
    rate_limit_key character varying(512),
    heartbeat_at timestamp with time zone,
    dispatched_at timestamp with time zone,
    claim_count integer DEFAULT 0 NOT NULL,
    claimed_by character varying(64),
    dispatcher character varying(20),
    saq_job_id character varying(255),
    claim_token character varying(128) DEFAULT (gen_random_uuid())::text NOT NULL,
    ledger_written boolean DEFAULT false NOT NULL,
    ledger_refused_at timestamp with time zone,
    node_attempt_count integer DEFAULT 0 NOT NULL,
    enqueue_failed_at timestamp with time zone,
    sandbox_dispatch_state text,
    sandbox_id text,
    work_item_id uuid,
    work_item_refs jsonb,
    is_replay boolean,
    variant_group_id uuid,
    work_intact boolean,
    run_classification jsonb,
    blocked_partial_summary jsonb,
    batch_id uuid,
    cost_breakdown jsonb,
    node_token_usage jsonb,
    input_payload jsonb,
    guardrail_summary_json jsonb,
    variant_config_snapshot jsonb,
    idempotency_key character varying(128),
    workspace_inputs_drift_detected boolean,
    schema_validator_mode character varying(30),
    schema_validation_outcome character varying(40),
    cancel_reason character varying(64),
    cancelled_by character varying(64),
    dispatch_phase text,
    dispatch_phase_entered_at timestamp with time zone,
    node_deadline_watchdog_fired_count integer DEFAULT 0 NOT NULL,
    execution_origin character varying(20),
    run_group_id uuid,
    run_group_kind character varying(16),
    guardrail_override_resume_pending boolean DEFAULT false NOT NULL,
    dispatch_phase_history jsonb,
    dispatch_phase_write_drops integer DEFAULT 0 NOT NULL,
    seam_coverage_json jsonb,
    execution_attempt_count integer DEFAULT 0 NOT NULL,
    CONSTRAINT ck_run_claim_count CHECK ((claim_count >= 0)),
    CONSTRAINT ck_run_node_attempt_count CHECK ((node_attempt_count >= 0)),
    CONSTRAINT ck_runs_cancel_reason CHECK (((cancel_reason IS NULL) OR ((cancel_reason)::text = ANY ((ARRAY['user_requested'::character varying, 'agent_requested'::character varying, 'hitl_review_expired'::character varying, 'hitl_review_missing'::character varying])::text[])))),
    CONSTRAINT ck_runs_claim_count_nonneg CHECK ((claim_count >= 0)),
    CONSTRAINT ck_runs_completed_after_started CHECK (((completed_at IS NULL) OR (started_at IS NULL) OR (completed_at >= started_at))),
    CONSTRAINT ck_runs_node_attempt_count_nonneg CHECK ((node_attempt_count >= 0)),
    CONSTRAINT ck_runs_parent_not_self CHECK (((parent_run_id IS NULL) OR (parent_run_id <> id))),
    CONSTRAINT ck_runs_status CHECK (((status)::text = ANY (ARRAY[('pending'::character varying)::text, ('running'::character varying)::text, ('awaiting_human'::character varying)::text, ('claimed'::character varying)::text, ('unknown'::character varying)::text, ('hitl_parked'::character varying)::text, ('complete'::character varying)::text, ('failed'::character varying)::text, ('cancelled'::character varying)::text, ('eval_failed'::character varying)::text, ('stalled'::character varying)::text, ('budget_exceeded'::character varying)::text, ('cost_ceiling_exceeded'::character varying)::text, ('router_no_match'::character varying)::text, ('compensation_failed'::character varying)::text, ('rejected'::character varying)::text]))),
    CONSTRAINT ck_runs_trigger_type CHECK (((trigger_type)::text = ANY ((ARRAY['manual'::character varying, 'webhook'::character varying, 'cron'::character varying, 'polling'::character varying, 'agent_signal'::character varying, 'ongoing'::character varying, 'correction'::character varying, 'slack_app_mention'::character varying, 'rerun'::character varying])::text[])))
)
WITH (autovacuum_enabled='true', autovacuum_vacuum_scale_factor='0.05', autovacuum_analyze_scale_factor='0.02');


--

-- Name: COLUMN runs.schema_validator_mode; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.runs.schema_validator_mode IS 'schema validator mode the run executed under (lenient|strict, FAR-902)';


--

-- Name: COLUMN runs.schema_validation_outcome; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.runs.schema_validation_outcome IS 'run-level schema validation outcome derived from per-node enforcement records (FAR-902)';


--

-- Name: COLUMN runs.node_deadline_watchdog_fired_count; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.runs.node_deadline_watchdog_fired_count IS 'node-deadline watchdog (FAR-369) firings — incremented on every firing, re-dispatch and terminal-fail alike (FAR-1463); copied to run_daily_facts at finalize';


--

-- Name: COLUMN runs.execution_origin; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.runs.execution_origin IS 'run-level execution provenance (FAR-1141): ''dispatched'' = the run''s frozen snapshot graph contains at least one dispatch node; NULL = executed by Modulo / recorded before this shipped';


--

-- Name: COLUMN runs.run_group_id; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.runs.run_group_id IS 'root run id of the retry/resume group this attempt belongs to (FAR-153 / 15.1)';


--

-- Name: COLUMN runs.run_group_kind; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.runs.run_group_kind IS 'why this row joined its group: ''retry'' | ''resume'' | NULL (FAR-153 / 15.1)';


--

-- Name: COLUMN runs.execution_attempt_count; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.runs.execution_attempt_count IS 'execution attempts of the graph (execute_run only - never resume_run, never a capacity-demoted claim); a pre-node death counts (FAR-2076)';


--

-- Name: saved_views; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.saved_views (
    name character varying(255) NOT NULL,
    description text,
    view_type character varying(50) NOT NULL,
    sort_by character varying(100),
    sort_order character varying(10) NOT NULL,
    account_id uuid NOT NULL,
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    organisation_id uuid NOT NULL,
    created_at timestamp with time zone DEFAULT CURRENT_TIMESTAMP NOT NULL,
    updated_at timestamp with time zone DEFAULT CURRENT_TIMESTAMP NOT NULL,
    deleted_at timestamp with time zone,
    filters jsonb NOT NULL,
    columns jsonb,
    CONSTRAINT ck_saved_views_type CHECK (((view_type)::text = ANY ((ARRAY['run_list'::character varying, 'pipeline_list'::character varying, 'audit_log'::character varying])::text[])))
);


--

-- Name: scheduled_reports; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.scheduled_reports (
    name character varying(255) NOT NULL,
    report_type character varying(50) NOT NULL,
    cron_expression character varying(100) NOT NULL,
    last_sent_at timestamp with time zone,
    next_send_at timestamp with time zone,
    active boolean DEFAULT true NOT NULL,
    created_by uuid,
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    organisation_id uuid NOT NULL,
    created_at timestamp with time zone DEFAULT CURRENT_TIMESTAMP NOT NULL,
    updated_at timestamp with time zone DEFAULT CURRENT_TIMESTAMP NOT NULL,
    deleted_at timestamp with time zone,
    deleted_by uuid,
    updated_by uuid,
    config_json jsonb,
    recipient_config jsonb
);


--

-- Name: schema_folders; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.schema_folders (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    organisation_id uuid NOT NULL,
    name character varying(255) NOT NULL,
    parent_id uuid,
    sort_order integer DEFAULT 0 NOT NULL,
    account_id uuid NOT NULL,
    created_at timestamp with time zone DEFAULT CURRENT_TIMESTAMP NOT NULL,
    updated_at timestamp with time zone DEFAULT CURRENT_TIMESTAMP NOT NULL
);


--

-- Name: schema_versions; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.schema_versions (
    schema_id uuid NOT NULL,
    version character varying(50) NOT NULL,
    version_number integer NOT NULL,
    published boolean DEFAULT false NOT NULL,
    deprecated boolean DEFAULT false NOT NULL,
    account_id uuid NOT NULL,
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    organisation_id uuid NOT NULL,
    created_at timestamp with time zone DEFAULT CURRENT_TIMESTAMP NOT NULL,
    updated_at timestamp with time zone DEFAULT CURRENT_TIMESTAMP NOT NULL,
    definition_json jsonb NOT NULL
);


--

-- Name: schemas; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.schemas (
    name character varying(255) NOT NULL,
    description character varying(2000),
    abstract_name character varying(255),
    account_id uuid NOT NULL,
    deprecated boolean DEFAULT false NOT NULL,
    deprecated_at timestamp with time zone,
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    organisation_id uuid NOT NULL,
    created_at timestamp with time zone DEFAULT CURRENT_TIMESTAMP NOT NULL,
    updated_at timestamp with time zone DEFAULT CURRENT_TIMESTAMP NOT NULL,
    system boolean DEFAULT false NOT NULL,
    folder_id uuid,
    collection_install_id uuid
);


--

-- Name: secrets; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.secrets (
    key character varying(255) NOT NULL,
    encrypted_value bytea NOT NULL,
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    organisation_id uuid NOT NULL,
    created_at timestamp with time zone DEFAULT CURRENT_TIMESTAMP NOT NULL,
    updated_at timestamp with time zone DEFAULT CURRENT_TIMESTAMP NOT NULL
);


--

-- Name: snapshot_schema_pins; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.snapshot_schema_pins (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    organisation_id uuid NOT NULL,
    snapshot_id uuid NOT NULL,
    node_id uuid NOT NULL,
    direction character varying(10) NOT NULL,
    schema_id uuid NOT NULL,
    schema_version character varying(50) NOT NULL,
    created_at timestamp with time zone DEFAULT CURRENT_TIMESTAMP NOT NULL,
    updated_at timestamp with time zone DEFAULT CURRENT_TIMESTAMP NOT NULL,
    CONSTRAINT ck_snapshot_schema_pins_direction CHECK (((direction)::text = ANY (ARRAY[('input'::character varying)::text, ('output'::character varying)::text])))
);


--

-- Name: spend_anomalies; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.spend_anomalies (
    anomaly_date date NOT NULL,
    pipeline_id uuid,
    amount numeric(14,6) NOT NULL,
    baseline numeric(14,6) NOT NULL,
    percent_above numeric(8,2) NOT NULL,
    dismissed boolean DEFAULT false NOT NULL,
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    organisation_id uuid NOT NULL,
    created_at timestamp with time zone DEFAULT CURRENT_TIMESTAMP NOT NULL,
    updated_at timestamp with time zone DEFAULT CURRENT_TIMESTAMP NOT NULL,
    CONSTRAINT ck_spend_anomalies_amount CHECK ((amount >= (0)::numeric)),
    CONSTRAINT ck_spend_anomalies_baseline CHECK ((baseline >= (0)::numeric)),
    CONSTRAINT ck_spend_anomalies_percent_above CHECK ((percent_above >= (0)::numeric))
);


--

-- Name: sso_providers; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.sso_providers (
    provider_type character varying(16) NOT NULL,
    name character varying(255) NOT NULL,
    client_id character varying(1024),
    client_secret bytea,
    discovery_url character varying(2048),
    metadata_url character varying(2048),
    metadata_xml text,
    entity_id character varying(1024),
    scopes text,
    enabled boolean DEFAULT true NOT NULL,
    auto_provision boolean DEFAULT false NOT NULL,
    default_role character varying(32) DEFAULT 'runner'::character varying NOT NULL,
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    organisation_id uuid NOT NULL,
    created_at timestamp with time zone DEFAULT CURRENT_TIMESTAMP NOT NULL,
    updated_at timestamp with time zone DEFAULT CURRENT_TIMESTAMP NOT NULL,
    group_mappings jsonb DEFAULT '[]'::json NOT NULL,
    provider_id character varying(64),
    allowed_domains jsonb DEFAULT '[]'::jsonb NOT NULL,
    preset character varying(32) DEFAULT 'custom'::character varying NOT NULL,
    tenant_domain character varying(255)
);


--

-- Name: suite_runs; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.suite_runs (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    organisation_id uuid NOT NULL,
    owner_team_id uuid,
    suite_id uuid NOT NULL,
    dataset_id uuid NOT NULL,
    dataset_version integer DEFAULT 1 NOT NULL,
    definition_checksum character varying(64) NOT NULL,
    model_backend_id uuid NOT NULL,
    scenario_signature character varying(64),
    baseline_tuple json,
    baseline_run_id uuid,
    baseline_locked boolean DEFAULT false NOT NULL,
    state character varying(20) DEFAULT 'pending'::character varying NOT NULL,
    version integer DEFAULT 0 NOT NULL,
    total_cases integer DEFAULT 0 NOT NULL,
    passed_cases integer DEFAULT 0 NOT NULL,
    failed_cases integer DEFAULT 0 NOT NULL,
    excluded_case_count integer DEFAULT 0 NOT NULL,
    total_cost_usd numeric(14,6),
    claimed_cost numeric(14,6) DEFAULT '0'::numeric,
    comparison_json json,
    regressed boolean,
    notified_at timestamp with time zone,
    completed_at timestamp with time zone,
    error_detail character varying(2000),
    extra json,
    created_at timestamp with time zone DEFAULT CURRENT_TIMESTAMP NOT NULL,
    updated_at timestamp with time zone DEFAULT CURRENT_TIMESTAMP NOT NULL,
    CONSTRAINT ck_eval_suite_run_excluded_cases CHECK ((excluded_case_count >= 0)),
    CONSTRAINT ck_eval_suite_run_failed_cases CHECK ((failed_cases >= 0)),
    CONSTRAINT ck_eval_suite_run_failed_le_total CHECK ((failed_cases <= total_cases)),
    CONSTRAINT ck_eval_suite_run_passed_cases CHECK ((passed_cases >= 0)),
    CONSTRAINT ck_eval_suite_run_passed_le_total CHECK ((passed_cases <= total_cases)),
    CONSTRAINT ck_eval_suite_run_total_cases CHECK ((total_cases >= 0)),
    CONSTRAINT ck_eval_suite_runs_claimed_cost CHECK (((claimed_cost IS NULL) OR (claimed_cost >= (0)::numeric))),
    CONSTRAINT ck_suite_runs_case_counts CHECK (((total_cases >= 0) AND (passed_cases >= 0) AND (failed_cases >= 0) AND (excluded_case_count >= 0))),
    CONSTRAINT ck_suite_runs_case_counts_consistent CHECK ((((passed_cases + failed_cases) + excluded_case_count) <= total_cases)),
    CONSTRAINT ck_suite_runs_dataset_version_gte_1 CHECK ((dataset_version >= 1)),
    CONSTRAINT ck_suite_runs_state CHECK (((state)::text = ANY ((ARRAY['pending'::character varying, 'running'::character varying, 'completed'::character varying, 'partial'::character varying, 'failed'::character varying, 'cancelled'::character varying])::text[]))),
    CONSTRAINT ck_suite_runs_total_cost_non_negative CHECK (((total_cost_usd IS NULL) OR (total_cost_usd >= (0)::numeric))),
    CONSTRAINT ck_suite_runs_version_gte_0 CHECK ((version >= 0))
);



--

-- Name: system_audit_events; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.system_audit_events (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    event_type character varying(100) NOT NULL,
    org_id uuid,
    actor_user_id uuid,
    resource_type character varying(100),
    resource_id uuid,
    payload_json json DEFAULT '{}'::json NOT NULL,
    request_id character varying(255),
    created_at timestamp with time zone DEFAULT CURRENT_TIMESTAMP NOT NULL
);


--

-- Name: system_config; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.system_config (
    key character varying(255) NOT NULL,
    updated_at timestamp with time zone DEFAULT CURRENT_TIMESTAMP NOT NULL,
    updated_by uuid,
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    value jsonb NOT NULL
);


--

-- Name: system_map_attribution_bindings; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.system_map_attribution_bindings (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    organisation_id uuid NOT NULL,
    pipeline_id uuid NOT NULL,
    trigger_id uuid,
    system_map_id uuid NOT NULL,
    node_key character varying(100) NOT NULL,
    binding_kind character varying(30) NOT NULL,
    created_at timestamp with time zone DEFAULT CURRENT_TIMESTAMP NOT NULL,
    updated_at timestamp with time zone DEFAULT CURRENT_TIMESTAMP NOT NULL,
    deleted_at timestamp with time zone,
    deleted_by uuid,
    CONSTRAINT ck_system_map_attribution_bindings_kind CHECK (((binding_kind)::text = ANY ((ARRAY['trigger'::character varying, 'action_pipeline'::character varying])::text[]))),
    CONSTRAINT ck_system_map_attribution_bindings_trigger_semantics CHECK (((((binding_kind)::text = 'trigger'::text) AND (trigger_id IS NOT NULL)) OR (((binding_kind)::text = 'action_pipeline'::text) AND (trigger_id IS NULL))))
);



--

-- Name: system_map_loop_admission_attempts; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.system_map_loop_admission_attempts (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    organisation_id uuid NOT NULL,
    loop_config_id uuid NOT NULL,
    idempotency_key character varying(255) NOT NULL,
    status character varying(30) DEFAULT 'pending'::character varying NOT NULL,
    created_at timestamp with time zone DEFAULT CURRENT_TIMESTAMP NOT NULL,
    updated_at timestamp with time zone DEFAULT CURRENT_TIMESTAMP NOT NULL,
    deleted_at timestamp with time zone,
    deleted_by uuid,
    CONSTRAINT ck_loop_admission_attempts_status CHECK (((status)::text = ANY ((ARRAY['pending'::character varying, 'admitted'::character varying, 'rejected'::character varying, 'duplicate'::character varying])::text[])))
);



--

-- Name: system_map_loop_configs; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.system_map_loop_configs (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    organisation_id uuid NOT NULL,
    revision_id uuid NOT NULL,
    node_key character varying(100) NOT NULL,
    target_signal character varying(100) NOT NULL,
    observation_window_seconds integer NOT NULL,
    action_pipeline_id uuid NOT NULL,
    budget_limit_usd numeric(14,6) NOT NULL,
    cooldown_seconds integer NOT NULL,
    outcome_metric character varying(100) NOT NULL,
    created_at timestamp with time zone DEFAULT CURRENT_TIMESTAMP NOT NULL,
    updated_at timestamp with time zone DEFAULT CURRENT_TIMESTAMP NOT NULL,
    deleted_at timestamp with time zone,
    deleted_by uuid,
    CONSTRAINT ck_loop_configs_budget CHECK ((budget_limit_usd >= (0)::numeric)),
    CONSTRAINT ck_loop_configs_cooldown CHECK ((cooldown_seconds >= 0)),
    CONSTRAINT ck_loop_configs_obs_window CHECK ((observation_window_seconds > 0))
);



--

-- Name: system_map_loop_reservations; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.system_map_loop_reservations (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    organisation_id uuid NOT NULL,
    loop_config_id uuid NOT NULL,
    run_id uuid NOT NULL,
    idempotency_key character varying(255) NOT NULL,
    reserved_at timestamp with time zone DEFAULT CURRENT_TIMESTAMP NOT NULL,
    cooldown_until timestamp with time zone NOT NULL,
    status character varying(30) DEFAULT 'active'::character varying NOT NULL,
    consumed_at timestamp with time zone,
    released_at timestamp with time zone,
    created_at timestamp with time zone DEFAULT CURRENT_TIMESTAMP NOT NULL,
    updated_at timestamp with time zone DEFAULT CURRENT_TIMESTAMP NOT NULL,
    deleted_at timestamp with time zone,
    deleted_by uuid,
    CONSTRAINT ck_loop_reservations_status CHECK (((status)::text = ANY ((ARRAY['active'::character varying, 'consumed'::character varying, 'released'::character varying, 'expired'::character varying])::text[])))
);



--

-- Name: system_map_nodes; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.system_map_nodes (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    organisation_id uuid NOT NULL,
    revision_id uuid NOT NULL,
    node_key character varying(100) NOT NULL,
    node_type character varying(50) NOT NULL,
    display_name character varying(255) NOT NULL,
    config_json jsonb DEFAULT '{}'::jsonb NOT NULL,
    pipeline_id uuid,
    connector_instance_id uuid,
    owner_team_id uuid,
    eval_id uuid,
    position_x numeric(10,2) DEFAULT '0'::numeric NOT NULL,
    position_y numeric(10,2) DEFAULT '0'::numeric NOT NULL,
    created_at timestamp with time zone DEFAULT CURRENT_TIMESTAMP NOT NULL,
    updated_at timestamp with time zone DEFAULT CURRENT_TIMESTAMP NOT NULL,
    deleted_at timestamp with time zone,
    deleted_by uuid,
    CONSTRAINT ck_system_map_nodes_type CHECK (((node_type)::text = ANY ((ARRAY['pipeline'::character varying, 'agent'::character varying, 'connector'::character varying, 'improvement_loop'::character varying, 'data_store'::character varying, 'external_system'::character varying])::text[])))
);



--

-- Name: system_map_relationships; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.system_map_relationships (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    organisation_id uuid NOT NULL,
    revision_id uuid NOT NULL,
    source_node_key character varying(100) NOT NULL,
    target_node_key character varying(100) NOT NULL,
    relationship_type character varying(50) NOT NULL,
    config_json jsonb DEFAULT '{}'::jsonb NOT NULL,
    created_at timestamp with time zone DEFAULT CURRENT_TIMESTAMP NOT NULL,
    updated_at timestamp with time zone DEFAULT CURRENT_TIMESTAMP NOT NULL,
    deleted_at timestamp with time zone,
    deleted_by uuid,
    CONSTRAINT ck_system_map_relationships_type CHECK (((relationship_type)::text = ANY ((ARRAY['flow'::character varying, 'data'::character varying, 'trigger'::character varying, 'loop_back'::character varying])::text[])))
);



--

-- Name: system_map_revisions; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.system_map_revisions (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    organisation_id uuid NOT NULL,
    system_map_id uuid NOT NULL,
    revision_number integer NOT NULL,
    status character varying(30) DEFAULT 'draft'::character varying NOT NULL,
    content_json jsonb DEFAULT '{}'::jsonb NOT NULL,
    created_at timestamp with time zone DEFAULT CURRENT_TIMESTAMP NOT NULL,
    updated_at timestamp with time zone DEFAULT CURRENT_TIMESTAMP NOT NULL,
    deleted_at timestamp with time zone,
    deleted_by uuid,
    published_at timestamp with time zone,
    effective_from timestamp with time zone,
    effective_to timestamp with time zone,
    CONSTRAINT ck_system_map_revisions_status CHECK (((status)::text = ANY ((ARRAY['draft'::character varying, 'published'::character varying, 'archived'::character varying])::text[])))
);



--

-- Name: system_maps; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.system_maps (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    organisation_id uuid NOT NULL,
    name character varying(255) NOT NULL,
    description character varying(2000),
    owner_team_id uuid,
    visibility character varying(10) DEFAULT 'org'::character varying NOT NULL,
    default_map boolean DEFAULT false NOT NULL,
    published_revision_id uuid,
    account_id uuid NOT NULL,
    updated_by uuid,
    deleted_by uuid,
    deleted_at timestamp with time zone,
    created_at timestamp with time zone DEFAULT CURRENT_TIMESTAMP NOT NULL,
    updated_at timestamp with time zone DEFAULT CURRENT_TIMESTAMP NOT NULL,
    CONSTRAINT ck_system_maps_team_owner CHECK ((((visibility)::text = 'org'::text) OR (owner_team_id IS NOT NULL))),
    CONSTRAINT ck_system_maps_visibility CHECK (((visibility)::text = ANY ((ARRAY['org'::character varying, 'team'::character varying])::text[])))
);



--

-- Name: team_memberships; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.team_memberships (
    team_id uuid NOT NULL,
    account_id uuid NOT NULL,
    role character varying(20) DEFAULT 'viewer'::character varying NOT NULL,
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    organisation_id uuid NOT NULL,
    created_at timestamp with time zone DEFAULT CURRENT_TIMESTAMP NOT NULL,
    updated_at timestamp with time zone DEFAULT CURRENT_TIMESTAMP NOT NULL,
    CONSTRAINT ck_team_memberships_role CHECK (((role)::text = ANY ((ARRAY['viewer'::character varying, 'runner'::character varying, 'operator'::character varying])::text[])))
);


--

-- Name: teams; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.teams (
    name character varying(255) NOT NULL,
    description character varying(2000),
    account_id uuid NOT NULL,
    daily_spend_limit numeric(14,6),
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    organisation_id uuid NOT NULL,
    created_at timestamp with time zone DEFAULT CURRENT_TIMESTAMP NOT NULL,
    updated_at timestamp with time zone DEFAULT CURRENT_TIMESTAMP NOT NULL,
    deleted_at timestamp with time zone,
    notification_endpoints jsonb NOT NULL,
    settings jsonb NOT NULL,
    CONSTRAINT ck_teams_daily_spend_limit CHECK (((daily_spend_limit IS NULL) OR (daily_spend_limit >= (0)::numeric)))
);


--

-- Name: tier_catalog; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.tier_catalog (
    tier_id character varying(255) NOT NULL,
    label character varying(255) NOT NULL,
    rank integer NOT NULL,
    requires_license boolean DEFAULT false NOT NULL,
    description character varying(2000)
);


--

-- Name: token_families; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.token_families (
    family_id uuid DEFAULT gen_random_uuid() NOT NULL,
    account_id uuid NOT NULL,
    organisation_id uuid NOT NULL,
    max_sequence integer NOT NULL,
    is_blacklisted boolean NOT NULL,
    created_at timestamp with time zone DEFAULT CURRENT_TIMESTAMP NOT NULL,
    blacklisted_at timestamp with time zone,
    rotated_at timestamp with time zone
);


--

-- Name: trigger_event_findings; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.trigger_event_findings (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    organisation_id uuid NOT NULL,
    trigger_event_id uuid NOT NULL,
    eval_id uuid NOT NULL,
    passed boolean NOT NULL,
    outcome text NOT NULL,
    score double precision,
    detail text,
    resolution text DEFAULT 'pending'::text NOT NULL,
    resolution_reason text,
    resolved_at timestamp with time zone,
    resolved_by uuid,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT ck_trigger_event_findings_resolution CHECK ((resolution = ANY (ARRAY['accepted'::text, 'dismissed'::text, 'superseded'::text, 'pending'::text])))
);



--

-- Name: trigger_events; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.trigger_events (
    trigger_id uuid NOT NULL,
    trigger_type character varying(20) NOT NULL,
    raw_payload_hash character varying(64) NOT NULL,
    received_at timestamp with time zone DEFAULT CURRENT_TIMESTAMP NOT NULL,
    validation_result character varying(50) NOT NULL,
    run_id uuid,
    error_detail character varying(2000),
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    organisation_id uuid NOT NULL,
    created_at timestamp with time zone DEFAULT CURRENT_TIMESTAMP NOT NULL,
    updated_at timestamp with time zone DEFAULT CURRENT_TIMESTAMP NOT NULL,
    CONSTRAINT ck_trigger_events_trigger_type CHECK (((trigger_type)::text = ANY ((ARRAY['manual'::character varying, 'webhook'::character varying, 'cron'::character varying, 'polling'::character varying, 'agent_signal'::character varying, 'ongoing'::character varying, 'slack_app_mention'::character varying])::text[]))),
    CONSTRAINT ck_trigger_events_validation_result CHECK (((validation_result)::text = ANY (ARRAY[('accepted'::character varying)::text, ('passed'::character varying)::text, ('hmac_failed'::character varying)::text, ('schema_validation_failed'::character varying)::text, ('deduplicated'::character varying)::text, ('concurrency_limit_reached'::character varying)::text, ('flood_rejected'::character varying)::text, ('timestamp_expired'::character varying)::text, ('validation_failed'::character varying)::text, ('rate_limited'::character varying)::text, ('no_match'::character varying)::text, ('condition_met'::character varying)::text, ('poll_error'::character varying)::text, ('signal_fired'::character varying)::text, ('event_type_not_accepted'::character varying)::text, ('spend_limit_reached'::character varying)::text, ('no_pipeline'::character varying)::text, ('test'::character varying)::text, ('paused'::character varying)::text, ('auto_deactivated'::character varying)::text, ('guardrail_blocked'::character varying)::text, ('coalesced'::character varying)::text, ('backpressure_skipped'::character varying)::text, ('event_value_filter_not_accepted'::character varying)::text, ('required_refs_missing'::character varying)::text, ('parse_failed'::character varying)::text])))
);


--

-- Name: triggers; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.triggers (
    pipeline_id uuid NOT NULL,
    trigger_type character varying(20) NOT NULL,
    active boolean DEFAULT true NOT NULL,
    max_concurrent_runs integer DEFAULT 1 NOT NULL,
    daily_spend_limit numeric(12,4),
    account_id uuid NOT NULL,
    cron_expression character varying(100),
    cron_timezone character varying(50),
    last_fired_at timestamp with time zone,
    next_fire_at timestamp with time zone,
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    organisation_id uuid NOT NULL,
    created_at timestamp with time zone DEFAULT CURRENT_TIMESTAMP NOT NULL,
    updated_at timestamp with time zone DEFAULT CURRENT_TIMESTAMP NOT NULL,
    deleted_at timestamp with time zone,
    streak_epoch timestamp with time zone DEFAULT CURRENT_TIMESTAMP,
    config_json jsonb NOT NULL,
    run_kind character varying(20) DEFAULT 'run'::character varying NOT NULL,
    eval_suite_id uuid,
    name character varying(255) NOT NULL,
    CONSTRAINT ck_triggers_max_concurrent_runs CHECK ((max_concurrent_runs > 0)),
    CONSTRAINT ck_triggers_ongoing_spend_limit CHECK ((((trigger_type)::text <> 'ongoing'::text) OR ((daily_spend_limit IS NOT NULL) AND (daily_spend_limit > (0)::numeric)))),
    CONSTRAINT ck_triggers_ongoing_target_range CHECK ((((trigger_type)::text <> 'ongoing'::text) OR ((max_concurrent_runs >= 1) AND (max_concurrent_runs <= 20)))),
    CONSTRAINT ck_triggers_run_kind CHECK (((run_kind)::text = ANY ((ARRAY['run'::character varying, 'suite_run'::character varying])::text[]))),
    CONSTRAINT ck_triggers_type CHECK (((trigger_type)::text = ANY (ARRAY[('manual'::character varying)::text, ('webhook'::character varying)::text, ('cron'::character varying)::text, ('polling'::character varying)::text, ('agent_signal'::character varying)::text, ('ongoing'::character varying)::text, ('slack_app_mention'::character varying)::text])))
);


--

-- Name: variant_batch_state; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.variant_batch_state (
    batch_id uuid NOT NULL,
    name text,
    pipeline_id uuid,
    variant_group_id uuid,
    input_payload jsonb DEFAULT '{}'::jsonb,
    organisation_id uuid NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    deleted_at timestamp with time zone,
    updated_at timestamp with time zone DEFAULT now() NOT NULL
);



--

-- Name: variant_groups; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.variant_groups (
    pipeline_id uuid NOT NULL,
    name character varying(255) NOT NULL,
    description character varying(2000),
    selection_strategy character varying(20) DEFAULT 'weighted'::character varying NOT NULL,
    run_count integer DEFAULT 0 NOT NULL,
    max_concurrent_runs integer DEFAULT 5 NOT NULL,
    degraded_evals boolean DEFAULT false NOT NULL,
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    organisation_id uuid NOT NULL,
    created_at timestamp with time zone DEFAULT CURRENT_TIMESTAMP NOT NULL,
    updated_at timestamp with time zone DEFAULT CURRENT_TIMESTAMP NOT NULL,
    deleted_at timestamp with time zone,
    variants jsonb NOT NULL,
    CONSTRAINT ck_variant_groups_max_concurrent_runs CHECK ((max_concurrent_runs > 0)),
    CONSTRAINT ck_variant_groups_run_count CHECK ((run_count >= 0)),
    CONSTRAINT ck_variant_groups_selection_strategy CHECK (((selection_strategy)::text = ANY ((ARRAY['weighted'::character varying, 'single'::character varying])::text[])))
);


--

-- Name: web_vital_events; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.web_vital_events (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    organisation_id uuid NOT NULL,
    metric_name character varying(50) NOT NULL,
    metric_value double precision NOT NULL,
    metric_rating character varying(20),
    route_path character varying(500),
    page_url character varying(2000),
    navigation_type character varying(50),
    created_at timestamp with time zone DEFAULT CURRENT_TIMESTAMP NOT NULL,
    updated_at timestamp with time zone DEFAULT CURRENT_TIMESTAMP NOT NULL,
    recorded_at timestamp with time zone DEFAULT CURRENT_TIMESTAMP NOT NULL,
    CONSTRAINT ck_web_vital_events_metric_value_nonneg CHECK ((metric_value >= (0)::double precision))
);


--

-- Name: webhook_dedup_hashes; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.webhook_dedup_hashes (
    trigger_id uuid NOT NULL,
    payload_hash character varying(64) NOT NULL,
    expires_at timestamp with time zone NOT NULL,
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    organisation_id uuid NOT NULL,
    created_at timestamp with time zone DEFAULT CURRENT_TIMESTAMP NOT NULL,
    updated_at timestamp with time zone DEFAULT CURRENT_TIMESTAMP NOT NULL,
    CONSTRAINT ck_webhook_dedup_expires_ordering CHECK ((expires_at > created_at)),
    CONSTRAINT ck_webhook_dedup_hash_length CHECK ((char_length((payload_hash)::text) = 64))
);


--

-- Name: webhook_payloads; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.webhook_payloads (
    trigger_event_id uuid,
    raw_body bytea NOT NULL,
    expires_at timestamp with time zone NOT NULL,
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    organisation_id uuid NOT NULL,
    created_at timestamp with time zone DEFAULT CURRENT_TIMESTAMP NOT NULL,
    updated_at timestamp with time zone DEFAULT CURRENT_TIMESTAMP NOT NULL,
    raw_payload jsonb NOT NULL,
    CONSTRAINT ck_webhook_payload_expires_ordering CHECK ((expires_at > created_at))
);


--

-- Name: _migration_0191_repoint_state _migration_0191_repoint_state_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public._migration_0191_repoint_state
    ADD CONSTRAINT _migration_0191_repoint_state_pkey PRIMARY KEY (profile_id);


--

-- Name: accounts accounts_email_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.accounts
    ADD CONSTRAINT accounts_email_key UNIQUE (email);


--

-- Name: accounts accounts_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.accounts
    ADD CONSTRAINT accounts_pkey PRIMARY KEY (id);


--

-- Name: agent_runner_bindings agent_runner_bindings_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.agent_runner_bindings
    ADD CONSTRAINT agent_runner_bindings_pkey PRIMARY KEY (id);


--

-- Name: agents agents_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.agents
    ADD CONSTRAINT agents_pkey PRIMARY KEY (id);


--

-- Name: assistant_context_sources assistant_context_sources_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.assistant_context_sources
    ADD CONSTRAINT assistant_context_sources_pkey PRIMARY KEY (id);


--

-- Name: assistant_skills assistant_skills_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.assistant_skills
    ADD CONSTRAINT assistant_skills_pkey PRIMARY KEY (id);


--

-- Name: audit_chain_heads audit_chain_heads_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.audit_chain_heads
    ADD CONSTRAINT audit_chain_heads_pkey PRIMARY KEY (id);


--

-- Name: audit_events audit_events_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.audit_events
    ADD CONSTRAINT audit_events_pkey PRIMARY KEY (id);


--

-- Name: chat_messages chat_messages_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.chat_messages
    ADD CONSTRAINT chat_messages_pkey PRIMARY KEY (id);


--

-- Name: chat_sessions chat_sessions_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.chat_sessions
    ADD CONSTRAINT chat_sessions_pkey PRIMARY KEY (id);


--

-- Name: collection_install collection_install_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.collection_install
    ADD CONSTRAINT collection_install_pkey PRIMARY KEY (install_id);


--

-- Name: composite_templates composite_templates_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.composite_templates
    ADD CONSTRAINT composite_templates_pkey PRIMARY KEY (id);


--

-- Name: connector_instances connector_instances_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.connector_instances
    ADD CONSTRAINT connector_instances_pkey PRIMARY KEY (id);


--

-- Name: connector_profiles connector_profiles_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.connector_profiles
    ADD CONSTRAINT connector_profiles_pkey PRIMARY KEY (id);


--

-- Name: cost_components cost_components_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.cost_components
    ADD CONSTRAINT cost_components_pkey PRIMARY KEY (id);


--

-- Name: deleted_defaults deleted_defaults_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.deleted_defaults
    ADD CONSTRAINT deleted_defaults_pkey PRIMARY KEY (id);


--

-- Name: dismissals dismissals_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.dismissals
    ADD CONSTRAINT dismissals_pkey PRIMARY KEY (id);


--

-- Name: environment_profiles environment_profiles_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.environment_profiles
    ADD CONSTRAINT environment_profiles_pkey PRIMARY KEY (id);


--

-- Name: error_events error_events_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.error_events
    ADD CONSTRAINT error_events_pkey PRIMARY KEY (id);


--

-- Name: error_forwarder_configs error_forwarder_configs_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.error_forwarder_configs
    ADD CONSTRAINT error_forwarder_configs_pkey PRIMARY KEY (id);


--

-- Name: error_groups error_groups_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.error_groups
    ADD CONSTRAINT error_groups_pkey PRIMARY KEY (id);


--

-- Name: error_notification_rules error_notification_rules_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.error_notification_rules
    ADD CONSTRAINT error_notification_rules_pkey PRIMARY KEY (id);


--

-- Name: eval_backfill_violations eval_backfill_violations_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.eval_backfill_violations
    ADD CONSTRAINT eval_backfill_violations_pkey PRIMARY KEY (id);


--

-- Name: eval_cases eval_cases_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.eval_cases
    ADD CONSTRAINT eval_cases_pkey PRIMARY KEY (id);


--

-- Name: eval_datasets eval_datasets_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.eval_datasets
    ADD CONSTRAINT eval_datasets_pkey PRIMARY KEY (id);


--

-- Name: eval_results eval_results_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.eval_results
    ADD CONSTRAINT eval_results_pkey PRIMARY KEY (id);


--

-- Name: eval_suites eval_suites_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.eval_suites
    ADD CONSTRAINT eval_suites_pkey PRIMARY KEY (id);


--

-- Name: evals evals_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.evals
    ADD CONSTRAINT evals_pkey PRIMARY KEY (id);


--

-- Name: evidence evidence_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.evidence
    ADD CONSTRAINT evidence_pkey PRIMARY KEY (id);


--

-- Name: evidence_redaction_rules evidence_redaction_rules_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.evidence_redaction_rules
    ADD CONSTRAINT evidence_redaction_rules_pkey PRIMARY KEY (id);


--

-- Name: external_agents external_agents_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.external_agents
    ADD CONSTRAINT external_agents_pkey PRIMARY KEY (id);


--

-- Name: feature_flag_catalog feature_flag_catalog_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.feature_flag_catalog
    ADD CONSTRAINT feature_flag_catalog_pkey PRIMARY KEY (name);


--

-- Name: feedback_records feedback_records_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.feedback_records
    ADD CONSTRAINT feedback_records_pkey PRIMARY KEY (id);


--

-- Name: hitl_claims hitl_claims_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.hitl_claims
    ADD CONSTRAINT hitl_claims_pkey PRIMARY KEY (id);


--

-- Name: invitations invitations_token_hash_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.invitations
    ADD CONSTRAINT invitations_token_hash_key UNIQUE (token_hash);


--

-- Name: journeys journeys_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.journeys
    ADD CONSTRAINT journeys_pkey PRIMARY KEY (id);


--

-- Name: library_primitives library_primitives_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.library_primitives
    ADD CONSTRAINT library_primitives_pkey PRIMARY KEY (id);


--

-- Name: library_sync_state library_sync_state_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.library_sync_state
    ADD CONSTRAINT library_sync_state_pkey PRIMARY KEY (id);


--

-- Name: lifecycle_map_stages lifecycle_map_stages_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.lifecycle_map_stages
    ADD CONSTRAINT lifecycle_map_stages_pkey PRIMARY KEY (id);


--

-- Name: lifecycle_maps lifecycle_maps_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.lifecycle_maps
    ADD CONSTRAINT lifecycle_maps_pkey PRIMARY KEY (id);


--

-- Name: mcp_servers mcp_servers_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.mcp_servers
    ADD CONSTRAINT mcp_servers_pkey PRIMARY KEY (id);


--

-- Name: mcp_setup_tokens mcp_setup_tokens_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.mcp_setup_tokens
    ADD CONSTRAINT mcp_setup_tokens_pkey PRIMARY KEY (id);


--

-- Name: mcp_setup_tokens mcp_setup_tokens_token_hash_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.mcp_setup_tokens
    ADD CONSTRAINT mcp_setup_tokens_token_hash_key UNIQUE (token_hash);


--

-- Name: metrics_staging metrics_staging_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.metrics_staging
    ADD CONSTRAINT metrics_staging_pkey PRIMARY KEY (id);


--

-- Name: model_backends model_backends_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.model_backends
    ADD CONSTRAINT model_backends_pkey PRIMARY KEY (id);


--

-- Name: modulo_journey_facts modulo_journey_facts_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.modulo_journey_facts
    ADD CONSTRAINT modulo_journey_facts_pkey PRIMARY KEY (id);


--

-- Name: node_categories node_categories_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.node_categories
    ADD CONSTRAINT node_categories_pkey PRIMARY KEY (id);


--

-- Name: node_observations node_observations_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.node_observations
    ADD CONSTRAINT node_observations_pkey PRIMARY KEY (id);


--

-- Name: nodes nodes_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.nodes
    ADD CONSTRAINT nodes_pkey PRIMARY KEY (id);


--

-- Name: notification_delivery_log notification_delivery_log_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.notification_delivery_log
    ADD CONSTRAINT notification_delivery_log_pkey PRIMARY KEY (id);


--

-- Name: notification_endpoints notification_endpoints_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.notification_endpoints
    ADD CONSTRAINT notification_endpoints_pkey PRIMARY KEY (id);


--

-- Name: notification_preferences notification_preferences_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.notification_preferences
    ADD CONSTRAINT notification_preferences_pkey PRIMARY KEY (id);


--

-- Name: notifications notifications_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.notifications
    ADD CONSTRAINT notifications_pkey PRIMARY KEY (id);


--

-- Name: oauth_authorization_codes oauth_authorization_codes_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.oauth_authorization_codes
    ADD CONSTRAINT oauth_authorization_codes_pkey PRIMARY KEY (code);


--

-- Name: oauth_clients oauth_clients_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.oauth_clients
    ADD CONSTRAINT oauth_clients_pkey PRIMARY KEY (id);


--

-- Name: oauth_consent_states oauth_consent_states_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.oauth_consent_states
    ADD CONSTRAINT oauth_consent_states_pkey PRIMARY KEY (state);


--

-- Name: oauth_token_families oauth_token_families_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.oauth_token_families
    ADD CONSTRAINT oauth_token_families_pkey PRIMARY KEY (family_id);


--

-- Name: onboarding_progress onboarding_progress_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.onboarding_progress
    ADD CONSTRAINT onboarding_progress_pkey PRIMARY KEY (id);


--

-- Name: org_api_keys org_api_keys_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.org_api_keys
    ADD CONSTRAINT org_api_keys_pkey PRIMARY KEY (id);


--

-- Name: org_daily_run_counts org_daily_run_counts_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.org_daily_run_counts
    ADD CONSTRAINT org_daily_run_counts_pkey PRIMARY KEY (id);


--

-- Name: org_memberships org_memberships_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.org_memberships
    ADD CONSTRAINT org_memberships_pkey PRIMARY KEY (id);


--

-- Name: organisation_cost_state organisation_cost_state_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.organisation_cost_state
    ADD CONSTRAINT organisation_cost_state_pkey PRIMARY KEY (org_id);


--

-- Name: organisations organisations_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.organisations
    ADD CONSTRAINT organisations_pkey PRIMARY KEY (id);


--

-- Name: parameter_schemas parameter_schemas_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.parameter_schemas
    ADD CONSTRAINT parameter_schemas_pkey PRIMARY KEY (id);


--

-- Name: parameter_sets parameter_sets_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.parameter_sets
    ADD CONSTRAINT parameter_sets_pkey PRIMARY KEY (id);


--

-- Name: pipeline_edges pipeline_edges_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.pipeline_edges
    ADD CONSTRAINT pipeline_edges_pkey PRIMARY KEY (id);


--

-- Name: pipeline_folders pipeline_folders_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.pipeline_folders
    ADD CONSTRAINT pipeline_folders_pkey PRIMARY KEY (id);


--

-- Name: pipeline_snapshots pipeline_snapshots_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.pipeline_snapshots
    ADD CONSTRAINT pipeline_snapshots_pkey PRIMARY KEY (id);


--

-- Name: pipelines pipelines_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.pipelines
    ADD CONSTRAINT pipelines_pkey PRIMARY KEY (id);


--

-- Name: collection_install_entity pk_collection_install_entity; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.collection_install_entity
    ADD CONSTRAINT pk_collection_install_entity PRIMARY KEY (install_id, entity_type, entity_id);


--

-- Name: invitations pk_invitations; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.invitations
    ADD CONSTRAINT pk_invitations PRIMARY KEY (id);


--

-- Name: org_mint_budget_usage pk_org_mint_budget_usage; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.org_mint_budget_usage
    ADD CONSTRAINT pk_org_mint_budget_usage PRIMARY KEY (organisation_id, window_start);


--

-- Name: run_evidence pk_run_evidence_run_node; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.run_evidence
    ADD CONSTRAINT pk_run_evidence_run_node PRIMARY KEY (run_id, node_id);


--

-- Name: run_node_outputs_quarantine pk_run_node_outputs_quarantine_run; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.run_node_outputs_quarantine
    ADD CONSTRAINT pk_run_node_outputs_quarantine_run PRIMARY KEY (run_id);


--

-- Name: run_node_outputs pk_run_node_outputs_run_node_attempt; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.run_node_outputs
    ADD CONSTRAINT pk_run_node_outputs_run_node_attempt PRIMARY KEY (run_id, node_id, attempt_key);


--

-- Name: policy_gate_decisions policy_gate_decisions_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.policy_gate_decisions
    ADD CONSTRAINT policy_gate_decisions_pkey PRIMARY KEY (id);


--

-- Name: policy_gates policy_gates_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.policy_gates
    ADD CONSTRAINT policy_gates_pkey PRIMARY KEY (id);


--

-- Name: primitive_abuse_reports primitive_abuse_reports_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.primitive_abuse_reports
    ADD CONSTRAINT primitive_abuse_reports_pkey PRIMARY KEY (id);


--

-- Name: primitive_ratings primitive_ratings_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.primitive_ratings
    ADD CONSTRAINT primitive_ratings_pkey PRIMARY KEY (id);


--

-- Name: publishers publishers_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.publishers
    ADD CONSTRAINT publishers_pkey PRIMARY KEY (id);


--

-- Name: run_attempts run_attempts_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.run_attempts
    ADD CONSTRAINT run_attempts_pkey PRIMARY KEY (id);


--

-- Name: run_daily_facts run_daily_facts_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.run_daily_facts
    ADD CONSTRAINT run_daily_facts_pkey PRIMARY KEY (id);


--

-- Name: run_number_counters run_number_counters_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.run_number_counters
    ADD CONSTRAINT run_number_counters_pkey PRIMARY KEY (organisation_id);


--

-- Name: runner_probe_cache runner_probe_cache_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.runner_probe_cache
    ADD CONSTRAINT runner_probe_cache_pkey PRIMARY KEY (id);


--

-- Name: runs runs_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.runs
    ADD CONSTRAINT runs_pkey PRIMARY KEY (id);


--

-- Name: saved_views saved_views_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.saved_views
    ADD CONSTRAINT saved_views_pkey PRIMARY KEY (id);


--

-- Name: scheduled_reports scheduled_reports_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.scheduled_reports
    ADD CONSTRAINT scheduled_reports_pkey PRIMARY KEY (id);


--

-- Name: schema_folders schema_folders_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.schema_folders
    ADD CONSTRAINT schema_folders_pkey PRIMARY KEY (id);


--

-- Name: schema_versions schema_versions_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.schema_versions
    ADD CONSTRAINT schema_versions_pkey PRIMARY KEY (id);


--

-- Name: schemas schemas_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.schemas
    ADD CONSTRAINT schemas_pkey PRIMARY KEY (id);


--

-- Name: secrets secrets_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.secrets
    ADD CONSTRAINT secrets_pkey PRIMARY KEY (id);


--

-- Name: snapshot_schema_pins snapshot_schema_pins_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.snapshot_schema_pins
    ADD CONSTRAINT snapshot_schema_pins_pkey PRIMARY KEY (id);


--

-- Name: spend_anomalies spend_anomalies_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.spend_anomalies
    ADD CONSTRAINT spend_anomalies_pkey PRIMARY KEY (id);


--

-- Name: sso_providers sso_providers_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.sso_providers
    ADD CONSTRAINT sso_providers_pkey PRIMARY KEY (id);


--

-- Name: suite_runs suite_runs_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.suite_runs
    ADD CONSTRAINT suite_runs_pkey PRIMARY KEY (id);


--

-- Name: system_audit_events system_audit_events_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.system_audit_events
    ADD CONSTRAINT system_audit_events_pkey PRIMARY KEY (id);


--

-- Name: system_config system_config_key_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.system_config
    ADD CONSTRAINT system_config_key_key UNIQUE (key);


--

-- Name: system_config system_config_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.system_config
    ADD CONSTRAINT system_config_pkey PRIMARY KEY (id);


--

-- Name: system_map_attribution_bindings system_map_attribution_bindings_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.system_map_attribution_bindings
    ADD CONSTRAINT system_map_attribution_bindings_pkey PRIMARY KEY (id);


--

-- Name: system_map_loop_admission_attempts system_map_loop_admission_attempts_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.system_map_loop_admission_attempts
    ADD CONSTRAINT system_map_loop_admission_attempts_pkey PRIMARY KEY (id);


--

-- Name: system_map_loop_configs system_map_loop_configs_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.system_map_loop_configs
    ADD CONSTRAINT system_map_loop_configs_pkey PRIMARY KEY (id);


--

-- Name: system_map_loop_reservations system_map_loop_reservations_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.system_map_loop_reservations
    ADD CONSTRAINT system_map_loop_reservations_pkey PRIMARY KEY (id);


--

-- Name: system_map_nodes system_map_nodes_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.system_map_nodes
    ADD CONSTRAINT system_map_nodes_pkey PRIMARY KEY (id);


--

-- Name: system_map_relationships system_map_relationships_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.system_map_relationships
    ADD CONSTRAINT system_map_relationships_pkey PRIMARY KEY (id);


--

-- Name: system_map_revisions system_map_revisions_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.system_map_revisions
    ADD CONSTRAINT system_map_revisions_pkey PRIMARY KEY (id);


--

-- Name: system_maps system_maps_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.system_maps
    ADD CONSTRAINT system_maps_pkey PRIMARY KEY (id);


--

-- Name: team_memberships team_memberships_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.team_memberships
    ADD CONSTRAINT team_memberships_pkey PRIMARY KEY (id);


--

-- Name: teams teams_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.teams
    ADD CONSTRAINT teams_pkey PRIMARY KEY (id);


--

-- Name: tier_catalog tier_catalog_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.tier_catalog
    ADD CONSTRAINT tier_catalog_pkey PRIMARY KEY (tier_id);


--

-- Name: token_families token_families_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.token_families
    ADD CONSTRAINT token_families_pkey PRIMARY KEY (family_id);


--

-- Name: trigger_event_findings trigger_event_findings_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.trigger_event_findings
    ADD CONSTRAINT trigger_event_findings_pkey PRIMARY KEY (id);


--

-- Name: trigger_events trigger_events_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.trigger_events
    ADD CONSTRAINT trigger_events_pkey PRIMARY KEY (id);


--

-- Name: triggers triggers_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.triggers
    ADD CONSTRAINT triggers_pkey PRIMARY KEY (id);


--

-- Name: agent_runner_bindings uq_agent_runner_bindings_org_agent_target; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.agent_runner_bindings
    ADD CONSTRAINT uq_agent_runner_bindings_org_agent_target UNIQUE (organisation_id, agent_id, target_env_var);


--

-- Name: assistant_context_sources uq_assistant_context_sources_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.assistant_context_sources
    ADD CONSTRAINT uq_assistant_context_sources_key UNIQUE (organisation_id, account_id, source_key);


--

-- Name: chat_sessions uq_chat_sessions_user_session_number; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.chat_sessions
    ADD CONSTRAINT uq_chat_sessions_user_session_number UNIQUE (account_id, session_number);


--

-- Name: connector_profiles uq_connector_profiles_connector_instance; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.connector_profiles
    ADD CONSTRAINT uq_connector_profiles_connector_instance UNIQUE (connector_instance_id);


--

-- Name: deleted_defaults uq_deleted_defaults_org_signal; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.deleted_defaults
    ADD CONSTRAINT uq_deleted_defaults_org_signal UNIQUE (organisation_id, signal);


--

-- Name: dismissals uq_dismissal_user_notification; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.dismissals
    ADD CONSTRAINT uq_dismissal_user_notification UNIQUE (notification_id, dismissed_by_user_id);


--

-- Name: error_groups uq_error_groups_org_fingerprint; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.error_groups
    ADD CONSTRAINT uq_error_groups_org_fingerprint UNIQUE (organisation_id, fingerprint);


--

-- Name: eval_backfill_violations uq_eval_backfill_violations_rev_eval; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.eval_backfill_violations
    ADD CONSTRAINT uq_eval_backfill_violations_rev_eval UNIQUE (migration_revision, eval_definition_id);


--

-- Name: eval_suites uq_eval_suites_org_legacy_suite_id; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.eval_suites
    ADD CONSTRAINT uq_eval_suites_org_legacy_suite_id UNIQUE (organisation_id, legacy_suite_id);


--

-- Name: evals uq_evals_id_organisation_id; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.evals
    ADD CONSTRAINT uq_evals_id_organisation_id UNIQUE (id, organisation_id);


--

-- Name: evidence uq_evidence_distinct_on; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.evidence
    ADD CONSTRAINT uq_evidence_distinct_on UNIQUE (organisation_id, subject_type, subject_id, key, created_at, id);


--

-- Name: evidence_redaction_rules uq_evidence_redaction_rules_org_key_role; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.evidence_redaction_rules
    ADD CONSTRAINT uq_evidence_redaction_rules_org_key_role UNIQUE (organisation_id, key_pattern, role);


--

-- Name: hitl_claims uq_hitl_claims_run_review; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.hitl_claims
    ADD CONSTRAINT uq_hitl_claims_run_review UNIQUE (run_id, review_id);


--

-- Name: journeys uq_journeys_org_kind_ref; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.journeys
    ADD CONSTRAINT uq_journeys_org_kind_ref UNIQUE (organisation_id, kind, ref);


--

-- Name: lifecycle_map_stages uq_lifecycle_map_stages_map_version_stage; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.lifecycle_map_stages
    ADD CONSTRAINT uq_lifecycle_map_stages_map_version_stage UNIQUE (map_id, version, stage_id);


--

-- Name: system_map_loop_admission_attempts uq_loop_admission_attempts_config_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.system_map_loop_admission_attempts
    ADD CONSTRAINT uq_loop_admission_attempts_config_key UNIQUE (loop_config_id, idempotency_key);


--

-- Name: system_map_loop_reservations uq_loop_reservations_config_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.system_map_loop_reservations
    ADD CONSTRAINT uq_loop_reservations_config_key UNIQUE (loop_config_id, idempotency_key);


--

-- Name: metrics_staging uq_metrics_staging_org_event_id; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.metrics_staging
    ADD CONSTRAINT uq_metrics_staging_org_event_id UNIQUE (organisation_id, event_id);


--

-- Name: modulo_journey_facts uq_modulo_journey_facts_run_writer; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.modulo_journey_facts
    ADD CONSTRAINT uq_modulo_journey_facts_run_writer UNIQUE (run_id, writer);


--

-- Name: node_observations uq_node_observations_run_node; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.node_observations
    ADD CONSTRAINT uq_node_observations_run_node UNIQUE (run_id, node_id);


--

-- Name: notification_preferences uq_notification_preferences_org_account_category; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.notification_preferences
    ADD CONSTRAINT uq_notification_preferences_org_account_category UNIQUE (organisation_id, account_id, category);


--

-- Name: onboarding_progress uq_onboarding_progress_org; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.onboarding_progress
    ADD CONSTRAINT uq_onboarding_progress_org UNIQUE (organisation_id);


--

-- Name: org_api_keys uq_org_api_keys_lookup_prefix; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.org_api_keys
    ADD CONSTRAINT uq_org_api_keys_lookup_prefix UNIQUE (lookup_prefix);


--

-- Name: org_memberships uq_org_memberships_account_org; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.org_memberships
    ADD CONSTRAINT uq_org_memberships_account_org UNIQUE (account_id, organisation_id);


--

-- Name: pipeline_edges uq_pipeline_edges_path; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.pipeline_edges
    ADD CONSTRAINT uq_pipeline_edges_path UNIQUE (pipeline_id, source_node_id, target_node_id, edge_type);


--

-- Name: pipeline_snapshots uq_pipeline_snapshot_version; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.pipeline_snapshots
    ADD CONSTRAINT uq_pipeline_snapshot_version UNIQUE (pipeline_id, snapshot_version);


--

-- Name: policy_gate_decisions uq_policy_gate_decisions_id_organisation_id; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.policy_gate_decisions
    ADD CONSTRAINT uq_policy_gate_decisions_id_organisation_id UNIQUE (id, organisation_id);


--

-- Name: policy_gates uq_policy_gates_id_organisation_id; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.policy_gates
    ADD CONSTRAINT uq_policy_gates_id_organisation_id UNIQUE (id, organisation_id);


--

-- Name: publishers uq_publishers_org_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.publishers
    ADD CONSTRAINT uq_publishers_org_key UNIQUE (organisation_id, public_key_hex);


--

-- Name: publishers uq_publishers_org_name; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.publishers
    ADD CONSTRAINT uq_publishers_org_name UNIQUE (organisation_id, name);


--

-- Name: primitive_ratings uq_ratings_per_user; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.primitive_ratings
    ADD CONSTRAINT uq_ratings_per_user UNIQUE (organisation_id, primitive_id, account_id);


--

-- Name: run_attempts uq_run_attempts_run_attempt_index; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.run_attempts
    ADD CONSTRAINT uq_run_attempts_run_attempt_index UNIQUE (run_id, attempt_index);


--

-- Name: runner_probe_cache uq_runner_probe_cache_org_machine; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.runner_probe_cache
    ADD CONSTRAINT uq_runner_probe_cache_org_machine UNIQUE (organisation_id, machine_id);


--

-- Name: runs uq_runs_org_run_number; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.runs
    ADD CONSTRAINT uq_runs_org_run_number UNIQUE (organisation_id, run_number);


--

-- Name: schema_versions uq_schema_versions_schema_version_organisation; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.schema_versions
    ADD CONSTRAINT uq_schema_versions_schema_version_organisation UNIQUE (schema_id, version, organisation_id);


--

-- Name: schemas uq_schemas_organisation_name; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.schemas
    ADD CONSTRAINT uq_schemas_organisation_name UNIQUE (organisation_id, name);


--

-- Name: secrets uq_secrets_org_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.secrets
    ADD CONSTRAINT uq_secrets_org_key UNIQUE (organisation_id, key);


--

-- Name: system_map_attribution_bindings uq_system_map_attribution_bindings_id_organisation_id; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.system_map_attribution_bindings
    ADD CONSTRAINT uq_system_map_attribution_bindings_id_organisation_id UNIQUE (id, organisation_id);


--

-- Name: system_map_loop_admission_attempts uq_system_map_loop_admission_attempts_id_organisation_id; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.system_map_loop_admission_attempts
    ADD CONSTRAINT uq_system_map_loop_admission_attempts_id_organisation_id UNIQUE (id, organisation_id);


--

-- Name: system_map_loop_configs uq_system_map_loop_configs_id_organisation_id; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.system_map_loop_configs
    ADD CONSTRAINT uq_system_map_loop_configs_id_organisation_id UNIQUE (id, organisation_id);


--

-- Name: system_map_loop_configs uq_system_map_loop_configs_revision_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.system_map_loop_configs
    ADD CONSTRAINT uq_system_map_loop_configs_revision_key UNIQUE (revision_id, node_key);


--

-- Name: system_map_loop_reservations uq_system_map_loop_reservations_id_organisation_id; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.system_map_loop_reservations
    ADD CONSTRAINT uq_system_map_loop_reservations_id_organisation_id UNIQUE (id, organisation_id);


--

-- Name: system_map_nodes uq_system_map_nodes_id_organisation_id; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.system_map_nodes
    ADD CONSTRAINT uq_system_map_nodes_id_organisation_id UNIQUE (id, organisation_id);


--

-- Name: system_map_nodes uq_system_map_nodes_revision_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.system_map_nodes
    ADD CONSTRAINT uq_system_map_nodes_revision_key UNIQUE (revision_id, node_key);


--

-- Name: system_map_relationships uq_system_map_relationships_id_organisation_id; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.system_map_relationships
    ADD CONSTRAINT uq_system_map_relationships_id_organisation_id UNIQUE (id, organisation_id);


--

-- Name: system_map_relationships uq_system_map_relationships_rev_src_tgt_type; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.system_map_relationships
    ADD CONSTRAINT uq_system_map_relationships_rev_src_tgt_type UNIQUE (revision_id, source_node_key, target_node_key, relationship_type);


--

-- Name: system_map_revisions uq_system_map_revisions_id_organisation_id; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.system_map_revisions
    ADD CONSTRAINT uq_system_map_revisions_id_organisation_id UNIQUE (id, organisation_id);


--

-- Name: system_map_revisions uq_system_map_revisions_map_number; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.system_map_revisions
    ADD CONSTRAINT uq_system_map_revisions_map_number UNIQUE (system_map_id, revision_number);


--

-- Name: system_maps uq_system_maps_id_organisation_id; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.system_maps
    ADD CONSTRAINT uq_system_maps_id_organisation_id UNIQUE (id, organisation_id);


--

-- Name: team_memberships uq_team_memberships_team_account; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.team_memberships
    ADD CONSTRAINT uq_team_memberships_team_account UNIQUE (team_id, account_id);


--

-- Name: trigger_event_findings uq_trigger_event_findings_id_organisation_id; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.trigger_event_findings
    ADD CONSTRAINT uq_trigger_event_findings_id_organisation_id UNIQUE (id, organisation_id);


--

-- Name: trigger_event_findings uq_trigger_event_findings_org_event_eval; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.trigger_event_findings
    ADD CONSTRAINT uq_trigger_event_findings_org_event_eval UNIQUE (organisation_id, trigger_event_id, eval_id);


--

-- Name: trigger_events uq_trigger_events_id_organisation_id; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.trigger_events
    ADD CONSTRAINT uq_trigger_events_id_organisation_id UNIQUE (id, organisation_id);


--

-- Name: webhook_dedup_hashes uq_webhook_dedup_trigger_hash; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.webhook_dedup_hashes
    ADD CONSTRAINT uq_webhook_dedup_trigger_hash UNIQUE (trigger_id, payload_hash);


--

-- Name: variant_batch_state variant_batch_state_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.variant_batch_state
    ADD CONSTRAINT variant_batch_state_pkey PRIMARY KEY (batch_id);


--

-- Name: variant_groups variant_groups_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.variant_groups
    ADD CONSTRAINT variant_groups_pkey PRIMARY KEY (id);


--

-- Name: web_vital_events web_vital_events_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.web_vital_events
    ADD CONSTRAINT web_vital_events_pkey PRIMARY KEY (id);


--

-- Name: webhook_dedup_hashes webhook_dedup_hashes_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.webhook_dedup_hashes
    ADD CONSTRAINT webhook_dedup_hashes_pkey PRIMARY KEY (id);


--

-- Name: webhook_payloads webhook_payloads_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.webhook_payloads
    ADD CONSTRAINT webhook_payloads_pkey PRIMARY KEY (id);


--

-- Name: idx_ssp_schema; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_ssp_schema ON public.snapshot_schema_pins USING btree (schema_id, schema_version);


--

-- Name: idx_ssp_snapshot; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_ssp_snapshot ON public.snapshot_schema_pins USING btree (snapshot_id);


--

-- Name: ix_agent_runner_bindings_account_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_agent_runner_bindings_account_id ON public.agent_runner_bindings USING btree (account_id);


--

-- Name: ix_agent_runner_bindings_agent_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_agent_runner_bindings_agent_id ON public.agent_runner_bindings USING btree (agent_id);


--

-- Name: ix_agent_runner_bindings_model_backend_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_agent_runner_bindings_model_backend_id ON public.agent_runner_bindings USING btree (model_backend_id);


--

-- Name: ix_agent_runner_bindings_organisation_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_agent_runner_bindings_organisation_id ON public.agent_runner_bindings USING btree (organisation_id);


--

-- Name: ix_agents_account_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_agents_account_id ON public.agents USING btree (account_id);


--

-- Name: ix_agents_collection_install_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_agents_collection_install_id ON public.agents USING btree (collection_install_id);


--

-- Name: ix_agents_input_schema_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_agents_input_schema_id ON public.agents USING btree (input_schema_id);


--

-- Name: ix_agents_library_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_agents_library_id ON public.agents USING btree (library_id);


--

-- Name: ix_agents_model_backend_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_agents_model_backend_id ON public.agents USING btree (model_backend_id);


--

-- Name: ix_agents_organisation_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_agents_organisation_id ON public.agents USING btree (organisation_id);


--

-- Name: ix_agents_organisation_id_created_at; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_agents_organisation_id_created_at ON public.agents USING btree (organisation_id, created_at DESC);


--

-- Name: ix_agents_organisation_id_deleted_at; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_agents_organisation_id_deleted_at ON public.agents USING btree (organisation_id, deleted_at) WHERE (deleted_at IS NULL);


--

-- Name: ix_agents_output_schema_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_agents_output_schema_id ON public.agents USING btree (output_schema_id);


--

-- Name: ix_agents_parameter_schema_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_agents_parameter_schema_id ON public.agents USING btree (parameter_schema_id);


--

-- Name: ix_assistant_context_sources_organisation_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_assistant_context_sources_organisation_id ON public.assistant_context_sources USING btree (organisation_id);


--

-- Name: ix_assistant_skills_account_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_assistant_skills_account_id ON public.assistant_skills USING btree (account_id);


--

-- Name: ix_assistant_skills_organisation_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_assistant_skills_organisation_id ON public.assistant_skills USING btree (organisation_id);


--

-- Name: ix_audit_chain_heads_last_event_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_audit_chain_heads_last_event_id ON public.audit_chain_heads USING btree (last_event_id);


--

-- Name: ix_audit_chain_heads_organisation_id; Type: INDEX; Schema: public; Owner: -
--

CREATE UNIQUE INDEX ix_audit_chain_heads_organisation_id ON public.audit_chain_heads USING btree (organisation_id);


--

-- Name: ix_audit_events_account_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_audit_events_account_id ON public.audit_events USING btree (account_id);


--

-- Name: ix_audit_events_event_type; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_audit_events_event_type ON public.audit_events USING btree (event_type);


--

-- Name: ix_audit_events_org_created_at; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_audit_events_org_created_at ON public.audit_events USING btree (organisation_id, created_at);


--

-- Name: ix_audit_events_org_resource; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_audit_events_org_resource ON public.audit_events USING btree (organisation_id, resource_type, resource_id);


--

-- Name: ix_audit_events_org_type_actor_time; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_audit_events_org_type_actor_time ON public.audit_events USING btree (organisation_id, event_type, account_id, created_at);


--

-- Name: ix_audit_events_organisation_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_audit_events_organisation_id ON public.audit_events USING btree (organisation_id);


--

-- Name: ix_audit_events_resource; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_audit_events_resource ON public.audit_events USING btree (resource_type, resource_id);


--

-- Name: ix_chat_messages_organisation_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_chat_messages_organisation_id ON public.chat_messages USING btree (organisation_id);


--

-- Name: ix_chat_messages_parent_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_chat_messages_parent_id ON public.chat_messages USING btree (parent_id);


--

-- Name: ix_chat_messages_session_created; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_chat_messages_session_created ON public.chat_messages USING btree (session_id, created_at);


--

-- Name: ix_chat_messages_session_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_chat_messages_session_id ON public.chat_messages USING btree (session_id);


--

-- Name: ix_chat_sessions_account_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_chat_sessions_account_id ON public.chat_sessions USING btree (account_id);


--

-- Name: ix_chat_sessions_organisation_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_chat_sessions_organisation_id ON public.chat_sessions USING btree (organisation_id);


--

-- Name: ix_collection_install_collection_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_collection_install_collection_id ON public.collection_install USING btree (collection_id);


--

-- Name: ix_collection_install_created_by; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_collection_install_created_by ON public.collection_install USING btree (created_by);


--

-- Name: ix_collection_install_entity_entity; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_collection_install_entity_entity ON public.collection_install_entity USING btree (entity_type, entity_id);


--

-- Name: ix_collection_install_organisation_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_collection_install_organisation_id ON public.collection_install USING btree (organisation_id);


--

-- Name: ix_composite_templates_account_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_composite_templates_account_id ON public.composite_templates USING btree (account_id);


--

-- Name: ix_composite_templates_input_schema_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_composite_templates_input_schema_id ON public.composite_templates USING btree (input_schema_id);


--

-- Name: ix_composite_templates_organisation_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_composite_templates_organisation_id ON public.composite_templates USING btree (organisation_id);


--

-- Name: ix_composite_templates_organisation_id_deleted_at; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_composite_templates_organisation_id_deleted_at ON public.composite_templates USING btree (organisation_id, deleted_at) WHERE (deleted_at IS NULL);


--

-- Name: ix_composite_templates_output_schema_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_composite_templates_output_schema_id ON public.composite_templates USING btree (output_schema_id);


--

-- Name: ix_composite_templates_parameter_schema_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_composite_templates_parameter_schema_id ON public.composite_templates USING btree (parameter_schema_id);


--

-- Name: ix_connector_instances_account_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_connector_instances_account_id ON public.connector_instances USING btree (account_id);


--

-- Name: ix_connector_instances_connector_type_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_connector_instances_connector_type_id ON public.connector_instances USING btree (connector_type_id);


--

-- Name: ix_connector_instances_org_account; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_connector_instances_org_account ON public.connector_instances USING btree (organisation_id, account_id);


--

-- Name: ix_connector_instances_org_type; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_connector_instances_org_type ON public.connector_instances USING btree (organisation_id, connector_type_id);


--

-- Name: ix_connector_instances_organisation_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_connector_instances_organisation_id ON public.connector_instances USING btree (organisation_id);


--

-- Name: ix_connector_instances_organisation_id_deleted_at; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_connector_instances_organisation_id_deleted_at ON public.connector_instances USING btree (organisation_id, deleted_at) WHERE (deleted_at IS NULL);


--

-- Name: ix_connector_instances_owner_team_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_connector_instances_owner_team_id ON public.connector_instances USING btree (owner_team_id);


--

-- Name: ix_connector_profiles_organisation_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_connector_profiles_organisation_id ON public.connector_profiles USING btree (organisation_id);


--

-- Name: ix_cost_components_org_enabled_sort; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_cost_components_org_enabled_sort ON public.cost_components USING btree (organisation_id, enabled, sort_order);


--

-- Name: ix_cost_components_organisation_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_cost_components_organisation_id ON public.cost_components USING btree (organisation_id);


--

-- Name: ix_cost_components_organisation_id_deleted_at; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_cost_components_organisation_id_deleted_at ON public.cost_components USING btree (organisation_id, deleted_at) WHERE (deleted_at IS NULL);


--

-- Name: ix_deleted_defaults_organisation_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_deleted_defaults_organisation_id ON public.deleted_defaults USING btree (organisation_id);


--

-- Name: ix_dismissals_dismissed_by_user_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_dismissals_dismissed_by_user_id ON public.dismissals USING btree (dismissed_by_user_id);


--

-- Name: ix_dismissals_notification_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_dismissals_notification_id ON public.dismissals USING btree (notification_id);


--

-- Name: ix_dismissals_org_user_scope; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_dismissals_org_user_scope ON public.dismissals USING btree (organisation_id, dismissed_by_user_id, dismiss_scope);


--

-- Name: ix_dismissals_organisation_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_dismissals_organisation_id ON public.dismissals USING btree (organisation_id);


--

-- Name: ix_dismissals_user_notification; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_dismissals_user_notification ON public.dismissals USING btree (dismissed_by_user_id, notification_id);


--

-- Name: ix_dismissals_user_scope; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_dismissals_user_scope ON public.dismissals USING btree (dismissed_by_user_id, dismiss_scope);


--

-- Name: ix_environment_profiles_account_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_environment_profiles_account_id ON public.environment_profiles USING btree (account_id);


--

-- Name: ix_environment_profiles_organisation_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_environment_profiles_organisation_id ON public.environment_profiles USING btree (organisation_id);


--

-- Name: ix_environment_profiles_organisation_id_deleted_at; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_environment_profiles_organisation_id_deleted_at ON public.environment_profiles USING btree (organisation_id, deleted_at) WHERE (deleted_at IS NULL);


--

-- Name: ix_environment_profiles_owner_team_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_environment_profiles_owner_team_id ON public.environment_profiles USING btree (owner_team_id);


--

-- Name: ix_error_events_org_created_at; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_error_events_org_created_at ON public.error_events USING btree (organisation_id, created_at);


--

-- Name: ix_error_events_org_fingerprint_created_at; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_error_events_org_fingerprint_created_at ON public.error_events USING btree (organisation_id, fingerprint, created_at);


--

-- Name: ix_error_events_org_level; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_error_events_org_level ON public.error_events USING btree (organisation_id, level, created_at);


--

-- Name: ix_error_events_org_status; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_error_events_org_status ON public.error_events USING btree (organisation_id, status);


--

-- Name: ix_error_events_organisation_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_error_events_organisation_id ON public.error_events USING btree (organisation_id);


--

-- Name: ix_error_events_status_new; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_error_events_status_new ON public.error_events USING btree (organisation_id, created_at) WHERE ((status)::text = 'new'::text);


--

-- Name: ix_error_forwarder_configs_organisation_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_error_forwarder_configs_organisation_id ON public.error_forwarder_configs USING btree (organisation_id);


--

-- Name: ix_error_forwarder_configs_organisation_id_deleted_at; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_error_forwarder_configs_organisation_id_deleted_at ON public.error_forwarder_configs USING btree (organisation_id, deleted_at) WHERE (deleted_at IS NULL);


--

-- Name: ix_error_groups_assigned_to; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_error_groups_assigned_to ON public.error_groups USING btree (assigned_to);


--

-- Name: ix_error_groups_org_status_last_seen; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_error_groups_org_status_last_seen ON public.error_groups USING btree (organisation_id, status, last_seen);


--

-- Name: ix_error_groups_organisation_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_error_groups_organisation_id ON public.error_groups USING btree (organisation_id);


--

-- Name: ix_error_groups_sample_event_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_error_groups_sample_event_id ON public.error_groups USING btree (sample_event_id);


--

-- Name: ix_error_notification_rules_organisation_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_error_notification_rules_organisation_id ON public.error_notification_rules USING btree (organisation_id);


--

-- Name: ix_eval_cases_dataset_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_eval_cases_dataset_id ON public.eval_cases USING btree (dataset_id);


--

-- Name: ix_eval_cases_input_hash; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_eval_cases_input_hash ON public.eval_cases USING btree (input_hash);


--

-- Name: ix_eval_cases_org_dataset; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_eval_cases_org_dataset ON public.eval_cases USING btree (organisation_id, dataset_id);


--

-- Name: ix_eval_cases_organisation_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_eval_cases_organisation_id ON public.eval_cases USING btree (organisation_id);


--

-- Name: ix_eval_datasets_organisation_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_eval_datasets_organisation_id ON public.eval_datasets USING btree (organisation_id);


--

-- Name: ix_eval_datasets_owner_team_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_eval_datasets_owner_team_id ON public.eval_datasets USING btree (owner_team_id);


--

-- Name: ix_eval_results_eval_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_eval_results_eval_id ON public.eval_results USING btree (eval_id);


--

-- Name: ix_eval_results_eval_id_resolution; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_eval_results_eval_id_resolution ON public.eval_results USING btree (eval_id, resolution);


--

-- Name: ix_eval_results_org_eval_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_eval_results_org_eval_id ON public.eval_results USING btree (organisation_id, eval_id);


--

-- Name: ix_eval_results_org_evaluated_at; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_eval_results_org_evaluated_at ON public.eval_results USING btree (organisation_id, evaluated_at);


--

-- Name: ix_eval_results_organisation_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_eval_results_organisation_id ON public.eval_results USING btree (organisation_id);


--

-- Name: ix_eval_results_run_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_eval_results_run_id ON public.eval_results USING btree (run_id);


--

-- Name: ix_eval_results_suite_run_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_eval_results_suite_run_id ON public.eval_results USING btree (suite_run_id);


--

-- Name: ix_eval_suites_organisation_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_eval_suites_organisation_id ON public.eval_suites USING btree (organisation_id);


--

-- Name: ix_eval_suites_owner_team_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_eval_suites_owner_team_id ON public.eval_suites USING btree (owner_team_id);


--

-- Name: ix_evals_account_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_evals_account_id ON public.evals USING btree (account_id);


--

-- Name: ix_evals_org_is_muted; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_evals_org_is_muted ON public.evals USING btree (organisation_id, is_muted) WHERE (deleted_at IS NULL);


--

-- Name: ix_evals_organisation_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_evals_organisation_id ON public.evals USING btree (organisation_id);


--

-- Name: ix_evals_pipeline_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_evals_pipeline_id ON public.evals USING btree (pipeline_id);


--

-- Name: ix_evidence_created_at; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_evidence_created_at ON public.evidence USING btree (organisation_id, subject_type, subject_id, key, created_at DESC, id DESC);


--

-- Name: ix_evidence_org_subject; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_evidence_org_subject ON public.evidence USING btree (organisation_id, subject_type, subject_id, key);


--

-- Name: ix_evidence_redaction_rules_organisation_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_evidence_redaction_rules_organisation_id ON public.evidence_redaction_rules USING btree (organisation_id);


--

-- Name: ix_external_agents_organisation_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_external_agents_organisation_id ON public.external_agents USING btree (organisation_id);


--

-- Name: ix_external_agents_owner_team_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_external_agents_owner_team_id ON public.external_agents USING btree (owner_team_id);


--

-- Name: ix_feature_flag_catalog_tier_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_feature_flag_catalog_tier_id ON public.feature_flag_catalog USING btree (tier_id);


--

-- Name: ix_feedback_records_account_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_feedback_records_account_id ON public.feedback_records USING btree (account_id);


--

-- Name: ix_feedback_records_correction_run_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_feedback_records_correction_run_id ON public.feedback_records USING btree (correction_run_id);


--

-- Name: ix_feedback_records_org_run_node; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_feedback_records_org_run_node ON public.feedback_records USING btree (organisation_id, run_id, producing_node_id);


--

-- Name: ix_feedback_records_org_status_created; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_feedback_records_org_status_created ON public.feedback_records USING btree (organisation_id, feedback_status, created_at);


--

-- Name: ix_feedback_records_organisation_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_feedback_records_organisation_id ON public.feedback_records USING btree (organisation_id);


--

-- Name: ix_feedback_records_producing_agent_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_feedback_records_producing_agent_id ON public.feedback_records USING btree (producing_agent_id);


--

-- Name: ix_feedback_records_run_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_feedback_records_run_id ON public.feedback_records USING btree (run_id);


--

-- Name: ix_hitl_claims_account_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_hitl_claims_account_id ON public.hitl_claims USING btree (account_id);


--

-- Name: ix_hitl_claims_decided_by; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_hitl_claims_decided_by ON public.hitl_claims USING btree (decided_by);


--

-- Name: ix_hitl_claims_expiry_sweep; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_hitl_claims_expiry_sweep ON public.hitl_claims USING btree (organisation_id, expires_at) WHERE ((decision IS NULL) AND (account_id IS NOT NULL));


--

-- Name: ix_hitl_claims_org_decision_at; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_hitl_claims_org_decision_at ON public.hitl_claims USING btree (organisation_id, decision_at) WHERE (decision IS NOT NULL);


--

-- Name: ix_hitl_claims_organisation_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_hitl_claims_organisation_id ON public.hitl_claims USING btree (organisation_id);


--

-- Name: ix_hitl_claims_overdue_sweep; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_hitl_claims_overdue_sweep ON public.hitl_claims USING btree (organisation_id, claimed_at) WHERE ((decision IS NULL) AND (account_id IS NOT NULL) AND (overdue_notified_at IS NULL));


--

-- Name: ix_hitl_claims_pipeline_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_hitl_claims_pipeline_id ON public.hitl_claims USING btree (pipeline_id);


--

-- Name: ix_hitl_claims_required_team_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_hitl_claims_required_team_id ON public.hitl_claims USING btree (required_team_id);


--

-- Name: ix_hitl_claims_run_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_hitl_claims_run_id ON public.hitl_claims USING btree (run_id);


--

-- Name: ix_hitl_claims_sweep_actor_window; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_hitl_claims_sweep_actor_window ON public.hitl_claims USING btree (organisation_id, decided_by, decision_at) WHERE ((decided_by IS NOT NULL) AND (decision IS NOT NULL));


--

-- Name: ix_hitl_claims_terminalize_sweep; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_hitl_claims_terminalize_sweep ON public.hitl_claims USING btree (terminalize_at) WHERE ((decision IS NULL) AND (account_id IS NULL));


--

-- Name: ix_invitations_expires_at_live; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_invitations_expires_at_live ON public.invitations USING btree (expires_at) WHERE ((consumed_at IS NULL) AND (revoked_at IS NULL));


--

-- Name: ix_invitations_org_email_live; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_invitations_org_email_live ON public.invitations USING btree (organisation_id, email) WHERE ((consumed_at IS NULL) AND (revoked_at IS NULL));


--

-- Name: ix_invitations_organisation_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_invitations_organisation_id ON public.invitations USING btree (organisation_id);


--

-- Name: ix_journeys_active_org_kind_ref; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_journeys_active_org_kind_ref ON public.journeys USING btree (organisation_id, kind, ref) WHERE (dismissed_at IS NULL);


--

-- Name: ix_journeys_canonical_work_item_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_journeys_canonical_work_item_id ON public.journeys USING btree (canonical_work_item_id);


--

-- Name: ix_journeys_org_provenance; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_journeys_org_provenance ON public.journeys USING btree (organisation_id, provenance);


--

-- Name: ix_journeys_organisation_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_journeys_organisation_id ON public.journeys USING btree (organisation_id);


--

-- Name: ix_journeys_owner_team_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_journeys_owner_team_id ON public.journeys USING btree (owner_team_id);


--

-- Name: ix_library_primitives_account_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_library_primitives_account_id ON public.library_primitives USING btree (account_id);


--

-- Name: ix_library_primitives_forked_from; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_library_primitives_forked_from ON public.library_primitives USING btree (forked_from);


--

-- Name: ix_library_primitives_organisation_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_library_primitives_organisation_id ON public.library_primitives USING btree (organisation_id);


--

-- Name: ix_library_primitives_organisation_id_deleted_at; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_library_primitives_organisation_id_deleted_at ON public.library_primitives USING btree (organisation_id, deleted_at) WHERE (deleted_at IS NULL);


--

-- Name: ix_library_primitives_owner_team_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_library_primitives_owner_team_id ON public.library_primitives USING btree (owner_team_id);


--

-- Name: ix_library_primitives_type_slug; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_library_primitives_type_slug ON public.library_primitives USING btree (primitive_type, slug);


--

-- Name: ix_library_primitives_update_available_version_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_library_primitives_update_available_version_id ON public.library_primitives USING btree (update_available_version_id);


--

-- Name: ix_library_primitives_version_group_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_library_primitives_version_group_id ON public.library_primitives USING btree (version_group_id);


--

-- Name: ix_library_primitives_visibility_contribution; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_library_primitives_visibility_contribution ON public.library_primitives USING btree (visibility, contribution_status) WHERE (deleted_at IS NULL);


--

-- Name: ix_lifecycle_map_stages_account_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_lifecycle_map_stages_account_id ON public.lifecycle_map_stages USING btree (account_id);


--

-- Name: ix_lifecycle_map_stages_map_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_lifecycle_map_stages_map_id ON public.lifecycle_map_stages USING btree (map_id);


--

-- Name: ix_lifecycle_map_stages_organisation_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_lifecycle_map_stages_organisation_id ON public.lifecycle_map_stages USING btree (organisation_id);


--

-- Name: ix_lifecycle_map_stages_pipeline_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_lifecycle_map_stages_pipeline_id ON public.lifecycle_map_stages USING btree (pipeline_id);


--

-- Name: ix_lifecycle_maps_account_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_lifecycle_maps_account_id ON public.lifecycle_maps USING btree (account_id);


--

-- Name: ix_lifecycle_maps_organisation_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_lifecycle_maps_organisation_id ON public.lifecycle_maps USING btree (organisation_id);


--

-- Name: ix_lifecycle_maps_organisation_id_deleted_at; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_lifecycle_maps_organisation_id_deleted_at ON public.lifecycle_maps USING btree (organisation_id, deleted_at) WHERE (deleted_at IS NULL);


--

-- Name: ix_lifecycle_maps_owner_team_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_lifecycle_maps_owner_team_id ON public.lifecycle_maps USING btree (owner_team_id);


--

-- Name: ix_loop_admission_attempts_deleted_by; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_loop_admission_attempts_deleted_by ON public.system_map_loop_admission_attempts USING btree (deleted_by);


--

-- Name: ix_loop_admission_attempts_loop_config_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_loop_admission_attempts_loop_config_id ON public.system_map_loop_admission_attempts USING btree (loop_config_id);


--

-- Name: ix_loop_admission_attempts_organisation_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_loop_admission_attempts_organisation_id ON public.system_map_loop_admission_attempts USING btree (organisation_id);


--

-- Name: ix_loop_reservations_cooldown_until; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_loop_reservations_cooldown_until ON public.system_map_loop_reservations USING btree (cooldown_until);


--

-- Name: ix_loop_reservations_deleted_by; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_loop_reservations_deleted_by ON public.system_map_loop_reservations USING btree (deleted_by);


--

-- Name: ix_loop_reservations_loop_config_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_loop_reservations_loop_config_id ON public.system_map_loop_reservations USING btree (loop_config_id);


--

-- Name: ix_loop_reservations_organisation_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_loop_reservations_organisation_id ON public.system_map_loop_reservations USING btree (organisation_id);


--

-- Name: ix_loop_reservations_run_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_loop_reservations_run_id ON public.system_map_loop_reservations USING btree (run_id);


--

-- Name: ix_mcp_servers_organisation_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_mcp_servers_organisation_id ON public.mcp_servers USING btree (organisation_id);


--

-- Name: ix_mcp_servers_owner_account_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_mcp_servers_owner_account_id ON public.mcp_servers USING btree (owner_account_id);


--

-- Name: ix_mcp_servers_owner_team_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_mcp_servers_owner_team_id ON public.mcp_servers USING btree (owner_team_id);


--

-- Name: ix_mcp_setup_tokens_created_by; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_mcp_setup_tokens_created_by ON public.mcp_setup_tokens USING btree (created_by);


--

-- Name: ix_mcp_setup_tokens_organisation_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_mcp_setup_tokens_organisation_id ON public.mcp_setup_tokens USING btree (organisation_id);


--

-- Name: ix_mcp_setup_tokens_resource_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_mcp_setup_tokens_resource_id ON public.mcp_setup_tokens USING btree (resource_id);


--

-- Name: ix_metrics_staging_org_recorded_at; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_metrics_staging_org_recorded_at ON public.metrics_staging USING btree (organisation_id, recorded_at);


--

-- Name: ix_metrics_staging_recorded_at; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_metrics_staging_recorded_at ON public.metrics_staging USING btree (recorded_at);


--

-- Name: ix_model_backends_account_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_model_backends_account_id ON public.model_backends USING btree (account_id);


--

-- Name: ix_model_backends_organisation_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_model_backends_organisation_id ON public.model_backends USING btree (organisation_id);


--

-- Name: ix_model_backends_owner_team_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_model_backends_owner_team_id ON public.model_backends USING btree (owner_team_id);


--

-- Name: ix_modulo_journey_facts_org_created; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_modulo_journey_facts_org_created ON public.modulo_journey_facts USING btree (organisation_id, created_at);


--

-- Name: ix_modulo_journey_facts_organisation_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_modulo_journey_facts_organisation_id ON public.modulo_journey_facts USING btree (organisation_id);


--

-- Name: ix_node_categories_account_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_node_categories_account_id ON public.node_categories USING btree (account_id);


--

-- Name: ix_node_categories_organisation_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_node_categories_organisation_id ON public.node_categories USING btree (organisation_id);


--

-- Name: ix_node_categories_organisation_id_deleted_at; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_node_categories_organisation_id_deleted_at ON public.node_categories USING btree (organisation_id, deleted_at) WHERE (deleted_at IS NULL);


--

-- Name: ix_node_observations_account_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_node_observations_account_id ON public.node_observations USING btree (account_id) WHERE (account_id IS NOT NULL);


--

-- Name: ix_node_observations_organisation_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_node_observations_organisation_id ON public.node_observations USING btree (organisation_id);


--

-- Name: ix_node_observations_run_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_node_observations_run_id ON public.node_observations USING btree (run_id);


--

-- Name: ix_nodes_account_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_nodes_account_id ON public.nodes USING btree (account_id);


--

-- Name: ix_nodes_organisation_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_nodes_organisation_id ON public.nodes USING btree (organisation_id);


--

-- Name: ix_nodes_parent_created; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_nodes_parent_created ON public.nodes USING btree (parent_node_id, created_at);


--

-- Name: ix_nodes_parent_node_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_nodes_parent_node_id ON public.nodes USING btree (parent_node_id);


--

-- Name: ix_nodes_pipeline_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_nodes_pipeline_id ON public.nodes USING btree (pipeline_id);


--

-- Name: ix_notification_delivery_log_endpoint_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_notification_delivery_log_endpoint_id ON public.notification_delivery_log USING btree (endpoint_id);


--

-- Name: ix_notification_delivery_log_event_type; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_notification_delivery_log_event_type ON public.notification_delivery_log USING btree (event_type);


--

-- Name: ix_notification_delivery_log_org_event; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_notification_delivery_log_org_event ON public.notification_delivery_log USING btree (organisation_id, event_type);


--

-- Name: ix_notification_delivery_log_organisation_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_notification_delivery_log_organisation_id ON public.notification_delivery_log USING btree (organisation_id);


--

-- Name: ix_notification_delivery_log_run_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_notification_delivery_log_run_id ON public.notification_delivery_log USING btree (run_id);


--

-- Name: ix_notification_endpoints_account_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_notification_endpoints_account_id ON public.notification_endpoints USING btree (account_id);


--

-- Name: ix_notification_endpoints_organisation_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_notification_endpoints_organisation_id ON public.notification_endpoints USING btree (organisation_id);


--

-- Name: ix_notification_endpoints_organisation_id_deleted_at; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_notification_endpoints_organisation_id_deleted_at ON public.notification_endpoints USING btree (organisation_id, deleted_at) WHERE (deleted_at IS NULL);


--

-- Name: ix_notification_endpoints_team_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_notification_endpoints_team_id ON public.notification_endpoints USING btree (team_id);


--

-- Name: ix_notification_preferences_account_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_notification_preferences_account_id ON public.notification_preferences USING btree (account_id);


--

-- Name: ix_notification_preferences_organisation_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_notification_preferences_organisation_id ON public.notification_preferences USING btree (organisation_id);


--

-- Name: ix_notifications_org_category; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_notifications_org_category ON public.notifications USING btree (organisation_id, category);


--

-- Name: ix_notifications_org_created_at; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_notifications_org_created_at ON public.notifications USING btree (organisation_id, created_at);


--

-- Name: ix_notifications_org_expires_at; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_notifications_org_expires_at ON public.notifications USING btree (organisation_id, expires_at);


--

-- Name: ix_notifications_org_level; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_notifications_org_level ON public.notifications USING btree (organisation_id, level);


--

-- Name: ix_notifications_org_level_scope_category; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_notifications_org_level_scope_category ON public.notifications USING btree (organisation_id, level, scope, category);


--

-- Name: ix_notifications_org_scope_target; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_notifications_org_scope_target ON public.notifications USING btree (organisation_id, scope, target_user_id);


--

-- Name: ix_notifications_organisation_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_notifications_organisation_id ON public.notifications USING btree (organisation_id);


--

-- Name: ix_notifications_target_user_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_notifications_target_user_id ON public.notifications USING btree (target_user_id) WHERE (target_user_id IS NOT NULL);


--

-- Name: ix_oauth_auth_codes_org_client; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_oauth_auth_codes_org_client ON public.oauth_authorization_codes USING btree (organisation_id, client_id);


--

-- Name: ix_oauth_authorization_codes_account_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_oauth_authorization_codes_account_id ON public.oauth_authorization_codes USING btree (account_id);


--

-- Name: ix_oauth_authorization_codes_client_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_oauth_authorization_codes_client_id ON public.oauth_authorization_codes USING btree (client_id);


--

-- Name: ix_oauth_authorization_codes_organisation_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_oauth_authorization_codes_organisation_id ON public.oauth_authorization_codes USING btree (organisation_id);


--

-- Name: ix_oauth_clients_account_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_oauth_clients_account_id ON public.oauth_clients USING btree (account_id);


--

-- Name: ix_oauth_clients_client_id; Type: INDEX; Schema: public; Owner: -
--

CREATE UNIQUE INDEX ix_oauth_clients_client_id ON public.oauth_clients USING btree (client_id);


--

-- Name: ix_oauth_clients_organisation_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_oauth_clients_organisation_id ON public.oauth_clients USING btree (organisation_id);


--

-- Name: ix_oauth_clients_team_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_oauth_clients_team_id ON public.oauth_clients USING btree (team_id);


--

-- Name: ix_oauth_consent_states_account_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_oauth_consent_states_account_id ON public.oauth_consent_states USING btree (account_id);


--

-- Name: ix_oauth_consent_states_organisation_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_oauth_consent_states_organisation_id ON public.oauth_consent_states USING btree (organisation_id);


--

-- Name: ix_oauth_token_families_client_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_oauth_token_families_client_id ON public.oauth_token_families USING btree (client_id);


--

-- Name: ix_oauth_token_families_org_client; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_oauth_token_families_org_client ON public.oauth_token_families USING btree (organisation_id, client_id);


--

-- Name: ix_oauth_token_families_organisation_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_oauth_token_families_organisation_id ON public.oauth_token_families USING btree (organisation_id);


--

-- Name: ix_onboarding_progress_organisation_id; Type: INDEX; Schema: public; Owner: -
--

CREATE UNIQUE INDEX ix_onboarding_progress_organisation_id ON public.onboarding_progress USING btree (organisation_id);


--

-- Name: ix_org_api_keys_account_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_org_api_keys_account_id ON public.org_api_keys USING btree (account_id);


--

-- Name: ix_org_api_keys_live_run_keys; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_org_api_keys_live_run_keys ON public.org_api_keys USING btree (organisation_id, run_id) WHERE ((revoked_at IS NULL) AND (run_id IS NOT NULL));


--

-- Name: ix_org_api_keys_organisation_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_org_api_keys_organisation_id ON public.org_api_keys USING btree (organisation_id);


--

-- Name: ix_org_api_keys_run_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_org_api_keys_run_id ON public.org_api_keys USING btree (run_id);


--

-- Name: ix_org_api_keys_stale_sweep; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_org_api_keys_stale_sweep ON public.org_api_keys USING btree (organisation_id, last_used_at) WHERE (revoked_at IS NULL);


--

-- Name: ix_org_api_keys_team_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_org_api_keys_team_id ON public.org_api_keys USING btree (team_id);


--

-- Name: ix_org_daily_run_counts_org_date; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_org_daily_run_counts_org_date ON public.org_daily_run_counts USING btree (organisation_id, run_date);


--

-- Name: ix_org_daily_run_counts_organisation_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_org_daily_run_counts_organisation_id ON public.org_daily_run_counts USING btree (organisation_id);


--

-- Name: ix_org_daily_run_counts_run_date; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_org_daily_run_counts_run_date ON public.org_daily_run_counts USING btree (run_date);


--

-- Name: ix_org_daily_run_counts_team_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_org_daily_run_counts_team_id ON public.org_daily_run_counts USING btree (team_id);


--

-- Name: ix_org_memberships_account_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_org_memberships_account_id ON public.org_memberships USING btree (account_id);


--

-- Name: ix_org_memberships_org_account; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_org_memberships_org_account ON public.org_memberships USING btree (organisation_id, account_id);


--

-- Name: ix_org_memberships_org_role_active; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_org_memberships_org_role_active ON public.org_memberships USING btree (organisation_id, role) WHERE (deactivated_at IS NULL);


--

-- Name: ix_org_memberships_organisation_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_org_memberships_organisation_id ON public.org_memberships USING btree (organisation_id);


--

-- Name: ix_org_mint_budget_usage_organisation_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_org_mint_budget_usage_organisation_id ON public.org_mint_budget_usage USING btree (organisation_id);


--

-- Name: ix_org_mint_budget_usage_window_start; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_org_mint_budget_usage_window_start ON public.org_mint_budget_usage USING btree (window_start);


--

-- Name: ix_organisations_created_at; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_organisations_created_at ON public.organisations USING btree (created_at);


--

-- Name: ix_organisations_deleted_at; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_organisations_deleted_at ON public.organisations USING btree (deleted_at) WHERE (deleted_at IS NULL);


--

-- Name: ix_organisations_status; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_organisations_status ON public.organisations USING btree (status);


--

-- Name: ix_parameter_schemas_account_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_parameter_schemas_account_id ON public.parameter_schemas USING btree (account_id);


--

-- Name: ix_parameter_schemas_organisation_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_parameter_schemas_organisation_id ON public.parameter_schemas USING btree (organisation_id);


--

-- Name: ix_parameter_sets_account_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_parameter_sets_account_id ON public.parameter_sets USING btree (account_id);


--

-- Name: ix_parameter_sets_organisation_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_parameter_sets_organisation_id ON public.parameter_sets USING btree (organisation_id);


--

-- Name: ix_parameter_sets_organisation_id_deleted_at; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_parameter_sets_organisation_id_deleted_at ON public.parameter_sets USING btree (organisation_id, deleted_at) WHERE (deleted_at IS NULL);


--

-- Name: ix_parameter_sets_parameter_schema_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_parameter_sets_parameter_schema_id ON public.parameter_sets USING btree (parameter_schema_id);


--

-- Name: ix_pipeline_edges_organisation_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_pipeline_edges_organisation_id ON public.pipeline_edges USING btree (organisation_id);


--

-- Name: ix_pipeline_edges_pipeline_created; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_pipeline_edges_pipeline_created ON public.pipeline_edges USING btree (pipeline_id, created_at, id);


--

-- Name: ix_pipeline_edges_pipeline_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_pipeline_edges_pipeline_id ON public.pipeline_edges USING btree (pipeline_id);


--

-- Name: ix_pipeline_folders_account_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_pipeline_folders_account_id ON public.pipeline_folders USING btree (account_id);


--

-- Name: ix_pipeline_folders_org_sort; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_pipeline_folders_org_sort ON public.pipeline_folders USING btree (organisation_id, sort_order, name);


--

-- Name: ix_pipeline_folders_organisation_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_pipeline_folders_organisation_id ON public.pipeline_folders USING btree (organisation_id);


--

-- Name: ix_pipeline_folders_parent_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_pipeline_folders_parent_id ON public.pipeline_folders USING btree (parent_id);


--

-- Name: ix_pipeline_snapshots_account_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_pipeline_snapshots_account_id ON public.pipeline_snapshots USING btree (account_id) WHERE (account_id IS NOT NULL);


--

-- Name: ix_pipeline_snapshots_channel_version; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_pipeline_snapshots_channel_version ON public.pipeline_snapshots USING btree (pipeline_id, channel, snapshot_version) WHERE (draft IS FALSE);


--

-- Name: ix_pipeline_snapshots_environment_profile_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_pipeline_snapshots_environment_profile_id ON public.pipeline_snapshots USING btree (environment_profile_id);


--

-- Name: ix_pipeline_snapshots_organisation_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_pipeline_snapshots_organisation_id ON public.pipeline_snapshots USING btree (organisation_id);


--

-- Name: ix_pipeline_snapshots_pipeline_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_pipeline_snapshots_pipeline_id ON public.pipeline_snapshots USING btree (pipeline_id);


--

-- Name: ix_pipeline_snapshots_version_substantive; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_pipeline_snapshots_version_substantive ON public.pipeline_snapshots USING btree (pipeline_id, snapshot_version DESC) WHERE ((created_kind)::text <> 'run'::text);


--

-- Name: ix_pipelines_account_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_pipelines_account_id ON public.pipelines USING btree (account_id);


--

-- Name: ix_pipelines_business_owner_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_pipelines_business_owner_id ON public.pipelines USING btree (business_owner_id);


--

-- Name: ix_pipelines_collection_install_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_pipelines_collection_install_id ON public.pipelines USING btree (collection_install_id);


--

-- Name: ix_pipelines_environment_profile_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_pipelines_environment_profile_id ON public.pipelines USING btree (environment_profile_id);


--

-- Name: ix_pipelines_folder_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_pipelines_folder_id ON public.pipelines USING btree (folder_id);


--

-- Name: ix_pipelines_org_deleted_created; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_pipelines_org_deleted_created ON public.pipelines USING btree (organisation_id, deleted_at, created_at) WHERE (deleted_at IS NULL);


--

-- Name: ix_pipelines_organisation_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_pipelines_organisation_id ON public.pipelines USING btree (organisation_id);


--

-- Name: ix_pipelines_organisation_id_deleted_at; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_pipelines_organisation_id_deleted_at ON public.pipelines USING btree (organisation_id, deleted_at) WHERE (deleted_at IS NULL);


--

-- Name: ix_pipelines_owner_team_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_pipelines_owner_team_id ON public.pipelines USING btree (owner_team_id);


--

-- Name: ix_pipelines_reliability_owner_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_pipelines_reliability_owner_id ON public.pipelines USING btree (reliability_owner_id);


--

-- Name: ix_policy_gate_decisions_organisation_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_policy_gate_decisions_organisation_id ON public.policy_gate_decisions USING btree (organisation_id);


--

-- Name: ix_policy_gates_organisation_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_policy_gates_organisation_id ON public.policy_gates USING btree (organisation_id);


--

-- Name: ix_primitive_abuse_reports_organisation_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_primitive_abuse_reports_organisation_id ON public.primitive_abuse_reports USING btree (organisation_id);


--

-- Name: ix_primitive_abuse_reports_primitive_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_primitive_abuse_reports_primitive_id ON public.primitive_abuse_reports USING btree (primitive_id);


--

-- Name: ix_primitive_abuse_reports_rating_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_primitive_abuse_reports_rating_id ON public.primitive_abuse_reports USING btree (rating_id);


--

-- Name: ix_primitive_abuse_reports_reporter_account_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_primitive_abuse_reports_reporter_account_id ON public.primitive_abuse_reports USING btree (reporter_account_id);


--

-- Name: ix_primitive_abuse_reports_reviewer_account_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_primitive_abuse_reports_reviewer_account_id ON public.primitive_abuse_reports USING btree (reviewer_account_id);


--

-- Name: ix_primitive_ratings_account_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_primitive_ratings_account_id ON public.primitive_ratings USING btree (account_id) WHERE (account_id IS NOT NULL);


--

-- Name: ix_primitive_ratings_organisation_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_primitive_ratings_organisation_id ON public.primitive_ratings USING btree (organisation_id);


--

-- Name: ix_primitive_ratings_primitive_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_primitive_ratings_primitive_id ON public.primitive_ratings USING btree (primitive_id);


--

-- Name: ix_publishers_organisation_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_publishers_organisation_id ON public.publishers USING btree (organisation_id);


--

-- Name: ix_run_attempts_organisation_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_run_attempts_organisation_id ON public.run_attempts USING btree (organisation_id);


--

-- Name: ix_run_daily_facts_folder_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_run_daily_facts_folder_id ON public.run_daily_facts USING btree (folder_id);


--

-- Name: ix_run_daily_facts_org_date; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_run_daily_facts_org_date ON public.run_daily_facts USING btree (organisation_id, run_date);


--

-- Name: ix_run_daily_facts_pipeline_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_run_daily_facts_pipeline_id ON public.run_daily_facts USING btree (pipeline_id);


--

-- Name: ix_run_daily_facts_team_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_run_daily_facts_team_id ON public.run_daily_facts USING btree (team_id);


--

-- Name: ix_run_evidence_organisation_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_run_evidence_organisation_id ON public.run_evidence USING btree (organisation_id);


--

-- Name: ix_run_evidence_run_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_run_evidence_run_id ON public.run_evidence USING btree (run_id);


--

-- Name: ix_run_node_outputs_audit; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_run_node_outputs_audit ON public.run_node_outputs USING btree (run_id, node_id) WHERE (node_id = '__mwi_audit__'::text);


--

-- Name: ix_run_node_outputs_enforcement_pending; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_run_node_outputs_enforcement_pending ON public.run_node_outputs USING btree (run_id) WHERE ((schema_enforcement_json IS NOT NULL) AND (attempt_key <> '__final__'::text));


--

-- Name: ix_run_node_outputs_organisation_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_run_node_outputs_organisation_id ON public.run_node_outputs USING btree (organisation_id);


--

-- Name: ix_runner_probe_cache_machine_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_runner_probe_cache_machine_id ON public.runner_probe_cache USING btree (machine_id);


--

-- Name: ix_runner_probe_cache_organisation_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_runner_probe_cache_organisation_id ON public.runner_probe_cache USING btree (organisation_id);


--

-- Name: ix_runs_org_batch; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_runs_org_batch ON public.runs USING btree (organisation_id, batch_id) WHERE (batch_id IS NOT NULL);


--

-- Name: ix_runs_org_created_pipeline; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_runs_org_created_pipeline ON public.runs USING btree (organisation_id, created_at) INCLUDE (pipeline_id);


--

-- Name: ix_runs_org_error_code; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_runs_org_error_code ON public.runs USING btree (organisation_id, error_code) WHERE (error_code IS NOT NULL);


--

-- Name: ix_runs_org_runner_marker_sweep; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_runs_org_runner_marker_sweep ON public.runs USING btree (organisation_id) WHERE (sandbox_dispatch_state IS NOT NULL);


--

-- Name: ix_runs_org_snapshot_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_runs_org_snapshot_id ON public.runs USING btree (organisation_id, snapshot_id);


--

-- Name: ix_runs_org_work_item_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_runs_org_work_item_id ON public.runs USING btree (organisation_id, work_item_id);


--

-- Name: ix_runs_organisation_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_runs_organisation_id ON public.runs USING btree (organisation_id);


--

-- Name: ix_runs_organisation_status; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_runs_organisation_status ON public.runs USING btree (organisation_id, status);


--

-- Name: ix_runs_owner_team_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_runs_owner_team_id ON public.runs USING btree (owner_team_id);


--

-- Name: ix_runs_parent_run_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_runs_parent_run_id ON public.runs USING btree (parent_run_id);


--

-- Name: ix_runs_pipeline_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_runs_pipeline_id ON public.runs USING btree (pipeline_id);


--

-- Name: ix_runs_pipeline_status; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_runs_pipeline_status ON public.runs USING btree (pipeline_id, status);


--

-- Name: ix_runs_probe; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_runs_probe ON public.runs USING btree (organisation_id, started_at);


--

-- Name: ix_runs_run_group_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_runs_run_group_id ON public.runs USING btree (run_group_id);


--

-- Name: ix_runs_snapshot_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_runs_snapshot_id ON public.runs USING btree (snapshot_id);


--

-- Name: ix_runs_status_heartbeat; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_runs_status_heartbeat ON public.runs USING btree (status, heartbeat_at) WHERE ((status)::text = ANY ((ARRAY['pending'::character varying, 'running'::character varying])::text[]));


--

-- Name: ix_runs_streak_engine; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_runs_streak_engine ON public.runs USING btree (trigger_id, completed_at DESC);


--

-- Name: ix_runs_trigger_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_runs_trigger_id ON public.runs USING btree (trigger_id);


--

-- Name: ix_runs_trigger_id_created_at; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_runs_trigger_id_created_at ON public.runs USING btree (trigger_id, created_at);


--

-- Name: ix_runs_trigger_id_status; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_runs_trigger_id_status ON public.runs USING btree (trigger_id, status);


--

-- Name: ix_runs_unclassified_terminal; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_runs_unclassified_terminal ON public.runs USING btree (status, completed_at DESC) WHERE (run_classification IS NULL);


--

-- Name: ix_runs_variant_group_batch; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_runs_variant_group_batch ON public.runs USING btree (variant_group_id, batch_id);


--

-- Name: ix_runs_webhook_decay_sweep; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_runs_webhook_decay_sweep ON public.runs USING btree (completed_at) INCLUDE (organisation_id, pipeline_id, status, node_deadline_watchdog_fired_count) WHERE (((trigger_type)::text = 'webhook'::text) AND ((status)::text = ANY ((ARRAY['budget_exceeded'::character varying, 'cancelled'::character varying, 'compensation_failed'::character varying, 'complete'::character varying, 'cost_ceiling_exceeded'::character varying, 'eval_failed'::character varying, 'failed'::character varying, 'rejected'::character varying, 'router_no_match'::character varying, 'stalled'::character varying])::text[])));


--

-- Name: ix_runs_work_item_refs_gin; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_runs_work_item_refs_gin ON public.runs USING gin (work_item_refs) WHERE (jsonb_array_length(work_item_refs) > 0);


--

-- Name: ix_runs_workspace_drift_sweep; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_runs_workspace_drift_sweep ON public.runs USING btree (id) WHERE (((status)::text = ANY ((ARRAY['complete'::character varying, 'failed'::character varying, 'cancelled'::character varying, 'eval_failed'::character varying, 'stalled'::character varying, 'budget_exceeded'::character varying, 'router_no_match'::character varying, 'cost_ceiling_exceeded'::character varying, 'compensation_failed'::character varying, 'rejected'::character varying])::text[])) AND (workspace_inputs_drift_detected IS NULL));


--

-- Name: ix_saved_views_account_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_saved_views_account_id ON public.saved_views USING btree (account_id);


--

-- Name: ix_saved_views_organisation_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_saved_views_organisation_id ON public.saved_views USING btree (organisation_id);


--

-- Name: ix_saved_views_organisation_id_deleted_at; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_saved_views_organisation_id_deleted_at ON public.saved_views USING btree (organisation_id, deleted_at) WHERE (deleted_at IS NULL);


--

-- Name: ix_saved_views_view_type; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_saved_views_view_type ON public.saved_views USING btree (view_type);


--

-- Name: ix_scheduled_reports_created_by; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_scheduled_reports_created_by ON public.scheduled_reports USING btree (created_by) WHERE (created_by IS NOT NULL);


--

-- Name: ix_scheduled_reports_due_scan; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_scheduled_reports_due_scan ON public.scheduled_reports USING btree (organisation_id, next_send_at) WHERE ((active IS TRUE) AND (next_send_at IS NOT NULL));


--

-- Name: ix_scheduled_reports_organisation_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_scheduled_reports_organisation_id ON public.scheduled_reports USING btree (organisation_id);


--

-- Name: ix_scheduled_reports_organisation_id_deleted_at; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_scheduled_reports_organisation_id_deleted_at ON public.scheduled_reports USING btree (organisation_id, deleted_at) WHERE (deleted_at IS NULL);


--

-- Name: ix_scheduled_reports_report_type; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_scheduled_reports_report_type ON public.scheduled_reports USING btree (report_type);


--

-- Name: ix_schema_folders_account_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_schema_folders_account_id ON public.schema_folders USING btree (account_id);


--

-- Name: ix_schema_folders_organisation_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_schema_folders_organisation_id ON public.schema_folders USING btree (organisation_id);


--

-- Name: ix_schema_folders_parent_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_schema_folders_parent_id ON public.schema_folders USING btree (parent_id);


--

-- Name: ix_schema_versions_account_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_schema_versions_account_id ON public.schema_versions USING btree (account_id);


--

-- Name: ix_schema_versions_organisation_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_schema_versions_organisation_id ON public.schema_versions USING btree (organisation_id);


--

-- Name: ix_schema_versions_schema_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_schema_versions_schema_id ON public.schema_versions USING btree (schema_id);


--

-- Name: ix_schemas_account_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_schemas_account_id ON public.schemas USING btree (account_id);


--

-- Name: ix_schemas_collection_install_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_schemas_collection_install_id ON public.schemas USING btree (collection_install_id);


--

-- Name: ix_schemas_folder_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_schemas_folder_id ON public.schemas USING btree (folder_id);


--

-- Name: ix_schemas_organisation_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_schemas_organisation_id ON public.schemas USING btree (organisation_id);


--

-- Name: ix_secrets_key; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_secrets_key ON public.secrets USING btree (key);


--

-- Name: ix_secrets_organisation_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_secrets_organisation_id ON public.secrets USING btree (organisation_id);


--

-- Name: ix_snapshot_schema_pins_organisation_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_snapshot_schema_pins_organisation_id ON public.snapshot_schema_pins USING btree (organisation_id);


--

-- Name: ix_snapshot_schema_pins_schema_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_snapshot_schema_pins_schema_id ON public.snapshot_schema_pins USING btree (schema_id);


--

-- Name: ix_snapshot_schema_pins_snapshot_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_snapshot_schema_pins_snapshot_id ON public.snapshot_schema_pins USING btree (snapshot_id);


--

-- Name: ix_spend_anomalies_anomaly_date; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_spend_anomalies_anomaly_date ON public.spend_anomalies USING btree (anomaly_date);


--

-- Name: ix_spend_anomalies_organisation_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_spend_anomalies_organisation_id ON public.spend_anomalies USING btree (organisation_id);


--

-- Name: ix_spend_anomalies_pipeline_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_spend_anomalies_pipeline_id ON public.spend_anomalies USING btree (pipeline_id);


--

-- Name: ix_sso_providers_organisation_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_sso_providers_organisation_id ON public.sso_providers USING btree (organisation_id);


--

-- Name: ix_suite_runs_active_check; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_suite_runs_active_check ON public.suite_runs USING btree (organisation_id, suite_id, dataset_id, state);


--

-- Name: ix_suite_runs_org_created; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_suite_runs_org_created ON public.suite_runs USING btree (organisation_id, created_at);


--

-- Name: ix_suite_runs_org_suite_notified; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_suite_runs_org_suite_notified ON public.suite_runs USING btree (organisation_id, suite_id, notified_at);


--

-- Name: ix_suite_runs_organisation_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_suite_runs_organisation_id ON public.suite_runs USING btree (organisation_id);


--

-- Name: ix_suite_runs_state_created; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_suite_runs_state_created ON public.suite_runs USING btree (organisation_id, state, created_at);


--

-- Name: ix_suite_runs_suite_dataset; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_suite_runs_suite_dataset ON public.suite_runs USING btree (suite_id, dataset_id);


--

-- Name: ix_system_audit_events_event_type; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_system_audit_events_event_type ON public.system_audit_events USING btree (event_type);


--

-- Name: ix_system_audit_events_org_id_created_at; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_system_audit_events_org_id_created_at ON public.system_audit_events USING btree (org_id, created_at);


--

-- Name: ix_system_config_updated_by; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_system_config_updated_by ON public.system_config USING btree (updated_by);


--

-- Name: ix_system_map_attribution_bindings_deleted_by; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_system_map_attribution_bindings_deleted_by ON public.system_map_attribution_bindings USING btree (deleted_by);


--

-- Name: ix_system_map_attribution_bindings_organisation_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_system_map_attribution_bindings_organisation_id ON public.system_map_attribution_bindings USING btree (organisation_id);


--

-- Name: ix_system_map_attribution_bindings_pipeline_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_system_map_attribution_bindings_pipeline_id ON public.system_map_attribution_bindings USING btree (pipeline_id);


--

-- Name: ix_system_map_attribution_bindings_system_map_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_system_map_attribution_bindings_system_map_id ON public.system_map_attribution_bindings USING btree (system_map_id);


--

-- Name: ix_system_map_attribution_bindings_trigger_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_system_map_attribution_bindings_trigger_id ON public.system_map_attribution_bindings USING btree (trigger_id);


--

-- Name: ix_system_map_loop_configs_action_pipeline_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_system_map_loop_configs_action_pipeline_id ON public.system_map_loop_configs USING btree (action_pipeline_id);


--

-- Name: ix_system_map_loop_configs_deleted_by; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_system_map_loop_configs_deleted_by ON public.system_map_loop_configs USING btree (deleted_by);


--

-- Name: ix_system_map_loop_configs_organisation_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_system_map_loop_configs_organisation_id ON public.system_map_loop_configs USING btree (organisation_id);


--

-- Name: ix_system_map_loop_configs_revision_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_system_map_loop_configs_revision_id ON public.system_map_loop_configs USING btree (revision_id);


--

-- Name: ix_system_map_nodes_connector_instance_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_system_map_nodes_connector_instance_id ON public.system_map_nodes USING btree (connector_instance_id);


--

-- Name: ix_system_map_nodes_deleted_by; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_system_map_nodes_deleted_by ON public.system_map_nodes USING btree (deleted_by);


--

-- Name: ix_system_map_nodes_eval_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_system_map_nodes_eval_id ON public.system_map_nodes USING btree (eval_id);


--

-- Name: ix_system_map_nodes_organisation_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_system_map_nodes_organisation_id ON public.system_map_nodes USING btree (organisation_id);


--

-- Name: ix_system_map_nodes_pipeline_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_system_map_nodes_pipeline_id ON public.system_map_nodes USING btree (pipeline_id);


--

-- Name: ix_system_map_nodes_revision_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_system_map_nodes_revision_id ON public.system_map_nodes USING btree (revision_id);


--

-- Name: ix_system_map_relationships_deleted_by; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_system_map_relationships_deleted_by ON public.system_map_relationships USING btree (deleted_by);


--

-- Name: ix_system_map_relationships_organisation_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_system_map_relationships_organisation_id ON public.system_map_relationships USING btree (organisation_id);


--

-- Name: ix_system_map_relationships_revision_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_system_map_relationships_revision_id ON public.system_map_relationships USING btree (revision_id);


--

-- Name: ix_system_map_revisions_deleted_by; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_system_map_revisions_deleted_by ON public.system_map_revisions USING btree (deleted_by);


--

-- Name: ix_system_map_revisions_effective_from; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_system_map_revisions_effective_from ON public.system_map_revisions USING btree (effective_from);


--

-- Name: ix_system_map_revisions_effective_to; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_system_map_revisions_effective_to ON public.system_map_revisions USING btree (effective_to);


--

-- Name: ix_system_map_revisions_map_effective_range; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_system_map_revisions_map_effective_range ON public.system_map_revisions USING btree (system_map_id, effective_from, effective_to);


--

-- Name: ix_system_map_revisions_organisation_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_system_map_revisions_organisation_id ON public.system_map_revisions USING btree (organisation_id);


--

-- Name: ix_system_map_revisions_status; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_system_map_revisions_status ON public.system_map_revisions USING btree (status);


--

-- Name: ix_system_map_revisions_system_map_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_system_map_revisions_system_map_id ON public.system_map_revisions USING btree (system_map_id);


--

-- Name: ix_system_maps_account_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_system_maps_account_id ON public.system_maps USING btree (account_id);


--

-- Name: ix_system_maps_deleted_by; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_system_maps_deleted_by ON public.system_maps USING btree (deleted_by);


--

-- Name: ix_system_maps_organisation_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_system_maps_organisation_id ON public.system_maps USING btree (organisation_id);


--

-- Name: ix_system_maps_owner_team_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_system_maps_owner_team_id ON public.system_maps USING btree (owner_team_id);


--

-- Name: ix_system_maps_published_revision_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_system_maps_published_revision_id ON public.system_maps USING btree (published_revision_id);


--

-- Name: ix_team_memberships_account_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_team_memberships_account_id ON public.team_memberships USING btree (account_id);


--

-- Name: ix_team_memberships_organisation_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_team_memberships_organisation_id ON public.team_memberships USING btree (organisation_id);


--

-- Name: ix_team_memberships_team_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_team_memberships_team_id ON public.team_memberships USING btree (team_id);


--

-- Name: ix_teams_account_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_teams_account_id ON public.teams USING btree (account_id);


--

-- Name: ix_teams_organisation_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_teams_organisation_id ON public.teams USING btree (organisation_id);


--

-- Name: ix_teams_organisation_id_deleted_at; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_teams_organisation_id_deleted_at ON public.teams USING btree (organisation_id, deleted_at) WHERE (deleted_at IS NULL);


--

-- Name: ix_token_families_account_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_token_families_account_id ON public.token_families USING btree (account_id);


--

-- Name: ix_token_families_organisation_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_token_families_organisation_id ON public.token_families USING btree (organisation_id);


--

-- Name: ix_trigger_events_org_created; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_trigger_events_org_created ON public.trigger_events USING btree (organisation_id, created_at, id);


--

-- Name: ix_trigger_events_organisation_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_trigger_events_organisation_id ON public.trigger_events USING btree (organisation_id);


--

-- Name: ix_trigger_events_received_at; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_trigger_events_received_at ON public.trigger_events USING btree (received_at);


--

-- Name: ix_trigger_events_run_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_trigger_events_run_id ON public.trigger_events USING btree (run_id);


--

-- Name: ix_trigger_events_trigger_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_trigger_events_trigger_id ON public.trigger_events USING btree (trigger_id);


--

-- Name: ix_trigger_events_trigger_org_created; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_trigger_events_trigger_org_created ON public.trigger_events USING btree (trigger_id, organisation_id, created_at, id);


--

-- Name: ix_triggers_account_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_triggers_account_id ON public.triggers USING btree (account_id);


--

-- Name: ix_triggers_agent_signal_active; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_triggers_agent_signal_active ON public.triggers USING btree (organisation_id) WHERE (((trigger_type)::text = 'agent_signal'::text) AND (active IS TRUE) AND (deleted_at IS NULL));


--

-- Name: ix_triggers_due_cron; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_triggers_due_cron ON public.triggers USING btree (organisation_id, next_fire_at) WHERE (((trigger_type)::text = 'cron'::text) AND (active IS TRUE) AND (deleted_at IS NULL) AND (cron_expression IS NOT NULL));


--

-- Name: ix_triggers_due_ongoing; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_triggers_due_ongoing ON public.triggers USING btree (organisation_id) WHERE (((trigger_type)::text = 'ongoing'::text) AND (active IS TRUE) AND (deleted_at IS NULL));


--

-- Name: ix_triggers_due_polling; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_triggers_due_polling ON public.triggers USING btree (organisation_id, next_fire_at) WHERE (((trigger_type)::text = 'polling'::text) AND (active IS TRUE) AND (deleted_at IS NULL));


--

-- Name: ix_triggers_eval_suite_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_triggers_eval_suite_id ON public.triggers USING btree (eval_suite_id);


--

-- Name: ix_triggers_next_fire_at; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_triggers_next_fire_at ON public.triggers USING btree (next_fire_at);


--

-- Name: ix_triggers_organisation_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_triggers_organisation_id ON public.triggers USING btree (organisation_id);


--

-- Name: ix_triggers_organisation_id_deleted_at; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_triggers_organisation_id_deleted_at ON public.triggers USING btree (organisation_id, deleted_at) WHERE (deleted_at IS NULL);


--

-- Name: ix_triggers_pipeline_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_triggers_pipeline_id ON public.triggers USING btree (pipeline_id);


--

-- Name: ix_variant_batch_state_organisation_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_variant_batch_state_organisation_id ON public.variant_batch_state USING btree (organisation_id);


--

-- Name: ix_variant_groups_degraded_evals; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_variant_groups_degraded_evals ON public.variant_groups USING btree (degraded_evals);


--

-- Name: ix_variant_groups_organisation_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_variant_groups_organisation_id ON public.variant_groups USING btree (organisation_id);


--

-- Name: ix_variant_groups_organisation_id_deleted_at; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_variant_groups_organisation_id_deleted_at ON public.variant_groups USING btree (organisation_id, deleted_at) WHERE (deleted_at IS NULL);


--

-- Name: ix_variant_groups_pipeline_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_variant_groups_pipeline_id ON public.variant_groups USING btree (pipeline_id);


--

-- Name: ix_web_vital_events_metric_name; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_web_vital_events_metric_name ON public.web_vital_events USING btree (metric_name);


--

-- Name: ix_web_vital_events_metric_name_recorded; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_web_vital_events_metric_name_recorded ON public.web_vital_events USING btree (metric_name, recorded_at);


--

-- Name: ix_web_vital_events_org_recorded; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_web_vital_events_org_recorded ON public.web_vital_events USING btree (organisation_id, recorded_at);


--

-- Name: ix_web_vital_events_organisation_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_web_vital_events_organisation_id ON public.web_vital_events USING btree (organisation_id);


--

-- Name: ix_webhook_dedup_hashes_expires_at; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_webhook_dedup_hashes_expires_at ON public.webhook_dedup_hashes USING btree (expires_at);


--

-- Name: ix_webhook_dedup_hashes_org_expires; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_webhook_dedup_hashes_org_expires ON public.webhook_dedup_hashes USING btree (organisation_id, expires_at);


--

-- Name: ix_webhook_dedup_hashes_organisation_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_webhook_dedup_hashes_organisation_id ON public.webhook_dedup_hashes USING btree (organisation_id);


--

-- Name: ix_webhook_dedup_hashes_trigger_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_webhook_dedup_hashes_trigger_id ON public.webhook_dedup_hashes USING btree (trigger_id);


--

-- Name: ix_webhook_payloads_expires_at; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_webhook_payloads_expires_at ON public.webhook_payloads USING btree (expires_at);


--

-- Name: ix_webhook_payloads_org_expires; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_webhook_payloads_org_expires ON public.webhook_payloads USING btree (organisation_id, expires_at);


--

-- Name: ix_webhook_payloads_organisation_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_webhook_payloads_organisation_id ON public.webhook_payloads USING btree (organisation_id);


--

-- Name: ix_webhook_payloads_trigger_event_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_webhook_payloads_trigger_event_id ON public.webhook_payloads USING btree (trigger_event_id);


--

-- Name: ix_webhook_payloads_trigger_event_org; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_webhook_payloads_trigger_event_org ON public.webhook_payloads USING btree (trigger_event_id, organisation_id);


--

-- Name: uq_attr_binding_action_per_pipeline_node; Type: INDEX; Schema: public; Owner: -
--

CREATE UNIQUE INDEX uq_attr_binding_action_per_pipeline_node ON public.system_map_attribution_bindings USING btree (organisation_id, pipeline_id, node_key) WHERE (((binding_kind)::text = 'action_pipeline'::text) AND (deleted_at IS NULL));


--

-- Name: uq_attr_binding_trigger_per_pipeline_node; Type: INDEX; Schema: public; Owner: -
--

CREATE UNIQUE INDEX uq_attr_binding_trigger_per_pipeline_node ON public.system_map_attribution_bindings USING btree (organisation_id, pipeline_id, node_key) WHERE (((binding_kind)::text = 'trigger'::text) AND (deleted_at IS NULL));


--

-- Name: uq_collection_install_org_collection; Type: INDEX; Schema: public; Owner: -
--

CREATE UNIQUE INDEX uq_collection_install_org_collection ON public.collection_install USING btree (organisation_id, collection_id) WHERE (deleted_at IS NULL);


--

-- Name: uq_composite_templates_org_name; Type: INDEX; Schema: public; Owner: -
--

CREATE UNIQUE INDEX uq_composite_templates_org_name ON public.composite_templates USING btree (organisation_id, name) WHERE (deleted_at IS NULL);


--

-- Name: uq_connector_instances_org_name; Type: INDEX; Schema: public; Owner: -
--

CREATE UNIQUE INDEX uq_connector_instances_org_name ON public.connector_instances USING btree (organisation_id, name) WHERE (deleted_at IS NULL);


--

-- Name: uq_cost_components_org_name_active; Type: INDEX; Schema: public; Owner: -
--

CREATE UNIQUE INDEX uq_cost_components_org_name_active ON public.cost_components USING btree (organisation_id, name) WHERE (deleted_at IS NULL);


--

-- Name: uq_cost_components_org_report_key_self; Type: INDEX; Schema: public; Owner: -
--

CREATE UNIQUE INDEX uq_cost_components_org_report_key_self ON public.cost_components USING btree (organisation_id, report_key) WHERE (((kind)::text = 'self_reported'::text) AND (deleted_at IS NULL));


--

-- Name: uq_enr_org_active; Type: INDEX; Schema: public; Owner: -
--

CREATE UNIQUE INDEX uq_enr_org_active ON public.error_notification_rules USING btree (organisation_id, signal) WHERE (deleted_at IS NULL);


--

-- Name: uq_environment_profiles_org_name; Type: INDEX; Schema: public; Owner: -
--

CREATE UNIQUE INDEX uq_environment_profiles_org_name ON public.environment_profiles USING btree (organisation_id, name) WHERE (deleted_at IS NULL);


--

-- Name: uq_eval_cases_dataset_hash_active; Type: INDEX; Schema: public; Owner: -
--

CREATE UNIQUE INDEX uq_eval_cases_dataset_hash_active ON public.eval_cases USING btree (dataset_id, input_hash) WHERE (deleted_at IS NULL);


--

-- Name: uq_eval_datasets_org_name_active; Type: INDEX; Schema: public; Owner: -
--

CREATE UNIQUE INDEX uq_eval_datasets_org_name_active ON public.eval_datasets USING btree (organisation_id, name) WHERE (deleted_at IS NULL);


--

-- Name: uq_library_primitive_version; Type: INDEX; Schema: public; Owner: -
--

CREATE UNIQUE INDEX uq_library_primitive_version ON public.library_primitives USING btree (organisation_id, source, slug, version) WHERE (deleted_at IS NULL);


--

-- Name: uq_lifecycle_map_stages_active_pipeline; Type: INDEX; Schema: public; Owner: -
--

CREATE UNIQUE INDEX uq_lifecycle_map_stages_active_pipeline ON public.lifecycle_map_stages USING btree (organisation_id, pipeline_id) WHERE (pipeline_id IS NOT NULL);


--

-- Name: uq_lifecycle_maps_org_name; Type: INDEX; Schema: public; Owner: -
--

CREATE UNIQUE INDEX uq_lifecycle_maps_org_name ON public.lifecycle_maps USING btree (organisation_id, name) WHERE (deleted_at IS NULL);


--

-- Name: uq_node_categories_org_name; Type: INDEX; Schema: public; Owner: -
--

CREATE UNIQUE INDEX uq_node_categories_org_name ON public.node_categories USING btree (organisation_id, name) WHERE (deleted_at IS NULL);


--

-- Name: uq_org_daily_run_counts; Type: INDEX; Schema: public; Owner: -
--

CREATE UNIQUE INDEX uq_org_daily_run_counts ON public.org_daily_run_counts USING btree (organisation_id, team_id, run_date) NULLS NOT DISTINCT;


--

-- Name: uq_org_forwarder_type; Type: INDEX; Schema: public; Owner: -
--

CREATE UNIQUE INDEX uq_org_forwarder_type ON public.error_forwarder_configs USING btree (organisation_id, forwarder_type) WHERE (deleted_at IS NULL);


--

-- Name: uq_organisations_slug; Type: INDEX; Schema: public; Owner: -
--

CREATE UNIQUE INDEX uq_organisations_slug ON public.organisations USING btree (slug) WHERE (deleted_at IS NULL);


--

-- Name: uq_parameter_schemas_org_name; Type: INDEX; Schema: public; Owner: -
--

CREATE UNIQUE INDEX uq_parameter_schemas_org_name ON public.parameter_schemas USING btree (organisation_id, name) WHERE (deleted_at IS NULL);


--

-- Name: uq_parameter_sets_schema_name; Type: INDEX; Schema: public; Owner: -
--

CREATE UNIQUE INDEX uq_parameter_sets_schema_name ON public.parameter_sets USING btree (parameter_schema_id, name) WHERE (deleted_at IS NULL);


--

-- Name: uq_pipelines_org_name; Type: INDEX; Schema: public; Owner: -
--

CREATE UNIQUE INDEX uq_pipelines_org_name ON public.pipelines USING btree (organisation_id, name) WHERE (deleted_at IS NULL);


--

-- Name: uq_policy_gate_decisions_eval_result_id; Type: INDEX; Schema: public; Owner: -
--

CREATE UNIQUE INDEX uq_policy_gate_decisions_eval_result_id ON public.policy_gate_decisions USING btree (eval_result_id) WHERE (eval_result_id IS NOT NULL);


--

-- Name: uq_policy_gates_eval_id_live; Type: INDEX; Schema: public; Owner: -
--

CREATE UNIQUE INDEX uq_policy_gates_eval_id_live ON public.policy_gates USING btree (eval_id) WHERE (deleted_at IS NULL);


--

-- Name: uq_run_daily_facts_run_id; Type: INDEX; Schema: public; Owner: -
--

CREATE UNIQUE INDEX uq_run_daily_facts_run_id ON public.run_daily_facts USING btree (run_id);


--

-- Name: uq_runs_idempotency; Type: INDEX; Schema: public; Owner: -
--

CREATE UNIQUE INDEX uq_runs_idempotency ON public.runs USING btree (pipeline_id, idempotency_key) WHERE (idempotency_key IS NOT NULL);


--

-- Name: uq_runs_pipeline_rate_limit_key; Type: INDEX; Schema: public; Owner: -
--

CREATE UNIQUE INDEX uq_runs_pipeline_rate_limit_key ON public.runs USING btree (pipeline_id, rate_limit_key) WHERE (rate_limit_key IS NOT NULL);


--

-- Name: uq_runs_resume_parent; Type: INDEX; Schema: public; Owner: -
--

CREATE UNIQUE INDEX uq_runs_resume_parent ON public.runs USING btree (parent_run_id) WHERE ((run_group_kind)::text = 'resume'::text);


--

-- Name: uq_runs_thread_non_resume; Type: INDEX; Schema: public; Owner: -
--

CREATE UNIQUE INDEX uq_runs_thread_non_resume ON public.runs USING btree (langgraph_thread_id) WHERE ((run_group_kind)::text IS DISTINCT FROM 'resume'::text);


--

-- Name: uq_saved_views_org_name; Type: INDEX; Schema: public; Owner: -
--

CREATE UNIQUE INDEX uq_saved_views_org_name ON public.saved_views USING btree (organisation_id, name) WHERE (deleted_at IS NULL);


--

-- Name: uq_spend_anomalies_org_date; Type: INDEX; Schema: public; Owner: -
--

CREATE UNIQUE INDEX uq_spend_anomalies_org_date ON public.spend_anomalies USING btree (organisation_id, anomaly_date) WHERE (pipeline_id IS NULL);


--

-- Name: uq_sso_providers_provider_id; Type: INDEX; Schema: public; Owner: -
--

CREATE UNIQUE INDEX uq_sso_providers_provider_id ON public.sso_providers USING btree (provider_id) WHERE (provider_id IS NOT NULL);


--

-- Name: uq_system_map_revisions_one_published; Type: INDEX; Schema: public; Owner: -
--

CREATE UNIQUE INDEX uq_system_map_revisions_one_published ON public.system_map_revisions USING btree (system_map_id) WHERE ((status)::text = 'published'::text);


--

-- Name: uq_system_maps_org_default_live; Type: INDEX; Schema: public; Owner: -
--

CREATE UNIQUE INDEX uq_system_maps_org_default_live ON public.system_maps USING btree (organisation_id) WHERE (default_map AND (deleted_at IS NULL));


--

-- Name: uq_system_maps_org_name_live; Type: INDEX; Schema: public; Owner: -
--

CREATE UNIQUE INDEX uq_system_maps_org_name_live ON public.system_maps USING btree (organisation_id, name) WHERE (deleted_at IS NULL);


--

-- Name: uq_teams_organisation_name; Type: INDEX; Schema: public; Owner: -
--

CREATE UNIQUE INDEX uq_teams_organisation_name ON public.teams USING btree (organisation_id, name) WHERE (deleted_at IS NULL);


--

-- Name: uq_triggers_org_pipeline_name; Type: INDEX; Schema: public; Owner: -
--

CREATE UNIQUE INDEX uq_triggers_org_pipeline_name ON public.triggers USING btree (organisation_id, pipeline_id, name) WHERE ((deleted_at IS NULL) AND (name IS NOT NULL));


--

-- Name: uq_variant_groups_org_name; Type: INDEX; Schema: public; Owner: -
--

CREATE UNIQUE INDEX uq_variant_groups_org_name ON public.variant_groups USING btree (organisation_id, name) WHERE (deleted_at IS NULL);


--

-- Name: agent_runner_bindings agent_runner_bindings_account_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.agent_runner_bindings
    ADD CONSTRAINT agent_runner_bindings_account_id_fkey FOREIGN KEY (account_id) REFERENCES public.accounts(id) ON DELETE RESTRICT;


--

-- Name: agent_runner_bindings agent_runner_bindings_agent_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.agent_runner_bindings
    ADD CONSTRAINT agent_runner_bindings_agent_id_fkey FOREIGN KEY (agent_id) REFERENCES public.agents(id) ON DELETE CASCADE;


--

-- Name: agent_runner_bindings agent_runner_bindings_model_backend_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.agent_runner_bindings
    ADD CONSTRAINT agent_runner_bindings_model_backend_id_fkey FOREIGN KEY (model_backend_id) REFERENCES public.model_backends(id) ON DELETE RESTRICT;


--

-- Name: agent_runner_bindings agent_runner_bindings_organisation_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.agent_runner_bindings
    ADD CONSTRAINT agent_runner_bindings_organisation_id_fkey FOREIGN KEY (organisation_id) REFERENCES public.organisations(id) ON DELETE CASCADE;


--

-- Name: agents agents_account_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.agents
    ADD CONSTRAINT agents_account_id_fkey FOREIGN KEY (account_id) REFERENCES public.accounts(id) ON DELETE RESTRICT;


--

-- Name: agents agents_library_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.agents
    ADD CONSTRAINT agents_library_id_fkey FOREIGN KEY (library_id) REFERENCES public.library_primitives(id) ON DELETE SET NULL;


--

-- Name: agents agents_model_backend_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.agents
    ADD CONSTRAINT agents_model_backend_id_fkey FOREIGN KEY (model_backend_id) REFERENCES public.model_backends(id) ON DELETE RESTRICT;


--

-- Name: agents agents_organisation_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.agents
    ADD CONSTRAINT agents_organisation_id_fkey FOREIGN KEY (organisation_id) REFERENCES public.organisations(id) ON DELETE CASCADE;


--

-- Name: assistant_context_sources assistant_context_sources_account_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.assistant_context_sources
    ADD CONSTRAINT assistant_context_sources_account_id_fkey FOREIGN KEY (account_id) REFERENCES public.accounts(id) ON DELETE CASCADE;


--

-- Name: assistant_context_sources assistant_context_sources_organisation_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.assistant_context_sources
    ADD CONSTRAINT assistant_context_sources_organisation_id_fkey FOREIGN KEY (organisation_id) REFERENCES public.organisations(id) ON DELETE CASCADE;


--

-- Name: assistant_skills assistant_skills_account_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.assistant_skills
    ADD CONSTRAINT assistant_skills_account_id_fkey FOREIGN KEY (account_id) REFERENCES public.accounts(id) ON DELETE CASCADE;


--

-- Name: assistant_skills assistant_skills_organisation_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.assistant_skills
    ADD CONSTRAINT assistant_skills_organisation_id_fkey FOREIGN KEY (organisation_id) REFERENCES public.organisations(id) ON DELETE CASCADE;


--

-- Name: audit_chain_heads audit_chain_heads_last_event_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.audit_chain_heads
    ADD CONSTRAINT audit_chain_heads_last_event_id_fkey FOREIGN KEY (last_event_id) REFERENCES public.audit_events(id) ON DELETE SET NULL;


--

-- Name: audit_chain_heads audit_chain_heads_organisation_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.audit_chain_heads
    ADD CONSTRAINT audit_chain_heads_organisation_id_fkey FOREIGN KEY (organisation_id) REFERENCES public.organisations(id) ON DELETE CASCADE;


--

-- Name: audit_events audit_events_account_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.audit_events
    ADD CONSTRAINT audit_events_account_id_fkey FOREIGN KEY (account_id) REFERENCES public.accounts(id) ON DELETE SET NULL;


--

-- Name: audit_events audit_events_organisation_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.audit_events
    ADD CONSTRAINT audit_events_organisation_id_fkey FOREIGN KEY (organisation_id) REFERENCES public.organisations(id) ON DELETE CASCADE;


--

-- Name: chat_messages chat_messages_organisation_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.chat_messages
    ADD CONSTRAINT chat_messages_organisation_id_fkey FOREIGN KEY (organisation_id) REFERENCES public.organisations(id) ON DELETE CASCADE;


--

-- Name: chat_messages chat_messages_parent_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.chat_messages
    ADD CONSTRAINT chat_messages_parent_id_fkey FOREIGN KEY (parent_id) REFERENCES public.chat_messages(id) ON DELETE SET NULL;


--

-- Name: chat_messages chat_messages_session_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.chat_messages
    ADD CONSTRAINT chat_messages_session_id_fkey FOREIGN KEY (session_id) REFERENCES public.chat_sessions(id) ON DELETE CASCADE;


--

-- Name: chat_sessions chat_sessions_account_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.chat_sessions
    ADD CONSTRAINT chat_sessions_account_id_fkey FOREIGN KEY (account_id) REFERENCES public.accounts(id) ON DELETE CASCADE;


--

-- Name: chat_sessions chat_sessions_organisation_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.chat_sessions
    ADD CONSTRAINT chat_sessions_organisation_id_fkey FOREIGN KEY (organisation_id) REFERENCES public.organisations(id) ON DELETE CASCADE;


--

-- Name: composite_templates composite_templates_account_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.composite_templates
    ADD CONSTRAINT composite_templates_account_id_fkey FOREIGN KEY (account_id) REFERENCES public.accounts(id) ON DELETE RESTRICT;


--

-- Name: composite_templates composite_templates_organisation_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.composite_templates
    ADD CONSTRAINT composite_templates_organisation_id_fkey FOREIGN KEY (organisation_id) REFERENCES public.organisations(id) ON DELETE CASCADE;


--

-- Name: composite_templates composite_templates_parameter_schema_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.composite_templates
    ADD CONSTRAINT composite_templates_parameter_schema_id_fkey FOREIGN KEY (parameter_schema_id) REFERENCES public.parameter_schemas(id) ON DELETE RESTRICT;


--

-- Name: connector_instances connector_instances_account_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.connector_instances
    ADD CONSTRAINT connector_instances_account_id_fkey FOREIGN KEY (account_id) REFERENCES public.accounts(id) ON DELETE RESTRICT;


--

-- Name: connector_instances connector_instances_organisation_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.connector_instances
    ADD CONSTRAINT connector_instances_organisation_id_fkey FOREIGN KEY (organisation_id) REFERENCES public.organisations(id) ON DELETE CASCADE;


--

-- Name: connector_instances connector_instances_owner_team_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.connector_instances
    ADD CONSTRAINT connector_instances_owner_team_id_fkey FOREIGN KEY (owner_team_id) REFERENCES public.teams(id) ON DELETE RESTRICT;


--

-- Name: connector_profiles connector_profiles_connector_instance_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.connector_profiles
    ADD CONSTRAINT connector_profiles_connector_instance_id_fkey FOREIGN KEY (connector_instance_id) REFERENCES public.connector_instances(id) ON DELETE CASCADE;


--

-- Name: connector_profiles connector_profiles_organisation_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.connector_profiles
    ADD CONSTRAINT connector_profiles_organisation_id_fkey FOREIGN KEY (organisation_id) REFERENCES public.organisations(id) ON DELETE CASCADE;


--

-- Name: cost_components cost_components_organisation_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.cost_components
    ADD CONSTRAINT cost_components_organisation_id_fkey FOREIGN KEY (organisation_id) REFERENCES public.organisations(id) ON DELETE CASCADE;


--

-- Name: deleted_defaults deleted_defaults_organisation_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.deleted_defaults
    ADD CONSTRAINT deleted_defaults_organisation_id_fkey FOREIGN KEY (organisation_id) REFERENCES public.organisations(id) ON DELETE CASCADE;


--

-- Name: dismissals dismissals_dismissed_by_user_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.dismissals
    ADD CONSTRAINT dismissals_dismissed_by_user_id_fkey FOREIGN KEY (dismissed_by_user_id) REFERENCES public.accounts(id) ON DELETE CASCADE;


--

-- Name: dismissals dismissals_notification_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.dismissals
    ADD CONSTRAINT dismissals_notification_id_fkey FOREIGN KEY (notification_id) REFERENCES public.notifications(id) ON DELETE CASCADE;


--

-- Name: dismissals dismissals_organisation_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.dismissals
    ADD CONSTRAINT dismissals_organisation_id_fkey FOREIGN KEY (organisation_id) REFERENCES public.organisations(id) ON DELETE CASCADE;


--

-- Name: environment_profiles environment_profiles_account_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.environment_profiles
    ADD CONSTRAINT environment_profiles_account_id_fkey FOREIGN KEY (account_id) REFERENCES public.accounts(id) ON DELETE RESTRICT;


--

-- Name: environment_profiles environment_profiles_organisation_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.environment_profiles
    ADD CONSTRAINT environment_profiles_organisation_id_fkey FOREIGN KEY (organisation_id) REFERENCES public.organisations(id) ON DELETE CASCADE;


--

-- Name: environment_profiles environment_profiles_owner_team_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.environment_profiles
    ADD CONSTRAINT environment_profiles_owner_team_id_fkey FOREIGN KEY (owner_team_id) REFERENCES public.teams(id) ON DELETE RESTRICT;


--

-- Name: error_events error_events_organisation_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.error_events
    ADD CONSTRAINT error_events_organisation_id_fkey FOREIGN KEY (organisation_id) REFERENCES public.organisations(id) ON DELETE CASCADE;


--

-- Name: error_forwarder_configs error_forwarder_configs_organisation_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.error_forwarder_configs
    ADD CONSTRAINT error_forwarder_configs_organisation_id_fkey FOREIGN KEY (organisation_id) REFERENCES public.organisations(id) ON DELETE CASCADE;


--

-- Name: error_groups error_groups_assigned_to_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.error_groups
    ADD CONSTRAINT error_groups_assigned_to_fkey FOREIGN KEY (assigned_to) REFERENCES public.accounts(id) ON DELETE SET NULL;


--

-- Name: error_groups error_groups_organisation_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.error_groups
    ADD CONSTRAINT error_groups_organisation_id_fkey FOREIGN KEY (organisation_id) REFERENCES public.organisations(id) ON DELETE CASCADE;


--

-- Name: error_groups error_groups_sample_event_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.error_groups
    ADD CONSTRAINT error_groups_sample_event_id_fkey FOREIGN KEY (sample_event_id) REFERENCES public.error_events(id) ON DELETE SET NULL;


--

-- Name: error_notification_rules error_notification_rules_organisation_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.error_notification_rules
    ADD CONSTRAINT error_notification_rules_organisation_id_fkey FOREIGN KEY (organisation_id) REFERENCES public.organisations(id) ON DELETE CASCADE;


--

-- Name: eval_cases eval_cases_dataset_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.eval_cases
    ADD CONSTRAINT eval_cases_dataset_id_fkey FOREIGN KEY (dataset_id) REFERENCES public.eval_datasets(id) ON DELETE RESTRICT;


--

-- Name: eval_cases eval_cases_organisation_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.eval_cases
    ADD CONSTRAINT eval_cases_organisation_id_fkey FOREIGN KEY (organisation_id) REFERENCES public.organisations(id) ON DELETE CASCADE;


--

-- Name: eval_datasets eval_datasets_organisation_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.eval_datasets
    ADD CONSTRAINT eval_datasets_organisation_id_fkey FOREIGN KEY (organisation_id) REFERENCES public.organisations(id) ON DELETE CASCADE;


--

-- Name: eval_datasets eval_datasets_owner_team_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.eval_datasets
    ADD CONSTRAINT eval_datasets_owner_team_id_fkey FOREIGN KEY (owner_team_id) REFERENCES public.teams(id) ON DELETE SET NULL;


--

-- Name: eval_results eval_results_organisation_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.eval_results
    ADD CONSTRAINT eval_results_organisation_id_fkey FOREIGN KEY (organisation_id) REFERENCES public.organisations(id) ON DELETE CASCADE;


--

-- Name: eval_results eval_results_resolved_by_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.eval_results
    ADD CONSTRAINT eval_results_resolved_by_fkey FOREIGN KEY (resolved_by) REFERENCES public.accounts(id) ON DELETE SET NULL;


--

-- Name: eval_results eval_results_run_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.eval_results
    ADD CONSTRAINT eval_results_run_id_fkey FOREIGN KEY (run_id) REFERENCES public.runs(id) ON DELETE CASCADE;


--

-- Name: eval_suites eval_suites_organisation_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.eval_suites
    ADD CONSTRAINT eval_suites_organisation_id_fkey FOREIGN KEY (organisation_id) REFERENCES public.organisations(id) ON DELETE CASCADE;


--

-- Name: eval_suites eval_suites_owner_team_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.eval_suites
    ADD CONSTRAINT eval_suites_owner_team_id_fkey FOREIGN KEY (owner_team_id) REFERENCES public.teams(id) ON DELETE RESTRICT;


--

-- Name: evals evals_account_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.evals
    ADD CONSTRAINT evals_account_id_fkey FOREIGN KEY (account_id) REFERENCES public.accounts(id) ON DELETE RESTRICT;


--

-- Name: evals evals_eval_suite_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.evals
    ADD CONSTRAINT evals_eval_suite_id_fkey FOREIGN KEY (eval_suite_id) REFERENCES public.eval_suites(id) ON DELETE SET NULL;


--

-- Name: evals evals_muted_by_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.evals
    ADD CONSTRAINT evals_muted_by_fkey FOREIGN KEY (muted_by) REFERENCES public.accounts(id) ON DELETE SET NULL;


--

-- Name: evals evals_organisation_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.evals
    ADD CONSTRAINT evals_organisation_id_fkey FOREIGN KEY (organisation_id) REFERENCES public.organisations(id) ON DELETE CASCADE;


--

-- Name: evals evals_pipeline_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.evals
    ADD CONSTRAINT evals_pipeline_id_fkey FOREIGN KEY (pipeline_id) REFERENCES public.pipelines(id) ON DELETE CASCADE;


--

-- Name: evidence evidence_organisation_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.evidence
    ADD CONSTRAINT evidence_organisation_id_fkey FOREIGN KEY (organisation_id) REFERENCES public.organisations(id) ON DELETE CASCADE;


--

-- Name: evidence_redaction_rules evidence_redaction_rules_organisation_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.evidence_redaction_rules
    ADD CONSTRAINT evidence_redaction_rules_organisation_id_fkey FOREIGN KEY (organisation_id) REFERENCES public.organisations(id) ON DELETE CASCADE;


--

-- Name: external_agents external_agents_organisation_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.external_agents
    ADD CONSTRAINT external_agents_organisation_id_fkey FOREIGN KEY (organisation_id) REFERENCES public.organisations(id) ON DELETE CASCADE;


--

-- Name: external_agents external_agents_owner_team_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.external_agents
    ADD CONSTRAINT external_agents_owner_team_id_fkey FOREIGN KEY (owner_team_id) REFERENCES public.teams(id) ON DELETE SET NULL;


--

-- Name: feature_flag_catalog feature_flag_catalog_tier_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.feature_flag_catalog
    ADD CONSTRAINT feature_flag_catalog_tier_id_fkey FOREIGN KEY (tier_id) REFERENCES public.tier_catalog(tier_id);


--

-- Name: feedback_records feedback_records_account_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.feedback_records
    ADD CONSTRAINT feedback_records_account_id_fkey FOREIGN KEY (account_id) REFERENCES public.accounts(id) ON DELETE RESTRICT;


--

-- Name: feedback_records feedback_records_correction_run_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.feedback_records
    ADD CONSTRAINT feedback_records_correction_run_id_fkey FOREIGN KEY (correction_run_id) REFERENCES public.runs(id) ON DELETE SET NULL;


--

-- Name: feedback_records feedback_records_organisation_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.feedback_records
    ADD CONSTRAINT feedback_records_organisation_id_fkey FOREIGN KEY (organisation_id) REFERENCES public.organisations(id) ON DELETE CASCADE;


--

-- Name: feedback_records feedback_records_producing_agent_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.feedback_records
    ADD CONSTRAINT feedback_records_producing_agent_id_fkey FOREIGN KEY (producing_agent_id) REFERENCES public.agents(id) ON DELETE SET NULL;


--

-- Name: feedback_records feedback_records_run_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.feedback_records
    ADD CONSTRAINT feedback_records_run_id_fkey FOREIGN KEY (run_id) REFERENCES public.runs(id) ON DELETE CASCADE;


--

-- Name: agents fk_agents_collection_install_id; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.agents
    ADD CONSTRAINT fk_agents_collection_install_id FOREIGN KEY (collection_install_id) REFERENCES public.collection_install(install_id) ON DELETE SET NULL;


--

-- Name: agents fk_agents_created_by; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.agents
    ADD CONSTRAINT fk_agents_created_by FOREIGN KEY (created_by) REFERENCES public.accounts(id) ON DELETE SET NULL;


--

-- Name: agents fk_agents_deleted_by; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.agents
    ADD CONSTRAINT fk_agents_deleted_by FOREIGN KEY (deleted_by) REFERENCES public.accounts(id) ON DELETE SET NULL;


--

-- Name: agents fk_agents_input_schema_version; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.agents
    ADD CONSTRAINT fk_agents_input_schema_version FOREIGN KEY (input_schema_id, input_schema_version, organisation_id) REFERENCES public.schema_versions(schema_id, version, organisation_id) ON DELETE RESTRICT;


--

-- Name: agents fk_agents_output_schema_version; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.agents
    ADD CONSTRAINT fk_agents_output_schema_version FOREIGN KEY (output_schema_id, output_schema_version, organisation_id) REFERENCES public.schema_versions(schema_id, version, organisation_id) ON DELETE RESTRICT;


--

-- Name: agents fk_agents_parameter_schema_id; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.agents
    ADD CONSTRAINT fk_agents_parameter_schema_id FOREIGN KEY (parameter_schema_id) REFERENCES public.parameter_schemas(id) ON DELETE RESTRICT;


--

-- Name: agents fk_agents_template_id; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.agents
    ADD CONSTRAINT fk_agents_template_id FOREIGN KEY (template_id) REFERENCES public.composite_templates(id) ON DELETE SET NULL;


--

-- Name: agents fk_agents_updated_by; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.agents
    ADD CONSTRAINT fk_agents_updated_by FOREIGN KEY (updated_by) REFERENCES public.accounts(id) ON DELETE SET NULL;


--

-- Name: collection_install fk_collection_install_collection_id; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.collection_install
    ADD CONSTRAINT fk_collection_install_collection_id FOREIGN KEY (collection_id) REFERENCES public.library_primitives(id) ON DELETE RESTRICT;


--

-- Name: collection_install fk_collection_install_created_by; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.collection_install
    ADD CONSTRAINT fk_collection_install_created_by FOREIGN KEY (created_by) REFERENCES public.accounts(id) ON DELETE SET NULL;


--

-- Name: collection_install_entity fk_collection_install_entity_install_id; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.collection_install_entity
    ADD CONSTRAINT fk_collection_install_entity_install_id FOREIGN KEY (install_id) REFERENCES public.collection_install(install_id) ON DELETE CASCADE;


--

-- Name: collection_install fk_collection_install_organisation_id; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.collection_install
    ADD CONSTRAINT fk_collection_install_organisation_id FOREIGN KEY (organisation_id) REFERENCES public.organisations(id) ON DELETE CASCADE;


--

-- Name: composite_templates fk_composite_templates_input_schema_id; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.composite_templates
    ADD CONSTRAINT fk_composite_templates_input_schema_id FOREIGN KEY (input_schema_id) REFERENCES public.schemas(id) ON DELETE SET NULL;


--

-- Name: composite_templates fk_composite_templates_output_schema_id; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.composite_templates
    ADD CONSTRAINT fk_composite_templates_output_schema_id FOREIGN KEY (output_schema_id) REFERENCES public.schemas(id) ON DELETE SET NULL;


--

-- Name: connector_instances fk_connector_instances_created_by; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.connector_instances
    ADD CONSTRAINT fk_connector_instances_created_by FOREIGN KEY (created_by) REFERENCES public.accounts(id) ON DELETE SET NULL;


--

-- Name: connector_instances fk_connector_instances_deleted_by; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.connector_instances
    ADD CONSTRAINT fk_connector_instances_deleted_by FOREIGN KEY (deleted_by) REFERENCES public.accounts(id) ON DELETE SET NULL;


--

-- Name: connector_instances fk_connector_instances_updated_by; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.connector_instances
    ADD CONSTRAINT fk_connector_instances_updated_by FOREIGN KEY (updated_by) REFERENCES public.accounts(id) ON DELETE SET NULL;


--

-- Name: eval_results fk_eval_results_eval_org; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.eval_results
    ADD CONSTRAINT fk_eval_results_eval_org FOREIGN KEY (eval_id, organisation_id) REFERENCES public.evals(id, organisation_id) ON DELETE CASCADE;


--

-- Name: eval_results fk_eval_results_suite_run_id; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.eval_results
    ADD CONSTRAINT fk_eval_results_suite_run_id FOREIGN KEY (suite_run_id) REFERENCES public.suite_runs(id) ON DELETE CASCADE;


--

-- Name: hitl_claims fk_hitl_claims_decided_by_accounts; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.hitl_claims
    ADD CONSTRAINT fk_hitl_claims_decided_by_accounts FOREIGN KEY (decided_by) REFERENCES public.accounts(id) ON DELETE SET NULL;


--

-- Name: mcp_setup_tokens fk_mcp_setup_tokens_created_by; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.mcp_setup_tokens
    ADD CONSTRAINT fk_mcp_setup_tokens_created_by FOREIGN KEY (created_by) REFERENCES public.accounts(id) ON DELETE RESTRICT;


--

-- Name: oauth_authorization_codes fk_oauth_authorization_codes_account_id; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.oauth_authorization_codes
    ADD CONSTRAINT fk_oauth_authorization_codes_account_id FOREIGN KEY (account_id) REFERENCES public.accounts(id) ON DELETE CASCADE;


--

-- Name: oauth_clients fk_oauth_clients_team_id; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.oauth_clients
    ADD CONSTRAINT fk_oauth_clients_team_id FOREIGN KEY (team_id) REFERENCES public.teams(id) ON DELETE SET NULL;


--

-- Name: pipelines fk_pipelines_business_owner_id; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.pipelines
    ADD CONSTRAINT fk_pipelines_business_owner_id FOREIGN KEY (business_owner_id) REFERENCES public.accounts(id) ON DELETE SET NULL;


--

-- Name: pipelines fk_pipelines_environment_profile_id; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.pipelines
    ADD CONSTRAINT fk_pipelines_environment_profile_id FOREIGN KEY (environment_profile_id) REFERENCES public.environment_profiles(id) ON DELETE SET NULL;


--

-- Name: pipelines fk_pipelines_folder_id; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.pipelines
    ADD CONSTRAINT fk_pipelines_folder_id FOREIGN KEY (folder_id) REFERENCES public.pipeline_folders(id) ON DELETE SET NULL;


--

-- Name: pipelines fk_pipelines_reliability_owner_id; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.pipelines
    ADD CONSTRAINT fk_pipelines_reliability_owner_id FOREIGN KEY (reliability_owner_id) REFERENCES public.accounts(id) ON DELETE SET NULL;


--

-- Name: policy_gate_decisions fk_policy_gate_decisions_eval_org; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.policy_gate_decisions
    ADD CONSTRAINT fk_policy_gate_decisions_eval_org FOREIGN KEY (eval_id, organisation_id) REFERENCES public.evals(id, organisation_id) ON DELETE RESTRICT;


--

-- Name: policy_gate_decisions fk_policy_gate_decisions_gate_org; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.policy_gate_decisions
    ADD CONSTRAINT fk_policy_gate_decisions_gate_org FOREIGN KEY (policy_gate_id, organisation_id) REFERENCES public.policy_gates(id, organisation_id) ON DELETE RESTRICT;


--

-- Name: policy_gates fk_policy_gates_eval_org; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.policy_gates
    ADD CONSTRAINT fk_policy_gates_eval_org FOREIGN KEY (eval_id, organisation_id) REFERENCES public.evals(id, organisation_id) ON DELETE CASCADE;


--

-- Name: run_evidence fk_run_evidence_organisation_id; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.run_evidence
    ADD CONSTRAINT fk_run_evidence_organisation_id FOREIGN KEY (organisation_id) REFERENCES public.organisations(id) ON DELETE CASCADE;


--

-- Name: scheduled_reports fk_scheduled_reports_deleted_by; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.scheduled_reports
    ADD CONSTRAINT fk_scheduled_reports_deleted_by FOREIGN KEY (deleted_by) REFERENCES public.accounts(id) ON DELETE SET NULL;


--

-- Name: scheduled_reports fk_scheduled_reports_updated_by; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.scheduled_reports
    ADD CONSTRAINT fk_scheduled_reports_updated_by FOREIGN KEY (updated_by) REFERENCES public.accounts(id) ON DELETE SET NULL;


--

-- Name: schemas fk_schemas_folder_id; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.schemas
    ADD CONSTRAINT fk_schemas_folder_id FOREIGN KEY (folder_id) REFERENCES public.schema_folders(id) ON DELETE SET NULL;


--

-- Name: system_config fk_system_config_updated_by; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.system_config
    ADD CONSTRAINT fk_system_config_updated_by FOREIGN KEY (updated_by) REFERENCES public.accounts(id) ON DELETE SET NULL;


--

-- Name: system_map_attribution_bindings fk_system_map_attribution_bindings_deleted_by; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.system_map_attribution_bindings
    ADD CONSTRAINT fk_system_map_attribution_bindings_deleted_by FOREIGN KEY (deleted_by) REFERENCES public.accounts(id) ON DELETE SET NULL;


--

-- Name: system_map_attribution_bindings fk_system_map_attribution_bindings_map; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.system_map_attribution_bindings
    ADD CONSTRAINT fk_system_map_attribution_bindings_map FOREIGN KEY (system_map_id, organisation_id) REFERENCES public.system_maps(id, organisation_id) ON DELETE CASCADE;


--

-- Name: system_map_loop_admission_attempts fk_system_map_loop_admission_attempts_config; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.system_map_loop_admission_attempts
    ADD CONSTRAINT fk_system_map_loop_admission_attempts_config FOREIGN KEY (loop_config_id, organisation_id) REFERENCES public.system_map_loop_configs(id, organisation_id) ON DELETE CASCADE;


--

-- Name: system_map_loop_admission_attempts fk_system_map_loop_admission_attempts_deleted_by; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.system_map_loop_admission_attempts
    ADD CONSTRAINT fk_system_map_loop_admission_attempts_deleted_by FOREIGN KEY (deleted_by) REFERENCES public.accounts(id) ON DELETE SET NULL;


--

-- Name: system_map_loop_configs fk_system_map_loop_configs_deleted_by; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.system_map_loop_configs
    ADD CONSTRAINT fk_system_map_loop_configs_deleted_by FOREIGN KEY (deleted_by) REFERENCES public.accounts(id) ON DELETE SET NULL;


--

-- Name: system_map_loop_configs fk_system_map_loop_configs_revision; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.system_map_loop_configs
    ADD CONSTRAINT fk_system_map_loop_configs_revision FOREIGN KEY (revision_id, organisation_id) REFERENCES public.system_map_revisions(id, organisation_id) ON DELETE CASCADE;


--

-- Name: system_map_loop_reservations fk_system_map_loop_reservations_config; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.system_map_loop_reservations
    ADD CONSTRAINT fk_system_map_loop_reservations_config FOREIGN KEY (loop_config_id, organisation_id) REFERENCES public.system_map_loop_configs(id, organisation_id) ON DELETE CASCADE;


--

-- Name: system_map_loop_reservations fk_system_map_loop_reservations_deleted_by; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.system_map_loop_reservations
    ADD CONSTRAINT fk_system_map_loop_reservations_deleted_by FOREIGN KEY (deleted_by) REFERENCES public.accounts(id) ON DELETE SET NULL;


--

-- Name: system_map_nodes fk_system_map_nodes_deleted_by; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.system_map_nodes
    ADD CONSTRAINT fk_system_map_nodes_deleted_by FOREIGN KEY (deleted_by) REFERENCES public.accounts(id) ON DELETE SET NULL;


--

-- Name: system_map_nodes fk_system_map_nodes_revision; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.system_map_nodes
    ADD CONSTRAINT fk_system_map_nodes_revision FOREIGN KEY (revision_id, organisation_id) REFERENCES public.system_map_revisions(id, organisation_id) ON DELETE CASCADE;


--

-- Name: system_map_relationships fk_system_map_relationships_deleted_by; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.system_map_relationships
    ADD CONSTRAINT fk_system_map_relationships_deleted_by FOREIGN KEY (deleted_by) REFERENCES public.accounts(id) ON DELETE SET NULL;


--

-- Name: system_map_relationships fk_system_map_relationships_revision; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.system_map_relationships
    ADD CONSTRAINT fk_system_map_relationships_revision FOREIGN KEY (revision_id, organisation_id) REFERENCES public.system_map_revisions(id, organisation_id) ON DELETE CASCADE;


--

-- Name: system_map_revisions fk_system_map_revisions_deleted_by; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.system_map_revisions
    ADD CONSTRAINT fk_system_map_revisions_deleted_by FOREIGN KEY (deleted_by) REFERENCES public.accounts(id) ON DELETE SET NULL;


--

-- Name: system_map_revisions fk_system_map_revisions_map_org; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.system_map_revisions
    ADD CONSTRAINT fk_system_map_revisions_map_org FOREIGN KEY (system_map_id, organisation_id) REFERENCES public.system_maps(id, organisation_id) ON DELETE CASCADE;


--

-- Name: system_maps fk_system_maps_published_revision; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.system_maps
    ADD CONSTRAINT fk_system_maps_published_revision FOREIGN KEY (published_revision_id, organisation_id) REFERENCES public.system_map_revisions(id, organisation_id) ON DELETE SET NULL;


--

-- Name: trigger_event_findings fk_trigger_event_findings_eval_org; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.trigger_event_findings
    ADD CONSTRAINT fk_trigger_event_findings_eval_org FOREIGN KEY (eval_id, organisation_id) REFERENCES public.evals(id, organisation_id) ON DELETE CASCADE;


--

-- Name: trigger_event_findings fk_trigger_event_findings_event_org; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.trigger_event_findings
    ADD CONSTRAINT fk_trigger_event_findings_event_org FOREIGN KEY (trigger_event_id, organisation_id) REFERENCES public.trigger_events(id, organisation_id) ON DELETE CASCADE;


--

-- Name: triggers fk_triggers_eval_suite_id; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.triggers
    ADD CONSTRAINT fk_triggers_eval_suite_id FOREIGN KEY (eval_suite_id) REFERENCES public.eval_suites(id) ON DELETE RESTRICT;


--

-- Name: variant_batch_state fk_variant_batch_state_organisation_id; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.variant_batch_state
    ADD CONSTRAINT fk_variant_batch_state_organisation_id FOREIGN KEY (organisation_id) REFERENCES public.organisations(id) ON DELETE CASCADE;


--

-- Name: hitl_claims hitl_claims_account_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.hitl_claims
    ADD CONSTRAINT hitl_claims_account_id_fkey FOREIGN KEY (account_id) REFERENCES public.accounts(id) ON DELETE SET NULL;


--

-- Name: hitl_claims hitl_claims_organisation_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.hitl_claims
    ADD CONSTRAINT hitl_claims_organisation_id_fkey FOREIGN KEY (organisation_id) REFERENCES public.organisations(id) ON DELETE CASCADE;


--

-- Name: hitl_claims hitl_claims_pipeline_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.hitl_claims
    ADD CONSTRAINT hitl_claims_pipeline_id_fkey FOREIGN KEY (pipeline_id) REFERENCES public.pipelines(id) ON DELETE CASCADE;


--

-- Name: hitl_claims hitl_claims_required_team_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.hitl_claims
    ADD CONSTRAINT hitl_claims_required_team_id_fkey FOREIGN KEY (required_team_id) REFERENCES public.teams(id) ON DELETE RESTRICT;


--

-- Name: hitl_claims hitl_claims_run_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.hitl_claims
    ADD CONSTRAINT hitl_claims_run_id_fkey FOREIGN KEY (run_id) REFERENCES public.runs(id) ON DELETE CASCADE;


--

-- Name: invitations invitations_invited_by_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.invitations
    ADD CONSTRAINT invitations_invited_by_fkey FOREIGN KEY (invited_by) REFERENCES public.accounts(id) ON DELETE RESTRICT;


--

-- Name: invitations invitations_organisation_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.invitations
    ADD CONSTRAINT invitations_organisation_id_fkey FOREIGN KEY (organisation_id) REFERENCES public.organisations(id) ON DELETE CASCADE;


--

-- Name: journeys journeys_map_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.journeys
    ADD CONSTRAINT journeys_map_id_fkey FOREIGN KEY (map_id) REFERENCES public.lifecycle_maps(id) ON DELETE SET NULL;


--

-- Name: journeys journeys_organisation_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.journeys
    ADD CONSTRAINT journeys_organisation_id_fkey FOREIGN KEY (organisation_id) REFERENCES public.organisations(id) ON DELETE CASCADE;


--

-- Name: journeys journeys_owner_team_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.journeys
    ADD CONSTRAINT journeys_owner_team_id_fkey FOREIGN KEY (owner_team_id) REFERENCES public.teams(id) ON DELETE RESTRICT;


--

-- Name: library_primitives library_primitives_account_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.library_primitives
    ADD CONSTRAINT library_primitives_account_id_fkey FOREIGN KEY (account_id) REFERENCES public.accounts(id) ON DELETE SET NULL;


--

-- Name: library_primitives library_primitives_forked_from_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.library_primitives
    ADD CONSTRAINT library_primitives_forked_from_fkey FOREIGN KEY (forked_from) REFERENCES public.library_primitives(id) ON DELETE RESTRICT;


--

-- Name: library_primitives library_primitives_organisation_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.library_primitives
    ADD CONSTRAINT library_primitives_organisation_id_fkey FOREIGN KEY (organisation_id) REFERENCES public.organisations(id) ON DELETE CASCADE;


--

-- Name: library_primitives library_primitives_owner_team_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.library_primitives
    ADD CONSTRAINT library_primitives_owner_team_id_fkey FOREIGN KEY (owner_team_id) REFERENCES public.teams(id) ON DELETE RESTRICT;


--

-- Name: library_primitives library_primitives_update_available_version_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.library_primitives
    ADD CONSTRAINT library_primitives_update_available_version_id_fkey FOREIGN KEY (update_available_version_id) REFERENCES public.library_primitives(id) ON DELETE SET NULL;


--

-- Name: library_primitives library_primitives_version_group_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.library_primitives
    ADD CONSTRAINT library_primitives_version_group_id_fkey FOREIGN KEY (version_group_id) REFERENCES public.library_primitives(id) ON DELETE SET NULL;


--

-- Name: lifecycle_map_stages lifecycle_map_stages_account_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.lifecycle_map_stages
    ADD CONSTRAINT lifecycle_map_stages_account_id_fkey FOREIGN KEY (account_id) REFERENCES public.accounts(id) ON DELETE RESTRICT;


--

-- Name: lifecycle_map_stages lifecycle_map_stages_map_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.lifecycle_map_stages
    ADD CONSTRAINT lifecycle_map_stages_map_id_fkey FOREIGN KEY (map_id) REFERENCES public.lifecycle_maps(id) ON DELETE CASCADE;


--

-- Name: lifecycle_map_stages lifecycle_map_stages_organisation_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.lifecycle_map_stages
    ADD CONSTRAINT lifecycle_map_stages_organisation_id_fkey FOREIGN KEY (organisation_id) REFERENCES public.organisations(id) ON DELETE CASCADE;


--

-- Name: lifecycle_map_stages lifecycle_map_stages_pipeline_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.lifecycle_map_stages
    ADD CONSTRAINT lifecycle_map_stages_pipeline_id_fkey FOREIGN KEY (pipeline_id) REFERENCES public.pipelines(id) ON DELETE RESTRICT;


--

-- Name: lifecycle_maps lifecycle_maps_account_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.lifecycle_maps
    ADD CONSTRAINT lifecycle_maps_account_id_fkey FOREIGN KEY (account_id) REFERENCES public.accounts(id) ON DELETE RESTRICT;


--

-- Name: lifecycle_maps lifecycle_maps_organisation_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.lifecycle_maps
    ADD CONSTRAINT lifecycle_maps_organisation_id_fkey FOREIGN KEY (organisation_id) REFERENCES public.organisations(id) ON DELETE CASCADE;


--

-- Name: lifecycle_maps lifecycle_maps_owner_team_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.lifecycle_maps
    ADD CONSTRAINT lifecycle_maps_owner_team_id_fkey FOREIGN KEY (owner_team_id) REFERENCES public.teams(id) ON DELETE RESTRICT;


--

-- Name: lifecycle_maps lifecycle_maps_updated_by_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.lifecycle_maps
    ADD CONSTRAINT lifecycle_maps_updated_by_fkey FOREIGN KEY (updated_by) REFERENCES public.accounts(id) ON DELETE SET NULL;


--

-- Name: mcp_servers mcp_servers_approved_by_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.mcp_servers
    ADD CONSTRAINT mcp_servers_approved_by_fkey FOREIGN KEY (approved_by) REFERENCES public.accounts(id) ON DELETE SET NULL;


--

-- Name: mcp_servers mcp_servers_organisation_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.mcp_servers
    ADD CONSTRAINT mcp_servers_organisation_id_fkey FOREIGN KEY (organisation_id) REFERENCES public.organisations(id) ON DELETE CASCADE;


--

-- Name: mcp_servers mcp_servers_owner_account_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.mcp_servers
    ADD CONSTRAINT mcp_servers_owner_account_id_fkey FOREIGN KEY (owner_account_id) REFERENCES public.accounts(id) ON DELETE SET NULL;


--

-- Name: mcp_servers mcp_servers_owner_team_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.mcp_servers
    ADD CONSTRAINT mcp_servers_owner_team_id_fkey FOREIGN KEY (owner_team_id) REFERENCES public.teams(id) ON DELETE SET NULL;


--

-- Name: mcp_setup_tokens mcp_setup_tokens_organisation_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.mcp_setup_tokens
    ADD CONSTRAINT mcp_setup_tokens_organisation_id_fkey FOREIGN KEY (organisation_id) REFERENCES public.organisations(id) ON DELETE CASCADE;


--

-- Name: metrics_staging metrics_staging_organisation_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.metrics_staging
    ADD CONSTRAINT metrics_staging_organisation_id_fkey FOREIGN KEY (organisation_id) REFERENCES public.organisations(id) ON DELETE CASCADE;


--

-- Name: model_backends model_backends_account_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.model_backends
    ADD CONSTRAINT model_backends_account_id_fkey FOREIGN KEY (account_id) REFERENCES public.accounts(id) ON DELETE RESTRICT;


--

-- Name: model_backends model_backends_organisation_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.model_backends
    ADD CONSTRAINT model_backends_organisation_id_fkey FOREIGN KEY (organisation_id) REFERENCES public.organisations(id) ON DELETE CASCADE;


--

-- Name: model_backends model_backends_owner_team_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.model_backends
    ADD CONSTRAINT model_backends_owner_team_id_fkey FOREIGN KEY (owner_team_id) REFERENCES public.teams(id) ON DELETE RESTRICT;


--

-- Name: modulo_journey_facts modulo_journey_facts_organisation_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.modulo_journey_facts
    ADD CONSTRAINT modulo_journey_facts_organisation_id_fkey FOREIGN KEY (organisation_id) REFERENCES public.organisations(id) ON DELETE CASCADE;


--

-- Name: node_categories node_categories_account_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.node_categories
    ADD CONSTRAINT node_categories_account_id_fkey FOREIGN KEY (account_id) REFERENCES public.accounts(id) ON DELETE RESTRICT;


--

-- Name: node_categories node_categories_organisation_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.node_categories
    ADD CONSTRAINT node_categories_organisation_id_fkey FOREIGN KEY (organisation_id) REFERENCES public.organisations(id) ON DELETE CASCADE;


--

-- Name: node_observations node_observations_account_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.node_observations
    ADD CONSTRAINT node_observations_account_id_fkey FOREIGN KEY (account_id) REFERENCES public.accounts(id) ON DELETE SET NULL;


--

-- Name: node_observations node_observations_organisation_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.node_observations
    ADD CONSTRAINT node_observations_organisation_id_fkey FOREIGN KEY (organisation_id) REFERENCES public.organisations(id) ON DELETE CASCADE;


--

-- Name: node_observations node_observations_run_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.node_observations
    ADD CONSTRAINT node_observations_run_id_fkey FOREIGN KEY (run_id) REFERENCES public.runs(id) ON DELETE CASCADE;


--

-- Name: nodes nodes_account_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.nodes
    ADD CONSTRAINT nodes_account_id_fkey FOREIGN KEY (account_id) REFERENCES public.accounts(id) ON DELETE RESTRICT;


--

-- Name: nodes nodes_organisation_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.nodes
    ADD CONSTRAINT nodes_organisation_id_fkey FOREIGN KEY (organisation_id) REFERENCES public.organisations(id) ON DELETE CASCADE;


--

-- Name: nodes nodes_parent_node_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.nodes
    ADD CONSTRAINT nodes_parent_node_id_fkey FOREIGN KEY (parent_node_id) REFERENCES public.nodes(id) ON DELETE SET NULL;


--

-- Name: nodes nodes_pipeline_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.nodes
    ADD CONSTRAINT nodes_pipeline_id_fkey FOREIGN KEY (pipeline_id) REFERENCES public.pipelines(id) ON DELETE CASCADE;


--

-- Name: notification_delivery_log notification_delivery_log_endpoint_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.notification_delivery_log
    ADD CONSTRAINT notification_delivery_log_endpoint_id_fkey FOREIGN KEY (endpoint_id) REFERENCES public.notification_endpoints(id) ON DELETE SET NULL;


--

-- Name: notification_delivery_log notification_delivery_log_organisation_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.notification_delivery_log
    ADD CONSTRAINT notification_delivery_log_organisation_id_fkey FOREIGN KEY (organisation_id) REFERENCES public.organisations(id) ON DELETE CASCADE;


--

-- Name: notification_delivery_log notification_delivery_log_run_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.notification_delivery_log
    ADD CONSTRAINT notification_delivery_log_run_id_fkey FOREIGN KEY (run_id) REFERENCES public.runs(id) ON DELETE SET NULL;


--

-- Name: notification_endpoints notification_endpoints_account_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.notification_endpoints
    ADD CONSTRAINT notification_endpoints_account_id_fkey FOREIGN KEY (account_id) REFERENCES public.accounts(id) ON DELETE SET NULL;


--

-- Name: notification_endpoints notification_endpoints_organisation_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.notification_endpoints
    ADD CONSTRAINT notification_endpoints_organisation_id_fkey FOREIGN KEY (organisation_id) REFERENCES public.organisations(id) ON DELETE CASCADE;


--

-- Name: notification_endpoints notification_endpoints_team_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.notification_endpoints
    ADD CONSTRAINT notification_endpoints_team_id_fkey FOREIGN KEY (team_id) REFERENCES public.teams(id) ON DELETE CASCADE;


--

-- Name: notification_preferences notification_preferences_account_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.notification_preferences
    ADD CONSTRAINT notification_preferences_account_id_fkey FOREIGN KEY (account_id) REFERENCES public.accounts(id) ON DELETE CASCADE;


--

-- Name: notification_preferences notification_preferences_organisation_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.notification_preferences
    ADD CONSTRAINT notification_preferences_organisation_id_fkey FOREIGN KEY (organisation_id) REFERENCES public.organisations(id) ON DELETE CASCADE;


--

-- Name: notifications notifications_organisation_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.notifications
    ADD CONSTRAINT notifications_organisation_id_fkey FOREIGN KEY (organisation_id) REFERENCES public.organisations(id) ON DELETE CASCADE;


--

-- Name: notifications notifications_target_user_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.notifications
    ADD CONSTRAINT notifications_target_user_id_fkey FOREIGN KEY (target_user_id) REFERENCES public.accounts(id) ON DELETE SET NULL;


--

-- Name: oauth_authorization_codes oauth_authorization_codes_organisation_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.oauth_authorization_codes
    ADD CONSTRAINT oauth_authorization_codes_organisation_id_fkey FOREIGN KEY (organisation_id) REFERENCES public.organisations(id) ON DELETE CASCADE;


--

-- Name: oauth_clients oauth_clients_account_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.oauth_clients
    ADD CONSTRAINT oauth_clients_account_id_fkey FOREIGN KEY (account_id) REFERENCES public.accounts(id) ON DELETE SET NULL;


--

-- Name: oauth_clients oauth_clients_organisation_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.oauth_clients
    ADD CONSTRAINT oauth_clients_organisation_id_fkey FOREIGN KEY (organisation_id) REFERENCES public.organisations(id) ON DELETE CASCADE;


--

-- Name: oauth_consent_states oauth_consent_states_account_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.oauth_consent_states
    ADD CONSTRAINT oauth_consent_states_account_id_fkey FOREIGN KEY (account_id) REFERENCES public.accounts(id) ON DELETE SET NULL;


--

-- Name: oauth_consent_states oauth_consent_states_organisation_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.oauth_consent_states
    ADD CONSTRAINT oauth_consent_states_organisation_id_fkey FOREIGN KEY (organisation_id) REFERENCES public.organisations(id) ON DELETE CASCADE;


--

-- Name: oauth_token_families oauth_token_families_organisation_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.oauth_token_families
    ADD CONSTRAINT oauth_token_families_organisation_id_fkey FOREIGN KEY (organisation_id) REFERENCES public.organisations(id) ON DELETE CASCADE;


--

-- Name: onboarding_progress onboarding_progress_organisation_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.onboarding_progress
    ADD CONSTRAINT onboarding_progress_organisation_id_fkey FOREIGN KEY (organisation_id) REFERENCES public.organisations(id) ON DELETE CASCADE;


--

-- Name: org_api_keys org_api_keys_account_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.org_api_keys
    ADD CONSTRAINT org_api_keys_account_id_fkey FOREIGN KEY (account_id) REFERENCES public.accounts(id) ON DELETE RESTRICT;


--

-- Name: org_api_keys org_api_keys_organisation_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.org_api_keys
    ADD CONSTRAINT org_api_keys_organisation_id_fkey FOREIGN KEY (organisation_id) REFERENCES public.organisations(id) ON DELETE CASCADE;


--

-- Name: org_api_keys org_api_keys_run_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.org_api_keys
    ADD CONSTRAINT org_api_keys_run_id_fkey FOREIGN KEY (run_id) REFERENCES public.runs(id) ON DELETE SET NULL;


--

-- Name: org_api_keys org_api_keys_team_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.org_api_keys
    ADD CONSTRAINT org_api_keys_team_id_fkey FOREIGN KEY (team_id) REFERENCES public.teams(id) ON DELETE CASCADE;


--

-- Name: org_daily_run_counts org_daily_run_counts_organisation_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.org_daily_run_counts
    ADD CONSTRAINT org_daily_run_counts_organisation_id_fkey FOREIGN KEY (organisation_id) REFERENCES public.organisations(id) ON DELETE CASCADE;


--

-- Name: org_daily_run_counts org_daily_run_counts_team_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.org_daily_run_counts
    ADD CONSTRAINT org_daily_run_counts_team_id_fkey FOREIGN KEY (team_id) REFERENCES public.teams(id) ON DELETE CASCADE;


--

-- Name: org_memberships org_memberships_account_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.org_memberships
    ADD CONSTRAINT org_memberships_account_id_fkey FOREIGN KEY (account_id) REFERENCES public.accounts(id) ON DELETE CASCADE;


--

-- Name: org_memberships org_memberships_organisation_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.org_memberships
    ADD CONSTRAINT org_memberships_organisation_id_fkey FOREIGN KEY (organisation_id) REFERENCES public.organisations(id) ON DELETE CASCADE;


--

-- Name: org_mint_budget_usage org_mint_budget_usage_organisation_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.org_mint_budget_usage
    ADD CONSTRAINT org_mint_budget_usage_organisation_id_fkey FOREIGN KEY (organisation_id) REFERENCES public.organisations(id) ON DELETE CASCADE;


--

-- Name: organisation_cost_state organisation_cost_state_org_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.organisation_cost_state
    ADD CONSTRAINT organisation_cost_state_org_id_fkey FOREIGN KEY (org_id) REFERENCES public.organisations(id) ON DELETE CASCADE;


--

-- Name: parameter_schemas parameter_schemas_account_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.parameter_schemas
    ADD CONSTRAINT parameter_schemas_account_id_fkey FOREIGN KEY (account_id) REFERENCES public.accounts(id) ON DELETE RESTRICT;


--

-- Name: parameter_schemas parameter_schemas_organisation_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.parameter_schemas
    ADD CONSTRAINT parameter_schemas_organisation_id_fkey FOREIGN KEY (organisation_id) REFERENCES public.organisations(id) ON DELETE CASCADE;


--

-- Name: parameter_sets parameter_sets_account_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.parameter_sets
    ADD CONSTRAINT parameter_sets_account_id_fkey FOREIGN KEY (account_id) REFERENCES public.accounts(id) ON DELETE RESTRICT;


--

-- Name: parameter_sets parameter_sets_organisation_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.parameter_sets
    ADD CONSTRAINT parameter_sets_organisation_id_fkey FOREIGN KEY (organisation_id) REFERENCES public.organisations(id) ON DELETE CASCADE;


--

-- Name: parameter_sets parameter_sets_parameter_schema_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.parameter_sets
    ADD CONSTRAINT parameter_sets_parameter_schema_id_fkey FOREIGN KEY (parameter_schema_id) REFERENCES public.parameter_schemas(id) ON DELETE RESTRICT;


--

-- Name: pipeline_edges pipeline_edges_organisation_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.pipeline_edges
    ADD CONSTRAINT pipeline_edges_organisation_id_fkey FOREIGN KEY (organisation_id) REFERENCES public.organisations(id) ON DELETE CASCADE;


--

-- Name: pipeline_edges pipeline_edges_pipeline_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.pipeline_edges
    ADD CONSTRAINT pipeline_edges_pipeline_id_fkey FOREIGN KEY (pipeline_id) REFERENCES public.pipelines(id) ON DELETE CASCADE;


--

-- Name: pipeline_folders pipeline_folders_account_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.pipeline_folders
    ADD CONSTRAINT pipeline_folders_account_id_fkey FOREIGN KEY (account_id) REFERENCES public.accounts(id) ON DELETE RESTRICT;


--

-- Name: pipeline_folders pipeline_folders_organisation_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.pipeline_folders
    ADD CONSTRAINT pipeline_folders_organisation_id_fkey FOREIGN KEY (organisation_id) REFERENCES public.organisations(id) ON DELETE CASCADE;


--

-- Name: pipeline_folders pipeline_folders_parent_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.pipeline_folders
    ADD CONSTRAINT pipeline_folders_parent_id_fkey FOREIGN KEY (parent_id) REFERENCES public.pipeline_folders(id) ON DELETE CASCADE;


--

-- Name: pipeline_snapshots pipeline_snapshots_account_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.pipeline_snapshots
    ADD CONSTRAINT pipeline_snapshots_account_id_fkey FOREIGN KEY (account_id) REFERENCES public.accounts(id) ON DELETE SET NULL;


--

-- Name: pipeline_snapshots pipeline_snapshots_environment_profile_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.pipeline_snapshots
    ADD CONSTRAINT pipeline_snapshots_environment_profile_id_fkey FOREIGN KEY (environment_profile_id) REFERENCES public.environment_profiles(id) ON DELETE SET NULL;


--

-- Name: pipeline_snapshots pipeline_snapshots_organisation_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.pipeline_snapshots
    ADD CONSTRAINT pipeline_snapshots_organisation_id_fkey FOREIGN KEY (organisation_id) REFERENCES public.organisations(id) ON DELETE CASCADE;


--

-- Name: pipeline_snapshots pipeline_snapshots_pipeline_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.pipeline_snapshots
    ADD CONSTRAINT pipeline_snapshots_pipeline_id_fkey FOREIGN KEY (pipeline_id) REFERENCES public.pipelines(id) ON DELETE RESTRICT;


--

-- Name: pipelines pipelines_account_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.pipelines
    ADD CONSTRAINT pipelines_account_id_fkey FOREIGN KEY (account_id) REFERENCES public.accounts(id) ON DELETE RESTRICT;


--

-- Name: pipelines pipelines_organisation_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.pipelines
    ADD CONSTRAINT pipelines_organisation_id_fkey FOREIGN KEY (organisation_id) REFERENCES public.organisations(id) ON DELETE CASCADE;


--

-- Name: pipelines pipelines_owner_team_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.pipelines
    ADD CONSTRAINT pipelines_owner_team_id_fkey FOREIGN KEY (owner_team_id) REFERENCES public.teams(id) ON DELETE RESTRICT;


--

-- Name: policy_gate_decisions policy_gate_decisions_organisation_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.policy_gate_decisions
    ADD CONSTRAINT policy_gate_decisions_organisation_id_fkey FOREIGN KEY (organisation_id) REFERENCES public.organisations(id) ON DELETE CASCADE;


--

-- Name: policy_gates policy_gates_organisation_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.policy_gates
    ADD CONSTRAINT policy_gates_organisation_id_fkey FOREIGN KEY (organisation_id) REFERENCES public.organisations(id) ON DELETE CASCADE;


--

-- Name: primitive_abuse_reports primitive_abuse_reports_organisation_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.primitive_abuse_reports
    ADD CONSTRAINT primitive_abuse_reports_organisation_id_fkey FOREIGN KEY (organisation_id) REFERENCES public.organisations(id) ON DELETE CASCADE;


--

-- Name: primitive_abuse_reports primitive_abuse_reports_primitive_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.primitive_abuse_reports
    ADD CONSTRAINT primitive_abuse_reports_primitive_id_fkey FOREIGN KEY (primitive_id) REFERENCES public.library_primitives(id) ON DELETE CASCADE;


--

-- Name: primitive_abuse_reports primitive_abuse_reports_rating_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.primitive_abuse_reports
    ADD CONSTRAINT primitive_abuse_reports_rating_id_fkey FOREIGN KEY (rating_id) REFERENCES public.primitive_ratings(id) ON DELETE SET NULL;


--

-- Name: primitive_abuse_reports primitive_abuse_reports_reporter_account_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.primitive_abuse_reports
    ADD CONSTRAINT primitive_abuse_reports_reporter_account_id_fkey FOREIGN KEY (reporter_account_id) REFERENCES public.accounts(id) ON DELETE SET NULL;


--

-- Name: primitive_abuse_reports primitive_abuse_reports_reviewer_account_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.primitive_abuse_reports
    ADD CONSTRAINT primitive_abuse_reports_reviewer_account_id_fkey FOREIGN KEY (reviewer_account_id) REFERENCES public.accounts(id) ON DELETE SET NULL;


--

-- Name: primitive_ratings primitive_ratings_account_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.primitive_ratings
    ADD CONSTRAINT primitive_ratings_account_id_fkey FOREIGN KEY (account_id) REFERENCES public.accounts(id) ON DELETE SET NULL;


--

-- Name: primitive_ratings primitive_ratings_organisation_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.primitive_ratings
    ADD CONSTRAINT primitive_ratings_organisation_id_fkey FOREIGN KEY (organisation_id) REFERENCES public.organisations(id) ON DELETE CASCADE;


--

-- Name: primitive_ratings primitive_ratings_primitive_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.primitive_ratings
    ADD CONSTRAINT primitive_ratings_primitive_id_fkey FOREIGN KEY (primitive_id) REFERENCES public.library_primitives(id) ON DELETE CASCADE;


--

-- Name: publishers publishers_organisation_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.publishers
    ADD CONSTRAINT publishers_organisation_id_fkey FOREIGN KEY (organisation_id) REFERENCES public.organisations(id) ON DELETE CASCADE;


--

-- Name: run_attempts run_attempts_organisation_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.run_attempts
    ADD CONSTRAINT run_attempts_organisation_id_fkey FOREIGN KEY (organisation_id) REFERENCES public.organisations(id) ON DELETE CASCADE;


--

-- Name: run_attempts run_attempts_run_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.run_attempts
    ADD CONSTRAINT run_attempts_run_id_fkey FOREIGN KEY (run_id) REFERENCES public.runs(id) ON DELETE CASCADE;


--

-- Name: run_daily_facts run_daily_facts_folder_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.run_daily_facts
    ADD CONSTRAINT run_daily_facts_folder_id_fkey FOREIGN KEY (folder_id) REFERENCES public.pipeline_folders(id) ON DELETE SET NULL;


--

-- Name: run_daily_facts run_daily_facts_organisation_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.run_daily_facts
    ADD CONSTRAINT run_daily_facts_organisation_id_fkey FOREIGN KEY (organisation_id) REFERENCES public.organisations(id) ON DELETE CASCADE;


--

-- Name: run_daily_facts run_daily_facts_pipeline_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.run_daily_facts
    ADD CONSTRAINT run_daily_facts_pipeline_id_fkey FOREIGN KEY (pipeline_id) REFERENCES public.pipelines(id) ON DELETE SET NULL;


--

-- Name: run_daily_facts run_daily_facts_team_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.run_daily_facts
    ADD CONSTRAINT run_daily_facts_team_id_fkey FOREIGN KEY (team_id) REFERENCES public.teams(id) ON DELETE SET NULL;


--

-- Name: run_evidence run_evidence_run_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.run_evidence
    ADD CONSTRAINT run_evidence_run_id_fkey FOREIGN KEY (run_id) REFERENCES public.runs(id) ON DELETE CASCADE;


--

-- Name: run_node_outputs run_node_outputs_organisation_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.run_node_outputs
    ADD CONSTRAINT run_node_outputs_organisation_id_fkey FOREIGN KEY (organisation_id) REFERENCES public.organisations(id) ON DELETE CASCADE;


--

-- Name: run_node_outputs run_node_outputs_run_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.run_node_outputs
    ADD CONSTRAINT run_node_outputs_run_id_fkey FOREIGN KEY (run_id) REFERENCES public.runs(id) ON DELETE CASCADE;


--

-- Name: run_number_counters run_number_counters_organisation_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.run_number_counters
    ADD CONSTRAINT run_number_counters_organisation_id_fkey FOREIGN KEY (organisation_id) REFERENCES public.organisations(id) ON DELETE CASCADE;


--

-- Name: runner_probe_cache runner_probe_cache_organisation_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.runner_probe_cache
    ADD CONSTRAINT runner_probe_cache_organisation_id_fkey FOREIGN KEY (organisation_id) REFERENCES public.organisations(id) ON DELETE CASCADE;


--

-- Name: runs runs_account_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.runs
    ADD CONSTRAINT runs_account_id_fkey FOREIGN KEY (account_id) REFERENCES public.accounts(id) ON DELETE SET NULL;


--

-- Name: runs runs_organisation_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.runs
    ADD CONSTRAINT runs_organisation_id_fkey FOREIGN KEY (organisation_id) REFERENCES public.organisations(id) ON DELETE CASCADE;


--

-- Name: runs runs_owner_team_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.runs
    ADD CONSTRAINT runs_owner_team_id_fkey FOREIGN KEY (owner_team_id) REFERENCES public.teams(id) ON DELETE RESTRICT;


--

-- Name: runs runs_parent_run_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.runs
    ADD CONSTRAINT runs_parent_run_id_fkey FOREIGN KEY (parent_run_id) REFERENCES public.runs(id) ON DELETE SET NULL;


--

-- Name: runs runs_pipeline_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.runs
    ADD CONSTRAINT runs_pipeline_id_fkey FOREIGN KEY (pipeline_id) REFERENCES public.pipelines(id) ON DELETE RESTRICT;


--

-- Name: runs runs_snapshot_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.runs
    ADD CONSTRAINT runs_snapshot_id_fkey FOREIGN KEY (snapshot_id) REFERENCES public.pipeline_snapshots(id) ON DELETE RESTRICT;


--

-- Name: runs runs_trigger_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.runs
    ADD CONSTRAINT runs_trigger_id_fkey FOREIGN KEY (trigger_id) REFERENCES public.triggers(id) ON DELETE SET NULL;


--

-- Name: runs runs_variant_group_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.runs
    ADD CONSTRAINT runs_variant_group_id_fkey FOREIGN KEY (variant_group_id) REFERENCES public.variant_groups(id) ON DELETE SET NULL;


--

-- Name: saved_views saved_views_account_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.saved_views
    ADD CONSTRAINT saved_views_account_id_fkey FOREIGN KEY (account_id) REFERENCES public.accounts(id) ON DELETE RESTRICT;


--

-- Name: saved_views saved_views_organisation_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.saved_views
    ADD CONSTRAINT saved_views_organisation_id_fkey FOREIGN KEY (organisation_id) REFERENCES public.organisations(id) ON DELETE CASCADE;


--

-- Name: scheduled_reports scheduled_reports_created_by_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.scheduled_reports
    ADD CONSTRAINT scheduled_reports_created_by_fkey FOREIGN KEY (created_by) REFERENCES public.accounts(id) ON DELETE SET NULL;


--

-- Name: scheduled_reports scheduled_reports_organisation_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.scheduled_reports
    ADD CONSTRAINT scheduled_reports_organisation_id_fkey FOREIGN KEY (organisation_id) REFERENCES public.organisations(id) ON DELETE CASCADE;


--

-- Name: schema_folders schema_folders_account_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.schema_folders
    ADD CONSTRAINT schema_folders_account_id_fkey FOREIGN KEY (account_id) REFERENCES public.accounts(id) ON DELETE RESTRICT;


--

-- Name: schema_folders schema_folders_organisation_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.schema_folders
    ADD CONSTRAINT schema_folders_organisation_id_fkey FOREIGN KEY (organisation_id) REFERENCES public.organisations(id) ON DELETE CASCADE;


--

-- Name: schema_folders schema_folders_parent_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.schema_folders
    ADD CONSTRAINT schema_folders_parent_id_fkey FOREIGN KEY (parent_id) REFERENCES public.schema_folders(id) ON DELETE CASCADE;


--

-- Name: schema_versions schema_versions_account_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.schema_versions
    ADD CONSTRAINT schema_versions_account_id_fkey FOREIGN KEY (account_id) REFERENCES public.accounts(id) ON DELETE RESTRICT;


--

-- Name: schema_versions schema_versions_organisation_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.schema_versions
    ADD CONSTRAINT schema_versions_organisation_id_fkey FOREIGN KEY (organisation_id) REFERENCES public.organisations(id) ON DELETE CASCADE;


--

-- Name: schema_versions schema_versions_schema_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.schema_versions
    ADD CONSTRAINT schema_versions_schema_id_fkey FOREIGN KEY (schema_id) REFERENCES public.schemas(id) ON DELETE CASCADE;


--

-- Name: schemas schemas_account_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.schemas
    ADD CONSTRAINT schemas_account_id_fkey FOREIGN KEY (account_id) REFERENCES public.accounts(id) ON DELETE RESTRICT;


--

-- Name: schemas schemas_organisation_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.schemas
    ADD CONSTRAINT schemas_organisation_id_fkey FOREIGN KEY (organisation_id) REFERENCES public.organisations(id) ON DELETE CASCADE;


--

-- Name: secrets secrets_organisation_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.secrets
    ADD CONSTRAINT secrets_organisation_id_fkey FOREIGN KEY (organisation_id) REFERENCES public.organisations(id) ON DELETE CASCADE;


--

-- Name: snapshot_schema_pins snapshot_schema_pins_organisation_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.snapshot_schema_pins
    ADD CONSTRAINT snapshot_schema_pins_organisation_id_fkey FOREIGN KEY (organisation_id) REFERENCES public.organisations(id) ON DELETE CASCADE;


--

-- Name: snapshot_schema_pins snapshot_schema_pins_schema_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.snapshot_schema_pins
    ADD CONSTRAINT snapshot_schema_pins_schema_id_fkey FOREIGN KEY (schema_id) REFERENCES public.schemas(id) ON DELETE RESTRICT;


--

-- Name: snapshot_schema_pins snapshot_schema_pins_schema_id_schema_version_organisation_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.snapshot_schema_pins
    ADD CONSTRAINT snapshot_schema_pins_schema_id_schema_version_organisation_fkey FOREIGN KEY (schema_id, schema_version, organisation_id) REFERENCES public.schema_versions(schema_id, version, organisation_id) ON DELETE RESTRICT;


--

-- Name: snapshot_schema_pins snapshot_schema_pins_snapshot_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.snapshot_schema_pins
    ADD CONSTRAINT snapshot_schema_pins_snapshot_id_fkey FOREIGN KEY (snapshot_id) REFERENCES public.pipeline_snapshots(id) ON DELETE CASCADE;


--

-- Name: spend_anomalies spend_anomalies_organisation_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.spend_anomalies
    ADD CONSTRAINT spend_anomalies_organisation_id_fkey FOREIGN KEY (organisation_id) REFERENCES public.organisations(id) ON DELETE CASCADE;


--

-- Name: spend_anomalies spend_anomalies_pipeline_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.spend_anomalies
    ADD CONSTRAINT spend_anomalies_pipeline_id_fkey FOREIGN KEY (pipeline_id) REFERENCES public.pipelines(id) ON DELETE SET NULL;


--

-- Name: sso_providers sso_providers_organisation_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.sso_providers
    ADD CONSTRAINT sso_providers_organisation_id_fkey FOREIGN KEY (organisation_id) REFERENCES public.organisations(id) ON DELETE CASCADE;


--

-- Name: suite_runs suite_runs_baseline_run_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.suite_runs
    ADD CONSTRAINT suite_runs_baseline_run_id_fkey FOREIGN KEY (baseline_run_id) REFERENCES public.suite_runs(id) ON DELETE SET NULL;


--

-- Name: suite_runs suite_runs_dataset_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.suite_runs
    ADD CONSTRAINT suite_runs_dataset_id_fkey FOREIGN KEY (dataset_id) REFERENCES public.eval_datasets(id) ON DELETE RESTRICT;


--

-- Name: suite_runs suite_runs_model_backend_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.suite_runs
    ADD CONSTRAINT suite_runs_model_backend_id_fkey FOREIGN KEY (model_backend_id) REFERENCES public.model_backends(id) ON DELETE RESTRICT;


--

-- Name: suite_runs suite_runs_organisation_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.suite_runs
    ADD CONSTRAINT suite_runs_organisation_id_fkey FOREIGN KEY (organisation_id) REFERENCES public.organisations(id) ON DELETE CASCADE;


--

-- Name: suite_runs suite_runs_owner_team_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.suite_runs
    ADD CONSTRAINT suite_runs_owner_team_id_fkey FOREIGN KEY (owner_team_id) REFERENCES public.teams(id) ON DELETE RESTRICT;


--

-- Name: suite_runs suite_runs_suite_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.suite_runs
    ADD CONSTRAINT suite_runs_suite_id_fkey FOREIGN KEY (suite_id) REFERENCES public.eval_suites(id) ON DELETE RESTRICT;


--

-- Name: system_map_attribution_bindings system_map_attribution_bindings_organisation_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.system_map_attribution_bindings
    ADD CONSTRAINT system_map_attribution_bindings_organisation_id_fkey FOREIGN KEY (organisation_id) REFERENCES public.organisations(id) ON DELETE CASCADE;


--

-- Name: system_map_attribution_bindings system_map_attribution_bindings_pipeline_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.system_map_attribution_bindings
    ADD CONSTRAINT system_map_attribution_bindings_pipeline_id_fkey FOREIGN KEY (pipeline_id) REFERENCES public.pipelines(id) ON DELETE CASCADE;


--

-- Name: system_map_attribution_bindings system_map_attribution_bindings_trigger_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.system_map_attribution_bindings
    ADD CONSTRAINT system_map_attribution_bindings_trigger_id_fkey FOREIGN KEY (trigger_id) REFERENCES public.triggers(id) ON DELETE CASCADE;


--

-- Name: system_map_loop_admission_attempts system_map_loop_admission_attempts_organisation_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.system_map_loop_admission_attempts
    ADD CONSTRAINT system_map_loop_admission_attempts_organisation_id_fkey FOREIGN KEY (organisation_id) REFERENCES public.organisations(id) ON DELETE CASCADE;


--

-- Name: system_map_loop_configs system_map_loop_configs_action_pipeline_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.system_map_loop_configs
    ADD CONSTRAINT system_map_loop_configs_action_pipeline_id_fkey FOREIGN KEY (action_pipeline_id) REFERENCES public.pipelines(id) ON DELETE RESTRICT;


--

-- Name: system_map_loop_configs system_map_loop_configs_organisation_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.system_map_loop_configs
    ADD CONSTRAINT system_map_loop_configs_organisation_id_fkey FOREIGN KEY (organisation_id) REFERENCES public.organisations(id) ON DELETE CASCADE;


--

-- Name: system_map_loop_reservations system_map_loop_reservations_organisation_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.system_map_loop_reservations
    ADD CONSTRAINT system_map_loop_reservations_organisation_id_fkey FOREIGN KEY (organisation_id) REFERENCES public.organisations(id) ON DELETE CASCADE;


--

-- Name: system_map_loop_reservations system_map_loop_reservations_run_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.system_map_loop_reservations
    ADD CONSTRAINT system_map_loop_reservations_run_id_fkey FOREIGN KEY (run_id) REFERENCES public.runs(id) ON DELETE RESTRICT;


--

-- Name: system_map_nodes system_map_nodes_connector_instance_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.system_map_nodes
    ADD CONSTRAINT system_map_nodes_connector_instance_id_fkey FOREIGN KEY (connector_instance_id) REFERENCES public.connector_instances(id) ON DELETE SET NULL;


--

-- Name: system_map_nodes system_map_nodes_eval_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.system_map_nodes
    ADD CONSTRAINT system_map_nodes_eval_id_fkey FOREIGN KEY (eval_id) REFERENCES public.evals(id) ON DELETE SET NULL;


--

-- Name: system_map_nodes system_map_nodes_organisation_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.system_map_nodes
    ADD CONSTRAINT system_map_nodes_organisation_id_fkey FOREIGN KEY (organisation_id) REFERENCES public.organisations(id) ON DELETE CASCADE;


--

-- Name: system_map_nodes system_map_nodes_owner_team_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.system_map_nodes
    ADD CONSTRAINT system_map_nodes_owner_team_id_fkey FOREIGN KEY (owner_team_id) REFERENCES public.teams(id) ON DELETE SET NULL;


--

-- Name: system_map_nodes system_map_nodes_pipeline_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.system_map_nodes
    ADD CONSTRAINT system_map_nodes_pipeline_id_fkey FOREIGN KEY (pipeline_id) REFERENCES public.pipelines(id) ON DELETE SET NULL;


--

-- Name: system_map_relationships system_map_relationships_organisation_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.system_map_relationships
    ADD CONSTRAINT system_map_relationships_organisation_id_fkey FOREIGN KEY (organisation_id) REFERENCES public.organisations(id) ON DELETE CASCADE;


--

-- Name: system_map_revisions system_map_revisions_organisation_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.system_map_revisions
    ADD CONSTRAINT system_map_revisions_organisation_id_fkey FOREIGN KEY (organisation_id) REFERENCES public.organisations(id) ON DELETE CASCADE;


--

-- Name: system_maps system_maps_account_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.system_maps
    ADD CONSTRAINT system_maps_account_id_fkey FOREIGN KEY (account_id) REFERENCES public.accounts(id) ON DELETE RESTRICT;


--

-- Name: system_maps system_maps_deleted_by_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.system_maps
    ADD CONSTRAINT system_maps_deleted_by_fkey FOREIGN KEY (deleted_by) REFERENCES public.accounts(id) ON DELETE SET NULL;


--

-- Name: system_maps system_maps_organisation_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.system_maps
    ADD CONSTRAINT system_maps_organisation_id_fkey FOREIGN KEY (organisation_id) REFERENCES public.organisations(id) ON DELETE CASCADE;


--

-- Name: system_maps system_maps_owner_team_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.system_maps
    ADD CONSTRAINT system_maps_owner_team_id_fkey FOREIGN KEY (owner_team_id) REFERENCES public.teams(id) ON DELETE SET NULL;


--

-- Name: system_maps system_maps_updated_by_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.system_maps
    ADD CONSTRAINT system_maps_updated_by_fkey FOREIGN KEY (updated_by) REFERENCES public.accounts(id) ON DELETE SET NULL;


--

-- Name: team_memberships team_memberships_account_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.team_memberships
    ADD CONSTRAINT team_memberships_account_id_fkey FOREIGN KEY (account_id) REFERENCES public.accounts(id) ON DELETE CASCADE;


--

-- Name: team_memberships team_memberships_organisation_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.team_memberships
    ADD CONSTRAINT team_memberships_organisation_id_fkey FOREIGN KEY (organisation_id) REFERENCES public.organisations(id) ON DELETE CASCADE;


--

-- Name: team_memberships team_memberships_team_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.team_memberships
    ADD CONSTRAINT team_memberships_team_id_fkey FOREIGN KEY (team_id) REFERENCES public.teams(id) ON DELETE CASCADE;


--

-- Name: teams teams_account_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.teams
    ADD CONSTRAINT teams_account_id_fkey FOREIGN KEY (account_id) REFERENCES public.accounts(id) ON DELETE RESTRICT;


--

-- Name: teams teams_organisation_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.teams
    ADD CONSTRAINT teams_organisation_id_fkey FOREIGN KEY (organisation_id) REFERENCES public.organisations(id) ON DELETE CASCADE;


--

-- Name: token_families token_families_account_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.token_families
    ADD CONSTRAINT token_families_account_id_fkey FOREIGN KEY (account_id) REFERENCES public.accounts(id) ON DELETE CASCADE;


--

-- Name: token_families token_families_organisation_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.token_families
    ADD CONSTRAINT token_families_organisation_id_fkey FOREIGN KEY (organisation_id) REFERENCES public.organisations(id) ON DELETE CASCADE;


--

-- Name: trigger_event_findings trigger_event_findings_organisation_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.trigger_event_findings
    ADD CONSTRAINT trigger_event_findings_organisation_id_fkey FOREIGN KEY (organisation_id) REFERENCES public.organisations(id) ON DELETE CASCADE;


--

-- Name: trigger_event_findings trigger_event_findings_resolved_by_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.trigger_event_findings
    ADD CONSTRAINT trigger_event_findings_resolved_by_fkey FOREIGN KEY (resolved_by) REFERENCES public.accounts(id) ON DELETE SET NULL;


--

-- Name: trigger_events trigger_events_organisation_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.trigger_events
    ADD CONSTRAINT trigger_events_organisation_id_fkey FOREIGN KEY (organisation_id) REFERENCES public.organisations(id) ON DELETE CASCADE;


--

-- Name: trigger_events trigger_events_run_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.trigger_events
    ADD CONSTRAINT trigger_events_run_id_fkey FOREIGN KEY (run_id) REFERENCES public.runs(id) ON DELETE SET NULL;


--

-- Name: trigger_events trigger_events_trigger_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.trigger_events
    ADD CONSTRAINT trigger_events_trigger_id_fkey FOREIGN KEY (trigger_id) REFERENCES public.triggers(id) ON DELETE CASCADE;


--

-- Name: triggers triggers_account_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.triggers
    ADD CONSTRAINT triggers_account_id_fkey FOREIGN KEY (account_id) REFERENCES public.accounts(id) ON DELETE RESTRICT;


--

-- Name: triggers triggers_organisation_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.triggers
    ADD CONSTRAINT triggers_organisation_id_fkey FOREIGN KEY (organisation_id) REFERENCES public.organisations(id) ON DELETE CASCADE;


--

-- Name: triggers triggers_pipeline_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.triggers
    ADD CONSTRAINT triggers_pipeline_id_fkey FOREIGN KEY (pipeline_id) REFERENCES public.pipelines(id) ON DELETE CASCADE;


--

-- Name: variant_groups variant_groups_organisation_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.variant_groups
    ADD CONSTRAINT variant_groups_organisation_id_fkey FOREIGN KEY (organisation_id) REFERENCES public.organisations(id) ON DELETE CASCADE;


--

-- Name: variant_groups variant_groups_pipeline_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.variant_groups
    ADD CONSTRAINT variant_groups_pipeline_id_fkey FOREIGN KEY (pipeline_id) REFERENCES public.pipelines(id) ON DELETE RESTRICT;


--

-- Name: web_vital_events web_vital_events_organisation_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.web_vital_events
    ADD CONSTRAINT web_vital_events_organisation_id_fkey FOREIGN KEY (organisation_id) REFERENCES public.organisations(id) ON DELETE CASCADE;


--

-- Name: webhook_dedup_hashes webhook_dedup_hashes_organisation_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.webhook_dedup_hashes
    ADD CONSTRAINT webhook_dedup_hashes_organisation_id_fkey FOREIGN KEY (organisation_id) REFERENCES public.organisations(id) ON DELETE CASCADE;


--

-- Name: webhook_dedup_hashes webhook_dedup_hashes_trigger_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.webhook_dedup_hashes
    ADD CONSTRAINT webhook_dedup_hashes_trigger_id_fkey FOREIGN KEY (trigger_id) REFERENCES public.triggers(id) ON DELETE CASCADE;


--

-- Name: webhook_payloads webhook_payloads_organisation_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.webhook_payloads
    ADD CONSTRAINT webhook_payloads_organisation_id_fkey FOREIGN KEY (organisation_id) REFERENCES public.organisations(id) ON DELETE CASCADE;


--

-- Name: webhook_payloads webhook_payloads_trigger_event_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.webhook_payloads
    ADD CONSTRAINT webhook_payloads_trigger_event_id_fkey FOREIGN KEY (trigger_event_id) REFERENCES public.trigger_events(id) ON DELETE CASCADE;


--""")


def downgrade() -> None:
    op.execute("""DROP SCHEMA public CASCADE;""")
