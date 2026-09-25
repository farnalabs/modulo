"""Final-state tests for the staging-schema reconciliation surface.

The migration chain was squashed into three idempotent reconciliation
migrations. The old ``0065_reconcile_staging_schema`` migration — which
detected a drifted pre-squash schema and repaired it (creating missing
``mcp_setup_tokens`` / ``lifecycle_maps``, dropping legacy ``scheduled_reports``)
— no longer exists. Its reconcile behaviour is folded into the reconciliation
chain's guarded DDL:

* ``0108_schema_org_identity`` owns ``mcp_setup_tokens`` (columns, indexes,
  tenant trigger, RLS policy),
* ``0110_schema_pipeline_runtime`` owns ``lifecycle_maps`` and
  ``scheduled_reports`` (columns, indexes, tenant triggers, RLS enablement +
  org-isolation policy).

These tests assert the reconciliation chain brings a database to that final
state: every object the old reconcile migration created is present in the new
chain's guarded DDL, and the chain has a single linear head.
"""

from pathlib import Path

from alembic.script import ScriptDirectory

_VERSIONS = Path(__file__).resolve().parents[3] / "src" / "modulo" / "db" / "migrations" / "versions"

_MIGRATION_0006 = "0108_schema_org_identity"
_MIGRATION_0008 = "0110_schema_pipeline_runtime"
_HEAD_MIGRATION = "0120_org_fk_hardening"
# Current chain head (tracks the latest migration; 0188_pipeline_run_context_defaults_default
# is main's sweep head, 0189_agent_runner_bindings (main, D6) chains off it,
# 0190_hitl_claim_context_json (main, FAR-613) re-parents onto 0189, main's
# 0191_bundled_runner_seed_backfill re-parents onto 0190_hitl_claim_context_json,
# and FAR-583's 0192_run_node_outputs re-parents onto 0191 with its
# 0193_run_node_outputs_sweep_index chaining onto 0192, and 0194_uuid_pk_server_defaults
# (FAR-718, from main) chains off 0193; improve-database's 0197/0198/0199 chain off it,
# 0201_spend_anomaly_unique_org_date (from main) chains off 0200, 0202_runs_error_code_claimed_by_indexes
# (from main) chains off 0201, 0195_hitl_claim_gate_config_json (FAR-634, from main) chains off 0202,
# 0203_triggers_add_name (FAR-681 slice 2, from main) chains off 0195, FAR-681
# slice 2 (#289/#293) added 0204_runner_probe_cache on top of 0203_triggers_add_name,
# and FAR-760's 0205_library_collection_type chains off 0204_runner_probe_cache,
# and FAR-644's 0206_deleted_defaults_signal_check chains off 0205,
# and FAR-761's 0207_collection_install_tracking chains off 0206, and #337's
# 0208_notification_indexes_and_constraint chains off 0207, 0209_collection_install_id_entity_columns
# chains off 0208, FAR-764's 0210_community_gate chains off 0209, and FAR-775's
# 0211_variant_batch_state chains off 0210, and 0212_variant_batch_state_updated_at chains off 0211,
# and FAR-788's 0213_runs_rerun_trigger_type chains off 0212, and
# 0215_drop_runs_blob_columns chains off 0213, FAR-748's 0216_audit_events_sweep_detection_index
# chains off 0215, 0217_hitl_claims_decided_by chains off 0216,
# and 0219_eval_cluster_check_constraints chains off 0218,
# and 0220_run_node_artifacts chains off 0219,
# and FAR-802's 0221_workspace_inputs_env_profiles chains off 0220,
# and FAR-794's 0222_journey_provenance chains off 0221,
# and 0223_agents_add_foreign_keys chains off 0222,
# and 0224_agents_add_indexes chains off 0223,
# and 0225_agents_constraints_rls chains off 0224,
# and 0226_agents_json_to_jsonb chains off 0225,
# and 0227_env_profiles_initialisation_strategy_check chains off 0226_agents_json_to_jsonb,
# and 0228_drop_scalar_agent_command chains off 0227_env_profiles_initialisation_strategy_check,
# and FAR-802's 0229_add_workspace_inputs_count chains off 0228_drop_scalar_agent_command,
# and 0230_token_families_refresh_grace chains off 0229_add_workspace_inputs_count,
# and 0231_token_families_reuse_replay_count chains off 0230_token_families_refresh_grace,
# and 0232_seed_modulo_sentinel_organisation chains off 0231_token_families_reuse_replay_count,
# and 0233_add_updated_at_audit_to_organisations chains off 0232_seed_modulo_sentinel_organisation,
# and 0234_add_organisations_indexes chains off 0233_add_updated_at_audit_to_organisations,
# and 0235_promote_organisations_json_to_jsonb chains off 0234_add_organisations_indexes,
# and 0236_add_organisations_constraints chains off 0235_promote_organisations_json_to_jsonb,
# and 0237_fix_token_family_org_nullable chains off 0236_add_organisations_constraints,
# and FAR-801's 0238_workspace_input_drift_and_audit chains off
# 0237_fix_token_family_org_nullable, and 0239_revert_organisations_audit_drift
# chains off 0238_workspace_input_drift_and_audit, and 0240_reinstate_organisations_audit_columns
# chains off 0239_revert_organisations_audit_drift, and 0241_journey_dismissal (FAR-795)
# chains off 0240_reinstate_organisations_audit_columns, and 0242_org_mint_budget_usage (FAR-795)
# chains off 0241_journey_dismissal, and 0243_remove_organisations_audit_drift (FAR-811)
# chains off 0242_org_mint_budget_usage, and 0244_pipeline_stdout_retention_config (FAR-811)
# chains off 0243_remove_organisations_audit_drift, and 0245_drop_organisations_created_by_fk
# (PR #559) chains off 0244_pipeline_stdout_retention_config, and 0246_sso_join_gate (FAR-855)
# chains off 0245_drop_organisations_created_by_fk, and 0247_sso_presets (FAR-853)
# chains off 0246_sso_join_gate, and 0248_agent_schema_profile (FAR-900)
# chains off 0247_sso_presets, and 0249_validation_level (FAR-935)
# chains off 0248_agent_schema_profile, and 0250_eval_policy_gate (FAR-1060)
# chains off 0249_validation_level, and 0251_schema_enforcement_telemetry (FAR-902)
# chains off 0250_eval_policy_gate, and 0252_enforcement_daily_facts (FAR-902) chains
# off 0251_schema_enforcement_telemetry, and 0253_runs_enforcement_mode_outcome (FAR-902)
# chains off 0252_enforcement_daily_facts, and 0254_eval_backfill_cutover (FAR-1100)
# chains off 0253_runs_enforcement_mode_outcome, and
# 0255_trigger_event_value_filter_label (FAR-1144) chains off
# 0254_eval_backfill_cutover, and 0256_pipeline_max_autonomy_level
# (FAR-1163) chains off 0255, and 0257_rename_remy_to_assistant
# (FAR-1196 Tier 3) chains off 0256_pipeline_max_autonomy_level, and
# 0258_pipeline_accountability_owners (FAR-1161) chains off
# 0257_rename_remy_to_assistant, and 0259_pipeline_snapshot_max_autonomy_check
# (FAR-1223) chains off 0258_pipeline_accountability_owners as the chain head.
_CHAIN_HEAD_MIGRATION = "0259_pipeline_snapshot_max_autonomy_check"


