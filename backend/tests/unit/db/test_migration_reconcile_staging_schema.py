"""Idempotency tests for the 0064 staging schema-drift reconciliation migration.

0064 must be a no-op on a healthy schema (all three tables present in their
current shape) and must repair a drifted pre-squash schema (missing
``mcp_setup_tokens`` / ``lifecycle_maps``, legacy ``scheduled_reports``). These
tests exercise ``upgrade()`` against a mocked inspect/op surface to assert both
paths without a live Postgres.
"""

import importlib.util
from pathlib import Path
from types import ModuleType
from typing import ClassVar
from unittest.mock import MagicMock, patch

import pytest

_VERSIONS = Path(__file__).resolve().parents[3] / "src" / "modulo" / "db" / "migrations" / "versions"


def _load_migration(filename: str, module_name: str) -> ModuleType:
    spec = importlib.util.spec_from_file_location(module_name, _VERSIONS / filename)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class _FakeInspector:
    """Mimics ``sqlalchemy.inspect(bind)`` for the migration's existence checks."""

    def __init__(self, tables: dict[str, list[str]]) -> None:
        self._tables = tables

    def has_table(self, table: str) -> bool:
        return table in self._tables

    def get_columns(self, table: str) -> list[dict[str, str]]:
        return [{"name": column} for column in self._tables.get(table, [])]


@pytest.fixture(scope="module")
def reconcile_migration() -> ModuleType:
    return _load_migration("0064_reconcile_staging_schema.py", "migration_0064_reconcile")


def _run_upgrade(
    reconcile_migration: ModuleType,
    tables: dict[str, list[str]],
    scheduled_report_rows: int = 0,
) -> MagicMock:
    """Run ``upgrade()`` with a mocked inspector + op, returning the mock op."""
    mock_bind = MagicMock()
    mock_result = MagicMock()
    mock_result.scalar_one.return_value = scheduled_report_rows
    mock_bind.execute.return_value = mock_result

    mock_op = MagicMock()
    mock_op.get_bind.return_value = mock_bind
    mock_op.f.side_effect = lambda name: name

    with (
        patch.object(reconcile_migration, "op", mock_op),
        patch.object(reconcile_migration.sa, "inspect", return_value=_FakeInspector(tables)),
    ):
        reconcile_migration.upgrade()
    return mock_op


_HEALTHY: dict[str, list[str]] = {
    "mcp_setup_tokens": ["id", "created_by", "resource_id", "token_hash", "organisation_id"],
    "lifecycle_maps": ["id", "account_id", "organisation_id", "deleted_at"],
    "scheduled_reports": ["id", "created_by", "report_type", "organisation_id"],
}


class TestReconcileHealthySchema:
    def test_upgrade_is_noop_when_tables_exist_in_current_shape(self, reconcile_migration: ModuleType) -> None:
        mock_op = _run_upgrade(reconcile_migration, _HEALTHY)

        assert mock_op.create_table.call_args_list == []
        assert mock_op.drop_table.call_args_list == []
        assert mock_op.create_index.call_args_list == []
        assert mock_op.create_foreign_key.call_args_list == []
        assert mock_op.execute.call_args_list == []


class TestReconcileDriftedSchema:
    _DRIFTED: ClassVar[dict[str, list[str]]] = {
        "scheduled_reports": ["id", "organisation_id", "period", "group_by", "format", "account_id"],
    }

    def test_creates_missing_mcp_setup_tokens_and_lifecycle_maps(self, reconcile_migration: ModuleType) -> None:
        mock_op = _run_upgrade(reconcile_migration, self._DRIFTED)

        created_tables = {call.args[0] for call in mock_op.create_table.call_args_list}
        assert "mcp_setup_tokens" in created_tables
        assert "lifecycle_maps" in created_tables
        assert "scheduled_reports" in created_tables

    def test_drops_legacy_scheduled_reports(self, reconcile_migration: ModuleType) -> None:
        mock_op = _run_upgrade(reconcile_migration, self._DRIFTED)

        dropped_tables = [call.args[0] for call in mock_op.drop_table.call_args_list]
        assert "scheduled_reports" in dropped_tables

    def test_creates_indexes_for_all_three_tables(self, reconcile_migration: ModuleType) -> None:
        mock_op = _run_upgrade(reconcile_migration, self._DRIFTED)

        index_names = {call.args[0] for call in mock_op.create_index.call_args_list}
        assert {
            "ix_mcp_setup_tokens_organisation_id",
            "ix_mcp_setup_tokens_resource_id",
            "ix_lifecycle_maps_organisation_id",
            "ix_lifecycle_maps_account_id",
            "ix_scheduled_reports_organisation_id",
            "ix_scheduled_reports_report_type",
            "ix_scheduled_reports_created_by",
        } <= index_names

    def test_creates_mcp_setup_tokens_created_by_fk(self, reconcile_migration: ModuleType) -> None:
        mock_op = _run_upgrade(reconcile_migration, self._DRIFTED)

        fk_names = {call.args[0] for call in mock_op.create_foreign_key.call_args_list}
        assert "fk_mcp_setup_tokens_created_by" in fk_names

    def test_enables_rls_on_all_three_tables(self, reconcile_migration: ModuleType) -> None:
        mock_op = _run_upgrade(reconcile_migration, self._DRIFTED)

        executed_sql = [str(call.args[0].text) for call in mock_op.execute.call_args_list]
        for table in ("mcp_setup_tokens", "lifecycle_maps", "scheduled_reports"):
            assert any(f'ALTER TABLE "{table}" ENABLE ROW LEVEL SECURITY' in sql for sql in executed_sql)
            assert any(f'CREATE POLICY rls_org_isolation ON "{table}"' in sql for sql in executed_sql)

    def test_refuses_to_drop_populated_legacy_scheduled_reports(self, reconcile_migration: ModuleType) -> None:
        mock_op = _run_upgrade(reconcile_migration, self._DRIFTED, scheduled_report_rows=3)

        assert mock_op.drop_table.call_args_list == []
        created_tables = {call.args[0] for call in mock_op.create_table.call_args_list}
        assert created_tables == {"mcp_setup_tokens", "lifecycle_maps"}

    def test_reconciles_legacy_scheduled_reports_after_615_created_by_add(
        self, reconcile_migration: ModuleType
    ) -> None:
        """Legacy shape that PR #615 already added created_by to must still reconcile.

        0037_add_scheduled_reports_created_by adds the column + index idempotently
        but leaves the table legacy-shaped (period/group_by remain). Detection by
        ``period`` (not ``created_by`` absence) must still fire the drop+recreate.
        """
        post_615 = {
            "scheduled_reports": [
                "id",
                "organisation_id",
                "period",
                "group_by",
                "format",
                "account_id",
                "created_by",
            ],
        }
        mock_op = _run_upgrade(reconcile_migration, post_615)

        dropped_tables = [call.args[0] for call in mock_op.drop_table.call_args_list]
        assert "scheduled_reports" in dropped_tables
        assert "scheduled_reports" in {call.args[0] for call in mock_op.create_table.call_args_list}
        index_names = {call.args[0] for call in mock_op.create_index.call_args_list}
        assert "ix_scheduled_reports_created_by" in index_names


class TestReconcileMigrationMetadata:
    def test_down_revision_merges_parallel_heads(self) -> None:
        migration = _load_migration("0064_reconcile_staging_schema.py", "migration_0064_meta")
        assert migration.revision == "0064_reconcile_staging_schema"
        assert migration.down_revision == (
            "0037_break_glass_enforcement",
            "0037_add_scheduled_reports_created_by",
        )
