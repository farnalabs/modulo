"""Unit tests for Alembic migration 0025_team_visibility_rls policy SQL generation."""

import importlib.util
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

_MIGRATION_PATH = (
    Path(__file__).resolve().parents[3]
    / "src"
    / "modulo"
    / "db"
    / "migrations"
    / "versions"
    / "0025_team_visibility_rls.py"
)


@pytest.fixture(scope="module")
def migration():
    spec = importlib.util.spec_from_file_location("m0025", str(_MIGRATION_PATH))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class TestMigration0025Constants:
    def test_team_scoped_tables_contains_expected_tables(self, migration) -> None:
        expected = {
            "pipelines",
            "stages",
            "connector_instances",
            "model_backends",
            "library_primitives",
        }
        assert set(migration._TEAM_SCOPED_TABLES) == expected
        assert len(migration._TEAM_SCOPED_TABLES) == 5

    def test_policy_using_covers_all_conditions(self, migration) -> None:
        policy = migration._TEAM_POLICY_USING
        assert "visibility" in policy
        assert "owner_team_id IS NULL" in policy
        assert "team_memberships" in policy
        assert "app.user_id" in policy
        assert "app.org_role" in policy
        assert "admin" in policy

    def test_policy_grants_org_visibility(self, migration) -> None:
        policy = migration._TEAM_POLICY_USING
        assert "visibility = 'org'" in policy
        assert "visibility IS NULL" in policy

    def test_policy_grants_team_membership_access(self, migration) -> None:
        policy = migration._TEAM_POLICY_USING
        assert "SELECT team_id FROM team_memberships" in policy
        assert "WHERE user_id = nullif(current_setting('app.user_id', true), '')::uuid" in policy

    def test_policy_grants_admin_bypass(self, migration) -> None:
        policy = migration._TEAM_POLICY_USING
        assert "nullif(current_setting('app.org_role', true), '') = 'admin'" in policy


class TestMigration0025Upgrade:
    def test_creates_policy_on_each_table(self, migration) -> None:
        mock_op = MagicMock()

        with patch("alembic.op.execute", mock_op.execute), patch.object(migration, "op", mock_op, create=True):
            migration.upgrade()

        assert mock_op.execute.call_count == 5

        for table in migration._TEAM_SCOPED_TABLES:
            call_found = False
            for call_args in mock_op.execute.call_args_list:
                sql_text = call_args[0][0]
                compiled = sql_text.compile(compile_kwargs={"literal_binds": True})
                sql = str(compiled)
                if f'CREATE POLICY rls_team_isolation ON "{table}"' in sql:
                    call_found = True
                    break
            assert call_found, f"No CREATE POLICY call found for table '{table}'"


class TestMigration0025Downgrade:
    def test_drops_policy_from_each_table(self, migration) -> None:
        mock_op = MagicMock()

        with patch("alembic.op.execute", mock_op.execute), patch.object(migration, "op", mock_op, create=True):
            migration.downgrade()

        assert mock_op.execute.call_count == 5

        for table in migration._TEAM_SCOPED_TABLES:
            call_found = False
            for call_args in mock_op.execute.call_args_list:
                sql_text = call_args[0][0]
                compiled = sql_text.compile(compile_kwargs={"literal_binds": True})
                sql = str(compiled)
                if f'DROP POLICY IF EXISTS rls_team_isolation ON "{table}"' in sql:
                    call_found = True
                    break
            assert call_found, f"No DROP POLICY call found for table '{table}'"
