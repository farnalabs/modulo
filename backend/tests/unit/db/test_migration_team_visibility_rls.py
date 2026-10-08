"""Tests for team-visibility RLS in the current squashed migrations."""

import importlib.util
from pathlib import Path
from types import ModuleType, SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

_VERSIONS = Path(__file__).resolve().parents[3] / "src" / "modulo" / "db" / "migrations" / "versions"


def _load_migration(filename: str, module_name: str) -> ModuleType:
    spec = importlib.util.spec_from_file_location(module_name, _VERSIONS / filename)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def team_library_migration() -> ModuleType:
    return _load_migration("0002_v2_teams_library.py", "migration_0002_team_rls")


@pytest.fixture(scope="module")
def pipeline_runtime_migration() -> ModuleType:
    return _load_migration("0003_v2_pipeline_runtime.py", "migration_0003_team_rls")


@pytest.fixture(scope="module")
def team_migrations(
    team_library_migration: ModuleType, pipeline_runtime_migration: ModuleType
) -> tuple[ModuleType, ...]:
    return team_library_migration, pipeline_runtime_migration


def _compiled_execute_sql(mock_op: MagicMock) -> list[str]:
    return [
        str(call.args[0].compile(compile_kwargs={"literal_binds": True})) for call in mock_op.execute.call_args_list
    ]


class TestTeamScopedTables:
    def test_combined_migrations_cover_every_team_scoped_table(self, team_migrations: tuple[ModuleType, ...]) -> None:
        actual = {table for migration in team_migrations for table in migration._TEAM_SCOPED_RLS}
        assert actual == {
            "library_primitives",
            "pipelines",
            "stages",
            "connector_instances",
            "model_backends",
            "environment_profiles",
        }

    def test_each_team_scoped_table_is_also_tenant_scoped(self, team_migrations: tuple[ModuleType, ...]) -> None:
        for migration in team_migrations:
            assert set(migration._TEAM_SCOPED_RLS) <= set(migration._STRICT_RLS)


class TestTeamPolicyUpgrade:
    @pytest.mark.parametrize("fixture_name", ["team_library_migration", "pipeline_runtime_migration"])
    def test_creates_complete_policy_for_each_owned_table(
        self, fixture_name: str, request: pytest.FixtureRequest
    ) -> None:
        migration = request.getfixturevalue(fixture_name)
        mock_op = MagicMock()

        with patch.object(migration, "op", mock_op):
            migration._enable_rls()

        sql_statements = _compiled_execute_sql(mock_op)
        team_policy_sql = [sql for sql in sql_statements if "CREATE POLICY rls_team_isolation" in sql]
        assert len(team_policy_sql) == len(migration._TEAM_SCOPED_RLS)
        for table in migration._TEAM_SCOPED_RLS:
            sql = next(sql for sql in team_policy_sql if f'ON "{table}"' in sql)
            assert "visibility = 'org'" in sql
            assert "visibility IS NULL" in sql
            assert "owner_team_id IS NULL" in sql
            assert "SELECT team_id FROM team_memberships" in sql
            assert "account_id = nullif(current_setting('app.user_id', true), '')::uuid" in sql
            assert "nullif(current_setting('app.org_role', true), '') = 'admin'" in sql


class TestTeamPolicyDowngrade:
    @pytest.mark.parametrize("fixture_name", ["team_library_migration", "pipeline_runtime_migration"])
    def test_drops_team_policy_from_each_owned_table(self, fixture_name: str, request: pytest.FixtureRequest) -> None:
        migration = request.getfixturevalue(fixture_name)
        mock_op = MagicMock()

        with patch.object(migration, "op", mock_op):
            migration.downgrade()

        sql_statements = _compiled_execute_sql(mock_op)
        for table in migration._TEAM_SCOPED_RLS:
            assert f'DROP POLICY IF EXISTS rls_team_isolation ON "{table}"' in sql_statements


# ---------------------------------------------------------------------------
# 0287_team_rls_lifecycle_evals — the three neither-layer tables (FAR-1514)
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def lifecycle_evals_team_rls_migration() -> ModuleType:
    return _load_migration("0287_team_rls_lifecycle_evals.py", "migration_0287_lifecycle_evals_team_rls")


def _executed_sql(mock_op: MagicMock) -> list[str]:
    """Plain-string statements executed by a migration (0287 uses f-strings)."""
    return [str(call.args[0]) for call in mock_op.execute.call_args_list]


