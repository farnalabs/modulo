"""Vocabulary/constraint tests for the ``auto_deactivated`` widening.

The migration chain was squashed into three idempotent reconciliation
migrations (``0108_schema_org_identity`` / ``0109_schema_teams_library`` /
``0110_schema_pipeline_runtime``). The per-feature migrations that used to carry
this surface (``0104_trigger_event_auto_deactivated`` head ``0105``, plus main's
``0106`` adding ``guardrail_blocked``) no longer exist;
``0110_schema_pipeline_runtime`` now owns the ``ck_trigger_events_validation_result``
constraint with the FULL 21-value vocabulary. This file asserts:

* the model vocabulary (``VALIDATION_RESULT_VALUES``) contains
  ``auto_deactivated`` and the ORM CHECK constraint reflects it,
* the widening migration's hardcoded vocabulary stays in sync with the
  model (the single source of truth) — a value added to one side and not the
  other breaks the constraint/model contract,
* the chain has a single linear head — ``0110_schema_pipeline_runtime``
  owned the original 21-value constraint; FAR-604's
  ``0176_trigger_event_validation_results`` widened it with ``coalesced`` /
  ``backpressure_skipped`` and is now the constraint's owner and the chain
  head.

The old SQLite round-trip (which ran the migration's upgrade/downgrade against
a mock ``op``) is obsolete: the reconciliation migration expresses the
constraint as guarded raw DDL (``ADD CONSTRAINT ... IF NOT EXISTS`` with the
full vocabulary) rather than a reversible drop/add pair, and its downgrade is a
no-op. The drift-guard tests below are the meaningful contract.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path
from types import ModuleType

from alembic.script import ScriptDirectory

from modulo.db.models.trigger_event import VALIDATION_RESULT_VALUES

_MIGRATION_NAME = "0176_trigger_event_validation_results"
_MIGRATION_PATH = (
    Path(__file__).resolve().parents[3] / "src" / "modulo" / "db" / "migrations" / "versions" / f"{_MIGRATION_NAME}.py"
)

# The chain head after the FAR-604 admission-healing migration widened the
# ck_trigger_events_validation_result vocabulary (coalesced /
# backpressure_skipped) on top of 0174_per_org_last_admin_guard, the
# FAR-461 in-app invite tokens migration (0177_invitations) chained off it,
# and the FAR-604 D2 HITL-capacity migration (0178_hitl_parked_status,
# renumbered from 0177 after the invitations collision) re-parented onto
# that.
_CHAIN_HEAD_MIGRATION_NAME = "0178_hitl_parked_status"
_CHECK_CONSTRAINT_NAME = "ck_trigger_events_validation_result"


def _load_migration() -> ModuleType:
    assert _MIGRATION_PATH.exists(), f"Migration file missing: {_MIGRATION_PATH}"
    spec = importlib.util.spec_from_file_location(f"migration_{_MIGRATION_NAME}", _MIGRATION_PATH)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _script() -> ScriptDirectory:
    return ScriptDirectory(str(_MIGRATION_PATH.parent.parent))


class TestModelVocabulary:
    def test_auto_deactivated_in_model_vocabulary(self) -> None:
        assert "auto_deactivated" in VALIDATION_RESULT_VALUES

    def test_guardrail_blocked_in_model_vocabulary(self) -> None:
        # Folded in from main's 0106 (guardrail_blocked), now part of 0008.
        assert "guardrail_blocked" in VALIDATION_RESULT_VALUES

    def test_far604_values_in_model_vocabulary(self) -> None:
        # FAR-604 admission healing: latest-wins coalescing + backpressure skip.
        assert "coalesced" in VALIDATION_RESULT_VALUES
        assert "backpressure_skipped" in VALIDATION_RESULT_VALUES

    def test_model_vocabulary_is_23_values(self) -> None:
        assert len(VALIDATION_RESULT_VALUES) == 23
        assert len(set(VALIDATION_RESULT_VALUES)) == len(VALIDATION_RESULT_VALUES)

    def test_orm_check_constraint_includes_auto_deactivated(self) -> None:
        from sqlalchemy import CheckConstraint

        from modulo.db.models.trigger_event import TriggerEvent

        checks = [c for c in TriggerEvent.__table_args__ if isinstance(c, CheckConstraint)]
        check = next(c for c in checks if c.name == _CHECK_CONSTRAINT_NAME)
        assert "auto_deactivated" in check.sqltext.text


class TestReconciliationMigration:
    def test_0008_is_single_chain_head(self) -> None:
        script = _script()
        heads = script.get_heads()
        assert heads == [_CHAIN_HEAD_MIGRATION_NAME], f"expected a single head, got {heads}"

    def test_0008_owns_trigger_events_validation_constraint(self) -> None:
        """The widening migration must create the constraint with the
        FULL model vocabulary — a value in the model but missing from the
        migration breaks the constraint on a fresh DB, and a value in the
        migration but not the model widens the constraint beyond the ORM."""
        _load_migration()
        source = Path(_MIGRATION_PATH).read_text(encoding="utf-8")
        assert _CHECK_CONSTRAINT_NAME in source
        for value in VALIDATION_RESULT_VALUES:
            assert f"'{value}'" in source, f"widening constraint DDL missing {value!r}"

    def test_0008_constraint_guards_idempotency(self) -> None:
        """The constraint is added only when absent (pg_constraint guard), so
        re-running the widening migration is a no-op."""
        source = Path(_MIGRATION_PATH).read_text(encoding="utf-8")
        assert f"conname='{_CHECK_CONSTRAINT_NAME}'" in source
        assert f"ADD CONSTRAINT {_CHECK_CONSTRAINT_NAME} CHECK" in source

    def test_0008_add_is_not_valid_then_validated(self) -> None:
        """FAR-604 F5 — the widened CHECK is added NOT VALID (instant, no
        full-table validation scan under ACCESS EXCLUSIVE on the hottest
        insert table) and then validated in a separate guarded step (VALIDATE
        CONSTRAINT takes SHARE UPDATE EXCLUSIVE — non-blocking for INSERTs).
        The validation step is guarded on ``NOT convalidated`` so re-running
        the reconciliation chain skips an already-validated constraint."""
        source = Path(_MIGRATION_PATH).read_text(encoding="utf-8")
        assert f"ADD CONSTRAINT {_CHECK_CONSTRAINT_NAME} CHECK" in source
        assert "NOT VALID" in source
        assert f"VALIDATE CONSTRAINT {_CHECK_CONSTRAINT_NAME}" in source
        assert "NOT convalidated" in source

    def test_0008_drop_guard_expected_def_is_whitespace_stripped(self) -> None:
        """FAR-604 F5 — the drop guard compares the live definition against a
        WHITESPACE-STRIPPED expected literal (it is compared against
        ``regexp_replace(pg_get_constraintdef(oid), '\\s+', '', 'g')``). A
        literal carrying the ``character varying`` space form never matches,
        so the drop — and its ACCESS EXCLUSIVE lock — would fire on EVERY
        re-run of the reconciliation chain (the 0110 guard pattern)."""
        module = _load_migration()
        drop_stmt: str = module._DROP_IF_DIFFERENT
        assert "regexp_replace(pg_get_constraintdef(oid), '\\s+', '', 'g')" in drop_stmt
        assert "::charactervarying" in drop_stmt
        assert "character varying" not in drop_stmt
