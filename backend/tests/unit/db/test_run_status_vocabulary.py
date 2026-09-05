"""Vocabulary/constraint parity tests for the ``hitl_parked`` status widening.

FAR-604 D2 (HITL capacity) adds the non-terminal ``hitl_parked`` run status to
the ``ck_runs_status`` CHECK constraint (migration ``0178_hitl_parked_status``,
renumbered from 0177 and re-parented onto main's ``0177_invitations`` after the
collision) and stamps ``hitl_claims.parked_at``. This file asserts:

* the model status sets (``db.models.run``) contain ``hitl_parked`` and the
  ORM CHECK constraint reflects it — the model is the single source of truth,
* the widening migration's hardcoded vocabulary stays in sync with the model
  (a status in the model but missing from the migration breaks writes on a
  fresh DB; a status in the migration but not the model widens beyond the ORM),
* the migration is idempotent (guarded drop-if-differs / add-if-absent) and
  its downgrade is safe (no residual ``hitl_parked`` rows can violate the
  restored constraint).

Mirrors the sibling ``test_trigger_event_vocabulary.py`` pattern.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path
from types import ModuleType

from modulo.db.models.run import ACTIVE_RUN_STATUSES, PIPELINE_CAPACITY_STATUSES, TERMINAL_STATUSES

_MIGRATION_NAME = "0178_hitl_parked_status"
_MIGRATION_PATH = (
    Path(__file__).resolve().parents[3] / "src" / "modulo" / "db" / "migrations" / "versions" / f"{_MIGRATION_NAME}.py"
)
_CHECK_CONSTRAINT_NAME = "ck_runs_status"
_EXPECTED_STATUSES = frozenset(
    {
        "pending",
        "running",
        "awaiting_human",
        "claimed",
        "unknown",
        "hitl_parked",
        "complete",
        "failed",
        "cancelled",
        "eval_failed",
        "stalled",
        "budget_exceeded",
        "cost_ceiling_exceeded",
        "router_no_match",
        "compensation_failed",
    }
)


def _load_migration() -> ModuleType:
    assert _MIGRATION_PATH.exists(), f"Migration file missing: {_MIGRATION_PATH}"
    spec = importlib.util.spec_from_file_location(f"migration_{_MIGRATION_NAME}", _MIGRATION_PATH)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _model_check_sqltext() -> str:
    from sqlalchemy import CheckConstraint

    from modulo.db.models.run import Run

    checks = [c for c in Run.__table_args__ if isinstance(c, CheckConstraint)]
    check = next(c for c in checks if c.name == _CHECK_CONSTRAINT_NAME)
    return check.sqltext.text


class TestModelStatusSets:
    def test_hitl_parked_is_non_terminal_active(self) -> None:
        assert "hitl_parked" in ACTIVE_RUN_STATUSES
        assert "hitl_parked" not in TERMINAL_STATUSES

    def test_pipeline_capacity_excludes_human_waiting_statuses(self) -> None:
        """FAR-604 D1: the pipeline capacity gate never counts human-waiting
        runs (awaiting_human / hitl_parked) — that is the whole design."""
        assert "awaiting_human" not in PIPELINE_CAPACITY_STATUSES
        assert "hitl_parked" not in PIPELINE_CAPACITY_STATUSES
        assert "pending" not in PIPELINE_CAPACITY_STATUSES
        assert PIPELINE_CAPACITY_STATUSES <= ACTIVE_RUN_STATUSES

    def test_model_check_constraint_includes_hitl_parked(self) -> None:
        sqltext = _model_check_sqltext()
        for status in _EXPECTED_STATUSES:
            assert f"'{status}'" in sqltext, f"model ck_runs_status missing {status!r}"

    def test_model_check_constraint_is_15_values(self) -> None:
        assert len(_EXPECTED_STATUSES) == 15


class TestWideningMigration:
    def test_migration_chains_off_main_head(self) -> None:
        module = _load_migration()
        assert module.revision == _MIGRATION_NAME
        assert module.down_revision == "0177_invitations"

    def test_migration_creates_constraint_with_full_vocabulary(self) -> None:
        """A status in the model but missing from the migration breaks writes
        on a fresh DB; a status in the migration but not the model widens the
        constraint beyond the ORM."""
        source = Path(_MIGRATION_PATH).read_text(encoding="utf-8")
        for status in _EXPECTED_STATUSES:
            assert f"'{status}'::character varying" in source, f"widening constraint DDL missing {status!r}"

    def test_migration_guards_idempotency(self) -> None:
        """The constraint is dropped only when its definition differs and
        re-added only when absent (pg_constraint guards), so re-running the
        reconciliation chain is a no-op."""
        source = Path(_MIGRATION_PATH).read_text(encoding="utf-8")
        assert f"conname='{_CHECK_CONSTRAINT_NAME}'" in source
        assert f"ADD CONSTRAINT {_CHECK_CONSTRAINT_NAME} CHECK" in source

    def test_drop_guard_expected_def_is_whitespace_stripped(self) -> None:
        """The drop guard compares the live definition against a
        whitespace-stripped expected literal (0110 guard pattern) — a literal
        carrying the ``character varying`` space form never matches, so the
        drop would fire on EVERY re-run."""
        module = _load_migration()
        drop_stmt: str = module._DROP_NEW
        assert "regexp_replace(pg_get_constraintdef(oid), '\\s+', '', 'g')" in drop_stmt
        assert "::charactervarying" in drop_stmt
        assert "character varying" not in drop_stmt

    def test_parked_at_column_is_idempotent(self) -> None:
        """The ``hitl_claims.parked_at`` add is guarded by an inspector check
        so an upgrade that already added the column never fails."""
        source = Path(_MIGRATION_PATH).read_text(encoding="utf-8")
        assert 'parked_at" not in existing_claims' in source

    def test_downgrade_moves_residual_parked_rows_first(self) -> None:
        """The downgrade restores the pre-0177 14-status constraint — residual
        ``hitl_parked`` rows would violate it, so they are demoted to
        ``awaiting_human`` BEFORE the constraint recreation."""
        source = Path(_MIGRATION_PATH).read_text(encoding="utf-8")
        downgrade_source = source.split("def downgrade()")[1]
        assert "status = 'awaiting_human' WHERE status = 'hitl_parked'" in downgrade_source
        assert downgrade_source.index("UPDATE runs SET status = 'awaiting_human'") < downgrade_source.index("_DROP_OLD")
