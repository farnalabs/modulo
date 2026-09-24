"""FAR-1196 Tier 3: migration 0257 - physical ``remy_*`` -> ``assistant_*`` rename.

Two lenses, following the repo's migration-test patterns:

* **Structure (Postgres branch, mocked ``op``)** - upgrade/downgrade must emit
  every required table/constraint/index/trigger rename, the RLS re-assert,
  and the system_config prefix rewrite. No live Postgres needed.
* **Round-trip (in-memory SQLite, real ``op`` via ``Operations.context``)** -
  the portable branch (table renames + data rewrite) upgrades, is idempotent,
  and downgrades cleanly.

Model parity is asserted against the renamed ORM models so a constraint-name
drift between model and migration fails here before ``test_initial_migration``
surfaces it against a live DB in CI.
"""

from __future__ import annotations

import importlib.util
from collections.abc import Iterator
from pathlib import Path
from types import ModuleType
from unittest.mock import MagicMock, patch

import pytest
import sqlalchemy as sa
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import CheckConstraint, UniqueConstraint

_MIGRATION_REVISION = "0257_rename_remy_to_assistant"
_MIGRATION_PATH = (
    Path(__file__).resolve().parents[3]
    / "src"
    / "modulo"
    / "db"
    / "migrations"
    / "versions"
    / f"{_MIGRATION_REVISION}.py"
)

_TABLE_RENAMES = (
    'ALTER TABLE public."remy_skills" RENAME TO "assistant_skills"',
    'ALTER TABLE public."remy_context_sources" RENAME TO "assistant_context_sources"',
)
_TABLE_RENAMES_DOWN = (
    'ALTER TABLE public."assistant_skills" RENAME TO "remy_skills"',
    'ALTER TABLE public."assistant_context_sources" RENAME TO "remy_context_sources"',
)
_CONSTRAINT_RENAMES = (
    'RENAME CONSTRAINT "remy_skills_pkey" TO "assistant_skills_pkey"',
    'RENAME CONSTRAINT "ck_remy_skills_owner" TO "ck_assistant_skills_owner"',
    'RENAME CONSTRAINT "remy_skills_organisation_id_fkey" TO "assistant_skills_organisation_id_fkey"',
    'RENAME CONSTRAINT "remy_skills_account_id_fkey" TO "assistant_skills_account_id_fkey"',
    'RENAME CONSTRAINT "remy_context_sources_pkey" TO "assistant_context_sources_pkey"',
    'RENAME CONSTRAINT "ck_remy_context_sources_owner" TO "ck_assistant_context_sources_owner"',
    'RENAME CONSTRAINT "ck_remy_context_sources_mode" TO "ck_assistant_context_sources_mode"',
    'RENAME CONSTRAINT "uq_remy_context_sources_key" TO "uq_assistant_context_sources_key"',
    'RENAME CONSTRAINT "remy_context_sources_organisation_id_fkey" TO "assistant_context_sources_organisation_id_fkey"',
    'RENAME CONSTRAINT "remy_context_sources_account_id_fkey" TO "assistant_context_sources_account_id_fkey"',
)
_INDEX_RENAMES = (
    'ALTER INDEX public."ix_remy_skills_organisation_id" RENAME TO "ix_assistant_skills_organisation_id"',
    'ALTER INDEX public."ix_remy_skills_account_id" RENAME TO "ix_assistant_skills_account_id"',
    'ALTER INDEX public."ix_remy_context_sources_organisation_id" '
    'RENAME TO "ix_assistant_context_sources_organisation_id"',
)
_TRIGGER_RENAMES = (
    'ALTER TRIGGER "trg_remy_skills_account_id_tenant" ON public."assistant_skills" '
    'RENAME TO "trg_assistant_skills_account_id_tenant"',
    'ALTER TRIGGER "trg_remy_context_sources_account_id_tenant" ON public."assistant_context_sources" '
    'RENAME TO "trg_assistant_context_sources_account_id_tenant"',
)
_RLS_STATEMENTS = (
    'ALTER TABLE public."assistant_skills" ENABLE ROW LEVEL SECURITY',
    'ALTER TABLE public."assistant_context_sources" ENABLE ROW LEVEL SECURITY',
    'CREATE POLICY rls_org_isolation ON public."assistant_skills"',
    'CREATE POLICY rls_org_isolation ON public."assistant_context_sources"',
)
_CONFIG_FORWARD = (
    "UPDATE system_config SET key = replace(key, 'remy_config:', 'assistant_config:') WHERE key LIKE 'remy_config:%'"
)
_CONFIG_REVERSE = (
    "UPDATE system_config SET key = replace(key, 'assistant_config:', 'remy_config:') WHERE key LIKE "
    "'assistant_config:%'"
)


