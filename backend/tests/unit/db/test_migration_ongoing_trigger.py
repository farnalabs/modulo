"""Final-state tests for the ``ongoing`` / ``slack_app_mention`` trigger types.

The migration chain was squashed into three idempotent reconciliation
migrations (``0108_schema_org_identity`` / ``0109_schema_teams_library`` /
``0110_schema_pipeline_runtime``). The per-feature migrations that used to
carry this surface (``0094_ongoing_trigger_type`` through
``0101_guardrails``) no longer exist, so the DDL source assertions here run
against ``0110_schema_pipeline_runtime`` — the reconciliation migration that
owns the trigger/run CHECK vocabulary, the streak-engine partial indexes, the
guardrail eval vocabulary, and the raw-output markers column:


* the wide ``ck_triggers_type`` / ``ck_runs_trigger_type`` vocabularies include
  ``ongoing`` and ``slack_app_mention``,
* the partial ``ck_triggers_ongoing_spend_limit`` /
  ``ck_triggers_ongoing_target_range`` checks exist,
* the ``ix_runs_trigger_id_status`` / ``ix_runs_trigger_id_created_at`` and
  streak-engine partial indexes exist,
* ``runs.raw_output_markers`` / ``run_classification`` / ``work_intact`` and
  ``eval_results.observed`` columns exist,
* ``ck_eval_definitions_type`` includes ``guardrail``,
* the ORM models' CHECK constraints carry the same vocabulary (drift guard).

"""

from __future__ import annotations

from pathlib import Path
from types import ModuleType

import pytest
from alembic.script import ScriptDirectory

_MIGRATION_0008 = "0110_schema_pipeline_runtime"
_MIGRATION_0113 = "0113_guardrail_summary"
_HEAD_MIGRATION = "0137_eval_suite_run"

_VERSIONS_DIR = Path(__file__).resolve().parents[3] / "src" / "modulo" / "db" / "migrations" / "versions"

_SPEND_PARTIAL = "trigger_type <> 'ongoing' OR (daily_spend_limit IS NOT NULL AND daily_spend_limit > 0)"
_TARGET_PARTIAL = "trigger_type <> 'ongoing' OR (max_concurrent_runs BETWEEN 1 AND 20)"


@pytest.fixture(scope="module")
def migration_0008() -> ModuleType:
    path = _VERSIONS_DIR / f"{_MIGRATION_0008}.py"
    assert path.exists(), f"Migration file missing: {path}"
    import importlib.util

    spec = importlib.util.spec_from_file_location(f"migration_{_MIGRATION_0008}", path)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def migration_0113() -> ModuleType:
    path = _VERSIONS_DIR / f"{_MIGRATION_0113}.py"
    assert path.exists(), f"Migration file missing: {path}"
    import importlib.util

    spec = importlib.util.spec_from_file_location(f"migration_{_MIGRATION_0113}", path)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _script() -> ScriptDirectory:
    return ScriptDirectory(str(_VERSIONS_DIR.parent))


