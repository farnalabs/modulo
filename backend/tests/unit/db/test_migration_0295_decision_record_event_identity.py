"""Drive migration ``0295_decision_record_event_identity`` (FAR-1108 chunk 8b).

The migration replaces chunk 4's TEMPORARY bridge index
``ix_tmp_policy_gate_decisions_run_gate_result`` with the permanent partial
unique event-identity index ``uq_policy_gate_decisions_eval_result_id``.

These tests exercise upgrade()/downgrade() against a fake Alembic ``op`` +
inspector, so idempotency and the partial-predicate shape are proved without a
database.  (Real-Postgres behaviour is covered by the integration suite.)
"""

from __future__ import annotations

import importlib.util
import types
from pathlib import Path
from typing import Any

import pytest

_MIGRATION_NAME = "0295_decision_record_event_identity"
_MIGRATION_PATH = (
    Path(__file__).resolve().parents[3] / "src" / "modulo" / "db" / "migrations" / "versions" / f"{_MIGRATION_NAME}.py"
)

_TMP_INDEX = "ix_tmp_policy_gate_decisions_run_gate_result"
_PERMANENT_INDEX = "uq_policy_gate_decisions_eval_result_id"


class _FakeInspector:
    def __init__(self, index_names: list[str] | None = None) -> None:
        self.index_names = index_names if index_names is not None else []

    def get_indexes(self, table_name: str) -> list[dict[str, str]]:
        return [{"name": n} for n in self.index_names]


class _FakeBind:
    def __init__(self, inspector: _FakeInspector) -> None:
        self._inspector = inspector


class _FakeOp:
    """Records index operations; accepts the partial-predicate kwargs."""

    def __init__(self, inspector: _FakeInspector | None = None) -> None:
        self.dialect = types.SimpleNamespace(name="postgresql")
        self._inspector = inspector or _FakeInspector()
        self.created_indexes: list[dict[str, Any]] = []
        self.dropped_indexes: list[tuple[str, str]] = []

    def get_bind(self) -> _FakeBind:
        return _FakeBind(self._inspector)

    def create_index(
        self,
        name: str,
        table_name: str,
        columns: list[str],
        unique: bool = False,
        **kwargs: Any,
    ) -> None:
        self.created_indexes.append(
            {
                "name": name,
                "table": table_name,
                "columns": list(columns),
                "unique": unique,
                "kwargs": kwargs,
            }
        )

    def drop_index(self, name: str, table_name: str) -> None:
        self.dropped_indexes.append((name, table_name))


def _load_migration() -> types.ModuleType:
    assert _MIGRATION_PATH.exists(), f"Migration file missing: {_MIGRATION_PATH}"
    spec = importlib.util.spec_from_file_location(f"migration_{_MIGRATION_NAME}", _MIGRATION_PATH)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _run(monkeypatch: pytest.MonkeyPatch, fake_op: _FakeOp, method: str) -> None:
    module = _load_migration()
    monkeypatch.setattr(module, "op", fake_op)
    monkeypatch.setattr(module.sa, "inspect", lambda bind: fake_op._inspector)
    getattr(module, method)()


class TestUpgrade:
    def test_drops_temporary_bridge_index(self, monkeypatch: pytest.MonkeyPatch) -> None:
        fake_op = _FakeOp(inspector=_FakeInspector(index_names=[_TMP_INDEX]))
        _run(monkeypatch, fake_op, "upgrade")
        assert (_TMP_INDEX, "policy_gate_decisions") in fake_op.dropped_indexes

    def test_creates_permanent_partial_unique_index(self, monkeypatch: pytest.MonkeyPatch) -> None:
        fake_op = _FakeOp()
        _run(monkeypatch, fake_op, "upgrade")
        created = [c for c in fake_op.created_indexes if c["name"] == _PERMANENT_INDEX]
        assert len(created) == 1
        rec = created[0]
        assert rec["table"] == "policy_gate_decisions"
        assert rec["unique"] is True
        assert rec["columns"] == ["eval_result_id"]
        # Partial predicate present on BOTH dialects (NULL-result rows unbounded).
        assert "postgresql_where" in rec["kwargs"]
        assert "sqlite_where" in rec["kwargs"]
        assert "IS NOT NULL" in str(rec["kwargs"]["postgresql_where"])

    def test_upgrade_creates_when_nothing_present(self, monkeypatch: pytest.MonkeyPatch) -> None:
        fake_op = _FakeOp()
        _run(monkeypatch, fake_op, "upgrade")
        assert fake_op.dropped_indexes == []
        assert len(fake_op.created_indexes) == 1

    def test_upgrade_idempotent_when_permanent_already_present(self, monkeypatch: pytest.MonkeyPatch) -> None:
        fake_op = _FakeOp(inspector=_FakeInspector(index_names=[_PERMANENT_INDEX]))
        _run(monkeypatch, fake_op, "upgrade")
        assert not fake_op.created_indexes


class TestDowngrade:
    def test_drops_permanent_and_restores_temporary(self, monkeypatch: pytest.MonkeyPatch) -> None:
        fake_op = _FakeOp(inspector=_FakeInspector(index_names=[_PERMANENT_INDEX]))
        _run(monkeypatch, fake_op, "downgrade")
        assert (_PERMANENT_INDEX, "policy_gate_decisions") in fake_op.dropped_indexes
        restored = [c for c in fake_op.created_indexes if c["name"] == _TMP_INDEX]
        assert len(restored) == 1
        assert restored[0]["columns"] == ["run_id", "policy_gate_id", "eval_result_id"]
        assert restored[0]["unique"] is True

    def test_downgrade_idempotent_when_temporary_present(self, monkeypatch: pytest.MonkeyPatch) -> None:
        fake_op = _FakeOp(inspector=_FakeInspector(index_names=[_TMP_INDEX, _PERMANENT_INDEX]))
        _run(monkeypatch, fake_op, "downgrade")
        assert not fake_op.created_indexes
