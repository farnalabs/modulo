"""External-agent + MCP-server registries, with MCP tool-definition pinning (FAR-1692 S1+S2).

Revision ID: 0314_external_agents_mcp_servers
Revises: 0313_collection_install_drop_installed_entities
Create Date: 2026-10-14

Two new org-scoped registry tables (FAR-1692 Decision D4):

* ``external_agents`` — non-Modulo agents (Claude Code, Cursor, opencode, ...),
  policy substrate and a record, NOT enforcement (ADR 055). Auto-populated by a
  later ingestion slice; manual entry allowed now.
* ``mcp_servers`` — the customer-side MCP servers their agents consume. Carries
  the S2 pinning columns: ``tools_json`` / ``tools_hash`` (last observed tool
  set + its canonical hash) and the approval record (``approval_status`` /
  ``approved_by`` / ``approved_at`` / ``approved_hash``). Drift detection
  (``approved_hash`` != observed hash -> ``approved-stale`` + notification +
  audit) is detect-and-surface only — see ``core/mcp/tool_pinning.py``.

Both tables carry the team-visibility pair (``visibility``/``owner_team_id``),
so each gets ONLY the full ``rls_team_isolation`` policy — no separate
org-only ``rls_org_isolation``: Postgres ORs permissive policies, so an org-only
policy alongside a team policy makes the team policy dead weight (the cross-team
leak migration 0124 fixed; 0287 repeats the pattern). This matches the core
team-scoped tables' DB-RLS-only posture (FAR-1514) — no request-time team gate
is wired on the new routes.

RLS — ENABLE + FORCE ROW LEVEL SECURITY + ``rls_team_isolation`` on both tables
(owner ``modulo_migrate``), plus DML grants to ``modulo_app``/``modulo_system``.
The ``OrgScoped`` ORM mixin alone is insufficient: the app role must not read a
cross-org (or cross-team) row.

AUTHORING NOTE (semgrep ``migration-fstring-sql``): this revision is above the
rule's 0247 exemption cutoff, so NO ``op.execute(f"...")`` appears here. Every
interpolated value is a module-level constant (role/table names), assembled
with plain string concatenation — no untrusted DDL reaches ``op.execute``.

Aditively reversible: downgrade drops both tables + their policies/grants.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision: str = "0314_external_agents_mcp_servers"
down_revision: str | None = "0313_collection_install_drop_installed_entities"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_MIGRATE_ROLE = "modulo_migrate"
_APP_ROLE = "modulo_app"
_SYSTEM_ROLE = "modulo_system"

_TABLE_EXTERNAL_AGENTS = "external_agents"
_TABLE_MCP_SERVERS = "mcp_servers"

# DDL fragments reused across the statement lists below, named once so the
# literal is defined in a single place (Sonar S1192) and the intent is explicit.
_ALTER_TABLE_PREFIX = 'ALTER TABLE "'
_SET_NULL = "SET NULL"

# Module-level tuple of the org-scoped tables this migration secures, so the
# architecture test (tests/unit/db/test_rls_coverage.py) sees the coverage
# without parsing the DDL below.
_ORG_SCOPED_TABLES: tuple[str, ...] = (_TABLE_EXTERNAL_AGENTS, _TABLE_MCP_SERVERS)

# JSON columns are jsonb on Postgres (0147 standardisation), generic JSON
# elsewhere; identical SQLAlchemy expression to the ORM models.
_JSON_TYPE = sa.JSON().with_variant(JSONB(), "postgresql")

# Verbatim 0124/0287 team policy: the org check is an AND gate (no cross-org
# leak), and the execution-context escape hatch only widens the team clause
# WITHIN the organisation (background reads set app.execution_context='true').
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

# Plain string concatenation (never an f-string, never a direct f-string
# argument to op.execute) keeps the migration-fstring-sql rule satisfied while
# interpolating only module-level constants.
_ENABLE_RLS_STATEMENTS: tuple[str, ...] = (
    _ALTER_TABLE_PREFIX + _TABLE_EXTERNAL_AGENTS + '" ENABLE ROW LEVEL SECURITY',
    _ALTER_TABLE_PREFIX + _TABLE_EXTERNAL_AGENTS + '" FORCE ROW LEVEL SECURITY',
    _ALTER_TABLE_PREFIX + _TABLE_MCP_SERVERS + '" ENABLE ROW LEVEL SECURITY',
    _ALTER_TABLE_PREFIX + _TABLE_MCP_SERVERS + '" FORCE ROW LEVEL SECURITY',
)

_CREATE_POLICY_STATEMENTS: tuple[str, ...] = (
    'CREATE POLICY rls_team_isolation ON "' + _TABLE_EXTERNAL_AGENTS + '" USING (' + _TEAM_POLICY + ")",
    'CREATE POLICY rls_team_isolation ON "' + _TABLE_MCP_SERVERS + '" USING (' + _TEAM_POLICY + ")",
)

_DROP_POLICY_STATEMENTS: tuple[str, ...] = (
    'DROP POLICY IF EXISTS rls_team_isolation ON "' + _TABLE_EXTERNAL_AGENTS + '"',
    'DROP POLICY IF EXISTS rls_team_isolation ON "' + _TABLE_MCP_SERVERS + '"',
    _ALTER_TABLE_PREFIX + _TABLE_EXTERNAL_AGENTS + '" DISABLE ROW LEVEL SECURITY',
    _ALTER_TABLE_PREFIX + _TABLE_MCP_SERVERS + '" DISABLE ROW LEVEL SECURITY',
)

_GRANT_CREATE_SCHEMA = "GRANT CREATE ON SCHEMA public TO " + _MIGRATE_ROLE
_GRANT_REFERENCES_ORGS = "GRANT REFERENCES ON TABLE public.organisations TO " + _MIGRATE_ROLE
_GRANT_REFERENCES_TEAMS = "GRANT REFERENCES ON TABLE public.teams TO " + _MIGRATE_ROLE
_GRANT_REFERENCES_ACCOUNTS = "GRANT REFERENCES ON TABLE public.accounts TO " + _MIGRATE_ROLE
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
    op.create_table(
        _TABLE_EXTERNAL_AGENTS,
        sa.Column("id", sa.Uuid(), nullable=False, server_default=sa.text("gen_random_uuid()")),
        sa.Column("organisation_id", sa.Uuid(), nullable=False),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("runtime_kind", sa.String(length=50), nullable=False, server_default="other"),
        sa.Column("host_descriptor", sa.String(length=500), nullable=True),
        sa.Column("status", sa.String(length=30), nullable=False, server_default="active"),
        sa.Column("discovery_provenance", sa.String(length=30), nullable=False, server_default="manual"),
        sa.Column("first_seen", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_seen", sa.DateTime(timezone=True), nullable=True),
        sa.Column("visibility", sa.String(length=10), nullable=False, server_default="org"),
        sa.Column("owner_team_id", sa.Uuid(), nullable=True),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.current_timestamp(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.current_timestamp(), nullable=False),
        sa.ForeignKeyConstraint(["organisation_id"], ["organisations.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["owner_team_id"], ["teams.id"], ondelete=_SET_NULL),
        sa.PrimaryKeyConstraint("id"),
        sa.CheckConstraint("visibility IN ('org', 'team')", name="ck_external_agents_visibility"),
        sa.CheckConstraint("visibility = 'org' OR owner_team_id IS NOT NULL", name="ck_external_agents_team_owner"),
        sa.CheckConstraint(
            "runtime_kind IN ('claude-code', 'cursor', 'opencode', 'other')",
            name="ck_external_agents_runtime_kind",
        ),
        sa.CheckConstraint("status IN ('active', 'inactive', 'unknown')", name="ck_external_agents_status"),
        sa.CheckConstraint(
            "discovery_provenance IN ('manual', 'mcp', 'webhook', 'forge')",
            name="ck_external_agents_discovery_provenance",
        ),
    )
    op.create_table(
        _TABLE_MCP_SERVERS,
        sa.Column("id", sa.Uuid(), nullable=False, server_default=sa.text("gen_random_uuid()")),
        sa.Column("organisation_id", sa.Uuid(), nullable=False),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("transport", sa.String(length=30), nullable=False),
        sa.Column("endpoint", sa.String(length=2000), nullable=True),
        sa.Column("command", sa.String(length=2000), nullable=True),
        sa.Column("declared_scopes", _JSON_TYPE, nullable=False),
        sa.Column("owner_account_id", sa.Uuid(), nullable=True),
        sa.Column("credentials_ciphertext", sa.LargeBinary(), nullable=True),
        sa.Column("tools_json", _JSON_TYPE, nullable=False),
        sa.Column("tools_hash", sa.String(length=64), nullable=True),
        sa.Column("approval_status", sa.String(length=30), nullable=False, server_default="unapproved"),
        sa.Column("approved_by", sa.Uuid(), nullable=True),
        sa.Column("approved_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("approved_hash", sa.String(length=64), nullable=True),
        sa.Column("last_observed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("visibility", sa.String(length=10), nullable=False, server_default="org"),
        sa.Column("owner_team_id", sa.Uuid(), nullable=True),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.current_timestamp(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.current_timestamp(), nullable=False),
        sa.ForeignKeyConstraint(["organisation_id"], ["organisations.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["owner_team_id"], ["teams.id"], ondelete=_SET_NULL),
        sa.ForeignKeyConstraint(["owner_account_id"], ["accounts.id"], ondelete=_SET_NULL),
        sa.ForeignKeyConstraint(["approved_by"], ["accounts.id"], ondelete=_SET_NULL),
        sa.PrimaryKeyConstraint("id"),
        sa.CheckConstraint("visibility IN ('org', 'team')", name="ck_mcp_servers_visibility"),
        sa.CheckConstraint("visibility = 'org' OR owner_team_id IS NOT NULL", name="ck_mcp_servers_team_owner"),
        sa.CheckConstraint("transport IN ('stdio', 'sse', 'http', 'websocket')", name="ck_mcp_servers_transport"),
        sa.CheckConstraint(
            "approval_status IN ('unapproved', 'approved', 'approved-stale', 'blocked')",
            name="ck_mcp_servers_approval_status",
        ),
    )


def _create_indexes() -> None:
    op.create_index("ix_external_agents_organisation_id", _TABLE_EXTERNAL_AGENTS, ["organisation_id"])
    op.create_index("ix_external_agents_owner_team_id", _TABLE_EXTERNAL_AGENTS, ["owner_team_id"])
    op.create_index("ix_mcp_servers_organisation_id", _TABLE_MCP_SERVERS, ["organisation_id"])
    op.create_index("ix_mcp_servers_owner_team_id", _TABLE_MCP_SERVERS, ["owner_team_id"])
    op.create_index("ix_mcp_servers_owner_account_id", _TABLE_MCP_SERVERS, ["owner_account_id"])


def upgrade() -> None:
    bind = op.get_bind()
    pg = _is_postgres(bind)

    # migrate_role can only be True when pg (it is resolved under the pg guard
    # below), so the later `if migrate_role` checks need no `and pg` conjunct.
    migrate_role = False
    app_role = False
    system_role = False
    if pg:
        op.execute("SET search_path TO public")
        migrate_role = _role_exists(bind, _MIGRATE_ROLE)
        app_role = _role_exists(bind, _APP_ROLE)
        system_role = _role_exists(bind, _SYSTEM_ROLE)
        if migrate_role:
            op.execute(_GRANT_CREATE_SCHEMA)
            op.execute(_GRANT_REFERENCES_ORGS)
            op.execute(_GRANT_REFERENCES_TEAMS)
            op.execute(_GRANT_REFERENCES_ACCOUNTS)

    if migrate_role:
        op.execute(_SET_ROLE_MIGRATE)

    _create_tables()

    if migrate_role:
        op.execute("RESET ROLE")
        _assert_owner_is_migrate(bind, _TABLE_EXTERNAL_AGENTS)
        _assert_owner_is_migrate(bind, _TABLE_MCP_SERVERS)

    _create_indexes()

    if pg:
        for statement in _ENABLE_RLS_STATEMENTS:
            op.execute(statement)
        for statement in _CREATE_POLICY_STATEMENTS:
            op.execute(statement)
        if app_role:
            _grant_dml(_APP_ROLE)
        if system_role:
            _grant_dml(_SYSTEM_ROLE)


def downgrade() -> None:
    bind = op.get_bind()
    if _is_postgres(bind):
        op.execute("SET search_path TO public")
        for statement in _DROP_POLICY_STATEMENTS:
            op.execute(statement)

    op.drop_index("ix_mcp_servers_owner_account_id", table_name=_TABLE_MCP_SERVERS)
    op.drop_index("ix_mcp_servers_owner_team_id", table_name=_TABLE_MCP_SERVERS)
    op.drop_index("ix_mcp_servers_organisation_id", table_name=_TABLE_MCP_SERVERS)
    op.drop_index("ix_external_agents_owner_team_id", table_name=_TABLE_EXTERNAL_AGENTS)
    op.drop_index("ix_external_agents_organisation_id", table_name=_TABLE_EXTERNAL_AGENTS)

    op.drop_table(_TABLE_MCP_SERVERS)
    op.drop_table(_TABLE_EXTERNAL_AGENTS)
