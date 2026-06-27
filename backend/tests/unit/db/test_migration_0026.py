"""Unit tests for Alembic migration 0026_team_rbac_cap SQL generation."""

import importlib.util
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

_MIGRATION_PATH = (
    Path(__file__).resolve().parents[3] / "src" / "modulo" / "db" / "migrations" / "versions" / "0026_team_rbac_cap.py"
)


@pytest.fixture(scope="module")
def migration():
    spec = importlib.util.spec_from_file_location("m0026", str(_MIGRATION_PATH))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class TestMigration0026Constants:
    def test_revision_string(self, migration) -> None:
        assert migration.revision == "0026_team_rbac_cap"

    def test_down_revision(self, migration) -> None:
        assert migration.down_revision == "0025_team_visibility_rls"


class TestMigration0026Upgrade:
    def test_upgrade_executes_five_steps(self, migration) -> None:
        mock_op = MagicMock()

        with patch("alembic.op.execute", mock_op.execute):
            with patch("alembic.op.create_check_constraint", mock_op.create_check_constraint):
                with patch.object(migration, "op", mock_op, create=True):
                    migration.upgrade()

        # UPDATE, ALTER, CHECK, FUNCTION, TRIGGER
        assert mock_op.execute.call_count >= 4
        assert mock_op.create_check_constraint.call_count == 1

    def test_upgrade_migrates_member_to_viewer(self, migration) -> None:
        mock_op = MagicMock()

        with patch("alembic.op.execute", mock_op.execute):
            with patch("alembic.op.create_check_constraint", mock_op.create_check_constraint):
                with patch.object(migration, "op", mock_op, create=True):
                    migration.upgrade()

        update_calls = [
            str(c[0][0].compile(compile_kwargs={"literal_binds": True}))
            for c in mock_op.execute.call_args_list
            if str(c[0][0].compile(compile_kwargs={"literal_binds": True})).startswith("UPDATE team_memberships")
        ]
        assert len(update_calls) == 1
        assert "team_memberships" in update_calls[0]
        assert "viewer" in update_calls[0]
        assert "member" in update_calls[0]

    def test_upgrade_sets_default_to_viewer(self, migration) -> None:
        mock_op = MagicMock()

        with patch("alembic.op.execute", mock_op.execute):
            with patch("alembic.op.create_check_constraint", mock_op.create_check_constraint):
                with patch.object(migration, "op", mock_op, create=True):
                    migration.upgrade()

        alter_calls = [
            str(c[0][0].compile(compile_kwargs={"literal_binds": True}))
            for c in mock_op.execute.call_args_list
            if "ALTER COLUMN role SET DEFAULT" in str(c[0][0])
        ]
        assert len(alter_calls) == 1
        assert "'viewer'" in alter_calls[0]

    def test_upgrade_creates_check_constraint(self, migration) -> None:
        mock_op = MagicMock()

        with patch("alembic.op.execute", mock_op.execute):
            with patch("alembic.op.create_check_constraint", mock_op.create_check_constraint):
                with patch.object(migration, "op", mock_op, create=True):
                    migration.upgrade()

        assert mock_op.create_check_constraint.call_count == 1
        name, table, _ = mock_op.create_check_constraint.call_args[0]
        assert name == "ck_team_memberships_role"
        assert table == "team_memberships"

    def test_upgrade_contains_trigger_function(self, migration) -> None:
        mock_op = MagicMock()

        with patch("alembic.op.execute", mock_op.execute):
            with patch("alembic.op.create_check_constraint", mock_op.create_check_constraint):
                with patch.object(migration, "op", mock_op, create=True):
                    migration.upgrade()

        all_sql = " ".join(
            str(c[0][0].compile(compile_kwargs={"literal_binds": True})) for c in mock_op.execute.call_args_list
        )
        assert "CREATE OR REPLACE FUNCTION check_team_privilege_cap" in all_sql
        assert "RAISE EXCEPTION" in all_sql

    def test_upgrade_contains_trigger_attachment(self, migration) -> None:
        mock_op = MagicMock()

        with patch("alembic.op.execute", mock_op.execute):
            with patch("alembic.op.create_check_constraint", mock_op.create_check_constraint):
                with patch.object(migration, "op", mock_op, create=True):
                    migration.upgrade()

        all_sql = " ".join(
            str(c[0][0].compile(compile_kwargs={"literal_binds": True})) for c in mock_op.execute.call_args_list
        )
        assert "CREATE TRIGGER trg_team_privilege_cap" in all_sql


class TestMigration0026Downgrade:
    def test_downgrade_drops_trigger(self, migration) -> None:
        mock_op = MagicMock()

        with patch("alembic.op.execute", mock_op.execute):
            with patch("alembic.op.drop_constraint", mock_op.drop_constraint):
                with patch.object(migration, "op", mock_op, create=True):
                    migration.downgrade()

        drop_trigger_calls = [
            str(c[0][0].compile(compile_kwargs={"literal_binds": True}))
            for c in mock_op.execute.call_args_list
            if "DROP TRIGGER" in str(c[0][0])
        ]
        assert len(drop_trigger_calls) == 1
        assert "trg_team_privilege_cap" in drop_trigger_calls[0]

    def test_downgrade_drops_function(self, migration) -> None:
        mock_op = MagicMock()

        with patch("alembic.op.execute", mock_op.execute):
            with patch("alembic.op.drop_constraint", mock_op.drop_constraint):
                with patch.object(migration, "op", mock_op, create=True):
                    migration.downgrade()

        drop_func_calls = [
            str(c[0][0].compile(compile_kwargs={"literal_binds": True}))
            for c in mock_op.execute.call_args_list
            if "DROP FUNCTION" in str(c[0][0])
        ]
        assert len(drop_func_calls) == 1
        assert "check_team_privilege_cap" in drop_func_calls[0]

    def test_downgrade_drops_constraint(self, migration) -> None:
        mock_op = MagicMock()

        with patch("alembic.op.execute", mock_op.execute):
            with patch("alembic.op.drop_constraint", mock_op.drop_constraint):
                with patch.object(migration, "op", mock_op, create=True):
                    migration.downgrade()

        assert mock_op.drop_constraint.call_count == 1
        name, table = mock_op.drop_constraint.call_args[0][:2]
        assert name == "ck_team_memberships_role"
        assert table == "team_memberships"

    def test_downgrade_reverts_default_to_member(self, migration) -> None:
        mock_op = MagicMock()

        with patch("alembic.op.execute", mock_op.execute):
            with patch("alembic.op.drop_constraint", mock_op.drop_constraint):
                with patch.object(migration, "op", mock_op, create=True):
                    migration.downgrade()

        alter_calls = [
            str(c[0][0].compile(compile_kwargs={"literal_binds": True}))
            for c in mock_op.execute.call_args_list
            if "ALTER COLUMN role SET DEFAULT" in str(c[0][0])
        ]
        assert len(alter_calls) == 1
        assert "'member'" in alter_calls[0]
