"""Unit tests for Alembic migration 0049_remy_tables SQL generation."""

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
    / "0049_remy_tables.py"
)


@pytest.fixture(scope="module")
def migration():
    spec = importlib.util.spec_from_file_location("m0049", str(_MIGRATION_PATH))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class TestMigration0049Constants:
    def test_revision_string(self, migration) -> None:
        assert migration.revision == "0049_remy_tables"

    def test_down_revision(self, migration) -> None:
        assert migration.down_revision == "0048_tier_catalog"


class TestMigration0049Upgrade:
    def test_upgrade_creates_three_tables(self, migration) -> None:
        mock_op = MagicMock()

        with patch("alembic.op.create_table", mock_op.create_table):
            with patch("alembic.op.create_index", mock_op.create_index):
                with patch("alembic.op.create_check_constraint", mock_op.create_check_constraint):
                    with patch.object(migration, "op", mock_op, create=True):
                        migration.upgrade()

        assert mock_op.create_table.call_count == 3
        table_names = [call[0][0] for call in mock_op.create_table.call_args_list]
        assert "chat_sessions" in table_names
        assert "chat_messages" in table_names
        assert "remy_skills" in table_names

    def test_upgrade_creates_four_indexes(self, migration) -> None:
        mock_op = MagicMock()

        with patch("alembic.op.create_table", mock_op.create_table):
            with patch("alembic.op.create_index", mock_op.create_index):
                with patch("alembic.op.create_check_constraint", mock_op.create_check_constraint):
                    with patch.object(migration, "op", mock_op, create=True):
                        migration.upgrade()

        assert mock_op.create_index.call_count == 4
        index_names = [call[0][0] for call in mock_op.create_index.call_args_list]
        assert "ix_chat_sessions_user_id" in index_names
        assert "ix_chat_messages_session_id" in index_names
        assert "ix_remy_skills_organisation_id" in index_names
        assert "ix_remy_skills_user_id" in index_names

    def test_upgrade_creates_two_check_constraints(self, migration) -> None:
        mock_op = MagicMock()

        with patch("alembic.op.create_table", mock_op.create_table):
            with patch("alembic.op.create_index", mock_op.create_index):
                with patch("alembic.op.create_check_constraint", mock_op.create_check_constraint):
                    with patch.object(migration, "op", mock_op, create=True):
                        migration.upgrade()

        assert mock_op.create_check_constraint.call_count == 2
        names = [call[0][0] for call in mock_op.create_check_constraint.call_args_list]
        assert "ck_remy_skills_owner" in names
        assert "ck_chat_messages_role" in names

    def test_upgrade_chat_sessions_has_all_columns(self, migration) -> None:
        mock_op = MagicMock()

        with patch("alembic.op.create_table", mock_op.create_table):
            with patch("alembic.op.create_index", mock_op.create_index):
                with patch("alembic.op.create_check_constraint", mock_op.create_check_constraint):
                    with patch.object(migration, "op", mock_op, create=True):
                        migration.upgrade()

        chat_sessions_call = [
            c for c in mock_op.create_table.call_args_list if c[0][0] == "chat_sessions"
        ][0]
        columns = chat_sessions_call[0][1:]
        col_names = [c.name for c in columns]
        assert "id" in col_names
        assert "organisation_id" in col_names
        assert "user_id" in col_names
        assert "provider" in col_names
        assert "model" in col_names
        assert "context_window_tokens" in col_names
        assert "name" in col_names
        assert "system_prompt_hash" in col_names
        assert "created_at" in col_names
        assert "updated_at" in col_names


class TestMigration0049Downgrade:
    def test_downgrade_drops_constraints_before_tables(self, migration) -> None:
        mock_op = MagicMock()

        with patch("alembic.op.drop_constraint", mock_op.drop_constraint):
            with patch("alembic.op.drop_table", mock_op.drop_table):
                with patch.object(migration, "op", mock_op, create=True):
                    migration.downgrade()

        assert mock_op.drop_constraint.call_count == 2
        constraint_names = [call[0][0] for call in mock_op.drop_constraint.call_args_list]
        assert "ck_chat_messages_role" in constraint_names
        assert "ck_remy_skills_owner" in constraint_names

    def test_downgrade_drops_tables_in_reverse_order(self, migration) -> None:
        mock_op = MagicMock()

        with patch("alembic.op.drop_constraint", mock_op.drop_constraint):
            with patch("alembic.op.drop_table", mock_op.drop_table):
                with patch.object(migration, "op", mock_op, create=True):
                    migration.downgrade()

        assert mock_op.drop_table.call_count == 3
        table_names = [call[0][0] for call in mock_op.drop_table.call_args_list]
        assert table_names == ["remy_skills", "chat_messages", "chat_sessions"]
