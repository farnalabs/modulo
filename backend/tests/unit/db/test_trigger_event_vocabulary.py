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
* the reconciliation migration's hardcoded vocabulary stays in sync with the
  model (the single source of truth) — a value added to one side and not the
  other breaks the constraint/model contract,
* the chain has a single linear head ``0110_schema_pipeline_runtime`` (the
  FAR-213 ``0111_run_blocked_partial_summary``, FAR-210
  ``0112_feedback_correction_state``, FAR-223
  ``0113_guardrail_summary``, FAR-296 ``0114_org_api_keys_run_id``,
  FAR-247 ``0115_notification_preferences``, FAR-309
  ``0116_guardrail_trust_pr_b``, TOCTOU ``0117_toctou_hardening``,
  batch-scoped variants ``0118_batch_scoped_variants``,
  ``0119_analytics_batch_id``, and org-FK hardening ``0120_org_fk_hardening``
  migrations chain on top of it).

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

_MIGRATION_NAME = "0110_schema_pipeline_runtime"
_MIGRATION_PATH = (
    Path(__file__).resolve().parents[3] / "src" / "modulo" / "db" / "migrations" / "versions" / f"{_MIGRATION_NAME}.py"
)

# The chain head after the FAR-210 feedback correction_state migration (0112),
# now topped by the FAR-309 PR B trust-model migration (0116), the TOCTOU
# hardening migration (0117), and the batch-scoped variants migration (0118),
# the metrics_staging migration (0121), and the FAR-363 library_sync_state
# (0122) + relax_registry_signature_check (0123) migrations.
_CHAIN_HEAD_MIGRATION_NAME = "0171_runs_list_performance_indexes"
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

    def test_model_vocabulary_is_21_values(self) -> None:
        assert len(VALIDATION_RESULT_VALUES) == 21
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
        """The reconciliation migration must create the constraint with the
        FULL model vocabulary — a value in the model but missing from the
        migration breaks the constraint on a fresh DB, and a value in the
        migration but not the model widens the constraint beyond the ORM."""
        _load_migration()
        source = Path(_MIGRATION_PATH).read_text(encoding="utf-8")
        assert _CHECK_CONSTRAINT_NAME in source
        for value in VALIDATION_RESULT_VALUES:
            assert f"'{value}'" in source, f"0008 constraint DDL missing {value!r}"

    def test_0008_constraint_guards_idempotency(self) -> None:
        """The constraint is added only when absent (pg_constraint guard), so
        re-running the reconciliation migration is a no-op."""
        source = Path(_MIGRATION_PATH).read_text(encoding="utf-8")
        assert f"conname='{_CHECK_CONSTRAINT_NAME}'" in source
        assert f"ADD CONSTRAINT {_CHECK_CONSTRAINT_NAME} CHECK" in source
