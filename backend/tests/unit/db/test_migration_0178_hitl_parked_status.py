"""Drive migration ``0178_hitl_parked_status`` upgrade/downgrade code paths.

The migration body is pure schema DDL that only executes inside ``alembic
upgrade head``; the unit suite provisions its schema out-of-band, so the
migration module is otherwise never exercised and SonarCloud's new-code
coverage gate reads it at 0%. These tests execute ``upgrade()`` /
``downgrade()`` against a *mocked* Alembic ``op`` (and a mocked inspector) so
every branch is covered — the idempotent ``parked_at`` add, the
drop-if-differs / add-if-absent constraint widen, and the downgrade's
residual-row demotion — without ever touching a real database. This keeps the
new-code coverage gate green without masking genuine logic duplication.
"""

from __future__ import annotations

import importlib.util
import types
from pathlib import Path
from unittest.mock import MagicMock, patch

_MIGRATION_NAME = "0178_hitl_parked_status"
_MIGRATION_PATH = (
    Path(__file__).resolve().parents[3] / "src" / "modulo" / "db" / "migrations" / "versions" / f"{_MIGRATION_NAME}.py"
)


def _load_migration() -> types.ModuleType:
    assert _MIGRATION_PATH.exists(), f"Migration file missing: {_MIGRATION_PATH}"
    spec = importlib.util.spec_from_file_location(f"migration_{_MIGRATION_NAME}", _MIGRATION_PATH)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _make_op() -> MagicMock:
    op = MagicMock()
    bind = MagicMock()
    op.get_bind.return_value = bind
    return op


def test_upgrade_adds_parked_at_column_when_absent() -> None:
    op = _make_op()
    inspector = MagicMock()
    inspector.get_columns.return_value = [{"name": "id"}, {"name": "decision"}]
    with (
        patch("alembic.op", op),
        patch("sqlalchemy.inspect", return_value=inspector),
    ):
        module = _load_migration()
        module.upgrade()
    op.add_column.assert_called_once()
    op.execute.assert_any_call(module._DROP_NEW)
    op.execute.assert_any_call(module._ADD_NEW)


def test_upgrade_is_idempotent_when_column_present() -> None:
    op = _make_op()
    inspector = MagicMock()
    inspector.get_columns.return_value = [{"name": "id"}, {"name": "parked_at"}]
    with (
        patch("alembic.op", op),
        patch("sqlalchemy.inspect", return_value=inspector),
    ):
        module = _load_migration()
        module.upgrade()
    op.add_column.assert_not_called()
    op.execute.assert_any_call(module._DROP_NEW)
    op.execute.assert_any_call(module._ADD_NEW)


def test_downgrade_runs_full_restore() -> None:
    op = _make_op()
    with patch("alembic.op", op):
        module = _load_migration()
        module.downgrade()
    executed = [c.args[0] for c in op.execute.call_args_list]
    assert any("status = 'awaiting_human' WHERE status = 'hitl_parked'" in c for c in executed)
    assert any("UPDATE hitl_claims SET parked_at = NULL" in c for c in executed)
    op.drop_column.assert_called_once()
    op.execute.assert_any_call(module._DROP_OLD)
    op.execute.assert_any_call(module._ADD_OLD)
