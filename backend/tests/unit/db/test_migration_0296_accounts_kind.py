"""Unit tests for migration 0296_accounts_kind downgrade guard (FAR-1385).

The downgrade restores NOT NULL on ``email``/``auth_provider`` and drops the
service-privilege CHECK, so it is only safe while every row is a human with an
email. It must refuse otherwise rather than resurrect a NOT NULL violation or
delete service principals. No database needed.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path
from types import ModuleType
from unittest.mock import MagicMock, patch

import pytest

_PATH = (
    Path(__file__).resolve().parents[3] / "src" / "modulo" / "db" / "migrations" / "versions" / "0296_accounts_kind.py"
)


def _load() -> ModuleType:
    spec = importlib.util.spec_from_file_location("migration_0296", _PATH)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _downgrade(service_rows: int, null_emails: int) -> MagicMock:
    module = _load()
    with patch.object(module, "op") as op:
        # Two scalar reads in order: service-row count, then NULL-email count.
        op.get_bind.return_value.execute.return_value.scalar.side_effect = [service_rows, null_emails]
        try:
            module.downgrade()
        finally:
            captured = op
    return captured


class TestDowngradeGuard:
    def test_refuses_while_service_rows_exist(self) -> None:
        module = _load()
        with patch.object(module, "op") as op:
            op.get_bind.return_value.execute.return_value.scalar.return_value = 3
            with pytest.raises(RuntimeError, match="service principals"):
                module.downgrade()
        op.drop_column.assert_not_called()

    def test_refuses_while_null_emails_exist(self) -> None:
        module = _load()
        with patch.object(module, "op") as op:
            op.get_bind.return_value.execute.return_value.scalar.side_effect = [0, 2]
            with pytest.raises(RuntimeError, match="NULL email"):
                module.downgrade()
        op.drop_column.assert_not_called()

    def test_drops_kind_when_all_rows_are_human_with_email(self) -> None:
        op = _downgrade(0, 0)
        op.drop_column.assert_called_once_with("accounts", "kind")
        op.drop_constraint.assert_any_call("ck_accounts_kind", "accounts", type_="check")
        op.drop_constraint.assert_any_call("ck_accounts_service_privilege", "accounts", type_="check")


def test_revision_chains_off_the_current_head() -> None:
    module = _load()
    assert module.revision == "0296_accounts_kind"
    assert module.down_revision == "0295_decision_record_event_identity"