class TestLifecycleEvalsTeamPolicy:
    """Upgrade: org-only policy dropped, team policy (with exec context) created."""

    @staticmethod
    def _pg_op() -> MagicMock:
        """A mock ``op`` whose dialect reports postgresql (0287 guards on it).

        ``dialect`` is a SimpleNamespace, not a MagicMock: ``name`` is a
        reserved Mock attribute, so assigning ``mock.name = "postgresql"``
        stores the mock's own name instead of the value.
        """
        mock_op = MagicMock()
        mock_op.get_context.return_value.dialect = SimpleNamespace(name="postgresql")
        return mock_op

    def test_targets_exactly_the_three_neither_layer_tables(
        self, lifecycle_evals_team_rls_migration: ModuleType
    ) -> None:
        assert set(lifecycle_evals_team_rls_migration._TEAM_SCOPED_TABLES) == {
            "lifecycle_maps",
            "eval_datasets",
            "eval_suites",
        }

    def test_upgrade_drops_org_only_and_creates_team_policy(
        self, lifecycle_evals_team_rls_migration: ModuleType
    ) -> None:
        migration = lifecycle_evals_team_rls_migration
        mock_op = self._pg_op()

        with patch.object(migration, "op", mock_op):
            migration.upgrade()

        statements = _executed_sql(mock_op)
        for table in migration._TEAM_SCOPED_TABLES:
            assert f"DROP POLICY IF EXISTS rls_org_isolation ON public.{table}" in statements, (
                f"{table}: org-only policy must be dropped — Postgres ORs permissive policies, "
                "so keeping it would make rls_team_isolation dead weight (the 0124 cross-team leak)"
            )
            create = next(s for s in statements if s.startswith(f"CREATE POLICY rls_team_isolation ON public.{table}"))
            # Full visibility matrix, verbatim 0124.
            assert "app.organisation_id" in create, f"{table}: team policy lost the org check (cross-org leak)"
            assert "(visibility)::text = 'org'::text" in create, f"{table}: team policy missing the org-visibility arm"
            assert "owner_team_id IS NULL" in create, f"{table}: team policy missing the unowned arm"
            assert "SELECT team_id FROM team_memberships" in create or "team_memberships.team_id" in create, (
                f"{table}: team policy missing the membership arm"
            )
            assert "app.user_id" in create, f"{table}: team policy missing the account binding"
            assert "app.org_role" in create, f"{table}: team policy missing the admin arm"
            # Execution-context escape hatch (background machinery reads with
            # org scope only — 0124).
            assert "app.execution_context" in create, (
                f"{table}: team policy missing the execution-context escape hatch — background "
                "reads (executor/cron/housekeeping) would lose team-private rows"
            )

    def test_upgrade_is_existence_guarded(self, lifecycle_evals_team_rls_migration: ModuleType) -> None:
        """Every DROP is guarded so a re-run (or partial run) is a no-op."""
        migration = lifecycle_evals_team_rls_migration
        mock_op = self._pg_op()

        with patch.object(migration, "op", mock_op):
            migration.upgrade()

        drops = [s for s in _executed_sql(mock_op) if s.startswith("DROP POLICY")]
        assert drops, "upgrade executed no DROP statements"
        for stmt in drops:
            assert "DROP POLICY IF EXISTS" in stmt, f"unguarded DROP would break idempotency: {stmt}"

    def test_upgrade_is_postgres_only(self, lifecycle_evals_team_rls_migration: ModuleType) -> None:
        """No policy DDL on the deprecated MariaDB / SQLite backends."""
        migration = lifecycle_evals_team_rls_migration
        mock_op = MagicMock()
        mock_op.get_context.return_value.dialect = SimpleNamespace(name="sqlite")

        with patch.object(migration, "op", mock_op):
            migration.upgrade()

        mock_op.execute.assert_not_called()

    def test_downgrade_restores_org_only_policy(self, lifecycle_evals_team_rls_migration: ModuleType) -> None:
        migration = lifecycle_evals_team_rls_migration
        mock_op = self._pg_op()

        with patch.object(migration, "op", mock_op):
            migration.downgrade()

        statements = _executed_sql(mock_op)
        for table in migration._TEAM_SCOPED_TABLES:
            assert f"DROP POLICY IF EXISTS rls_team_isolation ON public.{table}" in statements
            assert any(s.startswith(f"CREATE POLICY rls_org_isolation ON public.{table}") for s in statements), (
                f"{table}: downgrade must restore the pre-0287 org-only policy"
            )
