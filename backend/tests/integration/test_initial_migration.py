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
#   * `# expires: <date> (migration <rev> drops ...)` — an expand/contract
#     entry: the ORM reference was removed one release BEFORE the DDL lands,
#     so the drift disappears when that migration ships. Remove the entry in
#     that change (a stale entry would keep hiding drift and must fail loudly
#     instead).
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
#
# An entry describes a GENUINE divergence and is removed WITH the conversion:
# once the ORM maps the shared
# ``JSON().with_variant(JSONB(), "postgresql")`` alias the entry can never fire,
# so leaving it in place would silently mask a future revert to plain JSON —
# the exact failure this norm exists to prevent. FAR-1878 aligned the columns
# migration 0147 promoted (plus the few that already carried an inline JSON
# variant), and FAR-1941 aligned the remaining physical-jsonb columns, so their
# entries were removed in those changes.
#
# The map is now EMPTY — the residual is closed. Every physical-jsonb column is
# declared with the shared alias, so a JSONB(DB) -> JSON(ORM) ``modify_type``
# diff can only be a genuine revert, and it FAILS the test rather than being
# silently ignored.
_JSONB_DB_TO_JSON_ORM: dict[str, frozenset[str]] = {}
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
    # FAR-1962: the legacy eval_definitions table is DROPPED by migration 0324,
    # so its migration-owned CHECK no longer exists to be listed.
    "eval_results": frozenset({"ck_eval_results_run_xor_suite"}),
    "eval_suites": frozenset({"ck_eval_suites_minimum_delta"}),
    "journeys": frozenset({"ck_journey_run_count"}),
    # library_primitives: the two 0165 CHECK names (download_count /
    # review_count) were REMOVED from this map when migration 0310 dropped
    # those constraints — 0151's equivalent pair (ck_library_primitives_dl_count
    # / ck_library_primitives_review_count) is ORM-declared and present in the
    # DB, so no migration-owned CHECK remains for this table. Stale entries
    # here would mask a future accidental re-add.
    "notification_delivery_log": frozenset({"ck_notification_delivery_attempt_count"}),
    "notification_endpoints": frozenset({"ck_notification_endpoint_dead_letter"}),
    "org_daily_run_counts": frozenset({"ck_daily_run_count_run_count"}),
    "run_daily_facts": frozenset({"ck_run_daily_facts_status", "ck_run_daily_facts_trigger_type"}),
    # 0192: the STRICT dialect-specific meta-shape CHECK (jsonb_typeof on PG /
    # json_type on SQLite); the ORM declares the PORTABLE subset with a
    # different name (ck_run_node_outputs_meta_present) — matched below.
    # permanent (documented repo divergence)
    "run_node_outputs": frozenset({"ck_run_node_outputs_meta_shape"}),
    # ck_runs_cancel_reason (0260): the closed cancel-reason vocabulary is a
    # migration-owned DB backstop — the ORM declares the Python constant set
    # (db.models.run.CANCEL_REASON_VALUES), not a duplicate CHECK.
    # permanent (documented repo divergence)
    "runs": frozenset({"ck_run_claim_count", "ck_run_node_attempt_count", "ck_runs_cancel_reason"}),
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