def _source(name: str) -> str:
    path = _VERSIONS / f"{name}.py"
    assert path.exists(), f"Migration file missing: {path}"
    return path.read_text(encoding="utf-8")


def _script() -> ScriptDirectory:
    return ScriptDirectory(str(_VERSIONS.parent))


class TestReconciliationChain:
    def test_single_linear_head(self) -> None:
        heads = _script().get_heads()
        assert heads == [_CHAIN_HEAD_MIGRATION], f"expected a single head, got {heads}"


class TestMcpSetupTokensSurface:
    def test_0006_owns_mcp_setup_tokens_columns(self) -> None:
        source = _source(_MIGRATION_0006)
        for column in ("resource_id", "token_hash", "created_by", "organisation_id", "expires_at"):
            assert f'ADD COLUMN IF NOT EXISTS "{column}"' in source, f"0006 missing mcp_setup_tokens.{column}"

    def test_0006_creates_mcp_setup_tokens_indexes(self) -> None:
        source = _source(_MIGRATION_0006)
        assert "ix_mcp_setup_tokens_organisation_id" in source
        assert "ix_mcp_setup_tokens_resource_id" in source

    def test_0006_creates_mcp_setup_tokens_created_by_fk(self) -> None:
        source = _source(_MIGRATION_0006)
        assert "fk_mcp_setup_tokens_created_by" in source

    def test_0006_installs_mcp_setup_tokens_tenant_trigger(self) -> None:
        source = _source(_MIGRATION_0006)
        assert "trg_mcp_setup_tokens_created_by_tenant" in source

    def test_0006_enables_rls_and_org_isolation_policy(self) -> None:
        source = _source(_MIGRATION_0006)
        assert "mcp_setup_tokens ENABLE ROW LEVEL SECURITY" in source
        assert "CREATE POLICY rls_org_isolation ON public.mcp_setup_tokens" in source


