"""System Maps core tables + attribution bindings + loop admission/reservation (FAR-2236).

Revision ID: 0341_system_maps
Revises: 0340_drop_assistant_ui_driving_flag_row
Create Date: 2026-10-30

Five core tables:
* ``system_maps`` — top-level map container (org-scoped, soft-delete, team visibility).
* ``system_map_revisions`` — immutable revision history per map; one published revision per map.
* ``system_map_nodes`` — nodes within a revision (pipeline, agent, connector, loop, etc.).
* ``system_map_relationships`` — edges within a revision (flow, data, trigger, loop_back).
* ``system_map_loop_configs`` — improvement-loop parameters per loop node.

Two operational tables for loop admission control:
* ``system_map_loop_admission_attempts`` — idempotency-keyed attempt log (CAS deduplication).
* ``system_map_loop_reservations`` — active reservations with cooldown tracking.

One attribution binding table:
* ``system_map_attribution_bindings`` — binds pipelines/triggers/actions to a map node
  for cost/outcome attribution. Partial unique indexes enforce one binding per
  (org, pipeline_id, node_key) for triggers and action pipelines.

All tables are org-scoped with RLS on Postgres. ``system_maps`` carries
``visibility``/``owner_team_id`` and therefore uses ``rls_team_isolation`` (the
team policy includes the org check as an AND gate). The other seven tables have
no team-visibility columns and follow the house org-only pattern:
``rls_org_isolation`` (see 0110 / 0124).
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision: str = "0341_system_maps"
down_revision: str | None = "0340_drop_assistant_ui_driving_flag_row"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_MIGRATE_ROLE = "modulo_migrate"
_APP_ROLE = "modulo_app"
_SYSTEM_ROLE = "modulo_system"

_UUID_SERVER_DEFAULT = "gen_random_uuid()"
_FK_ORGANISATIONS_ID = "organisations.id"
_FK_ACCOUNTS_ID = "accounts.id"
_FK_PIPELINES_ID = "pipelines.id"

_TABLE_SYSTEM_MAPS = "system_maps"
_TABLE_SYSTEM_MAP_REVISIONS = "system_map_revisions"
_TABLE_SYSTEM_MAP_NODES = "system_map_nodes"
_TABLE_SYSTEM_MAP_RELATIONSHIPS = "system_map_relationships"
_TABLE_SYSTEM_MAP_LOOP_CONFIGS = "system_map_loop_configs"
_TABLE_SYSTEM_MAP_ATTRIBUTION_BINDINGS = "system_map_attribution_bindings"
_TABLE_SYSTEM_MAP_LOOP_ADMISSION_ATTEMPTS = "system_map_loop_admission_attempts"
_TABLE_SYSTEM_MAP_LOOP_RESERVATIONS = "system_map_loop_reservations"

# Module-level tuple so the architecture test (tests/unit/db/test_rls_coverage.py)
# sees the coverage without parsing the DDL below.
_ORG_SCOPED_TABLES: tuple[str, ...] = (
    _TABLE_SYSTEM_MAPS,
    _TABLE_SYSTEM_MAP_REVISIONS,
    _TABLE_SYSTEM_MAP_NODES,
    _TABLE_SYSTEM_MAP_RELATIONSHIPS,
    _TABLE_SYSTEM_MAP_LOOP_CONFIGS,
    _TABLE_SYSTEM_MAP_ATTRIBUTION_BINDINGS,
    _TABLE_SYSTEM_MAP_LOOP_ADMISSION_ATTEMPTS,
    _TABLE_SYSTEM_MAP_LOOP_RESERVATIONS,
)

# JSON columns are jsonb on Postgres (0147 standardisation), generic JSON
# elsewhere; identical SQLAlchemy expression to the ORM models.
_JSON_TYPE = sa.JSON().with_variant(JSONB(), "postgresql")

# Verbatim team policy: the org check is an AND gate (no cross-org leak),
# and the execution-context escape hatch only widens the team clause WITHIN
# the organisation (background reads set app.execution_context='true').
_TEAM_POLICY = (
    "((organisation_id = (NULLIF(current_setting('app.organisation_id'::text, true), ''::text))::uuid)"
    " AND (((visibility)::text = 'org'::text)"
    " OR (visibility IS NULL)"
    " OR (owner_team_id IS NULL)"
    " OR (owner_team_id IN ( SELECT team_memberships.team_id FROM public.team_memberships"
    " WHERE (team_memberships.account_id = (NULLIF(current_setting('app.user_id'::text, true), ''::text))::uuid)))"
    " OR (NULLIF(current_setting('app.org_role'::text, true), ''::text) = 'admin'::text)"
    " OR (NULLIF(current_setting('app.execution_context'::text, true), ''::text) = 'true'::text)))"
)

# Only system_maps carries visibility/owner_team_id; the team policy references
# both columns, so it cannot be applied to the other seven tables.
_TEAM_SCOPED_TABLES: tuple[str, ...] = (_TABLE_SYSTEM_MAPS,)
_ORG_ONLY_TABLES: tuple[str, ...] = tuple(t for t in _ORG_SCOPED_TABLES if t not in _TEAM_SCOPED_TABLES)

_ORG_POLICY = "((organisation_id = (NULLIF(current_setting('app.organisation_id'::text, true), ''::text))::uuid))"

_ALTER_TABLE_PREFIX = 'ALTER TABLE "'
_SET_NULL = "SET NULL"

_ENABLE_RLS_STATEMENTS: tuple[str, ...] = tuple(
    _ALTER_TABLE_PREFIX + table + '" ENABLE ROW LEVEL SECURITY' for table in _ORG_SCOPED_TABLES
) + tuple(_ALTER_TABLE_PREFIX + table + '" FORCE ROW LEVEL SECURITY' for table in _ORG_SCOPED_TABLES)

_CREATE_POLICY_STATEMENTS: tuple[str, ...] = tuple(
    'CREATE POLICY rls_team_isolation ON "' + table + '" USING (' + _TEAM_POLICY + ") WITH CHECK (" + _TEAM_POLICY + ")"
    for table in _TEAM_SCOPED_TABLES
) + tuple(
    'CREATE POLICY rls_org_isolation ON "' + table + '" USING (' + _ORG_POLICY + ") WITH CHECK (" + _ORG_POLICY + ")"
    for table in _ORG_ONLY_TABLES
)

_DROP_POLICY_STATEMENTS: tuple[str, ...] = (
    tuple('DROP POLICY IF EXISTS rls_team_isolation ON "' + table + '"' for table in _TEAM_SCOPED_TABLES)
    + tuple('DROP POLICY IF EXISTS rls_org_isolation ON "' + table + '"' for table in _ORG_ONLY_TABLES)
    + tuple(_ALTER_TABLE_PREFIX + table + '" DISABLE ROW LEVEL SECURITY' for table in _ORG_SCOPED_TABLES)
)

_GRANT_CREATE_SCHEMA = "GRANT CREATE ON SCHEMA public TO " + _MIGRATE_ROLE
_GRANT_REFERENCES_ORGS = "GRANT REFERENCES ON TABLE public.organisations TO " + _MIGRATE_ROLE
_GRANT_REFERENCES_TEAMS = "GRANT REFERENCES ON TABLE public.teams TO " + _MIGRATE_ROLE
_GRANT_REFERENCES_ACCOUNTS = "GRANT REFERENCES ON TABLE public.accounts TO " + _MIGRATE_ROLE
_GRANT_REFERENCES_PIPELINES = "GRANT REFERENCES ON TABLE public.pipelines TO " + _MIGRATE_ROLE
_GRANT_REFERENCES_CONNECTORS = "GRANT REFERENCES ON TABLE public.connector_instances TO " + _MIGRATE_ROLE
_GRANT_REFERENCES_EVALS = "GRANT REFERENCES ON TABLE public.evals TO " + _MIGRATE_ROLE
_GRANT_REFERENCES_TRIGGERS = "GRANT REFERENCES ON TABLE public.triggers TO " + _MIGRATE_ROLE
_GRANT_REFERENCES_RUNS = "GRANT REFERENCES ON TABLE public.runs TO " + _MIGRATE_ROLE
_SET_ROLE_MIGRATE = "SET ROLE " + _MIGRATE_ROLE


def _is_postgres(bind: sa.Connection) -> bool:
    return bind.dialect.name == "postgresql"


def _role_exists(bind: sa.Connection, role: str) -> bool:
    return (
        bind.execute(sa.text("SELECT 1 FROM pg_roles WHERE rolname = :role"), {"role": role}).scalar_one_or_none()
        is not None
    )


def _assert_owner_is_migrate(bind: sa.Connection, table: str) -> None:
    owner = bind.execute(
        sa.text("SELECT relowner::regrole::text FROM pg_class WHERE oid = to_regclass(:tbl)").bindparams(
            tbl="public." + table
        )
    ).scalar_one_or_none()
    if owner != _MIGRATE_ROLE:
        raise RuntimeError(
            table + " owner is " + repr(owner) + ", expected '" + _MIGRATE_ROLE + "' "
            "(the app role must NOT own it — owner bypasses RLS)"
        )


def _grant_dml(role: str) -> None:
    grant = "GRANT SELECT, INSERT, UPDATE, DELETE ON "
    for table in _ORG_SCOPED_TABLES:
        op.execute(grant + table + " TO " + role)


def _create_tables() -> None:
    # system_maps
    op.create_table(
        _TABLE_SYSTEM_MAPS,
        sa.Column("id", sa.Uuid(), nullable=False, server_default=sa.text(_UUID_SERVER_DEFAULT)),
        sa.Column("organisation_id", sa.Uuid(), nullable=False),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("description", sa.String(length=2000), nullable=True),
        sa.Column("owner_team_id", sa.Uuid(), nullable=True),
        sa.Column("visibility", sa.String(length=10), nullable=False, server_default="org"),
        sa.Column("default_map", sa.Boolean(), nullable=False, server_default="false"),
        sa.Column("published_revision_id", sa.Uuid(), nullable=True),
        sa.Column("account_id", sa.Uuid(), nullable=False),
        sa.Column("updated_by", sa.Uuid(), nullable=True),
        sa.Column("deleted_by", sa.Uuid(), nullable=True),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.current_timestamp(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.current_timestamp(), nullable=False),
        sa.ForeignKeyConstraint(["organisation_id"], [_FK_ORGANISATIONS_ID], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["owner_team_id"], ["teams.id"], ondelete=_SET_NULL),
        sa.ForeignKeyConstraint(["account_id"], [_FK_ACCOUNTS_ID], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["updated_by"], [_FK_ACCOUNTS_ID], ondelete=_SET_NULL),
        sa.ForeignKeyConstraint(["deleted_by"], [_FK_ACCOUNTS_ID], ondelete=_SET_NULL),
        sa.PrimaryKeyConstraint("id"),
        sa.CheckConstraint("visibility IN ('org', 'team')", name="ck_system_maps_visibility"),
        sa.CheckConstraint("visibility = 'org' OR owner_team_id IS NOT NULL", name="ck_system_maps_team_owner"),
        sa.UniqueConstraint("id", "organisation_id", name="uq_system_maps_id_organisation_id"),
    )

    # system_map_revisions
    op.create_table(
        _TABLE_SYSTEM_MAP_REVISIONS,
        sa.Column("id", sa.Uuid(), nullable=False, server_default=sa.text(_UUID_SERVER_DEFAULT)),
        sa.Column("organisation_id", sa.Uuid(), nullable=False),
        sa.Column("system_map_id", sa.Uuid(), nullable=False),
        sa.Column("revision_number", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(length=30), nullable=False, server_default="draft"),
        sa.Column("content_json", _JSON_TYPE, nullable=False, server_default=sa.text("'{}'")),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.current_timestamp(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.current_timestamp(), nullable=False),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("deleted_by", sa.Uuid(), nullable=True),
        sa.Column("published_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["organisation_id"], [_FK_ORGANISATIONS_ID], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["system_map_id"], [_TABLE_SYSTEM_MAPS + ".id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.CheckConstraint("status IN ('draft', 'published', 'archived')", name="ck_system_map_revisions_status"),
        sa.UniqueConstraint("system_map_id", "revision_number", name="uq_system_map_revisions_map_number"),
        sa.UniqueConstraint("id", "organisation_id", name="uq_system_map_revisions_id_organisation_id"),
    )

    # Composite FK system_maps.published_revision_id -> system_map_revisions.
    # Added AFTER system_map_revisions exists: a forward reference inside the
    # system_maps CREATE TABLE fails on Postgres because the target is created
    # later in this migration.
    op.create_foreign_key(
        "fk_system_maps_published_revision",
        _TABLE_SYSTEM_MAPS,
        _TABLE_SYSTEM_MAP_REVISIONS,
        ["published_revision_id", "organisation_id"],
        ["id", "organisation_id"],
        ondelete=_SET_NULL,
    )

    # system_map_nodes
    op.create_table(
        _TABLE_SYSTEM_MAP_NODES,
        sa.Column("id", sa.Uuid(), nullable=False, server_default=sa.text(_UUID_SERVER_DEFAULT)),
        sa.Column("organisation_id", sa.Uuid(), nullable=False),
        sa.Column("revision_id", sa.Uuid(), nullable=False),
        sa.Column("node_key", sa.String(length=100), nullable=False),
        sa.Column("node_type", sa.String(length=50), nullable=False),
        sa.Column("display_name", sa.String(length=255), nullable=False),
        sa.Column("config_json", _JSON_TYPE, nullable=False, server_default=sa.text("'{}'")),
        sa.Column("pipeline_id", sa.Uuid(), nullable=True),
        sa.Column("connector_instance_id", sa.Uuid(), nullable=True),
        sa.Column("owner_team_id", sa.Uuid(), nullable=True),
        sa.Column("eval_id", sa.Uuid(), nullable=True),
        sa.Column("position_x", sa.Numeric(10, 2), nullable=False, server_default="0"),
        sa.Column("position_y", sa.Numeric(10, 2), nullable=False, server_default="0"),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.current_timestamp(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.current_timestamp(), nullable=False),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("deleted_by", sa.Uuid(), nullable=True),
        sa.ForeignKeyConstraint(["organisation_id"], [_FK_ORGANISATIONS_ID], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["revision_id"], [_TABLE_SYSTEM_MAP_REVISIONS + ".id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["pipeline_id"], [_FK_PIPELINES_ID], ondelete=_SET_NULL),
        sa.ForeignKeyConstraint(["connector_instance_id"], ["connector_instances.id"], ondelete=_SET_NULL),
        sa.ForeignKeyConstraint(["owner_team_id"], ["teams.id"], ondelete=_SET_NULL),
        sa.ForeignKeyConstraint(["eval_id"], ["evals.id"], ondelete=_SET_NULL),
        sa.PrimaryKeyConstraint("id"),
        sa.CheckConstraint(
            "node_type IN ('pipeline', 'agent', 'connector', 'improvement_loop', 'data_store', 'external_system')",
            name="ck_system_map_nodes_type",
        ),
        sa.UniqueConstraint("revision_id", "node_key", name="uq_system_map_nodes_revision_key"),
    )

    # system_map_relationships
    op.create_table(
        _TABLE_SYSTEM_MAP_RELATIONSHIPS,
        sa.Column("id", sa.Uuid(), nullable=False, server_default=sa.text(_UUID_SERVER_DEFAULT)),
        sa.Column("organisation_id", sa.Uuid(), nullable=False),
        sa.Column("revision_id", sa.Uuid(), nullable=False),
        sa.Column("source_node_key", sa.String(length=100), nullable=False),
        sa.Column("target_node_key", sa.String(length=100), nullable=False),
        sa.Column("relationship_type", sa.String(length=50), nullable=False),
        sa.Column("config_json", _JSON_TYPE, nullable=False, server_default=sa.text("'{}'")),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.current_timestamp(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.current_timestamp(), nullable=False),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("deleted_by", sa.Uuid(), nullable=True),
        sa.ForeignKeyConstraint(["organisation_id"], [_FK_ORGANISATIONS_ID], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["revision_id"], [_TABLE_SYSTEM_MAP_REVISIONS + ".id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.CheckConstraint(
            "relationship_type IN ('flow', 'data', 'trigger', 'loop_back')",
            name="ck_system_map_relationships_type",
        ),
        sa.UniqueConstraint(
            "revision_id",
            "source_node_key",
            "target_node_key",
            "relationship_type",
            name="uq_system_map_relationships_rev_src_tgt_type",
        ),
    )

    # system_map_loop_configs
    op.create_table(
        _TABLE_SYSTEM_MAP_LOOP_CONFIGS,
        sa.Column("id", sa.Uuid(), nullable=False, server_default=sa.text(_UUID_SERVER_DEFAULT)),
        sa.Column("organisation_id", sa.Uuid(), nullable=False),
        sa.Column("revision_id", sa.Uuid(), nullable=False),
        sa.Column("node_key", sa.String(length=100), nullable=False),
        sa.Column("target_signal", sa.String(length=100), nullable=False),
        sa.Column("observation_window_seconds", sa.Integer(), nullable=False),
        sa.Column("action_pipeline_id", sa.Uuid(), nullable=False),
        sa.Column("budget_limit_usd", sa.Numeric(14, 6), nullable=False),
        sa.Column("cooldown_seconds", sa.Integer(), nullable=False),
        sa.Column("outcome_metric", sa.String(length=100), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.current_timestamp(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.current_timestamp(), nullable=False),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("deleted_by", sa.Uuid(), nullable=True),
        sa.ForeignKeyConstraint(["organisation_id"], [_FK_ORGANISATIONS_ID], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["revision_id"], [_TABLE_SYSTEM_MAP_REVISIONS + ".id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["action_pipeline_id"], [_FK_PIPELINES_ID], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("revision_id", "node_key", name="uq_system_map_loop_configs_revision_key"),
        sa.CheckConstraint("observation_window_seconds > 0", name="ck_loop_configs_obs_window"),
        sa.CheckConstraint("budget_limit_usd >= 0", name="ck_loop_configs_budget"),
        sa.CheckConstraint("cooldown_seconds >= 0", name="ck_loop_configs_cooldown"),
    )

    # system_map_attribution_bindings
    op.create_table(
        _TABLE_SYSTEM_MAP_ATTRIBUTION_BINDINGS,
        sa.Column("id", sa.Uuid(), nullable=False, server_default=sa.text(_UUID_SERVER_DEFAULT)),
        sa.Column("organisation_id", sa.Uuid(), nullable=False),
        sa.Column("pipeline_id", sa.Uuid(), nullable=False),
        sa.Column("trigger_id", sa.Uuid(), nullable=True),
        sa.Column("system_map_id", sa.Uuid(), nullable=False),
        sa.Column("node_key", sa.String(length=100), nullable=False),
        sa.Column("binding_kind", sa.String(length=30), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.current_timestamp(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.current_timestamp(), nullable=False),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("deleted_by", sa.Uuid(), nullable=True),
        sa.ForeignKeyConstraint(["organisation_id"], [_FK_ORGANISATIONS_ID], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["pipeline_id"], [_FK_PIPELINES_ID], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["trigger_id"], ["triggers.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["system_map_id"], [_TABLE_SYSTEM_MAPS + ".id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.CheckConstraint(
            "binding_kind IN ('trigger', 'action_pipeline')",
            name="ck_system_map_attribution_bindings_kind",
        ),
    )

    # system_map_loop_admission_attempts
    op.create_table(
        _TABLE_SYSTEM_MAP_LOOP_ADMISSION_ATTEMPTS,
        sa.Column("id", sa.Uuid(), nullable=False, server_default=sa.text(_UUID_SERVER_DEFAULT)),
        sa.Column("organisation_id", sa.Uuid(), nullable=False),
        sa.Column("loop_config_id", sa.Uuid(), nullable=False),
        sa.Column("idempotency_key", sa.String(length=255), nullable=False),
        sa.Column("status", sa.String(length=30), nullable=False, server_default="pending"),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.current_timestamp(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.current_timestamp(), nullable=False),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("deleted_by", sa.Uuid(), nullable=True),
        sa.ForeignKeyConstraint(["organisation_id"], [_FK_ORGANISATIONS_ID], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["loop_config_id"], [_TABLE_SYSTEM_MAP_LOOP_CONFIGS + ".id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.CheckConstraint(
            "status IN ('pending', 'admitted', 'rejected', 'duplicate')",
            name="ck_loop_admission_attempts_status",
        ),
        sa.UniqueConstraint("loop_config_id", "idempotency_key", name="uq_loop_admission_attempts_config_key"),
    )

    # system_map_loop_reservations
    op.create_table(
        _TABLE_SYSTEM_MAP_LOOP_RESERVATIONS,
        sa.Column("id", sa.Uuid(), nullable=False, server_default=sa.text(_UUID_SERVER_DEFAULT)),
        sa.Column("organisation_id", sa.Uuid(), nullable=False),
        sa.Column("loop_config_id", sa.Uuid(), nullable=False),
        sa.Column("run_id", sa.Uuid(), nullable=False),
        sa.Column("idempotency_key", sa.String(length=255), nullable=False),
        sa.Column(
            "reserved_at", sa.DateTime(timezone=True), server_default=sa.func.current_timestamp(), nullable=False
        ),
        sa.Column("cooldown_until", sa.DateTime(timezone=True), nullable=False),
        sa.Column("status", sa.String(length=30), nullable=False, server_default="active"),
        sa.Column("consumed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("released_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.current_timestamp(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.current_timestamp(), nullable=False),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("deleted_by", sa.Uuid(), nullable=True),
        sa.ForeignKeyConstraint(["organisation_id"], [_FK_ORGANISATIONS_ID], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["loop_config_id"], [_TABLE_SYSTEM_MAP_LOOP_CONFIGS + ".id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.CheckConstraint(
            "status IN ('active', 'consumed', 'released', 'expired')",
            name="ck_loop_reservations_status",
        ),
        sa.UniqueConstraint("loop_config_id", "idempotency_key", name="uq_loop_reservations_config_key"),
    )

    for table in _ORG_SCOPED_TABLES:
        if table == _TABLE_SYSTEM_MAPS:
            continue
        op.create_foreign_key(
            "fk_" + table + "_deleted_by",
            table,
            "accounts",
            ["deleted_by"],
            ["id"],
            ondelete=_SET_NULL,
        )


def _create_indexes() -> None:
    # system_maps
    op.create_index("ix_system_maps_organisation_id", _TABLE_SYSTEM_MAPS, ["organisation_id"])
    op.create_index("ix_system_maps_owner_team_id", _TABLE_SYSTEM_MAPS, ["owner_team_id"])
    op.create_index("ix_system_maps_published_revision_id", _TABLE_SYSTEM_MAPS, ["published_revision_id"])
    op.create_index("ix_system_maps_account_id", _TABLE_SYSTEM_MAPS, ["account_id"])
    op.create_index("ix_system_maps_deleted_by", _TABLE_SYSTEM_MAPS, ["deleted_by"])
    # Partial unique: one live default map per org
    op.execute(
        'CREATE UNIQUE INDEX uq_system_maps_org_default_live ON "'
        + _TABLE_SYSTEM_MAPS
        + '" (organisation_id) WHERE default_map AND deleted_at IS NULL'
    )

    # system_map_revisions
    op.create_index("ix_system_map_revisions_organisation_id", _TABLE_SYSTEM_MAP_REVISIONS, ["organisation_id"])
    op.create_index("ix_system_map_revisions_system_map_id", _TABLE_SYSTEM_MAP_REVISIONS, ["system_map_id"])
    op.create_index("ix_system_map_revisions_status", _TABLE_SYSTEM_MAP_REVISIONS, ["status"])
    op.create_index("ix_system_map_revisions_deleted_by", _TABLE_SYSTEM_MAP_REVISIONS, ["deleted_by"])

    # system_map_nodes
    op.create_index("ix_system_map_nodes_organisation_id", _TABLE_SYSTEM_MAP_NODES, ["organisation_id"])
    op.create_index("ix_system_map_nodes_revision_id", _TABLE_SYSTEM_MAP_NODES, ["revision_id"])
    op.create_index("ix_system_map_nodes_pipeline_id", _TABLE_SYSTEM_MAP_NODES, ["pipeline_id"])
    op.create_index("ix_system_map_nodes_connector_instance_id", _TABLE_SYSTEM_MAP_NODES, ["connector_instance_id"])
    op.create_index("ix_system_map_nodes_eval_id", _TABLE_SYSTEM_MAP_NODES, ["eval_id"])
    op.create_index("ix_system_map_nodes_deleted_by", _TABLE_SYSTEM_MAP_NODES, ["deleted_by"])

    # system_map_relationships
    op.create_index("ix_system_map_relationships_organisation_id", _TABLE_SYSTEM_MAP_RELATIONSHIPS, ["organisation_id"])
    op.create_index("ix_system_map_relationships_revision_id", _TABLE_SYSTEM_MAP_RELATIONSHIPS, ["revision_id"])
    op.create_index("ix_system_map_relationships_deleted_by", _TABLE_SYSTEM_MAP_RELATIONSHIPS, ["deleted_by"])

    # system_map_loop_configs
    op.create_index("ix_system_map_loop_configs_organisation_id", _TABLE_SYSTEM_MAP_LOOP_CONFIGS, ["organisation_id"])
    op.create_index("ix_system_map_loop_configs_revision_id", _TABLE_SYSTEM_MAP_LOOP_CONFIGS, ["revision_id"])
    op.create_index(
        "ix_system_map_loop_configs_action_pipeline_id", _TABLE_SYSTEM_MAP_LOOP_CONFIGS, ["action_pipeline_id"]
    )
    op.create_index("ix_system_map_loop_configs_deleted_by", _TABLE_SYSTEM_MAP_LOOP_CONFIGS, ["deleted_by"])

    # system_map_attribution_bindings
    op.create_index(
        "ix_system_map_attribution_bindings_organisation_id",
        _TABLE_SYSTEM_MAP_ATTRIBUTION_BINDINGS,
        ["organisation_id"],
    )
    op.create_index(
        "ix_system_map_attribution_bindings_pipeline_id",
        _TABLE_SYSTEM_MAP_ATTRIBUTION_BINDINGS,
        ["pipeline_id"],
    )
    op.create_index(
        "ix_system_map_attribution_bindings_trigger_id",
        _TABLE_SYSTEM_MAP_ATTRIBUTION_BINDINGS,
        ["trigger_id"],
    )
    op.create_index(
        "ix_system_map_attribution_bindings_system_map_id",
        _TABLE_SYSTEM_MAP_ATTRIBUTION_BINDINGS,
        ["system_map_id"],
    )
    op.create_index(
        "ix_system_map_attribution_bindings_deleted_by",
        _TABLE_SYSTEM_MAP_ATTRIBUTION_BINDINGS,
        ["deleted_by"],
    )
    # Partial unique: one trigger binding per (org, pipeline_id, node_key)
    op.execute(
        'CREATE UNIQUE INDEX uq_attr_binding_trigger_per_pipeline_node ON "'
        + _TABLE_SYSTEM_MAP_ATTRIBUTION_BINDINGS
        + "\" (organisation_id, pipeline_id, node_key) WHERE binding_kind = 'trigger'"
    )
    # Partial unique: one action_pipeline binding per (org, pipeline_id, node_key)
    op.execute(
        'CREATE UNIQUE INDEX uq_attr_binding_action_per_pipeline_node ON "'
        + _TABLE_SYSTEM_MAP_ATTRIBUTION_BINDINGS
        + "\" (organisation_id, pipeline_id, node_key) WHERE binding_kind = 'action_pipeline'"
    )

    # system_map_loop_admission_attempts
    op.create_index(
        "ix_loop_admission_attempts_organisation_id",
        _TABLE_SYSTEM_MAP_LOOP_ADMISSION_ATTEMPTS,
        ["organisation_id"],
    )
    op.create_index(
        "ix_loop_admission_attempts_loop_config_id",
        _TABLE_SYSTEM_MAP_LOOP_ADMISSION_ATTEMPTS,
        ["loop_config_id"],
    )
    op.create_index(
        "ix_loop_admission_attempts_deleted_by",
        _TABLE_SYSTEM_MAP_LOOP_ADMISSION_ATTEMPTS,
        ["deleted_by"],
    )

    # system_map_loop_reservations
    op.create_index(
        "ix_loop_reservations_organisation_id",
        _TABLE_SYSTEM_MAP_LOOP_RESERVATIONS,
        ["organisation_id"],
    )
    op.create_index(
        "ix_loop_reservations_loop_config_id",
        _TABLE_SYSTEM_MAP_LOOP_RESERVATIONS,
        ["loop_config_id"],
    )
    op.create_index(
        "ix_loop_reservations_run_id",
        _TABLE_SYSTEM_MAP_LOOP_RESERVATIONS,
        ["run_id"],
    )
    op.create_index(
        "ix_loop_reservations_cooldown_until",
        _TABLE_SYSTEM_MAP_LOOP_RESERVATIONS,
        ["cooldown_until"],
    )
    op.create_index(
        "ix_loop_reservations_deleted_by",
        _TABLE_SYSTEM_MAP_LOOP_RESERVATIONS,
        ["deleted_by"],
    )


_REFERENCE_GRANTS: tuple[str, ...] = (
    _GRANT_REFERENCES_ORGS,
    _GRANT_REFERENCES_TEAMS,
    _GRANT_REFERENCES_ACCOUNTS,
    _GRANT_REFERENCES_PIPELINES,
    _GRANT_REFERENCES_CONNECTORS,
    _GRANT_REFERENCES_EVALS,
    _GRANT_REFERENCES_TRIGGERS,
    _GRANT_REFERENCES_RUNS,
)


def _prepare_roles(bind: sa.Connection) -> tuple[bool, bool, bool]:
    """Detect the runtime roles and grant the migration role its references."""
    migrate_role = _role_exists(bind, _MIGRATE_ROLE)
    app_role = _role_exists(bind, _APP_ROLE)
    system_role = _role_exists(bind, _SYSTEM_ROLE)
    if migrate_role:
        op.execute(_GRANT_CREATE_SCHEMA)
        for grant in _REFERENCE_GRANTS:
            op.execute(grant)
    return migrate_role, app_role, system_role


def _apply_rls(app_role: bool, system_role: bool) -> None:
    """Enable RLS, install the policies, and grant DML to the runtime roles."""
    for statement in _ENABLE_RLS_STATEMENTS:
        op.execute(statement)
    for statement in _CREATE_POLICY_STATEMENTS:
        op.execute(statement)
    if app_role:
        _grant_dml(_APP_ROLE)
    if system_role:
        _grant_dml(_SYSTEM_ROLE)


def upgrade() -> None:
    bind = op.get_bind()
    pg = _is_postgres(bind)

    migrate_role = False
    app_role = False
    system_role = False
    if pg:
        op.execute("SET search_path TO public")
        migrate_role, app_role, system_role = _prepare_roles(bind)

    if migrate_role:
        op.execute(_SET_ROLE_MIGRATE)

    _create_tables()

    if migrate_role:
        op.execute("RESET ROLE")
        for table in _ORG_SCOPED_TABLES:
            _assert_owner_is_migrate(bind, table)

    _create_indexes()

    if pg:
        _apply_rls(app_role, system_role)


def downgrade() -> None:
    bind = op.get_bind()
    if _is_postgres(bind):
        op.execute("SET search_path TO public")
        for statement in _DROP_POLICY_STATEMENTS:
            op.execute(statement)

    # Drop indexes in reverse order
    op.drop_index("ix_loop_reservations_deleted_by", table_name=_TABLE_SYSTEM_MAP_LOOP_RESERVATIONS)
    op.drop_index("ix_loop_reservations_cooldown_until", table_name=_TABLE_SYSTEM_MAP_LOOP_RESERVATIONS)
    op.drop_index("ix_loop_reservations_run_id", table_name=_TABLE_SYSTEM_MAP_LOOP_RESERVATIONS)
    op.drop_index("ix_loop_reservations_loop_config_id", table_name=_TABLE_SYSTEM_MAP_LOOP_RESERVATIONS)
    op.drop_index("ix_loop_reservations_organisation_id", table_name=_TABLE_SYSTEM_MAP_LOOP_RESERVATIONS)

    op.drop_index("ix_loop_admission_attempts_deleted_by", table_name=_TABLE_SYSTEM_MAP_LOOP_ADMISSION_ATTEMPTS)
    op.drop_index("ix_loop_admission_attempts_loop_config_id", table_name=_TABLE_SYSTEM_MAP_LOOP_ADMISSION_ATTEMPTS)
    op.drop_index("ix_loop_admission_attempts_organisation_id", table_name=_TABLE_SYSTEM_MAP_LOOP_ADMISSION_ATTEMPTS)

    op.drop_index("ix_system_map_attribution_bindings_system_map_id", table_name=_TABLE_SYSTEM_MAP_ATTRIBUTION_BINDINGS)
    op.drop_index("ix_system_map_attribution_bindings_trigger_id", table_name=_TABLE_SYSTEM_MAP_ATTRIBUTION_BINDINGS)
    op.drop_index("ix_system_map_attribution_bindings_pipeline_id", table_name=_TABLE_SYSTEM_MAP_ATTRIBUTION_BINDINGS)
    op.drop_index("ix_system_map_attribution_bindings_deleted_by", table_name=_TABLE_SYSTEM_MAP_ATTRIBUTION_BINDINGS)
    op.drop_index(
        "ix_system_map_attribution_bindings_organisation_id", table_name=_TABLE_SYSTEM_MAP_ATTRIBUTION_BINDINGS
    )

    # Drop partial unique indexes
    op.execute("DROP INDEX IF EXISTS uq_attr_binding_action_per_pipeline_node")
    op.execute("DROP INDEX IF EXISTS uq_attr_binding_trigger_per_pipeline_node")

    op.drop_index("ix_system_map_loop_configs_deleted_by", table_name=_TABLE_SYSTEM_MAP_LOOP_CONFIGS)
    op.drop_index("ix_system_map_loop_configs_action_pipeline_id", table_name=_TABLE_SYSTEM_MAP_LOOP_CONFIGS)
    op.drop_index("ix_system_map_loop_configs_revision_id", table_name=_TABLE_SYSTEM_MAP_LOOP_CONFIGS)
    op.drop_index("ix_system_map_loop_configs_organisation_id", table_name=_TABLE_SYSTEM_MAP_LOOP_CONFIGS)

    op.drop_index("ix_system_map_relationships_deleted_by", table_name=_TABLE_SYSTEM_MAP_RELATIONSHIPS)
    op.drop_index("ix_system_map_relationships_revision_id", table_name=_TABLE_SYSTEM_MAP_RELATIONSHIPS)
    op.drop_index("ix_system_map_relationships_organisation_id", table_name=_TABLE_SYSTEM_MAP_RELATIONSHIPS)

    op.drop_index("ix_system_map_nodes_deleted_by", table_name=_TABLE_SYSTEM_MAP_NODES)
    op.drop_index("ix_system_map_nodes_eval_id", table_name=_TABLE_SYSTEM_MAP_NODES)
    op.drop_index("ix_system_map_nodes_connector_instance_id", table_name=_TABLE_SYSTEM_MAP_NODES)
    op.drop_index("ix_system_map_nodes_pipeline_id", table_name=_TABLE_SYSTEM_MAP_NODES)
    op.drop_index("ix_system_map_nodes_revision_id", table_name=_TABLE_SYSTEM_MAP_NODES)
    op.drop_index("ix_system_map_nodes_organisation_id", table_name=_TABLE_SYSTEM_MAP_NODES)

    op.drop_index("ix_system_map_revisions_deleted_by", table_name=_TABLE_SYSTEM_MAP_REVISIONS)
    op.drop_index("ix_system_map_revisions_status", table_name=_TABLE_SYSTEM_MAP_REVISIONS)
    op.drop_index("ix_system_map_revisions_system_map_id", table_name=_TABLE_SYSTEM_MAP_REVISIONS)
    op.drop_index("ix_system_map_revisions_organisation_id", table_name=_TABLE_SYSTEM_MAP_REVISIONS)

    op.drop_index("ix_system_maps_published_revision_id", table_name=_TABLE_SYSTEM_MAPS)
    op.drop_index("ix_system_maps_deleted_by", table_name=_TABLE_SYSTEM_MAPS)
    op.drop_index("ix_system_maps_account_id", table_name=_TABLE_SYSTEM_MAPS)
    op.drop_index("ix_system_maps_owner_team_id", table_name=_TABLE_SYSTEM_MAPS)
    op.drop_index("ix_system_maps_organisation_id", table_name=_TABLE_SYSTEM_MAPS)
    op.execute("DROP INDEX IF EXISTS uq_system_maps_org_default_live")

    for table in reversed(_ORG_SCOPED_TABLES):
        if table != _TABLE_SYSTEM_MAPS:
            op.drop_constraint("fk_" + table + "_deleted_by", table, type_="foreignkey")

    # The composite FK is a forward reference to system_map_revisions, so it
    # must be dropped before that table (which system_maps depends on).
    op.drop_constraint("fk_system_maps_published_revision", _TABLE_SYSTEM_MAPS, type_="foreignkey")

    # Drop tables in reverse dependency order
    op.drop_table(_TABLE_SYSTEM_MAP_LOOP_RESERVATIONS)
    op.drop_table(_TABLE_SYSTEM_MAP_LOOP_ADMISSION_ATTEMPTS)
    op.drop_table(_TABLE_SYSTEM_MAP_ATTRIBUTION_BINDINGS)
    op.drop_table(_TABLE_SYSTEM_MAP_LOOP_CONFIGS)
    op.drop_table(_TABLE_SYSTEM_MAP_RELATIONSHIPS)
    op.drop_table(_TABLE_SYSTEM_MAP_NODES)
    op.drop_table(_TABLE_SYSTEM_MAP_REVISIONS)
    op.drop_table(_TABLE_SYSTEM_MAPS)
