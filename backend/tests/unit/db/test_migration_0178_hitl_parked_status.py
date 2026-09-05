"""Drive migration ``0178_hitl_parked_status`` upgrade/downgrade code paths.

The migration body is pure schema DDL that only executes inside ``alembic
upgrade head``; the unit suite provisions its schema out-of-band, so the
migration module is otherwise never exercised and SonarCloud's new-code
coverage gate reads it at 0%. These tests execute ``upgrade()`` /
``downgrade()`` against a *mocked* Alembic ``op`` so every branch is covered —
the staged NOT VALID add + guarded online validate (qa F8), the
drop-if-differs guard, and the downgrade's residual-row demotion — without
ever touching a real database. This keeps the new-code coverage gate green
without masking genuine logic duplication.

Updated for qa F8/F13: the migration no longer touches ``hitl_claims
.parked_at`` (the ``hitl_parked`` STATUS carries the signal) and the
constraint widen is staged ``NOT VALID`` + ``VALIDATE CONSTRAINT``.
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
    return MagicMock()


def test_upgrade_stages_not_valid_add_then_validates() -> None:
    """The widen issues the guarded drop, the staged NOT VALID add, and the
    guarded VALIDATE — the online three-step (qa F8)."""
    op = _make_op()
    with patch("alembic.op", op):
        module = _load_migration()
        module.upgrade()
    executed = [c.args[0] for c in op.execute.call_args_list]
    assert executed == [module._DROP_NEW, module._ADD_NEW, module._VALIDATE_NEW]
    assert "NOT VALID;" in module._ADD_NEW
    assert "VALIDATE CONSTRAINT ck_runs_status" in module._VALIDATE_NEW


def test_upgrade_never_touches_hitl_claims_schema() -> None:
    """qa F13: no ``parked_at`` column add/drop anywhere — the upgrade is
    constraint-only."""
    op = _make_op()
    with patch("alembic.op", op):
        module = _load_migration()
        module.upgrade()
    op.add_column.assert_not_called()
    op.drop_column.assert_not_called()
    assert all("hitl_claims" not in c for c in (module._DROP_NEW, module._ADD_NEW, module._VALIDATE_NEW))


def test_downgrade_runs_full_restore() -> None:
    op = _make_op()
    with patch("alembic.op", op):
        module = _load_migration()
        module.downgrade()
    executed = [c.args[0] for c in op.execute.call_args_list]
    assert any("status = 'awaiting_human' WHERE status = 'hitl_parked'" in c for c in executed)
    op.execute.assert_any_call(module._DROP_OLD)
    op.execute.assert_any_call(module._ADD_OLD)
    # qa F13: no parked_at cleanup / drop in the downgrade either.
    assert not any("parked_at" in c for c in executed)
    op.drop_column.assert_not_called()
