"""Tests for team-visibility RLS in the current squashed migrations."""

import importlib.util
from pathlib import Path
from types import ModuleType
from unittest.mock import MagicMock, patch

import pytest

_VERSIONS = Path(__file__).resolve().parents[3] / "src" / "modulo" / "db" / "migrations" / "versions"


def _load_migration(filename: str, module_name: str) -> ModuleType:
    spec = importlib.util.spec_from_file_location(module_name, _VERSIONS / filename)
    assert spec is not None and spec.loader is not None
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