class TestLifecycleMapsSurface:
    def test_0008_owns_lifecycle_maps_columns(self) -> None:
        source = _source(_MIGRATION_0008)
        for column in ("id", "organisation_id", "account_id", "owner_team_id", "visibility", "version", "content_json"):
            assert f'ADD COLUMN IF NOT EXISTS "{column}"' in source, f"0008 missing lifecycle_maps.{column}"

    def test_0008_creates_lifecycle_maps_indexes(self) -> None:
        source = _source(_MIGRATION_0008)
        assert "ix_lifecycle_maps_organisation_id" in source
        assert "ix_lifecycle_maps_account_id" in source

    def test_0008_installs_lifecycle_maps_tenant_triggers(self) -> None:
        source = _source(_MIGRATION_0008)
        assert "trg_lifecycle_maps_account_id_tenant" in source
        assert "trg_lifecycle_maps_owner_team_id_tenant" in source

    def test_0008_enables_rls_and_org_isolation_policy(self) -> None:
        source = _source(_MIGRATION_0008)
        assert "lifecycle_maps ENABLE ROW LEVEL SECURITY" in source
        assert "CREATE POLICY rls_org_isolation ON public.lifecycle_maps" in source


class TestScheduledReportsSurface:
    def test_0008_owns_scheduled_reports_columns(self) -> None:
        source = _source(_MIGRATION_0008)
        for column in ("id", "organisation_id", "report_type", "cron_expression", "created_by", "active"):
            assert f'ADD COLUMN IF NOT EXISTS "{column}"' in source, f"0008 missing scheduled_reports.{column}"

    def test_0008_creates_scheduled_reports_indexes(self) -> None:
        source = _source(_MIGRATION_0008)
        assert "ix_scheduled_reports_organisation_id" in source
        assert "ix_scheduled_reports_report_type" in source
        assert "ix_scheduled_reports_created_by" in source

    def test_0008_installs_scheduled_reports_tenant_trigger(self) -> None:
        source = _source(_MIGRATION_0008)
        assert "trg_scheduled_reports_created_by_tenant" in source

    def test_0008_enables_rls_and_org_isolation_policy(self) -> None:
        source = _source(_MIGRATION_0008)
        assert "scheduled_reports ENABLE ROW LEVEL SECURITY" in source
        assert "CREATE POLICY rls_org_isolation ON public.scheduled_reports" in source


class TestOrgFkHardeningMigration:
    def test_uses_cascade_not_restrict(self) -> None:
        source = _source(_HEAD_MIGRATION)
        assert "ON DELETE CASCADE" in source
        assert "ON DELETE RESTRICT" not in source

    def test_upgrade_is_postgres_guarded(self) -> None:
        source = _source(_HEAD_MIGRATION)
        assert 'op.get_context().dialect.name == "postgresql"' in source

    def test_is_child_of_0119(self) -> None:
        source = _source(_HEAD_MIGRATION)
        assert 'down_revision = "0119_analytics_batch_id"' in source