class TestReconciliationChain:
    def test_single_head_is_0008(self) -> None:
        script = _script()
        assert script.get_heads() == [_HEAD_MIGRATION], (
            f"expected a single head {_HEAD_MIGRATION}, got {script.get_heads()}"
        )

    def test_0113_adds_guardrail_summary_column(self, migration_0113: ModuleType) -> None:
        source = _source(migration_0113)
        assert "guardrail_summary_json" in source
        assert "add_column" in source
        assert "0112_feedback_correction_state" in source

    def test_0008_owns_trigger_and_run_vocabulary(self, migration_0008: ModuleType) -> None:
        source = _source(migration_0008)
        assert "ck_triggers_type" in source
        assert "ck_runs_trigger_type" in source
        assert "'ongoing'" in source
        assert "'slack_app_mention'" in source

    def test_0008_creates_ongoing_partial_checks(self, migration_0008: ModuleType) -> None:
        source = _source(migration_0008)
        assert "ck_triggers_ongoing_spend_limit" in source
        assert "ck_triggers_ongoing_target_range" in source

    def test_0008_creates_trigger_id_indexes(self, migration_0008: ModuleType) -> None:
        source = _source(migration_0008)
        assert "ix_runs_trigger_id_status" in source
        assert "ix_runs_trigger_id_created_at" in source

    def test_0008_creates_streak_engine_partial_index(self, migration_0008: ModuleType) -> None:
        source = _source(migration_0008)
        assert "ix_runs_unclassified_terminal" in source

    def test_0008_adds_raw_output_markers_column(self, migration_0008: ModuleType) -> None:
        source = _source(migration_0008)
        assert 'ADD COLUMN IF NOT EXISTS "raw_output_markers" jsonb' in source

    def test_0008_adds_run_classification_and_work_intact(self, migration_0008: ModuleType) -> None:
        source = _source(migration_0008)
        assert 'ADD COLUMN IF NOT EXISTS "run_classification" jsonb' in source
        assert 'ADD COLUMN IF NOT EXISTS "work_intact" boolean' in source

    def test_0008_adds_observed_column_to_eval_results(self, migration_0008: ModuleType) -> None:
        source = _source(migration_0008)
        assert 'ADD COLUMN IF NOT EXISTS "observed" boolean DEFAULT false' in source

    def test_0008_widens_eval_type_check_with_guardrail(self, migration_0008: ModuleType) -> None:
        source = _source(migration_0008)
        assert "ck_eval_definitions_type" in source
        assert "'custom_function'" in source
        assert "'guardrail'" in source


def _source(module: ModuleType) -> str:
    path = _VERSIONS_DIR / f"{module.revision}.py"
    return path.read_text(encoding="utf-8")


class TestOrmCheckDriftGuard:
    def test_trigger_orm_check_includes_ongoing(self) -> None:
        from sqlalchemy import CheckConstraint

        from modulo.db.models.trigger import Trigger

        checks = [c for c in Trigger.__table_args__ if isinstance(c, CheckConstraint)]
        names = {c.name for c in checks}
        assert "ck_triggers_type" in names
        triggers_check = next(c for c in checks if c.name == "ck_triggers_type")
        assert "ongoing" in triggers_check.sqltext.text

    def test_run_orm_check_includes_ongoing(self) -> None:
        from sqlalchemy import CheckConstraint

        from modulo.db.models.run import Run

        checks = [c for c in Run.__table_args__ if isinstance(c, CheckConstraint)]
        names = {c.name for c in checks}
        assert "ck_runs_trigger_type" in names
        runs_check = next(c for c in checks if c.name == "ck_runs_trigger_type")
        assert "ongoing" in runs_check.sqltext.text

    def test_orm_partial_ongoing_checks_present(self) -> None:
        from sqlalchemy import CheckConstraint

        from modulo.db.models.trigger import Trigger

        checks = {c.name: c.sqltext.text for c in Trigger.__table_args__ if isinstance(c, CheckConstraint)}
        assert "ck_triggers_ongoing_spend_limit" in checks
        assert "ck_triggers_ongoing_target_range" in checks
        # Drift guard: the ORM partial CHECK strings must match the migration.
        assert checks["ck_triggers_ongoing_spend_limit"] == _SPEND_PARTIAL
        assert checks["ck_triggers_ongoing_target_range"] == _TARGET_PARTIAL

    def test_ongoing_status_set_matches_run_check(self) -> None:
        from sqlalchemy import CheckConstraint

        from modulo.db.models.run import ONGOING_ACTIVE_STATUSES, Run

        status_check = next(
            c for c in Run.__table_args__ if isinstance(c, CheckConstraint) and c.name == "ck_runs_status"
        )
        assert all(status in status_check.sqltext.text for status in ONGOING_ACTIVE_STATUSES)