# FAR-1902 expand/contract: the retired organisations cost mappings are
# removed from the ORM in this release, while the physical columns (and the
# ``ck_organisations_cum_spend`` CHECK) stay until migration
# ``0324_drop_organisations_cost_columns`` ships in a LATER release — an
# in-flight rolling deploy must never serve a release whose ORM still names a
# dropped column (FAR-583 precedent: the runs blob mappings were cut in B1,
# the columns dropped at B2b). Until 0324 lands the parity diff arrives as an
# ORM-side ``remove_column`` / ``remove_constraint``.
# expires: 2026-12-20 — two release windows out from this branch (2026-10-20):
# this release removes the mappings, the NEXT one ships 0324, and the date is
# the safety net that makes a forgotten drop fail loudly instead of silently
# hiding a genuine re-add of the columns.
_COST_COLUMN_PARITY_EXPIRES: dict[str, frozenset[str]] = {
    "organisations": frozenset(
        {
            "max_run_cost_cents",  # expires: 2026-12-20 (migration 0324 drops the column)
            "spend_ceiling_cents",  # expires: 2026-12-20 (migration 0324 drops the column)
            "org_cumulative_spend_cents",  # expires: 2026-12-20 (migration 0324 drops the column)
        }
    ),
}
# The CHECK constraint rides the same contract: the ORM declaration is gone,
# the DB-side constraint is dropped by 0324.
# expires: 2026-12-20 (same window as the columns above)
_COST_CHECK_PARITY_EXPIRES: dict[str, frozenset[str]] = {
    "organisations": frozenset({"ck_organisations_cum_spend"}),
}

# ORM-declared CHECKs whose DB-side counterpart is the strict dialect twin —
# the ORM's portable subset appears as add_constraint. PER ENTRY reason:
_ORM_CHECK_DIVERGENCE: frozenset[tuple[str, str]] = frozenset(
    {
        # run_node_outputs: the ORM's portable meta-present subset vs the
        # migration's strict jsonb_typeof shape (repo parity rule).
        # permanent (documented repo divergence)
        ("run_node_outputs", "ck_run_node_outputs_meta_present"),
    }
)
# The ``deleted_defaults`` signal guard (FAR-644) is NO LONGER listed here:
# migration 0206 creates it at the DB level, so the ORM declaration and the
# migrated schema now agree — the entry was removed with the drift it tracked.