def _load_migration() -> ModuleType:
    assert _MIGRATION_PATH.exists(), f"Migration file missing: {_MIGRATION_PATH}"
    spec = importlib.util.spec_from_file_location(f"migration_{_MIGRATION_REVISION}", _MIGRATION_PATH)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _pg_statements(module: ModuleType, fn: str) -> str:
    """Run upgrade/downgrade against a mocked Postgres-bound ``op``."""
    mock_op = MagicMock()
    mock_op.get_bind.return_value.dialect.name = "postgresql"
    with patch.object(module, "op", mock_op):
        getattr(module, fn)()
    return "\n".join(call.args[0] for call in mock_op.execute.call_args_list)


@pytest.fixture(scope="module")
def pg_upgrade_sql() -> str:
    return _pg_statements(_load_migration(), "upgrade")


@pytest.fixture(scope="module")
def pg_downgrade_sql() -> str:
    return _pg_statements(_load_migration(), "downgrade")


class TestMigrationMetadata:
    def test_revision_id(self) -> None:
        assert _load_migration().revision == _MIGRATION_REVISION

    def test_down_revision_is_current_head(self) -> None:
        """Chains onto the head `uv run alembic heads` resolved at delivery."""
        assert _load_migration().down_revision == "0256_pipeline_max_autonomy_level"


class TestPostgresUpgradeStructure:
    @pytest.fixture
    def sql(self, pg_upgrade_sql: str) -> str:
        return pg_upgrade_sql

    @pytest.mark.parametrize("fragment", _TABLE_RENAMES)
    def test_renames_tables(self, sql: str, fragment: str) -> None:
        assert fragment in sql

    @pytest.mark.parametrize("fragment", _CONSTRAINT_RENAMES)
    def test_renames_constraints(self, sql: str, fragment: str) -> None:
        assert fragment in sql

    @pytest.mark.parametrize("fragment", _INDEX_RENAMES)
    def test_renames_indexes(self, sql: str, fragment: str) -> None:
        assert fragment in sql

    @pytest.mark.parametrize("fragment", _TRIGGER_RENAMES)
    def test_renames_triggers(self, sql: str, fragment: str) -> None:
        assert fragment in sql

    @pytest.mark.parametrize("fragment", _RLS_STATEMENTS)
    def test_reasserts_rls(self, sql: str, fragment: str) -> None:
        assert fragment in sql

    def test_rewrites_config_key_prefix(self, sql: str) -> None:
        assert _CONFIG_FORWARD in sql

    def test_every_statement_is_guarded(self, sql: str) -> None:
        """Idempotency: each emitted statement carries an existence guard."""
        for statement in sql.splitlines():
            assert "IF " in statement

    def test_does_not_touch_nonexistent_skills_mode_check(self, sql: str) -> None:
        """The ticket listed ``ck_remy_skills_mode``; no such constraint
        exists in any shipped migration or the model, so none is renamed."""
        assert "ck_remy_skills_mode" not in sql


