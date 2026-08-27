"""Unit tests for migration 0147_json_to_jsonb_standardize (FAR-403).

The migration promotes ~66 ``(table, column)`` pairs still typed ``json`` to
``jsonb``. The original version did it with one blocking ``ALTER COLUMN ... TYPE``
statement per pair, each taking an ACCESS EXCLUSIVE lock + full-table rewrite —
which hangs forever on hot, continually written tables and wedges deploys (the
same bug that blocked ``runs`` in 0129_runs_json_to_jsonb).

These tests are **structural**: they load the migration module, pin the
``_JSON_TO_JSONB`` contract (an accidental change to the target columns would
silently convert the wrong set) and assert the emitted SQL is the non-blocking,
resumable design (temp-column ADD + bounded, ctid-batched UPDATE backfill)
rather than the old blocking ALTER COLUMN TYPE form. They run without a database.

Gap: there is no live-Postgres integration test for 0147 asserting the columns
are ``jsonb`` and that running the migration twice is a no-op. That requires the
Testcontainers integration harness; it is out of scope here and not run by this
unit test.
"""

import importlib.util
from pathlib import Path
from types import ModuleType

_VERSIONS = Path(__file__).resolve().parents[3] / "src" / "modulo" / "db" / "migrations" / "versions"
_MIGRATION_NAME = "0147_json_to_jsonb_standardize"
_MIGRATION_PATH = _VERSIONS / f"{_MIGRATION_NAME}.py"

# The (table, column) pairs this migration promotes. Pinned explicitly so a
# typo in the migration (e.g. targeting the wrong column) is caught by the unit
# test rather than silently converting the wrong set in a live deploy.
_EXPECTED_JSON_TO_JSONB: tuple[tuple[str, str], ...] = (
    ("accounts", "preferences"),
    ("agents", "agent_commands"),
    ("agents", "prompt_version_history"),
    ("agents", "connector_type_refs"),
    ("agents", "required_environment_capabilities"),
    ("agents", "evals"),
    ("agents", "retry_policy"),
    ("audit_events", "payload_json"),
    ("composite_templates", "sub_pipeline_graph_json"),
    ("composite_templates", "parameter_ports_json"),
    ("connector_instances", "config_json"),
    ("connector_instances", "allowed_operations"),
    ("environment_profiles", "capabilities_json"),
    ("environment_profiles", "config_json"),
    ("environment_profiles", "secret_refs_json"),
    ("error_events", "context_json"),
    ("error_forwarder_configs", "config_json"),
    ("eval_cases", "input_payload"),
    ("eval_cases", "expected_output"),
    ("eval_definitions", "config_json"),
    ("feedback_records", "rejected_output"),
    ("hitl_claims", "decision_payload"),
    ("library_primitives", "tags"),
    ("library_primitives", "content_json"),
    ("library_sync_state", "manifest_json"),
    ("library_sync_state", "catalog_json"),
    ("lifecycle_maps", "content_json"),
    ("model_backends", "default_params"),
    ("model_backends", "fallback_backend_ids"),
    ("notification_endpoints", "events"),
    ("organisations", "settings_json"),
    ("organisations", "otel_config_json"),
    ("organisations", "export_bundle_json"),
    ("organisations", "guardrail_pins_json"),
    ("parameter_schemas", "parameters"),
    ("parameter_sets", "values"),
    ("pipelines", "run_context_defaults"),
    ("pipelines", "rate_limit_config"),
    ("pipeline_snapshots", "graph_json"),
    ("pipeline_snapshots", "connector_bindings_json"),
    ("pipeline_snapshots", "schema_pins_json"),
    ("pipeline_snapshots", "prompt_pins_json"),
    ("pipeline_snapshots", "model_backend_pins_json"),
    ("pipeline_snapshots", "composite_bindings_json"),
    ("pipeline_snapshots", "parameter_bindings_json"),
    ("pipeline_snapshots", "guardrail_pins_json"),
    ("pipeline_snapshots", "config_json"),
    ("pipeline_snapshots", "run_context_defaults"),
    ("chat_messages", "tool_calls_json"),
    ("chat_messages", "tool_results_json"),
    ("remy_skills", "triggers"),
    ("scheduled_reports", "config_json"),
    ("scheduled_reports", "recipient_config"),
    ("schema_versions", "definition_json"),
    ("sso_providers", "group_mappings"),
    ("system_config", "value"),
    ("teams", "notification_endpoints"),
    ("teams", "settings"),
    ("triggers", "config_json"),
    ("variant_groups", "variants"),
    ("saved_views", "filters"),
    ("saved_views", "columns"),
    ("webhook_payloads", "raw_payload"),
    ("workspace_leases", "resource_usage_json"),
    ("workspace_leases", "output_artifact_refs_json"),
    ("feature_flag_catalog", "depends_on"),
)


