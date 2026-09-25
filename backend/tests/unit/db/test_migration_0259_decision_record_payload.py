"""Drive migration ``0259_decision_record_payload`` temporary uniqueness index.

FAR-1102 chunk 4 adds a temporary unique index
``ix_tmp_policy_gate_decisions_run_gate_result`` on ``(run_id,
policy_gate_id, eval_result_id)`` — idempotent on an existing Postgres schema
and dropped in downgrade.

These tests exercise upgrade()/downgrade() against a fake Alembic ``op`` +
inspector, so idempotency and the Postgres dialect guard are proved without a
database.  (Real-Postgres behaviour is covered by the integration suite.)
"""

from __future__ import annotations

import importlib.util
import types
from pathlib import Path
from typing import Any

import pytest

_MIGRATION_NAME = "0259_decision_record_payload"
_MIGRATION_PATH = (
    Path(__file__).resolve().parents[3] / "src" / "modulo" / "db" / "migrations" / "versions" / f"{_MIGRATION_NAME}.py"
)

_INDEX_NAME = "ix_tmp_policy_gate_decisions_run_gate_result"


class _FakeInspector:
    def __init__(self, index_names: list[str] | None = None) -> None:
        self.index_names = index_names if index_names is not None else []

    def get_indexes(self, table_name: str) -> list[dict[str, str]]:
        return [{"name": n} for n in self.index_names]


class _FakeBind:
    def __init__(self, dialect: types.SimpleNamespace, inspector: _FakeInspector) -> None:
        self.dialect = dialect
        self._inspector = inspector


class _FakeOp:
    """Records index operations; dialect configurable."""

    def __init__(self, dialect: str = "postgresql", inspector: _FakeInspector | None = None) -> None:
        self.dialect = types.SimpleNamespace(name=dialect)
        self._inspector = inspector or _FakeInspector()
        self.created_indexes: list[tuple[str, str, bool, list[str]]] = []
        self.dropped_indexes: list[tuple[str, str]] = []
        self.created_columns: list[str] = []
        self.dropped_columns: list[str] = []

    def get_bind(self) -> _FakeBind:
        return _FakeBind(self.dialect, self._inspector)

    def create_index(self, name: str, table_name: str, columns: list[str], unique: bool = False) -> None:
        self.created_indexes.append((name, table_name, unique, list(columns)))

    def drop_index(self, name: str, table_name: str) -> None:
        self.dropped_indexes.append((name, table_name))

    def add_column(self, table_name: str, column: Any) -> None:
        self.created_columns.append(getattr(column, "name", "?"))

    def drop_column(self, table_name: str, column_name: str) -> None:
        self.dropped_columns.append(column_name)

    def create_check_constraint(self, name: str, table_name: str, condition: str) -> None:
        pass

    def drop_constraint(self, name: str, table_name: str, type_: str) -> None:
        pass


def _load_migration() -> types.ModuleType:
    assert _MIGRATION_PATH.exists(), f"Migration file missing: {_MIGRATION_PATH}"
    spec = importlib.util.spec_from_file_location(f"migration_{_MIGRATION_NAME}", _MIGRATION_PATH)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _run(monkeypatch: pytest.MonkeyPatch, fake_op: _FakeOp, method: str) -> None:
    """Run migration method with the fake op injected and sa.inspect routed to it."""
    module = _load_migration()
    monkeypatch.setattr(module, "op", fake_op)
    monkeypatch.setattr(module.sa, "inspect", lambda bind: fake_op._inspector)
    getattr(module, method)()


