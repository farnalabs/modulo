"""Unit tests for migration ``0254_eval_backfill_cutover``'s Step-0 drain gate.

Pins the drain-gate status set: only the in-flight work that will actually run
to completion (``pending`` / ``running``) may block the FK repoint. The
never-draining states (``awaiting_human``, ``claimed``, ``hitl_parked``,
``unknown``) must NOT be patrolled — patrolling them made the gate
unsatisfiable in a live deployment (2026-09-22 prod-deploy outage: 13
never-draining runs aborted the rehearsal and blocked every deploy).
"""

from __future__ import annotations

import importlib.util
import types
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

_MIGRATION_NAME = "0254_eval_backfill_cutover"
_MIGRATION_PATH = (
    Path(__file__).resolve().parents[3] / "src" / "modulo" / "db" / "migrations" / "versions" / f"{_MIGRATION_NAME}.py"
)

_NEVER_DRAINING = {"awaiting_human", "claimed", "hitl_parked", "unknown"}


def _load_migration() -> types.ModuleType:
    assert _MIGRATION_PATH.exists(), f"Migration file missing: {_MIGRATION_PATH}"
    spec = importlib.util.spec_from_file_location(f"migration_{_MIGRATION_NAME}", _MIGRATION_PATH)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _bind_with_rows(rows: list[tuple[str]]) -> MagicMock:
    bind = MagicMock()
    bind.execute.return_value.fetchall.return_value = rows
    return bind


def test_drain_set_excludes_never_draining_states() -> None:
    module = _load_migration()
    drain = set(module._DRAIN_RUN_STATUSES)
    assert drain == {"pending", "running"}
    assert drain.isdisjoint(_NEVER_DRAINING)


def test_drain_sql_patrols_only_the_drain_set() -> None:
    op = MagicMock()
    op.get_bind.return_value = _bind_with_rows([])
    with patch("alembic.op", op):
        module = _load_migration()
        module._drain_check()
    sql = str(op.get_bind.return_value.execute.call_args.args[0])
    for status in module._DRAIN_RUN_STATUSES:
        assert f"'{status}'" in sql
    for status in _NEVER_DRAINING:
        assert f"'{status}'" not in sql


def test_drain_check_returns_when_no_active_runs() -> None:
    op = MagicMock()
    op.get_bind.return_value = _bind_with_rows([])
    with patch("alembic.op", op):
        module = _load_migration()
        module._drain_check()
    op.get_bind.return_value.execute.assert_called_once()


def test_drain_check_aborts_on_timeout_with_active_run() -> None:
    op = MagicMock()
    op.get_bind.return_value = _bind_with_rows([("run-1",)])
    fake_time = MagicMock()
    fake_time.monotonic.side_effect = [0.0, 0.0, 700.0]
    with patch("alembic.op", op):
        module = _load_migration()
    with patch.object(module, "time", fake_time), pytest.raises(RuntimeError, match=r"run\(s\) still in active state"):
        module._drain_check()
    fake_time.sleep.assert_called_once_with(module._DRAIN_POLL_INTERVAL_S)
