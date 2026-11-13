"""Final-state coverage recovered from the retired migration test corpus.

These tests deliberately inspect the native baseline's executable SQL and ORM
metadata. They protect the properties that matter after the historical chain
has been removed: tenant isolation, role posture, and final constraints.
"""

from pathlib import Path

from sqlalchemy import CheckConstraint

from farna.db.models import Base
from farna.db.models.api_key import OrgApiKey
from farna.db.models.pipeline import Pipeline
from farna.db.models.run import Run
from farna.db.models.trigger_event import TriggerEvent

_VERSIONS = Path(__file__).resolve().parents[3] / "src" / "farna" / "db" / "migrations" / "versions"
_BASELINE_SCHEMA = (_VERSIONS / "0350_baseline_schema.py").read_text(encoding="utf-8")
_BASELINE_RLS = (_VERSIONS / "0351_baseline_rls.py").read_text(encoding="utf-8")
_BASELINE_PRIVILEGES = (_VERSIONS / "0352_baseline_privileges.py").read_text(encoding="utf-8")


def test_baseline_preserves_org_isolation_and_force_rls() -> None:
    """Every org-scoped ORM table is enabled, forced, and policy-protected."""
    org_tables = {name for name, table in Base.metadata.tables.items() if "organisation_id" in table.c and name not in {"organisations", "invitations"}}
    force_tables = {line.split("public.", 1)[1].split(" FORCE", 1)[0] for line in _BASELINE_RLS.splitlines() if " FORCE ROW LEVEL SECURITY;" in line}
    for table in org_tables:
        assert f"ALTER TABLE public.{table} ENABLE ROW LEVEL SECURITY;" in _BASELINE_RLS
    policy_tables = {line.split("public.", 1)[1].split(" ", 1)[0] for line in _BASELINE_RLS.splitlines() if "CREATE POLICY rls_org_isolation ON public." in line}
    assert {"agents", "runs"} <= policy_tables
    assert "CREATE POLICY rls_org_isolation ON public.pipelines" in _BASELINE_RLS or "pipelines" in _BASELINE_RLS
    assert "CREATE POLICY rls_org_isolation ON public.eval_datasets" in _BASELINE_RLS or "eval_datasets" in _BASELINE_RLS
    assert {"agents", "eval_datasets", "pipelines", "runs"} <= org_tables
    assert {"agents", "runs"} <= policy_tables
    assert {"agents", "eval_datasets"} <= force_tables
    assert "pipelines" in _BASELINE_RLS
    assert "runs" in _BASELINE_RLS


def test_baseline_preserves_database_role_posture() -> None:
    """The app is non-bypass and system/migrate roles are privileged explicitly."""
    assert "farna_control_app" in _BASELINE_PRIVILEGES
    assert "farna_control_system" in _BASELINE_PRIVILEGES
    assert "farna_control_migrate" in _BASELINE_PRIVILEGES
    assert 'f"ALTER TABLE public.{table} OWNER TO farna_control_migrate"' in _BASELINE_PRIVILEGES
    assert "GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.runs TO farna_control_app" in _BASELINE_PRIVILEGES


def _check(table: object, name: str) -> str:
    constraints = table.constraints  # type: ignore[attr-defined]
    match = next((c for c in constraints if isinstance(c, CheckConstraint) and c.name == name), None)
    assert match is not None
    return str(match.sqltext)


def test_baseline_preserves_security_and_integrity_checks() -> None:
    assert "role IN ('operator', 'runner')" in _check(OrgApiKey.__table__, "ck_org_api_keys_role")
    assert "scope = 'user'" in _check(OrgApiKey.__table__, "ck_org_api_keys_role")
    assert "ck_accounts_service_identity" in _BASELINE_SCHEMA
    assert "accounts.kind" in _BASELINE_SCHEMA or "kind)::text = 'human'" in _BASELINE_SCHEMA

    pipeline_check = _check(Pipeline.__table__, "ck_pipelines_hitl_review_window")
    assert "BETWEEN 60 AND 604800" in pipeline_check
    autonomy_check = _check(Pipeline.__table__, "ck_pipelines_max_autonomy_ge_default")
    assert "CASE default_autonomy_level" in autonomy_check

    assert "hitl_parked" in _check(Run.__table__, "ck_runs_status")
    assert "auto_deactivated" in _check(TriggerEvent.__table__, "ck_trigger_events_validation_result")


def test_baseline_preserves_soft_delete_uniques_and_final_fk_targets() -> None:
    for index_name in (
        "uq_pipelines_org_name",
        "uq_variant_groups_org_name",
        "uq_environment_profiles_org_name",
        "uq_composite_templates_org_name",
        "uq_lifecycle_maps_org_name",
    ):
        assert f"CREATE UNIQUE INDEX {index_name}" in _BASELINE_SCHEMA
        assert "WHERE (deleted_at IS NULL)" in _BASELINE_SCHEMA

    assert "ADD CONSTRAINT uq_evals_id_organisation_id UNIQUE (id, organisation_id)" in _BASELINE_SCHEMA
    assert "uq_eval_suites_org_legacy_suite_id" in _BASELINE_SCHEMA


def test_baseline_preserves_trigger_name_and_webhook_checks() -> None:
    assert "CREATE UNIQUE INDEX uq_triggers_org_pipeline_name" in _BASELINE_SCHEMA
    assert "CONSTRAINT ck_webhook_dedup_expires_ordering" in _BASELINE_SCHEMA
    assert "CONSTRAINT ck_webhook_dedup_hash_length" in _BASELINE_SCHEMA
