"""Tests for Remy tables in the current squashed features migration."""

import importlib.util
from pathlib import Path
from types import ModuleType
from unittest.mock import MagicMock, patch

import pytest

_MIGRATION_PATH = (
    Path(__file__).resolve().parents[3]
    / "src"
    / "modulo"
    / "db"
    / "migrations"
    / "versions"
    / "0005_v2_features_system.py"
)
_REMY_TABLES = {"chat_sessions", "chat_messages", "remy_skills"}


@pytest.fixture(scope="module")
def migration() -> ModuleType:
    spec = importlib.util.spec_from_file_location("migration_0005_remy_tables", _MIGRATION_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def create_tables_op(migration: ModuleType) -> MagicMock:
    mock_op = MagicMock()
    mock_op.f.side_effect = lambda name: name
    with patch.object(migration, "op", mock_op):
        migration._create_tables()
    return mock_op


class TestRemyTableUpgrade:
    def test_creates_all_remy_tables(self, create_tables_op: MagicMock) -> None:
        table_names = {call.args[0] for call in create_tables_op.create_table.call_args_list}
        assert table_names >= _REMY_TABLES

    def test_creates_remy_indexes(self, create_tables_op: MagicMock) -> None:
        index_names = {call.args[0] for call in create_tables_op.create_index.call_args_list}
        assert {
            "ix_chat_sessions_organisation_id",
            "ix_chat_sessions_user_id",
            "ix_chat_messages_organisation_id",
            "ix_chat_messages_session_id",
            "ix_remy_skills_organisation_id",
            "ix_remy_skills_user_id",
        } <= index_names

    def test_declares_remy_check_constraints(self, create_tables_op: MagicMock) -> None:
        calls_by_table = {call.args[0]: call.args[1:] for call in create_tables_op.create_table.call_args_list}
        constraint_names = {
            item.name for table in _REMY_TABLES for item in calls_by_table[table] if getattr(item, "name", None)
        }
        assert "ck_chat_messages_role" in constraint_names
        assert "ck_remy_skills_owner" in constraint_names


class TestRemyTableDowngrade:
    def test_drops_remy_tables_in_dependency_order(self, migration: ModuleType) -> None:
        mock_op = MagicMock()
        mock_op.f.side_effect = lambda name: name
        with patch.object(migration, "op", mock_op):
            migration.downgrade()

        dropped_tables = [call.args[0] for call in mock_op.drop_table.call_args_list]
        positions = {table: dropped_tables.index(table) for table in _REMY_TABLES}
        assert positions["remy_skills"] < positions["chat_messages"] < positions["chat_sessions"]

    def test_drops_all_remy_indexes_before_their_tables(self, migration: ModuleType) -> None:
        mock_op = MagicMock()
        mock_op.f.side_effect = lambda name: name
        with patch.object(migration, "op", mock_op):
            migration.downgrade()

        dropped_indexes = {call.args[0] for call in mock_op.drop_index.call_args_list}
        assert {
            "ix_remy_skills_user_id",
            "ix_remy_skills_organisation_id",
            "ix_chat_messages_session_id",
            "ix_chat_messages_organisation_id",
            "ix_chat_sessions_user_id",
            "ix_chat_sessions_organisation_id",
        } <= dropped_indexes
