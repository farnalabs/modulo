"""Drive migration ``0254_eval_backfill_cutover`` Step 0 drain scope.

Regression for the 2026-09-22 production deploy wedge: the drain gate waited on
EVERY non-terminal run status, including the parked/recovery statuses
(``awaiting_human``, ``hitl_parked``, ``claimed``, ``unknown``, ``pending``)
that persist indefinitely by design.  A live database carrying any such run
could therefore never satisfy the gate, and the pre-deploy migration rehearsal
aborted after the 600s timeout on every attempt.

These tests pin the corrected scope — only an executing (``running``) run gates
the cutover — and exercise ``_drain_check`` against a fake Alembic ``op`` so the
behaviour is proved without a database.
"""

from __future__ import annotations

import importlib.util
import types
from pathlib import Path
from unittest.mock import patch

import pytest

_MIGRATION_NAME = "0254_eval_backfill_cutover"
_MIGRATION_PATH = (
    Path(__file__).resolve().parents[3] / "src" / "modulo" / "db" / "migrations" / "versions" / f"{_MIGRATION_NAME}.py"
)

_NON_EXECUTING_STATUSES = ("awaiting_human", "hitl_parked", "claimed", "unknown", "pending")


class _FakeResult:
    def __init__(self, rows: list[tuple[object, ...]]) -> None:
        self._rows = rows

    def fetchall(self) -> list[tuple[object, ...]]:
        return list(self._rows)


class _FakeBind:
    """Answers the two drain-gate queries by shape, recording each statement."""

    def __init__(
        self,
        non_blocking_rows: list[tuple[object, ...]],
        blocking_rows: list[tuple[object, ...]],
    ) -> None:
        self.non_blocking_rows = non_blocking_rows
        self.blocking_rows = blocking_rows
        self.queries: list[str] = []

    def execute(self, statement: object, *args: object, **kwargs: object) -> _FakeResult:
        sql = str(statement)
        self.queries.append(sql)
        if "GROUP BY" in sql:
            return _FakeResult(self.non_blocking_rows)
        return _FakeResult(self.blocking_rows)


class _FakeOp:
    def __init__(self, bind: _FakeBind) -> None:
        self._bind = bind

    def get_bind(self) -> _FakeBind:
        return self._bind


def _load_migration(op: object | None = None) -> types.ModuleType:
    assert _MIGRATION_PATH.exists(), f"Migration file missing: {_MIGRATION_PATH}"
    spec = importlib.util.spec_from_file_location(f"migration_{_MIGRATION_NAME}", _MIGRATION_PATH)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    if op is None:
        spec.loader.exec_module(module)
    else:
        with patch("alembic.op", op):
            spec.loader.exec_module(module)
    return module


def test_drain_scope_blocks_on_running_only() -> None:
    module = _load_migration()
    assert module._DRAIN_BLOCKING_RUN_STATUSES == ("running",)
    for status in _NON_EXECUTING_STATUSES:
        assert status in module._DRAIN_NON_BLOCKING_RUN_STATUSES
        assert status not in module._DRAIN_BLOCKING_RUN_STATUSES


def test_drain_check_does_not_wait_on_non_executing_runs() -> None:
    """Parked/recovery runs are logged, never waited on.  A 0s timeout makes a
    blocked loop fail immediately, so a clean return proves the gate ignored them."""
    bind = _FakeBind(non_blocking_rows=[("awaiting_human", 13), ("hitl_parked", 2)], blocking_rows=[])
    module = _load_migration(_FakeOp(bind))
    module._DRAIN_TIMEOUT_S = 0
    module._DRAIN_POLL_INTERVAL_S = 0

    module._drain_check()

    blocking_query = next(q for q in bind.queries if "GROUP BY" not in q)
    assert "'running'" in blocking_query
    for status in _NON_EXECUTING_STATUSES:
        assert status not in blocking_query, f"non-executing status {status} must not gate the drain"


def test_drain_check_aborts_on_a_stuck_running_run() -> None:
    bind = _FakeBind(non_blocking_rows=[], blocking_rows=[("9e1a1cad-ef0f-483a-8780-1d877f49ddbf",)])
    module = _load_migration(_FakeOp(bind))
    module._DRAIN_TIMEOUT_S = 0
    module._DRAIN_POLL_INTERVAL_S = 0

    with pytest.raises(RuntimeError, match="executing run"):
        module._drain_check()
