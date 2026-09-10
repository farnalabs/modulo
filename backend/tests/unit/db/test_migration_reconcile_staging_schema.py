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
# and FAR-761's 0207_collection_install_tracking chains off 0206,
# and FAR-764's 0209_community_gate chains off 0208_notification_indexes_and_constraint,
# and FAR-765's 0210_collection_install_id_columns chains off 0209 as the chain head.
_CHAIN_HEAD_MIGRATION = "0210_collection_install_id_columns"


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