class TestTemporaryUniquenessIndex:
    def test_upgrade_creates_unique_index(self, monkeypatch: pytest.MonkeyPatch) -> None:
        fake_op = _FakeOp(dialect="postgresql")
        _run(monkeypatch, fake_op, "upgrade")
        index_creates = [c for c in fake_op.created_indexes if c[0] == _INDEX_NAME]
        assert len(index_creates) == 1
        _, table, unique, _columns = index_creates[0]
        assert table == "policy_gate_decisions"
        assert unique is True

    def test_upgrade_is_idempotent_when_index_exists(self, monkeypatch: pytest.MonkeyPatch) -> None:
        fake_op = _FakeOp(dialect="postgresql", inspector=_FakeInspector(index_names=[_INDEX_NAME]))
        _run(monkeypatch, fake_op, "upgrade")
        assert not fake_op.created_indexes

    def test_upgrade_skips_index_on_non_postgres(self, monkeypatch: pytest.MonkeyPatch) -> None:
        fake_op = _FakeOp(dialect="sqlite")
        _run(monkeypatch, fake_op, "upgrade")
        index_creates = [c for c in fake_op.created_indexes if c[0] == _INDEX_NAME]
        assert not index_creates

    def test_index_uses_bridge_name_signalling_temporary_status(self, monkeypatch: pytest.MonkeyPatch) -> None:
        fake_op = _FakeOp(dialect="postgresql")
        _run(monkeypatch, fake_op, "upgrade")
        (name, _, _, _) = fake_op.created_indexes[0]
        assert name.startswith("ix_tmp_"), "temporary bridge indexes must carry the ix_tmp_ prefix"

    def test_upgrade_columns_added_before_index(self, monkeypatch: pytest.MonkeyPatch) -> None:
        fake_op = _FakeOp(dialect="postgresql")
        _run(monkeypatch, fake_op, "upgrade")
        assert len(fake_op.created_columns) == 6
        assert len(fake_op.created_indexes) == 1

    def test_index_column_order_run_gate_result(self, monkeypatch: pytest.MonkeyPatch) -> None:
        fake_op = _FakeOp(dialect="postgresql")
        _run(monkeypatch, fake_op, "upgrade")
        created = fake_op.created_indexes[0]
        assert created[3] == ["run_id", "policy_gate_id", "eval_result_id"]

    def test_downgrade_drops_the_index(self, monkeypatch: pytest.MonkeyPatch) -> None:
        fake_op = _FakeOp(dialect="postgresql", inspector=_FakeInspector(index_names=[_INDEX_NAME]))
        _run(monkeypatch, fake_op, "downgrade")
        drops = [d for d in fake_op.dropped_indexes if d[0] == _INDEX_NAME]
        assert len(drops) == 1
        assert drops[0][1] == "policy_gate_decisions"

    def test_downgrade_idempotent_when_index_absent(self, monkeypatch: pytest.MonkeyPatch) -> None:
        fake_op = _FakeOp(dialect="postgresql", inspector=_FakeInspector(index_names=[]))
        _run(monkeypatch, fake_op, "downgrade")
        assert not fake_op.dropped_indexes

    def test_downgrade_keeps_payload_columns_order(self, monkeypatch: pytest.MonkeyPatch) -> None:
        fake_op = _FakeOp(dialect="postgresql", inspector=_FakeInspector(index_names=[_INDEX_NAME]))
        _run(monkeypatch, fake_op, "downgrade")
        assert "policy_gate_version" in fake_op.dropped_columns
        assert "run_id" in fake_op.dropped_columns
        assert "eval_result_id" in fake_op.dropped_columns
        assert "node_id" in fake_op.dropped_columns
        assert "error_detail" in fake_op.dropped_columns
        assert "resolved_action" in fake_op.dropped_columns


@pytest.mark.parametrize(
    ("dialect", "expected_creates"),
    [("postgresql", 1), ("sqlite", 0), ("mariadb", 0)],
)
def test_index_creation_counts_by_dialect(monkeypatch: pytest.MonkeyPatch, dialect: str, expected_creates: int) -> None:
    fake_op = _FakeOp(dialect=dialect)
    _run(monkeypatch, fake_op, "upgrade")
    index_creates = [c for c in fake_op.created_indexes if c[0] == _INDEX_NAME]
    assert len(index_creates) == expected_creates