def _load_migration() -> ModuleType:
    assert _MIGRATION_PATH.exists(), f"Migration file missing: {_MIGRATION_PATH}"
    spec = importlib.util.spec_from_file_location(f"migration_{_MIGRATION_NAME}", _MIGRATION_PATH)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _source_code() -> str:
    """Return the migration's executable code, minus the module docstring.

    The docstring legitimately quotes the old blocking ``ALTER COLUMN ... TYPE``
    form to explain why it is being rewritten, so assertions on the SQL emitted
    by the migration must not match that historical prose.
    """
    source = _MIGRATION_PATH.read_text(encoding="utf-8")
    parts = source.split('"""', 2)
    return parts[2] if len(parts) >= 3 else source


def test_json_to_jsonb_contract_is_unchanged() -> None:
    module = _load_migration()
    assert module._JSON_TO_JSONB == _EXPECTED_JSON_TO_JSONB


def test_metadata_unchanged() -> None:
    module = _load_migration()
    assert module.revision == _MIGRATION_NAME
    assert module.down_revision == "0146_extend_runs_status_cost_ceiling"
    assert module.branch_labels is None
    assert module.depends_on is None


def test_upgrade_and_downgrade_are_callable() -> None:
    module = _load_migration()
    assert callable(module.upgrade)
    assert callable(module.downgrade)


def test_upgrade_does_not_emit_blocking_alter_column_type() -> None:
    """The rewritten migration must not use the blocking ALTER COLUMN TYPE form.

    A regression back to ``ALTER COLUMN "preferences" TYPE jsonb`` would re-
    introduce the ACCESS EXCLUSIVE lock + full-table rewrite that hung deploys.
    """
    assert '" TYPE "' not in _source_code(), "blocking ALTER COLUMN ... TYPE on json columns is forbidden"


def test_upgrade_uses_non_blocking_temp_column_and_batch_backfill() -> None:
    """The rewrite must use the additive temp-column + bounded batch UPDATE design."""
    code = _source_code()
    assert 'ADD COLUMN "' in code, "must ADD a temp column rather than ALTER TYPE"
    assert "update(" in code, "backfill must use an UPDATE statement"
    assert "_BATCH_SIZE" in code, "backfill must be bounded (row-level, no table lock)"
    assert ".cast(" in code, "backfill must copy via the lossless ::cast"
    assert "ctid" in code, "backfill must use the ctid-batched bounded subselect"
    assert "table_name" in code, "helpers must be generalised to accept a per-table argument"


def test_helpers_use_dedicated_autocommit_connection() -> None:
    """The migration must run SQL on a dedicated autocommit connection.

    Alembic wraps each migration in ``context.begin_transaction()``. Calling an
    explicit ``.commit()`` on ``op.get_bind()`` closes that transaction and
    breaks every subsequent statement — the root cause of the CI migration break
    (``Can't operate on closed transaction inside context manager``) this fix
    addresses. The migration must instead obtain its own autocommit connection
    from the engine so per-statement commits don't fight alembic's transaction.
    """
    code = _source_code()
    assert ".commit(" not in code, "no explicit .commit() on alembic's connection"
    assert "AUTOCOMMIT" in code, "must use an isolation_level=AUTOCOMMIT connection"
    assert "isolation_level=" in code, "must set isolation_level on the connection"
    assert "engine.connect(" in code, "must connect via the engine, independent of alembic"
