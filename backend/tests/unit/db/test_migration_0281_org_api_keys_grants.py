"""Unit tests for migration 0281_org_api_keys_grants downgrade guard (FAR-1477).

A downgrade (or a code rollback) while grant-bearing keys exist would silently
widen them to full-role keys, so the downgrade must refuse. No database needed.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path
from types import ModuleType
from unittest.mock import MagicMock, patch

import pytest

_PATH = (
    Path(__file__).resolve().parents[3]
    / "src"
    / "modulo"
    / "db"
    / "migrations"
    / "versions"
    / "0281_org_api_keys_grants.py"
)


def _load() -> ModuleType:
    spec = importlib.util.spec_from_file_location("migration_0281", _PATH)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _downgrade(grant_rows: int) -> MagicMock:
    module = _load()
    with patch.object(module, "op") as op:
        op.get_bind.return_value.execute.return_value.scalar.return_value = grant_rows
        try:
            module.downgrade()
        finally:
            captured = op
    return captured


class TestDowngradeGuard:
    def test_refuses_while_grant_bearing_keys_exist(self) -> None:
        module = _load()
        with patch.object(module, "op") as op:
            op.get_bind.return_value.execute.return_value.scalar.return_value = 2
            with pytest.raises(RuntimeError, match="revoke all grant-bearing keys"):
                module.downgrade()
        op.drop_column.assert_not_called()

    def test_drops_column_when_no_grant_bearing_keys(self) -> None:
        op = _downgrade(0)
        op.drop_column.assert_called_once_with("org_api_keys", "grants")

    def test_docstring_states_rollback_caveat(self) -> None:
        doc = _load().__doc__ or ""
        assert "FULL-role" in doc
        assert "cannot silently widen" not in doc
