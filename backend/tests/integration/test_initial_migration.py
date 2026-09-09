import uuid
from typing import Any

import pytest
from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from sqlalchemy import (
    JSON,
    UUID,
    CheckConstraint,
    Column,
    ForeignKey,
    ForeignKeyConstraint,
    Integer,
    MetaData,
    Table,
    inspect,
    select,
    update,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncEngine
from sqlalchemy.orm import Session

from modulo.db.models import Base, LibraryPrimitive, Organisation, Run
from tests.factories import (
    AccountFactory,
    OrganisationFactory,
    PipelineFactory,
    PipelineSnapshotFactory,
    RunFactory,
)

pytestmark = pytest.mark.integration


# ---------------------------------------------------------------------------
# Reasoned schema-parity ignore entries (FAR-583 qa M17 rewrite).
#
# The old blanket classes (ALL CheckConstraint divergence both directions,
# ANY JSONB<->JSON modify_type, bare-column-set FK matching, blanket
# audit-chain remove_columns) hid genuine drift. Every entry below carries a
# one-line reason and an expiry tag:
#   * `# expires: B2b`  — the drift disappears when migration 0192 drops the
#     legacy runs blob columns; remove the entry then (a stale entry would
#     keep hiding drift and must fail loudly instead).
#   * `# permanent (documented repo divergence)` — the repo's multi-backend
#     convention or DB-trigger-maintained surface.
# GENUINE drift stays listed as a KNOWN GAP with `# tracked: FAR-583
# follow-up` (the Conductor raises the ticket). Anything NOT listed fails.
# ---------------------------------------------------------------------------

# Multi-backend JSON parity: migrations create JSONB on Postgres; the ORM maps
# generic JSON for SQLite/MariaDB parity (repo-wide convention — e.g. the
# run_node_outputs model docstring). Keyed table -> columns, enumerated from
# compare_metadata (2026-09-06). A NEW JSONB(DB) -> JSON(ORM) divergence NOT
# listed here FAILS the test.
_JSONB_DB_TO_JSON_ORM: dict[str, frozenset[str]] = {
    # permanent (documented repo divergence) — multi-backend JSON parity
    "accounts": frozenset({"preferences"}),
    "agents": frozenset(
        {
            "agent_commands",
            "prompt_version_history",
            "connector_type_refs",
            "required_environment_capabilities",
            "evals",
            "retry_policy",
        }
    ),
    "audit_events": frozenset({"payload_json"}),
    "chat_messages": frozenset({"tool_calls_json", "tool_results_json"}),
    "composite_templates": frozenset({"sub_pipeline_graph_json", "parameter_ports_json"}),
    "connector_instances": frozenset({"config_json", "allowed_operations"}),
    "environment_profiles": frozenset({"capabilities_json", "config_json", "secret_refs_json"}),
    "error_events": frozenset({"context_json"}),
    "error_forwarder_configs": frozenset({"config_json"}),
    "eval_cases": frozenset({"input_payload", "expected_output"}),
    "eval_definitions": frozenset({"config_json"}),
    "eval_suites": frozenset({"eval_definition_ids"}),
    "feature_flag_catalog": frozenset({"depends_on"}),
    "feedback_records": frozenset({"rejected_output", "correction_state"}),
    # hitl_claims: jsonb in the migrations; generic JSON in the ORM for
    # SQLite/MariaDB parity — the same pattern as decision_payload /
    # context_json. 0195_hitl_claim_gate_config_json (FAR-634) adds
    # gate_config_json as JSONB on Postgres and generic JSON in the ORM.
    "hitl_claims": frozenset({"decision_payload", "context_json", "gate_config_json"}),
    "library_primitives": frozenset({"tags", "content_json"}),
    "library_sync_state": frozenset({"manifest_json", "catalog_json"}),
    "lifecycle_maps": frozenset({"content_json"}),
    "metrics_staging": frozenset({"payload"}),
    "model_backends": frozenset({"default_params", "fallback_backend_ids"}),
    "notification_endpoints": frozenset({"events"}),
    "organisations": frozenset({"settings_json", "otel_config_json", "export_bundle_json", "guardrail_pins_json"}),
    "parameter_schemas": frozenset({"parameters"}),
    "parameter_sets": frozenset({"values"}),
    "pipeline_snapshots": frozenset(
        {
            "graph_json",
            "connector_bindings_json",
            "schema_pins_json",
            "prompt_pins_json",
            "model_backend_pins_json",
            "composite_bindings_json",
            "parameter_bindings_json",
            "guardrail_pins_json",
            "config_json",
            "run_context_defaults",
        }
    ),
    "pipelines": frozenset({"run_context_defaults", "rate_limit_config"}),
    "remy_skills": frozenset({"triggers"}),
    # permanent (documented repo divergence) — the new table keeps its three
    # blob columns after B2b (they ARE the store once the legacy columns go).
    "run_node_outputs": frozenset({"outputs_json", "node_telemetry_json", "raw_output_markers"}),
    # runs: the three legacy blob columns EXPIRE at B2b (dropped by 0192);
    # the rest are the multi-backend parity convention.
    "runs": frozenset(
        {
            "outputs_json",  # expires: B2b
            "node_telemetry_json",  # expires: B2b
            "raw_output_markers",  # expires: B2b
            "cost_breakdown",
            "node_token_usage",
            "input_payload",
            "run_classification",
            "blocked_partial_summary",
            "guardrail_summary_json",
            "work_item_refs",
            "variant_config_snapshot",
        }
    ),
    "saved_views": frozenset({"filters", "columns"}),
    "scheduled_reports": frozenset({"config_json", "recipient_config"}),
    "schema_versions": frozenset({"definition_json"}),
    "sso_providers": frozenset({"group_mappings"}),
    "system_config": frozenset({"value"}),
    "teams": frozenset({"notification_endpoints", "settings"}),
    "triggers": frozenset({"config_json"}),
    "variant_groups": frozenset({"variants"}),
    "webhook_payloads": frozenset({"raw_payload"}),
    "workspace_leases": frozenset({"resource_usage_json", "output_artifact_refs_json"}),
}
# Per-column B2b expiry: these parity entries MUST be removed together with
# the dropped legacy columns (a stale entry keeps hiding drift after B2b).
_JSONB_DB_TO_JSON_ORM_EXPIRES_B2B: frozenset[tuple[str, str]] = frozenset(
    {
        ("runs", "outputs_json"),  # expires: B2b
        ("runs", "node_telemetry_json"),  # expires: B2b
        ("runs", "raw_output_markers"),  # expires: B2b
    }
)

# CHECK constraints that exist ONLY in migrations — added post-hoc by 0157
# (strict numeric guards) and 0165 (counter/vocabulary guards) as DB-side
# backstops; the ORM deliberately declares the portable subset or none (repo
# parity rule: dialect-specific SQL like jsonb_typeof never belongs in the
# ORM). Keyed table -> constraint names.
# permanent (documented repo divergence)
_MIGRATION_OWNED_CHECKS: dict[str, frozenset[str]] = {
    "audit_chain_heads": frozenset({"ck_audit_event_event_count"}),
    "cost_components": frozenset({"ck_cost_component_sort_order"}),
    "error_groups": frozenset({"ck_error_group_count"}),
    "error_notification_rules": frozenset(
        {
            "ck_error_notification_rule_cooldown",
            "ck_error_notification_rule_min_count",
            "ck_error_notification_rule_window",
        }
    ),
    "eval_definitions": frozenset({"ck_eval_definitions_pass_threshold"}),
    "eval_results": frozenset({"ck_eval_results_run_xor_suite"}),
    "eval_suites": frozenset({"ck_eval_suites_minimum_delta"}),
    "journeys": frozenset({"ck_journey_run_count"}),
    "library_primitives": frozenset({"ck_library_primitive_download_count", "ck_library_primitive_review_count"}),
    "notification_delivery_log": frozenset({"ck_notification_delivery_attempt_count"}),
    "notification_endpoints": frozenset({"ck_notification_endpoint_dead_letter"}),
    "org_daily_run_counts": frozenset({"ck_daily_run_count_run_count"}),
    "run_daily_facts": frozenset({"ck_run_daily_facts_status", "ck_run_daily_facts_trigger_type"}),
    # 0192: the STRICT dialect-specific meta-shape CHECK (jsonb_typeof on PG /
    # json_type on SQLite); the ORM declares the PORTABLE subset with a
    # different name (ck_run_node_outputs_meta_present) — matched below.
    # permanent (documented repo divergence)
    "run_node_outputs": frozenset({"ck_run_node_outputs_meta_shape"}),
    "runs": frozenset({"ck_run_claim_count", "ck_run_node_attempt_count"}),
    "spend_anomalies": frozenset({"ck_spend_anomalies_percent_above"}),
    "suite_runs": frozenset(
        {
            "ck_eval_suite_run_excluded_cases",
            "ck_eval_suite_run_failed_cases",
            "ck_eval_suite_run_failed_le_total",
            "ck_eval_suite_run_passed_cases",
            "ck_eval_suite_run_passed_le_total",
            "ck_eval_suite_run_total_cases",
            "ck_eval_suite_runs_claimed_cost",
        }
    ),
    "teams": frozenset({"ck_teams_daily_spend_limit"}),
    "variant_groups": frozenset({"ck_variant_groups_max_concurrent_runs", "ck_variant_groups_run_count"}),
}

# ORM-declared CHECKs whose DB-side counterpart is the strict dialect twin —
# the ORM's portable subset appears as add_constraint. PER ENTRY reason:
_ORM_CHECK_DIVERGENCE: frozenset[tuple[str, str]] = frozenset(
    {
        # run_node_outputs: the ORM's portable meta-present subset vs the
        # migration's strict jsonb_typeof shape (repo parity rule).
        # permanent (documented repo divergence)
        ("run_node_outputs", "ck_run_node_outputs_meta_present"),
        # deleted_defaults: the ORM declares this signal guard but NO
        # migration has created it — genuine drift, known gap.
        # tracked: FAR-583 follow-up
        ("deleted_defaults", "ck_deleted_defaults_signal_nonempty"),
    }
)

# ORM-declared graph-consistency FKs to nodes.id that no migration has
# created yet — genuine drift, KNOWN GAP (pre-existing; the ORM is stricter
# than the migrated schema).
# tracked: FAR-583 follow-up
_NODES_ID_FK_KNOWN_GAPS: frozenset[tuple[str, str]] = frozenset(
    {
        ("eval_definitions", "node_id"),
        ("eval_results", "node_id"),
        ("node_observations", "node_id"),
        ("pipeline_edges", "source_node_id"),
        ("pipeline_edges", "target_node_id"),
        ("run_evidence", "node_id"),
        ("snapshot_schema_pins", "node_id"),
    }
)

# Tables that exist in the DB but deliberately have NO ORM model.
# permanent (documented repo divergence)
_NO_ORM_MODEL_TABLES: frozenset[str] = frozenset(
    {
        # Runtime-managed LangGraph checkpoint tables (ModuloPostgresSaver
        # setup at startup, raw SQL) — not Alembic migrations, not ORM.
        "checkpoints",
        "checkpoint_blobs",
        "checkpoint_writes",
        "checkpoint_migrations",
        # The reconciliation chain's raw-SQL run-number counter path; the ORM
        # model was deleted (FAR-253 dead-code cleanup).
        "run_number_counters",
        # FAR-583 ops/remediation quarantine side table (migration 0192):
        # written by migrations + the sweep, read by ops SQL only.
        "run_node_outputs_quarantine",
        # FAR-590 bundled-runner seed backfill scratch table (migration 0191):
        # captures the pre-repoint profile rows so the DOWNGRADE can revert
        # exactly those rows; deliberately kept after upgrade (dropped only by
        # the downgrade), never mapped by the ORM.
        "_migration_0191_repoint_state",
    }
)

# Audit-chain columns the DB triggers own (0108): present in the DB, absent
# from ORM metadata, never read/written by the ORM directly.
# Keyed table -> columns. permanent (documented repo divergence)
_AUDIT_CHAIN_COLUMNS: dict[str, frozenset[str]] = {
    "agents": frozenset({"created_by", "updated_by", "deleted_by", "deleted_at"}),
    "connector_instances": frozenset({"created_by", "updated_by", "deleted_by", "deleted_at"}),
    "scheduled_reports": frozenset({"created_by", "updated_by", "deleted_by", "deleted_at"}),
}
# Their FKs to accounts.id (same tables; deleted_at carries no FK).
# permanent (documented repo divergence)
_AUDIT_CHAIN_FK_COLUMNS: dict[str, frozenset[str]] = {
    "agents": frozenset({"created_by", "updated_by", "deleted_by"}),
    "connector_instances": frozenset({"created_by", "updated_by", "deleted_by"}),
    "scheduled_reports": frozenset({"created_by", "updated_by", "deleted_by"}),
}


def _is_benign_migration_managed(diff: tuple[Any, ...]) -> bool:
    """Classify ONE compare_metadata diff against the reasoned entries above.

    Non-listed drift returns False -> the test fails (the failure path is
    intact: remove any entry and its diffs start failing this test).
    """
    # ``modify_comment``/``add_table_comment`` arrive as single-element
    # lists wrapping the ``(kind, ...)`` tuple; plain tuple diffs carry the
    # kind at index 0. Normalise both before matching.
    inner = diff[0] if isinstance(diff, list) and diff else diff
    kind = inner[0] if isinstance(inner, (tuple, list)) and inner else None

    if kind in ("remove_index", "add_index"):
        # Performance indexes created by migrations (e.g.
        # ``ix_agent_account_id``) that the ORM models do not declare — extra
        # DB indexes never break ORM reads/writes.
        # permanent (documented repo divergence)
        return True
    if kind in ("modify_comment", "add_table_comment"):
        # Column/table comments declared on ORM models but not mirrored in
        # every migration — cosmetic.
        # permanent (documented repo divergence)
        return True
    if kind == "remove_table":
        # Tables with no ORM model — per-table reasoned entries above.
        return inner[1].name in _NO_ORM_MODEL_TABLES
    if kind == "add_column":
        # modulo_journey_facts.updated_at: the ORM's TimestampMixin declares
        # updated_at but no migration ever created the column; created_at is
        # the write instant.
        # permanent (documented repo divergence)
        return inner[2] == "modulo_journey_facts" and inner[3].name == "updated_at"
    if kind == "remove_column":
        # Audit-chain columns the DB triggers own (0108) — per-table entries.
        return inner[2] in _AUDIT_CHAIN_COLUMNS and inner[3].name in _AUDIT_CHAIN_COLUMNS[inner[2]]
    if kind == "remove_fk":
        # The audit-chain FKs on the trigger-maintained columns — per-table.
        return inner[1].table.name in _AUDIT_CHAIN_FK_COLUMNS and bool(
            {col.name for col in inner[1].columns} <= _AUDIT_CHAIN_FK_COLUMNS[inner[1].table.name]
        )
    if kind == "add_fk":
        # Graph-consistency FKs to nodes.id the ORM declares but no migration
        # has created yet — per-(table, column) KNOWN GAP entries.
        table = inner[1].table.name
        col_names = {col.name for col in inner[1].columns}
        return any(
            known_table == table and col_names == {known_col} for (known_table, known_col) in _NODES_ID_FK_KNOWN_GAPS
        )
    if kind == "remove_constraint":
        # CHECK guards that exist ONLY in migrations (0157/0165/0192) —
        # per-(table, constraint) entries.
        constraint = inner[1]
        table_checks = _MIGRATION_OWNED_CHECKS.get(constraint.table.name, frozenset())
        return constraint.name in table_checks
    if kind == "add_constraint":
        # ORM CHECKs whose DB counterpart is the strict dialect twin (or a
        # known-gap missing migration) — per-(table, constraint) entries.
        constraint = inner[1]
        return (constraint.table.name, constraint.name) in _ORM_CHECK_DIVERGENCE
    if kind == "modify_type":
        # Multi-backend JSON parity, per-(table, column) entries: JSONB in the
        # DB, generic JSON in the ORM.
        if isinstance(inner[5], JSONB) and isinstance(inner[6], JSON):
            columns = _JSONB_DB_TO_JSON_ORM.get(inner[2], frozenset())
            return inner[3] in columns
        return False
    return False


async def test_initial_migration_creates_domain_tables(db_engine: AsyncEngine) -> None:
    async with db_engine.connect() as connection:
        table_names = await connection.run_sync(lambda sync_connection: inspect(sync_connection).get_table_names())

    assert {
        "agents",
        "hitl_claims",
        "org_api_keys",
        "organisations",
        "pipeline_edges",
        "pipeline_snapshots",
        "pipelines",
        "runs",
    } <= set(table_names)


async def test_migrated_schema_matches_orm_metadata(db_engine: AsyncEngine) -> None:
    """The migrated schema matches ORM metadata, modulo REASONED divergence.

    The parity ignore-list is per-table/per-constraint (FAR-583 qa M17
    rewrite — the old blanket classes hid genuine drift). Every entry carries
    a one-line reason and an expiry tag:

    * ``# expires: B2b`` — the drift disappears when migration 0192 drops the
      legacy ``runs`` blob columns; the entry must be removed then (a stale
      entry keeps hiding drift and must fail loudly instead).
    * ``# permanent (documented repo divergence)`` — the repo's multi-backend
      convention (JSONB in migrations / generic JSON in the ORM) or
      DB-trigger-maintained columns.

    GENUINE drift that stays listed as a KNOWN GAP (tracked, not silently
    ignored): ORM-declared graph-consistency FKs to ``nodes.id`` and the
    ``deleted_defaults`` CHECK that no migration has created yet
    (``# tracked: FAR-583 follow-up`` — raised with the Conductor). Anything
    NOT listed fails the test.
    """
    async with db_engine.connect() as connection:
        differences = await connection.run_sync(
            lambda sync_connection: compare_metadata(MigrationContext.configure(sync_connection), Base.metadata),
        )

    real_drift = [d for d in differences if not _is_benign_migration_managed(d)]
    assert real_drift == [], f"Schema drift vs ORM metadata (excluding reasoned divergence): {real_drift}"


async def test_persisted_factories_insert_valid_relationships(db_engine: AsyncEngine) -> None:
    def insert_graph(connection: Any) -> tuple[object, object]:
        with Session(bind=connection) as session:
            factories = (
                AccountFactory,
                OrganisationFactory,
                PipelineFactory,
                PipelineSnapshotFactory,
                RunFactory,
            )
            for factory_class in factories:
                factory_class._meta.sqlalchemy_session = session
            run = RunFactory()
            session.flush()
            persisted = session.scalar(select(Run).where(Run.id == run.id))
            assert persisted is not None
            result = persisted.organisation_id, persisted.pipeline_id
            session.rollback()
            return result

    async with db_engine.connect() as connection:
        organisation_id, pipeline_id = await connection.run_sync(insert_graph)

    assert organisation_id is not None
    assert pipeline_id is not None


async def test_library_fork_provenance_is_registry_only_and_immutable(
    db_engine: AsyncEngine,
) -> None:
    organisation_id = uuid.uuid4()
    registry_id = uuid.uuid4()
    local_id = uuid.uuid4()
    fork_id = uuid.uuid4()
    common = {"tags": [], "content_json": {}}

    async with db_engine.begin() as connection:
        await connection.execute(
            Organisation.__table__.insert().values(
                id=organisation_id,
                name="Fork test",
                slug=f"fork-test-{organisation_id}",
                settings_json={},
            ),
        )
        await connection.execute(
            LibraryPrimitive.__table__.insert().values(
                id=registry_id,
                organisation_id=organisation_id,
                source="registry",
                primitive_type="agent",
                name="Registry",
                slug="registry",
                author="publisher",
                version="1.0.0",
                visibility="org",
                source_url="https://registry.example/agent",
                checksum="a" * 64,
                ed25519_signature="signature",
                verified=True,
                download_count=0,
                average_rating=1,
                review_count=0,
                **common,
            ),
        )
        await connection.execute(
            LibraryPrimitive.__table__.insert().values(
                id=local_id,
                organisation_id=organisation_id,
                source="local",
                primitive_type="agent",
                name="Local",
                slug="local",
                author="user",
                version="1.0.0",
                visibility="org",
                **common,
            ),
        )

    with pytest.raises(DBAPIError):
        async with db_engine.begin() as connection:
            await connection.execute(
                LibraryPrimitive.__table__.insert().values(
                    id=uuid.uuid4(),
                    organisation_id=organisation_id,
                    source="local",
                    primitive_type="agent",
                    name="Invalid fork",
                    slug="invalid-fork",
                    author="user",
                    version="1.0.0",
                    visibility="org",
                    forked_from=local_id,
                    **common,
                ),
            )

    async with db_engine.begin() as connection:
        await connection.execute(
            LibraryPrimitive.__table__.insert().values(
                id=fork_id,
                organisation_id=organisation_id,
                source="local",
                primitive_type="agent",
                name="Valid fork",
                slug="valid-fork",
                author="user",
                version="1.0.0",
                visibility="org",
                forked_from=registry_id,
                **common,
            ),
        )

    with pytest.raises(DBAPIError):
        async with db_engine.begin() as connection:
            await connection.execute(
                update(LibraryPrimitive).where(LibraryPrimitive.id == fork_id).values(forked_from=None),
            )


class TestParityIgnoreListClassify:
    """qa M17: the failure path stays INTACT — every reasoned entry ignores
    its own diff class, and an UNLISTED divergence of the same kind is NOT
    ignored (the old blanket classes silently passed exactly these)."""

    @staticmethod
    def _check(table_name: str, constraint_name: str) -> Any:
        table = Table(table_name, MetaData(), Column("x", Integer))
        constraint = CheckConstraint("x > 0", name=constraint_name)
        table.append_constraint(constraint)
        return constraint

    @staticmethod
    def _fk(table_name: str, col_name: str, ref: str = "accounts.id") -> Any:
        table = Table(
            table_name,
            MetaData(),
            Column(col_name, Integer(), ForeignKey(ref)),
        )
        # ``Table.constraints`` is a SET (nondeterministic order) that also
        # carries an auto PK placeholder — pick the FK explicitly.
        return next(c for c in table.constraints if isinstance(c, ForeignKeyConstraint))

    def test_listed_entries_are_ignored(self) -> None:
        # CHECK that exists only in migrations (0165).
        assert _is_benign_migration_managed(
            ("remove_constraint", self._check("suite_runs", "ck_eval_suite_run_total_cases"))
        )
        # JSONB(DB) -> JSON(ORM) parity, listed column.
        assert _is_benign_migration_managed(("modify_type", None, "runs", "raw_output_markers", {}, JSONB(), JSON()))
        # Audit-chain FK + column (0108).
        assert _is_benign_migration_managed(("remove_fk", self._fk("agents", "created_by")))
        assert _is_benign_migration_managed(("remove_column", None, "agents", Column("created_by", UUID())))
        # Table with no ORM model (FAR-583 quarantine side table).
        assert _is_benign_migration_managed(
            ("remove_table", Table("run_node_outputs_quarantine", MetaData(), Column("run_id", Integer)))
        )
        # Known gap: nodes.id FK the ORM declares, no migration created.
        assert _is_benign_migration_managed(("add_fk", self._fk("pipeline_edges", "source_node_id", "nodes.id")))
        # Known gap / strict-twin divergence: ORM CHECK add side.
        assert _is_benign_migration_managed(
            ("add_constraint", self._check("deleted_defaults", "ck_deleted_defaults_signal_nonempty"))
        )
        assert _is_benign_migration_managed(
            ("add_constraint", self._check("run_node_outputs", "ck_run_node_outputs_meta_present"))
        )
        # B2b-expiring legacy column still listed while the columns exist.
        assert _is_benign_migration_managed(("modify_type", None, "runs", "outputs_json", {}, JSONB(), JSON()))
        # Comments/indexes stay blanket-ignored (reasoned in the predicate).
        assert _is_benign_migration_managed(("modify_comment",))
        assert _is_benign_migration_managed(("add_index", None, "t", "ix_something"))

    def test_unlisted_check_constraint_is_not_ignored(self) -> None:
        assert not _is_benign_migration_managed(
            ("remove_constraint", self._check("brand_new_table", "ck_brand_new_guard"))
        )
        assert not _is_benign_migration_managed(("remove_constraint", self._check("runs", "ck_run_brand_new_guard")))
        assert not _is_benign_migration_managed(
            ("add_constraint", self._check("brand_new_table", "ck_orm_only_new_guard"))
        )

    def test_unlisted_jsonb_to_json_modify_type_is_not_ignored(self) -> None:
        assert not _is_benign_migration_managed(
            ("modify_type", None, "brand_new_table", "config_json", {}, JSONB(), JSON())
        )
        # A listed table's UNLISTED column is still real drift.
        assert not _is_benign_migration_managed(
            ("modify_type", None, "runs", "brand_new_json_column", {}, JSONB(), JSON())
        )
        # The reverse direction (JSON in DB, JSONB in ORM) is genuine drift.
        assert not _is_benign_migration_managed(
            ("modify_type", None, "runs", "raw_output_markers", {}, JSON(), JSONB())
        )

    def test_unlisted_fk_and_column_drift_is_not_ignored(self) -> None:
        # An unlisted table's audit-like column is real drift.
        assert not _is_benign_migration_managed(
            ("remove_column", None, "brand_new_table", Column("created_by", UUID()))
        )
        # An unlisted FK (non-audit columns) is real drift.
        assert not _is_benign_migration_managed(("remove_fk", self._fk("agents", "pipeline_id", "pipelines.id")))
        # An unlisted nodes.id FK is real drift.
        assert not _is_benign_migration_managed(("add_fk", self._fk("brand_new_table", "node_id", "nodes.id")))

    def test_unlisted_remove_table_is_not_ignored(self) -> None:
        assert not _is_benign_migration_managed(
            ("remove_table", Table("brand_new_table", MetaData(), Column("x", Integer)))
        )
