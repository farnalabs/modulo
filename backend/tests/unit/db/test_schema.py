from modulo.db.models import Base


def test_initial_schema_contains_required_tables() -> None:
    required = {
        "agents",
        "audit_chain_heads",
        "audit_events",
        "connector_instances",
        "environment_profiles",
        "eval_definitions",
        "eval_results",
        "feedback_records",
        "hitl_claims",
        "library_primitives",
        "model_backends",
        "notification_delivery_log",
        "notification_endpoints",
        "oauth_authorization_codes",
        "oauth_clients",
        "oauth_token_families",
        "org_api_keys",
        "org_daily_run_counts",
        "organisations",
        "pipeline_edges",
        "pipeline_snapshots",
        "pipelines",
        "primitive_ratings",
        "publishers",
        "runs",
        "scheduled_reports",
        "schema_versions",
        "schemas",
        "secrets",
        "spend_anomalies",
        "sso_providers",
        "stages",
        "team_memberships",
        "teams",
        "token_families",
        "trigger_events",
        "triggers",
        "users",
        "variant_groups",
        "webhook_dedup_hashes",
        "webhook_payloads",
        "workspace_leases",
    }

    assert required == set(Base.metadata.tables)


def test_all_resource_tables_are_organisation_scoped() -> None:
    for name, table in Base.metadata.tables.items():
        if name != "organisations":
            assert "organisation_id" in table.c, f"{name} is missing organisation_id"


def test_initial_schema_includes_forward_compatible_fields() -> None:
    tables = Base.metadata.tables

    for name in (
        "connector_instances",
        "library_primitives",
        "model_backends",
        "pipelines",
        "stages",
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
        "claimed_by",
        "claimed_at",
        "claim_token",
        "expires_at",
    } <= set(tables["hitl_claims"].c.keys())


def test_reviewed_security_and_provenance_contracts() -> None:
    tables = Base.metadata.tables

    assert {"hashed_secret", "team_id"} <= set(tables["org_api_keys"].c.keys())
    assert "key_hash" not in tables["org_api_keys"].c
    assert "stage_id" in tables["pipelines"].c
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
        "stages",
    ):
        constraint_names = {constraint.name for constraint in tables[name].constraints}
        assert any(value is not None and value.endswith("_team_owner") for value in constraint_names)

    trigger_checks = " ".join(
        str(constraint.sqltext) for constraint in tables["trigger_events"].constraints if hasattr(constraint, "sqltext")
    )
    for outcome in (
        "passed",
        "hmac_failed",
        "schema_validation_failed",
        "deduplicated",
        "concurrency_limit_reached",
        "timestamp_expired",
        "validation_failed",
        "rate_limited",
    ):
        assert outcome in trigger_checks