class TestPostgresDowngradeStructure:
    @pytest.fixture
    def sql(self, pg_downgrade_sql: str) -> str:
        return pg_downgrade_sql

    @pytest.mark.parametrize("fragment", _TABLE_RENAMES_DOWN)
    def test_reverses_table_renames(self, sql: str, fragment: str) -> None:
        assert fragment in sql

    def test_reverses_constraint_renames(self, sql: str) -> None:
        for fragment in (
            'RENAME CONSTRAINT "assistant_skills_pkey" TO "remy_skills_pkey"',
            'RENAME CONSTRAINT "ck_assistant_skills_owner" TO "ck_remy_skills_owner"',
            'RENAME CONSTRAINT "assistant_skills_organisation_id_fkey" TO "remy_skills_organisation_id_fkey"',
            'RENAME CONSTRAINT "assistant_skills_account_id_fkey" TO "remy_skills_account_id_fkey"',
            'RENAME CONSTRAINT "assistant_context_sources_pkey" TO "remy_context_sources_pkey"',
            'RENAME CONSTRAINT "ck_assistant_context_sources_owner" TO "ck_remy_context_sources_owner"',
            'RENAME CONSTRAINT "ck_assistant_context_sources_mode" TO "ck_remy_context_sources_mode"',
            'RENAME CONSTRAINT "uq_assistant_context_sources_key" TO "uq_remy_context_sources_key"',
            'RENAME CONSTRAINT "assistant_context_sources_organisation_id_fkey" '
            'TO "remy_context_sources_organisation_id_fkey"',
            'RENAME CONSTRAINT "assistant_context_sources_account_id_fkey" TO "remy_context_sources_account_id_fkey"',
        ):
            assert fragment in sql

    def test_reverses_index_renames(self, sql: str) -> None:
        for fragment in (
            'ALTER INDEX public."ix_assistant_skills_organisation_id" RENAME TO "ix_remy_skills_organisation_id"',
            'ALTER INDEX public."ix_assistant_skills_account_id" RENAME TO "ix_remy_skills_account_id"',
            'ALTER INDEX public."ix_assistant_context_sources_organisation_id" '
            'RENAME TO "ix_remy_context_sources_organisation_id"',
        ):
            assert fragment in sql

    def test_reverses_trigger_renames(self, sql: str) -> None:
        for fragment in (
            'ALTER TRIGGER "trg_assistant_skills_account_id_tenant" ON public."assistant_skills" '
            'RENAME TO "trg_remy_skills_account_id_tenant"',
            'ALTER TRIGGER "trg_assistant_context_sources_account_id_tenant" ON public."assistant_context_sources" '
            'RENAME TO "trg_remy_context_sources_account_id_tenant"',
        ):
            assert fragment in sql

    def test_reverses_config_key_prefix(self, sql: str) -> None:
        assert _CONFIG_REVERSE in sql


def _scaffold(conn: sa.Connection) -> None:
    """The pre-migration table shapes (SQLite portable branch)."""
    conn.execute(sa.text("CREATE TABLE remy_skills (id INTEGER PRIMARY KEY, organisation_id CHAR(32))"))
    conn.execute(sa.text("CREATE TABLE remy_context_sources (id INTEGER PRIMARY KEY, organisation_id CHAR(32))"))
    conn.execute(sa.text("CREATE TABLE system_config (id INTEGER PRIMARY KEY, key TEXT NOT NULL, value TEXT NOT NULL)"))
    conn.execute(sa.text("INSERT INTO system_config (id, key, value) VALUES (1, 'remy_config:org-1', '{}')"))


def _run(engine: sa.Engine, module: ModuleType, fn: str) -> None:
    with engine.begin() as conn:
        context = MigrationContext.configure(conn)
        with Operations.context(context):
            getattr(module, fn)()


def _table_names(engine: sa.Engine) -> set[str]:
    with engine.connect() as conn:
        return set(sa.inspect(conn).get_table_names())


def _config_key(engine: sa.Engine) -> str:
    with engine.connect() as conn:
        return str(conn.execute(sa.text("SELECT key FROM system_config WHERE id = 1")).scalar_one())


@pytest.fixture
def sqlite_engine() -> Iterator[sa.Engine]:
    engine = sa.create_engine("sqlite://", poolclass=sa.pool.StaticPool)
    yield engine
    engine.dispose()