# ORM-declared FKs to nodes.id were REMOVED from the models (FAR-644): the
# deprecated ``nodes`` table never holds JSON-graph node ids, so no migration
# should create these constraints (see migration 0170's correction). Any new
# add_fk diff now FAILS the test — the old KNOWN-GAP wheel is gone.

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
        # FAR-795 per-org agent-mint budget tally (migration 0242): written
        # exclusively via a single atomic raw-SQL upsert in
        # modulo.core.runtime_config.mint_budget (the migration comment states
        # the module has no ORM model to fall back on), never via the ORM.
        # permanent (documented repo divergence)
        "org_mint_budget_usage",
        # FAR-1100 chunk 3 (migration 0254): migration-owned audit table for
        # PolicyGate binding-validation rejections during the Eval backfill.
        # Written by the migration only, never mapped by the ORM.
        # permanent (documented repo divergence)
        "eval_backfill_violations",
        # FAR-1962 (chunk 13 slice 3+4): the legacy eval_definitions table was
        # DROPPED by migration 0324 — it is no longer a DB-vs-ORM divergence
        # (neither side has it), so the entry was removed with the table.
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
        if inner[2] == "modulo_journey_facts" and inner[3].name == "updated_at":
            return True
        # DB-owned audit-chain columns the migrations add but the ORM does not
        # declare (organisations + the sibling audit-chain tables). The reverse
        # remove_column direction is handled below; here the DB carries the
        # column the ORM omits, which is equally benign, migration-owned divergence.
        # permanent (documented repo divergence)
        return inner[2] in _AUDIT_CHAIN_COLUMNS and inner[3].name in _AUDIT_CHAIN_COLUMNS[inner[2]]
    if kind == "remove_column":
        # Audit-chain columns the DB triggers own (0108) — per-table entries.
        if inner[2] in _AUDIT_CHAIN_COLUMNS and inner[3].name in _AUDIT_CHAIN_COLUMNS[inner[2]]:
            return True
        # FAR-1902 cost-column contract cut: the ORM mappings were removed one
        # release BEFORE migration 0324 drops the physical columns, so the
        # parity diff arrives as an ORM-side remove_column until it lands.
        # Per-column expiring entries (never per-table).
        return inner[3].name in _COST_COLUMN_PARITY_EXPIRES.get(inner[2], frozenset())
    if kind == "remove_fk":
        # The audit-chain FKs on the trigger-maintained columns — per-table.
        return inner[1].table.name in _AUDIT_CHAIN_FK_COLUMNS and bool(
            {col.name for col in inner[1].columns} <= _AUDIT_CHAIN_FK_COLUMNS[inner[1].table.name]
        )
    if kind == "add_fk":
        # DB-owned audit-chain FKs the migrations add but the ORM does not declare
        # (organisations.created_by + the sibling audit-chain tables) are benign,
        # migration-owned divergence. Any FK not in that set is real drift (the
        # far-644 nodes.id KNOWN-GAP wheel was closed with the drift itself; a
        # stale wheel would keep hiding new FK drift).
        # permanent (documented repo divergence)
        return inner[1].table.name in _AUDIT_CHAIN_FK_COLUMNS and bool(
            {col.name for col in inner[1].columns} <= _AUDIT_CHAIN_FK_COLUMNS[inner[1].table.name]
        )
    if kind == "remove_constraint":
        # CHECK guards that exist ONLY in migrations (0157/0165/0192) —
        # per-(table, constraint) entries.
        constraint = inner[1]
        # FAR-1902: the retired cost CHECK is dropped by migration 0324 in a
        # later release — an expiring entry until then.
        if constraint.name in _COST_CHECK_PARITY_EXPIRES.get(constraint.table.name, frozenset()):
            return True
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
    a one-line reason:

    * ``# expires: <date> (migration <rev> drops ...)`` — an expand/contract
      entry: the ORM reference was removed one release BEFORE the DDL lands,
      so the drift disappears when that migration ships. The FAR-1902
      organisations cost columns/CHECK are listed this way until migration
      ``0324_drop_organisations_cost_columns`` lands.
    * ``# permanent (documented repo divergence)`` — the repo's multi-backend
      convention (JSONB in migrations / generic JSON in the ORM) or
      DB-trigger-maintained columns.

    The former GENUINE drift entries (ORM-declared FKs to ``nodes.id`` and the
    ``deleted_defaults`` CHECK, both ``# tracked: FAR-583 follow-up``) were
    closed with the drift itself in FAR-644: the ORM FKs were stripped (the
    deprecated ``nodes`` table never holds JSON-graph node ids) and migration
    0206 creates the CHECK. Anything NOT listed fails the test.
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
        # Audit-chain FK + column (0108).
        assert _is_benign_migration_managed(("remove_fk", self._fk("agents", "created_by")))
        assert _is_benign_migration_managed(("remove_column", None, "agents", Column("created_by", UUID())))
        # FAR-1902 expand/contract: the cost columns/CHECK are unmapped from
        # the ORM while migration 0324 has not shipped yet — the ORM-side
        # remove_column / remove_constraint is benign until then.
        assert _is_benign_migration_managed(
            ("remove_column", None, "organisations", Column("max_run_cost_cents", Integer()))
        )
        assert _is_benign_migration_managed(
            ("remove_column", None, "organisations", Column("spend_ceiling_cents", Integer()))
        )
        assert _is_benign_migration_managed(
            ("remove_column", None, "organisations", Column("org_cumulative_spend_cents", Integer()))
        )
        assert _is_benign_migration_managed(
            ("remove_constraint", self._check("organisations", "ck_organisations_cum_spend"))
        )
        # Table with no ORM model (FAR-583 quarantine side table).
        assert _is_benign_migration_managed(
            ("remove_table", Table("run_node_outputs_quarantine", MetaData(), Column("run_id", Integer)))
        )
        # A known-gap wheel that was CLOSED (FAR-644) must NOT stay silent:
        # nodes.id FKs were stripped from the ORM, so any add_fk is real drift.
        assert not _is_benign_migration_managed(("add_fk", self._fk("pipeline_edges", "source_node_id", "nodes.id")))
        # Strict-twin divergence: ORM CHECK add side (run_node_outputs only —
        # deleted_defaults is now created by migration 0206, so its former
        # wheel must fail like the others).
        assert _is_benign_migration_managed(
            ("add_constraint", self._check("run_node_outputs", "ck_run_node_outputs_meta_present"))
        )
        assert not _is_benign_migration_managed(
            ("add_constraint", self._check("deleted_defaults", "ck_deleted_defaults_signal_nonempty"))
        )
        # FAR-1878/FAR-1941 aligned every physical-jsonb column to JSONB_COL, so
        # their entries were removed WITH the conversion: the SAME modify_type
        # diff for them must now be REAL drift, not silently ignored.
        assert not _is_benign_migration_managed(("modify_type", None, "runs", "cost_breakdown", {}, JSONB(), JSON()))
        assert not _is_benign_migration_managed(("modify_type", None, "runs", "node_token_usage", {}, JSONB(), JSON()))
        assert not _is_benign_migration_managed(("modify_type", None, "runs", "input_payload", {}, JSONB(), JSON()))
        # B2b/drop (migration 0215) REMOVED the three runs-blob-column expiry
        # entries; the classifier's hypothetical remove_column case remains
        # valid for exercising the remaining audit-chain convention.
        assert not _is_benign_migration_managed(("modify_type", None, "runs", "outputs_json", {}, JSONB(), JSON())), (
            "the B2b expiry for the runs blob columns must NOT persist after 0215"
        )
        # B2b (0215): the runs blob columns are gone from BOTH sides - a
        # remove_column on them is NO LONGER possible and must NOT stay
        # benign: a DB that still has them is a DB-AHEAD-OF-IMAGE condition.
        assert not _is_benign_migration_managed(("remove_column", None, "runs", Column("outputs_json", JSONB()))), (
            "the B2b remove_column expiry for the runs blob columns must NOT persist after 0215"
        )
        # Comments/indexes stay blanket-ignored (reasoned in the predicate).
        assert _is_benign_migration_managed(("modify_comment",))
        assert _is_benign_migration_managed(("add_index", None, "t", "ix_something"))

    def test_unlisted_check_constraint_is_not_ignored(self) -> None:
        assert not _is_benign_migration_managed(
            ("remove_constraint", self._check("brand_new_table", "ck_brand_new_guard"))
        )
        assert not _is_benign_migration_managed(("remove_constraint", self._check("runs", "ck_run_brand_new_guard")))
        # The FAR-1902 expiry is per-(table, constraint), not per-table: an
        # UNLISTED organisations CHECK is real drift.
        assert not _is_benign_migration_managed(
            ("remove_constraint", self._check("organisations", "ck_organisations_brand_new_guard"))
        )
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
            ("modify_type", None, "runs", "blocked_partial_summary", {}, JSON(), JSONB())
        )

    def test_unlisted_fk_and_column_drift_is_not_ignored(self) -> None:
        # An unlisted table's audit-like column is real drift.
        assert not _is_benign_migration_managed(
            ("remove_column", None, "brand_new_table", Column("created_by", UUID()))
        )
        # A listed table's UNLISTED remove_column is real drift (the B2b
        # expiry set is per-column, never per-table).
        assert not _is_benign_migration_managed(("remove_column", None, "runs", Column("brand_new_column", JSONB())))
        # FAR-1902 expiry is likewise per-column: a NEW organisations column
        # the ORM drops is real drift, not covered by the cost-column set.
        assert not _is_benign_migration_managed(
            ("remove_column", None, "organisations", Column("brand_new_column", Integer()))
        )
        # An unlisted FK (non-audit columns) is real drift.
        assert not _is_benign_migration_managed(("remove_fk", self._fk("agents", "pipeline_id", "pipelines.id")))
        # An unlisted nodes.id FK is real drift.
        assert not _is_benign_migration_managed(("add_fk", self._fk("brand_new_table", "node_id", "nodes.id")))

    def test_unlisted_remove_table_is_not_ignored(self) -> None:
        assert not _is_benign_migration_managed(
            ("remove_table", Table("brand_new_table", MetaData(), Column("x", Integer)))
        )
