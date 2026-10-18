"""Unit tests for migration 0314_external_agents_mcp_servers (FAR-1692 S1+S2).

Structural contract (no Postgres / Testcontainers needed). Every sibling
migration (0304-0310) pins its own shape with a dedicated test; this gives 0314
the same first-class coverage. It pins the four things a reviewer needs to trust
the migration without a live DB:

* **Revision chain** - 0314 revises ``0313_collection_install_drop_installed_entities``; the
  migrations directory has exactly one head (``0317_drop_retired_pipeline_diff_rollback_flag``, which
  chains onto ``0316_policy_gate_evaluation_mode``, which
  chains onto ``0315_service_accounts_lifecycle``, which chains onto
  ``0314_external_agents_mcp_servers``), the revision id matches the filename,
  and neither ``branch_labels`` nor ``depends_on`` is set (the pre-commit
  check-migration-heads hook and every ``test_single_head_*`` pin depend on that shape).
* **Org-scoped RLS coverage** - ``_ORG_SCOPED_TABLES`` names both new tables, so
  ``test_rls_coverage.py`` sees them without parsing the DDL.
* **Upgrade (mocked ``op``)** - on Postgres it runs the new-table ceremony
  (``SET ROLE modulo_migrate`` -> create -> ownership assertion -> ENABLE/FORCE
  ROW LEVEL SECURITY + the verbatim ``rls_team_isolation`` team policy on BOTH
  tables -> DML grants) and creates both tables with their indexes; on SQLite it
  creates the tables with no ceremony.
* **RLS statement list** - pinned exactly (2x ENABLE + 2x FORCE + 2x
  ``CREATE POLICY rls_team_isolation``), and the policy is the 0124/0287
  AND-gated team policy - no dead org-only ``rls_org_isolation`` policy
  (Postgres ORs permissive policies; an org-only policy beside a team policy is
  dead weight, the 0124 cross-team-leak bug).
* **CHECK constraints** - both tables' vocabularies are pinned.
* **Downgrade** - drops the policies, disables RLS, drops the indexes and the
  two tables (additively reversible).
* **Model parity** - the ORM models carry the same CHECK names/expressions the
  migration writes and the same indexes, so ``create_all`` and a migrated DB
  agree and model/migration drift cannot silently reappear.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path
from types import ModuleType
from unittest.mock import MagicMock, patch

import sqlalchemy as sa
from alembic.script import ScriptDirectory

from modulo.db.models import ExternalAgent, McpServer

_VERSIONS = Path(__file__).resolve().parents[3] / "src" / "modulo" / "db" / "migrations" / "versions"
_MIGRATION_NAME = "0314_external_agents_mcp_servers"
_MIGRATION_PATH = _VERSIONS / f"{_MIGRATION_NAME}.py"
_DOWN_REVISION = "0313_collection_install_drop_installed_entities"
_HEAD_MIGRATION = "0320_evidence_redaction_rules"

_EXTERNAL_AGENTS = "external_agents"
_MCP_SERVERS = "mcp_servers"

_EXPECTED_INDEXES = {
    "ix_external_agents_organisation_id",
    "ix_external_agents_owner_team_id",
    "ix_mcp_servers_organisation_id",
    "ix_mcp_servers_owner_team_id",
    "ix_mcp_servers_owner_account_id",
}

_EXTERNAL_AGENT_CHECKS = {
    "ck_external_agents_visibility",
    "ck_external_agents_team_owner",
    "ck_external_agents_runtime_kind",
    "ck_external_agents_status",
    "ck_external_agents_discovery_provenance",
}
_MCP_SERVER_CHECKS = {
    "ck_mcp_servers_visibility",
    "ck_mcp_servers_team_owner",
    "ck_mcp_servers_transport",
    "ck_mcp_servers_approval_status",
}
_TEAM_OWNER_EXPRESSION = "visibility = 'org' OR owner_team_id IS NOT NULL"


def _load_migration() -> ModuleType:
    assert _MIGRATION_PATH.exists(), f"Migration file missing: {_MIGRATION_PATH}"
    spec = importlib.util.spec_from_file_location(f"migration_{_MIGRATION_NAME}", _MIGRATION_PATH)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class _Recorder:
    """Captures op.execute SQL, op.create_table and op.create_index calls."""

    def __init__(self) -> None:
        self.sql: list[str] = []
        self.tables: list[str] = []
        self.columns: dict[str, list[str]] = {}
        self.checks: dict[str, dict[str, str]] = {}
        self.indexes: list[tuple[str, str, list[str]]] = []

    def execute(self, stmt: object, *_args: object, **_kwargs: object) -> MagicMock:
        self.sql.append(str(stmt))
        return MagicMock()

    def create_table(self, name: str, *args: object, **_kwargs: object) -> None:
        self.tables.append(name)
        self.columns[name] = [a.name for a in args if isinstance(a, sa.Column)]
        self.checks[name] = {a.name: str(a.sqltext) for a in args if isinstance(a, sa.CheckConstraint)}

    def create_index(self, name: str, table_name: str, columns: list[str], *_args: object, **_kwargs: object) -> None:
        self.indexes.append((name, table_name, list(columns)))

    def drop_index(self, name: str, *_args: object, **_kwargs: object) -> None:
        self.indexes = [i for i in self.indexes if i[0] != name]

    def drop_table(self, name: str, *_args: object, **_kwargs: object) -> None:
        if name in self.tables:
            self.tables.remove(name)


def _make_bind(dialect: str) -> MagicMock:
    bind = MagicMock()
    bind.dialect.name = dialect
    return bind


def _run(what: str, dialect: str, *, roles_exist: bool) -> _Recorder:
    """Run ``upgrade``/``downgrade`` with a mocked ``op`` and stubbed role probes."""
    migration = _load_migration()
    rec = _Recorder()
    bind = _make_bind(dialect)
    rec.get_bind = MagicMock(return_value=bind)  # type: ignore[attr-defined]
    with (
        patch.object(migration, "op", rec),
        patch.object(migration, "_role_exists", return_value=roles_exist),
        patch.object(migration, "_assert_owner_is_migrate", return_value=None),
    ):
        getattr(migration, what)()
    return rec


def _joined(rec: _Recorder) -> str:
    return "\n".join(rec.sql)


class TestChain:
    def test_single_head_is_0317_drop_retired_pipeline_diff_rollback_flag(self) -> None:
        heads = ScriptDirectory(str(_VERSIONS.parent)).get_heads()
        assert heads == [_HEAD_MIGRATION], f"expected a single head, got {heads}"

    def test_down_revision_is_0313_collection_install_drop_installed_entities(self) -> None:
        assert _load_migration().down_revision == _DOWN_REVISION

    def test_revision_id_matches_filename(self) -> None:
        assert _load_migration().revision == _MIGRATION_NAME

    def test_no_branch_labels_or_depends_on(self) -> None:
        module = _load_migration()
        assert module.branch_labels is None
        assert module.depends_on is None


class TestOrgScopedCoverage:
    def test_org_scoped_tables_names_both_new_tables(self) -> None:
        # test_rls_coverage.py reads this tuple to prove every org-scoped ORM
        # table has an ENABLE ROW LEVEL SECURITY migration.
        assert _load_migration()._ORG_SCOPED_TABLES == (_EXTERNAL_AGENTS, _MCP_SERVERS)


class TestUpgradeSqlite:
    def test_creates_both_tables_without_ceremony(self) -> None:
        rec = _run("upgrade", "sqlite", roles_exist=False)
        assert set(rec.tables) == {_EXTERNAL_AGENTS, _MCP_SERVERS}
        joined = _joined(rec).upper()
        assert "SET ROLE" not in joined
        assert "ROW LEVEL SECURITY" not in joined

    def test_uses_organisation_id_not_org_id(self) -> None:
        rec = _run("upgrade", "sqlite", roles_exist=False)
        assert "organisation_id" in rec.columns[_EXTERNAL_AGENTS]
        assert "organisation_id" in rec.columns[_MCP_SERVERS]
        assert "org_id" not in _joined(rec)

    def test_creates_the_five_indexes(self) -> None:
        rec = _run("upgrade", "sqlite", roles_exist=False)
        assert {name for name, _table, _cols in rec.indexes} == _EXPECTED_INDEXES


class TestUpgradePostgres:
    def test_full_ceremony_present(self) -> None:
        joined = _joined(_run("upgrade", "postgresql", roles_exist=True))
        assert "SET ROLE modulo_migrate" in joined
        assert "RESET ROLE" in joined
        assert "ENABLE ROW LEVEL SECURITY" in joined
        assert "FORCE ROW LEVEL SECURITY" in joined
        assert "CREATE POLICY rls_team_isolation" in joined
        assert "GRANT " in joined
        assert "modulo_app" in joined
        assert "modulo_system" in joined

    def test_rls_statement_list_is_pinned(self) -> None:
        module = _load_migration()
        assert (
            f'ALTER TABLE "{_EXTERNAL_AGENTS}" ENABLE ROW LEVEL SECURITY',
            f'ALTER TABLE "{_EXTERNAL_AGENTS}" FORCE ROW LEVEL SECURITY',
            f'ALTER TABLE "{_MCP_SERVERS}" ENABLE ROW LEVEL SECURITY',
            f'ALTER TABLE "{_MCP_SERVERS}" FORCE ROW LEVEL SECURITY',
        ) == module._ENABLE_RLS_STATEMENTS
        assert len(module._CREATE_POLICY_STATEMENTS) == 2
        for table in (_EXTERNAL_AGENTS, _MCP_SERVERS):
            assert any(
                statement.startswith(f'CREATE POLICY rls_team_isolation ON "{table}" USING (')
                for statement in module._CREATE_POLICY_STATEMENTS
            ), f"no rls_team_isolation policy for {table}"

        rec = _run("upgrade", "postgresql", roles_exist=True)
        for statement in (*module._ENABLE_RLS_STATEMENTS, *module._CREATE_POLICY_STATEMENTS):
            assert rec.sql.count(statement) == 1, f"expected exactly one {statement!r}"

    def test_policy_is_the_verbatim_team_isolation_policy(self) -> None:
        module = _load_migration()
        for statement in module._CREATE_POLICY_STATEMENTS:
            assert "organisation_id = (NULLIF(current_setting('app.organisation_id'" in statement
            assert "owner_team_id IN ( SELECT team_memberships.team_id" in statement
            assert "app.execution_context" in statement

    def test_no_dead_org_only_policy(self) -> None:
        # Postgres ORs permissive policies: an org-only rls_org_isolation beside
        # the team policy would be dead weight and re-open the 0124 leak. 0314
        # ships ONLY rls_team_isolation on both tables.
        joined = _joined(_run("upgrade", "postgresql", roles_exist=True))
        assert "rls_org_isolation" not in joined

    def test_ownership_assertion_invoked_for_both_tables(self) -> None:
        migration = _load_migration()
        rec = _Recorder()
        bind = _make_bind("postgresql")
        rec.get_bind = MagicMock(return_value=bind)  # type: ignore[attr-defined]
        with (
            patch.object(migration, "op", rec),
            patch.object(migration, "_role_exists", return_value=True),
            patch.object(migration, "_assert_owner_is_migrate") as assert_mock,
        ):
            migration.upgrade()
        asserted = {call.args[1] for call in assert_mock.call_args_list}
        assert asserted == {_EXTERNAL_AGENTS, _MCP_SERVERS}

    def test_owner_assertion_skipped_when_migrate_role_absent(self) -> None:
        # Without the modulo_migrate role there is no SET ROLE, no owner
        # assertion and no grants - the tables are still created (SQLite/pre-role
        # bootstrap path).
        migration = _load_migration()
        rec = _Recorder()
        bind = _make_bind("postgresql")
        rec.get_bind = MagicMock(return_value=bind)  # type: ignore[attr-defined]
        with (
            patch.object(migration, "op", rec),
            patch.object(migration, "_role_exists", return_value=False),
            patch.object(migration, "_assert_owner_is_migrate") as assert_mock,
        ):
            migration.upgrade()
        assert set(rec.tables) == {_EXTERNAL_AGENTS, _MCP_SERVERS}
        assert_mock.assert_not_called()


class TestUpgradeChecks:
    def test_external_agents_check_constraints(self) -> None:
        rec = _run("upgrade", "sqlite", roles_exist=False)
        assert set(rec.checks[_EXTERNAL_AGENTS]) == _EXTERNAL_AGENT_CHECKS
        assert rec.checks[_EXTERNAL_AGENTS]["ck_external_agents_visibility"] == "visibility IN ('org', 'team')"
        assert (
            rec.checks[_EXTERNAL_AGENTS]["ck_external_agents_runtime_kind"]
            == "runtime_kind IN ('claude-code', 'cursor', 'opencode', 'other')"
        )
        assert (
            rec.checks[_EXTERNAL_AGENTS]["ck_external_agents_discovery_provenance"]
            == "discovery_provenance IN ('manual', 'mcp', 'webhook', 'forge')"
        )

    def test_mcp_servers_check_constraints(self) -> None:
        rec = _run("upgrade", "sqlite", roles_exist=False)
        assert set(rec.checks[_MCP_SERVERS]) == _MCP_SERVER_CHECKS
        assert (
            rec.checks[_MCP_SERVERS]["ck_mcp_servers_transport"] == "transport IN ('stdio', 'sse', 'http', 'websocket')"
        )
        assert (
            rec.checks[_MCP_SERVERS]["ck_mcp_servers_approval_status"]
            == "approval_status IN ('unapproved', 'approved', 'approved-stale', 'blocked')"
        )

    def test_team_owner_check_on_both_tables(self) -> None:
        rec = _run("upgrade", "sqlite", roles_exist=False)
        assert rec.checks[_EXTERNAL_AGENTS]["ck_external_agents_team_owner"] == _TEAM_OWNER_EXPRESSION
        assert rec.checks[_MCP_SERVERS]["ck_mcp_servers_team_owner"] == _TEAM_OWNER_EXPRESSION


class TestDowngrade:
    def test_drops_policies_and_disables_rls(self) -> None:
        joined = _joined(_run("downgrade", "postgresql", roles_exist=True))
        for table in (_EXTERNAL_AGENTS, _MCP_SERVERS):
            assert f'DROP POLICY IF EXISTS rls_team_isolation ON "{table}"' in joined
            assert f'ALTER TABLE "{table}" DISABLE ROW LEVEL SECURITY' in joined

    def test_drops_indexes_and_tables(self) -> None:
        rec = _run("downgrade", "postgresql", roles_exist=True)
        assert not rec.indexes
        assert not rec.tables


class TestModelParity:
    def test_external_agent_carries_the_migration_checks(self) -> None:
        model_checks = {
            c.name: str(c.sqltext) for c in ExternalAgent.__table__.constraints if isinstance(c, sa.CheckConstraint)
        }
        rec = _run("upgrade", "sqlite", roles_exist=False)
        assert model_checks == rec.checks[_EXTERNAL_AGENTS]

    def test_mcp_server_carries_the_migration_checks(self) -> None:
        model_checks = {
            c.name: str(c.sqltext) for c in McpServer.__table__.constraints if isinstance(c, sa.CheckConstraint)
        }
        rec = _run("upgrade", "sqlite", roles_exist=False)
        assert model_checks == rec.checks[_MCP_SERVERS]

    def test_models_expose_the_org_scope_and_team_visibility_pair(self) -> None:
        for model, table in ((ExternalAgent, _EXTERNAL_AGENTS), (McpServer, _MCP_SERVERS)):
            assert table == model.__tablename__
            columns = set(model.__table__.columns.keys())
            assert {"organisation_id", "visibility", "owner_team_id", "deleted_at"} <= columns

    def test_models_declare_the_migration_indexes(self) -> None:
        for model, names in (
            (ExternalAgent, {"ix_external_agents_organisation_id", "ix_external_agents_owner_team_id"}),
            (
                McpServer,
                {
                    "ix_mcp_servers_organisation_id",
                    "ix_mcp_servers_owner_team_id",
                    "ix_mcp_servers_owner_account_id",
                },
            ),
        ):
            assert names <= {index.name for index in model.__table__.indexes}