class TestSqliteRoundTrip:
    def test_upgrade_renames_tables(self, sqlite_engine: sa.Engine) -> None:
        with sqlite_engine.begin() as conn:
            _scaffold(conn)
        _run(sqlite_engine, _load_migration(), "upgrade")
        tables = _table_names(sqlite_engine)
        assert "assistant_skills" in tables
        assert "assistant_context_sources" in tables

    def test_upgrade_rewrites_config_key(self, sqlite_engine: sa.Engine) -> None:
        with sqlite_engine.begin() as conn:
            _scaffold(conn)
        _run(sqlite_engine, _load_migration(), "upgrade")
        assert _config_key(sqlite_engine) == "assistant_config:org-1"

    def test_upgrade_is_idempotent(self, sqlite_engine: sa.Engine) -> None:
        with sqlite_engine.begin() as conn:
            _scaffold(conn)
        module = _load_migration()
        _run(sqlite_engine, module, "upgrade")
        _run(sqlite_engine, module, "upgrade")
        tables = _table_names(sqlite_engine)
        assert "assistant_skills" in tables
        assert _config_key(sqlite_engine) == "assistant_config:org-1"

    def test_downgrade_restores_original_names(self, sqlite_engine: sa.Engine) -> None:
        with sqlite_engine.begin() as conn:
            _scaffold(conn)
        module = _load_migration()
        _run(sqlite_engine, module, "upgrade")
        _run(sqlite_engine, module, "downgrade")
        tables = _table_names(sqlite_engine)
        assert "remy_skills" in tables
        assert "remy_context_sources" in tables
        assert _config_key(sqlite_engine) == "remy_config:org-1"

    def test_downgrade_never_drops_tables(self, sqlite_engine: sa.Engine) -> None:
        with sqlite_engine.begin() as conn:
            _scaffold(conn)
            conn.execute(sa.text("INSERT INTO remy_skills (id, organisation_id) VALUES (1, 'org1')"))
        module = _load_migration()
        _run(sqlite_engine, module, "upgrade")
        _run(sqlite_engine, module, "downgrade")
        with sqlite_engine.connect() as conn:
            count = conn.execute(sa.text("SELECT COUNT(*) FROM remy_skills")).scalar_one()
        assert count == 1


class TestModelParity:
    """The migration's rename targets are exactly what the models declare."""

    def test_model_table_names(self) -> None:
        from modulo.db.models.assistant_context_source import AssistantContextSource
        from modulo.db.models.assistant_skill import AssistantSkill

        assert AssistantSkill.__table__.name == "assistant_skills"
        assert AssistantContextSource.__table__.name == "assistant_context_sources"

    def test_migration_targets_cover_model_constraints(self) -> None:
        from modulo.db.models.assistant_context_source import AssistantContextSource
        from modulo.db.models.assistant_skill import AssistantSkill

        module = _load_migration()
        migration_targets = {new for _table, _old, new in module._CONSTRAINTS}
        model_names: set[str] = set()
        for model in (AssistantSkill, AssistantContextSource):
            for constraint in model.__table__.constraints:
                if isinstance(constraint, CheckConstraint | UniqueConstraint) and constraint.name is not None:
                    model_names.add(constraint.name)
        assert model_names
        assert model_names <= migration_targets

    def test_models_declare_no_skills_mode_check(self) -> None:
        """Guards the ticket correction: the skills table never had one."""
        from modulo.db.models.assistant_skill import AssistantSkill

        check_names = {c.name for c in AssistantSkill.__table__.constraints if isinstance(c, CheckConstraint)}
        assert check_names == {"ck_assistant_skills_owner"}

    def test_chat_tables_are_not_renamed(self) -> None:
        """chat_sessions/chat_messages table names are unchanged by 0257."""
        from modulo.db.models.assistant_message import ChatMessage
        from modulo.db.models.assistant_session import ChatSession

        assert ChatSession.__table__.name == "chat_sessions"
        assert ChatMessage.__table__.name == "chat_messages"

        source = _MIGRATION_PATH.read_text(encoding="utf-8")
        assert "chat_sessions" not in source.split('"""', 2)[-1]
        assert "chat_messages" not in source.split('"""', 2)[-1]
