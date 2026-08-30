from modulo.db.models import Base


def test_initial_schema_contains_required_tables() -> None:
    required = {
        "accounts",
        "agents",
        "audit_chain_heads",
        "audit_events",
        "chat_messages",
        "chat_sessions",
        "composite_templates",
        "connector_instances",
        "connector_profiles",
        "cost_components",
        "deleted_defaults",
        "dismissals",
        "environment_profiles",
        "error_events",
        "error_forwarder_configs",
        "error_groups",
        "error_notification_rules",
        "eval_cases",
        "eval_datasets",
        "eval_definitions",
        "eval_results",
        "eval_suites",
        "feature_flag_catalog",
        "feedback_records",
        "hitl_claims",
        "journeys",
        "library_primitives",
        "library_sync_state",
        "lifecycle_maps",
        "lifecycle_map_stages",
        "mcp_setup_tokens",
        "metrics_staging",
        "model_backends",
        "modulo_journey_facts",
        "node_categories",
        "node_observations",
        "nodes",
        "notification_delivery_log",
        "notification_endpoints",
        "notification_preferences",
        "notifications",
        "onboarding_progress",
        "oauth_authorization_codes",
        "oauth_clients",
        "oauth_consent_states",
        "oauth_token_families",
        "org_api_keys",
        "org_daily_run_counts",
        "org_memberships",
        "organisations",
        "pipeline_edges",
        "pipeline_folders",
        "parameter_schemas",
        "parameter_sets",
        "pipeline_snapshots",
        "pipelines",
        "primitive_abuse_reports",
        "primitive_ratings",
        "publishers",
        "remy_context_sources",
        "remy_skills",
        "run_daily_facts",
        "run_evidence",
        "runs",
        "saved_views",
        "scheduled_reports",
        "snapshot_schema_pins",
        "schema_folders",
        "schema_versions",
        "schemas",
        "secrets",
        "spend_anomalies",
        "sso_providers",
        "suite_runs",
        "system_config",
        "team_memberships",
        "teams",
        "tier_catalog",
        "token_families",
        "trigger_events",
        "triggers",
        "variant_groups",
        "webhook_dedup_hashes",
        "webhook_payloads",
        "web_vital_events",
        "workspace_leases",
    }

    assert required == set(Base.metadata.tables)


def test_all_resource_tables_are_organisation_scoped() -> None:
    for name, table in Base.metadata.tables.items():
        if name not in (
            "organisations",
            "accounts",
            "system_config",
            "tier_catalog",
            "feature_flag_catalog",
            "library_sync_state",
        ):
            assert "organisation_id" in table.c, f"{name} is missing organisation_id"


def test_initial_schema_includes_forward_compatible_fields() -> None:
    tables = Base.metadata.tables

    for name in (
        "connector_instances",
        "library_primitives",
        "model_backends",
        "pipelines",
    ):
        assert {"owner_team_id", "visibility"} <= set(tables[name].c.keys())

    assert tables["agents"].c.evals.nullable
    assert {
        "id",
        "pipeline_id",
        "source_node_id",
        "target_node_id",
        "edge_type",
        "hitl_gate_config",
    } <= set(tables["pipeline_edges"].c.keys())
    assert {
        "run_id",
        "gate_id",
        "pipeline_id",
        "account_id",
        "claimed_at",
        "claim_token",
        "expires_at",
    } <= set(tables["hitl_claims"].c.keys())


def test_reviewed_security_and_provenance_contracts() -> None:
    tables = Base.metadata.tables

    assert {"hashed_secret", "team_id"} <= set(tables["org_api_keys"].c.keys())
    assert "key_hash" not in tables["org_api_keys"].c
    assert "graph_nodes_json" in tables["pipelines"].c
    assert {
        "forked_from",
        "source_url",
        "checksum",
        "ed25519_signature",
        "verified",
        "download_count",
        "average_rating",
        "review_count",
    } <= set(tables["library_primitives"].c.keys())
    assert {"received_at", "validation_result", "error_detail"} <= set(tables["trigger_events"].c.keys())

    agent_foreign_keys = {constraint.name for constraint in tables["agents"].foreign_key_constraints}
    assert "fk_agents_input_schema_version" in agent_foreign_keys
    assert "fk_agents_output_schema_version" in agent_foreign_keys


def test_visibility_and_trigger_outcome_constraints_are_complete() -> None:
    tables = Base.metadata.tables
    for name in (
        "connector_instances",
        "library_primitives",
        "model_backends",
        "pipelines",
    ):
        constraint_names = {constraint.name for constraint in tables[name].constraints}
        assert any(value is not None and value.endswith("_team_owner") for value in constraint_names)

    trigger_checks = " ".join(
        str(constraint.sqltext) for constraint in tables["trigger_events"].constraints if hasattr(constraint, "sqltext")
    )
    # The ORM CheckConstraint is generated from the full 19-value vocabulary —
    # every value must appear and the generated SQL must match exactly.
    from modulo.db.models.trigger_event import VALIDATION_RESULT_VALUES

    assert f"validation_result IN {tuple(VALIDATION_RESULT_VALUES)}" in trigger_checks
    for outcome in VALIDATION_RESULT_VALUES:
        assert outcome in trigger_checks
